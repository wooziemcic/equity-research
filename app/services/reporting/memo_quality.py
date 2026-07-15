from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from app import config
from app.services.openai_service import OpenAIProviderError, structured_parse
from app.utils import database


SUPPORTED = {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
GENERIC_HEADINGS = {
    "UNITED STATES", "UNITED", "SECURITIES AND EXCHANGE COMMISSION", "WASHINGTON D C",
    "FORM 10 K", "FORM 10 Q", "PART I", "PART II", "TABLE OF CONTENTS", "SIGNATURES",
}
FAMILY_PRIORITY = {
    "revenue_growth": 10,
    "profitability": 9,
    "cash_flow": 8,
    "debt_liquidity": 7,
    "guidance": 6,
    "strategy": 5,
    "operating_driver": 4,
    "risk": 3,
    "valuation": 2,
    "capital_allocation": 1,
}
FINANCIAL_FAMILIES = {"revenue_growth", "profitability", "cash_flow", "debt_liquidity", "valuation", "capital_allocation"}
IMMATERIAL_TERMS = (
    "geographic information", "geographical", "jurisdiction", "internal revenue code",
    "accounting policy", "accounting policies", "tax carryforward", "deferred tax asset",
    "asset retirement obligation", "customer advances",
)
BOILERPLATE_TERMS = (
    "forward looking statements", "table of contents", "securities and exchange commission",
    "united states", "washington d.c", "signatures", "large accelerated filer",
    "aggregate market value of the registrant", "held by non-affiliates",
    "forward-looking terms", "other comparable terms",
)
UNSUPPORTED_INFERENCE_TERMS = (
    "demonstrating market confidence", "indicating market confidence", "showing market confidence",
    "diluting shareholder value", "demonstrating financial strength", "indicating financial strength",
)
RISK_FAMILIES = {
    "acquisition_integration": ("acquisition", "integration", "business combination"),
    "leverage_refinancing": ("leverage", "refinancing", "debt", "covenant", "interest expense"),
    "execution": ("execution", "implement", "strategy", "operational"),
    "demand_macro": ("demand", "macroeconomic", "recession", "cyclical", "housing"),
    "competition_pricing": ("competition", "competitive", "pricing pressure"),
    "concentration": ("concentration", "customer", "supplier"),
    "regulatory_litigation": ("regulatory", "litigation", "lawsuit", "legal proceeding"),
    "liquidity": ("liquidity", "cash requirements", "capital resources"),
    "dilution_capital_structure": ("dilution", "convertible", "capital structure", "common stock"),
}


def _has_ellipsis(value: Any) -> bool:
    text = str(value or "")
    return "..." in text or chr(0x2026) in text


class MemoGenerationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "MEMO_GENERATION_FAILED", attempt_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.attempt_id = attempt_id


class MemoDraftItem(BaseModel):
    candidate_id: str
    concise_claim: str


class InvestmentMemoDraft(BaseModel):
    investment_view: str
    supporting_facts: list[MemoDraftItem] = Field(default_factory=list)
    risks: list[MemoDraftItem] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    conclusion: str


@dataclass
class MemoEvidenceCandidate:
    candidate_id: str
    evidence_id: str
    version_document_id: str
    claim_family: str
    claim_text: str
    supporting_quote: str
    metric_name: str | None
    numeric_value: float | None
    unit: str | None
    currency: str | None
    reporting_period: str | None
    filing_or_publication_date: str | None
    source_type: str
    form_type: str | None
    section_heading: str | None
    page_number: int | None
    source_priority: float
    recency_score: float
    materiality_score: float
    completeness_score: float
    decision_relevance_score: float
    rejection_reasons: list[str] = field(default_factory=list)
    eligible_for_memo: bool = False
    candidate_kind: str = "SUPPORTING"
    citation: str = ""

    def to_record(self, attempt_id: str, analysis_run_id: str) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "attempt_id": attempt_id,
            "analysis_run_id": analysis_run_id,
            "evidence_id": self.evidence_id,
            "version_document_id": self.version_document_id,
            "claim_family": self.claim_family,
            "claim_text": self.claim_text,
            "supporting_quote": self.supporting_quote,
            "metric_name": self.metric_name,
            "numeric_value": self.numeric_value,
            "unit": self.unit,
            "currency": self.currency,
            "reporting_period": self.reporting_period,
            "filing_or_publication_date": self.filing_or_publication_date,
            "source_type": self.source_type,
            "form_type": self.form_type,
            "section_heading": self.section_heading,
            "page_number": self.page_number,
            "source_priority": self.source_priority,
            "recency_score": self.recency_score,
            "materiality_score": self.materiality_score,
            "completeness_score": self.completeness_score,
            "decision_relevance_score": self.decision_relevance_score,
            "rejection_reasons_json": json.dumps(self.rejection_reasons, sort_keys=True),
            "eligible_for_memo": int(self.eligible_for_memo),
            "candidate_kind": self.candidate_kind,
            "citation": self.citation,
            "created_at": database.utc_now_iso(),
        }

    def model_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "claim_family": self.claim_family,
            "supporting_quote": self.supporting_quote,
            "metric_name": self.metric_name,
            "numeric_value": self.numeric_value,
            "unit": self.unit,
            "currency": self.currency,
            "reporting_period": self.reporting_period,
            "source_type": self.source_type,
            "form_type": self.form_type,
        }


