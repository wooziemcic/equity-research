from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from docx import Document
from streamlit.testing.v1 import AppTest

from app import config
from app.services.analysis.financial_metrics import calculate_revenue_growth
from app.services.analysis.scenario_analysis import set_scenario_probabilities
from app.services.analysis_pipeline import create_analysis_run, validate_analysis_eligibility
from app.services.checklist_service import ensure_package_checklist
from app.services.package_builder import build_package_version, lock_version
from app.services.package_service import PackageInput, create_package
from app.services.processing_pipeline import run_processing_pipeline
from app.services.recommendation_engine import (
    complete_analyst_review,
    generate_recommendation,
    generate_scorecard,
    override_scorecard_item,
    pm_decision,
)
from app.services.reporting.investment_report import citation_audit, generate_investment_report
from app.services.upload_service import UploadCandidate, store_uploaded_files
from app.utils import database


@pytest.fixture(autouse=True)
def phase6_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    monkeypatch.setattr(config, "PACKAGE_DIR", tmp_path / "packages")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "MAX_UPLOAD_FILE_MB", 10)
    monkeypatch.setattr(config, "MAX_UPLOAD_BATCH_MB", 50)
    monkeypatch.setattr(config, "CHUNK_SIZE", 1000)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 100)


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "phase6.db"
    database.initialize_database(db_path)
    return db_path


def _package(temp_db: Path, ticker: str = "QXO", security_type: str = "Common Equity") -> dict:
    package = create_package(PackageInput(ticker, security_type, date(2026, 7, 13), 3, ""), db_path=temp_db)
    package = database.update_package_company_metadata(
        package["package_id"],
        {
            "ticker": ticker,
            "company_name": f"{ticker} Inc.",
            "cik": "0001234567",
            "exchange": "NYSE",
            "sic": "7370",
            "industry_description": "Services",
            "fiscal_year_end": "1231",
            "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0001234567",
            "resolution_status": "RESOLVED",
            "resolution_source": "test",
            "resolution_timestamp": "2026-07-13T00:00:00+00:00",
        },
        db_path=temp_db,
    )
    ensure_package_checklist(package, db_path=temp_db)
    return database.update_package_review_acknowledgement(
        package["package_id"],
        checklist_reviewed=True,
        reviewed_by="analyst",
        review_note="Reviewed.",
        missing_core_acknowledged=True,
        stale_documents_acknowledged=True,
        needs_review_acknowledged=True,
        db_path=temp_db,
    )


def _locked_processed_version(temp_db: Path, text: str = "Plain source document for controlled evidence citations.", ticker: str = "QXO") -> tuple[dict, dict, dict]:
    package = _package(temp_db, ticker=ticker)
    store_uploaded_files(
        package,
        [UploadCandidate("facts.txt", text.encode("utf-8"))],
        source_type="other",
        authorization_confirmed=True,
        metadata_by_name={"facts.txt": {"final_category_code": "other"}},
        db_path=temp_db,
    )
    version = build_package_version(database.get_package_by_package_id(package["package_id"], db_path=temp_db), db_path=temp_db)
    locked = lock_version(version["version_id"], db_path=temp_db)
    run = run_processing_pipeline(locked["version_id"], db_path=temp_db)
    return package, locked, run


def _add_evidence(
    temp_db: Path,
    run: dict,
    *,
    evidence_id: str,
    evidence_type: str,
    metric_name: str,
    value: float | None,
    unit: str | None = None,
    currency: str | None = "USD",
    period: str | None = "FY2026",
    claim: str | None = None,
    confidence: str = "High",
    verification_status: str = config.VERIFICATION_SUPPORTS,
) -> dict:
    chunk = database.list_document_chunks(run["processing_run_id"], version_id=run["version_id"], db_path=temp_db)[0]
    locator = json.loads(chunk["source_locator_json"])
    locator.update({"chunk_id": chunk["chunk_id"]})
    record = {
        "evidence_id": evidence_id,
        "processing_run_id": run["processing_run_id"],
        "version_id": run["version_id"],
        "version_document_id": chunk["version_document_id"],
        "evidence_type": evidence_type,
        "claim_text": claim or f"{metric_name} was {value} {unit or ''} {period or ''}.",
        "normalized_subject": "qxo",
        "metric_name": metric_name,
        "value": value,
        "unit": unit,
        "currency": currency,
        "period": period,
        "scenario": None,
        "direction": None,
        "source_text": chunk["chunk_text"],
        "page_number": chunk.get("page_number"),
        "sheet_name": chunk.get("sheet_name"),
        "cell_or_row_range": chunk.get("row_range"),
        "section_heading": chunk.get("section_heading"),
        "extraction_method": "TEST_EVIDENCE",
        "confidence": confidence,
        "verification_status": verification_status,
        "analyst_status": config.ANALYST_STATUS_ACCEPTED,
        "analyst_note": "",
        "source_locator_json": json.dumps(locator, sort_keys=True),
        "source_text_hash": None,
        "created_by": "test",
        "created_at": database.utc_now_iso(),
        "updated_at": database.utc_now_iso(),
    }
    database.create_evidence_record(record, db_path=temp_db)
    return record


