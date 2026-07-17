from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.services.document_classifier import classify_document
from app.services.candidate_validation_service import validate_candidate_metadata
from app.services.earnings_cycle_service import fiscal_period_from_report_date, resolve_earnings_cycle
from app.services.package_assembly_service import package_contents, public_package_summary
from app.services.package_discovery_service import (
    RecipePlannerAgent,
    SlotQueryPlannerAgent,
    automatic_selection_reason,
    candidate_review_explanation,
    get_earnings_anchor,
    override_earnings_anchor,
)
from app.services.package_naming_service import classify_upload_filename, generate_package_display_filename
from app.services.package_recipe_service import (
    assign_document,
    create_package_from_active_recipe,
    list_slot_instances,
    recalculate_completion,
)
from app.services.preliminary_recommendation_service import preliminary_report_gate
from app.services.slot_policy_service import effective_document_counts
from app.utils import database


@pytest.fixture()
def assembly_package(tmp_path: Path) -> tuple[Path, dict]:
    db_path = tmp_path / "phase6b1.db"
    database.initialize_database(db_path)
    package = create_package_from_active_recipe(
        {
            "ticker": "MDT", "company_name": "MEDTRONIC PLC", "cik": "0001613103",
            "exchange": "NYSE", "fiscal_year_end": "0430", "resolution_status": "RESOLVED",
        },
        research_cutoff=date.today(), compilation_date=date.today(), compiled_by="Unit Analyst",
        created_by="Unit Analyst", db_path=db_path,
    )
    return db_path, package


def _slot(db_path: Path, package_id: str, slot_type: str) -> dict:
    return next(row for row in list_slot_instances(package_id, db_path=db_path) if row["normalized_slot_type"] == slot_type)


def _document(
    db_path: Path, package: dict, tmp_path: Path, filename: str, *, form: str = "", public: bool = True,
) -> dict:
    path = tmp_path / filename
    path.write_bytes(b"%PDF-1.7\nphase6b1")
    return database.create_document_record(
        {
            "document_id": database.generate_document_id("DOC-6B1"), "package_id": package["package_id"],
            "ticker": package["ticker"], "category": "SEC Filing" if form else "Research",
            "document_type": form or "Research", "title": path.stem, "source_name": "SEC" if form else "Company",
            "source_url": f"https://www.sec.gov/{filename}" if form else f"https://investor.example.test/{filename}",
            "source_domain": "www.sec.gov" if form else "investor.example.test", "form_type": form or None,
            "publication_date": "2026-06-03", "document_date": "2026-06-03", "local_filename": filename,
            "local_path": str(path), "mime_type": "application/pdf", "file_size_bytes": path.stat().st_size,
            "sha256_hash": hashlib.sha256(path.read_bytes() + filename.encode()).hexdigest(),
            "collection_method": "PUBLIC_DISCOVERY" if public else "UPLOAD",
            "collection_status": "DOWNLOADED", "is_public": public, "original_filename": filename,
        },
        db_path=db_path,
    )


def test_earnings_cycle_keeps_report_period_separate_from_filing_date() -> None:
    cycle = resolve_earnings_cycle([{
        "source_type": "SEC_10Q", "fiscal_year": 2026, "fiscal_quarter": "Q1",
        "reporting_period_end": "2025-07-25", "filing_date": "2025-09-03", "filing_form": "10-Q",
    }])
    assert cycle.reporting_period_end == "2025-07-25"
    assert cycle.filing_date == "2025-09-03"
    assert cycle.confidence == "MEDIUM" and cycle.validation_status == "VALIDATED"


def test_earnings_cycle_agreement_is_high_confidence_and_extracts_quarter() -> None:
    cycle = resolve_earnings_cycle([
        {"source_type": "SEC_10Q", "title": "Q1 FY26", "fiscal_year": 2026, "reporting_period_end": "2025-07-25"},
        {"source_type": "OFFICIAL_EARNINGS_RELEASE", "title": "First Quarter Fiscal 2026 Results", "fiscal_year": 2026, "reporting_period_end": "2025-07-25", "earnings_release_date": "2025-09-03"},
    ])
    assert cycle.fiscal_quarter == "Q1" and cycle.confidence == "HIGH"


