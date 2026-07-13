from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.package_service import PackageInput, create_package, generate_package_id
from app.utils import database


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "cutler_research_test.db"
    database.initialize_database(db_path)
    return db_path


def test_database_initialization_creates_packages_table(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'packages'"
        ).fetchone()
    assert table is not None


def test_package_insertion_and_retrieval(temp_db: Path) -> None:
    package = create_package(
        PackageInput(
            ticker="qxo",
            security_type="Common Equity",
            research_cutoff_date=date.today(),
            filing_history_years=3,
            analyst_notes="Initial setup note",
        ),
        db_path=temp_db,
    )

    loaded = database.get_package_by_package_id(package["package_id"], db_path=temp_db)
    assert loaded is not None
    assert loaded["ticker"] == "QXO"
    assert loaded["status"] == config.STATUS_DRAFT
    assert loaded["analyst_notes"] == "Initial setup note"


def test_unique_package_id_generation() -> None:
    first = generate_package_id("QXO")
    second = generate_package_id("QXO")
    assert first.startswith("CRAI-QXO-")
    assert second.startswith("CRAI-QXO-")
    assert first != second


def test_package_listing_and_count(temp_db: Path) -> None:
    create_package(
        PackageInput("QXO", "Common Equity", date.today(), 3, ""),
        db_path=temp_db,
    )
    create_package(
        PackageInput("GOOGL", "Common Equity", date.today(), 2, ""),
        db_path=temp_db,
    )

    packages = database.list_packages(db_path=temp_db)
    counts = database.count_packages_by_status(db_path=temp_db)

    assert len(packages) == 2
    assert counts[config.STATUS_DRAFT] == 2
    assert database.count_all_packages(db_path=temp_db) == 2


def test_required_fields_are_validated(temp_db: Path) -> None:
    with pytest.raises(ValueError):
        create_package(
            PackageInput("", "Common Equity", date.today(), 3, ""),
            db_path=temp_db,
        )


def test_multiple_packages_for_same_ticker_are_allowed(temp_db: Path) -> None:
    first = create_package(
        PackageInput("QXO", "Common Equity", date.today(), 3, "first"),
        db_path=temp_db,
    )
    second = create_package(
        PackageInput("QXO", "Common Equity", date.today(), 5, "second"),
        db_path=temp_db,
    )

    packages = database.list_packages_by_ticker("QXO", db_path=temp_db)
    assert len(packages) == 2
    assert first["package_id"] != second["package_id"]


def test_analyst_notes_are_stored_without_sql_concatenation_risk(temp_db: Path) -> None:
    note = "O'Brien thesis; DROP TABLE packages;"
    package = create_package(
        PackageInput("BF-B", "Common Equity", date.today(), 1, note),
        db_path=temp_db,
    )
    loaded = database.get_package_by_package_id(package["package_id"], db_path=temp_db)

    assert loaded is not None
    assert loaded["analyst_notes"] == note
    assert database.count_all_packages(db_path=temp_db) == 1
