from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from app import config
from app.services.candidate_validation_service import (
    fetch_and_validate_candidate,
    validate_candidate_metadata,
    validate_candidate_response,
)
from app.services.default_recipe_service import BundledRecipeError, validate_bundled_recipe
from app.services.package_discovery_service import (
    CandidateRankingAgent,
    EarningsAnchorAgent,
    RecipePlannerAgent,
    SlotQueryPlannerAgent,
    SourceRouterAgent,
    approve_and_download_candidate,
    discovery_preview,
    get_discovery_run,
    latest_discovery_run,
    list_discovery_candidates,
    override_earnings_anchor,
    resume_discovery,
    run_discovery,
    select_curated_sec_filings,
)
from app.services.package_recipe_service import (
    create_package_from_active_recipe,
    get_active_recipe,
    list_recipe_slots,
    list_slot_instances,
)
from app.services.search import (
    BraveSearchClient,
    BraveSearchRequest,
    BraveSearchResponse,
    BraveSearchResult,
    DisabledSearchProvider,
    MockSearchProvider,
)
from app.utils import database


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict | None = None,
        content: bytes = b"",
        content_type: str = "application/json",
        url: str = "https://8.8.8.8/result",
        headers: dict | None = None,
        history: list | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.url = url
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.history = history or []

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if not response.url:
            response.url = url
        return response


@pytest.fixture()
def recipe_package(tmp_path: Path) -> tuple[Path, dict]:
    db_path = tmp_path / "phase6b.db"
    database.initialize_database(db_path)
    package = create_package_from_active_recipe(
        {"ticker": "MSFT", "company_name": "MICROSOFT CORP", "cik": "0000789019", "exchange": "Nasdaq", "resolution_status": "RESOLVED"},
        research_cutoff=date.today(), compilation_date=date.today(), compiled_by="Unit Analyst", created_by="Unit Analyst", db_path=db_path,
    )
    return db_path, package


def _slot(db_path: Path, package_id: str, slot_type: str) -> dict:
    return next(row for row in list_slot_instances(package_id, db_path=db_path) if row["normalized_slot_type"] == slot_type)


def _mock_result(*, more: bool = False) -> BraveSearchResponse:
    return BraveSearchResponse(
        query="",
        results=(BraveSearchResult(1, "MSFT Investor Presentation", "https://8.8.8.8/MSFT-investor-presentation.pdf", "Official MSFT investor presentation"),),
        more_results_available=more,
        result_count=1,
        response_status="SUCCESS",
        safe_rate_limit_metadata={"x-ratelimit-remaining": "9"},
    )


def test_empty_database_bootstraps_valid_recipe_once(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    recipe = get_active_recipe(db_path=db_path)
    assert recipe and recipe["recipe_name"] == "Cutler Common Equity"
    slots = list_recipe_slots(recipe["recipe_id"], db_path=db_path)
    assert len(slots) == 28
    assert 22 not in {row["order_number"] for row in slots}
    assert [(row["display_name"], row["suborder"]) for row in slots if row["suborder"]] == [
        ("Sell-Side Downgrade", 1), ("Latest Earnings Call Audio", 1),
    ]
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM package_recipes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM phase6a_audit_events WHERE event_type='SYSTEM_RECIPE_BOOTSTRAPPED'").fetchone()[0] == 1


def test_bundled_recipe_schema_and_checksum_block_tampering(tmp_path: Path) -> None:
    payload = validate_bundled_recipe()
    assert len(payload["slots"]) == 28 and len(payload["checksum"]) == 64
    payload["slots"][0]["display_name"] = "Tampered"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BundledRecipeError, match="checksum"):
        validate_bundled_recipe(path)


def test_deliberately_inactive_recipe_is_not_reactivated(tmp_path: Path) -> None:
    db_path = tmp_path / "inactive.db"
    database.initialize_database(db_path)
    recipe = get_active_recipe(db_path=db_path)
    with database.get_connection(db_path) as connection:
        connection.execute("UPDATE package_recipes SET status='SUPERSEDED' WHERE recipe_id=?", (recipe["recipe_id"],))
    database.initialize_database(db_path)
    assert get_active_recipe(db_path=db_path) is None
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM package_recipes").fetchone()[0] == 1


