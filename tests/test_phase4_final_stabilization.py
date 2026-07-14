from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services import processing_pipeline
from app.services.checklist_service import ensure_package_checklist
from app.services.collectors import sec_collector
from app.services.evidence_service import evidence_from_chunk
from app.services.official_ir_service import _material
from app.services.package_builder import build_package_version, lock_version
from app.services.package_service import PackageInput, create_package
from app.services.research_window import ALL_MONTHS, document_window_status, normalize_window
from app.services.reporting.investment_report import _stable_rows
from app.services.upload_service import UploadCandidate, store_uploaded_files
from app.utils import database


@pytest.fixture(autouse=True)
def phase4_final_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    monkeypatch.setattr(config, "PACKAGE_DIR", tmp_path / "packages")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(config, "CHUNK_SIZE", 400)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 40)
    monkeypatch.setattr(config, "OCR_ENABLED", False)
    monkeypatch.setattr(config, "PROCESSING_CONCURRENCY_ENABLED", True)


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase4-final.db"
    database.initialize_database(path)
    return path


def _package(temp_db: Path, *, years: tuple[int, ...] = (2026,), months: tuple[int, ...] = (1, 2, 3)) -> dict:
    package = create_package(
        PackageInput(
            ticker="TST",
            security_type="Common Equity",
            research_cutoff_date=date(2026, 7, 14),
            filing_history_years=1,
            selected_years=years,
            selected_months=months,
        ),
        db_path=temp_db,
    )
    package = database.update_package_company_metadata(
        package["package_id"],
        {
            "ticker": "TST",
            "company_name": "Test Company",
            "cik": "0001234567",
            "exchange": "NYSE",
            "sic": "7370",
            "industry_description": "Services",
            "fiscal_year_end": "1231",
            "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0001234567",
            "resolution_status": "RESOLVED",
            "resolution_source": "test",
            "resolution_timestamp": database.utc_now_iso(),
        },
        db_path=temp_db,
    )
    ensure_package_checklist(package, db_path=temp_db)
    return database.update_package_review_acknowledgement(
        package["package_id"],
        checklist_reviewed=True,
        reviewed_by="test",
        review_note="Phase 4 isolated test package.",
        missing_core_acknowledged=True,
        stale_documents_acknowledged=True,
        needs_review_acknowledged=True,
        db_path=temp_db,
    )


def _locked_text_package(temp_db: Path, count: int = 3) -> dict:
    package = _package(temp_db)
    uploads = [
        UploadCandidate(
            f"document-{index}.txt",
            f"Revenue was ${100 + index} million in Q1 2026. Debt was ${20 + index} million in Q1 2026.".encode(),
        )
        for index in range(1, count + 1)
    ]
    store_uploaded_files(
        package,
        uploads,
        source_type="other",
        authorization_confirmed=True,
        metadata_by_name={item.original_filename: {"final_category_code": "other", "document_date": "2026-03-31"} for item in uploads},
        db_path=temp_db,
    )
    built = build_package_version(database.get_package_by_package_id(package["package_id"], db_path=temp_db), db_path=temp_db)
    return lock_version(built["version_id"], db_path=temp_db)


def test_research_window_handles_single_and_multiple_years_and_cutoff() -> None:
    one_month = normalize_window(selected_years=(2026,), selected_months=(3,), cutoff="2026-07-14")
    several = normalize_window(selected_years=(2026,), selected_months=(1, 3, 6), cutoff="2026-07-14")
    all_months = normalize_window(selected_years=(2025,), selected_months=None, cutoff="2026-07-14")
    multiple_years = normalize_window(selected_years=(2024, 2025), selected_months=(2,), cutoff="2026-07-14")

    assert one_month.contains("2026-03-31") and not one_month.contains("2026-04-01")
    assert several.contains("2026-06-30") and not several.contains("2026-05-01")
    assert all_months.months == ALL_MONTHS
    assert multiple_years.months == ALL_MONTHS and multiple_years.contains("2024-12-31")
    assert not several.contains("2026-07-15")
    with pytest.raises(ValueError, match="Select at least one month"):
        normalize_window(selected_years=(2026,), selected_months=(8, 9), cutoff="2026-07-14")


