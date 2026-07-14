from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

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
        _create_phase5_tables(connection)
        _create_phase6_tables(connection)
        _create_phase7_tables(connection)


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


def update_package_research_settings(
    package_id: str,
    *,
    filing_history_years: int,
    research_cutoff_date: str,
    db_path: Path | str = DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update editable research settings on the working package."""
    initialize_database(db_path)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE packages
            SET filing_history_years = ?, research_cutoff_date = ?, updated_at = ?
            WHERE package_id = ?
            """,
            (filing_history_years, research_cutoff_date, utc_now_iso(), package_id),
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
                        checklist_snapshot_json, created_by, created_at, notes, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                total_size_bytes, checklist_snapshot_json, created_by, created_at,
                notes, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return _insert_record("claim_conflicts", conflict, db_path=db_path)


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