def test_msft_creates_recipe_backed_package(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    assert package["ticker"] == "MSFT"
    assert len(list_slot_instances(package["package_id"], db_path=db_path)) == 28
    home = (config.PROJECT_ROOT / "app" / "Home.py").read_text(encoding="utf-8")
    assert "Create Equity Research Package" in home and "pages/8_Package_Assembly.py" in home


def test_brave_request_uses_header_and_supported_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "unit-secret-value"
    response = FakeResponse(
        payload={"query": {"more_results_available": True}, "web": {"results": [{"title": "Result", "url": "https://example.org/report.pdf", "extra_snippets": ["extra"]}]}},
        headers={"x-ratelimit-remaining": "4", "Authorization": "not-safe", "x-request-id": "request-1"},
    )
    session = FakeSession([response])
    client = BraveSearchClient(key, session=session)
    request = BraveSearchRequest(
        query='site:example.org "exact phrase" filetype:pdf', count=99, offset=9,
        freshness="2026-01-01to2026-07-01", extra_snippets=True,
    )
    result = client.search(request)
    call = session.calls[0]
    assert call["headers"]["X-Subscription-Token"] == key
    assert key not in call["url"] and key not in json.dumps(call["params"])
    assert call["params"]["count"] == config.BRAVE_MAX_RESULTS_PER_QUERY
    assert call["params"]["offset"] == config.BRAVE_MAX_PAGES_PER_QUERY - 1
    assert call["params"]["freshness"] == request.freshness
    assert call["params"]["extra_snippets"] == "true"
    assert result.more_results_available and result.safe_rate_limit_metadata == {"x-ratelimit-remaining": "4"}


def test_canonical_brave_key_precedes_compatibility_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_PROJECT_DOTENV", {"BRAVE_SEARCH_API_KEY": "canonical", "SEARCH_API_KEY": "legacy"})
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "environment-canonical")
    monkeypatch.setenv("SEARCH_API_KEY", "environment-legacy")
    assert config.brave_search_api_key() == "canonical"


def test_brave_retries_are_bounded_and_fail_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BRAVE_REQUEST_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "BRAVE_REQUEST_BACKOFF_SECONDS", 0)
    session = FakeSession([FakeResponse(status_code=503), FakeResponse(status_code=503)])
    result = BraveSearchClient("secret", session=session).search(BraveSearchRequest("minimal query", count=1))
    assert len(session.calls) == 2
    assert result.response_status == "FAILED" and result.error_category == "PROVIDER_ERROR"
    assert "secret" not in result.safe_error_message


def test_disabled_and_mock_providers_do_not_make_live_requests() -> None:
    disabled = DisabledSearchProvider()
    assert not disabled.is_configured()
    assert disabled.search(BraveSearchRequest("query")).response_status == "DISABLED"
    mock = MockSearchProvider([_mock_result()])
    assert mock.search(BraveSearchRequest("site:example.org test")).result_count == 1
    assert len(mock.requests) == 1


