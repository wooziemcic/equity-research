from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.package_service import PackageInput, create_package
from app.services.workspace_service import (
    WorkspaceError,
    atomic_write_bytes,
    package_workspace,
    sanitize_filename,
    safe_document_path,
    write_metadata_json,
)
from app.utils import database


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "phase2.db"
    database.initialize_database(db_path)
    return db_path


@pytest.fixture()
def package(temp_db: Path) -> dict:
    return create_package(
        PackageInput("QXO", "Common Equity", date.today(), 3, ""),
        db_path=temp_db,
    )


def test_existing_phase1_database_upgrades_successfully(tmp_path: Path) -> None:
    db_path = tmp_path / "phase1.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                company_name TEXT,
                security_type TEXT NOT NULL,
                status TEXT NOT NULL,
                research_cutoff_date TEXT NOT NULL,
                filing_history_years INTEGER NOT NULL,
                analyst_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    database.initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(packages)")
        }
        document_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(documents)")
        }
    assert {"packages", "documents", "collection_runs"} <= tables
    assert {"cik", "resolution_status", "last_collection_at"} <= columns
    assert "source_identity_key" in document_columns
    assert "idx_documents_package_source_identity_current" in indexes


def test_document_insertion_duplicate_checks_and_status_updates(temp_db: Path, package: dict) -> None:
    document = {
        "document_id": "DOC-1",
        "package_id": package["package_id"],
        "ticker": "QXO",
        "category": "SEC Filing",
        "document_type": "10-K",
        "title": "QXO 10-K",
        "source_name": "SEC EDGAR",
        "source_url": "https://www.sec.gov/a",
        "source_domain": "sec.gov",
        "accession_number": "0001",
        "sha256_hash": "abc",
        "collection_method": "SEC",
        "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
    }
    database.create_document_record(document, db_path=temp_db)
    assert database.document_exists_by_accession(package["package_id"], "0001", db_path=temp_db)
    assert database.document_exists_by_url(package["package_id"], "https://www.sec.gov/a", db_path=temp_db)
    assert database.document_exists_by_hash(package["package_id"], "abc", db_path=temp_db)
    database.update_document_status("DOC-1", config.DOCUMENT_STATUS_FAILED, error_message="boom", db_path=temp_db)
    loaded = database.get_document_by_document_id("DOC-1", db_path=temp_db)
    assert loaded["collection_status"] == config.DOCUMENT_STATUS_FAILED
    assert database.list_documents_by_category(package["package_id"], "SEC Filing", db_path=temp_db)


def test_collection_runs_and_package_document_counts(temp_db: Path, package: dict) -> None:
    database.create_collection_run(
        run_id="RUN-1",
        package_id=package["package_id"],
        source_type="SEC",
        status=config.COLLECTION_STATUS_RUNNING,
        db_path=temp_db,
    )
    database.update_collection_run(
        "RUN-1",
        status=config.COLLECTION_STATUS_COMPLETE,
        documents_discovered=1,
        documents_downloaded=1,
        db_path=temp_db,
    )
    runs = database.list_recent_collection_runs(package["package_id"], db_path=temp_db)
    assert runs[0]["documents_downloaded"] == 1


def test_workspace_filename_sanitization_and_atomic_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    assert sanitize_filename("QXO 10-K:/bad?.html") == "QXO_10-K_bad_.html"
    root = package_workspace("CRAI-QXO-1")
    assert (root / "sec").exists()
    path = safe_document_path("CRAI-QXO-1", "sec", "../safe.html")
    atomic_write_bytes(path, b"hello")
    assert path.read_bytes() == b"hello"
    metadata = write_metadata_json("CRAI-QXO-1", "snapshot.json", {"ok": True})
    assert metadata.exists()


def test_path_traversal_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    with pytest.raises(WorkspaceError):
        package_workspace("../bad")