def create_memo_candidates(
    analysis_run_id: str,
    attempt_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[list[MemoEvidenceCandidate], dict[str, Any]]:
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise MemoGenerationError("Analysis run does not exist.", attempt_id=attempt_id)
    version = database.get_package_version(run["version_id"], db_path=db_path) or {}
    package = database.get_package_by_package_id(run["package_id"], db_path=db_path) or {}
    docs = _document_lookup(run["version_id"], db_path=db_path)
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=run["version_id"], db_path=db_path)
    candidates: list[MemoEvidenceCandidate] = []
    for row in evidence:
        document = docs.get(str(row.get("version_document_id") or ""))
        candidate = _candidate_from_evidence(row, document, package, attempt_id)
        candidates.append(candidate)
    _apply_duplicate_and_recency_rules(candidates)
    for candidate in candidates:
        candidate.eligible_for_memo = not candidate.rejection_reasons
    database.create_memo_evidence_candidates(
        [candidate.to_record(attempt_id, analysis_run_id) for candidate in candidates],
        db_path=db_path,
    )
    return candidates, {"run": run, "version": version, "package": package}


def select_memo_candidates(
    candidates: list[MemoEvidenceCandidate],
) -> tuple[list[MemoEvidenceCandidate], list[MemoEvidenceCandidate]]:
    supporting = [item for item in candidates if item.eligible_for_memo and item.candidate_kind == "SUPPORTING"]
    risks = [item for item in candidates if item.eligible_for_memo and item.candidate_kind == "RISK"]
    supporting.sort(key=_candidate_sort_key, reverse=True)
    risks.sort(key=_candidate_sort_key, reverse=True)
    selected_supporting: list[MemoEvidenceCandidate] = []
    family_counts: dict[str, int] = {}
    for candidate in supporting:
        if family_counts.get(candidate.claim_family, 0):
            continue
        selected_supporting.append(candidate)
        family_counts[candidate.claim_family] = 1
        if len(selected_supporting) == 5:
            break
    if len(selected_supporting) < 3:
        for candidate in supporting:
            if candidate in selected_supporting or family_counts.get(candidate.claim_family, 0) >= 2:
                continue
            selected_supporting.append(candidate)
            family_counts[candidate.claim_family] = family_counts.get(candidate.claim_family, 0) + 1
            if len(selected_supporting) == min(5, len(supporting)):
                break
    selected_risks: list[MemoEvidenceCandidate] = []
    seen_risks: set[str] = set()
    for candidate in risks:
        if candidate.claim_family in seen_risks:
            continue
        selected_risks.append(candidate)
        seen_risks.add(candidate.claim_family)
        if len(selected_risks) == 4:
            break
    return selected_supporting[:5], selected_risks[:4]