def test_planner_skips_complete_and_manual_slots(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    preview = discovery_preview(package["package_id"], db_path=db_path)
    types = {row["normalized_slot_type"] for row in preview["slots_to_search"]}
    assert "latest_earnings_release" in types
    assert "bbg_des" not in types and "sell_side_reports" not in types
    assert preview["maximum_possible_brave_queries"] <= config.BRAVE_MAX_QUERIES_PER_PACKAGE
    assert preview["manual_only_slots_remaining"] == 18


def test_source_router_and_query_planner_are_slot_bounded(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    plans = RecipePlannerAgent().plan(package["package_id"], db_path=db_path)
    sec_plan = next(row for row in plans if row.normalized_slot_type == "most_recent_10_q_and_10_k")
    assert SourceRouterAgent().route(sec_plan, package, brave_configured=True).selected_route == "SEC"
    manual = next(row for row in plans if row.normalized_slot_type == "sell_side_reports")
    assert SourceRouterAgent().route(manual, package, brave_configured=True).selected_route == "MANUAL_UPLOAD"
    presentation = next(row for row in plans if row.normalized_slot_type == "investor_presentations")
    queries = SlotQueryPlannerAgent().plan_queries(presentation, package, None)
    assert len(queries) <= 3 and "investor presentation" in queries[0].query


def test_earnings_anchor_requires_authoritative_source_and_detects_agreement() -> None:
    agent = EarningsAnchorAgent()
    low = agent.determine([{"source_type": "BRAVE_SNIPPET", "reporting_period_end": "2026-03-31"}])
    assert low.validation_status == "NEEDS_ANALYST_REVIEW"
    high = agent.determine([
        {"source_type": "OFFICIAL_EARNINGS_RELEASE", "fiscal_year": 2026, "fiscal_quarter": "Q1", "reporting_period_end": "2026-03-31", "earnings_release_date": "2026-04-25"},
        {"source_type": "SEC_8K", "fiscal_year": 2026, "fiscal_quarter": "Q1", "reporting_period_end": "2026-03-31", "earnings_release_date": "2026-04-25", "filing_date": "2026-04-25"},
    ])
    assert high.confidence == "HIGH" and high.validation_status == "VALIDATED"


def test_earnings_anchor_prefers_latest_period_and_override_is_audited(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    anchor = EarningsAnchorAgent().determine([
        {"source_type": "OFFICIAL_EARNINGS_RELEASE", "reporting_period_end": "2025-12-31", "earnings_release_date": "2026-02-01"},
        {"source_type": "SEC", "reporting_period_end": "2026-03-31", "filing_date": "2026-04-20"},
    ])
    assert anchor.reporting_period_end == "2026-03-31"
    overridden = override_earnings_anchor(
        package["package_id"], {"fiscal_year": 2026, "fiscal_quarter": "Q1", "reporting_period_end": "2026-03-31", "earnings_release_date": "2026-04-25"},
        reason="Confirmed against issuer release.", actor="analyst", db_path=db_path,
    )
    assert overridden["analyst_override"] == 1
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM phase6a_audit_events WHERE event_type='EARNINGS_ANCHOR_OVERRIDDEN'").fetchone()[0] == 1


def test_curated_sec_selection_excludes_broad_history_and_amendments() -> None:
    rows = [
        {"form_type": "10-K", "filing_date": "2026-02-01", "report_period": "2025-12-31", "primary_document_url": "https://sec.gov/k"},
        {"form_type": "10-K/A", "filing_date": "2026-03-01", "report_period": "2025-12-31", "primary_document_url": "https://sec.gov/ka"},
        {"form_type": "10-Q", "filing_date": "2026-05-01", "report_period": "2026-03-31", "primary_document_url": "https://sec.gov/q"},
        {"form_type": "DEF 14A", "filing_date": "2026-04-01", "report_period": "", "primary_document_url": "https://sec.gov/proxy"},
        {"form_type": "8-K", "filing_date": "2026-05-02", "report_period": "2026-03-31", "filing_items": "2.02,9.01", "primary_document_url": "https://sec.gov/earnings"},
        {"form_type": "8-K", "filing_date": "2026-05-03", "report_period": "", "filing_items": "5.02", "primary_document_url": "https://sec.gov/unrelated"},
    ]
    selected = select_curated_sec_filings(rows, research_cutoff="2026-07-01")
    assert {row["form_type"] for row in selected["most_recent_10_q_and_10_k"]} == {"10-K", "10-Q"}
    assert selected["executive_compensation_information"][0]["form_type"] == "DEF 14A"
    assert selected["latest_earnings_release"][0]["primary_document_url"].endswith("earnings")
    assert all(row["form_type"] != "10-K/A" for values in selected.values() for row in values)


@pytest.mark.parametrize("title", [
    "National_New_Account_2026__3_.pdf", "Customer Account Application", "Vendor W-9 Form",
    "Careers Brochure", "Privacy Policy", "Product Catalogue",
])
def test_non_investor_material_is_rejected(title: str) -> None:
    result = validate_candidate_metadata(
        title=title, url=f"https://8.8.8.8/{title.replace(' ', '-')}.pdf", slot_type="investor_presentations",
        company_name="QXO INC", ticker="QXO", description="routine corporate form",
    )
    assert not result.eligible and result.status == "NON_INVESTOR_MATERIAL"


def test_file_validation_requires_real_pdf_and_blocks_ssrf() -> None:
    fake_pdf = FakeResponse(content=b"<html>Error</html>", content_type="application/pdf", url="https://8.8.8.8/report.pdf")
    assert validate_candidate_response(fake_pdf.url, fake_pdf).status == "MIME_MISMATCH"
    real_pdf = FakeResponse(content=b"%PDF-1.7\nvalid", content_type="application/pdf", url="https://8.8.8.8/report.pdf")
    valid = validate_candidate_response(real_pdf.url, real_pdf)
    assert valid.eligible and valid.sha256_hash == hashlib.sha256(real_pdf.content).hexdigest()
    blocked = fetch_and_validate_candidate("http://127.0.0.1/private")
    assert not blocked.eligible and blocked.reason_code == "UNSAFE_URL"


def test_candidate_ranking_components_are_explained() -> None:
    agent = CandidateRankingAgent()
    sec = agent.rank(
        slot_type="latest_earnings_release", title="MSFT Earnings Release", url="https://sec.gov/report.pdf",
        description="quarterly results", source_route="SEC", company_name="MICROSOFT CORP", ticker="MSFT",
        publication_date="2026-04-25", anchor_date="2026-04-25", mime_type="application/pdf", validation_status="CONTENT_VALID",
    )
    marketing = agent.rank(
        slot_type="latest_earnings_release", title="Corporate brochure", url="https://go.example.com/brochure.pdf",
        description="general material", source_route="BRAVE_OFFICIAL", company_name="MICROSOFT CORP", ticker="MSFT",
        publication_date="2024-01-01", anchor_date="2026-04-25",
    )
    assert sec.overall_score > marketing.overall_score
    assert sec.source_authority_score == 100 and sec.reasons


def test_discovery_uses_cache_deduplicates_candidates_and_paginates_only_when_allowed(
    recipe_package: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, package = recipe_package
    slot = _slot(db_path, package["package_id"], "investor_presentations")
    provider = MockSearchProvider([_mock_result(more=False)])
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])):
        first = run_discovery(package["package_id"], slot_instance_ids=[slot["package_slot_instance_id"]], actor="analyst", provider=provider, db_path=db_path)
        second = run_discovery(package["package_id"], slot_instance_ids=[slot["package_slot_instance_id"]], actor="analyst", provider=provider, db_path=db_path)
    assert first["queries_executed"] == 1 and second["cached_queries_reused"] == 1
    assert len(provider.requests) == 1
    assert len(list_discovery_candidates(package["package_id"], db_path=db_path)) == 1


def test_second_page_requires_provider_more_results_flag(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    slot = _slot(db_path, package["package_id"], "investor_presentations")
    provider = MockSearchProvider([_mock_result(more=True), BraveSearchResponse(query="", response_status="SUCCESS")])
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])):
        run_discovery(package["package_id"], slot_instance_ids=[slot["package_slot_instance_id"]], actor="analyst", provider=provider, db_path=db_path)
    assert [request.offset for request in provider.requests] == [0, 1]


