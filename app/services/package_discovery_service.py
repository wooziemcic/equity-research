from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from app import config
from app.services.candidate_validation_service import (
    CandidateValidation,
    fetch_and_validate_candidate,
    normalized_candidate_url,
    validate_candidate_metadata,
)
from app.services.collectors.sec_collector import FilingCandidate, preview_cutler_profile
from app.services.official_ir_service import discover_official_ir_materials, resolve_official_company_website
from app.services.package_recipe_service import (
    assign_document,
    get_package_recipe_instance,
    list_assignments,
    list_slot_instances,
    recalculate_completion,
)
from app.services.search import (
    BraveSearchRequest,
    BraveSearchResponse,
    SearchProvider,
    get_search_provider,
)
from app.services.workspace_service import atomic_write_bytes, safe_document_path, sanitize_filename
from app.utils import database


SEARCH_PROFILE_VERSION = "1.0"
MANUAL_ONLY_ROUTE = "MANUAL_UPLOAD"
SEC_SLOT_TYPES = {
    "liquidity_and_capital_resources",
    "description_of_business_and_risk",
    "executive_compensation_information",
    "most_recent_10_q_and_10_k",
}
OFFICIAL_IR_CATEGORIES = {
    "latest_earnings_release": {"Earnings Release"},
    "available_supplemental_or_earnings_presentation": {"Earnings Presentation", "Quarterly Supplement", "Financial Supplement"},
    "latest_earnings_call_transcript": {"Official Transcript"},
    "latest_earnings_call_audio": {"Earnings Audio", "Earnings Webcast"},
    "investor_presentations": {"Investor Presentation", "Investor Day", "Merger / Acquisition Presentation"},
    "material_company_press_releases_since_last_earnings_release": {"Company Press Release", "Official Company Material"},
}
REJECTED_CANDIDATE_STATUSES = {
    "REJECTED", "OUTSIDE_WINDOW", "NON_INVESTOR_MATERIAL", "MIME_MISMATCH",
    "COMPANY_MISMATCH", "SOURCE_NOT_AUTHORITATIVE", "UNSUPPORTED_FORMAT", "FAILED",
}


def _token(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8).upper()}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


@dataclass(frozen=True)
class SlotDiscoveryPlan:
    package_slot_instance_id: str
    normalized_slot_type: str
    display_name: str
    applicable: bool
    auto_searchable: bool
    already_complete: bool
    source_priority: tuple[str, ...]
    anchor_required: bool
    date_start: str | None
    date_end: str
    maximum_queries: int
    maximum_candidates: int
    allowed_types: tuple[str, ...]
    analyst_review_required: bool
    reason: str


@dataclass(frozen=True)
class SourceRouteDecision:
    selected_route: str
    alternative_routes: tuple[str, ...]
    reasons: tuple[str, ...]
    authoritative_source_available: bool
    brave_required: bool
    manual_upload_required: bool


@dataclass(frozen=True)
class EarningsAnchor:
    fiscal_year: int | None
    fiscal_quarter: str | None
    reporting_period_end: str | None
    earnings_release_date: str | None
    filing_date: str | None
    anchor_source: str
    confidence: str
    validation_status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateScore:
    company_identity_score: float
    source_authority_score: float
    slot_relevance_score: float
    freshness_score: float
    format_score: float
    overall_score: float
    reasons: tuple[str, ...]


def list_active_search_profiles(*, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM slot_search_profiles WHERE status='ACTIVE' AND enabled=1 ORDER BY normalized_slot_type"
        ).fetchall()
    profiles = []
    for row in rows:
        profile = dict(row)
        for field in (
            "authoritative_source_order_json", "query_templates_json", "positive_terms_json",
            "exclusion_terms_json", "allowed_domains_json", "allowed_file_types_json",
        ):
            profile[field.removesuffix("_json")] = _loads(profile[field], [])
        profiles.append(profile)
    return profiles


