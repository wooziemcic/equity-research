from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.official_ir_service import (
    OfficialIrCollectionResult,
    OfficialWebsiteCandidate,
    extract_q4_public_endpoints,
    _urls_from_verified_package_metadata,
    resolve_and_collect_official_ir_materials,
)
from app.services.checklist_service import ensure_package_checklist
from app.services.package_builder import build_package_version
from app.services.package_service import PackageInput, create_package
from app.services.reporting.memo_quality import (
    MemoDraftItem,
    MemoEvidenceCandidate,
    MemoGenerationError,
    _apply_duplicate_and_recency_rules,
    _candidate_rejections,
    _is_complete_sentence,
    _meaningful_heading,
    _validate_draft_items,
    audit_memo_quality,
    select_memo_candidates,
)
from app.services.research_workflow_service import planned_collection_preview, start_automated_collection
from app.utils import database


@pytest.fixture()
def phase5_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "phase5.db"
    database.initialize_database(db_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "PACKAGE_DIR", tmp_path / "packages")
    return db_path


@pytest.fixture()
def phase5_package(phase5_db: Path) -> dict:
    package = create_package(
        PackageInput("QXO", "Common Equity", date(2026, 7, 15), 3, ""),
        db_path=phase5_db,
    )
    return database.update_package_company_metadata(
        package["package_id"],
        {
            "ticker": "QXO",
            "company_name": "QXO, Inc.",
            "cik": "0002054521",
            "resolution_status": "RESOLVED",
            "resolution_source": "test",
            "resolution_timestamp": database.utc_now_iso(),
        },
        db_path=phase5_db,
    )


def _store_ir_candidate(db_path: Path, package_id: str, run_id: str, candidate_id: str, **updates) -> None:
    row = {
        "candidate_id": candidate_id,
        "package_id": package_id,
        "discovery_run_id": run_id,
        "title": "Q1 2026 Earnings Release",
        "source_url": f"https://investors.qxo.com/{candidate_id}.pdf",
        "canonical_url": f"https://investors.qxo.com/{candidate_id}.pdf",
        "official_domain": "investors.qxo.com",
        "category": "Earnings Release",
        "publication_date": "2026-05-01",
        "document_date": "2026-05-01",
        "mime_type": "application/pdf",
        "file_extension": ".pdf",
        "discovery_page": "https://investors.qxo.com",
        "discovery_method": "html_link",
        "confidence": "HIGH",
        "cutoff_eligibility": "ELIGIBLE",
        "download_status": "DISCOVERED",
        "selected": 0,
        "rejection_reason": None,
        "created_at": database.utc_now_iso(),
    }
    row.update(updates)
    database.upsert_ir_material_candidate(row, db_path=db_path)


