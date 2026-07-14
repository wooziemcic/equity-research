from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.conflict_audit_service import evidence_comparable
from app.services.collectors.sec_collector import _eight_k_selection
from app.services.official_ir_service import (
    OfficialIrMaterial,
    canonicalize_url,
    discover_official_ir_materials,
    download_official_ir_materials,
    extract_ir_entry_points,
    ir_domain_allowed,
    is_blocked_aggregator,
    parse_feed,
    parse_sitemap,
    resolve_official_company_website,
    validate_official_website_candidate,
)
from app.services.openai_evidence_service import OpenAIEvidenceBatch, run_openai_evidence_extraction
from app.services.openai_service import StructuredParseResult
from app.services.package_service import PackageInput, create_package
from app.services.processing_pipeline import ProcessingEligibility, run_processing_pipeline
from app.services.reporting.investment_report import _supporting_facts, memo_to_sections
from app.services.reporting.pdf_generator import build_pdf_report
from app.services.sec_audit_service import audit_sec_collection, reconcile_draft_with_current_profile
from app.utils import database


class FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", content: bytes | None = None, headers: dict | None = None, json_data: dict | None = None, url: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        self._json = json_data or {}
        self.url = url

    def json(self) -> dict:
        return self._json


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"Unexpected live-style request: {url}")
        response = self.responses.pop(0)
        if not response.url:
            response.url = url
        return response