def _seed_analysis_evidence(temp_db: Path, run: dict) -> None:
    for record in [
        ("EVD-REV25", "REPORTED_REVENUE", "revenue", 100.0, "million", "FY2025", "Revenue was $100 million in FY2025."),
        ("EVD-REV26", "REPORTED_REVENUE", "revenue", 120.0, "million", "FY2026", "Revenue was $120 million in FY2026."),
        ("EVD-FCF", "REPORTED_CASH_FLOW", "cash_flow", 24.0, "million", "FY2026", "Free cash flow was $24 million in FY2026."),
        ("EVD-DEBT", "REPORTED_DEBT", "debt", 30.0, "million", "FY2026", "Debt was $30 million in FY2026."),
        ("EVD-CASH", "REPORTED_LIQUIDITY", "liquidity", 10.0, "million", "FY2026", "Cash was $10 million in FY2026."),
        ("EVD-EBITDA", "OTHER_FACT", "ebitda", 60.0, "million", "FY2026", "EBITDA was $60 million in FY2026."),
        ("EVD-REF", "OTHER_FACT", "reference_price", 20.0, None, "FY2026", "Reference price was $20 on the package date."),
        ("EVD-PT", "PRICE_TARGET", "price_target", 28.0, None, "FY2026", "Analyst price target was $28."),
        ("EVD-DESC", "COMPANY_DESCRIPTION", "description", None, None, "FY2026", "The company operates a recurring revenue business."),
        ("EVD-CAT", "CATALYST", "catalyst", None, None, "FY2026", "A product launch is a catalyst."),
        ("EVD-RISK", "RISK", "risk", None, None, "FY2026", "Execution risk remains elevated."),
    ]:
        evidence_id, evidence_type, metric_name, value, unit, period, claim = record
        _add_evidence(
            temp_db,
            run,
            evidence_id=evidence_id,
            evidence_type=evidence_type,
            metric_name=metric_name,
            value=value,
            unit=unit,
            period=period,
            claim=claim,
        )


def test_phase6_schema_and_eligibility_blocks_bad_inputs(temp_db: Path) -> None:
    with database.get_connection(temp_db) as connection:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"analysis_runs", "analysis_metrics", "analysis_scorecard_items", "analysis_scenarios", "recommendation_decisions", "generated_reports"} <= tables
    package = _package(temp_db)
    store_uploaded_files(package, [UploadCandidate("plain.txt", b"Plain text without numbers.")], source_type="other", authorization_confirmed=True, metadata_by_name={"plain.txt": {"final_category_code": "other"}}, db_path=temp_db)
    built = build_package_version(package, db_path=temp_db)
    assert not validate_analysis_eligibility(built["version_id"], db_path=temp_db).is_eligible
    locked = lock_version(built["version_id"], db_path=temp_db)
    assert not validate_analysis_eligibility(locked["version_id"], db_path=temp_db).is_eligible
    no_evidence_run = run_processing_pipeline(locked["version_id"], db_path=temp_db)
    assert not validate_analysis_eligibility(locked["version_id"], no_evidence_run["processing_run_id"], db_path=temp_db).is_eligible
    _, other_version, other_run = _locked_processed_version(temp_db, "Revenue was $1 million.", ticker="ABC")
    wrong = validate_analysis_eligibility(locked["version_id"], other_run["processing_run_id"], db_path=temp_db)
    assert not wrong.is_eligible
    assert any("another version" in error for error in wrong.errors)
    database.update_package_version(other_version["version_id"], {"integrity_status": config.INTEGRITY_FAILED}, db_path=temp_db)
    assert not validate_analysis_eligibility(other_version["version_id"], other_run["processing_run_id"], db_path=temp_db).is_eligible