def test_official_ir_orchestration_needs_no_manual_url_and_preserves_inventory(
    phase5_package: dict, phase5_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "IRDISC-PHASE5"
    resolved = OfficialWebsiteCandidate(
        "https://qxo.com", "qxo.com", "SEC submissions metadata:website", "HIGH",
        ("Issuer verified.",), (), is_verified=True,
    )
    calls: dict[str, object] = {}

    def fake_resolve(package, **kwargs):
        calls["analyst_url"] = kwargs.get("analyst_url")
        return resolved, [resolved]

    def fake_discover(package, official_url, **kwargs):
        calls["official_url"] = official_url
        _store_ir_candidate(phase5_db, package["package_id"], run_id, "SELECTED")
        _store_ir_candidate(phase5_db, package["package_id"], run_id, "UNSELECTED", category="ESG / Sustainability")
        _store_ir_candidate(
            phase5_db, package["package_id"], run_id, "OUTSIDE",
            cutoff_eligibility=config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW,
            download_status=config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW,
        )
        _store_ir_candidate(
            phase5_db, package["package_id"], run_id, "UNKNOWN",
            publication_date=None, document_date=None,
            cutoff_eligibility="DATE_REVIEW_REQUIRED", download_status="DATE_REVIEW_REQUIRED",
        )
        return {
            "run_id": run_id, "status": "COMPLETED", "pages_crawled": 3,
            "warnings": [], "errors": [], "ir_url": "https://investors.qxo.com",
        }

    def fake_download(package, materials, **kwargs):
        calls["downloaded"] = [row["candidate_id"] for row in materials]
        return {"downloaded_now": 1, "already_collected": 0, "duplicate": 0, "failed": 0, "excluded": 0}

    monkeypatch.setattr("app.services.official_ir_service.resolve_official_company_website", fake_resolve)
    monkeypatch.setattr("app.services.official_ir_service.discover_official_ir_materials", fake_discover)
    monkeypatch.setattr("app.services.official_ir_service.download_official_ir_materials", fake_download)
    monkeypatch.setattr(database, "update_ir_discovery_run", lambda *args, **kwargs: None)

    result = resolve_and_collect_official_ir_materials(
        phase5_package,
        selected_workspace_categories=["Earnings releases"],
        analyst_ir_url=None,
        db_path=phase5_db,
    )

    assert calls == {
        "analyst_url": None,
        "official_url": "https://qxo.com",
        "downloaded": ["SELECTED"],
    }
    assert result.official_website_url == "https://qxo.com"
    assert result.official_ir_url == "https://investors.qxo.com"
    assert (result.downloaded_now, result.not_selected, result.outside_selected_window, result.date_review_required) == (1, 1, 1, 1)
    statuses = {
        row["candidate_id"]: row["download_status"]
        for row in database.list_ir_material_candidates(phase5_package["package_id"], db_path=phase5_db)
    }
    assert statuses == {
        "SELECTED": "DISCOVERED",
        "UNSELECTED": "NOT_SELECTED",
        "OUTSIDE": config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW,
        "UNKNOWN": "DATE_REVIEW_REQUIRED",
    }


def test_main_collection_runs_ir_without_override_and_ir_not_found_is_nonfatal(
    phase5_package: dict, phase5_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.research_workflow_service.preview_filings", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.services.research_workflow_service.ensure_package_checklist", lambda *args, **kwargs: [])
    captured = {}

    def fake_ir(package, **kwargs):
        captured.update(kwargs)
        return OfficialIrCollectionResult(None, None, "NOT_FOUND", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr("app.services.research_workflow_service.resolve_and_collect_official_ir_materials", fake_ir)
    result = start_automated_collection(
        phase5_package,
        filing_types=["10-K"],
        ir_url=None,
        public_materials=["Earnings releases"],
        db_path=phase5_db,
    )
    assert captured["analyst_ir_url"] is None
    assert captured["selected_workspace_categories"] == ["Earnings releases"]
    assert not result.errors
    assert result.ir_summary["resolution_status"] == "NOT_FOUND"
    assert any("could not be verified" in warning for warning in result.warnings)
    assert "IR URL: Not available" not in Path("app/pages/0_Research_Workspace.py").read_text(encoding="utf-8")
    plan = planned_collection_preview(["Earnings releases"])
    assert any(row["source"] == "Earnings releases" and row["selected"] for row in plan)
    assert all(row["collection_method"] == "Investor-relations discovery" for row in plan[2:])


def test_q4_public_endpoints_are_allowlisted_and_private_or_unrelated_are_rejected() -> None:
    script = """
        window.config = {
          feed: 'https://www.q4api.com/feed/PressRelease.json',
          deck: 'https://investors.qxo.com/files/2026-presentation.pdf',
          private: 'https://www.q4api.com/private/admin.json?token=secret',
          unrelated: 'https://unrelated.example.net/news.json'
        };
    """
    endpoints = extract_q4_public_endpoints("https://investors.qxo.com", script, company_domain="qxo.com")
    assert "https://www.q4api.com/feed/PressRelease.json" in endpoints
    assert "https://investors.qxo.com/files/2026-presentation.pdf" in endpoints
    assert all("private" not in item and "token=" not in item and "unrelated" not in item for item in endpoints)


def test_fresh_draft_reuses_only_same_issuer_verified_package_metadata(
    phase5_package: dict, phase5_db: Path,
) -> None:
    peer = create_package(PackageInput("QXO", "Common Equity", date(2026, 7, 15), 3, ""), db_path=phase5_db)
    database.update_package_company_metadata(
        peer["package_id"],
        {"ticker": "QXO", "company_name": "QXO, Inc.", "cik": "0002054521"},
        db_path=phase5_db,
    )
    database.update_package_official_sites(
        peer["package_id"],
        {
            "official_website_url": "https://qxo.com",
            "official_website_domain": "qxo.com",
            "official_website_confidence": "HIGH",
            "official_website_source": "SEC filing",
            "official_website_checked_at": database.utc_now_iso(),
        },
        db_path=phase5_db,
    )
    assert _urls_from_verified_package_metadata(phase5_package, db_path=phase5_db) == [
        ("https://qxo.com", "Existing verified package metadata")
    ]
    database.update_package_company_metadata(
        peer["package_id"],
        {"ticker": "QXO", "company_name": "Different issuer", "cik": "0009999999"},
        db_path=phase5_db,
    )
    assert _urls_from_verified_package_metadata(phase5_package, db_path=phase5_db) == []


def test_official_ir_document_is_in_manifest_checklist_and_package_zip(
    phase5_package: dict, phase5_db: Path, tmp_path: Path,
) -> None:
    content = b"%PDF-1.4 official earnings presentation"
    source = config.DOWNLOAD_DIR / phase5_package["package_id"] / "investor_relations" / "QXO_Q1_2026_Earnings_Presentation.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    database.create_document_record(
        {
            "document_id": "DOC-IR-PHASE5",
            "package_id": phase5_package["package_id"],
            "ticker": "QXO",
            "category": "Earnings Presentation",
            "document_type": "PDF",
            "title": "Q1 2026 Earnings Presentation",
            "source_name": "Official Investor Relations",
            "source_url": "https://investors.qxo.com/q1-2026-presentation.pdf",
            "source_domain": "investors.qxo.com",
            "publication_date": "2026-05-01",
            "local_filename": source.name,
            "local_path": str(source),
            "mime_type": "application/pdf",
            "file_size_bytes": len(content),
            "sha256_hash": hashlib.sha256(content).hexdigest(),
            "collection_method": "INVESTOR_RELATIONS",
            "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
            "is_public": True,
            "final_category_code": "earnings_presentation",
            "selected_window_status": "ELIGIBLE",
        },
        db_path=phase5_db,
    )
    checklist = ensure_package_checklist(phase5_package, db_path=phase5_db)
    phase5_package = database.update_package_review_acknowledgement(
        phase5_package["package_id"],
        checklist_reviewed=True,
        reviewed_by="analyst",
        review_note="Phase 5 fixture reviewed.",
        missing_core_acknowledged=True,
        stale_documents_acknowledged=True,
        needs_review_acknowledged=True,
        db_path=phase5_db,
    )
    version = build_package_version(phase5_package, db_path=phase5_db)
    manifest = json.loads(Path(version["manifest_path"]).read_text(encoding="utf-8"))
    manifest_paths = [row["relative_path"] for row in manifest["documents"]]
    assert any(path.startswith("02_Investor_Relations/") for path in manifest_paths)
    assert any(row.get("category_code") == "earnings_presentation" and row.get("matched_document_count") == 1 for row in checklist)
    with zipfile.ZipFile(version["zip_path"]) as archive:
        names = archive.namelist()
    assert any(name.startswith("02_Investor_Relations/") for name in names)


def _memo_candidate(
    candidate_id: str,
    family: str,
    *,
    value: float | None = 100.0,
    period: str = "FY2026",
    date_value: str = "2026-05-01",
    kind: str = "SUPPORTING",
    claim: str | None = None,
) -> MemoEvidenceCandidate:
    text = claim or f"Revenue was USD {value:g} million in {period}."
    return MemoEvidenceCandidate(
        candidate_id=candidate_id,
        evidence_id=f"EVD-{candidate_id}",
        version_document_id="VDOC-1",
        claim_family=family,
        claim_text=text,
        supporting_quote=text,
        metric_name=family,
        numeric_value=value,
        unit="million" if value is not None else None,
        currency="USD" if value is not None else None,
        reporting_period=period,
        filing_or_publication_date=date_value,
        source_type="SEC",
        form_type="10-Q",
        section_heading="Results of Operations" if kind == "SUPPORTING" else "Risk Factors",
        page_number=12,
        source_priority=10.0,
        recency_score=float(date.fromisoformat(date_value).toordinal()),
        materiality_score=10.0,
        completeness_score=1.0,
        decision_relevance_score=10.0,
        eligible_for_memo=True,
        candidate_kind=kind,
        citation="[From: QXO 10-Q, filed May 1, 2026, Results of Operations]",
    )


def test_memo_selection_prefers_current_diverse_complete_facts_and_distinct_risks() -> None:
    latest_revenue = _memo_candidate("REV-NEW", "revenue_growth")
    stale_revenue = _memo_candidate("REV-OLD", "revenue_growth", value=80, period="FY2024", date_value="2025-02-01")
    profitability = _memo_candidate("PROFIT", "profitability", claim="Adjusted EBITDA was USD 30 million in FY2026.")
    cash_flow = _memo_candidate("CASH", "cash_flow", claim="Operating cash flow was USD 20 million in FY2026.")
    debt = _memo_candidate("DEBT", "debt_liquidity", claim="Total debt was USD 40 million in FY2026.")
    risk_one = _memo_candidate(
        "RISK-1", "acquisition_integration", value=None, kind="RISK",
        claim="Acquisition integration may increase execution risk.",
    )
    risk_two = _memo_candidate(
        "RISK-2", "leverage_refinancing", value=None, kind="RISK",
        claim="Refinancing needs could increase leverage and liquidity risk.",
    )
    candidates = [stale_revenue, latest_revenue, profitability, cash_flow, debt, risk_one, risk_two]
    _apply_duplicate_and_recency_rules(candidates)
    for item in candidates:
        item.eligible_for_memo = not item.rejection_reasons
    supporting, risks = select_memo_candidates(candidates)
    assert {item.candidate_id for item in supporting} == {"REV-NEW", "PROFIT", "CASH", "DEBT"}
    assert {item.claim_family for item in risks} == {"acquisition_integration", "leverage_refinancing"}
    assert "newer_equivalent_evidence_available" in stale_revenue.rejection_reasons
    assert all(_is_complete_sentence(item.claim_text) for item in [*supporting, *risks])


def test_sentence_and_citation_rules_reject_fragments_ellipsis_and_generic_headings() -> None:
    assert not _is_complete_sentence("Revenue increased and.")
    assert not _is_complete_sentence("Revenue increased...")
    assert not _is_complete_sentence("Secure IT system operations critical to the business.")
    assert _is_complete_sentence("Revenue increased during the quarter.")
    assert _meaningful_heading("UNITED STATES") is None
    assert _meaningful_heading("FORM 10-K") is None
    assert _meaningful_heading("Liquidity and Capital Resources") == "Liquidity and Capital Resources"


def test_industry_market_size_and_cover_page_values_are_not_issuer_facts() -> None:
    industry = _memo_candidate(
        "INDUSTRY", "revenue_growth", value=800.0,
        claim="The building products distribution industry had USD 800 billion of revenue across North America and Western Europe in 2024.",
    )
    reasons = _candidate_rejections(
        industry,
        {"verification_status": config.VERIFICATION_SUPPORTS},
        {"ticker": "QXO", "publication_date": "2026-02-27", "form_type": "10-K"},
        {"ticker": "QXO", "company_name": "QXO, Inc."},
    )
    assert "industry_market_size_not_issuer_performance" in reasons
    assert "immaterial_geography_tax_or_accounting_policy" in reasons

    cover = _memo_candidate(
        "COVER", "operating_driver", value=13.73,
        claim="The aggregate market value of the registrant held by non-affiliates was USD 13.73 billion in 2025.",
    )
    cover_reasons = _candidate_rejections(
        cover,
        {"verification_status": config.VERIFICATION_SUPPORTS},
        {"ticker": "QXO", "publication_date": "2026-02-27", "form_type": "10-K"},
        {"ticker": "QXO", "company_name": "QXO, Inc."},
    )
    assert "filing_boilerplate" in cover_reasons


@pytest.mark.parametrize(
    ("candidate_id", "claim"),
    [
        ("INVENTED", "Revenue was USD 100 million in FY2026."),
        ("VALID", "Revenue was USD 999 million in FY2026."),
    ],
)
def test_structured_memo_output_cannot_invent_candidate_ids_or_numbers(candidate_id: str, claim: str) -> None:
    valid = _memo_candidate("VALID", "revenue_growth")
    with pytest.raises(MemoGenerationError):
        _validate_draft_items(
            [MemoDraftItem(candidate_id=candidate_id, concise_claim=claim)],
            {valid.candidate_id: valid},
            "supporting",
        )


def test_structured_memo_output_cannot_add_unsupported_investment_interpretation() -> None:
    candidate = _memo_candidate(
        "VALID", "operating_driver",
        claim="The aggregate market value held by non-affiliates was USD 13.73 billion in 2025.",
        value=13.73,
    )
    with pytest.raises(MemoGenerationError, match="unsupported interpretation"):
        _validate_draft_items(
            [
                MemoDraftItem(
                    candidate_id="VALID",
                    concise_claim="The aggregate market value was USD 13.73 billion in 2025, demonstrating market confidence.",
                )
            ],
            {candidate.candidate_id: candidate},
            "supporting",
        )


def test_failed_memo_quality_audit_is_persisted_and_blocks_release(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, object] = {}
    monkeypatch.setattr(database, "create_memo_quality_audit", lambda record, **kwargs: saved.update(record) or record)
    monkeypatch.setattr(database, "update_memo_generation_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(database, "update_analysis_run", lambda *args, **kwargs: None)
    memo = {
        "investment_view": "Revenue improved...",
        "supporting_facts": [{"claim": "Revenue increased and.", "citation": "", "candidate_id": "VALID"}],
        "risks": [],
        "missing_information": ["Valuation evidence is missing."],
        "conclusion": "Analyst review is required.",
    }
    audit = audit_memo_quality(
        memo,
        [_memo_candidate("VALID", "revenue_growth")],
        attempt_id="MEMO-1",
        analysis_run_id="ANALYSIS-1",
        one_page_fit=True,
    )
    assert audit["status"] == "FAILED"
    assert audit["complete_sentence_check"] == "FAILED"
    assert audit["ellipsis_check"] == "FAILED"
    assert audit["citation_check"] == "FAILED"
    assert saved["status"] == "FAILED"