class RecipePlannerAgent:
    def plan(self, package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[SlotDiscoveryPlan]:
        package = database.get_package_by_package_id(package_id, db_path=db_path)
        if not package:
            raise ValueError("Research package does not exist.")
        profiles = {row["normalized_slot_type"]: row for row in list_active_search_profiles(db_path=db_path)}
        assignments = list_assignments(package_id, db_path=db_path)
        selected_counts: dict[str, int] = {}
        for assignment in assignments:
            if assignment["assignment_status"] == "APPROVED" and assignment.get("selected_for_package"):
                selected_counts[assignment["package_slot_instance_id"]] = selected_counts.get(assignment["package_slot_instance_id"], 0) + 1
        plans: list[SlotDiscoveryPlan] = []
        for slot in list_slot_instances(package_id, db_path=db_path):
            profile = profiles.get(slot["normalized_slot_type"])
            complete = slot["completion_status"] == "COMPLETE" or selected_counts.get(slot["package_slot_instance_id"], 0) >= slot["maximum_documents"]
            applicable = slot["applicability_status"] not in {"NOT_APPLICABLE"}
            acknowledged_unavailable = bool(slot["analyst_acknowledged"] and slot["completion_status"] == "NOT_AVAILABLE")
            searchable = bool(profile and applicable and not complete and not acknowledged_unavailable)
            if not applicable:
                reason = "Slot is not applicable."
            elif complete:
                reason = "Slot is already complete or at its document cap."
            elif acknowledged_unavailable:
                reason = "Analyst acknowledged that the material is unavailable."
            elif not profile:
                reason = "Licensed, internal, or manual-only item."
            else:
                reason = "Missing public item is eligible for bounded discovery."
            plans.append(
                SlotDiscoveryPlan(
                    package_slot_instance_id=slot["package_slot_instance_id"],
                    normalized_slot_type=slot["normalized_slot_type"],
                    display_name=slot["display_name_snapshot"],
                    applicable=applicable,
                    auto_searchable=searchable,
                    already_complete=complete,
                    source_priority=tuple(profile.get("authoritative_source_order", [])) if profile else (MANUAL_ONLY_ROUTE,),
                    anchor_required=bool(profile and profile.get("freshness_rule") == "LATEST_COMPLETED_CYCLE"),
                    date_start=None,
                    date_end=str(package["research_cutoff_date"])[:10],
                    maximum_queries=int(profile["maximum_queries"]) if searchable else 0,
                    maximum_candidates=int(profile["maximum_candidates"]) if profile else 0,
                    allowed_types=tuple(profile.get("allowed_file_types", [])) if profile else (),
                    analyst_review_required=bool(slot["analyst_review_required"]),
                    reason=reason,
                )
            )
        return plans


class SourceRouterAgent:
    def route(self, plan: SlotDiscoveryPlan, package: dict[str, Any], *, brave_configured: bool) -> SourceRouteDecision:
        if not plan.auto_searchable:
            return SourceRouteDecision(MANUAL_ONLY_ROUTE, (), (plan.reason,), False, False, True)
        priorities = plan.source_priority
        if plan.normalized_slot_type in SEC_SLOT_TYPES:
            selected = "SEC"
        elif plan.normalized_slot_type == "material_company_press_releases_since_last_earnings_release" and package.get("official_newsroom_url"):
            selected = "OFFICIAL_NEWSROOM"
        elif package.get("official_ir_url") or package.get("official_website_url"):
            selected = "OFFICIAL_IR"
        else:
            selected = "OFFICIAL_IR_RESOLUTION"
        brave_required = selected not in {"SEC"} and not (package.get("official_ir_url") or package.get("official_website_url")) and brave_configured
        return SourceRouteDecision(
            selected,
            tuple(route for route in priorities if route != selected),
            ("Selected the highest-priority authoritative route available for this slot.",),
            selected in {"SEC", "OFFICIAL_IR", "OFFICIAL_NEWSROOM"},
            brave_required,
            False,
        )


class SlotQueryPlannerAgent:
    _PHRASES = {
        "latest_earnings_release": '("quarterly results" OR "financial results" OR "earnings release")',
        "available_supplemental_or_earnings_presentation": '("earnings presentation" OR "results presentation" OR "quarterly presentation") filetype:pdf',
        "latest_earnings_call_transcript": '("earnings transcript" OR "prepared remarks" OR "earnings commentary")',
        "latest_earnings_call_audio": '("earnings webcast" OR "earnings call audio" OR "event replay")',
        "investor_presentations": '("investor presentation" OR "corporate presentation") filetype:pdf',
        "material_company_press_releases_since_last_earnings_release": "(acquisition OR launch OR approval OR guidance OR financing OR executive)",
    }

    def plan_queries(
        self,
        plan: SlotDiscoveryPlan,
        package: dict[str, Any],
        anchor: EarningsAnchor | None,
    ) -> list[BraveSearchRequest]:
        phrase = self._PHRASES.get(plan.normalized_slot_type)
        if not phrase or plan.maximum_queries <= 0:
            return []
        domain_url = package.get("official_ir_url") or package.get("official_newsroom_url") or package.get("official_website_url") or ""
        domain = (urlparse(str(domain_url)).hostname or "").removeprefix("www.")
        site = f"site:{domain} " if domain else ""
        company = str(package.get("company_name") or package["ticker"]).replace('"', "")
        identity = f'("{company}" OR "{package["ticker"]}")'
        query = f"{site}{identity} {phrase}".strip()
        freshness = None
        if anchor and anchor.earnings_release_date:
            freshness = f"{anchor.earnings_release_date}to{plan.date_end}"
        return [
            BraveSearchRequest(
                query=query,
                count=config.BRAVE_MAX_RESULTS_PER_QUERY,
                freshness=freshness,
                package_id=package["package_id"],
                slot_instance_id=plan.package_slot_instance_id,
                query_purpose=plan.normalized_slot_type,
            )
        ][: plan.maximum_queries]


class EarningsAnchorAgent:
    def determine(self, sources: Iterable[dict[str, Any]]) -> EarningsAnchor:
        authoritative = [row for row in sources if str(row.get("source_type") or "").upper() != "BRAVE_SNIPPET"]
        if not authoritative:
            return EarningsAnchor(None, None, None, None, None, "NONE", "LOW", "NEEDS_ANALYST_REVIEW", ("No authoritative earnings-cycle source is available.",))
        def key(row: dict[str, Any]) -> tuple[str, str]:
            return (str(row.get("reporting_period_end") or ""), str(row.get("earnings_release_date") or row.get("filing_date") or ""))
        latest_period = max(str(row.get("reporting_period_end") or "") for row in authoritative)
        current = [row for row in authoritative if str(row.get("reporting_period_end") or "") == latest_period] if latest_period else authoritative
        selected = max(current, key=key)
        periods = {str(row.get("reporting_period_end") or "") for row in current if row.get("reporting_period_end")}
        release_dates = {str(row.get("earnings_release_date") or "") for row in current if row.get("earnings_release_date")}
        types = {str(row.get("source_type") or "").upper() for row in current}
        conflict = len(periods) > 1 or len(release_dates) > 1
        agreement = bool(types & {"OFFICIAL_EARNINGS_RELEASE", "OFFICIAL_IR"}) and bool(types & {"SEC_8K", "SEC"})
        confidence = "HIGH" if agreement and not conflict else "MEDIUM" if not conflict else "LOW"
        status = "VALIDATED" if confidence in {"HIGH", "MEDIUM"} else "NEEDS_ANALYST_REVIEW"
        return EarningsAnchor(
            int(selected["fiscal_year"]) if selected.get("fiscal_year") else None,
            str(selected.get("fiscal_quarter") or "") or None,
            str(selected.get("reporting_period_end") or "") or None,
            str(selected.get("earnings_release_date") or "") or None,
            str(selected.get("filing_date") or "") or None,
            str(selected.get("source_type") or "AUTHORITATIVE_SOURCE"),
            confidence,
            status,
            ("Official earnings release and SEC filing agree." if agreement else "Latest authoritative reporting-period source selected.",),
        )


class CandidateValidationAgent:
    def validate(self, **kwargs: Any) -> CandidateValidation:
        return validate_candidate_metadata(**kwargs)


class CandidateRankingAgent:
    _SLOT_TERMS = {
        "latest_earnings_release": ("earnings", "financial results", "quarterly results"),
        "available_supplemental_or_earnings_presentation": ("earnings presentation", "results presentation", "supplement"),
        "latest_earnings_call_transcript": ("transcript", "prepared remarks", "earnings commentary"),
        "latest_earnings_call_audio": ("webcast", "audio", "event replay"),
        "investor_presentations": ("investor presentation", "corporate presentation", "investor day"),
        "material_company_press_releases_since_last_earnings_release": ("acquisition", "guidance", "financing", "approval", "executive"),
    }

    def rank(
        self,
        *,
        slot_type: str,
        title: str,
        url: str,
        description: str,
        source_route: str,
        company_name: str,
        ticker: str,
        publication_date: str | None,
        anchor_date: str | None,
        mime_type: str = "",
        validation_status: str = "METADATA_VALID",
    ) -> CandidateScore:
        domain = (urlparse(url).hostname or "").lower()
        source = 100.0 if source_route == "SEC" or domain.endswith("sec.gov") else 90.0 if source_route.startswith("OFFICIAL") else 65.0
        haystack = f"{title} {description} {url}".lower()
        terms = self._SLOT_TERMS.get(slot_type, tuple(slot_type.split("_")))
        relevance = 100.0 if any(term in haystack for term in terms) else 55.0
        identity = 100.0 if ticker.lower() in haystack or any(token.lower() in haystack for token in company_name.split() if len(token) > 2) else 60.0 if source >= 90 else 20.0
        freshness = 75.0
        if publication_date and anchor_date:
            freshness = 100.0 if publication_date >= anchor_date else 45.0
        format_score = 100.0 if mime_type == "application/pdf" and validation_status == "CONTENT_VALID" else 80.0 if mime_type.startswith("text/html") else 60.0
        overall = round(source * .30 + relevance * .25 + identity * .20 + freshness * .15 + format_score * .10, 2)
        return CandidateScore(identity, source, relevance, freshness, format_score, overall, ("Deterministic authority, relevance, identity, freshness, and format components were applied.",))


class CompletenessAgent:
    def guidance(self, package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> str:
        candidates = list_discovery_candidates(package_id, db_path=db_path)
        review = [row for row in candidates if row["candidate_status"] == "NEEDS_ANALYST_REVIEW"]
        if review:
            return f"Review the {review[0]['title']} candidate."
        plans = RecipePlannerAgent().plan(package_id, db_path=db_path)
        required_public = [plan for plan in plans if plan.auto_searchable and not plan.already_complete]
        if required_public:
            return f"Find the missing public item: {required_public[0].display_name}."
        manual = [plan for plan in plans if not plan.auto_searchable and not plan.already_complete and plan.applicable]
        if manual:
            return f"Upload the authorized material for {manual[0].display_name}."
        return "All required public items are complete."


def discovery_preview(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    plans = RecipePlannerAgent().plan(package_id, db_path=db_path)
    searchable = [plan for plan in plans if plan.auto_searchable]
    cached = 0
    with database.get_connection(db_path) as connection:
        cached = int(connection.execute("SELECT COUNT(*) FROM search_query_cache WHERE expires_at > ?", (database.utc_now_iso(),)).fetchone()[0])
    return {
        "slots_to_search": [asdict(plan) for plan in searchable],
        "slots_skipped": [asdict(plan) for plan in plans if not plan.auto_searchable],
        "maximum_possible_brave_queries": min(config.BRAVE_MAX_QUERIES_PER_PACKAGE, sum(plan.maximum_queries for plan in searchable)),
        "cached_queries_expected": cached,
        "manual_only_slots_remaining": sum(not plan.auto_searchable and not plan.already_complete and plan.applicable for plan in plans),
    }


def select_curated_sec_filings(filings: Iterable[FilingCandidate | dict[str, Any]], *, research_cutoff: str) -> dict[str, list[dict[str, Any]]]:
    rows = [dict(item.__dict__) if hasattr(item, "__dict__") else dict(item) for item in filings]
    rows = [row for row in rows if str(row.get("filing_date") or "") <= research_cutoff and not str(row.get("form_type") or "").upper().endswith("/A")]
    def latest(forms: set[str]) -> dict[str, Any] | None:
        eligible = [row for row in rows if str(row.get("form_type") or "").upper() in forms]
        return max(eligible, key=lambda row: (str(row.get("report_period") or ""), str(row.get("filing_date") or ""))) if eligible else None
    ten_k, ten_q, proxy = latest({"10-K"}), latest({"10-Q"}), latest({"DEF 14A"})
    earnings_8k = [row for row in rows if str(row.get("form_type") or "").upper() == "8-K" and "2.02" in str(row.get("filing_items") or "")]
    earnings = max(earnings_8k, key=lambda row: str(row.get("filing_date") or "")) if earnings_8k else None
    return {
        "most_recent_10_q_and_10_k": [row for row in (ten_q, ten_k) if row],
        "liquidity_and_capital_resources": [row for row in (ten_q or ten_k,) if row],
        "description_of_business_and_risk": [row for row in (ten_k,) if row],
        "executive_compensation_information": [row for row in (proxy,) if row],
        "latest_earnings_release": [row for row in (earnings,) if row],
    }


def _store_router_decision(run_id: str, package_id: str, plan: SlotDiscoveryPlan, route: SourceRouteDecision, *, db_path: Path | str) -> None:
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO source_router_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _token("ROUTE"), package_id, plan.package_slot_instance_id, run_id, route.selected_route,
                _json(route.alternative_routes), _json(route.reasons), int(route.authoritative_source_available),
                int(route.brave_required), int(route.manual_upload_required), database.utc_now_iso(),
            ),
        )


def _cache_key(request: BraveSearchRequest, provider_name: str) -> str:
    payload = {
        "provider": provider_name, "query": " ".join(request.query.lower().split()), "freshness": request.freshness,
        "country": request.country, "language": request.search_language, "count": request.count,
        "offset": request.offset, "profile": SEARCH_PROFILE_VERSION,
    }
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _execute_search(
    request: BraveSearchRequest,
    *,
    provider: SearchProvider,
    discovery_run_id: str,
    slot_run_id: str,
    db_path: Path | str,
) -> BraveSearchResponse:
    key = _cache_key(request, provider.provider_name)
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        cached = connection.execute("SELECT response_json FROM search_query_cache WHERE cache_key=? AND expires_at>?", (key, now)).fetchone()
    if cached:
        response = BraveSearchResponse.from_dict(json.loads(cached["response_json"]))
        response = BraveSearchResponse(**{**response.to_dict(), "results": response.results, "cache_status": "HIT"})
    else:
        response = provider.search(request)
        if response.response_status == "SUCCESS":
            expires = (datetime.now(UTC) + timedelta(hours=config.BRAVE_QUERY_CACHE_HOURS)).replace(microsecond=0).isoformat()
            with database.get_connection(db_path) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO search_query_cache VALUES (?, ?, ?, ?, ?, ?)",
                    (key, provider.provider_name, hashlib.sha256(request.query.encode()).hexdigest(), _json(response.to_dict()), now, expires),
                )
    estimated = None
    if response.response_status == "SUCCESS" and response.cache_status != "HIT" and config.BRAVE_COST_PER_1000_REQUESTS is not None:
        estimated = config.BRAVE_COST_PER_1000_REQUESTS / 1000
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO search_queries(
                search_query_id, discovery_run_id, slot_discovery_run_id, package_id, package_slot_instance_id,
                provider, query_text, query_hash, freshness_filter, country, language, result_limit,
                page_offset, status, executed_at, duration_ms, result_count, more_results_available,
                cache_hit, safe_rate_limit_json, estimated_cost, error_category, safe_error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _token("QUERY"), discovery_run_id, slot_run_id, request.package_id, request.slot_instance_id,
                provider.provider_name, request.query, hashlib.sha256(request.query.encode()).hexdigest(), request.freshness,
                request.country, request.search_language, request.count, request.offset, response.response_status, now,
                response.request_duration_ms, response.result_count, int(response.more_results_available),
                int(response.cache_status == "HIT"), _json(response.safe_rate_limit_metadata), estimated,
                response.error_category or None, response.safe_error_message or None,
            ),
        )
    return response