def test_analysis_run_calculations_scorecard_recommendation_and_scenarios(temp_db: Path) -> None:
    _, version, run = _locked_processed_version(temp_db)
    _seed_analysis_evidence(temp_db, run)
    analysis = create_analysis_run(version["version_id"], run["processing_run_id"], db_path=temp_db)
    assert analysis["status"] == config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW
    metrics = database.list_analysis_metrics(analysis["analysis_run_id"], db_path=temp_db)
    metric_by_code = {metric["metric_code"]: metric for metric in metrics}
    assert round(metric_by_code["REVENUE_GROWTH_CALCULATED"]["value"], 4) == 0.2
    assert round(metric_by_code["FCF_CONVERSION"]["value"], 4) == 0.2
    assert metric_by_code["NET_DEBT"]["value"] == 20000000.0
    assert metric_by_code["DEBT_TO_EBITDA"]["value"] == 0.5
    same_period = calculate_revenue_growth(
        {"evidence_id": "A", "value": 100, "unit": "million", "currency": "USD", "period": "FY2026", "verification_status": config.VERIFICATION_SUPPORTS},
        {"evidence_id": "B", "value": 90, "unit": "million", "currency": "USD", "period": "FY2026", "verification_status": config.VERIFICATION_SUPPORTS},
    )
    assert same_period.value is None
    assert "same period" in same_period.warning
    scorecard = database.list_scorecard_items(analysis["analysis_run_id"], db_path=temp_db)
    assert round(sum(item["weight"] for item in scorecard), 6) == 1
    missing_scorecard = generate_scorecard("RUN-MISSING", security_type="Common Equity", evidence=[], metrics=[], conflicts=[], db_path=temp_db)
    assert all(item["score"] == 0 for item in missing_scorecard if item["pillar_code"] != "EVIDENCE_QUALITY")
    decision = database.get_recommendation_decision(analysis["analysis_run_id"], db_path=temp_db)
    assert decision["preliminary_rating"] in {config.RECOMMENDATION_BUY, config.RECOMMENDATION_HOLD, config.RECOMMENDATION_SELL, config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED, config.RECOMMENDATION_INSUFFICIENT_EVIDENCE}
    assert decision["why_not_buy"]
    scenarios = database.list_analysis_scenarios(analysis["analysis_run_id"], db_path=temp_db)
    assert {scenario["scenario_name"] for scenario in scenarios} == {"Bull", "Base", "Bear"}
    assert all(json.loads(scenario["evidence_ids_json"]) for scenario in scenarios)
    updated = set_scenario_probabilities(analysis["analysis_run_id"], {"Bear": 0.25, "Base": 0.5, "Bull": 0.25}, rationale="Analyst scenario weights.", db_path=temp_db)
    assert round(sum(item["probability"] for item in updated), 6) == 1
    with pytest.raises(ValueError):
        set_scenario_probabilities(analysis["analysis_run_id"], {"Bear": 0.2, "Base": 0.5, "Bull": 0.2}, rationale="Bad total.", db_path=temp_db)


def test_recommendation_rule_outcomes(temp_db: Path) -> None:
    _, version, run = _locked_processed_version(temp_db)
    _seed_analysis_evidence(temp_db, run)
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=version["version_id"], db_path=temp_db)
    buy_score = [{"effective_score": 8.5, "weight": 1.0}]
    hold_score = [{"effective_score": 5.5, "weight": 1.0}]
    sell_score = [{"effective_score": 2.0, "weight": 1.0}]
    metrics = [
        {"metric_code": "REFERENCE_PRICE", "value": 20.0, "source_evidence_ids_json": '["EVD-REF"]'},
        {"metric_code": "PRICE_TARGET", "value": 28.0, "source_evidence_ids_json": '["EVD-PT"]'},
    ]
    assert generate_recommendation("RUN-BUY", evidence=evidence, metrics=metrics, scorecard_items=buy_score, conflicts=[], db_path=temp_db)["preliminary_rating"] == config.RECOMMENDATION_BUY
    assert generate_recommendation("RUN-HOLD", evidence=evidence, metrics=[metrics[0], {"metric_code": "PRICE_TARGET", "value": 21.0}], scorecard_items=hold_score, conflicts=[], db_path=temp_db)["preliminary_rating"] == config.RECOMMENDATION_HOLD
    assert generate_recommendation("RUN-SELL", evidence=evidence, metrics=[metrics[0], {"metric_code": "PRICE_TARGET", "value": 15.0}], scorecard_items=sell_score, conflicts=[], db_path=temp_db)["preliminary_rating"] == config.RECOMMENDATION_SELL
    assert generate_recommendation("RUN-INSUFF", evidence=evidence, metrics=[], scorecard_items=hold_score, conflicts=[], db_path=temp_db)["preliminary_rating"] == config.RECOMMENDATION_INSUFFICIENT_EVIDENCE
    unsupported = [dict(evidence[0], verification_status=config.VERIFICATION_DOES_NOT_SUPPORT)]
    assert generate_recommendation("RUN-REVIEW", evidence=unsupported, metrics=metrics, scorecard_items=buy_score, conflicts=[], db_path=temp_db)["preliminary_rating"] == config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED


