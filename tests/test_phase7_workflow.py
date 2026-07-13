from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import config
from app.services.analysis_pipeline import AnalysisPipelineError
from app.services.checklist_service import coverage_summary
from app.services.combined_export_service import create_combined_export
from app.services.package_builder import ReadinessResult, build_package_version, lock_version, sha256_file
from app.services.package_service import PackageInput, create_package
from app.services.research_workflow_service import (
    TIMELINE_COMPLETED,
    TIMELINE_FAILED,
    get_or_create_research_package,
    normalize_ticker_input,
    resolve_search_ticker,
    run_research_workflow,
    update_research_settings,
    workflow_idempotency_key,
)
from app.services.upload_service import UploadCandidate, store_uploaded_files
from app.utils import database


class FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = ""
        self.content = b""
        self.headers = {}

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def phase7_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "phase7.db")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    monkeypatch.setattr(config, "PACKAGE_DIR", tmp_path / "packages")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler Capital tests@example.test")
    monkeypatch.setattr(config, "SEC_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(config, "MAX_UPLOAD_FILE_MB", 10)
    monkeypatch.setattr(config, "MAX_UPLOAD_BATCH_MB", 50)
    database.initialize_database(config.DATABASE_PATH)


@pytest.fixture()
def company_metadata() -> dict:
    return {
        "ticker": "QXO",
        "company_name": "QXO, Inc.",
        "cik": "0001234567",
        "exchange": "NYSE",
        "sic": "7370",
        "industry_description": "Services",
        "fiscal_year_end": "1231",
        "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0001234567",
        "resolution_status": "RESOLVED",
        "resolution_source": "test",
        "resolution_timestamp": "2026-07-13T00:00:00+00:00",
    }


def _ready_package(company_metadata: dict, *, ticker: str = "QXO") -> dict:
    package = create_package(PackageInput(ticker, "Common Equity", date(2026, 7, 13), 3, ""), db_path=config.DATABASE_PATH)
    package = database.update_package_company_metadata(
        package["package_id"],
        {**company_metadata, "ticker": ticker, "company_name": f"{ticker}, Inc."},
        db_path=config.DATABASE_PATH,
    )
    store_uploaded_files(
        package,
        [UploadCandidate("facts.txt", b"Revenue was $120 million in FY2026.")],
        source_type="other",
        authorization_confirmed=True,
        metadata_by_name={"facts.txt": {"final_category_code": "other", "title": "Facts"}},
        db_path=config.DATABASE_PATH,
    )
    return database.update_package_review_acknowledgement(
        package["package_id"],
        checklist_reviewed=True,
        reviewed_by="analyst",
        review_note="Phase 7 test acknowledgement.",
        missing_core_acknowledged=True,
        stale_documents_acknowledged=True,
        needs_review_acknowledged=True,
        db_path=config.DATABASE_PATH,
    )


def test_ticker_search_normalizes_resolves_and_handles_unresolved() -> None:
    assert normalize_ticker_input(" q x o ") == "QXO"
    session = FakeSession(
        [
            FakeResponse(json_data={"fields": ["cik", "name", "ticker", "exchange"], "data": [[1234567, "QXO, Inc.", "QXO", "NYSE"]]}),
            FakeResponse(json_data={"name": "QXO, Inc.", "sic": "7370", "sicDescription": "Services", "fiscalYearEnd": "1231"}),
        ]
    )
    resolved = resolve_search_ticker(" qxo ", session=session)
    assert resolved.status == "RESOLVED"
    assert resolved.metadata["ticker"] == "QXO"
    assert resolved.metadata["cik"] == "0001234567"
    unresolved = resolve_search_ticker("BAD/TICKER", session=FakeSession([]))
    assert unresolved.status == "UNRESOLVED"


def test_package_reuse_creation_and_workspace_settings(company_metadata: dict) -> None:
    created, was_created = get_or_create_research_package(company_metadata, db_path=config.DATABASE_PATH)
    assert was_created
    assert created["security_type"] == "Common Equity"
    reused, was_created = get_or_create_research_package(company_metadata, db_path=config.DATABASE_PATH)
    assert not was_created
    assert reused["package_id"] == created["package_id"]
    updated = update_research_settings(
        reused["package_id"],
        filing_history_years=5,
        research_cutoff_date=date(2026, 7, 13),
        db_path=config.DATABASE_PATH,
    )
    assert updated["filing_history_years"] == 5
    assert updated["research_cutoff_date"] == "2026-07-13"
    with pytest.raises(ValueError):
        update_research_settings(reused["package_id"], filing_history_years=4, research_cutoff_date=date(2026, 7, 13), db_path=config.DATABASE_PATH)


def test_collection_timeline_uses_real_backend_state(company_metadata: dict) -> None:
    package = _ready_package(company_metadata)
    database.create_collection_run(run_id="RUN-SEC-TIMELINE", package_id=package["package_id"], source_type="SEC", status=config.COLLECTION_STATUS_RUNNING, db_path=config.DATABASE_PATH)
    database.update_collection_run(
        "RUN-SEC-TIMELINE",
        status=config.COLLECTION_STATUS_COMPLETE,
        documents_discovered=1,
        documents_downloaded=1,
        db_path=config.DATABASE_PATH,
    )
    from app.services.research_workflow_service import collection_timeline

    rows = {row["stage"]: row for row in collection_timeline(package["package_id"], db_path=config.DATABASE_PATH)}
    assert rows["Company verified"]["status"] == TIMELINE_COMPLETED
    assert rows["SEC filing inventory loaded"]["status"] == TIMELINE_COMPLETED
    assert rows["Package readiness checked"]["status"] in {TIMELINE_COMPLETED, "Completed with warnings", TIMELINE_FAILED}


def test_checklist_coverage_counts_effective_status_case_insensitive() -> None:
    items = [
        {"requirement_level": "required", "effective_status": "available"},
        {"requirement_level": "Required", "effective_status": "AVAILABLE"},
        {"requirement_level": "REQUIRED", "effective_status": config.CHECKLIST_STATUS_AVAILABLE},
        {"requirement_level": "required", "effective_status": "missing"},
        {"requirement_level": "recommended", "effective_status": "missing"},
        {"requirement_level": "Recommended", "effective_status": "NEEDS_REVIEW"},
        {"requirement_level": "optional", "effective_status": "MISSING"},
        {"requirement_level": "required", "effective_status": "NOT_APPLICABLE"},
        {"requirement_level": "recommended", "effective_status": "NOT_AVAILABLE"},
    ]
    summary = coverage_summary(items)
    assert summary["available_required"] == 3
    assert summary["missing_required"] == 1
    assert summary["missing_recommended"] == 2
    assert summary["not_applicable"] == 1
    assert summary["not_available"] == 1


def test_workflow_orchestration_is_idempotent_and_records_ids(company_metadata: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _ready_package(company_metadata)
    calls = {"build": 0, "lock": 0, "process": 0, "analysis": 0, "report": 0}

    def fake_build(pkg: dict, *, notes: str = "", db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["build"] += 1
        return {"version_id": "QXO-20260713-V001", "status": config.VERSION_STATUS_BUILT, "integrity_status": config.INTEGRITY_VERIFIED}

    def fake_lock(version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["lock"] += 1
        return {"version_id": version_id, "status": config.VERSION_STATUS_LOCKED, "integrity_status": config.INTEGRITY_VERIFIED}

    def fake_processing(version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["process"] += 1
        return {"processing_run_id": "RUN-PROC-P7", "status": config.PROCESSING_STATUS_COMPLETED}

    def fake_analysis(version_id: str, processing_run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["analysis"] += 1
        return {"analysis_run_id": "RUN-AN-P7", "status": config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW}

    def fake_report(analysis_run_id: str, *, final: bool = False, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["report"] += 1
        return {"report_id": "RPT-P7"}

    monkeypatch.setattr("app.services.research_workflow_service.validate_package_readiness", lambda pkg, db_path=config.DATABASE_PATH: ReadinessResult(config.READINESS_READY, [], [], ["ready"]))
    monkeypatch.setattr("app.services.research_workflow_service.build_package_version", fake_build)
    monkeypatch.setattr("app.services.research_workflow_service.lock_version", fake_lock)
    monkeypatch.setattr("app.services.research_workflow_service.run_processing_pipeline", fake_processing)
    monkeypatch.setattr("app.services.research_workflow_service.create_analysis_run", fake_analysis)
    monkeypatch.setattr("app.services.research_workflow_service.generate_investment_report", fake_report)

    key = workflow_idempotency_key(package, db_path=config.DATABASE_PATH)
    first = run_research_workflow(package["package_id"], idempotency_key=key, db_path=config.DATABASE_PATH)
    second = run_research_workflow(package["package_id"], idempotency_key=key, db_path=config.DATABASE_PATH)
    assert first["workflow_run_id"] == second["workflow_run_id"]
    assert second["status"] == config.WORKFLOW_STATUS_COMPLETED
    assert second["version_id"] == "QXO-20260713-V001"
    assert second["processing_run_id"] == "RUN-PROC-P7"
    assert second["analysis_run_id"] == "RUN-AN-P7"
    assert second["report_id"] == "RPT-P7"
    assert calls == {"build": 1, "lock": 1, "process": 1, "analysis": 1, "report": 1}


def test_workflow_failure_preserves_completed_outputs(company_metadata: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _ready_package(company_metadata, ticker="FLT")
    monkeypatch.setattr("app.services.research_workflow_service.validate_package_readiness", lambda pkg, db_path=config.DATABASE_PATH: ReadinessResult(config.READINESS_READY, [], [], ["ready"]))
    monkeypatch.setattr(
        "app.services.research_workflow_service.build_package_version",
        lambda pkg, *, notes="", db_path=config.DATABASE_PATH: {"version_id": "FLT-20260713-V001", "status": config.VERSION_STATUS_BUILT, "integrity_status": config.INTEGRITY_VERIFIED},
    )
    monkeypatch.setattr("app.services.research_workflow_service.lock_version", lambda version_id, *, db_path=config.DATABASE_PATH: (_ for _ in ()).throw(ValueError("lock failed")))
    failed = run_research_workflow(package["package_id"], idempotency_key="P7-FAIL", db_path=config.DATABASE_PATH)
    assert failed["status"] == config.WORKFLOW_STATUS_FAILED
    assert failed["version_id"] == "FLT-20260713-V001"
    assert "lock failed" in failed["error_message"]


def test_workflow_true_processing_technical_failure_remains_failed(company_metadata: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _ready_package(company_metadata, ticker="TPF")
    calls = {"analysis": 0}
    monkeypatch.setattr("app.services.research_workflow_service.validate_package_readiness", lambda pkg, db_path=config.DATABASE_PATH: ReadinessResult(config.READINESS_READY, [], [], ["ready"]))
    monkeypatch.setattr(
        "app.services.research_workflow_service.build_package_version",
        lambda pkg, *, notes="", db_path=config.DATABASE_PATH: {"version_id": "TPF-20260713-V001", "status": config.VERSION_STATUS_BUILT, "integrity_status": config.INTEGRITY_VERIFIED},
    )
    monkeypatch.setattr(
        "app.services.research_workflow_service.lock_version",
        lambda version_id, *, db_path=config.DATABASE_PATH: {"version_id": version_id, "status": config.VERSION_STATUS_LOCKED, "integrity_status": config.INTEGRITY_VERIFIED},
    )
    monkeypatch.setattr(
        "app.services.research_workflow_service.run_processing_pipeline",
        lambda version_id, *, db_path=config.DATABASE_PATH: {"processing_run_id": "RUN-PROC-TECH-FAIL", "status": config.PROCESSING_STATUS_FAILED},
    )

    def fake_analysis(*args, **kwargs) -> dict:
        calls["analysis"] += 1
        return {}

    monkeypatch.setattr("app.services.research_workflow_service.create_analysis_run", fake_analysis)
    failed = run_research_workflow(package["package_id"], idempotency_key="P7-TECH-FAIL", db_path=config.DATABASE_PATH)
    assert failed["status"] == config.WORKFLOW_STATUS_FAILED
    assert "technical status FAILED" in failed["error_message"]
    assert calls["analysis"] == 0


def test_retry_resumes_from_metric_stage_without_duplicate_package_or_processing_run(company_metadata: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _ready_package(company_metadata, ticker="RTY")
    calls = {"build": 0, "lock": 0, "process": 0, "analysis": 0, "report": 0}
    version_id = "RTY-20260713-V001"
    processing_run_id = "RUN-PROC-RETRY"

    def fake_build(pkg: dict, *, notes: str = "", db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["build"] += 1
        version = database.create_package_version(
            {
                "version_id": version_id,
                "parent_package_id": pkg["package_id"],
                "version_number": 1,
                "ticker": pkg["ticker"],
                "company_name": pkg.get("company_name"),
                "security_type": pkg["security_type"],
                "research_cutoff_date": pkg["research_cutoff_date"],
                "status": config.VERSION_STATUS_BUILT,
                "document_count": 1,
                "checklist_snapshot_json": "[]",
                "created_by": "test",
                "created_at": database.utc_now_iso(),
            },
            db_path=config.DATABASE_PATH,
        )
        return database.update_package_version(version["version_id"], {"integrity_status": config.INTEGRITY_VERIFIED}, db_path=config.DATABASE_PATH) or version

    def fake_lock(locked_version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["lock"] += 1
        return database.update_package_version(
            locked_version_id,
            {"status": config.VERSION_STATUS_LOCKED, "locked_at": database.utc_now_iso()},
            db_path=config.DATABASE_PATH,
        ) or {}

    def fake_processing(locked_version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["process"] += 1
        return database.create_processing_run(
            {
                "processing_run_id": processing_run_id,
                "version_id": locked_version_id,
                "package_id": package["package_id"],
                "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
                "parser_config_version": config.PARSER_CONFIG_VERSION,
                "embedding_config_json": "{}",
                "ocr_config_json": "{}",
                "retrieval_config_json": "{}",
                "started_at": database.utc_now_iso(),
                "completed_at": database.utc_now_iso(),
                "total_documents": 1,
                "successful_documents": 1,
                "partial_documents": 0,
                "failed_documents": 0,
                "pages_processed": 1,
                "tables_detected": 0,
                "sheets_processed": 0,
                "chunks_created": 1,
                "evidence_records_created": 1,
                "warnings_json": "[]",
                "errors_json": "[]",
                "created_by": "test",
                "status": config.PROCESSING_STATUS_COMPLETED,
            },
            db_path=config.DATABASE_PATH,
        )

    def fake_analysis(locked_version_id: str, run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["analysis"] += 1
        analysis_id = "RUN-AN-RETRY-FAIL" if calls["analysis"] == 1 else "RUN-AN-RETRY-OK"
        status = config.ANALYSIS_STATUS_FAILED if calls["analysis"] == 1 else config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW
        created = database.create_analysis_run(
            {
                "analysis_run_id": analysis_id,
                "package_id": package["package_id"],
                "version_id": locked_version_id,
                "processing_run_id": run_id,
                "analysis_configuration_version": config.ANALYSIS_CONFIGURATION_VERSION,
                "scorecard_version": config.SCORECARD_VERSION,
                "valuation_configuration_version": config.VALUATION_CONFIGURATION_VERSION,
                "created_by": "test",
                "created_at": database.utc_now_iso(),
                "updated_at": database.utc_now_iso(),
                "status": status,
                "preliminary_recommendation": None,
                "confidence": None,
                "evidence_coverage": None,
                "package_coverage": 0.0,
                "research_cutoff": package["research_cutoff_date"],
                "error_message": None,
            },
            db_path=config.DATABASE_PATH,
        )
        if calls["analysis"] == 1:
            raise AnalysisPipelineError(
                "metric failure",
                analysis_run_id=analysis_id,
                diagnostics={"analysis_run_id": analysis_id, "processing_run_id": run_id},
            )
        return created

    def fake_report(analysis_run_id: str, *, final: bool = False, db_path: Path | str = config.DATABASE_PATH) -> dict:
        calls["report"] += 1
        return {"report_id": "RPT-RETRY"}

    monkeypatch.setattr("app.services.research_workflow_service.validate_package_readiness", lambda pkg, db_path=config.DATABASE_PATH: ReadinessResult(config.READINESS_READY, [], [], ["ready"]))
    monkeypatch.setattr("app.services.research_workflow_service.build_package_version", fake_build)
    monkeypatch.setattr("app.services.research_workflow_service.lock_version", fake_lock)
    monkeypatch.setattr("app.services.research_workflow_service.run_processing_pipeline", fake_processing)
    monkeypatch.setattr("app.services.research_workflow_service.create_analysis_run", fake_analysis)
    monkeypatch.setattr("app.services.research_workflow_service.generate_investment_report", fake_report)

    first = run_research_workflow(package["package_id"], idempotency_key="P7-RETRY", db_path=config.DATABASE_PATH)
    assert first["status"] == config.WORKFLOW_STATUS_FAILED
    assert first["version_id"] == version_id
    assert first["processing_run_id"] == processing_run_id
    retried = run_research_workflow(package["package_id"], idempotency_key="P7-RETRY", retry_failed=True, db_path=config.DATABASE_PATH)
    assert retried["status"] == config.WORKFLOW_STATUS_COMPLETED
    assert retried["analysis_run_id"] == "RUN-AN-RETRY-OK"
    assert calls == {"build": 1, "lock": 1, "process": 1, "analysis": 2, "report": 1}
    assert len(database.list_package_versions(package["package_id"], db_path=config.DATABASE_PATH)) == 1
    assert len(database.list_processing_runs(version_id, db_path=config.DATABASE_PATH)) == 1


def test_combined_export_includes_locked_package_report_and_safety_filters(company_metadata: dict) -> None:
    package = _ready_package(company_metadata, ticker="ZIP")
    version = build_package_version(package, db_path=config.DATABASE_PATH)
    locked = lock_version(version["version_id"], db_path=config.DATABASE_PATH)
    processing = database.create_processing_run(
        {
            "processing_run_id": "RUN-PROC-ZIP",
            "version_id": locked["version_id"],
            "package_id": package["package_id"],
            "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "embedding_config_json": "{}",
            "ocr_config_json": "{}",
            "retrieval_config_json": "{}",
            "started_at": database.utc_now_iso(),
            "completed_at": database.utc_now_iso(),
            "total_documents": 1,
            "successful_documents": 1,
            "partial_documents": 0,
            "failed_documents": 0,
            "pages_processed": 0,
            "tables_detected": 0,
            "sheets_processed": 0,
            "chunks_created": 0,
            "evidence_records_created": 0,
            "warnings_json": "[]",
            "errors_json": "[]",
            "created_by": "test",
            "status": config.PROCESSING_STATUS_COMPLETED,
        },
        db_path=config.DATABASE_PATH,
    )
    analysis = database.create_analysis_run(
        {
            "analysis_run_id": "RUN-AN-ZIP",
            "package_id": package["package_id"],
            "version_id": locked["version_id"],
            "processing_run_id": processing["processing_run_id"],
            "analysis_configuration_version": config.ANALYSIS_CONFIGURATION_VERSION,
            "scorecard_version": config.SCORECARD_VERSION,
            "valuation_configuration_version": config.VALUATION_CONFIGURATION_VERSION,
            "created_by": "test",
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
            "status": config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW,
            "preliminary_recommendation": config.RECOMMENDATION_INSUFFICIENT_EVIDENCE,
            "confidence": config.CONFIDENCE_INSUFFICIENT,
            "evidence_coverage": 0.0,
            "package_coverage": 0.5,
            "research_cutoff": locked["research_cutoff_date"],
        },
        db_path=config.DATABASE_PATH,
    )
    report_dir = config.REPORT_DIR / locked["version_id"] / analysis["analysis_run_id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    docx_path = report_dir / "Investment_Report.docx"
    pdf_path = report_dir / "Investment_Report.pdf"
    docx_path.write_bytes(b"docx bytes")
    pdf_path.write_bytes(b"%PDF bytes")
    report = database.create_generated_report(
        {
            "report_id": "RPT-ZIP",
            "analysis_run_id": analysis["analysis_run_id"],
            "package_id": package["package_id"],
            "version_id": locked["version_id"],
            "processing_run_id": processing["processing_run_id"],
            "report_version": 1,
            "report_kind": "INVESTMENT_REPORT",
            "report_status": config.REPORT_STATUS_DRAFT,
            "recommendation": config.RECOMMENDATION_INSUFFICIENT_EVIDENCE,
            "confidence": config.CONFIDENCE_INSUFFICIENT,
            "docx_path": str(docx_path),
            "docx_sha256": sha256_file(docx_path),
            "pdf_path": str(pdf_path),
            "pdf_sha256": sha256_file(pdf_path),
            "template_version": config.REPORT_TEMPLATE_VERSION,
            "citation_audit_status": "PASSED",
            "warnings_json": "[]",
            "created_at": database.utc_now_iso(),
        },
        db_path=config.DATABASE_PATH,
    )
    export = create_combined_export(analysis["analysis_run_id"], report_id=report["report_id"], db_path=config.DATABASE_PATH)
    assert export["zip_sha256"]
    with zipfile.ZipFile(export["zip_path"]) as archive:
        names = archive.namelist()
    assert any(name.endswith("00_Package_Manifest/package_manifest.json") for name in names)
    assert any(name.endswith("12_Final_Analysis/Investment_Report.pdf") for name in names)
    assert any(name.endswith("12_Final_Analysis/Investment_Report.docx") for name in names)
    assert any(name.endswith("12_Final_Analysis/evidence_ledger.xlsx") for name in names)
    assert any(name.endswith("12_Final_Analysis/conflicts.csv") for name in names)
    assert all(not Path(name).is_absolute() for name in names)
    assert not any(name.endswith(".db") or ".env" in name or "database" in Path(name).parts for name in names)
    second = create_combined_export(analysis["analysis_run_id"], report_id=report["report_id"], db_path=config.DATABASE_PATH)
    assert second["export_version"] == export["export_version"] + 1
    assert second["zip_path"] != export["zip_path"]


def test_phase7_primary_pages_direct_load() -> None:
    for path in [
        "app/Home.py",
        "app/pages/0_Research_Workspace.py",
        "app/pages/6_Investment_Result.py",
        "app/pages/7_Research_History.py",
        "app/pages/1_New_Research_Package.py",
        "app/pages/2_Document_Collection.py",
        "app/pages/3_Package_Review.py",
        "app/pages/4_Investment_Analysis.py",
        "app/pages/5_Generated_Reports.py",
    ]:
        app = AppTest.from_file(path, default_timeout=12)
        app.run()
        assert not list(app.exception)


def test_phase7_streamlit_chrome_config_and_primary_pages_have_no_sidebar_dependency() -> None:
    config_text = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert 'toolbarMode = "minimal"' in config_text
    assert "showSidebarNavigation = false" in config_text
    for path in [
        Path("app/Home.py"),
        Path("app/pages/0_Research_Workspace.py"),
        Path("app/pages/6_Investment_Result.py"),
    ]:
        source = path.read_text(encoding="utf-8")
        assert "render_sidebar()" not in source
    assert "Advanced Workbench" in Path("app/Home.py").read_text(encoding="utf-8")


def test_phase7_home_search_field_and_secondary_links_render() -> None:
    source = Path("app/Home.py").read_text(encoding="utf-8")
    assert 'st.form("ticker_search_form"' in source
    assert "form_submit_button" in source
    app = AppTest.from_file("app/Home.py", default_timeout=12)
    app.run()
    assert not list(app.exception)
    assert len(app.text_input) == 1
    assert app.text_input[0].label == "Ticker"
    assert app.text_input[0].placeholder == "Enter ticker — e.g. QXO"
    assert any(button.label == "Search" for button in app.button)
    assert any(link.label == "Recent Research" for link in app.get("page_link"))
    assert any(link.label == "Advanced Workbench" for link in app.get("page_link"))


def test_phase7_missing_sec_configuration_is_compact_near_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEC_USER_AGENT", "")
    app = AppTest.from_file("app/Home.py", default_timeout=12)
    app.run()
    assert not list(app.exception)
    assert not any("SEC company verification is not configured." in item.value for item in app.markdown)
    app.text_input[0].input("QXO")
    app.button[0].click()
    app.run()
    assert not list(app.exception)
    assert any("SEC company verification is not configured." in item.value for item in app.markdown)


def test_phase7_search_button_valid_configuration_and_invalid_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler Capital tests@example.test")
    app = AppTest.from_file("app/Home.py", default_timeout=12)
    app.run()
    app.text_input[0].input("BAD/TICKER")
    app.button[0].click()
    app.run()
    assert not list(app.exception)
    assert any("Ticker could not be verified in the supported SEC company database." in item.value for item in app.error)


def test_config_loads_project_root_dotenv_from_non_repo_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    app_dir = project / "app"
    outside = tmp_path / "outside"
    app_dir.mkdir(parents=True)
    outside.mkdir()
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "config.py").write_text(Path("app/config.py").read_text(encoding="utf-8"), encoding="utf-8")
    (project / ".env").write_text(
        "SEC_USER_AGENT=Cutler Equity Research sec-contact@cutlercapital.test\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("SEC_USER_AGENT", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.config as config; print(config.sec_user_agent_is_configured())",
        ],
        cwd=outside,
        env={**env, "PYTHONPATH": str(project)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_sec_user_agent_validation_accepts_real_contact_and_rejects_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler Equity Research sec-contact@cutlercapital.test")
    assert config.sec_user_agent_is_configured()
    for value in [
        "",
        "research@example.com",
        "Cutler Research research@example.com",
        "Cutler Research research@your-domain.com",
        "Cutler Research placeholder",
        "Cutler Research no-email",
        "sec-contact@cutlercapital.test",
    ]:
        monkeypatch.setattr(config, "SEC_USER_AGENT", value)
        assert not config.sec_user_agent_is_configured()