def test_sec_inventory_retains_outside_window_and_version_snapshots_window(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _package(temp_db, years=(2026,), months=(3,))

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "filings": {
                    "recent": {
                        "form": ["10-Q", "8-K"],
                        "filingDate": ["2026-03-15", "2026-04-01"],
                        "reportDate": ["2026-03-01", "2026-03-31"],
                        "primaryDocument": ["q.htm", "eightk.htm"],
                        "accessionNumber": ["0001234567-26-000001", "0001234567-26-000002"],
                        "items": ["", "2.02"],
                    }
                }
            }

    monkeypatch.setattr(sec_collector, "request_with_retries", lambda *args, **kwargs: Response())
    inventory = sec_collector.preview_cutler_profile(package, db_path=temp_db)

    assert len(inventory) == 2
    assert inventory[0].selected is True
    assert inventory[1].selected is False
    assert inventory[1].inventory_status == config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW
    assert document_window_status(package, "2026-04-01") == config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW

    version = database.allocate_package_version(
        {
            "parent_package_id": package["package_id"],
            "ticker": package["ticker"],
            "company_name": package["company_name"],
            "security_type": package["security_type"],
            "research_cutoff_date": package["research_cutoff_date"],
            "status": config.VERSION_STATUS_BUILDING,
            "created_by": "test",
            "created_at": database.utc_now_iso(),
            "notes": "window snapshot",
            "collection_profile_name": package["collection_profile_name"],
            "collection_profile_snapshot_json": package["collection_profile_snapshot_json"],
            "selected_years_json": package["selected_years_json"],
            "selected_months_json": package["selected_months_json"],
            "research_window_fingerprint": package["research_window_fingerprint"],
        },
        db_path=temp_db,
    )
    assert json.loads(version["selected_years_json"]) == [2026]
    assert json.loads(version["selected_months_json"]) == [3]
    assert version["research_window_fingerprint"] == package["research_window_fingerprint"]


def test_ir_materials_use_publication_date_and_retain_outside_window(temp_db: Path) -> None:
    package = _package(temp_db, years=(2026,), months=(3,))
    eligible = _material(
        package,
        "Q1 earnings release 2026-03-15",
        "https://ir.example.test/q1-results",
        "https://ir.example.test/",
        "STATIC_LINK",
        "text/html",
        "ir.example.test",
    )
    outside = _material(
        package,
        "Investor presentation 2026-04-01",
        "https://ir.example.test/april-presentation.pdf",
        "https://ir.example.test/",
        "STATIC_LINK",
        "application/pdf",
        "ir.example.test",
    )

    assert eligible.cutoff_eligibility == "ELIGIBLE"
    assert outside.cutoff_eligibility == config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW
    assert outside.download_status == config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW
    assert outside.selected is False


def test_report_fingerprint_rows_sort_mixed_metric_values_stably() -> None:
    rows = [("revenue", None, "USD"), ("revenue", 100.0, "USD"), (None, 20.0, None)]
    assert _stable_rows(rows) == _stable_rows(reversed(rows))
    assert float(config.REPORT_TEMPLATE_VERSION) >= 7.0