@pytest.fixture()
def phase3_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "phase3.db"
    database.initialize_database(path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "IR_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(config, "SEC_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(config, "HTTP_MAX_RETRIES", 1)
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    return path


@pytest.fixture()
def qxo_package(phase3_db: Path) -> dict:
    package = create_package(PackageInput("QXO", "Common Equity", date(2026, 7, 14), 3, ""), db_path=phase3_db)
    return database.update_package_company_metadata(
        package["package_id"],
        {
            "ticker": "QXO", "company_name": "QXO, Inc.", "cik": "0002054521", "exchange": "NYSE",
            "sic": "5031", "industry_description": "Building products", "fiscal_year_end": "1231",
            "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0002054521",
            "resolution_status": "RESOLVED", "resolution_source": "test", "resolution_timestamp": database.utc_now_iso(),
        },
        db_path=phase3_db,
    )


def _sec_document(package: dict, index: int, form: str, *, db_path: Path, sha: str | None = None) -> dict:
    accession = f"0002054521-26-{index:06d}"
    record = database.create_document_record(
        {
            "document_id": database.generate_document_id("DOC-SEC"), "package_id": package["package_id"], "ticker": "QXO",
            "category": form, "document_type": "HTML", "title": f"QXO {form}", "source_name": "SEC EDGAR",
            "source_url": f"https://www.sec.gov/Archives/{accession}.htm", "source_domain": "sec.gov",
            "accession_number": accession, "form_type": form, "publication_date": "2026-02-27",
            "local_filename": f"{index}.htm", "local_path": f"C:/fixture/{index}.htm", "mime_type": "text/html",
            "file_size_bytes": 10, "sha256_hash": sha or f"{index:064x}", "collection_method": "SEC",
            "collection_status": config.DOCUMENT_STATUS_DOWNLOADED, "is_public": True,
        },
        db_path=db_path,
    )
    return record


def test_92_document_audit_is_unique_with_expected_family_breakdown(qxo_package: dict, phase3_db: Path) -> None:
    for index, form in enumerate(["10-K"] * 3 + ["10-Q"] * 9 + ["8-K"] * 80, start=1):
        _sec_document(qxo_package, index, form, db_path=phase3_db)
    audit = audit_sec_collection(qxo_package["package_id"], db_path=phase3_db)
    assert audit.total_sec_inventory == 92
    assert audit.unique_accession_numbers == 92
    assert audit.duplicate_accession_numbers == 0
    assert audit.duplicate_content_hashes == 0
    assert audit.family_breakdown == {"10-K": 3, "10-Q": 9, "8-K": 80, "S-3": 0, "S-4": 0, "DEF 14A": 0, "144": 0, "EXCLUDED_OR_UNKNOWN": 0}


def test_duplicate_accession_and_reconciliation_are_non_destructive(qxo_package: dict, phase3_db: Path) -> None:
    first = _sec_document(qxo_package, 1, "10-K", db_path=phase3_db)
    second = _sec_document(qxo_package, 2, "S-1", db_path=phase3_db)
    with database.get_connection(phase3_db) as connection:
        connection.execute("UPDATE documents SET accession_number = ? WHERE document_id = ?", (first["accession_number"], second["document_id"]))
    audit = audit_sec_collection(qxo_package["package_id"], db_path=phase3_db)
    assert audit.duplicate_accession_numbers == 1
    result = reconcile_draft_with_current_profile(qxo_package["package_id"], db_path=phase3_db)
    assert result == {"included": 1, "excluded": 1}
    assert len(database.list_documents_by_package(qxo_package["package_id"], db_path=phase3_db)) == 2
    inclusions = {row["document_id"]: row for row in database.list_draft_document_inclusions(qxo_package["package_id"], db_path=phase3_db)}
    assert inclusions[second["document_id"]]["included"] == 0


def test_conflict_comparability_rejects_period_unit_and_same_document() -> None:
    base = {"metric_name": "Revenue", "period": "2025", "unit": "USD millions", "currency": "USD", "source_text_hash": "a", "version_document_id": "DOC-A"}
    assert evidence_comparable(base, {**base, "period": "2024", "source_text_hash": "b", "version_document_id": "DOC-B"}) == (False, "DIFFERENT_PERIOD")
    assert evidence_comparable(base, {**base, "unit": "percent", "currency": None, "source_text_hash": "b", "version_document_id": "DOC-B"}) == (False, "INCOMPATIBLE_UNIT")
    assert evidence_comparable(base, {**base, "source_text_hash": "b"}) == (False, "SAME_SOURCE_DOCUMENT")


def test_eight_k_modes_are_explicit_and_material_list_is_never_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEC_8K_COLLECTION_MODE", "ALL_8K")
    assert _eight_k_selection("")[:2] == ("ELIGIBLE", True)
    monkeypatch.setattr(config, "SEC_8K_COLLECTION_MODE", "ANALYST_SELECTION")
    assert _eight_k_selection("1.01")[:2] == ("AWAITING_8K_SELECTION", False)
    monkeypatch.setattr(config, "SEC_8K_COLLECTION_MODE", "MATERIAL_8K_ONLY")
    monkeypatch.setattr(config, "SEC_8K_APPROVED_ITEMS", ())
    status, selected, reason = _eight_k_selection("1.01")
    assert (status, selected) == ("EXCLUDED_8K_MODE", False)
    assert "No investment-team-approved" in reason
    monkeypatch.setattr(config, "SEC_8K_APPROVED_ITEMS", ("1.01",))
    assert _eight_k_selection("Item 1.01")[:2] == ("ELIGIBLE", True)


def test_conflict_fingerprint_is_idempotent(phase3_db: Path) -> None:
    record = {
        "conflict_id": "CONFLICT-1", "processing_run_id": "RUN-1", "subject": "qxo", "metric": "Revenue", "period": "2025",
        "evidence_id_a": "A", "evidence_id_b": "B", "conflict_type": "VALUE_DIFFERENCE", "severity": "MEDIUM",
        "explanation": "Different values.", "analyst_status": "UNREVIEWED", "conflict_fingerprint": "stable-fingerprint",
        "comparability_status": "COMPARABLE", "created_at": database.utc_now_iso(),
    }
    first = database.create_claim_conflict(record, db_path=phase3_db)
    second = database.create_claim_conflict({**record, "conflict_id": "CONFLICT-2"}, db_path=phase3_db)
    assert first["conflict_id"] == second["conflict_id"]
    assert len(database.list_claim_conflicts("RUN-1", db_path=phase3_db)) == 1


def test_sec_metadata_resolves_official_site_and_rejects_cross_domain_redirect(qxo_package: dict, phase3_db: Path) -> None:
    session = FakeSession([
        FakeResponse(json_data={"website": "https://www.qxo.com"}),
        FakeResponse(text="<html>QXO, Inc. CIK 2054521</html>", url="https://www.qxo.com", headers={"Content-Type": "text/html"}),
    ])
    resolved, candidates = resolve_official_company_website(qxo_package, session=session, db_path=phase3_db)
    assert resolved and resolved.domain == "qxo.com"
    assert resolved.discovery_source.startswith("SEC submissions metadata")
    redirected = validate_official_website_candidate(
        qxo_package, "https://qxo.com", discovery_source="test",
        session=FakeSession([FakeResponse(text="QXO", url="https://unrelated-host.com")]),
    )
    assert not redirected.is_verified
    assert any("Redirect" in reason for reason in redirected.rejection_reasons)


def test_ir_entry_points_sitemap_feed_subdomain_and_blocked_aggregator() -> None:
    html = '<a href="https://investors.qxo.com">Investor Relations</a><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    points = extract_ir_entry_points("https://qxo.com", html)
    assert "https://investors.qxo.com" in points
    assert "https://qxo.com/feed.xml" in points
    assert parse_sitemap("https://qxo.com", "<urlset><url><loc>/files/annual-report.pdf</loc></url></urlset>") == ["https://qxo.com/files/annual-report.pdf"]
    assert parse_feed("https://qxo.com", "<rss><channel><item><title>Earnings Release</title><link>/q1.html</link></item></channel></rss>") == [("https://qxo.com/q1.html", "Earnings Release")]
    assert ir_domain_allowed("qxo.com", "investors.qxo.com")
    assert is_blocked_aggregator("finance.yahoo.com")
    assert not ir_domain_allowed("qxo.com", "finance.yahoo.com", directly_linked=True)


def test_static_discovery_finds_pdf_and_javascript_page_needs_review(qxo_package: dict, phase3_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "IR_MAX_PAGES", 1)
    homepage = '<a href="https://investors.qxo.com/files/2026-01-01-earnings-presentation.pdf">Earnings Presentation</a>'
    result = discover_official_ir_materials(
        qxo_package, "https://qxo.com",
        session=FakeSession([FakeResponse(text="User-agent: *\nAllow: /"), FakeResponse(text=homepage, headers={"Content-Type": "text/html"})]),
        db_path=phase3_db,
    )
    assert len(result["materials"]) == 1
    assert result["materials"][0].category == "Earnings Presentation"
    js_result = discover_official_ir_materials(
        qxo_package, "https://qxo.com",
        session=FakeSession([FakeResponse(text="User-agent: *\nAllow: /"), FakeResponse(text="<script>window.__DATA__={}</script>", headers={"Content-Type": "text/html"})]),
        db_path=phase3_db,
    )
    assert js_result["status"] == "NEEDS_MANUAL_REVIEW"
    assert js_result["warnings"][0].startswith("NEEDS_MANUAL_REVIEW")


def test_pdf_html_download_cutoff_url_and_hash_dedup(qxo_package: dict, phase3_db: Path) -> None:
    pdf = OfficialIrMaterial("Q1 Earnings Presentation", "https://investors.qxo.com/q1.pdf", "https://investors.qxo.com/q1.pdf", "investors.qxo.com", "Earnings Presentation", "2026-05-01", "2026-05-01", "application/pdf", ".pdf", "https://investors.qxo.com", "html_link", "HIGH", "ELIGIBLE", "DISCOVERED", True)
    session = FakeSession([FakeResponse(content=b"%PDF-1.4 fixture", headers={"Content-Type": "application/pdf"})])
    first = download_official_ir_materials(qxo_package, [pdf], session=session, db_path=phase3_db)
    assert first["downloaded_now"] == 1
    second = download_official_ir_materials(qxo_package, [pdf], session=FakeSession([]), db_path=phase3_db)
    assert second["already_collected"] == 1
    same_hash = replace(pdf, title="Duplicate", source_url="https://investors.qxo.com/duplicate.pdf", canonical_url="https://investors.qxo.com/duplicate.pdf")
    duplicate = download_official_ir_materials(qxo_package, [same_hash], session=FakeSession([FakeResponse(content=b"%PDF-1.4 fixture", headers={"Content-Type": "application/pdf"})]), db_path=phase3_db)
    assert duplicate["duplicate"] == 1
    html_material = replace(pdf, title="Earnings Release", source_url="https://investors.qxo.com/q1.html", canonical_url="https://investors.qxo.com/q1.html", category="Earnings Release", mime_type="text/html", file_extension=".html")
    html_result = download_official_ir_materials(qxo_package, [html_material], session=FakeSession([FakeResponse(content=b"<html>QXO earnings</html>", headers={"Content-Type": "text/html"})]), db_path=phase3_db)
    assert html_result["downloaded_now"] == 1
    excluded = replace(pdf, source_url="https://investors.qxo.com/future.pdf", canonical_url="https://investors.qxo.com/future.pdf", cutoff_eligibility="AFTER_CUTOFF", download_status="EXCLUDED_CUTOFF", selected=False)
    assert download_official_ir_materials(qxo_package, [excluded], session=FakeSession([]), db_path=phase3_db)["excluded"] == 1


def test_private_local_and_yahoo_candidates_never_validate(qxo_package: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostbyname", lambda host: "127.0.0.1" if host == "private.test" else "93.184.216.34")
    local = validate_official_website_candidate(qxo_package, "https://localhost/investors", discovery_source="test", session=FakeSession([]))
    private = validate_official_website_candidate(qxo_package, "https://private.test", discovery_source="test", session=FakeSession([]))
    yahoo = validate_official_website_candidate(qxo_package, "https://finance.yahoo.com/quote/QXO", discovery_source="search", session=FakeSession([]))
    assert not local.is_verified and not private.is_verified and not yahoo.is_verified
    assert canonicalize_url("https://QXO.com/a#fragment") == "https://qxo.com/a"


def test_unchanged_openai_chunks_are_reused_and_changed_chunk_is_incremental(qxo_package: dict, phase3_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = database.create_package_version(
        {
            "version_id": "PV-CACHE", "parent_package_id": qxo_package["package_id"], "version_number": 1,
            "ticker": "QXO", "company_name": "QXO, Inc.", "security_type": "Common Equity",
            "research_cutoff_date": "2026-07-14", "status": config.VERSION_STATUS_LOCKED,
        },
        db_path=phase3_db,
    )
    run = database.create_processing_run(
        {
            "processing_run_id": "RUN-CACHE", "version_id": version["version_id"], "package_id": qxo_package["package_id"],
            "pipeline_version": "test", "parser_config_version": "test", "started_at": database.utc_now_iso(),
            "total_documents": 1, "created_by": "test", "status": config.PROCESSING_STATUS_COMPLETED,
        },
        db_path=phase3_db,
    )
    for index in range(2):
        text = f"Revenue and net sales were USD {index + 1} million for 2025."
        database.create_document_chunk(
            {
                "chunk_id": f"CHUNK-{index}", "processing_run_id": run["processing_run_id"], "version_id": version["version_id"],
                "version_document_id": "VDOC-1", "chunk_index": index, "chunk_text": text,
                "character_count": len(text), "token_estimate": 12, "extraction_method": "TEST",
                "source_locator_json": json.dumps({"display_title": "QXO 10-K"}), "chunk_hash": f"hash-{index}",
                "created_at": database.utc_now_iso(),
            },
            db_path=phase3_db,
        )
    calls: list[list[str]] = []

    def fake_structured_parse(**kwargs):
        calls.append([row["chunk_id"] for row in kwargs["user_payload"]["chunks"]])
        return StructuredParseResult(parsed=OpenAIEvidenceBatch(items=[]), endpoint="responses.parse")

    monkeypatch.setattr("app.services.openai_evidence_service.structured_parse", fake_structured_parse)
    first = run_openai_evidence_extraction(version=version, processing_run_id=run["processing_run_id"], db_path=phase3_db)
    second = run_openai_evidence_extraction(version=version, processing_run_id=run["processing_run_id"], db_path=phase3_db)
    assert first.openai_batches == 1 and len(calls) == 1
    assert second.openai_batches == 0 and second.chunks_reused == 2
    with database.get_connection(phase3_db) as connection:
        connection.execute("UPDATE document_chunks SET chunk_hash = ? WHERE chunk_id = ?", ("hash-changed", "CHUNK-1"))
    incremental = run_openai_evidence_extraction(version=version, processing_run_id=run["processing_run_id"], db_path=phase3_db)
    assert incremental.openai_batches == 1
    assert incremental.chunks_reused == 1
    assert calls[-1] == ["CHUNK-1"]


def test_running_processing_record_is_not_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = {"version_id": "PV-INTERRUPTED", "parent_package_id": "PKG-1"}
    monkeypatch.setattr(
        "app.services.processing_pipeline.validate_processing_eligibility",
        lambda *args, **kwargs: ProcessingEligibility(True, version, [{"document_id": "VDOC-1"}], tmp_path, [], [], {}),
    )
    monkeypatch.setattr("app.services.processing_pipeline.processing_fingerprint", lambda *args, **kwargs: "same")
    monkeypatch.setattr(
        "app.services.processing_pipeline.database.list_processing_runs",
        lambda *args, **kwargs: [{"processing_run_id": "RUN-INTERRUPTED", "status": config.PROCESSING_STATUS_RUNNING, "processing_fingerprint": "same"}],
    )

    def request_new_run(*args, **kwargs):
        raise RuntimeError("new run requested")

    monkeypatch.setattr("app.services.processing_pipeline.database.create_processing_run", request_new_run)
    with pytest.raises(RuntimeError, match="new run requested"):
        run_processing_pipeline(version["version_id"], db_path=tmp_path / "unused.db")


def test_compact_memo_prefers_new_verified_evidence_and_targets_two_pages(tmp_path: Path) -> None:
    docs = {
        "VDOC": {"ticker": "QXO", "form_type": "10-Q", "publication_date": "2026-05-12", "title": "QXO 10-Q"}
    }
    base = {
        "verification_status": config.VERIFICATION_SUPPORTS, "value": 100.0, "metric_name": "Revenue",
        "unit": "USD millions", "currency": "USD", "source_text": "Revenue and net sales were USD 100 million.",
        "source_locator_json": json.dumps({"display_title": "QXO 10-Q"}), "version_document_id": "VDOC", "page_number": 12,
    }
    evidence = [
        {**base, "evidence_id": "EVD-OLD", "period": "2025-03-31", "source_text": "Revenue and net sales were USD 100 million in 2025.", "source_text_hash": "same"},
        {**base, "evidence_id": "EVD-NEW", "period": "2026-03-31", "value": 120.0, "source_text": "Revenue and net sales were USD 120 million in 2026.", "source_text_hash": "new"},
        {**base, "evidence_id": "EVD-DUP", "period": "2026-03-31", "value": 120.0, "source_text": "Revenue and net sales were USD 120 million in 2026.", "source_text_hash": "new"},
        {**base, "evidence_id": "EVD-PAGE", "metric_name": "Page", "period": "2026", "unit": None, "value": 12, "source_text_hash": "page"},
    ]
    facts = _supporting_facts(evidence, docs)
    assert facts[0]["claim"] == "Revenue and net sales were USD 120 million in 2026."
    assert len(facts) == 2
    assert all(row["citation"].startswith("[From: QXO 10-Q, filed May 12, 2026") for row in facts)
    memo = {
        "company_name": "QXO, Inc.", "ticker": "QXO", "recommendation": "Analyst Review Required", "confidence": "Medium",
        "research_cutoff": "July 14, 2026", "investment_view": "Operating evidence is positive, but valuation evidence is unavailable.",
        "supporting_evidence": facts, "catalysts": [],
        "risks": [{"claim": "Acquisition integration remains a material risk.", "citation": "[From: QXO 10-K, filed February 27, 2026]"}],
        "limitations": ["Closed-corpus limitation: valuation evidence was unavailable."],
        "conclusion": "Analyst review is required before assigning a final recommendation.",
    }
    sections = memo_to_sections(memo)
    rendered = " ".join(str(value) for section in sections for value in section.get("paragraphs", []))
    assert "EVD-" not in rendered and "PV-" not in rendered and "RUN-" not in rendered
    assert "sha256" not in rendered.lower() and "source inventory" not in rendered.lower()
    path = tmp_path / "memo.pdf"
    build_pdf_report(path, sections)
    from pypdf import PdfReader

    assert 1 <= len(PdfReader(str(path)).pages) <= 2
