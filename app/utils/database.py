from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.config import DATABASE_PATH, ensure_directories

logger = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Raised when the application database operation cannot be completed."""


def utc_now_iso() -> str:
    """Return a consistently formatted UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@contextmanager
def get_connection(db_path: Path | str = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with row dictionaries enabled."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        logger.exception("SQLite operation failed")
        raise DatabaseError("The research database could not complete the request.") from exc
    finally:
        connection.close()


def initialize_database(db_path: Path | str = DATABASE_PATH) -> None:
    """Create and safely upgrade the application database schema."""
    ensure_directories()
    with get_connection(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS packages (
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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_packages_ticker ON packages (ticker)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_packages_status ON packages (status)")
        _ensure_package_columns(connection)
        _create_phase2_tables(connection)
        _ensure_document_columns(connection)
        _create_phase3_tables(connection)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _ensure_package_columns(connection: sqlite3.Connection) -> None:
    """Add Phase 2 package metadata columns without touching existing data."""
    columns = _table_columns(connection, "packages")
    additions = {
        "cik": "TEXT",
        "exchange": "TEXT",
        "sic": "TEXT",
        "industry_description": "TEXT",
        "fiscal_year_end": "TEXT",
        "sec_company_url": "TEXT",
        "resolution_status": "TEXT",
        "resolution_source": "TEXT",
        "resolution_timestamp": "TEXT",
        "last_collection_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE packages ADD COLUMN {column} {definition}")


def _create_phase2_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            category TEXT NOT NULL,
            document_type TEXT NOT NULL,
            title TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_domain TEXT,
            accession_number TEXT,
            form_type TEXT,
            publication_date TEXT,
            report_period TEXT,
            local_filename TEXT,
            local_path TEXT,
            mime_type TEXT,
            file_size_bytes INTEGER,
            sha256_hash TEXT,
            collection_method TEXT NOT NULL,
            collection_status TEXT NOT NULL,
            is_public INTEGER NOT NULL DEFAULT 1,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            documents_discovered INTEGER NOT NULL DEFAULT 0,
            documents_downloaded INTEGER NOT NULL DEFAULT 0,
            documents_skipped INTEGER NOT NULL DEFAULT 0,
            documents_failed INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_documents_package_id ON documents (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_documents_ticker ON documents (ticker)",
        "CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents (sha256_hash)",
        "CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents (source_url)",
        "CREATE INDEX IF NOT EXISTS idx_documents_accession ON documents (accession_number)",
        "CREATE INDEX IF NOT EXISTS idx_collection_runs_package_id ON collection_runs (package_id)",
    ):
        connection.execute(sql)


def _ensure_document_columns(connection: sqlite3.Connection) -> None:
    """Add Phase 3 upload metadata columns without altering existing records."""
    columns = _table_columns(connection, "documents")
    additions = {
        "original_filename": "TEXT",
        "stored_filename": "TEXT",
        "file_extension": "TEXT",
        "detected_file_type": "TEXT",
        "source_type": "TEXT",
        "source_institution": "TEXT",
        "suggested_category_code": "TEXT",
        "suggested_category": "TEXT",
        "suggested_confidence": "TEXT",
        "final_category_code": "TEXT",
        "classification_method": "TEXT",
        "classification_rules_matched": "TEXT",
        "document_title": "TEXT",
        "document_date": "TEXT",
        "upload_method": "TEXT",
        "uploaded_by": "TEXT",
        "analyst_notes": "TEXT",
        "authorization_confirmed": "INTEGER NOT NULL DEFAULT 0",
        "upload_status": "TEXT",
        "archive_origin_document_id": "TEXT",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")


def _create_phase3_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            number_selected INTEGER NOT NULL DEFAULT 0,
            number_uploaded INTEGER NOT NULL DEFAULT 0,
            number_duplicated INTEGER NOT NULL DEFAULT 0,
            number_skipped INTEGER NOT NULL DEFAULT 0,
            number_failed INTEGER NOT NULL DEFAULT 0,
            total_bytes_uploaded INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error_summary TEXT,
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS package_checklist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checklist_item_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            category_code TEXT NOT NULL,
            display_name TEXT NOT NULL,
            requirement_level TEXT NOT NULL,
            checklist_group TEXT NOT NULL,
            applicability TEXT NOT NULL,
            automatic_status TEXT NOT NULL,
            analyst_override_status TEXT,
            effective_status TEXT NOT NULL,
            analyst_note TEXT,
            matched_document_count INTEGER NOT NULL DEFAULT 0,
            latest_document_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (package_id, checklist_item_id),
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_checklist_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            checklist_item_id TEXT NOT NULL,
            link_method TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (package_id, document_id, checklist_item_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            document_id TEXT,
            event_type TEXT NOT NULL,
            event_details_json TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_upload_runs_package_id ON upload_runs (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_checklist_package_id ON package_checklist_items (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_checklist_package_id ON document_checklist_links (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_package_id ON audit_events (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_documents_final_category ON documents (final_category_code)",
        "CREATE INDEX IF NOT EXISTS idx_documents_is_public ON documents (is_public)",
    ):
        connection.execute(sql)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a SQLite row to a regular dictionary."""
    return dict(row) if row is not None else None


def create_package_record(
    *,
    package_id: str,
    ticker: str,
    company_name: str | None,
    security_type: str,
    status: str,
    research_cutoff_date: str,
    filing_history_years: int,
    analyst_notes: str,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    """Insert and return a package record."""
    now = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO packages (
                package_id, ticker, company_name, security_type, status,
                research_cutoff_date, filing_history_years, analyst_notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package_id,
                ticker,
                company_name,
                security_type,
                status,
                research_cutoff_date,
                filing_history_years,
                analyst_notes,
                now,
                now,
            ),
        )
    record = get_package_by_package_id(package_id, db_path=db_path)
    if record is None:
        raise DatabaseError("The research package was created but could not be reloaded.")
    return record


def list_packages(
    *,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Return packages ordered by most recently updated."""
    initialize_database(db_path)
    sql = "SELECT * FROM packages ORDER BY updated_at DESC, created_at DESC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_package_by_package_id(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Return one package by its human-readable package id."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM packages WHERE package_id = ?",
            (package_id,),
        ).fetchone()
    return row_to_dict(row)


def list_packages_by_ticker(
    ticker: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Return all packages for a normalized ticker."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM packages
            WHERE ticker = ?
            ORDER BY created_at DESC
            """,
            (ticker,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_packages_by_status(*, db_path: Path | str = DATABASE_PATH) -> dict[str, int]:
    """Return package counts keyed by status."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM packages GROUP BY status"
        ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def count_all_packages(*, db_path: Path | str = DATABASE_PATH) -> int:
    """Return the total number of package records."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM packages").fetchone()
    return int(row["count"])


def update_package_status(
    package_id: str,
    status: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update a package status and timestamp, returning the refreshed record."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET status = ?, updated_at = ?
            WHERE package_id = ?
            """,
            (status, utc_now_iso(), package_id),
        )
    return get_package_by_package_id(package_id, db_path=db_path)


def update_package_company_metadata(
    package_id: str,
    metadata: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Persist resolved public-company metadata on a package."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET ticker = ?, company_name = ?, cik = ?, exchange = ?, sic = ?,
                industry_description = ?, fiscal_year_end = ?, sec_company_url = ?,
                resolution_status = ?, resolution_source = ?, resolution_timestamp = ?,
                updated_at = ?
            WHERE package_id = ?
            """,
            (
                metadata.get("ticker"),
                metadata.get("company_name"),
                metadata.get("cik"),
                metadata.get("exchange"),
                metadata.get("sic"),
                metadata.get("industry_description"),
                metadata.get("fiscal_year_end"),
                metadata.get("sec_company_url"),
                metadata.get("resolution_status"),
                metadata.get("resolution_source"),
                metadata.get("resolution_timestamp"),
                utc_now_iso(),
                package_id,
            ),
        )
    return get_package_by_package_id(package_id, db_path=db_path)


def update_package_collection_state(
    package_id: str,
    status: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update package collection status and last collection timestamp."""
    now = utc_now_iso()
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET status = ?, last_collection_at = ?, updated_at = ?
            WHERE package_id = ?
            """,
            (status, now, now, package_id),
        )
    return get_package_by_package_id(package_id, db_path=db_path)


def create_collection_run(
    *,
    run_id: str,
    package_id: str,
    source_type: str,
    status: str,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    """Create a collection run record."""
    initialize_database(db_path)
    started_at = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO collection_runs (run_id, package_id, source_type, started_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, package_id, source_type, started_at, status),
        )
    return get_collection_run(run_id, db_path=db_path) or {}


def update_collection_run(
    run_id: str,
    *,
    status: str,
    documents_discovered: int = 0,
    documents_downloaded: int = 0,
    documents_skipped: int = 0,
    documents_failed: int = 0,
    error_summary: str | None = None,
    completed: bool = True,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update a collection run summary."""
    initialize_database(db_path)
    completed_at = utc_now_iso() if completed else None
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE collection_runs
            SET completed_at = ?, status = ?, documents_discovered = ?,
                documents_downloaded = ?, documents_skipped = ?, documents_failed = ?,
                error_summary = ?
            WHERE run_id = ?
            """,
            (
                completed_at,
                status,
                documents_discovered,
                documents_downloaded,
                documents_skipped,
                documents_failed,
                error_summary,
                run_id,
            ),
        )
    return get_collection_run(run_id, db_path=db_path)


def get_collection_run(run_id: str, *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM collection_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return row_to_dict(row)


def list_recent_collection_runs(
    package_id: str,
    *,
    limit: int = 10,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM collection_runs
            WHERE package_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (package_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_document_record(
    document: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    """Insert and return a document record."""
    initialize_database(db_path)
    now = utc_now_iso()
    fields = (
        "document_id",
        "package_id",
        "ticker",
        "category",
        "document_type",
        "title",
        "source_name",
        "source_url",
        "source_domain",
        "accession_number",
        "form_type",
        "publication_date",
        "report_period",
        "local_filename",
        "local_path",
        "mime_type",
        "file_size_bytes",
        "sha256_hash",
        "collection_method",
        "collection_status",
        "is_public",
        "error_message",
        "original_filename",
        "stored_filename",
        "file_extension",
        "detected_file_type",
        "source_type",
        "source_institution",
        "suggested_category_code",
        "suggested_category",
        "suggested_confidence",
        "final_category_code",
        "classification_method",
        "classification_rules_matched",
        "document_title",
        "document_date",
        "upload_method",
        "uploaded_by",
        "analyst_notes",
        "authorization_confirmed",
        "upload_status",
        "archive_origin_document_id",
        "created_at",
        "updated_at",
    )
    prepared = dict(document)
    prepared.setdefault("is_public", True)
    prepared["is_public"] = int(bool(prepared["is_public"]))
    prepared.setdefault("authorization_confirmed", False)
    prepared["authorization_confirmed"] = int(bool(prepared["authorization_confirmed"]))
    prepared["created_at"] = now
    prepared["updated_at"] = now
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO documents (
                document_id, package_id, ticker, category, document_type, title,
                source_name, source_url, source_domain, accession_number, form_type,
                publication_date, report_period, local_filename, local_path,
                mime_type, file_size_bytes, sha256_hash, collection_method,
                collection_status, is_public, error_message, original_filename,
                stored_filename, file_extension, detected_file_type, source_type,
                source_institution, suggested_category_code, suggested_category,
                suggested_confidence, final_category_code, classification_method,
                classification_rules_matched, document_title, document_date,
                upload_method, uploaded_by, analyst_notes, authorization_confirmed,
                upload_status, archive_origin_document_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(prepared.get(field) for field in fields),
        )
    return get_document_by_document_id(document["document_id"], db_path=db_path) or {}


def get_document_by_document_id(
    document_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    return row_to_dict(row)


def update_document_status(
    document_id: str,
    status: str,
    *,
    error_message: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE documents
            SET collection_status = ?, error_message = ?, updated_at = ?
            WHERE document_id = ?
            """,
            (status, error_message, utc_now_iso(), document_id),
        )
    return get_document_by_document_id(document_id, db_path=db_path)


def list_documents_by_package(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM documents
            WHERE package_id = ?
            ORDER BY COALESCE(publication_date, created_at) DESC
            """,
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_documents_by_category(
    package_id: str,
    category: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM documents WHERE package_id = ? AND category = ?",
            (package_id, category),
        ).fetchall()
    return [dict(row) for row in rows]


def document_exists_by_accession(
    package_id: str,
    accession_number: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    if not accession_number:
        return False
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM documents
            WHERE package_id = ? AND accession_number = ?
            LIMIT 1
            """,
            (package_id, accession_number),
        ).fetchone()
    return row is not None


def document_exists_by_url(
    package_id: str,
    source_url: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM documents WHERE package_id = ? AND source_url = ? LIMIT 1",
            (package_id, source_url),
        ).fetchone()
    return row is not None


def document_exists_by_hash(
    package_id: str,
    sha256_hash: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM documents
            WHERE package_id = ? AND sha256_hash = ?
            LIMIT 1
            """,
            (package_id, sha256_hash),
        ).fetchone()
    return row is not None


def count_documents_by_status_and_category(
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT collection_status, category, COUNT(*) AS count
            FROM documents
            GROUP BY collection_status, category
            """
        ).fetchall()
    return [dict(row) for row in rows]


def count_documents_for_package(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM documents
            WHERE package_id = ? AND collection_status = 'DOWNLOADED'
            """,
            (package_id,),
        ).fetchone()
    return int(row["count"])


def delete_incomplete_document(
    document_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            DELETE FROM documents
            WHERE document_id = ? AND collection_status IN ('FAILED', 'DISCOVERED')
            """,
            (document_id,),
        )


def dashboard_public_collection_metrics(
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        docs = connection.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE collection_status = 'DOWNLOADED'"
        ).fetchone()
        resolved = connection.execute(
            "SELECT COUNT(*) AS count FROM packages WHERE resolution_status = 'RESOLVED'"
        ).fetchone()
        failed = connection.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE collection_status = 'FAILED'"
        ).fetchone()
    return {
        "public_documents": int(docs["count"]),
        "resolved_packages": int(resolved["count"]),
        "failed_items": int(failed["count"]),
    }


def create_upload_run(
    *,
    run_id: str,
    package_id: str,
    number_selected: int,
    status: str,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO upload_runs (run_id, package_id, started_at, number_selected, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, package_id, utc_now_iso(), number_selected, status),
        )
    return get_upload_run(run_id, db_path=db_path) or {}


def update_upload_run(
    run_id: str,
    *,
    status: str,
    number_uploaded: int = 0,
    number_duplicated: int = 0,
    number_skipped: int = 0,
    number_failed: int = 0,
    total_bytes_uploaded: int = 0,
    error_summary: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE upload_runs
            SET completed_at = ?, status = ?, number_uploaded = ?,
                number_duplicated = ?, number_skipped = ?, number_failed = ?,
                total_bytes_uploaded = ?, error_summary = ?
            WHERE run_id = ?
            """,
            (
                utc_now_iso(),
                status,
                number_uploaded,
                number_duplicated,
                number_skipped,
                number_failed,
                total_bytes_uploaded,
                error_summary,
                run_id,
            ),
        )
    return get_upload_run(run_id, db_path=db_path)


def get_upload_run(run_id: str, *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM upload_runs WHERE run_id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def list_recent_upload_runs(
    package_id: str,
    *,
    limit: int = 10,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM upload_runs
            WHERE package_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (package_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_audit_event(
    *,
    event_id: str,
    package_id: str,
    event_type: str,
    document_id: str | None = None,
    event_details_json: str | None = None,
    actor: str = "analyst",
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, package_id, document_id, event_type,
                event_details_json, actor, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                package_id,
                document_id,
                event_type,
                event_details_json,
                actor,
                utc_now_iso(),
            ),
        )
    return get_audit_event(event_id, db_path=db_path) or {}


def get_audit_event(event_id: str, *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM audit_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return row_to_dict(row)


def list_audit_events(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM audit_events WHERE package_id = ? ORDER BY created_at DESC",
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_checklist_item(
    item: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    now = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO package_checklist_items (
                checklist_item_id, package_id, category_code, display_name,
                requirement_level, checklist_group, applicability, automatic_status,
                analyst_override_status, effective_status, analyst_note,
                matched_document_count, latest_document_date, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id, checklist_item_id) DO UPDATE SET
                category_code = excluded.category_code,
                display_name = excluded.display_name,
                requirement_level = excluded.requirement_level,
                checklist_group = excluded.checklist_group,
                applicability = excluded.applicability,
                automatic_status = excluded.automatic_status,
                effective_status = COALESCE(package_checklist_items.analyst_override_status, excluded.automatic_status),
                matched_document_count = excluded.matched_document_count,
                latest_document_date = excluded.latest_document_date,
                updated_at = excluded.updated_at
            """,
            (
                item["checklist_item_id"],
                item["package_id"],
                item["category_code"],
                item["display_name"],
                item["requirement_level"],
                item["checklist_group"],
                item.get("applicability", "APPLICABLE"),
                item["automatic_status"],
                item.get("analyst_override_status"),
                item["effective_status"],
                item.get("analyst_note"),
                item.get("matched_document_count", 0),
                item.get("latest_document_date"),
                now,
                now,
            ),
        )
    return get_checklist_item(item["package_id"], item["checklist_item_id"], db_path=db_path) or {}


def get_checklist_item(
    package_id: str,
    checklist_item_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM package_checklist_items
            WHERE package_id = ? AND checklist_item_id = ?
            """,
            (package_id, checklist_item_id),
        ).fetchone()
    return row_to_dict(row)


def list_checklist_items(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM package_checklist_items
            WHERE package_id = ?
            ORDER BY checklist_group, requirement_level, display_name
            """,
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_checklist_override(
    package_id: str,
    checklist_item_id: str,
    override_status: str | None,
    note: str | None,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        if override_status:
            connection.execute(
                """
                UPDATE package_checklist_items
                SET analyst_override_status = ?, effective_status = ?,
                    analyst_note = ?, updated_at = ?
                WHERE package_id = ? AND checklist_item_id = ?
                """,
                (override_status, override_status, note, utc_now_iso(), package_id, checklist_item_id),
            )
        else:
            connection.execute(
                """
                UPDATE package_checklist_items
                SET analyst_override_status = NULL, effective_status = automatic_status,
                    analyst_note = ?, updated_at = ?
                WHERE package_id = ? AND checklist_item_id = ?
                """,
                (note, utc_now_iso(), package_id, checklist_item_id),
            )
    return get_checklist_item(package_id, checklist_item_id, db_path=db_path)


def replace_document_checklist_links(
    package_id: str,
    links: list[dict[str, str]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> None:
    initialize_database(db_path)
    now = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute("DELETE FROM document_checklist_links WHERE package_id = ?", (package_id,))
        connection.executemany(
            """
            INSERT OR IGNORE INTO document_checklist_links (
                package_id, document_id, checklist_item_id, link_method, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    package_id,
                    link["document_id"],
                    link["checklist_item_id"],
                    link.get("link_method", "CATEGORY"),
                    now,
                )
                for link in links
            ],
        )


def update_document_metadata(
    document_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "title",
        "category",
        "source_institution",
        "publication_date",
        "document_date",
        "analyst_notes",
        "final_category_code",
        "document_title",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_document_by_document_id(document_id, db_path=db_path)
    selected["updated_at"] = utc_now_iso()
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE documents SET {sql} WHERE document_id = ?",
            (*selected.values(), document_id),
        )
    return get_document_by_document_id(document_id, db_path=db_path)


def mark_document_deleted(
    document_id: str,
    *,
    actor: str = "analyst",
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE documents
            SET collection_status = 'DELETED', upload_status = 'DELETED',
                deleted_at = ?, deleted_by = ?, updated_at = ?
            WHERE document_id = ?
            """,
            (utc_now_iso(), actor, utc_now_iso(), document_id),
        )
    return get_document_by_document_id(document_id, db_path=db_path)


def phase3_dashboard_metrics(*, db_path: Path | str = DATABASE_PATH) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        licensed = connection.execute(
            """
            SELECT COUNT(*) AS count FROM documents
            WHERE is_public = 0 AND collection_status = 'DOWNLOADED'
            """
        ).fetchone()
        needing_review = connection.execute(
            """
            SELECT COUNT(DISTINCT package_id) AS count
            FROM package_checklist_items
            WHERE effective_status IN ('MISSING', 'NEEDS_REVIEW', 'STALE')
            """
        ).fetchone()
        missing_core = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM package_checklist_items
            WHERE requirement_level = 'required' AND effective_status = 'MISSING'
            """
        ).fetchone()
    return {
        "licensed_documents": int(licensed["count"]),
        "packages_needing_review": int(needing_review["count"]),
        "missing_core_items": int(missing_core["count"]),
    }


def document_counts_for_package(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT is_public, collection_status, COUNT(*) AS count, COALESCE(SUM(file_size_bytes), 0) AS bytes
            FROM documents
            WHERE package_id = ? AND collection_status != 'DELETED'
            GROUP BY is_public, collection_status
            """,
            (package_id,),
        ).fetchall()
    result = {"public": 0, "licensed": 0, "duplicates": 0, "failed": 0, "total": 0, "bytes": 0}
    for row in rows:
        count = int(row["count"])
        status = row["collection_status"]
        result["total"] += count
        result["bytes"] += int(row["bytes"] or 0)
        if status == "DUPLICATE":
            result["duplicates"] += count
        if status == "FAILED":
            result["failed"] += count
        if status == "DOWNLOADED":
            if int(row["is_public"]):
                result["public"] += count
            else:
                result["licensed"] += count
    return result