def test_incomplete_anchor_requires_review_and_names_missing_fields() -> None:
    cycle = resolve_earnings_cycle([{"source_type": "SEC_8K", "filing_date": "2026-05-12"}])
    assert cycle.reporting_period_end is None
    assert cycle.validation_status == "NEEDS_ANALYST_REVIEW"
    assert "fiscal quarter" in cycle.evidence_summary and "reporting period end" in cycle.evidence_summary


def test_fiscal_period_inference_uses_report_date_not_filing_date() -> None:
    assert fiscal_period_from_report_date("2025-07-25", "0430", "10-Q") == (2026, "Q1")
    assert fiscal_period_from_report_date("2026-04-30", "0430", "10-K") == (2026, "Q4")


def test_analyst_anchor_confirmation_persists_and_is_audited(assembly_package: tuple[Path, dict]) -> None:
    db_path, package = assembly_package
    confirmed = override_earnings_anchor(
        package["package_id"],
        {"fiscal_year": 2026, "fiscal_quarter": "Q4", "fiscal_period_label": "Q4 FY26", "reporting_period_end": "2026-04-30"},
        reason="Confirmed from the issuer release.", actor="analyst", db_path=db_path,
    )
    assert confirmed["fiscal_quarter"] == "Q4" and confirmed["analyst_override"] == 1
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM phase6a_audit_events WHERE event_type='EARNINGS_ANCHOR_OVERRIDDEN'").fetchone()[0] == 1


def test_query_planner_creates_bounded_fallback_ladder(assembly_package: tuple[Path, dict]) -> None:
    db_path, package = assembly_package
    with database.get_connection(db_path) as connection:
        connection.execute("UPDATE packages SET official_ir_url='https://investor.medtronic.com', official_website_url='https://www.medtronic.com' WHERE package_id=?", (package["package_id"],))
    package = database.get_package_by_package_id(package["package_id"], db_path=db_path)
    plan = next(item for item in RecipePlannerAgent().plan(package["package_id"], db_path=db_path) if item.normalized_slot_type == "latest_earnings_release")
    queries = SlotQueryPlannerAgent().plan_queries(plan, package, None)
    assert [row.query_purpose[-2:] for row in queries] == ["L1", "L2", "L3"]
    assert "site:investor.medtronic.com" in queries[0].query
    assert "site:medtronic.com" in queries[1].query
    assert "site:" not in queries[2].query


def test_completed_search_does_not_complete_unfilled_slot(assembly_package: tuple[Path, dict]) -> None:
    db_path, package = assembly_package
    with database.get_connection(db_path) as connection:
        pri = connection.execute("SELECT package_recipe_instance_id FROM package_recipe_instances WHERE package_id=?", (package["package_id"],)).fetchone()[0]
        slot = _slot(db_path, package["package_id"], "latest_earnings_release")
        connection.execute("INSERT INTO package_discovery_runs(discovery_run_id, package_id, package_recipe_instance_id, search_profile_version, status, started_at, started_by, slot_count_requested, slot_count_completed) VALUES ('DISC-T', ?, ?, '1.0', 'COMPLETED', ?, 'test', 1, 1)", (package["package_id"], pri, database.utc_now_iso()))
        connection.execute("INSERT INTO slot_discovery_runs(slot_discovery_run_id, discovery_run_id, package_id, package_slot_instance_id, normalized_slot_type, source_route, status, started_at, completed_at) VALUES ('SDISC-T', 'DISC-T', ?, ?, 'latest_earnings_release', 'OFFICIAL_IR', 'COMPLETED', ?, ?)", (package["package_id"], slot["package_slot_instance_id"], database.utc_now_iso(), database.utc_now_iso()))
    summary = public_package_summary(package["package_id"], db_path=db_path)
    assert summary["discovery"]["completed"] == 1
    assert summary["public_package"]["filled"] == 0