def _upsert_candidate(
    *,
    run_id: str,
    slot_run_id: str,
    package: dict[str, Any],
    plan: SlotDiscoveryPlan,
    route: str,
    source_provider: str,
    title: str,
    url: str,
    description: str = "",
    publication_date: str | None = None,
    mime_type: str = "",
    rank: int = 1,
    validation_status: str = "METADATA_VALID",
    explicit_status: str | None = None,
    rejection_reason: str = "",
    db_path: Path | str,
) -> dict[str, Any]:
    canonical = normalized_candidate_url(url) or url
    domains = {
        (urlparse(str(package.get(field) or "")).hostname or "").lower().removeprefix("www.")
        for field in ("official_website_url", "official_ir_url", "official_newsroom_url")
        if package.get(field)
    }
    validation = CandidateValidationAgent().validate(
        title=title, url=url, slot_type=plan.normalized_slot_type, company_name=str(package.get("company_name") or ""),
        ticker=package["ticker"], official_domains=domains, description=description,
        publication_date=publication_date, research_cutoff=str(package["research_cutoff_date"]),
    )
    status = explicit_status or ("ELIGIBLE" if validation.eligible else validation.status)
    reason_code = "" if validation.eligible else validation.reason_code
    reason = rejection_reason or validation.reason
    anchor = get_earnings_anchor(package["package_id"], db_path=db_path)
    if (
        status == "ELIGIBLE"
        and plan.normalized_slot_type == "material_company_press_releases_since_last_earnings_release"
        and publication_date
        and (anchor or {}).get("earnings_release_date")
        and publication_date < str(anchor["earnings_release_date"])
    ):
        status = "OUTSIDE_WINDOW"
        reason_code = "BEFORE_EARNINGS_ANCHOR"
        reason = "The company release predates the latest completed earnings cycle."
    score = CandidateRankingAgent().rank(
        slot_type=plan.normalized_slot_type, title=title, url=url, description=description, source_route=route,
        company_name=str(package.get("company_name") or ""), ticker=package["ticker"], publication_date=publication_date,
        anchor_date=(anchor or {}).get("earnings_release_date"), mime_type=mime_type, validation_status=validation_status,
    )
    profile = next((row for row in list_active_search_profiles(db_path=db_path) if row["normalized_slot_type"] == plan.normalized_slot_type), {})
    if status == "ELIGIBLE":
        status = "NEEDS_ANALYST_REVIEW" if score.overall_score >= float(profile.get("minimum_review_score", 55)) else "REJECTED"
    if status in REJECTED_CANDIDATE_STATUSES and not reason_code:
        reason_code = status
    now = database.utc_now_iso()
    candidate_id = _token("CAND")
    metadata = {"description": description, "score_reasons": score.reasons, "final_package_format_pending": "PDF" if mime_type != "application/pdf" else None}
    with database.get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT candidate_id FROM discovered_candidates WHERE package_slot_instance_id=? AND canonical_url=?",
            (plan.package_slot_instance_id, canonical),
        ).fetchone()
        if existing:
            candidate_id = existing["candidate_id"]
            connection.execute(
                """UPDATE discovered_candidates SET discovery_run_id=?, slot_discovery_run_id=?, source_provider=?, source_route=?,
                   rank=?, title=?, original_url=?, publication_date=?, mime_type=?, file_extension=?,
                   company_identity_score=?, source_authority_score=?, slot_relevance_score=?, freshness_score=?,
                   format_score=?, overall_score=?, validation_status=?, candidate_status=?, rejection_reason_code=?,
                   rejection_reason=?, metadata_json=?, updated_at=? WHERE candidate_id=?""",
                (
                    run_id, slot_run_id, source_provider, route, rank, title, url, publication_date, mime_type,
                    Path(urlparse(url).path).suffix.lower(), score.company_identity_score, score.source_authority_score,
                    score.slot_relevance_score, score.freshness_score, score.format_score, score.overall_score,
                    validation_status if validation.eligible else validation.status, status, reason_code or None, reason or None,
                    _json(metadata), now, candidate_id,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO discovered_candidates(
                    candidate_id, discovery_run_id, slot_discovery_run_id, package_id, package_slot_instance_id,
                    source_provider, source_route, rank, title, canonical_url, original_url, domain,
                    publication_date, date_confidence, mime_type, file_extension, company_identity_score,
                    source_authority_score, slot_relevance_score, freshness_score, format_score, overall_score,
                    validation_status, candidate_status, rejection_reason_code, rejection_reason, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id, run_id, slot_run_id, package["package_id"], plan.package_slot_instance_id,
                    source_provider, route, rank, title, canonical, url, (urlparse(canonical).hostname or "").lower(),
                    publication_date, "HIGH" if publication_date else "UNKNOWN", mime_type, Path(urlparse(url).path).suffix.lower(),
                    score.company_identity_score, score.source_authority_score, score.slot_relevance_score, score.freshness_score,
                    score.format_score, score.overall_score, validation_status if validation.eligible else validation.status,
                    status, reason_code or None, reason or None, _json(metadata), now, now,
                ),
            )
    return get_candidate(candidate_id, db_path=db_path) or {}


def _fiscal_quarter_from_text(value: str) -> str | None:
    normalized = value.lower()
    match = re.search(r"\bq([1-4])\b", normalized)
    if match:
        return f"Q{match.group(1)}"
    words = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}
    return next((quarter for word, quarter in words.items() if f"{word} quarter" in normalized), None)