def test_processing_interrupt_resume_reuse_retry_and_progress(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = _locked_text_package(temp_db)
    original_prepare = processing_pipeline._prepare_document
    interrupted_once = False

    def interrupt_second(document: dict, **kwargs):
        nonlocal interrupted_once
        if document["document_id"].endswith("0002") and not interrupted_once:
            interrupted_once = True
            raise KeyboardInterrupt("test interruption")
        return original_prepare(document, **kwargs)

    monkeypatch.setattr(processing_pipeline, "_prepare_document", interrupt_second)
    with pytest.raises(KeyboardInterrupt, match="test interruption"):
        processing_pipeline.run_processing_pipeline(version["version_id"], max_workers=1, db_path=temp_db)

    runs = database.list_processing_runs(version_id=version["version_id"], db_path=temp_db)
    interrupted = runs[0]
    assert interrupted["status"] == config.PROCESSING_STATUS_INTERRUPTED
    before = database.list_processing_document_items(interrupted["processing_run_id"], db_path=temp_db)
    assert [item["status"] for item in before].count("COMPLETED") == 1

    monkeypatch.setattr(processing_pipeline, "_prepare_document", original_prepare)
    progress: list[dict] = []
    resumed = processing_pipeline.resume_processing_run(
        interrupted["processing_run_id"], max_workers=2, progress_callback=progress.append, db_path=temp_db
    )
    assert resumed["processing_run_id"] == interrupted["processing_run_id"]
    assert resumed["status"] == config.PROCESSING_STATUS_COMPLETED
    items = database.list_processing_document_items(resumed["processing_run_id"], db_path=temp_db)
    assert [item["attempt_count"] for item in items] == [1, 1, 1]
    assert progress[-1]["completed"] == progress[-1]["total"] == 3
    assert all(progress[index]["completed"] <= progress[index + 1]["completed"] for index in range(len(progress) - 1))

    reused = processing_pipeline.run_processing_pipeline(version["version_id"], db_path=temp_db)
    assert reused["processing_run_id"] == resumed["processing_run_id"]

    changed_id = items[1]["version_document_id"]
    with database.get_connection(temp_db) as connection:
        connection.execute(
            "UPDATE processing_document_items SET processing_fingerprint = 'changed' WHERE processing_run_id = ? AND version_document_id = ?",
            (resumed["processing_run_id"], changed_id),
        )
    processing_pipeline.resume_processing_run(resumed["processing_run_id"], max_workers=2, db_path=temp_db)
    changed = {item["version_document_id"]: item for item in database.list_processing_document_items(resumed["processing_run_id"], db_path=temp_db)}
    assert changed[changed_id]["attempt_count"] == 2
    assert all(item["attempt_count"] == 1 for doc_id, item in changed.items() if doc_id != changed_id)

    failed_id = items[2]["version_document_id"]
    with database.get_connection(temp_db) as connection:
        connection.execute(
            "UPDATE processing_document_items SET status = 'FAILED' WHERE processing_run_id = ? AND version_document_id = ?",
            (resumed["processing_run_id"], failed_id),
        )
    processing_pipeline.retry_failed_documents(resumed["processing_run_id"], max_workers=2, db_path=temp_db)
    retried = {item["version_document_id"]: item for item in database.list_processing_document_items(resumed["processing_run_id"], db_path=temp_db)}
    assert retried[failed_id]["status"] == "COMPLETED"
    assert retried[failed_id]["attempt_count"] == 2


def test_document_batch_write_is_transactional(temp_db: Path) -> None:
    version = _locked_text_package(temp_db, count=1)
    run = processing_pipeline.run_processing_pipeline(version["version_id"], max_workers=1, db_path=temp_db)
    run_id = run["processing_run_id"]
    result = database.list_document_processing_results(run_id, db_path=temp_db)[0]
    item = database.list_processing_document_items(run_id, db_path=temp_db)[0]
    pages = database.list_document_pages(run_id, item["version_document_id"], db_path=temp_db)
    chunks_before = database.list_document_chunks(run_id, version_id=version["version_id"], db_path=temp_db)

    with pytest.raises(sqlite3.IntegrityError):
        database.commit_processed_document(
            result=result,
            item=item,
            pages=[pages[0], pages[0]],
            sheets=[],
            chunks=[],
            evidence=[],
            verifications=[],
            db_path=temp_db,
        )

    assert len(database.list_document_processing_results(run_id, db_path=temp_db)) == 1
    assert len(database.list_document_pages(run_id, item["version_document_id"], db_path=temp_db)) == len(pages)
    assert len(database.list_document_chunks(run_id, version_id=version["version_id"], db_path=temp_db)) == len(chunks_before)


@pytest.mark.parametrize(
    ("text", "expected_value"),
    [
        ("Revenue filing date was 2026-07-14.", None),
        ("Revenue appears on page 123.", None),
        ("Revenue accession 0001234567-26-123456 was filed.", None),
        ("Revenue was $450 million in Q1 2026.", 450.0),
    ],
)
def test_deterministic_evidence_ignores_structural_numbers(text: str, expected_value: float | None) -> None:
    records = evidence_from_chunk(
        {
            "chunk_id": "CHK-1",
            "processing_run_id": "RUN-1",
            "version_id": "PV-1",
            "version_document_id": "PVD-1",
            "chunk_text": text,
            "chunk_hash": "hash",
            "extraction_method": "TEXT",
            "source_locator_json": json.dumps({"display_title": "Test filing"}),
            "page_number": 1,
            "sheet_name": None,
            "row_range": None,
            "section_heading": "Financial Results",
        }
    )
    assert records and records[0]["value"] == expected_value