def test_two_document_sec_slot_completes_only_after_both_filings(
    assembly_package: tuple[Path, dict], tmp_path: Path,
) -> None:
    db_path, package = assembly_package
    slot = _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")
    assert effective_document_counts(slot) == {"minimum": 2, "preferred": 2, "maximum": 2}
    ten_k = _document(db_path, package, tmp_path, "MDT-10K.pdf", form="10-K")
    ten_q = _document(db_path, package, tmp_path, "MDT-10Q.pdf", form="10-Q")
    assign_document(slot["package_slot_instance_id"], ten_k["document_id"], actor="test", db_path=db_path)
    assert _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")["completion_status"] == "PARTIAL"
    assign_document(slot["package_slot_instance_id"], ten_q["document_id"], actor="test", db_path=db_path)
    recalculate_completion(package["package_id"], db_path=db_path)
    assert _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")["completion_status"] == "COMPLETE"


@pytest.mark.parametrize(("filename", "slot_type"), [
    ("MDT GS 6.4.26.pdf", "sell_side_reports"),
    ("MDT JPM 6.3.26.pdf", "sell_side_reports"),
    ("MDT Jefferies 10.11.22 Initiation.pdf", "initiated_coverage_report"),
    ("MDT Moody's 10.11.24.pdf", "credit_reports"),
    ("MDT S&P Global 9.15.25.pdf", "credit_reports"),
    ("MDT BI 6.25.26 Credit.pdf", "credit_reports"),
    ("MDT Evercore 7.6.26 Industry.pdf", "industry_report"),
    ("MDT Morningstar 3.11.26.pdf", "morningstar_report_and_most_recent_model"),
    ("MDT Morningstar 6.29.26.xlsm", "morningstar_report_and_most_recent_model"),
    ("MDT DES 7.9.26.pdf", "bbg_des"),
    ("MDT DVD 7.9.26.pdf", "bbg_dvd"),
    ("MDT HDS 7.9.26.pdf", "bbg_hds"),
    ("MDT ANR 7.9.26.xlsm", "bbg_anr"),
    ("MDT DRSK 7.9.26.pdf", "drsk_default_risk"),
    ("MDT FA 7.9.26.pdf", "bbg_fa"),
    ("MDT Credit Ratios 7.9.26.pdf", "bbg_fa_credit_ratios"),
    ("MDT EV EBITDA Analysis 7.9.26.xlsx", "ccm_historical_multiples_valuation"),
])
def test_mdt_filename_classification(filename: str, slot_type: str) -> None:
    result = classify_upload_filename(filename)
    assert result.normalized_slot_type == slot_type and result.confidence == "HIGH"


def test_cutler_filename_sanitizes_disambiguates_and_never_invents_date() -> None:
    document = {"title": "Risk: Factors?", "original_filename": "source.pdf", "mime_type": "application/pdf"}
    first = generate_package_display_filename(ticker="MDT", slot_type="description_of_business_and_risk", document=document)
    second = generate_package_display_filename(ticker="MDT", slot_type="description_of_business_and_risk", document=document, existing_names=[first])
    assert first == "MDT Business and Risk Factors Undated.pdf"
    assert second.endswith(" 2.pdf") and not any(char in first for char in '<>:"/\\|?*')


def test_authoritative_auto_selection_rules_are_deterministic(assembly_package: tuple[Path, dict]) -> None:
    db_path, package = assembly_package
    plan = next(item for item in RecipePlannerAgent().plan(package["package_id"], db_path=db_path) if item.normalized_slot_type == "most_recent_10_q_and_10_k")
    candidate = {
        "candidate_status": "NEEDS_ANALYST_REVIEW", "canonical_url": "https://www.sec.gov/Archives/10q.htm",
        "source_route": "SEC", "publication_date": "2026-06-03",
        "metadata_json": '{"source_metadata":{"form_type":"10-Q","accession_number":"0001-26-000001"}}',
    }
    assert "Latest applicable original 10-Q" in automatic_selection_reason(candidate, package, plan)