def _fiscal_year_from_text(value: str) -> int | None:
    match = re.search(r"\b(?:fy\s*)?(20\d{2})\b", value, re.I)
    return int(match.group(1)) if match else None


def _store_anchor(package_id: str, anchor: EarningsAnchor, *, db_path: Path | str) -> dict[str, Any]:
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute("DELETE FROM earnings_cycle_anchors WHERE package_id=? AND analyst_override=0", (package_id,))
        anchor_id = _token("ANCHOR")
        connection.execute(
            """INSERT INTO earnings_cycle_anchors(
                anchor_id, package_id, fiscal_year, fiscal_quarter, reporting_period_end, earnings_release_date,
                filing_date, anchor_source, confidence, validation_status, analyst_override, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (anchor_id, package_id, anchor.fiscal_year, anchor.fiscal_quarter, anchor.reporting_period_end,
             anchor.earnings_release_date, anchor.filing_date, anchor.anchor_source, anchor.confidence,
             anchor.validation_status, now, now),
        )
    return get_earnings_anchor(package_id, db_path=db_path) or {}


def get_earnings_anchor(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM earnings_cycle_anchors WHERE package_id=? ORDER BY analyst_override DESC, created_at DESC LIMIT 1",
            (package_id,),
        ).fetchone()
    return dict(row) if row else None


def override_earnings_anchor(
    package_id: str,
    values: dict[str, Any],
    *,
    reason: str,
    actor: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("An earnings-anchor override reason is required.")
    current = get_earnings_anchor(package_id, db_path=db_path)
    now = database.utc_now_iso()
    anchor_id = _token("ANCHOR")
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO earnings_cycle_anchors(
                anchor_id, package_id, fiscal_year, fiscal_quarter, reporting_period_end, earnings_release_date,
                filing_date, anchor_source, confidence, validation_status, analyst_override, override_reason,
                created_at, updated_at, approved_at, approved_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ANALYST_OVERRIDE', 'HIGH', 'CONFIRMED', 1, ?, ?, ?, ?, ?)""",
            (anchor_id, package_id, values.get("fiscal_year"), values.get("fiscal_quarter"), values.get("reporting_period_end"),
             values.get("earnings_release_date"), values.get("filing_date"), reason.strip(), now, now, now, actor),
        )
        connection.execute(
            "INSERT INTO phase6a_audit_events VALUES (?, ?, NULL, NULL, NULL, 'EARNINGS_ANCHOR_OVERRIDDEN', ?, ?, ?)",
            (_token("P6B"), package_id, _json({"previous_anchor_id": (current or {}).get("anchor_id"), "new_anchor_id": anchor_id, "reason": reason}), actor, now),
        )
    return get_earnings_anchor(package_id, db_path=db_path) or {}