def test_one_slot_failure_does_not_erase_other_completed_slots(recipe_package: tuple[Path, dict]) -> None:
    from app.services import package_discovery_service as discovery_service

    db_path, package = recipe_package
    presentation = _slot(db_path, package["package_id"], "investor_presentations")
    earnings = _slot(db_path, package["package_id"], "latest_earnings_release")
    provider = MockSearchProvider([_mock_result(), _mock_result()])
    original_upsert = discovery_service._upsert_candidate
    def fail_one_slot(**kwargs):
        if kwargs["plan"].normalized_slot_type == "investor_presentations":
            raise RuntimeError("synthetic slot failure")
        return original_upsert(**kwargs)
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])), patch(
        "app.services.package_discovery_service.preview_cutler_profile", return_value=[]
    ), patch("app.services.package_discovery_service._upsert_candidate", side_effect=fail_one_slot):
        run = run_discovery(
            package["package_id"], slot_instance_ids=[presentation["package_slot_instance_id"], earnings["package_slot_instance_id"]],
            actor="analyst", provider=provider, db_path=db_path,
        )
    assert run["status"] == "COMPLETED_WITH_WARNINGS" and run["slot_count_completed"] == 1
    with database.get_connection(db_path) as connection:
        statuses = {row[0] for row in connection.execute("SELECT status FROM slot_discovery_runs WHERE discovery_run_id=?", (run["discovery_run_id"],))}
    assert statuses == {"COMPLETED", "FAILED"}