def test_governance_and_report_generation(temp_db: Path) -> None:
    _, version, run = _locked_processed_version(temp_db)
    _seed_analysis_evidence(temp_db, run)
    analysis = create_analysis_run(version["version_id"], run["processing_run_id"], db_path=temp_db)
    item = database.list_scorecard_items(analysis["analysis_run_id"], db_path=temp_db)[0]
    with pytest.raises(ValueError):
        override_scorecard_item(item["item_id"], override_score=7, rationale="", db_path=temp_db)
    overridden = override_scorecard_item(item["item_id"], override_score=7, rationale="Analyst evidence interpretation.", db_path=temp_db)
    assert overridden["analyst_override_score"] == 7
    with pytest.raises(ValueError):
        pm_decision(analysis["analysis_run_id"], action="APPROVE", note="Too early.", db_path=temp_db)
    reviewed = complete_analyst_review(analysis["analysis_run_id"], decision=config.RECOMMENDATION_HOLD, note="Analyst reviewed.", db_path=temp_db)
    assert reviewed["status"] == config.ANALYSIS_STATUS_NEEDS_PM_APPROVAL
    draft = generate_investment_report(analysis["analysis_run_id"], final=False, db_path=temp_db)
    assert Path(draft["docx_path"]).exists()
    assert Path(draft["pdf_path"]).exists()
    assert draft["docx_sha256"] and draft["pdf_sha256"]
    doc = Document(draft["docx_path"])
    assert any("Closed-corpus" in paragraph.text for paragraph in doc.paragraphs)
    approved = pm_decision(analysis["analysis_run_id"], action="APPROVE", note="Approved for final report.", db_path=temp_db)
    assert approved["status"] == config.ANALYSIS_STATUS_PM_APPROVED
    final = generate_investment_report(analysis["analysis_run_id"], final=True, db_path=temp_db)
    assert final["report_status"] == config.REPORT_STATUS_FINAL
    assert final["report_version"] == draft["report_version"] + 1
    database.create_thesis_item(
        {
            "thesis_item_id": "THS-UNSUPPORTED",
            "analysis_run_id": analysis["analysis_run_id"],
            "item_type": "RISK",
            "claim": "Unsupported material claim.",
            "evidence_ids_json": '["EVD-REV26"]',
            "citation_status": config.VERIFICATION_DOES_NOT_SUPPORT,
            "confidence": config.CONFIDENCE_HIGH,
            "analyst_status": config.ANALYST_STATUS_UNREVIEWED,
            "source_type": "TEST",
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
        },
        db_path=temp_db,
    )
    assert citation_audit(analysis["analysis_run_id"], db_path=temp_db)["status"] == "FAILED"
    with pytest.raises(ValueError):
        generate_investment_report(analysis["analysis_run_id"], final=True, db_path=temp_db)


def test_profiles_and_streamlit_pages_load(temp_db: Path) -> None:
    for profile in config.SCORECARD_PROFILES.values():
        assert round(sum(weight for _, weight in profile.values()), 6) == 1
    for path in [
        "app/Home.py",
        "app/pages/1_New_Research_Package.py",
        "app/pages/2_Document_Collection.py",
        "app/pages/3_Package_Review.py",
        "app/pages/4_Investment_Analysis.py",
        "app/pages/5_Generated_Reports.py",
    ]:
        app = AppTest.from_file(path, default_timeout=10)
        app.run()
        assert not list(app.exception)