def run_discovery(
    package_id: str,
    *,
    slot_instance_ids: Iterable[str] | None = None,
    actor: str,
    provider: SearchProvider | None = None,
    db_path: Path | str = config.DATABASE_PATH,
    resume_run_id: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    database.initialize_database(db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    instance = get_package_recipe_instance(package_id, db_path=db_path)
    if not package or not instance:
        raise ValueError("Discovery requires a recipe-backed package.")
    search_provider = provider or get_search_provider()
    selected_ids = set(slot_instance_ids or [])
    plans = [plan for plan in RecipePlannerAgent().plan(package_id, db_path=db_path) if plan.auto_searchable and (not selected_ids or plan.package_slot_instance_id in selected_ids)]
    now = database.utc_now_iso()
    run_id = resume_run_id or _token("DISC")
    with database.get_connection(db_path) as connection:
        if resume_run_id:
            connection.execute("UPDATE package_discovery_runs SET status='RUNNING', completed_at=NULL, safe_error_json='{}' WHERE discovery_run_id=?", (run_id,))
        else:
            connection.execute(
                """INSERT INTO package_discovery_runs(
                    discovery_run_id, package_id, package_recipe_instance_id, search_profile_version,
                    status, started_at, started_by, slot_count_requested
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?)""",
                (run_id, package_id, instance["package_recipe_instance_id"], SEARCH_PROFILE_VERSION, now, actor, len(plans)),
            )
    started = time.perf_counter()
    warnings: list[str] = []
    completed = 0
    sec_inventory: list[FilingCandidate] | None = None
    official_discovery: dict[str, Any] | None = None
    official_resolution_attempted = False
    anchor = get_earnings_anchor(package_id, db_path=db_path)
    anchor_model = EarningsAnchor(
        anchor.get("fiscal_year"), anchor.get("fiscal_quarter"), anchor.get("reporting_period_end"),
        anchor.get("earnings_release_date"), anchor.get("filing_date"), anchor.get("anchor_source", ""),
        anchor.get("confidence", "LOW"), anchor.get("validation_status", "NEEDS_ANALYST_REVIEW"), (),
    ) if anchor else None
    for plan in plans:
        slot_started = time.perf_counter()
        route = SourceRouterAgent().route(plan, package, brave_configured=search_provider.is_configured())
        _store_router_decision(run_id, package_id, plan, route, db_path=db_path)
        with database.get_connection(db_path) as connection:
            previous = connection.execute(
                "SELECT * FROM slot_discovery_runs WHERE discovery_run_id=? AND package_slot_instance_id=? ORDER BY started_at DESC LIMIT 1",
                (run_id, plan.package_slot_instance_id),
            ).fetchone()
            if previous and previous["status"] == "COMPLETED":
                completed += 1
                continue
            slot_run_id = previous["slot_discovery_run_id"] if previous else _token("SDISC")
            if previous:
                connection.execute("UPDATE slot_discovery_runs SET status='RUNNING', started_at=?, completed_at=NULL WHERE slot_discovery_run_id=?", (database.utc_now_iso(), slot_run_id))
            else:
                connection.execute(
                    """INSERT INTO slot_discovery_runs(
                        slot_discovery_run_id, discovery_run_id, package_id, package_slot_instance_id,
                        normalized_slot_type, source_route, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)""",
                    (slot_run_id, run_id, package_id, plan.package_slot_instance_id, plan.normalized_slot_type, route.selected_route, database.utc_now_iso()),
                )
        slot_warnings: list[str] = []
        try:
            created: list[dict[str, Any]] = []
            if plan.normalized_slot_type in SEC_SLOT_TYPES or plan.normalized_slot_type == "latest_earnings_release":
                if sec_inventory is None:
                    sec_inventory = preview_cutler_profile(
                        package, enabled_families={"10-K", "10-Q", "8-K", "DEF 14A"}, session=session, db_path=db_path,
                    )
                    curated = select_curated_sec_filings(sec_inventory, research_cutoff=str(package["research_cutoff_date"])[:10])
                    anchor_sources = [
                        {
                            "source_type": "SEC_8K" if row.get("form_type") == "8-K" else "SEC",
                            "reporting_period_end": row.get("report_period"), "filing_date": row.get("filing_date"),
                            "earnings_release_date": row.get("filing_date") if row.get("form_type") == "8-K" else None,
                            "fiscal_year": int(str(row.get("report_period") or row.get("filing_date"))[:4]) if str(row.get("report_period") or row.get("filing_date"))[:4].isdigit() else None,
                        }
                        for rows in curated.values() for row in rows
                    ]
                    if anchor_sources:
                        anchor_model = EarningsAnchorAgent().determine(anchor_sources)
                        _store_anchor(package_id, anchor_model, db_path=db_path)
                curated = select_curated_sec_filings(sec_inventory or [], research_cutoff=str(package["research_cutoff_date"])[:10])
                for rank, row in enumerate(curated.get(plan.normalized_slot_type, []), start=1):
                    created.append(_upsert_candidate(
                        run_id=run_id, slot_run_id=slot_run_id, package=package, plan=plan, route="SEC", source_provider="SEC",
                        title=str(row.get("title") or f"{package['ticker']} {row.get('form_type')}"), url=str(row.get("primary_document_url") or ""),
                        description=str(row.get("selection_reason") or "Curated SEC filing"), publication_date=str(row.get("filing_date") or "") or None,
                        mime_type="text/html", rank=rank, db_path=db_path,
                    ))
            if plan.normalized_slot_type in OFFICIAL_IR_CATEGORIES:
                if not official_resolution_attempted:
                    official_resolution_attempted = True
                    official = package.get("official_website_url")
                    if not official:
                        resolved, _ = resolve_official_company_website(package, session=session, db_path=db_path)
                        official = resolved.url if resolved else None
                        package = database.get_package_by_package_id(package_id, db_path=db_path) or package
                    if official:
                        official_discovery = discover_official_ir_materials(package, str(official), session=session, db_path=db_path)
                if official_discovery:
                    for rank, material in enumerate(official_discovery["materials"], start=1):
                        if material.category not in OFFICIAL_IR_CATEGORIES[plan.normalized_slot_type]:
                            continue
                        explicit = material.download_status if material.download_status in REJECTED_CANDIDATE_STATUSES | {"NEEDS_JS_MANUAL_REVIEW"} else None
                        created.append(_upsert_candidate(
                            run_id=run_id, slot_run_id=slot_run_id, package=package, plan=plan, route="OFFICIAL_IR", source_provider="OFFICIAL_SITE",
                            title=material.title, url=material.source_url, description=material.discovery_method,
                            publication_date=material.publication_date or None, mime_type=material.mime_type, rank=rank,
                            explicit_status=explicit, rejection_reason=material.rejection_reason, db_path=db_path,
                        ))
                        if (
                            plan.normalized_slot_type == "latest_earnings_release"
                            and material.publication_date
                            and explicit not in REJECTED_CANDIDATE_STATUSES
                        ):
                            prior = anchor_model
                            anchor_sources = []
                            if prior:
                                anchor_sources.append({
                                    "source_type": "SEC", "fiscal_year": prior.fiscal_year,
                                    "fiscal_quarter": prior.fiscal_quarter,
                                    "reporting_period_end": prior.reporting_period_end,
                                    "filing_date": prior.filing_date,
                                })
                            anchor_sources.append({
                                "source_type": "OFFICIAL_EARNINGS_RELEASE",
                                "fiscal_year": _fiscal_year_from_text(f"{material.title} {material.source_url}") or (prior.fiscal_year if prior else None),
                                "fiscal_quarter": _fiscal_quarter_from_text(f"{material.title} {material.source_url}") or (prior.fiscal_quarter if prior else None),
                                "reporting_period_end": prior.reporting_period_end if prior else None,
                                "earnings_release_date": material.publication_date,
                            })
                            anchor_model = EarningsAnchorAgent().determine(anchor_sources)
                            _store_anchor(package_id, anchor_model, db_path=db_path)
                queries = SlotQueryPlannerAgent().plan_queries(plan, package, anchor_model)
                probable = any(row.get("candidate_status") not in REJECTED_CANDIDATE_STATUSES for row in created)
                if not probable and search_provider.is_configured():
                    for request in queries:
                        with database.get_connection(db_path) as connection:
                            used = int(connection.execute(
                                "SELECT COUNT(*) FROM search_queries WHERE discovery_run_id=? AND cache_hit=0", (run_id,)
                            ).fetchone()[0])
                        if used >= config.BRAVE_MAX_QUERIES_PER_PACKAGE:
                            slot_warnings.append("PACKAGE_QUERY_BUDGET_REACHED")
                            break
                        response = _execute_search(request, provider=search_provider, discovery_run_id=run_id, slot_run_id=slot_run_id, db_path=db_path)
                        for result in response.results[: plan.maximum_candidates]:
                            created.append(_upsert_candidate(
                                run_id=run_id, slot_run_id=slot_run_id, package=package, plan=plan, route="BRAVE_OFFICIAL",
                                source_provider=search_provider.provider_name, title=result.title, url=result.url,
                                description=" ".join((result.description, *result.extra_snippets)), rank=result.rank, db_path=db_path,
                            ))
                        if (
                            response.more_results_available
                            and config.BRAVE_MAX_PAGES_PER_QUERY > 1
                            and len(created) < plan.maximum_candidates
                            and used + 1 < config.BRAVE_MAX_QUERIES_PER_PACKAGE
                            and used + 1 < plan.maximum_queries
                        ):
                            second = BraveSearchRequest(**{**asdict(request), "offset": 1})
                            page = _execute_search(second, provider=search_provider, discovery_run_id=run_id, slot_run_id=slot_run_id, db_path=db_path)
                            for result in page.results[: max(0, plan.maximum_candidates - len(created))]:
                                created.append(_upsert_candidate(
                                    run_id=run_id, slot_run_id=slot_run_id, package=package, plan=plan, route="BRAVE_OFFICIAL",
                                    source_provider=search_provider.provider_name, title=result.title, url=result.url,
                                    description=" ".join((result.description, *result.extra_snippets)), rank=result.rank, db_path=db_path,
                                ))
            completed += 1
            rejected = sum(row.get("candidate_status") in REJECTED_CANDIDATE_STATUSES for row in created)
            with database.get_connection(db_path) as connection:
                query_stats = connection.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(result_count),0) AS results, COALESCE(SUM(cache_hit),0) AS cached FROM search_queries WHERE slot_discovery_run_id=?",
                    (slot_run_id,),
                ).fetchone()
                connection.execute(
                    """UPDATE slot_discovery_runs SET status='COMPLETED', completed_at=?, query_count=?, result_count=?,
                       candidate_count=?, rejected_count=?, cached=?, warnings_json=?, duration_ms=? WHERE slot_discovery_run_id=?""",
                    (database.utc_now_iso(), query_stats["count"], query_stats["results"], len(created), rejected,
                     int(query_stats["cached"] > 0), _json(slot_warnings), round((time.perf_counter() - slot_started) * 1000), slot_run_id),
                )
        except Exception as exc:
            safe_message = f"{type(exc).__name__}: discovery stage failed."
            warnings.append(f"{plan.display_name}: {safe_message}")
            with database.get_connection(db_path) as connection:
                connection.execute(
                    "UPDATE slot_discovery_runs SET status='FAILED', completed_at=?, safe_error_json=?, duration_ms=? WHERE slot_discovery_run_id=?",
                    (database.utc_now_iso(), _json({"category": type(exc).__name__, "message": "Slot discovery failed safely."}), round((time.perf_counter() - slot_started) * 1000), slot_run_id),
                )
    duration = round((time.perf_counter() - started) * 1000)
    with database.get_connection(db_path) as connection:
        stats = connection.execute(
            """SELECT COUNT(*) AS candidates,
                      SUM(CASE WHEN candidate_status IN ('REJECTED','NON_INVESTOR_MATERIAL','MIME_MISMATCH','COMPANY_MISMATCH','FAILED') THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN candidate_status='AUTO_SELECTED' THEN 1 ELSE 0 END) AS auto_selected,
                      SUM(CASE WHEN candidate_status='NEEDS_ANALYST_REVIEW' THEN 1 ELSE 0 END) AS review
               FROM discovered_candidates WHERE discovery_run_id=?""",
            (run_id,),
        ).fetchone()
        query_stats = connection.execute(
            "SELECT COUNT(*) AS queries, COALESCE(SUM(cache_hit),0) AS cached, COALESCE(SUM(result_count),0) AS results FROM search_queries WHERE discovery_run_id=?",
            (run_id,),
        ).fetchone()
        failed_slots = int(connection.execute("SELECT COUNT(*) FROM slot_discovery_runs WHERE discovery_run_id=? AND status='FAILED'", (run_id,)).fetchone()[0])
        status = "COMPLETED_WITH_WARNINGS" if warnings or failed_slots else "COMPLETED"
        connection.execute(
            """UPDATE package_discovery_runs SET status=?, completed_at=?, slot_count_completed=?, queries_executed=?,
               cached_queries_reused=?, results_considered=?, candidates_created=?, candidates_rejected=?,
               candidates_auto_selected=?, candidates_needing_review=?, warnings_json=?, total_duration_ms=?
               WHERE discovery_run_id=?""",
            (status, database.utc_now_iso(), completed, query_stats["queries"] - query_stats["cached"], query_stats["cached"],
             query_stats["results"], stats["candidates"], stats["rejected"] or 0, stats["auto_selected"] or 0,
             stats["review"] or 0, _json(warnings), duration, run_id),
        )
        actual_requests = int(query_stats["queries"] - query_stats["cached"])
        estimated = actual_requests * config.BRAVE_COST_PER_1000_REQUESTS / 1000 if config.BRAVE_COST_PER_1000_REQUESTS is not None else None
        connection.execute(
            "INSERT INTO discovery_usage VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (_token("USE"), run_id, search_provider.provider_name, actual_requests, query_stats["cached"], query_stats["results"], estimated, database.utc_now_iso()),
        )
    return get_discovery_run(run_id, db_path=db_path) or {}