def synthesize_memo(
    analysis_run_id: str,
    *,
    client: Any | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[dict[str, Any], str, list[MemoEvidenceCandidate]]:
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise MemoGenerationError("Analysis run does not exist.")
    attempt_id = f"MEMO-{secrets.token_hex(8).upper()}"
    database.create_memo_generation_attempt(
        {
            "attempt_id": attempt_id,
            "analysis_run_id": analysis_run_id,
            "version_id": run["version_id"],
            "processing_run_id": run["processing_run_id"],
            "status": "RUNNING",
            "model": config.OPENAI_MODEL if config.MEMO_SYNTHESIS_REQUIRED else "DETERMINISTIC_TEST_MODE",
            "endpoint": None,
            "selected_candidate_ids_json": "[]",
            "rejected_candidate_count": 0,
            "draft_json": None,
            "error_code": None,
            "error_message": None,
            "created_at": database.utc_now_iso(),
            "completed_at": None,
        },
        db_path=db_path,
    )
    try:
        candidates, context = create_memo_candidates(analysis_run_id, attempt_id, db_path=db_path)
        selected_supporting, selected_risks = select_memo_candidates(candidates)
        selected = [*selected_supporting, *selected_risks]
        limitations = _limitations(run, selected_supporting)
        database.update_memo_generation_attempt(
            attempt_id,
            {
                "selected_candidate_ids_json": json.dumps([item.candidate_id for item in selected]),
                "rejected_candidate_count": sum(bool(item.rejection_reasons) for item in candidates),
            },
            db_path=db_path,
        )
        if config.MEMO_SYNTHESIS_REQUIRED and len(selected_supporting) < 3:
            raise MemoGenerationError("Fewer than three strong, decision-relevant supporting facts were available.")
        if config.MEMO_SYNTHESIS_REQUIRED:
            result = structured_parse(
                system_prompt=_memo_system_prompt(),
                user_payload={
                    "company_name": context["version"].get("company_name") or context["package"].get("company_name"),
                    "ticker": context["version"].get("ticker") or context["package"].get("ticker"),
                    "recommendation": _recommendation(analysis_run_id, run, db_path=db_path),
                    "supporting_candidates": [item.model_payload() for item in selected_supporting],
                    "risk_candidates": [item.model_payload() for item in selected_risks],
                    "allowed_missing_information": limitations,
                    "prompt_version": config.MEMO_PROMPT_VERSION,
                    "schema_version": config.MEMO_SCHEMA_VERSION,
                },
                schema=InvestmentMemoDraft,
                client=client,
                max_output_tokens=config.MEMO_MAX_OUTPUT_TOKENS,
                pipeline_stage="investment_memo_synthesis",
                usage_context={
                    "analysis_run_id": analysis_run_id,
                    "processing_run_id": run["processing_run_id"],
                    "attempt_id": attempt_id,
                },
                db_path=str(db_path),
            )
            draft = result.parsed
            endpoint = result.endpoint
        else:
            draft = _deterministic_test_draft(selected_supporting, selected_risks, limitations, run)
            endpoint = "Deterministic test mode"
        database.update_memo_generation_attempt(
            attempt_id,
            {"endpoint": endpoint, "draft_json": draft.model_dump_json()},
            db_path=db_path,
        )
        memo = _validated_memo(draft, selected_supporting, selected_risks, context, run, analysis_run_id, db_path=db_path)
        database.update_memo_generation_attempt(
            attempt_id,
            {
                "status": "SYNTHESIZED",
                "endpoint": endpoint,
                "draft_json": json.dumps(memo, sort_keys=True),
                "completed_at": database.utc_now_iso(),
            },
            db_path=db_path,
        )
        database.update_analysis_run(
            analysis_run_id,
            {"memo_generation_status": "SYNTHESIZED", "memo_generation_error": None},
            db_path=db_path,
        )
        memo["memo_generation_attempt_id"] = attempt_id
        memo["memo_model"] = config.OPENAI_MODEL if config.MEMO_SYNTHESIS_REQUIRED else "DETERMINISTIC_TEST_MODE"
        memo["memo_endpoint"] = endpoint
        return memo, attempt_id, candidates
    except Exception as exc:
        code = exc.code if isinstance(exc, (MemoGenerationError, OpenAIProviderError)) else "MEMO_GENERATION_FAILED"
        message = exc.safe_message if isinstance(exc, OpenAIProviderError) else str(exc)
        database.update_memo_generation_attempt(
            attempt_id,
            {"status": "MEMO_GENERATION_FAILED", "error_code": code, "error_message": message[:500], "completed_at": database.utc_now_iso()},
            db_path=db_path,
        )
        database.update_analysis_run(
            analysis_run_id,
            {"memo_generation_status": "MEMO_GENERATION_FAILED", "memo_generation_error": message[:500]},
            db_path=db_path,
        )
        raise MemoGenerationError("Memo generation failed validation. Retry memo generation without rerunning research processing.", code=code, attempt_id=attempt_id) from exc


def audit_memo_quality(
    memo: dict[str, Any],
    candidates: list[MemoEvidenceCandidate],
    *,
    attempt_id: str,
    analysis_run_id: str,
    one_page_fit: bool,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    facts = [*memo.get("supporting_facts", []), *memo.get("risks", [])]
    prose = [memo.get("investment_view", ""), memo.get("conclusion", ""), *memo.get("missing_information", [])]
    all_claims = [str(item.get("claim") or "") for item in facts] + [str(item) for item in prose]
    checks = {
        "complete_sentence_check": _check(all(_is_complete_sentence(item) for item in all_claims if item)),
        "ellipsis_check": _check(all(not _has_ellipsis(item) for item in all_claims)),
        "citation_check": _check(all(bool(item.get("citation")) for item in facts)),
        "numeric_validation_check": _check(all(item.get("numeric_validation", True) for item in facts)),
        "period_validation_check": _check(all(item.get("period_validation", True) for item in facts)),
        "unit_validation_check": _check(all(item.get("unit_validation", True) for item in facts)),
        "currency_validation_check": _check(all(item.get("currency_validation", True) for item in facts)),
        "recency_check": _check(all(not item.rejection_reasons for item in candidates if item.candidate_id in {fact.get("candidate_id") for fact in facts})),
        "duplicate_check": _check(len({_normalize_claim(item.get("claim")) for item in facts}) == len(facts)),
        "source_heading_check": _check(all(not _generic_heading_from_citation(str(item.get("citation") or "")) for item in facts)),
        "risk_coverage_check": "PASSED" if memo.get("risks") else "PASSED_WITH_WARNINGS",
        "decision_relevance_check": "PASSED" if len(memo.get("supporting_facts", [])) >= 3 else "PASSED_WITH_WARNINGS",
        "one_page_fit_check": _check(one_page_fit),
    }
    critical = [key for key, value in checks.items() if value == "FAILED"]
    warnings = [key for key, value in checks.items() if value == "PASSED_WITH_WARNINGS"]
    if config.MEMO_SYNTHESIS_REQUIRED and len(memo.get("supporting_facts", [])) < 3:
        critical.append("decision_relevance_check")
        checks["decision_relevance_check"] = "FAILED"
    status = "FAILED" if critical else "PASSED_WITH_WARNINGS" if warnings else "PASSED"
    audit = {
        "audit_id": f"MQA-{secrets.token_hex(8).upper()}",
        "attempt_id": attempt_id,
        "analysis_run_id": analysis_run_id,
        "status": status,
        **checks,
        "reasons_json": json.dumps(sorted(set(critical + warnings))),
        "created_at": database.utc_now_iso(),
    }
    database.create_memo_quality_audit(audit, db_path=db_path)
    database.update_memo_generation_attempt(
        attempt_id,
        {"status": "COMPLETED" if status != "FAILED" else "MEMO_GENERATION_FAILED", "completed_at": database.utc_now_iso()},
        db_path=db_path,
    )
    database.update_analysis_run(
        analysis_run_id,
        {"memo_generation_status": status, "memo_generation_error": None if status != "FAILED" else "Memo quality audit failed."},
        db_path=db_path,
    )
    return audit


def _candidate_from_evidence(
    evidence: dict[str, Any], document: dict[str, Any] | None, package: dict[str, Any], attempt_id: str
) -> MemoEvidenceCandidate:
    quote = _clean_text(evidence.get("source_text") or "")
    family = _claim_family(evidence)
    kind = "RISK" if _is_risk_evidence(evidence, quote) else "SUPPORTING"
    sentence = _best_complete_sentence(evidence, quote, risk=kind == "RISK")
    source_date = None if not document else document.get("publication_date") or document.get("filing_date") or document.get("document_date")
    heading = _meaningful_heading(evidence.get("section_heading"))
    value = _float_or_none(evidence.get("value"))
    candidate = MemoEvidenceCandidate(
        candidate_id=_candidate_id(attempt_id, str(evidence.get("evidence_id") or "")),
        evidence_id=str(evidence.get("evidence_id") or ""),
        version_document_id=str(evidence.get("version_document_id") or ""),
        claim_family=_risk_family(quote) if kind == "RISK" else family,
        claim_text=sentence,
        supporting_quote=sentence,
        metric_name=str(evidence.get("metric_name") or "") or None,
        numeric_value=value,
        unit=str(evidence.get("unit") or "") or None,
        currency=str(evidence.get("currency") or "") or None,
        reporting_period=str(evidence.get("period") or "") or None,
        filing_or_publication_date=str(source_date or "") or None,
        source_type=str((document or {}).get("collection_method") or (document or {}).get("source_name") or "Locked package"),
        form_type=str((document or {}).get("form_type") or "") or None,
        section_heading=heading,
        page_number=_int_or_none(evidence.get("page_number")),
        source_priority=_source_priority(document or {}),
        recency_score=_date_score(source_date),
        materiality_score=_materiality_score(family, quote, kind),
        completeness_score=1.0 if sentence and _is_complete_sentence(sentence) else 0.0,
        decision_relevance_score=float(FAMILY_PRIORITY.get(family, 0)),
        candidate_kind=kind,
        citation=_citation(evidence, document or {}),
    )
    candidate.rejection_reasons.extend(_candidate_rejections(candidate, evidence, document, package))
    return candidate


def _candidate_rejections(
    candidate: MemoEvidenceCandidate,
    evidence: dict[str, Any],
    document: dict[str, Any] | None,
    package: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    quote = candidate.supporting_quote
    lowered = quote.lower()
    if evidence.get("verification_status") not in SUPPORTED:
        reasons.append("unsupported_verification_status")
    if not document:
        reasons.append("source_document_not_in_locked_package")
    if not quote or not _is_complete_sentence(quote):
        reasons.append("incomplete_or_truncated_sentence")
    if quote and quote[0].isalpha() and not quote[0].isupper():
        reasons.append("incomplete_or_truncated_sentence")
    if _has_ellipsis(quote):
        reasons.append("ellipsis_or_truncated_text")
    if any(term in lowered for term in BOILERPLATE_TERMS):
        reasons.append("filing_boilerplate")
    if any(term in lowered for term in IMMATERIAL_TERMS):
        reasons.append("immaterial_geography_tax_or_accounting_policy")
    if candidate.claim_family == "revenue_growth" and "industry" in lowered and not any(
        issuer in lowered
        for issuer in (str(package.get("ticker") or "").lower(), str(package.get("company_name") or "").lower())
        if issuer
    ):
        reasons.append("industry_market_size_not_issuer_performance")
    if "industry" in lowered and any(term in lowered for term in ("north america", "western europe", "geographic")):
        reasons.append("immaterial_geography_tax_or_accounting_policy")
    if any(term in lowered for term in (
        "we calculate", "weighted-average number", "accounting standards codification",
        "expected volatility is based on historical volatility",
    )):
        reasons.append("routine_accounting_definition")
    if "predecessor" in lowered or "discontinued operation" in lowered:
        reasons.append("historical_reporting_regime")
    if not candidate.filing_or_publication_date:
        reasons.append("missing_filing_or_publication_date")
    if not candidate.citation:
        reasons.append("missing_meaningful_source_locator")
    if candidate.materiality_score <= 0:
        reasons.append("not_decision_relevant")
    if candidate.candidate_kind == "SUPPORTING" and candidate.claim_family in FINANCIAL_FAMILIES:
        if candidate.numeric_value is None:
            reasons.append("missing_validated_numeric_value")
        if not candidate.metric_name:
            reasons.append("unclear_metric_identity")
        if not candidate.unit:
            reasons.append("missing_unit")
        if not candidate.reporting_period:
            reasons.append("missing_reporting_period")
        if _amount_family(candidate.claim_family) and not candidate.currency:
            reasons.append("missing_currency")
        if candidate.numeric_value is not None and not _number_supported(candidate.claim_text, candidate.numeric_value):
            reasons.append("unsupported_numeric_value")
        if candidate.unit == "%" and candidate.currency:
            reasons.append("percentage_interpreted_as_currency")
    if candidate.candidate_kind == "SUPPORTING" and candidate.numeric_value is not None:
        metric_tokens = [token for token in re.findall(r"[a-z]+", str(candidate.metric_name or "").lower()) if len(token) >= 4]
        if metric_tokens and not any(token in lowered for token in metric_tokens):
            reasons.append("metric_identity_not_supported_by_quote")
    if candidate.candidate_kind == "RISK" and not _risk_family(quote):
        reasons.append("unclear_risk_family")
    if package.get("ticker") and document and str(document.get("ticker") or package.get("ticker")).upper() != str(package.get("ticker")).upper():
        reasons.append("issuer_identity_mismatch")
    return list(dict.fromkeys(reasons))


def _apply_duplicate_and_recency_rules(candidates: list[MemoEvidenceCandidate]) -> None:
    seen: dict[tuple[Any, ...], MemoEvidenceCandidate] = {}
    for candidate in sorted(candidates, key=_candidate_sort_key, reverse=True):
        key = (
            candidate.candidate_kind,
            candidate.claim_family,
            candidate.numeric_value,
            candidate.unit,
            candidate.currency,
            candidate.reporting_period,
            _normalize_claim(candidate.supporting_quote),
        )
        if key in seen:
            candidate.rejection_reasons.append("duplicate_or_near_duplicate_fact")
        else:
            seen[key] = candidate
    groups: dict[tuple[str, str, str], list[MemoEvidenceCandidate]] = {}
    for candidate in candidates:
        if candidate.candidate_kind != "SUPPORTING" or candidate.rejection_reasons:
            continue
        cadence = "ANNUAL" if str(candidate.form_type or "").upper().startswith(("10-K", "20-F")) or "FY" in str(candidate.reporting_period or "").upper() else "QUARTERLY"
        groups.setdefault((candidate.claim_family, str(candidate.metric_name or "").lower(), cadence), []).append(candidate)
    for rows in groups.values():
        rows.sort(key=_candidate_sort_key, reverse=True)
        for stale in rows[1:]:
            stale.rejection_reasons.append("newer_equivalent_evidence_available")


def _validated_memo(
    draft: InvestmentMemoDraft,
    supporting: list[MemoEvidenceCandidate],
    risks: list[MemoEvidenceCandidate],
    context: dict[str, Any],
    run: dict[str, Any],
    analysis_run_id: str,
    *,
    db_path: Path | str,
) -> dict[str, Any]:
    support_map = {item.candidate_id: item for item in supporting}
    risk_map = {item.candidate_id: item for item in risks}
    facts = _validate_draft_items(draft.supporting_facts, support_map, "supporting")
    risk_rows = _validate_draft_items(draft.risks, risk_map, "risk")
    limitations = _limitations(run, supporting)
    missing = [item for item in draft.missing_information[:3] if _supported_limitation(item, limitations)] or limitations[:3]
    for text, limit, label in ((draft.investment_view, 120, "investment view"), (draft.conclusion, 80, "conclusion")):
        if len(_clean_text(text).split()) > limit or not _is_complete_sentence(text) or _has_ellipsis(text):
            raise MemoGenerationError(f"The {label} failed sentence or length validation.")
    selected_quotes = " ".join(item.supporting_quote for item in [*supporting, *risks])
    if not _numeric_tokens(draft.investment_view) <= _numeric_tokens(selected_quotes):
        raise MemoGenerationError("The investment view introduced an unsupported number.")
    if not _numeric_tokens(draft.conclusion) <= _numeric_tokens(selected_quotes):
        raise MemoGenerationError("The conclusion introduced an unsupported number.")
    if _normalize_claim(draft.conclusion) in {_normalize_claim(item) for item in missing}:
        raise MemoGenerationError("The conclusion merely repeats missing information.")
    decision = database.get_recommendation_decision(analysis_run_id, db_path=db_path) or {}
    version, package = context["version"], context["package"]
    return {
        "mode": config.REPORT_MODE,
        "company_name": version.get("company_name") or package.get("company_name") or version.get("ticker") or "Company",
        "ticker": version.get("ticker") or package.get("ticker") or "",
        "research_cutoff": _readable_date(run.get("research_cutoff") or version.get("research_cutoff_date")),
        "recommendation": _recommendation_label(decision, run),
        "confidence": str(decision.get("confidence") or run.get("confidence") or "Not available").title(),
        "investment_view": _clean_text(draft.investment_view),
        "investment_view_citations": list(dict.fromkeys(item.citation for item in supporting[:3] if item.citation)),
        "supporting_facts": facts[:5],
        "risks": risk_rows[:4],
        "missing_information": [_clean_text(item) for item in missing],
        "conclusion": _clean_text(draft.conclusion),
    }


def _validate_draft_items(
    rows: list[MemoDraftItem], candidate_map: dict[str, MemoEvidenceCandidate], label: str
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        candidate = candidate_map.get(row.candidate_id)
        if not candidate:
            raise MemoGenerationError(f"OpenAI returned an unknown {label} candidate ID.")
        claim = _clean_text(row.concise_claim)
        if row.candidate_id in seen:
            raise MemoGenerationError(f"OpenAI returned a duplicate {label} candidate ID.")
        if len(claim.split()) > 45 or not _is_complete_sentence(claim) or _has_ellipsis(claim):
            raise MemoGenerationError(f"A {label} claim failed complete-sentence validation.")
        numeric_ok = _claim_numbers_supported(claim, candidate)
        if not numeric_ok:
            raise MemoGenerationError(f"A {label} claim introduced an unsupported number.")
        if not _claim_semantically_supported(claim, candidate.supporting_quote):
            raise MemoGenerationError(f"A {label} claim introduced an unsupported interpretation.")
        seen.add(row.candidate_id)
        output.append(
            {
                "candidate_id": candidate.candidate_id,
                "claim_family": candidate.claim_family,
                "claim": claim,
                "citation": candidate.citation,
                "numeric_validation": numeric_ok,
                "period_validation": not candidate.reporting_period or _period_supported(claim, candidate),
                "unit_validation": not candidate.unit or _unit_supported(claim, candidate),
                "currency_validation": not candidate.currency or _currency_supported(claim, candidate),
            }
        )
    return output


def _memo_system_prompt() -> str:
    return (
        "Write a concise one-page institutional investment memo using only the supplied validated candidates. "
        "Return candidate IDs exactly as supplied for every supporting fact and risk. Do not create citations, IDs, numbers, dates, periods, units, companies, or sources. "
        "Use complete sentences with an explicit subject and finite verb, without ellipses. Keep each fact or risk at 45 words or fewer, the investment view at 120 words or fewer, and the conclusion at 80 words or fewer. "
        "Keep each concise claim lexically close to its supporting quote. Do not add qualitative interpretations such as market confidence, financial strength, significance, or shareholder dilution unless those ideas appear in the quote. "
        "The investment view must cover operating direction, financial strength or weakness, the main uncertainty, and why the recommendation remains provisional. "
        "The conclusion must summarize direction, a positive, a risk, missing valuation or evidence, and required analyst action."
    )


def _deterministic_test_draft(
    supporting: list[MemoEvidenceCandidate], risks: list[MemoEvidenceCandidate], limitations: list[str], run: dict[str, Any]
) -> InvestmentMemoDraft:
    family_text = ", ".join(dict.fromkeys(item.claim_family.replace("_", " ") for item in supporting[:3])) or "limited operating evidence"
    risk_text = risks[0].claim_family.replace("_", " ") if risks else "incomplete risk evidence"
    return InvestmentMemoDraft(
        investment_view=(
            f"Recent verified evidence addresses {family_text}. The locked package provides a bounded view of financial strength and operating direction. "
            f"The primary uncertainty is {risk_text}. The recommendation remains provisional until the missing valuation or evidence is reviewed."
        ),
        supporting_facts=[MemoDraftItem(candidate_id=item.candidate_id, concise_claim=item.claim_text) for item in supporting],
        risks=[MemoDraftItem(candidate_id=item.candidate_id, concise_claim=item.claim_text) for item in risks],
        missing_information=limitations[:3],
        conclusion=(
            "The available evidence indicates the current operating direction, with verified financial facts as the principal positive and the identified risks as the main uncertainty. "
            "Missing valuation or evidence prevents a final rating, so analyst review is required."
        ),
    )


def _document_lookup(version_id: str, *, db_path: Path | str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for version_doc in database.list_package_version_documents(version_id, db_path=db_path):
        original = database.get_document_by_document_id(version_doc.get("original_document_id"), db_path=db_path) or {}
        rows[version_doc["document_id"]] = {**version_doc, **original}
    return rows


def _best_complete_sentence(evidence: dict[str, Any], quote: str, *, risk: bool) -> str:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", quote) if item.strip()]
    value = _float_or_none(evidence.get("value"))
    metric = str(evidence.get("metric_name") or "").lower()
    for sentence in sentences:
        lowered = sentence.lower()
        if risk and any(term in lowered for terms in RISK_FAMILIES.values() for term in terms) and _is_complete_sentence(sentence):
            return _clean_text(sentence)
        if not risk and _is_complete_sentence(sentence) and (value is None or _number_supported(sentence, value)) and (not metric or any(token in lowered for token in metric.split("_") if len(token) > 3)):
            return _clean_text(sentence)
    claim = _clean_text(evidence.get("claim_text") or "")
    return claim if _is_complete_sentence(claim) else ""


def _is_complete_sentence(value: Any) -> bool:
    text = _clean_text(value)
    if len(text.split()) < 4 or text[-1:] not in ".!?" or _has_ellipsis(text):
        return False
    stem = text[:-1].rstrip().lower()
    if re.search(r"\b(and|or|but|with|including|of|to|for|from|as)$", stem):
        return False
    finite_verb = re.search(
        r"\b(?:is|are|was|were|has|have|had|do|does|did|may|might|can|could|will|would|should|must|"
        r"[a-z]+ed|[a-z]+ates|[a-z]+izes|[a-z]+ifies|addresses|contains|depends|faces|focuses|holds|"
        r"indicates|involves|limits|operates|prevents|provides|reflects|remains|requires|supports|shows)\b",
        stem,
    )
    return bool(finite_verb)


def _claim_family(evidence: dict[str, Any]) -> str:
    text = f"{evidence.get('metric_name') or ''} {evidence.get('evidence_type') or ''} {evidence.get('source_text') or ''}".lower()
    if any(term in text for term in ("revenue", "sales", "organic growth")):
        return "revenue_growth"
    if any(term in text for term in ("ebitda", "operating income", "margin", "profitability", "earnings")):
        return "profitability"
    if any(term in text for term in ("operating cash flow", "free cash flow", "cash provided by operating")):
        return "cash_flow"
    if any(term in text for term in ("debt", "leverage", "liquidity", "cash balance", "refinancing")):
        return "debt_liquidity"
    if any(term in text for term in ("acquisition", "divestiture", "merger", "strategic")):
        return "strategy"
    if any(term in text for term in ("guidance", "outlook", "expects")):
        return "guidance"
    if any(term in text for term in ("valuation", "price target", "reference price")):
        return "valuation"
    if any(term in text for term in ("dividend", "buyback", "capital allocation")):
        return "capital_allocation"
    return "operating_driver"


def _is_risk_evidence(evidence: dict[str, Any], quote: str) -> bool:
    heading = str(evidence.get("section_heading") or "").lower()
    evidence_type = str(evidence.get("evidence_type") or "").upper()
    return evidence_type == "RISK" or "risk factor" in heading or any(term in quote.lower() for terms in RISK_FAMILIES.values() for term in terms) and any(term in quote.lower() for term in ("risk", "may", "could", "adverse", "uncertain"))


def _risk_family(text: str) -> str:
    lowered = text.lower()
    return next((family for family, terms in RISK_FAMILIES.items() if any(term in lowered for term in terms)), "")


def _materiality_score(family: str, quote: str, kind: str) -> float:
    lowered = quote.lower()
    if any(term in lowered for term in IMMATERIAL_TERMS):
        return 0.0
    if kind == "RISK":
        return 8.0 if _risk_family(quote) else 0.0
    return float(FAMILY_PRIORITY.get(family, 0))


def _source_priority(document: dict[str, Any]) -> float:
    form = str(document.get("form_type") or "").upper()
    title = str(document.get("title") or "").lower()
    if form.startswith("10-Q"):
        return 10.0
    if form.startswith("10-K"):
        return 9.0
    if "earnings" in title and ("release" in title or "presentation" in title):
        return 8.0
    if document.get("collection_method") == "INVESTOR_RELATIONS":
        return 7.0
    if document.get("is_public"):
        return 6.0
    return 5.0


def _candidate_sort_key(candidate: MemoEvidenceCandidate) -> tuple[float, float, float, float]:
    return (
        candidate.decision_relevance_score,
        candidate.recency_score,
        candidate.source_priority,
        candidate.completeness_score,
    )


def _citation(evidence: dict[str, Any], document: dict[str, Any]) -> str:
    if not document:
        return ""
    form = str(document.get("form_type") or "").replace("/A", " amendment")
    title = str(document.get("title") or document.get("document_title") or "Official company material")
    ticker = str(document.get("ticker") or "").strip()
    source = f"{ticker} {form}".strip() if form else title
    source_date = document.get("publication_date") or document.get("filing_date") or document.get("document_date")
    pieces = [source]
    if source_date:
        pieces.append(f"{'filed' if form else 'dated'} {_readable_date(source_date)}")
    heading = _meaningful_heading(evidence.get("section_heading"))
    if heading:
        pieces.append(heading)
    elif evidence.get("page_number"):
        pieces.append(f"page {evidence['page_number']}")
    return f"[From: {', '.join(pieces)}]"


def _meaningful_heading(value: Any) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9&' ]+", " ", _clean_text(value)).strip()
    normalized = re.sub(r"\s+", " ", cleaned).upper()
    if not cleaned or normalized in GENERIC_HEADINGS or re.fullmatch(r"PART\s+[IVX0-9]+", normalized):
        return None
    return cleaned


def _generic_heading_from_citation(value: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9 ]+", " ", value).upper()
    return any(re.search(rf"\b{re.escape(heading)}\b", normalized) for heading in GENERIC_HEADINGS)


def _limitations(run: dict[str, Any], supporting: list[MemoEvidenceCandidate]) -> list[str]:
    rows: list[str] = []
    if run.get("reference_price") is None:
        rows.append("The locked package does not contain a current reference price or sufficient valuation evidence.")
    available = {item.claim_family for item in supporting}
    for family, label in (("cash_flow", "current operating cash-flow evidence"), ("debt_liquidity", "current debt or liquidity evidence"), ("guidance", "current management guidance")):
        if family not in available:
            rows.append(f"The selected evidence does not provide complete {label}.")
    return rows[:3] or ["The memo is limited to evidence available in the locked research package as of the cutoff date."]


def _recommendation(analysis_run_id: str, run: dict[str, Any], *, db_path: Path | str) -> str:
    return _recommendation_label(database.get_recommendation_decision(analysis_run_id, db_path=db_path) or {}, run)


def _recommendation_label(decision: dict[str, Any], run: dict[str, Any]) -> str:
    value = str(decision.get("effective_rating") or run.get("preliminary_recommendation") or "ANALYST_REVIEW_REQUIRED").upper()
    return {
        "BUY": "Buy", "HOLD": "Hold", "SELL": "Sell",
        "ANALYST_REVIEW_REQUIRED": "Analyst Review Required",
        "INSUFFICIENT_EVIDENCE": "Insufficient Evidence",
        "NEEDS_ANALYST_REVIEW": "Analyst Review Required",
    }.get(value, "Analyst Review Required")


def _supported_limitation(value: str, allowed: list[str]) -> bool:
    normalized = set(_normalize_claim(value).split())
    return any(len(normalized & set(_normalize_claim(item).split())) >= max(3, min(len(normalized), 5)) for item in allowed)


def _claim_numbers_supported(claim: str, candidate: MemoEvidenceCandidate) -> bool:
    claim_numbers = _numeric_tokens(claim)
    allowed = _numeric_tokens(candidate.supporting_quote)
    if candidate.numeric_value is not None:
        allowed.add(f"{candidate.numeric_value:g}")
    allowed.update(_numeric_tokens(candidate.reporting_period or ""))
    return claim_numbers <= allowed


def _claim_semantically_supported(claim: str, supporting_quote: str) -> bool:
    lowered_claim = _clean_text(claim).lower()
    lowered_quote = _clean_text(supporting_quote).lower()
    if any(term in lowered_claim and term not in lowered_quote for term in UNSUPPORTED_INFERENCE_TERMS):
        return False
    stopwords = {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is", "its",
        "of", "on", "or", "that", "the", "their", "to", "was", "were", "with", "qxo",
        "company", "inc", "approximately", "about", "reported", "held", "had", "total",
    }

    def stems(value: str) -> set[str]:
        output: set[str] = set()
        for token in re.findall(r"[a-z]+", value):
            if token in stopwords or len(token) <= 2:
                continue
            output.add(re.sub(r"(?:ing|ed|es|s)$", "", token))
        return output

    unsupported = stems(lowered_claim) - stems(lowered_quote)
    return len(unsupported) <= 5


def _period_supported(claim: str, candidate: MemoEvidenceCandidate) -> bool:
    period = str(candidate.reporting_period or "")
    return not _numeric_tokens(period) or bool(_numeric_tokens(period) & _numeric_tokens(claim))


def _unit_supported(claim: str, candidate: MemoEvidenceCandidate) -> bool:
    unit = str(candidate.unit or "").lower()
    aliases = {unit, "%" if unit == "percent" else unit, "million" if unit in {"mm", "m"} else unit, "billion" if unit in {"bn", "b"} else unit}
    return any(alias and alias in claim.lower() for alias in aliases)


def _currency_supported(claim: str, candidate: MemoEvidenceCandidate) -> bool:
    currency = str(candidate.currency or "").lower()
    return currency in claim.lower() or (currency == "usd" and "$" in claim)


def _number_supported(text: str, value: float) -> bool:
    normalized = text.replace(",", "")
    token = f"{value:g}"
    return bool(re.search(rf"(?<!\d){re.escape(token)}(?:0+)?(?!\d)", normalized))


def _numeric_tokens(value: Any) -> set[str]:
    return {match.lstrip("0") or "0" for match in re.findall(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))}


def _amount_family(family: str) -> bool:
    return family in {"revenue_growth", "profitability", "cash_flow", "debt_liquidity", "valuation", "capital_allocation"}


def _candidate_id(attempt_id: str, evidence_id: str) -> str:
    digest = hashlib.sha256(f"{attempt_id}|{evidence_id}".encode("utf-8")).hexdigest()[:18].upper()
    return f"MEC-{digest}"


def _date_score(value: Any) -> float:
    try:
        parsed = date.fromisoformat(str(value)[:10])
        return float(parsed.toordinal())
    except (TypeError, ValueError):
        return 0.0


def _readable_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%B %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(value or "Not available")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_claim(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_text(value).lower()).strip()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _check(value: bool) -> str:
    return "PASSED" if value else "FAILED"