def test_approve_candidate_validates_download_and_creates_assignment(
    recipe_package: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, package = recipe_package
    slot = _slot(db_path, package["package_id"], "investor_presentations")
    provider = MockSearchProvider([_mock_result()])
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])):
        run_discovery(package["package_id"], slot_instance_ids=[slot["package_slot_instance_id"]], actor="analyst", provider=provider, db_path=db_path)
    candidate = list_discovery_candidates(package["package_id"], db_path=db_path)[0]
    response = FakeResponse(content=b"%PDF-1.7\nvalidated", content_type="application/pdf", url=candidate["canonical_url"])
    session = FakeSession([response])
    monkeypatch.setattr(
        "app.services.package_discovery_service.safe_document_path",
        lambda package_id, source, filename: tmp_path / sanitize_test_filename(filename),
    )
    monkeypatch.setattr(
        "app.services.package_discovery_service.atomic_write_bytes",
        lambda path, content: path.write_bytes(content),
    )
    result = approve_and_download_candidate(candidate["candidate_id"], actor="analyst", session=session, db_path=db_path)
    assert result["document"]["collection_status"] == "DOWNLOADED"
    assert result["assignment"]["assignment_status"] == "APPROVED"


def sanitize_test_filename(value: str) -> str:
    return "candidate" + Path(value).suffix


def test_resume_reuses_completed_slot_runs(recipe_package: tuple[Path, dict]) -> None:
    db_path, package = recipe_package
    slot = _slot(db_path, package["package_id"], "investor_presentations")
    provider = MockSearchProvider([_mock_result()])
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])):
        run = run_discovery(package["package_id"], slot_instance_ids=[slot["package_slot_instance_id"]], actor="analyst", provider=provider, db_path=db_path)
    with database.get_connection(db_path) as connection:
        connection.execute("UPDATE package_discovery_runs SET status='INTERRUPTED' WHERE discovery_run_id=?", (run["discovery_run_id"],))
        connection.execute("UPDATE slot_discovery_runs SET status='FAILED' WHERE discovery_run_id=?", (run["discovery_run_id"],))
    with patch("app.services.package_discovery_service.resolve_official_company_website", return_value=(None, [])):
        resumed = resume_discovery(package["package_id"], actor="analyst", provider=provider, db_path=db_path)
    assert resumed["discovery_run_id"] == run["discovery_run_id"]
    assert resumed["status"] in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}


def test_ui_contract_has_no_automatic_network_on_render() -> None:
    board = (config.PROJECT_ROOT / "app" / "pages" / "8_Package_Assembly.py").read_text(encoding="utf-8")
    advanced = (config.PROJECT_ROOT / "app" / "pages" / "1_New_Research_Package.py").read_text(encoding="utf-8")
    assert "Find All Missing Public Items" in board and 'st.button("Find Automatically"' in board
    assert "Approve And Download" in board and "Latest Completed Earnings Cycle" in board
    assert "Test Brave Search Connection" in advanced and "if st.button" in advanced
    assert "X-Subscription-Token" not in board + advanced