def get_discovery_run(run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM package_discovery_runs WHERE discovery_run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_discovery_run(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM package_discovery_runs WHERE package_id=? ORDER BY started_at DESC LIMIT 1", (package_id,)
        ).fetchone()
    return dict(row) if row else None


def resume_discovery(package_id: str, *, actor: str, provider: SearchProvider | None = None, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = latest_discovery_run(package_id, db_path=db_path)
    if not run or run["status"] not in {"PARTIAL", "FAILED", "INTERRUPTED", "COMPLETED_WITH_WARNINGS"}:
        raise ValueError("No interrupted or partial discovery run is available to resume.")
    with database.get_connection(db_path) as connection:
        failed = connection.execute(
            "SELECT package_slot_instance_id FROM slot_discovery_runs WHERE discovery_run_id=? AND status!='COMPLETED'", (run["discovery_run_id"],)
        ).fetchall()
    return run_discovery(package_id, slot_instance_ids=[row[0] for row in failed], actor=actor, provider=provider, db_path=db_path, resume_run_id=run["discovery_run_id"])


def stop_discovery(package_id: str, *, actor: str, db_path: Path | str = config.DATABASE_PATH) -> bool:
    run = latest_discovery_run(package_id, db_path=db_path)
    if not run or run["status"] not in {"PENDING", "RUNNING"}:
        return False
    with database.get_connection(db_path) as connection:
        connection.execute("UPDATE package_discovery_runs SET status='CANCELLED', completed_at=? WHERE discovery_run_id=?", (database.utc_now_iso(), run["discovery_run_id"]))
    return True


def list_discovery_candidates(
    package_id: str,
    *,
    slot_instance_id: str | None = None,
    include_audit_only: bool = False,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    database.initialize_database(db_path)
    clauses, params = ["package_id=?"], [package_id]
    if slot_instance_id:
        clauses.append("package_slot_instance_id=?")
        params.append(slot_instance_id)
    if not include_audit_only:
        clauses.append("candidate_status NOT IN ('NON_INVESTOR_MATERIAL','MIME_MISMATCH','COMPANY_MISMATCH','FAILED')")
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            f"SELECT * FROM discovered_candidates WHERE {' AND '.join(clauses)} ORDER BY overall_score DESC, rank", tuple(params)
        ).fetchall()
    return [dict(row) for row in rows]


def get_candidate(candidate_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM discovered_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def decide_candidate(
    candidate_id: str,
    decision: str,
    *,
    actor: str,
    reason_code: str = "",
    notes: str = "",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, db_path=db_path)
    if not candidate:
        raise ValueError("Discovery candidate does not exist.")
    normalized = decision.strip().upper()
    statuses = {"REJECT": "REJECTED", "DEFER": "NEEDS_ANALYST_REVIEW", "MARK_DUPLICATE": "DUPLICATE", "REPLACE": "APPROVED"}
    if normalized not in statuses:
        raise ValueError("Unsupported candidate decision.")
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        previous = connection.execute("SELECT decision_id FROM candidate_decisions WHERE candidate_id=? ORDER BY decided_at DESC LIMIT 1", (candidate_id,)).fetchone()
        connection.execute(
            "INSERT INTO candidate_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_token("CDEC"), candidate_id, candidate["package_id"], candidate["package_slot_instance_id"], normalized,
             reason_code or None, notes or None, now, actor, previous[0] if previous else None),
        )
        connection.execute(
            "UPDATE discovered_candidates SET candidate_status=?, rejection_reason_code=?, rejection_reason=?, updated_at=? WHERE candidate_id=?",
            (statuses[normalized], reason_code or None, notes or None, now, candidate_id),
        )
    return get_candidate(candidate_id, db_path=db_path) or {}


def approve_and_download_candidate(
    candidate_id: str,
    *,
    actor: str,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, db_path=db_path)
    if not candidate or candidate["candidate_status"] in REJECTED_CANDIDATE_STATUSES:
        raise ValueError("Only an eligible discovery candidate can be approved.")
    package = database.get_package_by_package_id(candidate["package_id"], db_path=db_path)
    if not package:
        raise ValueError("Candidate package no longer exists.")
    existing = database.get_document_by_url(package["package_id"], candidate["canonical_url"], db_path=db_path)
    if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
        document = existing
        status = "ALREADY_COLLECTED"
    else:
        validated = fetch_and_validate_candidate(candidate["canonical_url"], session=session)
        if not validated.eligible:
            with database.get_connection(db_path) as connection:
                connection.execute(
                    "UPDATE discovered_candidates SET candidate_status=?, validation_status=?, rejection_reason_code=?, rejection_reason=?, updated_at=? WHERE candidate_id=?",
                    (validated.status, validated.status, validated.reason_code, validated.reason, database.utc_now_iso(), candidate_id),
                )
            raise ValueError(validated.reason or "Candidate content validation failed.")
        duplicate = database.get_document_by_hash(package["package_id"], validated.sha256_hash, db_path=db_path)
        if duplicate and duplicate.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            document = duplicate
            status = "DUPLICATE"
        else:
            suffix = validated.file_extension or mimetypes.guess_extension(validated.mime_type) or ".bin"
            filename = sanitize_filename(f"{package['ticker']}_{candidate['title']}{suffix}")
            path = safe_document_path(package["package_id"], "discovery", filename)
            atomic_write_bytes(path, validated.content)
            document = database.create_document_record(
                {
                    "document_id": database.generate_document_id("DOC-DISC"), "package_id": package["package_id"],
                    "ticker": package["ticker"], "category": "Official Company Material" if candidate["source_route"] != "SEC" else "SEC Filing",
                    "document_type": "Discovery Candidate", "title": candidate["title"], "source_name": candidate["domain"],
                    "source_url": validated.final_url or candidate["canonical_url"], "source_domain": candidate["domain"],
                    "publication_date": candidate.get("publication_date"), "local_filename": path.name, "local_path": str(path),
                    "mime_type": validated.mime_type, "file_size_bytes": validated.content_length, "sha256_hash": validated.sha256_hash,
                    "collection_method": "PUBLIC_DISCOVERY", "collection_status": config.DOCUMENT_STATUS_DOWNLOADED, "is_public": True,
                },
                db_path=db_path,
            )
            database.update_document_metadata(
                document["document_id"],
                {"canonical_url": candidate["canonical_url"], "official_domain": candidate["domain"],
                 "final_package_format_pending": "PDF" if validated.mime_type != "application/pdf" else None},
                db_path=db_path,
            )
            status = "DOWNLOADED"
    try:
        assignment = assign_document(
            candidate["package_slot_instance_id"], document["document_id"], actor=actor,
            assignment_source="PUBLIC_DISCOVERY", db_path=db_path,
        )
    except ValueError as exc:
        if "already assigned" not in str(exc).lower():
            raise
        assignment = next(
            row for row in list_assignments(package["package_id"], db_path=db_path)
            if row["package_slot_instance_id"] == candidate["package_slot_instance_id"] and row["document_id"] == document["document_id"]
        )
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        previous = connection.execute("SELECT decision_id FROM candidate_decisions WHERE candidate_id=? ORDER BY decided_at DESC LIMIT 1", (candidate_id,)).fetchone()
        connection.execute(
            "INSERT INTO candidate_decisions VALUES (?, ?, ?, ?, 'ACCEPT', 'ANALYST_APPROVED', NULL, ?, ?, ?)",
            (_token("CDEC"), candidate_id, package["package_id"], candidate["package_slot_instance_id"], now, actor, previous[0] if previous else None),
        )
        connection.execute(
            """UPDATE discovered_candidates SET candidate_status=?, validation_status='CONTENT_VALID', final_redirect_url=?,
               mime_type=?, file_extension=?, file_signature=?, content_length=?, updated_at=? WHERE candidate_id=?""",
            (status, validated.final_url if 'validated' in locals() else candidate.get("final_redirect_url"),
             validated.mime_type if 'validated' in locals() else candidate.get("mime_type"),
             validated.file_extension if 'validated' in locals() else candidate.get("file_extension"),
             validated.file_signature if 'validated' in locals() else candidate.get("file_signature"),
             validated.content_length if 'validated' in locals() else candidate.get("content_length"), now, candidate_id),
        )
    recalculate_completion(package["package_id"], actor=actor, db_path=db_path)
    return {"candidate": get_candidate(candidate_id, db_path=db_path), "document": document, "assignment": assignment}


def discovery_audit_details(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = latest_discovery_run(package_id, db_path=db_path)
    candidates = list_discovery_candidates(package_id, include_audit_only=True, db_path=db_path)
    with database.get_connection(db_path) as connection:
        routes = [dict(row) for row in connection.execute(
            "SELECT * FROM source_router_decisions WHERE package_id=? ORDER BY created_at", (package_id,)
        ).fetchall()]
        queries = [dict(row) for row in connection.execute(
            "SELECT * FROM search_queries WHERE package_id=? ORDER BY executed_at", (package_id,)
        ).fetchall()]
        usage = [dict(row) for row in connection.execute(
            "SELECT * FROM discovery_usage WHERE discovery_run_id=?", ((run or {}).get("discovery_run_id"),)
        ).fetchall()] if run else []
    funnel: dict[str, int] = {}
    rejections: dict[str, int] = {}
    for candidate in candidates:
        funnel[candidate["candidate_status"]] = funnel.get(candidate["candidate_status"], 0) + 1
        if candidate.get("rejection_reason_code"):
            rejections[candidate["rejection_reason_code"]] = rejections.get(candidate["rejection_reason_code"], 0) + 1
    return {
        "search_configuration": {
            "provider": config.SEARCH_PROVIDER, "configured": bool(config.brave_search_api_key()),
            "search_profile_version": SEARCH_PROFILE_VERSION, "package_query_budget": config.BRAVE_MAX_QUERIES_PER_PACKAGE,
            "cache_hours": config.BRAVE_QUERY_CACHE_HOURS,
        },
        "earnings_anchor": get_earnings_anchor(package_id, db_path=db_path),
        "source_routing": routes,
        "brave_usage": [{key: value for key, value in row.items() if key not in {"query_text"}} for row in queries],
        "usage_summary": usage,
        "candidate_funnel": funnel,
        "rejection_reasons": rejections,
        "latest_run": run,
    }


class CandidateDiscoveryAgent:
    """Structured facade used by the orchestrator for one bounded package run."""

    def run(self, package_id: str, *, actor: str, provider: SearchProvider | None = None, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
        return run_discovery(package_id, actor=actor, provider=provider, db_path=db_path)