def test_review_explanation_is_specific() -> None:
    text = candidate_review_explanation('["PUBLICATION_DATE_MISSING", "CONTENT_NOT_DOWNLOADED"]')
    assert "publication date" in text and "content" in text
    assert text != "Needs review"


def test_legal_legend_does_not_reject_real_investor_presentation() -> None:
    result = validate_candidate_metadata(
        title="QXO Investor Presentation",
        url="https://investors.qxo.com/events/investor-presentation.pdf",
        slot_type="investor_presentations", company_name="QXO, Inc.", ticker="QXO",
        official_domains={"investors.qxo.com"},
        description="This presentation is not a substitute for the registration statement.",
    )
    assert result.eligible


def test_qxo_new_account_form_remains_audit_only() -> None:
    result = validate_candidate_metadata(
        title="QXO National New Account Form",
        url="https://qxo.com/forms/national-new-account.pdf",
        slot_type="investor_presentations", company_name="QXO, Inc.", ticker="QXO",
        official_domains={"qxo.com"},
    )
    assert result.status == "NON_INVESTOR_MATERIAL"


def test_package_contents_contains_only_approved_assignments(
    assembly_package: tuple[Path, dict], tmp_path: Path,
) -> None:
    db_path, package = assembly_package
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    approved = _document(db_path, package, tmp_path, "MDT-Earnings.pdf")
    unassigned = _document(db_path, package, tmp_path, "MDT-Rejected.pdf")
    assign_document(slot["package_slot_instance_id"], approved["document_id"], actor="test", db_path=db_path)
    contents = package_contents(package["package_id"], db_path=db_path)
    assert [row["document_id"] for row in contents if row["analysis_eligible"]] == [approved["document_id"]]
    assert any(row["artifact_type"] == "CHECKLIST" for row in contents)
    assert unassigned["document_id"] not in {row["document_id"] for row in contents}
    assert contents[0]["display_filename"].startswith("MDT Earnings Release")


def test_preliminary_gate_uses_only_approved_package_documents(
    assembly_package: tuple[Path, dict], tmp_path: Path,
) -> None:
    db_path, package = assembly_package
    filing_slot = _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")
    earnings_slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    filing = _document(db_path, package, tmp_path, "MDT-10K-gate.pdf", form="10-K")
    earnings = _document(db_path, package, tmp_path, "MDT-Earnings-gate.pdf")
    assert preliminary_report_gate(package["package_id"], db_path=db_path)["status"] == "NOT_READY"
    assign_document(filing_slot["package_slot_instance_id"], filing["document_id"], actor="test", db_path=db_path)
    assign_document(earnings_slot["package_slot_instance_id"], earnings["document_id"], actor="test", db_path=db_path)
    gate = preliminary_report_gate(package["package_id"], db_path=db_path)
    assert gate["status"] == "PRELIMINARY_READY" and gate["package_incomplete"]


@pytest.mark.parametrize(("filename", "category"), [
    ("MDT Morningstar 6.29.26.xlsm", "morningstar_model"),
    ("MDT DES 7.9.26.pdf", "bloomberg_des"),
    ("MDT DVD 7.9.26.pdf", "bloomberg_dvd"),
    ("MDT HDS 7.9.26.pdf", "bloomberg_hds"),
    ("MDT Credit Ratios 7.9.26.pdf", "bloomberg_credit_ratios"),
])
def test_document_classifier_supports_cutler_samples(filename: str, category: str) -> None:
    assert classify_document(filename).category_code == category


def test_phase6b1_schema_is_additive_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "schema.db"
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT schema_value FROM schema_metadata WHERE schema_key='database_schema_version'").fetchone()[0] == "6B.2"
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(discovered_candidates)")}
        document_columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        assert {"review_reason_codes_json", "query_fallback_level", "automatic_selection_reason", "downloaded_document_id"} <= candidate_columns
        assert {"package_display_filename", "working_package_inclusion", "audit_package_inclusion"} <= document_columns
        assert connection.execute("SELECT COUNT(*) FROM preliminary_package_reports").fetchone()[0] == 0
