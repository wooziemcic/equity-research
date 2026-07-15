from __future__ import annotations

import hashlib
import logging
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from app import config
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
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        logger.exception("SQLite operation failed")
        raise DatabaseError("The research database could not complete the request.") from exc
    finally:
        connection.close()


def initialize_database(db_path: Path | str = DATABASE_PATH) -> None:
    """Create and safely upgrade the application database schema."""
    ensure_directories()
    _backup_before_phase6a_migration(db_path)
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
        _ensure_collection_run_columns(connection)
        _ensure_document_columns(connection)
        _create_phase3_tables(connection)
        _ensure_phase4_package_columns(connection)
        _create_phase4_tables(connection)
        _ensure_phase4_version_columns(connection)
        _create_phase2_stabilization_schema(connection)
        _create_phase5_tables(connection)
        _create_phase6_tables(connection)
        _create_phase7_tables(connection)
        _create_phase3_official_ir_schema(connection)
        _create_phase4_final_schema(connection)
        _create_phase5_memo_schema(connection)
        _create_phase51_stabilization_schema(connection)
        _create_phase6a_recipe_schema(connection)


def _backup_before_phase6a_migration(db_path: Path | str) -> Path | None:
    """Back up the configured development database once before the Phase 6A migration."""
    path = Path(db_path).resolve()
    configured = Path(DATABASE_PATH).resolve()
    if path != configured or config.DATABASE_ENVIRONMENT != "DEVELOPMENT" or not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as probe:
            exists = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='package_recipes'"
            ).fetchone()
        if exists:
            return None
    except sqlite3.Error:
        pass
    backup_dir = config.MIGRATION_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    recent = sorted(backup_dir.glob("cutler_research_pre_phase6a_*.db"), key=lambda item: item.stat().st_mtime)
    if recent and datetime.now(UTC).timestamp() - recent[-1].stat().st_mtime < 24 * 60 * 60:
        return recent[-1]
    destination = backup_dir / f"cutler_research_pre_phase6a_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    shutil.copy2(path, destination)
    return destination


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
            source_identity_key TEXT,
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
            documents_already_collected INTEGER NOT NULL DEFAULT 0,
            documents_duplicated INTEGER NOT NULL DEFAULT 0,
            documents_not_found INTEGER NOT NULL DEFAULT 0,
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
        "source_identity_key": "TEXT",
        "deleted_at": "TEXT",
        "deleted_by": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_source_identity ON documents (source_identity_key)")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_package_source_identity_current
        ON documents(package_id, source_identity_key)
        WHERE source_identity_key IS NOT NULL
          AND collection_status IN ('DISCOVERED', 'DOWNLOADED')
        """
    )


def _ensure_collection_run_columns(connection: sqlite3.Connection) -> None:
    """Add distinct collection result counters without rewriting history."""
    columns = _table_columns(connection, "collection_runs")
    for column in ("documents_already_collected", "documents_duplicated", "documents_not_found"):
        if column not in columns:
            connection.execute(f"ALTER TABLE collection_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")


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


def _ensure_phase4_package_columns(connection: sqlite3.Connection) -> None:
    """Add package-level checklist review fields for Phase 4."""
    columns = _table_columns(connection, "packages")
    additions = {
        "checklist_reviewed": "INTEGER NOT NULL DEFAULT 0",
        "reviewed_by": "TEXT",
        "reviewed_timestamp": "TEXT",
        "review_note": "TEXT",
        "missing_core_acknowledged": "INTEGER NOT NULL DEFAULT 0",
        "stale_documents_acknowledged": "INTEGER NOT NULL DEFAULT 0",
        "needs_review_acknowledged": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE packages ADD COLUMN {column} {definition}")


def _create_phase4_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS package_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL UNIQUE,
            display_version TEXT NOT NULL,
            parent_package_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            previous_version_id TEXT,
            ticker TEXT NOT NULL,
            company_name TEXT,
            security_type TEXT NOT NULL,
            research_cutoff_date TEXT NOT NULL,
            status TEXT NOT NULL,
            document_count INTEGER NOT NULL DEFAULT 0,
            public_document_count INTEGER NOT NULL DEFAULT 0,
            licensed_document_count INTEGER NOT NULL DEFAULT 0,
            total_size_bytes INTEGER NOT NULL DEFAULT 0,
            checklist_snapshot_json TEXT,
            manifest_path TEXT,
            manifest_sha256 TEXT,
            inventory_path TEXT,
            checklist_report_path TEXT,
            integrity_report_path TEXT,
            integrity_status TEXT,
            zip_path TEXT,
            zip_sha256 TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            locked_at TEXT,
            notes TEXT,
            error_message TEXT,
            UNIQUE(parent_package_id, version_number),
            FOREIGN KEY (parent_package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS package_version_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            original_document_id TEXT NOT NULL,
            category TEXT,
            title TEXT,
            source_name TEXT,
            source_url TEXT,
            publication_date TEXT,
            original_filename TEXT,
            package_filename TEXT,
            relative_package_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256_hash TEXT NOT NULL,
            mime_type TEXT,
            is_public INTEGER NOT NULL,
            included_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (version_id) REFERENCES package_versions(version_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS package_version_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            version_id TEXT,
            parent_package_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_details_json TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_package_versions_parent ON package_versions (parent_package_id)",
        "CREATE INDEX IF NOT EXISTS idx_package_versions_status ON package_versions (status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_versions_parent_number ON package_versions (parent_package_id, version_number)",
        "CREATE INDEX IF NOT EXISTS idx_package_version_documents_version ON package_version_documents (version_id)",
        "CREATE INDEX IF NOT EXISTS idx_package_version_events_version ON package_version_events (version_id)",
        "CREATE INDEX IF NOT EXISTS idx_package_version_events_parent ON package_version_events (parent_package_id)",
    ):
        connection.execute(sql)


def _ensure_phase4_version_columns(connection: sqlite3.Connection) -> None:
    """Add the human-readable version label while preserving existing rows."""
    columns = _table_columns(connection, "package_versions")
    if "display_version" not in columns:
        connection.execute("ALTER TABLE package_versions ADD COLUMN display_version TEXT")
    rows = connection.execute(
        "SELECT version_id, ticker, research_cutoff_date, version_number FROM package_versions WHERE display_version IS NULL OR display_version = ''"
    ).fetchall()
    for row in rows:
        ticker = "".join(character if character.isalnum() else "-" for character in str(row["ticker"] or "").upper()).strip("-") or "PACKAGE"
        cutoff = str(row["research_cutoff_date"] or "").replace("-", "")
        display_version = f"{ticker}-{cutoff}-V{int(row['version_number']):03d}"
        connection.execute(
            "UPDATE package_versions SET display_version = ? WHERE version_id = ?",
            (display_version, row["version_id"]),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_versions_parent_number ON package_versions (parent_package_id, version_number)"
    )


def _create_phase2_stabilization_schema(connection: sqlite3.Connection) -> None:
    """Add the intern-guide profile and batch metadata without rewriting history."""
    package_columns = _table_columns(connection, "packages")
    for column in ("collection_profile_name", "collection_profile_snapshot_json"):
        if column not in package_columns:
            connection.execute(f"ALTER TABLE packages ADD COLUMN {column} TEXT")

    version_columns = _table_columns(connection, "package_versions")
    for column in ("collection_profile_name", "collection_profile_snapshot_json"):
        if column not in version_columns:
            connection.execute(f"ALTER TABLE package_versions ADD COLUMN {column} TEXT")

    document_columns = _table_columns(connection, "documents")
    document_additions = {
        "normalized_form_family": "TEXT",
        "parent_accession_number": "TEXT",
        "inferred_source": "TEXT",
        "source_confidence": "TEXT",
        "source_inference_reason": "TEXT",
        "analyst_corrected_source": "TEXT",
        "final_source": "TEXT",
        "inferred_category_code": "TEXT",
        "category_confidence": "TEXT",
        "category_inference_reason": "TEXT",
        "analyst_corrected_category_code": "TEXT",
        "upload_batch_id": "TEXT",
        "managed_filename": "TEXT",
    }
    for column, definition in document_additions.items():
        if column not in document_columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")

    run_columns = _table_columns(connection, "collection_runs")
    for column in ("documents_eligible", "documents_excluded_profile", "documents_awaiting_selection"):
        if column not in run_columns:
            connection.execute(f"ALTER TABLE collection_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sec_filing_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            primary_document TEXT NOT NULL,
            original_form_type TEXT NOT NULL,
            normalized_form_family TEXT,
            filing_date TEXT,
            source_url TEXT,
            inventory_status TEXT NOT NULL,
            conditional_rule TEXT,
            selected INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            discovered_at TEXT NOT NULL,
            UNIQUE(package_id, accession_number, primary_document)
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_sec_inventory_package ON sec_filing_inventory(package_id)")


def _create_phase5_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            processing_run_id TEXT NOT NULL UNIQUE,
            version_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            parser_config_version TEXT NOT NULL,
            embedding_config_json TEXT,
            ocr_config_json TEXT,
            retrieval_config_json TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            total_documents INTEGER NOT NULL DEFAULT 0,
            successful_documents INTEGER NOT NULL DEFAULT 0,
            partial_documents INTEGER NOT NULL DEFAULT 0,
            failed_documents INTEGER NOT NULL DEFAULT 0,
            pages_processed INTEGER NOT NULL DEFAULT 0,
            tables_detected INTEGER NOT NULL DEFAULT 0,
            sheets_processed INTEGER NOT NULL DEFAULT 0,
            chunks_created INTEGER NOT NULL DEFAULT 0,
            evidence_records_created INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT,
            errors_json TEXT,
            created_by TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (version_id) REFERENCES package_versions(version_id),
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_processing_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            original_document_id TEXT,
            parser_used TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            detected_language TEXT,
            page_count INTEGER NOT NULL DEFAULT 0,
            sheet_count INTEGER NOT NULL DEFAULT 0,
            extracted_character_count INTEGER NOT NULL DEFAULT 0,
            ocr_required INTEGER NOT NULL DEFAULT 0,
            ocr_pages INTEGER NOT NULL DEFAULT 0,
            table_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            extracted_content_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_record_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            page_label TEXT,
            extraction_method TEXT NOT NULL,
            native_text_character_count INTEGER NOT NULL DEFAULT 0,
            ocr_text_character_count INTEGER NOT NULL DEFAULT 0,
            ocr_confidence REAL,
            page_text_path TEXT,
            normalized_text TEXT,
            image_render_path TEXT,
            processing_warnings_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_record_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            sheet_index INTEGER NOT NULL,
            hidden_state TEXT,
            used_range TEXT,
            formula_cell_count INTEGER NOT NULL DEFAULT 0,
            cached_value_cell_count INTEGER NOT NULL DEFAULT 0,
            external_link_count INTEGER NOT NULL DEFAULT 0,
            warning_flags TEXT,
            extracted_representation_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            page_number INTEGER,
            sheet_name TEXT,
            row_range TEXT,
            section_heading TEXT,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            character_count INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL,
            extraction_method TEXT NOT NULL,
            source_locator_json TEXT NOT NULL,
            chunk_hash TEXT NOT NULL,
            duplicate_group_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            normalized_subject TEXT,
            metric_name TEXT,
            value REAL,
            unit TEXT,
            currency TEXT,
            period TEXT,
            scenario TEXT,
            direction TEXT,
            source_text TEXT NOT NULL,
            page_number INTEGER,
            sheet_name TEXT,
            cell_or_row_range TEXT,
            section_heading TEXT,
            extraction_method TEXT NOT NULL,
            confidence TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            analyst_status TEXT NOT NULL,
            analyst_note TEXT,
            source_locator_json TEXT,
            source_text_hash TEXT,
            extraction_fingerprint TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS citation_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_id TEXT NOT NULL UNIQUE,
            evidence_id TEXT NOT NULL,
            citation_locator_json TEXT NOT NULL,
            verification_method TEXT NOT NULL,
            support_status TEXT NOT NULL,
            support_score REAL NOT NULL,
            verifier_note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (evidence_id) REFERENCES evidence_records(evidence_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_duplicate_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            duplicate_group_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            duplicate_type TEXT NOT NULL,
            canonical_chunk_hash TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            member_chunk_ids_json TEXT NOT NULL,
            explanation TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conflict_id TEXT NOT NULL UNIQUE,
            processing_run_id TEXT NOT NULL,
            subject TEXT,
            metric TEXT,
            period TEXT,
            evidence_id_a TEXT NOT NULL,
            evidence_id_b TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            explanation TEXT NOT NULL,
            analyst_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_processing_runs_version ON processing_runs (version_id)",
        "CREATE INDEX IF NOT EXISTS idx_processing_runs_package ON processing_runs (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_processing_results_run ON document_processing_results (processing_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_pages_run_doc ON document_pages (processing_run_id, version_document_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_sheets_run_doc ON document_sheets (processing_run_id, version_document_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_run ON document_chunks (processing_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_version ON document_chunks (version_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_hash ON document_chunks (chunk_hash)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_records_run ON evidence_records (processing_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_records_type ON evidence_records (evidence_type)",
        "CREATE INDEX IF NOT EXISTS idx_citation_verifications_evidence ON citation_verifications (evidence_id)",
        "CREATE INDEX IF NOT EXISTS idx_duplicate_groups_run ON content_duplicate_groups (processing_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_claim_conflicts_run ON claim_conflicts (processing_run_id)",
    ):
        connection.execute(sql)
    evidence_columns = _table_columns(connection, "evidence_records")
    if "extraction_fingerprint" not in evidence_columns:
        connection.execute("ALTER TABLE evidence_records ADD COLUMN extraction_fingerprint TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_run_extraction_fingerprint "
        "ON evidence_records (processing_run_id, extraction_fingerprint) WHERE extraction_fingerprint IS NOT NULL"
    )


def _create_phase6_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_run_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            analysis_configuration_version TEXT NOT NULL,
            scorecard_version TEXT NOT NULL,
            valuation_configuration_version TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            preliminary_recommendation TEXT,
            analyst_adjusted_recommendation TEXT,
            pm_approved_recommendation TEXT,
            confidence TEXT,
            evidence_coverage REAL,
            package_coverage REAL,
            research_cutoff TEXT,
            reference_price REAL,
            reference_price_currency TEXT,
            reference_price_date TEXT,
            reference_price_evidence_id TEXT,
            time_horizon TEXT,
            analyst_notes TEXT,
            pm_notes TEXT,
            error_message TEXT,
            ai_review_status TEXT,
            ai_model TEXT,
            ai_endpoint TEXT,
            openai_diagnostics_json TEXT,
            FOREIGN KEY (version_id) REFERENCES package_versions(version_id),
            FOREIGN KEY (processing_run_id) REFERENCES processing_runs(processing_run_id)
        )
        """
    )
    columns = _table_columns(connection, "analysis_runs")
    for column in ("ai_review_status", "ai_model", "ai_endpoint", "openai_diagnostics_json"):
        if column not in columns:
            connection.execute(f"ALTER TABLE analysis_runs ADD COLUMN {column} TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            metric_code TEXT NOT NULL,
            display_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            currency TEXT,
            period TEXT,
            scenario TEXT,
            calculation_method TEXT NOT NULL,
            formula_description TEXT NOT NULL,
            source_evidence_ids_json TEXT NOT NULL,
            confidence TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            warning TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_scorecard_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            pillar_code TEXT NOT NULL,
            pillar_name TEXT NOT NULL,
            score REAL NOT NULL,
            weight REAL NOT NULL,
            weighted_score REAL NOT NULL,
            evidence_quality TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            analyst_override_score REAL,
            analyst_override_rationale TEXT,
            effective_score REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            scenario_name TEXT NOT NULL,
            scenario_assumptions_json TEXT NOT NULL,
            revenue_assumption TEXT,
            margin_assumption TEXT,
            earnings_assumption TEXT,
            multiple_assumption TEXT,
            implied_value REAL,
            reference_price REAL,
            upside_downside REAL,
            probability REAL,
            evidence_ids_json TEXT NOT NULL,
            analyst_overrides_json TEXT,
            warnings_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_thesis_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thesis_item_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            claim TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            citation_status TEXT NOT NULL,
            confidence TEXT NOT NULL,
            analyst_status TEXT NOT NULL,
            source_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            preliminary_rating TEXT NOT NULL,
            effective_rating TEXT NOT NULL,
            recommendation_rationale TEXT NOT NULL,
            why_not_buy TEXT NOT NULL,
            why_not_hold TEXT NOT NULL,
            why_not_sell TEXT NOT NULL,
            confidence TEXT NOT NULL,
            evidence_coverage REAL NOT NULL,
            abstention_reason TEXT,
            generated_at TEXT NOT NULL,
            analyst_decision TEXT,
            analyst_identity TEXT,
            analyst_decision_at TEXT,
            pm_decision TEXT,
            pm_identity TEXT,
            pm_decision_at TEXT,
            pm_note TEXT,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            report_version INTEGER NOT NULL,
            report_kind TEXT NOT NULL,
            report_status TEXT NOT NULL,
            recommendation TEXT,
            confidence TEXT,
            docx_path TEXT,
            docx_sha256 TEXT,
            pdf_path TEXT,
            pdf_sha256 TEXT,
            template_version TEXT NOT NULL,
            citation_audit_status TEXT NOT NULL,
            warnings_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id),
            UNIQUE (analysis_run_id, report_version)
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_analysis_runs_version ON analysis_runs (version_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_runs_processing ON analysis_runs (processing_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_runs_status ON analysis_runs (status)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_metrics_run ON analysis_metrics (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_scorecard_run ON analysis_scorecard_items (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_scenarios_run ON analysis_scenarios (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_thesis_run ON analysis_thesis_items (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_recommendation_decisions_run ON recommendation_decisions (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_run ON generated_reports (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_generated_reports_version ON generated_reports (version_id)",
    ):
        connection.execute(sql)


def _create_phase7_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS research_workflow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_run_id TEXT NOT NULL UNIQUE,
            package_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            version_id TEXT,
            processing_run_id TEXT,
            analysis_run_id TEXT,
            report_id TEXT,
            stage_statuses_json TEXT,
            warnings_json TEXT,
            errors_json TEXT,
            error_message TEXT,
            created_by TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (package_id) REFERENCES packages(package_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS combined_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id TEXT NOT NULL UNIQUE,
            analysis_run_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            report_id TEXT,
            export_version INTEGER NOT NULL,
            zip_path TEXT NOT NULL,
            zip_sha256 TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            total_size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            metadata_json TEXT,
            warnings_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (analysis_run_id, export_version),
            FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_research_workflows_package ON research_workflow_runs (package_id)",
        "CREATE INDEX IF NOT EXISTS idx_research_workflows_status ON research_workflow_runs (status)",
        "CREATE INDEX IF NOT EXISTS idx_research_workflows_analysis ON research_workflow_runs (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_combined_exports_analysis ON combined_exports (analysis_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_combined_exports_version ON combined_exports (version_id)",
    ):
        connection.execute(sql)


def _create_phase3_official_ir_schema(connection: sqlite3.Connection) -> None:
    """Add Phase 3 audit, reuse, performance, and official-IR records without backfilling history."""
    additions_by_table = {
        "packages": {
            "official_website_url": "TEXT", "official_website_domain": "TEXT", "official_website_confidence": "TEXT",
            "official_website_source": "TEXT", "official_website_checked_at": "TEXT", "official_ir_url": "TEXT",
            "official_ir_domain": "TEXT", "official_ir_confirmed": "INTEGER NOT NULL DEFAULT 0",
        },
        "documents": {
            "excluded_from_next_build": "INTEGER NOT NULL DEFAULT 0", "profile_exclusion_reason": "TEXT",
            "canonical_url": "TEXT", "official_domain": "TEXT", "discovery_page": "TEXT",
            "discovery_method": "TEXT", "discovery_confidence": "TEXT",
        },
        "sec_filing_inventory": {"filing_items": "TEXT", "selection_reason": "TEXT"},
        "package_versions": {"build_fingerprint": "TEXT", "reused_from_version_id": "TEXT"},
        "processing_runs": {"processing_fingerprint": "TEXT", "reused_from_processing_run_id": "TEXT", "duration_seconds": "REAL"},
        "analysis_runs": {"evidence_fingerprint": "TEXT", "metrics_fingerprint": "TEXT", "duration_seconds": "REAL"},
        "claim_conflicts": {"conflict_fingerprint": "TEXT", "comparability_status": "TEXT"},
        "generated_reports": {"input_fingerprint": "TEXT", "report_mode": "TEXT", "memo_json": "TEXT", "duration_seconds": "REAL"},
    }
    for table, additions in additions_by_table.items():
        columns = _table_columns(connection, table)
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS official_website_candidates (
            candidate_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL,
            discovery_source TEXT NOT NULL, discovered_at TEXT NOT NULL, confidence TEXT NOT NULL,
            validation_reasons_json TEXT NOT NULL, rejection_reasons_json TEXT NOT NULL,
            analyst_confirmation_status TEXT NOT NULL, is_verified INTEGER NOT NULL DEFAULT 0,
            UNIQUE(package_id, url)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ir_discovery_runs (
            discovery_run_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, official_url TEXT, official_domain TEXT,
            ir_url TEXT, ir_domain TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            duration_seconds REAL, pages_crawled INTEGER NOT NULL DEFAULT 0, materials_discovered INTEGER NOT NULL DEFAULT 0,
            materials_downloaded INTEGER NOT NULL DEFAULT 0, materials_needing_review INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT, errors_json TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ir_material_candidates (
            candidate_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, discovery_run_id TEXT NOT NULL,
            title TEXT NOT NULL, source_url TEXT NOT NULL, canonical_url TEXT NOT NULL, official_domain TEXT NOT NULL,
            category TEXT NOT NULL, publication_date TEXT, document_date TEXT, mime_type TEXT, file_extension TEXT,
            discovery_page TEXT, discovery_method TEXT NOT NULL, confidence TEXT NOT NULL,
            cutoff_eligibility TEXT NOT NULL, download_status TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
            rejection_reason TEXT, created_at TEXT NOT NULL, UNIQUE(package_id, canonical_url)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_stage_performance (
            performance_id TEXT PRIMARY KEY, workflow_run_id TEXT, package_id TEXT, version_id TEXT,
            processing_run_id TEXT, analysis_run_id TEXT, stage_name TEXT NOT NULL, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL, duration_seconds REAL NOT NULL, files_examined INTEGER NOT NULL DEFAULT 0,
            files_reused INTEGER NOT NULL DEFAULT 0, files_processed INTEGER NOT NULL DEFAULT 0,
            chunks_examined INTEGER NOT NULL DEFAULT 0, openai_batches INTEGER NOT NULL DEFAULT 0,
            openai_input_size INTEGER NOT NULL DEFAULT 0, evidence_created INTEGER NOT NULL DEFAULT 0,
            metrics_created INTEGER NOT NULL DEFAULT 0, conflicts_examined INTEGER NOT NULL DEFAULT 0,
            reports_generated INTEGER NOT NULL DEFAULT 0, reused INTEGER NOT NULL DEFAULT 0, details_json TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS openai_chunk_extractions (
            cache_key TEXT PRIMARY KEY, processing_run_id TEXT NOT NULL, chunk_hash TEXT NOT NULL,
            model TEXT NOT NULL, prompt_version TEXT NOT NULL, schema_version TEXT NOT NULL,
            status TEXT NOT NULL, evidence_count INTEGER NOT NULL DEFAULT 0, completed_at TEXT NOT NULL,
            UNIQUE(processing_run_id, chunk_hash, model, prompt_version, schema_version)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_document_inclusions (
            package_id TEXT NOT NULL, document_id TEXT NOT NULL, included INTEGER NOT NULL,
            reason TEXT, profile_name TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(package_id, document_id)
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_official_candidates_package ON official_website_candidates(package_id)",
        "CREATE INDEX IF NOT EXISTS idx_ir_runs_package ON ir_discovery_runs(package_id)",
        "CREATE INDEX IF NOT EXISTS idx_ir_materials_package ON ir_material_candidates(package_id)",
        "CREATE INDEX IF NOT EXISTS idx_performance_workflow ON workflow_stage_performance(workflow_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_build_fingerprint ON package_versions(parent_package_id, build_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_processing_fingerprint ON processing_runs(version_id, processing_fingerprint)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_conflicts_fingerprint ON claim_conflicts(conflict_fingerprint) WHERE conflict_fingerprint IS NOT NULL",
    ):
        connection.execute(sql)


def _create_phase4_final_schema(connection: sqlite3.Connection) -> None:
    """Add final-phase resumability and research-window fields without rewriting history."""
    additions_by_table = {
        "packages": {
            "selected_years_json": "TEXT",
            "selected_months_json": "TEXT",
            "research_window_fingerprint": "TEXT",
            "archived_at": "TEXT",
            "archive_reason": "TEXT",
        },
        "package_versions": {
            "selected_years_json": "TEXT",
            "selected_months_json": "TEXT",
            "research_window_fingerprint": "TEXT",
        },
        "documents": {"selected_window_status": "TEXT"},
        "processing_runs": {
            "last_checkpoint_at": "TEXT",
            "resume_count": "INTEGER NOT NULL DEFAULT 0",
            "reused_documents": "INTEGER NOT NULL DEFAULT 0",
            "database_write_seconds": "REAL NOT NULL DEFAULT 0",
            "chunking_seconds": "REAL NOT NULL DEFAULT 0",
            "deterministic_extraction_seconds": "REAL NOT NULL DEFAULT 0",
            "conflict_analysis_seconds": "REAL NOT NULL DEFAULT 0",
            "openai_extraction_seconds": "REAL NOT NULL DEFAULT 0",
        },
        "document_processing_results": {
            "processing_fingerprint": "TEXT",
            "parse_started_at": "TEXT",
            "parse_completed_at": "TEXT",
            "parse_duration_seconds": "REAL",
            "document_type": "TEXT",
            "file_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "normalized_character_reduction": "INTEGER NOT NULL DEFAULT 0",
            "chunk_count": "INTEGER NOT NULL DEFAULT 0",
            "evidence_count": "INTEGER NOT NULL DEFAULT 0",
            "reuse_status": "TEXT",
            "warnings_json": "TEXT",
            "errors_json": "TEXT",
            "chunking_duration_seconds": "REAL NOT NULL DEFAULT 0",
            "extraction_duration_seconds": "REAL NOT NULL DEFAULT 0",
            "database_write_duration_seconds": "REAL NOT NULL DEFAULT 0",
            "attempt_count": "INTEGER NOT NULL DEFAULT 1",
        },
    }
    for table, additions in additions_by_table.items():
        columns = _table_columns(connection, table)
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_document_items (
            processing_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            processing_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            reuse_status TEXT NOT NULL DEFAULT 'NEW',
            parse_started_at TEXT,
            parse_completed_at TEXT,
            parse_duration_seconds REAL NOT NULL DEFAULT 0,
            file_size_bytes INTEGER NOT NULL DEFAULT 0,
            document_type TEXT,
            extracted_character_count INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(processing_run_id, version_document_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_stage_timings (
            timing_id TEXT PRIMARY KEY,
            processing_run_id TEXT NOT NULL,
            version_document_id TEXT,
            stage_name TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conflict_analysis_summaries (
            processing_run_id TEXT PRIMARY KEY,
            valid_unresolved_conflicts INTEGER NOT NULL DEFAULT 0,
            excluded_same_source INTEGER NOT NULL DEFAULT 0,
            excluded_unit_mismatches INTEGER NOT NULL DEFAULT 0,
            excluded_currency_mismatches INTEGER NOT NULL DEFAULT 0,
            excluded_period_mismatches INTEGER NOT NULL DEFAULT 0,
            excluded_duplicate_evidence INTEGER NOT NULL DEFAULT 0,
            pairs_examined INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_processing_items_status ON processing_document_items(processing_run_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_processing_items_fingerprint ON processing_document_items(version_document_id, processing_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_processing_timings_run ON processing_stage_timings(processing_run_id, stage_name)",
        "CREATE INDEX IF NOT EXISTS idx_documents_window_status ON documents(package_id, selected_window_status)",
    ):
        connection.execute(sql)


def _create_phase5_memo_schema(connection: sqlite3.Connection) -> None:
    """Add official-IR outcome and memo-quality audit records without rewriting history."""
    additions_by_table = {
        "analysis_runs": {
            "memo_generation_status": "TEXT",
            "memo_generation_error": "TEXT",
        },
        "generated_reports": {
            "memo_generation_attempt_id": "TEXT",
            "memo_quality_status": "TEXT",
        },
    }
    for table, additions in additions_by_table.items():
        columns = _table_columns(connection, table)
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memo_generation_attempts (
            attempt_id TEXT PRIMARY KEY,
            analysis_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            model TEXT,
            endpoint TEXT,
            selected_candidate_ids_json TEXT NOT NULL,
            rejected_candidate_count INTEGER NOT NULL DEFAULT 0,
            draft_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memo_evidence_candidates (
            candidate_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            analysis_run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            version_document_id TEXT NOT NULL,
            claim_family TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            supporting_quote TEXT NOT NULL,
            metric_name TEXT,
            numeric_value REAL,
            unit TEXT,
            currency TEXT,
            reporting_period TEXT,
            filing_or_publication_date TEXT,
            source_type TEXT,
            form_type TEXT,
            section_heading TEXT,
            page_number INTEGER,
            source_priority REAL NOT NULL,
            recency_score REAL NOT NULL,
            materiality_score REAL NOT NULL,
            completeness_score REAL NOT NULL,
            decision_relevance_score REAL NOT NULL,
            rejection_reasons_json TEXT NOT NULL,
            eligible_for_memo INTEGER NOT NULL DEFAULT 0,
            candidate_kind TEXT NOT NULL,
            citation TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memo_quality_audits (
            audit_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            analysis_run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            complete_sentence_check TEXT NOT NULL,
            ellipsis_check TEXT NOT NULL,
            citation_check TEXT NOT NULL,
            numeric_validation_check TEXT NOT NULL,
            period_validation_check TEXT NOT NULL,
            unit_validation_check TEXT NOT NULL,
            currency_validation_check TEXT NOT NULL,
            recency_check TEXT NOT NULL,
            duplicate_check TEXT NOT NULL,
            source_heading_check TEXT NOT NULL,
            risk_coverage_check TEXT NOT NULL,
            decision_relevance_check TEXT NOT NULL,
            one_page_fit_check TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_memo_attempts_analysis ON memo_generation_attempts(analysis_run_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memo_candidates_attempt ON memo_evidence_candidates(attempt_id, eligible_for_memo)",
        "CREATE INDEX IF NOT EXISTS idx_memo_audits_analysis ON memo_quality_audits(analysis_run_id, created_at)",
    ):
        connection.execute(sql)


def _create_phase51_stabilization_schema(connection: sqlite3.Connection) -> None:
    """Add recommendation-resume, safe usage, and IR approval audit records."""
    ir_columns = _table_columns(connection, "ir_material_candidates")
    for column, definition in {
        "analyst_approved": "INTEGER NOT NULL DEFAULT 0",
        "approval_timestamp": "TEXT",
        "original_confidence": "TEXT",
        "final_download_result": "TEXT",
    }.items():
        if column not in ir_columns:
            connection.execute(f"ALTER TABLE ir_material_candidates ADD COLUMN {column} {definition}")
    exclusion_patterns = (
        "%new account%", "%new_account%", "%account application%", "%account_application%",
        "%credit application%", "%credit_application%", "%customer application%", "%customer_application%",
        "%customer form%", "%customer_form%", "%vendor form%", "%vendor_form%",
        "%supplier form%", "%supplier_form%", "%employment application%", "%employment_application%",
        "%w-9%", "%w_9%", "%banking instructions%", "%banking_instructions%",
        "%order form%", "%order_form%",
    )
    candidate_text = "LOWER(COALESCE(title, '') || ' ' || COALESCE(source_url, ''))"
    matches = " OR ".join(f"{candidate_text} LIKE ?" for _ in exclusion_patterns)
    connection.execute(
        f"""
        UPDATE ir_material_candidates
        SET download_status = 'NON_INVESTOR_MATERIAL',
            category = 'Non-Investor Material',
            selected = 0,
            rejection_reason = 'Excluded non-investor material signal found during Phase 5.1 relevance migration.'
        WHERE {matches}
        """,
        exclusion_patterns,
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_attempts (
            attempt_id TEXT PRIMARY KEY,
            analysis_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            model TEXT,
            endpoint TEXT,
            original_evidence_count INTEGER NOT NULL DEFAULT 0,
            eligible_candidate_count INTEGER NOT NULL DEFAULT 0,
            supporting_candidate_count INTEGER NOT NULL DEFAULT 0,
            risk_candidate_count INTEGER NOT NULL DEFAULT 0,
            metric_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            openai_call_count INTEGER NOT NULL DEFAULT 0,
            failure_category TEXT,
            diagnostics_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS narrative_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            analysis_run_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            candidate_fingerprint TEXT NOT NULL,
            candidate_kind TEXT,
            claim_family TEXT,
            eligible INTEGER NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 0,
            exclusion_reason TEXT,
            rank_score REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(attempt_id, evidence_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_stage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL,
            analysis_run_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            status TEXT NOT NULL,
            detail_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS openai_usage_ledger (
            usage_id TEXT PRIMARY KEY,
            analysis_run_id TEXT,
            processing_run_id TEXT,
            workflow_run_id TEXT,
            attempt_id TEXT,
            pipeline_stage TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            model TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            output_status TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_recommendation_attempts_analysis ON recommendation_attempts(analysis_run_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_narrative_candidates_attempt ON narrative_candidates(attempt_id, selected)",
        "CREATE INDEX IF NOT EXISTS idx_recommendation_stage_attempt ON recommendation_stage_events(attempt_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_openai_usage_analysis ON openai_usage_ledger(analysis_run_id, pipeline_stage)",
    ):
        connection.execute(sql)


def _create_phase6a_recipe_schema(connection: sqlite3.Connection) -> None:
    """Create the additive Phase 6A recipe and package-assembly schema."""
    package_columns = _table_columns(connection, "packages")
    for column, definition in {
        "compilation_date": "TEXT",
        "compiled_by": "TEXT",
        "source_legacy_package_id": "TEXT",
    }.items():
        if column not in package_columns:
            connection.execute(f"ALTER TABLE packages ADD COLUMN {column} {definition}")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            schema_key TEXT PRIMARY KEY,
            schema_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recipe_imports (
            import_id TEXT PRIMARY KEY,
            workbook_filename TEXT NOT NULL,
            workbook_sha256 TEXT NOT NULL,
            available_sheets_json TEXT NOT NULL,
            selected_sheets_json TEXT NOT NULL,
            importer_version TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            imported_by TEXT NOT NULL,
            import_report_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS package_recipes (
            recipe_id TEXT PRIMARY KEY,
            recipe_name TEXT NOT NULL,
            recipe_type TEXT NOT NULL,
            security_type TEXT NOT NULL,
            version INTEGER NOT NULL,
            description TEXT,
            source_workbook_name TEXT NOT NULL,
            source_workbook_hash TEXT NOT NULL,
            source_sheet TEXT NOT NULL,
            importer_version TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT,
            superseded_at TEXT,
            superseded_by_recipe_id TEXT,
            notes TEXT,
            import_id TEXT,
            UNIQUE(recipe_name, version),
            FOREIGN KEY (import_id) REFERENCES recipe_imports(import_id)
        );
        CREATE TABLE IF NOT EXISTS recipe_import_rows (
            import_row_id TEXT PRIMARY KEY,
            import_id TEXT NOT NULL,
            recipe_id TEXT,
            sheet_name TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            source_coordinates_json TEXT NOT NULL,
            raw_values_json TEXT NOT NULL,
            normalized_values_json TEXT,
            warnings_json TEXT,
            include_in_recipe INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (import_id) REFERENCES recipe_imports(import_id),
            FOREIGN KEY (recipe_id) REFERENCES package_recipes(recipe_id)
        );
        CREATE TABLE IF NOT EXISTS research_slots (
            slot_id TEXT PRIMARY KEY,
            recipe_id TEXT NOT NULL,
            order_number INTEGER,
            suborder INTEGER NOT NULL DEFAULT 0,
            display_name TEXT NOT NULL,
            normalized_slot_type TEXT NOT NULL,
            section_code TEXT NOT NULL,
            section_name TEXT NOT NULL,
            required_level TEXT NOT NULL,
            long_applicable INTEGER,
            short_applicable INTEGER,
            conditional_rule TEXT,
            preferred_sources_json TEXT NOT NULL,
            fallback_sources_json TEXT NOT NULL,
            instructions TEXT,
            minimum_documents INTEGER NOT NULL DEFAULT 1,
            maximum_documents INTEGER NOT NULL DEFAULT 1,
            freshness_rule TEXT,
            anchor_rule TEXT,
            allowed_document_types_json TEXT NOT NULL,
            expected_output_format TEXT,
            auto_search_enabled INTEGER NOT NULL DEFAULT 0,
            manual_upload_allowed INTEGER NOT NULL DEFAULT 1,
            analyst_review_required INTEGER NOT NULL DEFAULT 1,
            default_status TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            source_sheet TEXT NOT NULL,
            source_row INTEGER NOT NULL,
            source_coordinates_json TEXT NOT NULL,
            raw_import_json TEXT NOT NULL,
            import_warning TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES package_recipes(recipe_id)
        );
        CREATE TABLE IF NOT EXISTS recipe_approvals (
            approval_id TEXT PRIMARY KEY,
            recipe_id TEXT NOT NULL,
            approver TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            workbook_sha256 TEXT NOT NULL,
            recipe_version INTEGER NOT NULL,
            normalized_recipe_snapshot_json TEXT NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES package_recipes(recipe_id)
        );
        CREATE TABLE IF NOT EXISTS package_recipe_instances (
            package_recipe_instance_id TEXT PRIMARY KEY,
            package_id TEXT NOT NULL UNIQUE,
            recipe_id TEXT NOT NULL,
            recipe_version INTEGER NOT NULL,
            recipe_snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            status TEXT NOT NULL,
            locked_at TEXT,
            locked_by TEXT,
            FOREIGN KEY (package_id) REFERENCES packages(package_id),
            FOREIGN KEY (recipe_id) REFERENCES package_recipes(recipe_id)
        );
        CREATE TABLE IF NOT EXISTS package_slot_instances (
            package_slot_instance_id TEXT PRIMARY KEY,
            package_recipe_instance_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            order_number INTEGER,
            suborder INTEGER NOT NULL DEFAULT 0,
            display_name_snapshot TEXT NOT NULL,
            section_snapshot TEXT NOT NULL,
            requirement_snapshot TEXT NOT NULL,
            instructions_snapshot TEXT,
            preferred_sources_snapshot_json TEXT NOT NULL,
            minimum_documents INTEGER NOT NULL,
            maximum_documents INTEGER NOT NULL,
            applicability_status TEXT NOT NULL,
            completion_status TEXT NOT NULL,
            analyst_acknowledged INTEGER NOT NULL DEFAULT 0,
            analyst_notes TEXT,
            selected_document_count INTEGER NOT NULL DEFAULT 0,
            latest_selected_document_date TEXT,
            cap_override_approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (package_recipe_instance_id) REFERENCES package_recipe_instances(package_recipe_instance_id),
            FOREIGN KEY (package_id) REFERENCES packages(package_id),
            FOREIGN KEY (slot_id) REFERENCES research_slots(slot_id)
        );
        CREATE TABLE IF NOT EXISTS slot_document_assignments (
            assignment_id TEXT PRIMARY KEY,
            package_slot_instance_id TEXT NOT NULL,
            package_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            assignment_source TEXT NOT NULL,
            suggested_slot_id TEXT,
            final_slot_id TEXT,
            suggestion_confidence REAL,
            suggestion_reason TEXT,
            matched_tokens_json TEXT NOT NULL DEFAULT '[]',
            assignment_status TEXT NOT NULL,
            selected_for_package INTEGER NOT NULL DEFAULT 0,
            highlighted_research INTEGER NOT NULL DEFAULT 0,
            display_order INTEGER NOT NULL DEFAULT 0,
            analyst_notes TEXT,
            assigned_at TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT,
            replaced_assignment_id TEXT,
            FOREIGN KEY (package_slot_instance_id) REFERENCES package_slot_instances(package_slot_instance_id),
            FOREIGN KEY (package_id) REFERENCES packages(package_id),
            FOREIGN KEY (document_id) REFERENCES documents(document_id)
        );
        CREATE TABLE IF NOT EXISTS recipe_corrections (
            correction_id TEXT PRIMARY KEY,
            package_id TEXT NOT NULL,
            package_slot_instance_id TEXT,
            document_id TEXT,
            correction_type TEXT NOT NULL,
            original_value TEXT,
            corrected_value TEXT,
            reason TEXT NOT NULL,
            corrected_at TEXT NOT NULL,
            corrected_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS phase6a_audit_events (
            event_id TEXT PRIMARY KEY,
            package_id TEXT,
            recipe_id TEXT,
            package_slot_instance_id TEXT,
            document_id TEXT,
            event_type TEXT NOT NULL,
            event_details_json TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS package_snapshot_imports (
            snapshot_import_id TEXT PRIMARY KEY,
            source_package_id TEXT NOT NULL,
            new_package_id TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            imported_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recipe_status ON package_recipes(status, security_type);
        CREATE INDEX IF NOT EXISTS idx_slots_recipe_order ON research_slots(recipe_id, order_number, suborder);
        CREATE INDEX IF NOT EXISTS idx_slot_instances_package ON package_slot_instances(package_id, order_number, suborder);
        CREATE INDEX IF NOT EXISTS idx_assignments_slot ON slot_document_assignments(package_slot_instance_id, assignment_status);
        CREATE INDEX IF NOT EXISTS idx_assignments_package ON slot_document_assignments(package_id, selected_for_package);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_active_slot_document_assignment
        ON slot_document_assignments(package_slot_instance_id, document_id)
        WHERE assignment_status NOT IN ('REJECTED', 'REPLACED', 'REMOVED');
        CREATE TRIGGER IF NOT EXISTS immutable_approved_recipe_slots_update
        BEFORE UPDATE ON research_slots
        WHEN (SELECT status FROM package_recipes WHERE recipe_id = OLD.recipe_id)
             IN ('APPROVED', 'ACTIVE', 'SUPERSEDED', 'ARCHIVED')
        BEGIN
            SELECT RAISE(ABORT, 'Approved recipe slots are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS immutable_approved_recipe_slots_delete
        BEFORE DELETE ON research_slots
        WHEN (SELECT status FROM package_recipes WHERE recipe_id = OLD.recipe_id)
             IN ('APPROVED', 'ACTIVE', 'SUPERSEDED', 'ARCHIVED')
        BEGIN
            SELECT RAISE(ABORT, 'Approved recipe slots are immutable');
        END;
        """
    )
    connection.execute(
        """
        INSERT INTO schema_metadata(schema_key, schema_value, updated_at)
        VALUES ('database_schema_version', '6A.0', ?)
        ON CONFLICT(schema_key) DO UPDATE SET schema_value=excluded.schema_value, updated_at=excluded.updated_at
        """,
        (utc_now_iso(),),
    )


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
    collection_profile_name: str | None = None,
    collection_profile_snapshot_json: str | None = None,
    selected_years_json: str | None = None,
    selected_months_json: str | None = None,
    research_window_fingerprint: str | None = None,
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
                collection_profile_name, collection_profile_snapshot_json,
                selected_years_json, selected_months_json, research_window_fingerprint,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                collection_profile_name,
                collection_profile_snapshot_json,
                selected_years_json,
                selected_months_json,
                research_window_fingerprint,
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
    include_archived: bool = False,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Return packages ordered by most recently updated."""
    initialize_database(db_path)
    sql = "SELECT * FROM packages"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY updated_at DESC, created_at DESC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def archive_draft_package(
    package_id: str,
    *,
    reason: str,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    """Archive an unlocked draft without deleting any package or audit rows."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        locked = connection.execute(
            "SELECT 1 FROM package_versions WHERE parent_package_id = ? AND status = 'LOCKED' LIMIT 1",
            (package_id,),
        ).fetchone()
        if locked:
            raise ValueError("Packages with locked versions cannot be archived as manual-test drafts.")
        now = utc_now_iso()
        cursor = connection.execute(
            "UPDATE packages SET archived_at = ?, archive_reason = ?, updated_at = ? WHERE package_id = ? AND archived_at IS NULL",
            (now, reason[:500], now, package_id),
        )
        if not cursor.rowcount:
            raise ValueError("Draft package does not exist or is already archived.")
    return get_package_by_package_id(package_id, db_path=db_path) or {}


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


def update_package_research_settings(
    package_id: str,
    *,
    filing_history_years: int,
    research_cutoff_date: str,
    selected_years_json: str | None = None,
    selected_months_json: str | None = None,
    research_window_fingerprint: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update editable research settings on the working package."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET filing_history_years = ?, research_cutoff_date = ?,
                selected_years_json = ?, selected_months_json = ?, research_window_fingerprint = ?,
                updated_at = ?
            WHERE package_id = ?
            """,
            (
                filing_history_years,
                research_cutoff_date,
                selected_years_json,
                selected_months_json,
                research_window_fingerprint,
                utc_now_iso(),
                package_id,
            ),
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
    documents_already_collected: int = 0,
    documents_duplicated: int = 0,
    documents_not_found: int = 0,
    documents_failed: int = 0,
    documents_eligible: int = 0,
    documents_excluded_profile: int = 0,
    documents_awaiting_selection: int = 0,
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
                documents_downloaded = ?, documents_skipped = ?, documents_already_collected = ?,
                documents_duplicated = ?, documents_not_found = ?, documents_failed = ?,
                documents_eligible = ?, documents_excluded_profile = ?, documents_awaiting_selection = ?,
                error_summary = ?
            WHERE run_id = ?
            """,
            (
                completed_at,
                status,
                documents_discovered,
                documents_downloaded,
                documents_skipped,
                documents_already_collected,
                documents_duplicated,
                documents_not_found,
                documents_failed,
                documents_eligible,
                documents_excluded_profile,
                documents_awaiting_selection,
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


def replace_sec_filing_inventory(
    package_id: str,
    rows: list[dict[str, Any]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> None:
    """Replace the current editable package inventory; locked version snapshots are untouched."""
    initialize_database(db_path)
    now = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute("DELETE FROM sec_filing_inventory WHERE package_id = ?", (package_id,))
        connection.executemany(
            """
            INSERT INTO sec_filing_inventory (
                package_id, accession_number, primary_document, original_form_type,
                normalized_form_family, filing_date, source_url, inventory_status,
                conditional_rule, selected, metadata_json, filing_items, selection_reason, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    package_id,
                    row["accession_number"],
                    row["primary_document"],
                    row["original_form_type"],
                    row.get("normalized_form_family"),
                    row.get("filing_date"),
                    row.get("source_url"),
                    row["inventory_status"],
                    row.get("conditional_rule"),
                    int(bool(row.get("selected"))),
                    row.get("metadata_json"),
                    row.get("filing_items"),
                    row.get("selection_reason"),
                    now,
                )
                for row in rows
            ],
        )


def list_sec_filing_inventory(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM sec_filing_inventory WHERE package_id = ? ORDER BY filing_date DESC, accession_number",
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


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


def generate_document_id(prefix: str = "DOC") -> str:
    """Return a globally unique document row identifier."""
    return f"{prefix}-{secrets.token_hex(12).upper()}"


def normalize_source_url(source_url: str | None) -> str:
    """Normalize source URLs for package-scoped duplicate detection."""
    if not source_url:
        return ""
    parsed = urlparse(str(source_url).strip())
    if not parsed.scheme:
        return str(source_url).strip().lower()
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = quote(unquote(parsed.path or ""), safe="/:@")
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def source_identity_key(document: dict[str, Any]) -> str | None:
    """Return a stable package-scoped source identity key separate from document_id."""
    existing = str(document.get("source_identity_key") or "").strip()
    if existing:
        return existing.lower()
    source_url = normalize_source_url(document.get("source_url"))
    accession = str(document.get("accession_number") or "").strip()
    collection_method = str(document.get("collection_method") or "").upper()
    source_name = str(document.get("source_name") or "").upper()
    sha256_hash = str(document.get("sha256_hash") or "").strip().lower()

    if accession:
        parsed = urlparse(source_url)
        parts = [part for part in parsed.path.split("/") if part]
        cik = ""
        primary_document = Path(parsed.path).name
        if "data" in parts:
            data_index = parts.index("data")
            if data_index + 1 < len(parts):
                cik = parts[data_index + 1]
        cik_or_ticker = cik or str(document.get("ticker") or "").lower()
        return f"sec:{cik_or_ticker}:{accession.lower()}:{primary_document.lower()}"
    if collection_method == "LICENSED_UPLOAD" or source_url.startswith("local-upload://"):
        return f"upload:{sha256_hash}" if sha256_hash else None
    if collection_method == "INVESTOR_RELATIONS" or source_name == "INVESTOR RELATIONS":
        return f"ir:{source_url}" if source_url else None
    if source_url:
        suffix = f":{sha256_hash}" if sha256_hash else ""
        return f"public:{source_url}{suffix}"
    if sha256_hash:
        return f"upload:{sha256_hash}"
    return None


def _legacy_or_missing_document_id(document_id: str | None) -> bool:
    if not document_id:
        return True
    return document_id.startswith(("DOC-SEC-", "DOC-IR-", "DOC-UPLOAD-"))


def _row_for_document_identity(connection: sqlite3.Connection, prepared: dict[str, Any]) -> sqlite3.Row | None:
    package_id = prepared.get("package_id")
    accession = prepared.get("accession_number")
    source_url = prepared.get("source_url")
    sha256_hash = prepared.get("sha256_hash")
    identity = prepared.get("source_identity_key")
    checks: list[tuple[str, tuple[Any, ...]]] = []
    if accession:
        checks.append(
            (
                """
                SELECT * FROM documents
                WHERE package_id = ? AND accession_number = ?
                  AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
                ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
                LIMIT 1
                """,
                (package_id, accession),
            )
        )
    if source_url:
        checks.append(
            (
                """
                SELECT * FROM documents
                WHERE package_id = ? AND source_url = ?
                  AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
                ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
                LIMIT 1
                """,
                (package_id, source_url),
            )
        )
    if sha256_hash:
        checks.append(
            (
                """
                SELECT * FROM documents
                WHERE package_id = ? AND sha256_hash = ?
                  AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
                ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
                LIMIT 1
                """,
                (package_id, sha256_hash),
            )
        )
    if identity:
        checks.append(
            (
                """
                SELECT * FROM documents
                WHERE package_id = ? AND source_identity_key = ?
                  AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
                ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
                LIMIT 1
                """,
                (package_id, identity),
            )
        )
    for sql, params in checks:
        row = connection.execute(sql, params).fetchone()
        if row:
            return row
    return None


def _document_id_exists(connection: sqlite3.Connection, document_id: str, package_id: str) -> bool:
    row = connection.execute(
        "SELECT package_id FROM documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return bool(row and row["package_id"] != package_id)


def _update_existing_document_from_insert(
    connection: sqlite3.Connection,
    existing: sqlite3.Row,
    prepared: dict[str, Any],
) -> None:
    existing_dict = dict(existing)
    incoming_status = prepared.get("collection_status")
    existing_status = existing_dict.get("collection_status")
    repair_existing = incoming_status == "DOWNLOADED" and existing_status in {"FAILED", "DUPLICATE", "SKIPPED", "DISCOVERED"}
    safe_fields = (
        "category",
        "document_type",
        "is_public",
        "title",
        "source_name",
        "source_url",
        "source_domain",
        "source_identity_key",
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
        "is_public",
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
    )
    updates: dict[str, Any] = {}
    for field in safe_fields:
        incoming = prepared.get(field)
        if incoming is None or incoming == "":
            continue
        if repair_existing or existing_dict.get(field) in {None, ""}:
            updates[field] = incoming
    if repair_existing:
        updates["collection_status"] = "DOWNLOADED"
        updates["error_message"] = None
    if updates:
        updates["updated_at"] = utc_now_iso()
        sql = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE documents SET {sql} WHERE document_id = ?",
            (*updates.values(), existing["document_id"]),
        )


def create_document_record(
    document: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    """Insert or reuse a package-scoped document record idempotently."""
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
        "source_identity_key",
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
    prepared["source_identity_key"] = source_identity_key(prepared)
    if _legacy_or_missing_document_id(prepared.get("document_id")):
        prepared["document_id"] = generate_document_id()
    prepared.setdefault("is_public", True)
    prepared["is_public"] = int(bool(prepared["is_public"]))
    prepared.setdefault("authorization_confirmed", False)
    prepared["authorization_confirmed"] = int(bool(prepared["authorization_confirmed"]))
    prepared["created_at"] = now
    prepared["updated_at"] = now
    with get_connection(db_path) as connection:
        existing = _row_for_document_identity(connection, prepared)
        if existing:
            _update_existing_document_from_insert(connection, existing, prepared)
            row = connection.execute("SELECT * FROM documents WHERE document_id = ?", (existing["document_id"],)).fetchone()
            return dict(row) if row else dict(existing)
        while _document_id_exists(connection, prepared["document_id"], prepared["package_id"]):
            prepared["document_id"] = generate_document_id()
        try:
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, package_id, ticker, category, document_type, title,
                    source_name, source_url, source_domain, source_identity_key,
                    accession_number, form_type, publication_date, report_period,
                    local_filename, local_path, mime_type, file_size_bytes,
                    sha256_hash, collection_method, collection_status, is_public,
                    error_message, original_filename, stored_filename, file_extension,
                    detected_file_type, source_type, source_institution,
                    suggested_category_code, suggested_category, suggested_confidence,
                    final_category_code, classification_method,
                    classification_rules_matched, document_title, document_date,
                    upload_method, uploaded_by, analyst_notes, authorization_confirmed,
                    upload_status, archive_origin_document_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(prepared.get(field) for field in fields),
            )
        except sqlite3.IntegrityError:
            winner = _row_for_document_identity(connection, prepared)
            if winner:
                _update_existing_document_from_insert(connection, winner, prepared)
                row = connection.execute("SELECT * FROM documents WHERE document_id = ?", (winner["document_id"],)).fetchone()
                return dict(row) if row else dict(winner)
            prepared["document_id"] = generate_document_id()
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, package_id, ticker, category, document_type, title,
                    source_name, source_url, source_domain, source_identity_key,
                    accession_number, form_type, publication_date, report_period,
                    local_filename, local_path, mime_type, file_size_bytes,
                    sha256_hash, collection_method, collection_status, is_public,
                    error_message, original_filename, stored_filename, file_extension,
                    detected_file_type, source_type, source_institution,
                    suggested_category_code, suggested_category, suggested_confidence,
                    final_category_code, classification_method,
                    classification_rules_matched, document_title, document_date,
                    upload_method, uploaded_by, analyst_notes, authorization_confirmed,
                    upload_status, archive_origin_document_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(prepared.get(field) for field in fields),
            )
    return get_document_by_document_id(prepared["document_id"], db_path=db_path) or {}


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


def mark_failed_documents_superseded(
    package_id: str,
    *,
    accession_number: str | None = None,
    source_url: str | None = None,
    sha256_hash: str | None = None,
    source_identity_key_value: str | None = None,
    winning_document_id: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    """Mark recoverable failed document attempts as superseded for current counts."""
    clauses = ["package_id = ?", "collection_status = 'FAILED'"]
    clause_params: list[Any] = [package_id]
    identity = source_identity_key_value
    if not identity:
        identity = source_identity_key({"source_url": source_url, "sha256_hash": sha256_hash, "accession_number": accession_number})
    match_clauses: list[str] = []
    match_params: list[Any] = []
    if accession_number:
        match_clauses.append("accession_number = ?")
        match_params.append(accession_number)
    if source_url:
        match_clauses.append("source_url = ?")
        match_params.append(source_url)
    if sha256_hash:
        match_clauses.append("sha256_hash = ?")
        match_params.append(sha256_hash)
    if identity:
        match_clauses.append("source_identity_key = ?")
        match_params.append(identity)
    if not match_clauses:
        return 0
    if winning_document_id:
        clauses.append("document_id != ?")
        clause_params.append(winning_document_id)
    sql = f"""
        UPDATE documents
        SET collection_status = 'SUPERSEDED',
            error_message = COALESCE(error_message, 'Superseded by repaired document record.'),
            updated_at = ?
        WHERE {' AND '.join(clauses)}
          AND ({' OR '.join(match_clauses)})
    """
    ordered_params = [utc_now_iso(), *clause_params, *match_params]
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        cursor = connection.execute(sql, tuple(ordered_params))
        return int(cursor.rowcount or 0)


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


def get_document_by_accession(
    package_id: str,
    accession_number: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    if not accession_number:
        return None
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM documents
            WHERE package_id = ? AND accession_number = ?
              AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
            ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
            LIMIT 1
            """,
            (package_id, accession_number),
        ).fetchone()
    return row_to_dict(row)


def get_document_by_url(
    package_id: str,
    source_url: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    if not source_url:
        return None
    identity = source_identity_key({"package_id": package_id, "source_url": source_url, "collection_method": "PUBLIC"})
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM documents
            WHERE package_id = ?
              AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
              AND (source_url = ? OR source_identity_key = ?)
            ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
            LIMIT 1
            """,
            (package_id, source_url, identity),
        ).fetchone()
    return row_to_dict(row)


def get_document_by_hash(
    package_id: str,
    sha256_hash: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    if not sha256_hash:
        return None
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM documents
            WHERE package_id = ? AND sha256_hash = ?
              AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
            ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
            LIMIT 1
            """,
            (package_id, sha256_hash),
        ).fetchone()
    return row_to_dict(row)


def get_document_by_source_identity(
    package_id: str,
    identity: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    if not identity:
        return None
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM documents
            WHERE package_id = ? AND source_identity_key = ?
              AND collection_status NOT IN ('DELETED', 'SUPERSEDED', 'RESOLVED')
            ORDER BY CASE collection_status WHEN 'DOWNLOADED' THEN 0 WHEN 'DISCOVERED' THEN 1 ELSE 2 END, updated_at DESC
            LIMIT 1
            """,
            (package_id, identity),
        ).fetchone()
    return row_to_dict(row)


def document_exists_by_accession(
    package_id: str,
    accession_number: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    return get_document_by_accession(package_id, accession_number, db_path=db_path) is not None


def document_exists_by_url(
    package_id: str,
    source_url: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    return get_document_by_url(package_id, source_url, db_path=db_path) is not None


def document_exists_by_hash(
    package_id: str,
    sha256_hash: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> bool:
    return get_document_by_hash(package_id, sha256_hash, db_path=db_path) is not None


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
    normalized = dict(item)
    normalized["requirement_level"] = str(item.get("requirement_level") or "").strip().lower()
    for key in ("automatic_status", "analyst_override_status", "effective_status"):
        if normalized.get(key):
            normalized[key] = str(normalized[key]).strip().upper()
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
                normalized["checklist_item_id"],
                normalized["package_id"],
                normalized["category_code"],
                normalized["display_name"],
                normalized["requirement_level"],
                normalized["checklist_group"],
                normalized.get("applicability", "APPLICABLE"),
                normalized["automatic_status"],
                normalized.get("analyst_override_status"),
                normalized["effective_status"],
                normalized.get("analyst_note"),
                normalized.get("matched_document_count", 0),
                normalized.get("latest_document_date"),
                now,
                now,
            ),
        )
    return get_checklist_item(normalized["package_id"], normalized["checklist_item_id"], db_path=db_path) or {}


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
    normalized_override = str(override_status).strip().upper() if override_status else None
    with get_connection(db_path) as connection:
        if normalized_override:
            connection.execute(
                """
                UPDATE package_checklist_items
                SET analyst_override_status = ?, effective_status = ?,
                    analyst_note = ?, updated_at = ?
                WHERE package_id = ? AND checklist_item_id = ?
                """,
                (normalized_override, normalized_override, note, utc_now_iso(), package_id, checklist_item_id),
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
        "normalized_form_family",
        "parent_accession_number",
        "inferred_source",
        "source_confidence",
        "source_inference_reason",
        "analyst_corrected_source",
        "final_source",
        "inferred_category_code",
        "category_confidence",
        "category_inference_reason",
        "analyst_corrected_category_code",
        "upload_batch_id",
        "managed_filename",
        "source_name",
        "source_type",
        "document_type",
        "is_public",
        "excluded_from_next_build",
        "profile_exclusion_reason",
        "canonical_url",
        "official_domain",
        "discovery_page",
        "discovery_method",
        "discovery_confidence",
        "selected_window_status",
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


def update_document_window_statuses(
    rows: list[tuple[str, str]], *, db_path: Path | str = DATABASE_PATH
) -> None:
    if not rows:
        return
    initialize_database(db_path)
    now = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.executemany(
            "UPDATE documents SET selected_window_status = ?, updated_at = ? WHERE document_id = ?",
            [(status, now, document_id) for document_id, status in rows],
        )


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
            WHERE UPPER(effective_status) IN ('MISSING', 'NEEDS_REVIEW', 'STALE')
            """
        ).fetchone()
        missing_core = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM package_checklist_items
            WHERE LOWER(requirement_level) = 'required'
              AND UPPER(effective_status) IN ('MISSING', 'NEEDS_REVIEW', 'STALE')
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


def update_package_review_acknowledgement(
    package_id: str,
    *,
    checklist_reviewed: bool,
    reviewed_by: str,
    review_note: str,
    missing_core_acknowledged: bool,
    stale_documents_acknowledged: bool,
    needs_review_acknowledged: bool,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET checklist_reviewed = ?, reviewed_by = ?, reviewed_timestamp = ?,
                review_note = ?, missing_core_acknowledged = ?,
                stale_documents_acknowledged = ?, needs_review_acknowledged = ?,
                updated_at = ?
            WHERE package_id = ?
            """,
            (
                int(checklist_reviewed),
                reviewed_by,
                utc_now_iso(),
                review_note,
                int(missing_core_acknowledged),
                int(stale_documents_acknowledged),
                int(needs_review_acknowledged),
                utc_now_iso(),
                package_id,
            ),
        )
    return get_package_by_package_id(package_id, db_path=db_path)


def next_package_version_number(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version FROM package_versions WHERE parent_package_id = ?",
            (package_id,),
        ).fetchone()
    return int(row["next_version"])


def format_display_version(ticker: str, research_cutoff_date: str, version_number: int) -> str:
    """Format the package-scoped human-readable version label."""
    clean_ticker = "".join(character if character.isalnum() else "-" for character in str(ticker).upper()).strip("-") or "PACKAGE"
    cutoff = str(research_cutoff_date).replace("-", "")
    return f"{clean_ticker}-{cutoff}-V{int(version_number):03d}"


def allocate_package_version(
    version: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
    max_attempts: int = 8,
) -> dict[str, Any]:
    """Allocate and insert a package version number atomically."""
    initialize_database(db_path)
    for _ in range(max_attempts):
        try:
            with get_connection(db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                latest = connection.execute(
                    "SELECT version_number, version_id FROM package_versions WHERE parent_package_id = ? ORDER BY version_number DESC LIMIT 1",
                    (version["parent_package_id"],),
                ).fetchone()
                version_number = int(latest["version_number"] if latest else 0) + 1
                record = {
                    **version,
                    "version_id": f"PV-{uuid.uuid4().hex.upper()}",
                    "display_version": format_display_version(version["ticker"], version["research_cutoff_date"], version_number),
                    "version_number": version_number,
                    "previous_version_id": latest["version_id"] if latest else None,
                    "created_at": version.get("created_at", utc_now_iso()),
                }
                connection.execute(
                    """
                    INSERT INTO package_versions (
                        version_id, display_version, parent_package_id, version_number, previous_version_id,
                        ticker, company_name, security_type, research_cutoff_date, status,
                        document_count, public_document_count, licensed_document_count, total_size_bytes,
                        checklist_snapshot_json, collection_profile_name, collection_profile_snapshot_json,
                        selected_years_json, selected_months_json, research_window_fingerprint,
                        created_by, created_at, notes, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["version_id"],
                        record["display_version"],
                        record["parent_package_id"],
                        record["version_number"],
                        record.get("previous_version_id"),
                        record["ticker"],
                        record.get("company_name"),
                        record["security_type"],
                        record["research_cutoff_date"],
                        record["status"],
                        record.get("document_count", 0),
                        record.get("public_document_count", 0),
                        record.get("licensed_document_count", 0),
                        record.get("total_size_bytes", 0),
                        record.get("checklist_snapshot_json"),
                        record.get("collection_profile_name"),
                        record.get("collection_profile_snapshot_json"),
                        record.get("selected_years_json"),
                        record.get("selected_months_json"),
                        record.get("research_window_fingerprint"),
                        record.get("created_by", "analyst"),
                        record["created_at"],
                        record.get("notes"),
                        record.get("error_message"),
                    ),
                )
            return get_package_version(record["version_id"], db_path=db_path) or record
        except sqlite3.IntegrityError:
            continue
    raise DatabaseError("Could not allocate a unique package version.")


def latest_package_version(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM package_versions
            WHERE parent_package_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (package_id,),
        ).fetchone()
    return row_to_dict(row)


def create_package_version(
    version: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    version_id = version.get("version_id") or f"PV-{uuid.uuid4().hex.upper()}"
    display_version = version.get("display_version") or format_display_version(
        version["ticker"], version["research_cutoff_date"], int(version["version_number"])
    )
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO package_versions (
                version_id, display_version, parent_package_id, version_number, previous_version_id,
                ticker, company_name, security_type, research_cutoff_date, status,
                document_count, public_document_count, licensed_document_count,
                total_size_bytes, checklist_snapshot_json, collection_profile_name,
                collection_profile_snapshot_json, selected_years_json, selected_months_json,
                research_window_fingerprint, created_by, created_at,
                notes, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                display_version,
                version["parent_package_id"],
                version["version_number"],
                version.get("previous_version_id"),
                version["ticker"],
                version.get("company_name"),
                version["security_type"],
                version["research_cutoff_date"],
                version["status"],
                version.get("document_count", 0),
                version.get("public_document_count", 0),
                version.get("licensed_document_count", 0),
                version.get("total_size_bytes", 0),
                version.get("checklist_snapshot_json"),
                version.get("collection_profile_name"),
                version.get("collection_profile_snapshot_json"),
                version.get("selected_years_json"),
                version.get("selected_months_json"),
                version.get("research_window_fingerprint"),
                version.get("created_by", "analyst"),
                version.get("created_at", utc_now_iso()),
                version.get("notes"),
                version.get("error_message"),
            ),
        )
    return get_package_version(version_id, db_path=db_path) or {}


def get_package_version(
    version_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM package_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
    return row_to_dict(row)


def update_package_version(
    version_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "status",
        "document_count",
        "public_document_count",
        "licensed_document_count",
        "total_size_bytes",
        "checklist_snapshot_json",
        "manifest_path",
        "manifest_sha256",
        "inventory_path",
        "checklist_report_path",
        "integrity_report_path",
        "integrity_status",
        "zip_path",
        "zip_sha256",
        "locked_at",
        "notes",
        "error_message",
        "build_fingerprint",
        "reused_from_version_id",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_package_version(version_id, db_path=db_path)
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE package_versions SET {sql} WHERE version_id = ?",
            (*selected.values(), version_id),
        )
    return get_package_version(version_id, db_path=db_path)


def list_package_versions(
    package_id: str | None = None,
    *,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM package_versions"
    params: list[Any] = []
    if package_id:
        sql += " WHERE parent_package_id = ?"
        params.append(package_id)
    sql += " ORDER BY created_at DESC, version_number DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_package_version_document(
    document: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO package_version_documents (
                version_id, document_id, original_document_id, category, title,
                source_name, source_url, publication_date, original_filename,
                package_filename, relative_package_path, file_size, sha256_hash,
                mime_type, is_public, included_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document["version_id"],
                document["document_id"],
                document["original_document_id"],
                document.get("category"),
                document.get("title"),
                document.get("source_name"),
                document.get("source_url"),
                document.get("publication_date"),
                document.get("original_filename"),
                document.get("package_filename"),
                document["relative_package_path"],
                document["file_size"],
                document["sha256_hash"],
                document.get("mime_type"),
                int(bool(document.get("is_public"))),
                document.get("included_status", "INCLUDED"),
                document.get("created_at", utc_now_iso()),
            ),
        )
    return document


def list_package_version_documents(
    version_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM package_version_documents WHERE version_id = ? ORDER BY relative_package_path",
            (version_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_package_version_event(
    *,
    event_id: str,
    parent_package_id: str,
    event_type: str,
    version_id: str | None = None,
    event_details_json: str | None = None,
    actor: str = "analyst",
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO package_version_events (
                event_id, version_id, parent_package_id, event_type,
                event_details_json, actor, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                version_id,
                parent_package_id,
                event_type,
                event_details_json,
                actor,
                utc_now_iso(),
            ),
        )
    return {"event_id": event_id}


def list_package_version_events(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM package_version_events
            WHERE parent_package_id = ?
            ORDER BY created_at DESC
            """,
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def lock_package_version(
    version_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    version = get_package_version(version_id, db_path=db_path)
    if not version or version["status"] == "LOCKED":
        return version
    return update_package_version(
        version_id,
        {"status": "LOCKED", "locked_at": utc_now_iso()},
        db_path=db_path,
    )


def phase4_dashboard_metrics(*, db_path: Path | str = DATABASE_PATH) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        built = connection.execute(
            "SELECT COUNT(*) AS count FROM package_versions WHERE status IN ('BUILT', 'LOCKED')"
        ).fetchone()
        locked = connection.execute(
            "SELECT COUNT(*) AS count FROM package_versions WHERE status = 'LOCKED'"
        ).fetchone()
        failures = connection.execute(
            "SELECT COUNT(*) AS count FROM package_versions WHERE integrity_status = 'FAILED' OR status = 'BUILD_FAILED'"
        ).fetchone()
        ready = connection.execute(
            "SELECT COUNT(*) AS count FROM packages WHERE checklist_reviewed = 1"
        ).fetchone()
    return {
        "built_versions": int(built["count"]),
        "locked_versions": int(locked["count"]),
        "integrity_failures": int(failures["count"]),
        "packages_ready_to_build": int(ready["count"]),
    }


def _insert_record(
    table: str,
    record: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    initialize_database(db_path)
    clean_record = {key: value for key, value in record.items() if key != "id"}
    keys = list(clean_record.keys())
    placeholders = ", ".join("?" for _ in keys)
    columns = ", ".join(keys)
    with get_connection(db_path) as connection:
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(clean_record[key] for key in keys),
        )
    return clean_record


def _insert_records(
    table: str,
    records: list[dict[str, Any]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    if not records:
        return []
    initialize_database(db_path)
    clean_records = [{key: value for key, value in record.items() if key != "id"} for record in records]
    keys = list(clean_records[0])
    if any(list(record) != keys for record in clean_records):
        raise ValueError("Batch records must use the same ordered fields.")
    placeholders = ", ".join("?" for _ in keys)
    columns = ", ".join(keys)
    with get_connection(db_path) as connection:
        connection.executemany(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            [tuple(record[key] for key in keys) for record in clean_records],
        )
    return clean_records


def create_processing_run(
    run: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    _insert_record("processing_runs", run, db_path=db_path)
    return get_processing_run(run["processing_run_id"], db_path=db_path) or run


def get_processing_run(
    processing_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM processing_runs WHERE processing_run_id = ?",
            (processing_run_id,),
        ).fetchone()
    return row_to_dict(row)


def update_processing_run(
    processing_run_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "completed_at",
        "total_documents",
        "successful_documents",
        "partial_documents",
        "failed_documents",
        "pages_processed",
        "tables_detected",
        "sheets_processed",
        "chunks_created",
        "evidence_records_created",
        "warnings_json",
        "errors_json",
        "status",
        "processing_fingerprint",
        "reused_from_processing_run_id",
        "duration_seconds",
        "last_checkpoint_at",
        "resume_count",
        "reused_documents",
        "database_write_seconds",
        "chunking_seconds",
        "deterministic_extraction_seconds",
        "conflict_analysis_seconds",
        "openai_extraction_seconds",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_processing_run(processing_run_id, db_path=db_path)
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE processing_runs SET {sql} WHERE processing_run_id = ?",
            (*selected.values(), processing_run_id),
        )
    return get_processing_run(processing_run_id, db_path=db_path)


def list_processing_runs(
    version_id: str | None = None,
    *,
    package_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM processing_runs"
    params: list[Any] = []
    clauses: list[str] = []
    if version_id:
        clauses.append("version_id = ?")
        params.append(version_id)
    if package_id:
        clauses.append("package_id = ?")
        params.append(package_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_document_processing_result(
    result: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("document_processing_results", result, db_path=db_path)


def upsert_processing_document_item(
    item: dict[str, Any], *, db_path: Path | str = DATABASE_PATH
) -> dict[str, Any]:
    initialize_database(db_path)
    fields = (
        "processing_run_id", "version_id", "version_document_id", "processing_fingerprint",
        "status", "attempt_count", "reuse_status", "parse_started_at", "parse_completed_at",
        "parse_duration_seconds", "file_size_bytes", "document_type", "extracted_character_count",
        "page_count", "chunk_count", "evidence_count", "warning_count", "error_message", "updated_at",
    )
    with get_connection(db_path) as connection:
        connection.execute(
            f"""
            INSERT INTO processing_document_items ({', '.join(fields)})
            VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(processing_run_id, version_document_id) DO UPDATE SET
                processing_fingerprint=excluded.processing_fingerprint,
                status=excluded.status,
                attempt_count=excluded.attempt_count,
                reuse_status=excluded.reuse_status,
                parse_started_at=excluded.parse_started_at,
                parse_completed_at=excluded.parse_completed_at,
                parse_duration_seconds=excluded.parse_duration_seconds,
                file_size_bytes=excluded.file_size_bytes,
                document_type=excluded.document_type,
                extracted_character_count=excluded.extracted_character_count,
                page_count=excluded.page_count,
                chunk_count=excluded.chunk_count,
                evidence_count=excluded.evidence_count,
                warning_count=excluded.warning_count,
                error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            tuple(item.get(field) for field in fields),
        )
    return item


def initialize_processing_document_items(
    items: list[dict[str, Any]], *, db_path: Path | str = DATABASE_PATH
) -> None:
    if not items:
        return
    initialize_database(db_path)
    fields = tuple(items[0])
    with get_connection(db_path) as connection:
        connection.executemany(
            f"INSERT OR IGNORE INTO processing_document_items ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
            [tuple(item[field] for field in fields) for item in items],
        )


def list_processing_document_items(
    processing_run_id: str, *, db_path: Path | str = DATABASE_PATH
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM processing_document_items WHERE processing_run_id = ? ORDER BY version_document_id",
            (processing_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def commit_processed_document(
    *,
    result: dict[str, Any],
    item: dict[str, Any],
    pages: list[dict[str, Any]],
    sheets: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
    db_path: Path | str = DATABASE_PATH,
) -> None:
    """Replace one run/document's derived rows in a single serialized transaction."""
    initialize_database(db_path)
    run_id = str(item["processing_run_id"])
    document_id = str(item["version_document_id"])
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        evidence_ids = [
            row["evidence_id"]
            for row in connection.execute(
                "SELECT evidence_id FROM evidence_records WHERE processing_run_id = ? AND version_document_id = ?",
                (run_id, document_id),
            ).fetchall()
        ]
        if evidence_ids:
            placeholders = ", ".join("?" for _ in evidence_ids)
            connection.execute(f"DELETE FROM citation_verifications WHERE evidence_id IN ({placeholders})", evidence_ids)
            connection.execute(
                f"DELETE FROM claim_conflicts WHERE evidence_id_a IN ({placeholders}) OR evidence_id_b IN ({placeholders})",
                (*evidence_ids, *evidence_ids),
            )
        connection.execute(
            "DELETE FROM evidence_records WHERE processing_run_id = ? AND version_document_id = ?",
            (run_id, document_id),
        )
        for table in ("document_chunks", "document_pages", "document_sheets", "document_processing_results"):
            connection.execute(
                f"DELETE FROM {table} WHERE processing_run_id = ? AND version_document_id = ?",
                (run_id, document_id),
            )
        connection.execute(
            "UPDATE document_chunks SET duplicate_group_id = NULL WHERE processing_run_id = ?", (run_id,)
        )
        connection.execute(
            "DELETE FROM content_duplicate_groups WHERE processing_run_id = ?", (run_id,)
        )
        _connection_insert(connection, "document_processing_results", result)
        for table, rows in (
            ("document_pages", pages),
            ("document_sheets", sheets),
            ("document_chunks", chunks),
            ("evidence_records", evidence),
            ("citation_verifications", verifications),
        ):
            _connection_insert_many(connection, table, rows)
        if evidence:
            connection.executemany(
                "UPDATE evidence_records SET verification_status = ? WHERE evidence_id = ?",
                [(verification["support_status"], verification["evidence_id"]) for verification in verifications],
            )
        fields = (
            "processing_run_id", "version_id", "version_document_id", "processing_fingerprint",
            "status", "attempt_count", "reuse_status", "parse_started_at", "parse_completed_at",
            "parse_duration_seconds", "file_size_bytes", "document_type", "extracted_character_count",
            "page_count", "chunk_count", "evidence_count", "warning_count", "error_message", "updated_at",
        )
        connection.execute(
            f"""
            INSERT INTO processing_document_items ({', '.join(fields)})
            VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(processing_run_id, version_document_id) DO UPDATE SET
                processing_fingerprint=excluded.processing_fingerprint, status=excluded.status,
                attempt_count=excluded.attempt_count, reuse_status=excluded.reuse_status,
                parse_started_at=excluded.parse_started_at, parse_completed_at=excluded.parse_completed_at,
                parse_duration_seconds=excluded.parse_duration_seconds, file_size_bytes=excluded.file_size_bytes,
                document_type=excluded.document_type, extracted_character_count=excluded.extracted_character_count,
                page_count=excluded.page_count, chunk_count=excluded.chunk_count,
                evidence_count=excluded.evidence_count, warning_count=excluded.warning_count,
                error_message=excluded.error_message, updated_at=excluded.updated_at
            """,
            tuple(item.get(field) for field in fields),
        )


def _connection_insert(connection: sqlite3.Connection, table: str, record: dict[str, Any]) -> None:
    clean = {key: value for key, value in record.items() if key != "id"}
    connection.execute(
        f"INSERT INTO {table} ({', '.join(clean)}) VALUES ({', '.join('?' for _ in clean)})",
        tuple(clean.values()),
    )


def _connection_insert_many(connection: sqlite3.Connection, table: str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    clean = [{key: value for key, value in record.items() if key != "id"} for record in records]
    keys = list(clean[0])
    if any(list(record) != keys for record in clean):
        raise ValueError("Batch records must use the same ordered fields.")
    connection.executemany(
        f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
        [tuple(record[key] for key in keys) for record in clean],
    )


def create_processing_stage_timing(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("processing_stage_timings", record, db_path=db_path)


def list_processing_stage_timings(processing_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM processing_stage_timings WHERE processing_run_id = ? ORDER BY duration_seconds DESC, timing_id",
            (processing_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_conflict_analysis_summary(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    initialize_database(db_path)
    fields = tuple(record)
    updates = ", ".join(f"{field}=excluded.{field}" for field in fields if field != "processing_run_id")
    with get_connection(db_path) as connection:
        connection.execute(
            f"INSERT INTO conflict_analysis_summaries ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)}) "
            f"ON CONFLICT(processing_run_id) DO UPDATE SET {updates}",
            tuple(record[field] for field in fields),
        )
    return record


def get_conflict_analysis_summary(processing_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM conflict_analysis_summaries WHERE processing_run_id = ?", (processing_run_id,)
        ).fetchone()
    return row_to_dict(row)


def list_document_processing_results(
    processing_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM document_processing_results
            WHERE processing_run_id = ?
            ORDER BY version_document_id, id
            """,
            (processing_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_document_page(
    page: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("document_pages", page, db_path=db_path)


def list_document_pages(
    processing_run_id: str,
    version_document_id: str | None = None,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM document_pages WHERE processing_run_id = ?"
    params: list[Any] = [processing_run_id]
    if version_document_id:
        sql += " AND version_document_id = ?"
        params.append(version_document_id)
    sql += " ORDER BY version_document_id, page_number"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_document_sheet(
    sheet: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("document_sheets", sheet, db_path=db_path)


def list_document_sheets(
    processing_run_id: str,
    version_document_id: str | None = None,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM document_sheets WHERE processing_run_id = ?"
    params: list[Any] = [processing_run_id]
    if version_document_id:
        sql += " AND version_document_id = ?"
        params.append(version_document_id)
    sql += " ORDER BY version_document_id, sheet_index"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_document_chunk(
    chunk: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("document_chunks", chunk, db_path=db_path)


def create_document_chunks(
    chunks: list[dict[str, Any]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    return _insert_records("document_chunks", chunks, db_path=db_path)


def update_document_chunk_duplicate_group(
    chunk_id: str,
    duplicate_group_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE document_chunks SET duplicate_group_id = ? WHERE chunk_id = ?",
            (duplicate_group_id, chunk_id),
        )


def list_document_chunks(
    processing_run_id: str,
    *,
    version_id: str | None = None,
    version_document_id: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM document_chunks WHERE processing_run_id = ?"
    params: list[Any] = [processing_run_id]
    if version_id:
        sql += " AND version_id = ?"
        params.append(version_id)
    if version_document_id:
        sql += " AND version_document_id = ?"
        params.append(version_document_id)
    sql += " ORDER BY version_document_id, chunk_index"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_evidence_record(
    evidence: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("evidence_records", evidence, db_path=db_path)


def create_evidence_records(
    evidence: list[dict[str, Any]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    return _insert_records("evidence_records", evidence, db_path=db_path)


def get_evidence_record(
    evidence_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM evidence_records WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
    return row_to_dict(row)


def get_evidence_by_fingerprint(
    processing_run_id: str,
    extraction_fingerprint: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    if not extraction_fingerprint:
        return None
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM evidence_records WHERE processing_run_id = ? AND extraction_fingerprint = ? LIMIT 1",
            (processing_run_id, extraction_fingerprint),
        ).fetchone()
    return row_to_dict(row)


def list_evidence_records(
    processing_run_id: str,
    *,
    version_id: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM evidence_records WHERE processing_run_id = ?"
    params: list[Any] = [processing_run_id]
    if version_id:
        sql += " AND version_id = ?"
        params.append(version_id)
    sql += " ORDER BY created_at, evidence_id"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def update_evidence_record(
    evidence_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "verification_status",
        "analyst_status",
        "analyst_note",
        "claim_text",
        "evidence_type",
        "normalized_subject",
        "metric_name",
        "value",
        "unit",
        "currency",
        "period",
        "scenario",
        "direction",
        "updated_at",
        "extraction_method",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_evidence_record(evidence_id, db_path=db_path)
    if "updated_at" not in selected:
        selected["updated_at"] = utc_now_iso()
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE evidence_records SET {sql} WHERE evidence_id = ?",
            (*selected.values(), evidence_id),
        )
    return get_evidence_record(evidence_id, db_path=db_path)


def update_evidence_analyst_status(
    evidence_id: str,
    analyst_status: str,
    analyst_note: str | None = None,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    return update_evidence_record(
        evidence_id,
        {"analyst_status": analyst_status, "analyst_note": analyst_note or ""},
        db_path=db_path,
    )


def create_citation_verification(
    verification: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("citation_verifications", verification, db_path=db_path)


def list_citation_verifications(
    evidence_id: str | None = None,
    *,
    processing_run_id: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    if evidence_id:
        with get_connection(db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM citation_verifications WHERE evidence_id = ? ORDER BY created_at DESC",
                (evidence_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    if processing_run_id:
        with get_connection(db_path) as connection:
            rows = connection.execute(
                """
                SELECT cv.*
                FROM citation_verifications cv
                JOIN evidence_records er ON er.evidence_id = cv.evidence_id
                WHERE er.processing_run_id = ?
                ORDER BY cv.created_at DESC
                """,
                (processing_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    return []


def create_duplicate_group(
    duplicate_group: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    return _insert_record("content_duplicate_groups", duplicate_group, db_path=db_path)


def list_duplicate_groups(
    processing_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM content_duplicate_groups
            WHERE processing_run_id = ?
            ORDER BY member_count DESC, duplicate_group_id
            """,
            (processing_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_claim_conflict(
    conflict: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    try:
        return _insert_record("claim_conflicts", conflict, db_path=db_path)
    except sqlite3.IntegrityError:
        fingerprint = conflict.get("conflict_fingerprint")
        if not fingerprint:
            raise
        initialize_database(db_path)
        with get_connection(db_path) as connection:
            row = connection.execute(
                "SELECT * FROM claim_conflicts WHERE conflict_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else conflict


def list_claim_conflicts(
    processing_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM claim_conflicts
            WHERE processing_run_id = ?
            ORDER BY severity DESC, created_at DESC
            """,
            (processing_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def find_package_version_by_build_fingerprint(
    package_id: str,
    build_fingerprint: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM package_versions
            WHERE parent_package_id = ? AND build_fingerprint = ?
              AND status IN ('BUILT', 'LOCKED')
            ORDER BY version_number DESC LIMIT 1
            """,
            (package_id, build_fingerprint),
        ).fetchone()
    return row_to_dict(row)


def update_package_official_sites(
    package_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "official_website_url", "official_website_domain", "official_website_confidence",
        "official_website_source", "official_website_checked_at", "official_ir_url",
        "official_ir_domain", "official_ir_confirmed",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_package_by_package_id(package_id, db_path=db_path)
    selected["updated_at"] = utc_now_iso()
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        sql = ", ".join(f"{key} = ?" for key in selected)
        connection.execute(f"UPDATE packages SET {sql} WHERE package_id = ?", (*selected.values(), package_id))
    return get_package_by_package_id(package_id, db_path=db_path)


def upsert_official_website_candidate(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    initialize_database(db_path)
    fields = (
        "candidate_id", "package_id", "url", "domain", "discovery_source", "discovered_at", "confidence",
        "validation_reasons_json", "rejection_reasons_json", "analyst_confirmation_status", "is_verified",
    )
    with get_connection(db_path) as connection:
        connection.execute(
            f"""
            INSERT INTO official_website_candidates ({', '.join(fields)})
            VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(package_id, url) DO UPDATE SET
              domain=excluded.domain, discovery_source=excluded.discovery_source, discovered_at=excluded.discovered_at,
              confidence=excluded.confidence, validation_reasons_json=excluded.validation_reasons_json,
              rejection_reasons_json=excluded.rejection_reasons_json,
              analyst_confirmation_status=excluded.analyst_confirmation_status, is_verified=excluded.is_verified
            """,
            tuple(record.get(field) for field in fields),
        )
        row = connection.execute(
            "SELECT * FROM official_website_candidates WHERE package_id = ? AND url = ?",
            (record["package_id"], record["url"]),
        ).fetchone()
    return dict(row) if row else record


def list_official_website_candidates(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM official_website_candidates WHERE package_id = ? ORDER BY is_verified DESC, discovered_at DESC",
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_ir_discovery_run(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("ir_discovery_runs", record, db_path=db_path)


def update_ir_discovery_run(run_id: str, updates: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    allowed = {
        "official_url", "official_domain", "ir_url", "ir_domain", "status", "completed_at", "duration_seconds",
        "pages_crawled", "materials_discovered", "materials_downloaded", "materials_needing_review", "warnings_json", "errors_json",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        if selected:
            sql = ", ".join(f"{key} = ?" for key in selected)
            connection.execute(f"UPDATE ir_discovery_runs SET {sql} WHERE discovery_run_id = ?", (*selected.values(), run_id))
        row = connection.execute("SELECT * FROM ir_discovery_runs WHERE discovery_run_id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


def list_ir_discovery_runs(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM ir_discovery_runs WHERE package_id = ? ORDER BY started_at DESC", (package_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_ir_material_candidate(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    initialize_database(db_path)
    fields = tuple(record.keys())
    updates = [field for field in fields if field not in {"candidate_id", "package_id", "canonical_url", "created_at"}]
    with get_connection(db_path) as connection:
        connection.execute(
            f"""
            INSERT INTO ir_material_candidates ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(package_id, canonical_url) DO UPDATE SET {', '.join(f'{field}=excluded.{field}' for field in updates)}
            """,
            tuple(record[field] for field in fields),
        )
        row = connection.execute(
            "SELECT * FROM ir_material_candidates WHERE package_id = ? AND canonical_url = ?",
            (record["package_id"], record["canonical_url"]),
        ).fetchone()
    return dict(row) if row else record


def list_ir_material_candidates(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM ir_material_candidates WHERE package_id = ? ORDER BY publication_date DESC, title",
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_ir_material_candidate(candidate_id: str, updates: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any] | None:
    allowed = {
        "selected", "download_status", "rejection_reason", "confidence", "category",
        "analyst_approved", "approval_timestamp", "original_confidence", "final_download_result",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        if selected:
            sql = ", ".join(f"{key} = ?" for key in selected)
            connection.execute(f"UPDATE ir_material_candidates SET {sql} WHERE candidate_id = ?", (*selected.values(), candidate_id))
        row = connection.execute("SELECT * FROM ir_material_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return row_to_dict(row)


def create_recommendation_attempt(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("recommendation_attempts", record, db_path=db_path)


def update_recommendation_attempt(
    attempt_id: str, updates: dict[str, Any], *, db_path: Path | str = DATABASE_PATH
) -> dict[str, Any] | None:
    allowed = {
        "status", "model", "endpoint", "original_evidence_count", "eligible_candidate_count",
        "supporting_candidate_count", "risk_candidate_count", "metric_count", "conflict_count",
        "openai_call_count", "failure_category", "diagnostics_json", "completed_at",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        if selected:
            sql = ", ".join(f"{key} = ?" for key in selected)
            connection.execute(f"UPDATE recommendation_attempts SET {sql} WHERE attempt_id = ?", (*selected.values(), attempt_id))
        row = connection.execute("SELECT * FROM recommendation_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return row_to_dict(row)


def list_recommendation_attempts(
    analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM recommendation_attempts WHERE analysis_run_id = ? ORDER BY created_at DESC",
            (analysis_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_narrative_candidate(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("narrative_candidates", record, db_path=db_path)


def list_narrative_candidates(attempt_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM narrative_candidates WHERE attempt_id = ? ORDER BY selected DESC, rank_score DESC, candidate_id",
            (attempt_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_recommendation_stage_event(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("recommendation_stage_events", record, db_path=db_path)


def list_recommendation_stage_events(attempt_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM recommendation_stage_events WHERE attempt_id = ? ORDER BY id", (attempt_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def create_openai_usage_record(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("openai_usage_ledger", record, db_path=db_path)


def list_openai_usage(
    *, analysis_run_id: str | None = None, processing_run_id: str | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    clauses: list[str] = []
    values: list[Any] = []
    if analysis_run_id:
        clauses.append("analysis_run_id = ?")
        values.append(analysis_run_id)
    if processing_run_id:
        clauses.append("processing_run_id = ?")
        values.append(processing_run_id)
    sql = "SELECT * FROM openai_usage_ledger"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(values)).fetchall()
    return [dict(row) for row in rows]


def create_workflow_stage_performance(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("workflow_stage_performance", record, db_path=db_path)


def list_workflow_stage_performance(
    *, workflow_run_id: str | None = None, package_id: str | None = None, db_path: Path | str = DATABASE_PATH
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if workflow_run_id:
        clauses.append("workflow_run_id = ?")
        params.append(workflow_run_id)
    if package_id:
        clauses.append("package_id = ?")
        params.append(package_id)
    sql = "SELECT * FROM workflow_stage_performance"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at"
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def get_openai_chunk_extraction(
    processing_run_id: str, chunk_hash: str, model: str, prompt_version: str, schema_version: str,
    *, db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM openai_chunk_extractions
            WHERE processing_run_id = ? AND chunk_hash = ? AND model = ? AND prompt_version = ? AND schema_version = ?
            """,
            (processing_run_id, chunk_hash, model, prompt_version, schema_version),
        ).fetchone()
    return row_to_dict(row)


def replace_draft_document_inclusions(
    package_id: str,
    rows: list[dict[str, Any]],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute("DELETE FROM draft_document_inclusions WHERE package_id = ?", (package_id,))
        connection.executemany(
            """
            INSERT INTO draft_document_inclusions(package_id, document_id, included, reason, profile_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (package_id, row["document_id"], int(bool(row.get("included"))), row.get("reason"), row.get("profile_name"), utc_now_iso())
                for row in rows
            ],
        )


def list_draft_document_inclusions(package_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM draft_document_inclusions WHERE package_id = ?", (package_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_openai_chunk_extraction(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    initialize_database(db_path)
    fields = ("cache_key", "processing_run_id", "chunk_hash", "model", "prompt_version", "schema_version", "status", "evidence_count", "completed_at")
    with get_connection(db_path) as connection:
        connection.execute(
            f"""
            INSERT INTO openai_chunk_extractions ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(cache_key) DO UPDATE SET status=excluded.status, evidence_count=excluded.evidence_count, completed_at=excluded.completed_at
            """,
            tuple(record.get(field) for field in fields),
        )
    return record


def phase5_dashboard_metrics(*, db_path: Path | str = DATABASE_PATH) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        runs = connection.execute("SELECT COUNT(*) AS count FROM processing_runs").fetchone()
        completed = connection.execute(
            "SELECT COUNT(*) AS count FROM processing_runs WHERE status IN ('COMPLETED', 'COMPLETED_WITH_WARNINGS')"
        ).fetchone()
        evidence = connection.execute("SELECT COUNT(*) AS count FROM evidence_records").fetchone()
        conflicts = connection.execute("SELECT COUNT(*) AS count FROM claim_conflicts").fetchone()
    return {
        "processing_runs": int(runs["count"]),
        "completed_processing_runs": int(completed["count"]),
        "evidence_records": int(evidence["count"]),
        "claim_conflicts": int(conflicts["count"]),
    }


def create_analysis_run(
    run: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    _insert_record("analysis_runs", run, db_path=db_path)
    return get_analysis_run(run["analysis_run_id"], db_path=db_path) or run


def get_analysis_run(
    analysis_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM analysis_runs WHERE analysis_run_id = ?",
            (analysis_run_id,),
        ).fetchone()
    return row_to_dict(row)


def update_analysis_run(
    analysis_run_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "updated_at",
        "status",
        "preliminary_recommendation",
        "analyst_adjusted_recommendation",
        "pm_approved_recommendation",
        "confidence",
        "evidence_coverage",
        "package_coverage",
        "reference_price",
        "reference_price_currency",
        "reference_price_date",
        "reference_price_evidence_id",
        "time_horizon",
        "analyst_notes",
        "pm_notes",
        "error_message",
        "ai_review_status",
        "ai_model",
        "ai_endpoint",
        "openai_diagnostics_json",
        "memo_generation_status",
        "memo_generation_error",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_analysis_run(analysis_run_id, db_path=db_path)
    selected.setdefault("updated_at", utc_now_iso())
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE analysis_runs SET {sql} WHERE analysis_run_id = ?",
            (*selected.values(), analysis_run_id),
        )
    return get_analysis_run(analysis_run_id, db_path=db_path)


def list_analysis_runs(
    version_id: str | None = None,
    *,
    processing_run_id: str | None = None,
    package_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM analysis_runs"
    clauses: list[str] = []
    params: list[Any] = []
    if version_id:
        clauses.append("version_id = ?")
        params.append(version_id)
    if processing_run_id:
        clauses.append("processing_run_id = ?")
        params.append(processing_run_id)
    if package_id:
        clauses.append("package_id = ?")
        params.append(package_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_analysis_metric(metric: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("analysis_metrics", metric, db_path=db_path)


def list_analysis_metrics(analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM analysis_metrics WHERE analysis_run_id = ? ORDER BY metric_code, period, scenario",
            (analysis_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_scorecard_item(item: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("analysis_scorecard_items", item, db_path=db_path)


def list_scorecard_items(analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM analysis_scorecard_items WHERE analysis_run_id = ? ORDER BY pillar_code",
            (analysis_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_scorecard_item(
    item_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {"analyst_override_score", "analyst_override_rationale", "effective_score", "weighted_score", "updated_at"}
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return None
    selected.setdefault("updated_at", utc_now_iso())
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(f"UPDATE analysis_scorecard_items SET {sql} WHERE item_id = ?", (*selected.values(), item_id))
        row = connection.execute("SELECT * FROM analysis_scorecard_items WHERE item_id = ?", (item_id,)).fetchone()
    return row_to_dict(row)


def create_analysis_scenario(scenario: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("analysis_scenarios", scenario, db_path=db_path)


def list_analysis_scenarios(analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM analysis_scenarios WHERE analysis_run_id = ? ORDER BY scenario_name",
            (analysis_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_analysis_scenario(
    scenario_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "scenario_assumptions_json",
        "revenue_assumption",
        "margin_assumption",
        "earnings_assumption",
        "multiple_assumption",
        "implied_value",
        "reference_price",
        "upside_downside",
        "probability",
        "analyst_overrides_json",
        "warnings_json",
        "updated_at",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return None
    selected.setdefault("updated_at", utc_now_iso())
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(f"UPDATE analysis_scenarios SET {sql} WHERE scenario_id = ?", (*selected.values(), scenario_id))
        row = connection.execute("SELECT * FROM analysis_scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()
    return row_to_dict(row)


def create_thesis_item(item: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("analysis_thesis_items", item, db_path=db_path)


def list_thesis_items(analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM analysis_thesis_items WHERE analysis_run_id = ? ORDER BY item_type, thesis_item_id",
            (analysis_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_recommendation_decision(decision: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    _insert_record("recommendation_decisions", decision, db_path=db_path)
    return get_recommendation_decision(decision["analysis_run_id"], db_path=db_path) or decision


def get_recommendation_decision(
    analysis_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM recommendation_decisions WHERE analysis_run_id = ? ORDER BY generated_at DESC LIMIT 1",
            (analysis_run_id,),
        ).fetchone()
    return row_to_dict(row)


def update_recommendation_decision(
    analysis_run_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "effective_rating",
        "analyst_decision",
        "analyst_identity",
        "analyst_decision_at",
        "pm_decision",
        "pm_identity",
        "pm_decision_at",
        "pm_note",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_recommendation_decision(analysis_run_id, db_path=db_path)
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"""
            UPDATE recommendation_decisions
            SET {sql}
            WHERE id = (
                SELECT id FROM recommendation_decisions
                WHERE analysis_run_id = ?
                ORDER BY generated_at DESC LIMIT 1
            )
            """,
            (*selected.values(), analysis_run_id),
        )
    return get_recommendation_decision(analysis_run_id, db_path=db_path)


def next_report_version(analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH) -> int:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(report_version), 0) + 1 AS next_version FROM generated_reports WHERE analysis_run_id = ?",
            (analysis_run_id,),
        ).fetchone()
    return int(row["next_version"])


def create_generated_report(report: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("generated_reports", report, db_path=db_path)


def list_generated_reports(
    analysis_run_id: str | None = None,
    *,
    version_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM generated_reports"
    clauses: list[str] = []
    params: list[Any] = []
    if analysis_run_id:
        clauses.append("analysis_run_id = ?")
        params.append(analysis_run_id)
    if version_id:
        clauses.append("version_id = ?")
        params.append(version_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, report_version DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def create_memo_generation_attempt(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("memo_generation_attempts", record, db_path=db_path)


def update_memo_generation_attempt(
    attempt_id: str, updates: dict[str, Any], *, db_path: Path | str = DATABASE_PATH
) -> dict[str, Any] | None:
    allowed = {
        "status", "model", "endpoint", "selected_candidate_ids_json", "rejected_candidate_count",
        "draft_json", "error_code", "error_message", "completed_at",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        if selected:
            sql = ", ".join(f"{key} = ?" for key in selected)
            connection.execute(f"UPDATE memo_generation_attempts SET {sql} WHERE attempt_id = ?", (*selected.values(), attempt_id))
        row = connection.execute("SELECT * FROM memo_generation_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return row_to_dict(row)


def latest_memo_generation_attempt(
    analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM memo_generation_attempts WHERE analysis_run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (analysis_run_id,),
        ).fetchone()
    return row_to_dict(row)


def create_memo_evidence_candidates(
    records: list[dict[str, Any]], *, db_path: Path | str = DATABASE_PATH
) -> None:
    if not records:
        return
    initialize_database(db_path)
    fields = tuple(records[0])
    with get_connection(db_path) as connection:
        connection.executemany(
            f"INSERT INTO memo_evidence_candidates ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            [tuple(record[field] for field in fields) for record in records],
        )


def list_memo_evidence_candidates(
    attempt_id: str, *, db_path: Path | str = DATABASE_PATH
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM memo_evidence_candidates WHERE attempt_id = ? ORDER BY eligible_for_memo DESC, decision_relevance_score DESC, recency_score DESC, candidate_id",
            (attempt_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_memo_quality_audit(record: dict[str, Any], *, db_path: Path | str = DATABASE_PATH) -> dict[str, Any]:
    return _insert_record("memo_quality_audits", record, db_path=db_path)


def latest_memo_quality_audit(
    analysis_run_id: str, *, db_path: Path | str = DATABASE_PATH
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM memo_quality_audits WHERE analysis_run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (analysis_run_id,),
        ).fetchone()
    return row_to_dict(row)


def create_research_workflow_run(
    workflow: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    try:
        _insert_record("research_workflow_runs", workflow, db_path=db_path)
    except sqlite3.IntegrityError:
        existing = get_research_workflow_by_key(workflow.get("idempotency_key") or "", db_path=db_path)
        if existing:
            return existing
        raise
    return get_research_workflow_run(workflow["workflow_run_id"], db_path=db_path) or workflow


def get_research_workflow_run(
    workflow_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM research_workflow_runs WHERE workflow_run_id = ?",
            (workflow_run_id,),
        ).fetchone()
    return row_to_dict(row)


def get_research_workflow_by_key(
    idempotency_key: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM research_workflow_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return row_to_dict(row)


def latest_research_workflow_run(
    package_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM research_workflow_runs
            WHERE package_id = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (package_id,),
        ).fetchone()
    return row_to_dict(row)


def list_research_workflow_runs(
    package_id: str | None = None,
    *,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM research_workflow_runs"
    params: list[Any] = []
    if package_id:
        sql += " WHERE package_id = ?"
        params.append(package_id)
    sql += " ORDER BY started_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def update_research_workflow_run(
    workflow_run_id: str,
    updates: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    allowed = {
        "status",
        "current_step",
        "version_id",
        "processing_run_id",
        "analysis_run_id",
        "report_id",
        "stage_statuses_json",
        "warnings_json",
        "errors_json",
        "error_message",
        "updated_at",
        "completed_at",
    }
    selected = {key: value for key, value in updates.items() if key in allowed}
    if not selected:
        return get_research_workflow_run(workflow_run_id, db_path=db_path)
    selected.setdefault("updated_at", utc_now_iso())
    sql = ", ".join(f"{key} = ?" for key in selected)
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE research_workflow_runs SET {sql} WHERE workflow_run_id = ?",
            (*selected.values(), workflow_run_id),
        )
    return get_research_workflow_run(workflow_run_id, db_path=db_path)


def next_combined_export_version(
    analysis_run_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(export_version), 0) + 1 AS next_version FROM combined_exports WHERE analysis_run_id = ?",
            (analysis_run_id,),
        ).fetchone()
    return int(row["next_version"])


def create_combined_export(
    export: dict[str, Any],
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any]:
    _insert_record("combined_exports", export, db_path=db_path)
    return get_combined_export(export["export_id"], db_path=db_path) or export


def get_combined_export(
    export_id: str,
    *,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM combined_exports WHERE export_id = ?",
            (export_id,),
        ).fetchone()
    return row_to_dict(row)


def list_combined_exports(
    analysis_run_id: str | None = None,
    *,
    version_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str = DATABASE_PATH,
) -> list[dict[str, Any]]:
    initialize_database(db_path)
    sql = "SELECT * FROM combined_exports"
    clauses: list[str] = []
    params: list[Any] = []
    if analysis_run_id:
        clauses.append("analysis_run_id = ?")
        params.append(analysis_run_id)
    if version_id:
        clauses.append("version_id = ?")
        params.append(version_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, export_version DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with get_connection(db_path) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def phase6_dashboard_metrics(*, db_path: Path | str = DATABASE_PATH) -> dict[str, int]:
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        runs = connection.execute("SELECT COUNT(*) AS count FROM analysis_runs").fetchone()
        approved = connection.execute("SELECT COUNT(*) AS count FROM analysis_runs WHERE status = 'PM_APPROVED'").fetchone()
        reports = connection.execute("SELECT COUNT(*) AS count FROM generated_reports").fetchone()
        finals = connection.execute("SELECT COUNT(*) AS count FROM generated_reports WHERE report_status = 'FINAL'").fetchone()
    return {
        "analysis_runs": int(runs["count"]),
        "pm_approved_runs": int(approved["count"]),
        "investment_reports": int(reports["count"]),
        "final_reports": int(finals["count"]),
    }
