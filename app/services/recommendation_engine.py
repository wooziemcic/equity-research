from __future__ import annotations

import json
import secrets
from typing import Any

from app import config
from app.utils import database


MATERIAL_SECTIONS = {
    "BUSINESS": {"COMPANY_DESCRIPTION", "BUSINESS_SEGMENT", "MANAGEMENT_CLAIM"},
    "FINANCIALS": {"REPORTED_REVENUE", "REPORTED_GROWTH", "REPORTED_MARGIN", "REPORTED_EPS", "REPORTED_CASH_FLOW"},
    "BALANCE_SHEET": {"REPORTED_DEBT", "REPORTED_LIQUIDITY"},
    "VALUATION": {"PRICE_TARGET", "VALUATION_MULTIPLE", "ANALYST_ESTIMATE"},
    "CATALYSTS": {"CATALYST", "CAPITAL_ALLOCATION", "ACQUISITION", "DIVESTITURE"},
    "RISKS": {"RISK", "LEGAL_REGULATORY", "SHORT_SELLER_CLAIM", "ACTIVIST_CLAIM"},
}


def _item_id() -> str:
    return f"SCI-{secrets.token_hex(8).upper()}"


def _thesis_id() -> str:
    return f"THS-{secrets.token_hex(8).upper()}"


def _decision_id() -> str:
    return f"REC-{secrets.token_hex(8).upper()}"


def usable_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in evidence
        if item.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
        and item.get("analyst_status") != config.ANALYST_STATUS_REJECTED
    ]


def evidence_coverage(evidence: list[dict[str, Any]]) -> float:
    usable = usable_evidence(evidence)
    if not MATERIAL_SECTIONS:
        return 0.0
    covered = 0
    for evidence_types in MATERIAL_SECTIONS.values():
        if any(item.get("evidence_type") in evidence_types for item in usable):
            covered += 1
    return covered / len(MATERIAL_SECTIONS)


def package_coverage(version: dict[str, Any]) -> float:
    try:
        snapshot = json.loads(version.get("checklist_snapshot_json") or "[]")
    except json.JSONDecodeError:
        return 0.0
    if not snapshot:
        return 0.0
    material = [item for item in snapshot if str(item.get("requirement_level") or "").strip().lower() in {"required", "recommended"}]
    if not material:
        return 0.0
    available = [item for item in material if str(item.get("effective_status") or "").strip().upper() in {"AVAILABLE", "NOT_APPLICABLE"}]
    return len(available) / len(material)


def recommendation_confidence(
    *,
    coverage: float,
    conflicts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    reference_price: float | None,
) -> str:
    usable = usable_evidence(evidence)
    if not usable or coverage < 0.35:
        return config.CONFIDENCE_INSUFFICIENT
    if conflicts and len(conflicts) > config.MAX_UNRESOLVED_CONFLICTS:
        return config.CONFIDENCE_LOW
    if reference_price is None:
        return config.CONFIDENCE_LOW
    if any(str(item.get("confidence", "")).lower() in {"needs review", "low"} for item in usable):
        return config.CONFIDENCE_LOW
    if coverage >= 0.75 and all(item.get("verification_status") == config.VERIFICATION_SUPPORTS for item in usable):
        return config.CONFIDENCE_HIGH
    return config.CONFIDENCE_MEDIUM


def _evidence_for_types(evidence: list[dict[str, Any]], types: set[str]) -> list[dict[str, Any]]:
    return [item for item in usable_evidence(evidence) if item.get("evidence_type") in types]


def _score_from_values(metrics: list[dict[str, Any]], metric_codes: set[str], positive_high: bool = True) -> tuple[float, str]:
    values = [float(metric["value"]) for metric in metrics if metric.get("metric_code") in metric_codes and metric.get("value") is not None]
    if not values:
        return 0.0, "Missing package-supported evidence; score is not set to neutral."
    value = values[-1]
    if metric_codes & {"REVENUE_GROWTH_CALCULATED", "REPORTED_MARGIN", "FCF_CONVERSION"}:
        score = min(max((value * 100 + 5), 0), 10)
    elif metric_codes & {"DEBT_TO_EBITDA"}:
        score = max(0, min(10, 10 - value * 1.5))
    elif metric_codes & {"PRICE_TARGET"}:
        score = min(max(value / 10, 0), 10)
    else:
        score = min(max(value / 100000000, 2), 8)
    if not positive_high:
        score = 10 - score
    return round(score, 2), "Score derived from deterministic metric values linked to evidence."


def generate_scorecard(
    analysis_run_id: str,
    *,
    security_type: str,
    evidence: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    db_target = db_path or config.DATABASE_PATH
    profile = config.SCORECARD_PROFILES.get(security_type, config.SCORECARD_PROFILES["Other"])
    total_weight = round(sum(weight for _, weight in profile.values()), 6)
    if total_weight != 1:
        raise ValueError("Scorecard weights must total 100%.")
    created: list[dict[str, Any]] = []
    coverage = evidence_coverage(evidence)
    for pillar_code, (pillar_name, weight) in profile.items():
        relevant = _pillar_evidence(pillar_code, evidence)
        score, rationale = _pillar_score(pillar_code, relevant, metrics, conflicts, coverage)
        evidence_quality = _evidence_quality(relevant)
        item = {
            "item_id": _item_id(),
            "analysis_run_id": analysis_run_id,
            "pillar_code": pillar_code,
            "pillar_name": pillar_name,
            "score": score,
            "weight": weight,
            "weighted_score": round(score * weight, 4),
            "evidence_quality": evidence_quality,
            "evidence_ids_json": json.dumps([record["evidence_id"] for record in relevant], sort_keys=True),
            "rationale": rationale,
            "analyst_override_score": None,
            "analyst_override_rationale": None,
            "effective_score": score,
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
        }
        database.create_scorecard_item(item, db_path=db_target)
        created.append(item)
    return created


def _pillar_evidence(pillar_code: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {
        "BUSINESS_QUALITY": {"COMPANY_DESCRIPTION", "BUSINESS_SEGMENT", "COMPETITIVE_POSITION"},
        "REVENUE_EARNINGS_DIRECTION": {"REPORTED_REVENUE", "REPORTED_GROWTH", "REPORTED_EPS", "ANALYST_ESTIMATE", "MANAGEMENT_GUIDANCE"},
        "PROFITABILITY_CASH_FLOW": {"REPORTED_MARGIN", "REPORTED_CASH_FLOW"},
        "BALANCE_SHEET_LIQUIDITY": {"REPORTED_DEBT", "REPORTED_LIQUIDITY"},
        "VALUATION": {"PRICE_TARGET", "VALUATION_MULTIPLE"},
        "CATALYSTS": {"CATALYST", "CAPITAL_ALLOCATION", "ACQUISITION", "DIVESTITURE"},
        "DOWNSIDE_RISK": {"RISK", "LEGAL_REGULATORY", "SHORT_SELLER_CLAIM"},
        "EVIDENCE_QUALITY": set().union(*MATERIAL_SECTIONS.values()),
        "LIQUIDITY": {"REPORTED_LIQUIDITY"},
        "LEVERAGE": {"REPORTED_DEBT"},
        "INTEREST_COVERAGE": {"REPORTED_CASH_FLOW", "REPORTED_DEBT"},
        "COVENANT_RISK": {"COVENANT"},
        "RATING_DIRECTION": {"CREDIT_RATING"},
        "CREDIT_QUALITY": {"CREDIT_RATING", "REPORTED_DEBT", "REPORTED_LIQUIDITY"},
        "COUPON_MATURITY": {"CONVERTIBLE_TERM"},
        "CONVERSION_PREMIUM": {"CONVERTIBLE_TERM"},
        "BOND_FLOOR": {"CONVERTIBLE_TERM", "REPORTED_DEBT"},
    }
    return _evidence_for_types(evidence, mapping.get(pillar_code, set().union(*MATERIAL_SECTIONS.values())))


def _pillar_score(
    pillar_code: str,
    relevant: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    coverage: float,
) -> tuple[float, str]:
    if pillar_code == "EVIDENCE_QUALITY":
        conflict_penalty = min(len(conflicts), 5) * 0.7
        return max(0.0, round(coverage * 10 - conflict_penalty, 2)), "Evidence quality reflects verified coverage minus unresolved conflict penalties."
    if not relevant:
        return 0.0, "Missing package-supported evidence; score is not set to neutral."
    if pillar_code in {"REVENUE_EARNINGS_DIRECTION", "FINANCIAL_DIRECTION"}:
        return _score_from_values(metrics, {"REVENUE_GROWTH_CALCULATED", "EPS"})
    if pillar_code == "PROFITABILITY_CASH_FLOW":
        return _score_from_values(metrics, {"REPORTED_MARGIN", "FCF_CONVERSION"})
    if pillar_code in {"BALANCE_SHEET_LIQUIDITY", "LIQUIDITY", "LEVERAGE", "CREDIT_QUALITY"}:
        score, rationale = _score_from_values(metrics, {"DEBT_TO_EBITDA"}, positive_high=False)
        return (score or min(7.0, 4.0 + len(relevant))), rationale if score else "Score reflects available liquidity/debt evidence without leverage calculation."
    if pillar_code in {"DOWNSIDE_RISK", "RISKS", "COVENANT_RISK", "RECOVERY_DOWNSIDE"}:
        return max(0.0, 8.0 - len(relevant)), "Risk score is lower when more package-supported downside evidence is present."
    if pillar_code == "VALUATION":
        return min(8.0, 3.0 + len(relevant)), "Valuation score reflects availability of package-supported valuation evidence."
    return min(8.0, 4.0 + len(relevant)), "Score reflects count and quality of verified package-supported evidence."


def _evidence_quality(records: list[dict[str, Any]]) -> str:
    if not records:
        return config.CONFIDENCE_INSUFFICIENT
    if all(record.get("verification_status") == config.VERIFICATION_SUPPORTS for record in records):
        return config.CONFIDENCE_HIGH
    if any(record.get("verification_status") == config.VERIFICATION_PARTIALLY_SUPPORTS for record in records):
        return config.CONFIDENCE_MEDIUM
    return config.CONFIDENCE_LOW


def effective_score(scorecard_items: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("effective_score") or 0) * float(item.get("weight") or 0) for item in scorecard_items), 4)


def generate_thesis_items(
    analysis_run_id: str,
    *,
    evidence: list[dict[str, Any]],
    ai_review: Any | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    db_target = db_path or config.DATABASE_PATH
    if config.OPENAI_REQUIRED and ai_review is None:
        raise ValueError("OpenAI is required for thesis drafting.")
    if ai_review is not None:
        created: list[dict[str, Any]] = []
        for model_item in ai_review.thesis_items:
            records = [record for record in evidence if record["evidence_id"] in model_item.evidence_ids]
            citation_status = config.VERIFICATION_SUPPORTS if records else config.VERIFICATION_AMBIGUOUS
            item = {
                "thesis_item_id": _thesis_id(),
                "analysis_run_id": analysis_run_id,
                "item_type": model_item.item_type,
                "claim": model_item.claim if not model_item.abstain else "Unsupported claim was removed; the model abstained.",
                "evidence_ids_json": json.dumps(model_item.evidence_ids, sort_keys=True),
                "citation_status": citation_status,
                "confidence": model_item.confidence if records else config.CONFIDENCE_INSUFFICIENT,
                "analyst_status": config.ANALYST_STATUS_UNREVIEWED,
                "source_type": "OPENAI_LOCKED_CORPUS" if records else None,
                "created_at": database.utc_now_iso(),
                "updated_at": database.utc_now_iso(),
            }
            database.create_thesis_item(item, db_path=db_target)
            created.append(item)
        return created
    created: list[dict[str, Any]] = []
    definitions = [
        ("BULL_THESIS", {"REPORTED_GROWTH", "REPORTED_REVENUE", "CATALYST", "PRICE_TARGET"}),
        ("BEAR_THESIS", {"RISK", "REPORTED_DEBT", "SHORT_SELLER_CLAIM"}),
        ("CATALYST", {"CATALYST", "CAPITAL_ALLOCATION", "ACQUISITION"}),
        ("RISK", {"RISK", "LEGAL_REGULATORY", "SHORT_SELLER_CLAIM"}),
        ("RATING_UPGRADE_CONDITION", {"PRICE_TARGET", "MANAGEMENT_GUIDANCE"}),
        ("RATING_DOWNGRADE_CONDITION", {"RISK", "REPORTED_DEBT"}),
    ]
    for item_type, evidence_types in definitions:
        records = _evidence_for_types(evidence, evidence_types)[:3]
        if not records:
            claim = f"{item_type.replace('_', ' ').title()} lacks verified package-supported evidence."
            citation_status = config.VERIFICATION_AMBIGUOUS
            confidence = config.CONFIDENCE_INSUFFICIENT
        else:
            claim = " ".join(record["claim_text"] for record in records[:2])
            citation_status = config.VERIFICATION_SUPPORTS if all(record["verification_status"] == config.VERIFICATION_SUPPORTS for record in records) else config.VERIFICATION_PARTIALLY_SUPPORTS
            confidence = _evidence_quality(records)
        item = {
            "thesis_item_id": _thesis_id(),
            "analysis_run_id": analysis_run_id,
            "item_type": item_type,
            "claim": claim,
            "evidence_ids_json": json.dumps([record["evidence_id"] for record in records], sort_keys=True),
            "citation_status": citation_status,
            "confidence": confidence,
            "analyst_status": config.ANALYST_STATUS_UNREVIEWED,
            "source_type": "MIXED_LOCKED_CORPUS" if records else None,
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
        }
        database.create_thesis_item(item, db_path=db_target)
        created.append(item)
    return created


def generate_recommendation(
    analysis_run_id: str,
    *,
    evidence: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    scorecard_items: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    narrative: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    db_target = db_path or config.DATABASE_PATH
    model_narrative_supplied = narrative is not None
    if config.OPENAI_REQUIRED and narrative is None:
        raise ValueError("OpenAI is required for recommendation narrative generation.")
    coverage = evidence_coverage(evidence)
    score = effective_score(scorecard_items)
    reference_prices = [metric for metric in metrics if metric.get("metric_code") == "REFERENCE_PRICE" and metric.get("value") is not None]
    price_targets = [metric for metric in metrics if metric.get("metric_code") == "PRICE_TARGET" and metric.get("value") is not None]
    reference_price = float(reference_prices[-1]["value"]) if reference_prices else None
    target_price = float(price_targets[-1]["value"]) if price_targets else None
    upside = ((target_price - reference_price) / reference_price) if target_price is not None and reference_price else None
    unsupported = [item for item in evidence if item.get("verification_status") in {config.VERIFICATION_DOES_NOT_SUPPORT, config.VERIFICATION_SOURCE_MISSING, config.VERIFICATION_HASH_MISMATCH}]
    usable = usable_evidence(evidence)
    confidence = recommendation_confidence(coverage=coverage, conflicts=conflicts, evidence=evidence, reference_price=reference_price)
    abstention_reason = None
    if unsupported:
        rating = config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED
        abstention_reason = "Unsupported or missing citations require analyst review."
    elif not usable:
        rating = config.RECOMMENDATION_INSUFFICIENT_EVIDENCE
        abstention_reason = "No verified package-supported evidence is available for deterministic recommendation."
    elif coverage < config.MIN_EVIDENCE_COVERAGE:
        rating = config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED
        abstention_reason = "Evidence coverage is below the configured minimum."
    elif len(conflicts) > config.MAX_UNRESOLVED_CONFLICTS:
        rating = config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED
        abstention_reason = "Unresolved conflicts exceed the configured threshold."
    elif reference_price is None and not price_targets:
        rating = config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED if model_narrative_supplied else config.RECOMMENDATION_INSUFFICIENT_EVIDENCE
        abstention_reason = "No package-contained valuation evidence or reference price is available."
    elif reference_price is None:
        rating = config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED if model_narrative_supplied else config.RECOMMENDATION_INSUFFICIENT_EVIDENCE
        abstention_reason = "No package-contained reference price is available for upside/downside."
    elif score >= config.BUY_SCORE_THRESHOLD and upside is not None and upside >= config.MIN_BUY_UPSIDE:
        rating = config.RECOMMENDATION_BUY
    elif score <= config.SELL_SCORE_THRESHOLD or (upside is not None and upside <= config.MAX_SELL_DOWNSIDE):
        rating = config.RECOMMENDATION_SELL
    elif score >= config.HOLD_SCORE_THRESHOLD:
        rating = config.RECOMMENDATION_HOLD
    else:
        rating = config.RECOMMENDATION_INSUFFICIENT_EVIDENCE
        abstention_reason = "Score and valuation evidence are not strong enough for Buy/Hold/Sell."
    narrative = narrative or {}
    rationale = str(narrative.get("rationale") or _rationale(rating, score, coverage, upside, abstention_reason))
    if narrative.get("conflict_explanations"):
        rationale = f"{rationale} Conflict explanation: {narrative['conflict_explanations']}"
    decision = {
        "decision_id": _decision_id(),
        "analysis_run_id": analysis_run_id,
        "preliminary_rating": rating,
        "effective_rating": rating,
        "recommendation_rationale": rationale,
        "why_not_buy": str(narrative.get("why_not_buy") or _why_not(config.RECOMMENDATION_BUY, rating, score, coverage, upside)),
        "why_not_hold": str(narrative.get("why_not_hold") or _why_not(config.RECOMMENDATION_HOLD, rating, score, coverage, upside)),
        "why_not_sell": str(narrative.get("why_not_sell") or _why_not(config.RECOMMENDATION_SELL, rating, score, coverage, upside)),
        "confidence": confidence,
        "evidence_coverage": coverage,
        "abstention_reason": narrative.get("abstention_reason") or abstention_reason,
        "generated_at": database.utc_now_iso(),
        "analyst_decision": None,
        "analyst_identity": None,
        "analyst_decision_at": None,
        "pm_decision": None,
        "pm_identity": None,
        "pm_decision_at": None,
        "pm_note": None,
    }
    database.create_recommendation_decision(decision, db_path=db_target)
    return decision


def _rationale(rating: str, score: float, coverage: float, upside: float | None, abstention: str | None) -> str:
    if abstention:
        return f"{rating}: {abstention} Score={score:.2f}, evidence coverage={coverage:.0%}."
    upside_text = "unavailable" if upside is None else f"{upside:.0%}"
    return f"{rating}: score={score:.2f}, evidence coverage={coverage:.0%}, package-supported upside/downside={upside_text}."


def _why_not(candidate: str, selected: str, score: float, coverage: float, upside: float | None) -> str:
    if selected == candidate:
        return f"{candidate} was selected by the configured deterministic rules."
    if candidate == config.RECOMMENDATION_BUY:
        return "Buy was not selected because score, evidence coverage, or package-supported upside did not meet Buy thresholds."
    if candidate == config.RECOMMENDATION_HOLD:
        return "Hold was not selected because the rules selected a stronger rating or abstained due to evidence limitations."
    if candidate == config.RECOMMENDATION_SELL:
        return "Sell was not selected because downside/risk thresholds were not met."
    return "Not selected by the configured rules."


def override_scorecard_item(
    item_id: str,
    *,
    override_score: float,
    rationale: str,
    db_path: str | None = None,
) -> dict[str, Any]:
    if not rationale.strip():
        raise ValueError("Scorecard overrides require a rationale.")
    if not 0 <= override_score <= 10:
        raise ValueError("Override score must be between 0 and 10.")
    db_target = db_path or config.DATABASE_PATH
    row = database.update_scorecard_item(
        item_id,
        {
            "analyst_override_score": override_score,
            "analyst_override_rationale": rationale,
            "effective_score": override_score,
        },
        db_path=db_target,
    )
    if not row:
        raise ValueError("Scorecard item does not exist.")
    return row


def complete_analyst_review(
    analysis_run_id: str,
    *,
    decision: str,
    note: str,
    analyst_identity: str = "analyst",
    db_path: str | None = None,
) -> dict[str, Any]:
    if decision not in {
        config.RECOMMENDATION_BUY,
        config.RECOMMENDATION_HOLD,
        config.RECOMMENDATION_SELL,
        config.RECOMMENDATION_INSUFFICIENT_EVIDENCE,
        config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED,
    }:
        raise ValueError("Unsupported analyst recommendation.")
    if decision != config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED and not note.strip():
        raise ValueError("Analyst review requires a note.")
    db_target = db_path or config.DATABASE_PATH
    database.update_recommendation_decision(
        analysis_run_id,
        {
            "effective_rating": decision,
            "analyst_decision": decision,
            "analyst_identity": analyst_identity,
            "analyst_decision_at": database.utc_now_iso(),
        },
        db_path=db_target,
    )
    return database.update_analysis_run(
        analysis_run_id,
        {
            "status": config.ANALYSIS_STATUS_NEEDS_PM_APPROVAL,
            "analyst_adjusted_recommendation": decision,
            "analyst_notes": note,
        },
        db_path=db_target,
    ) or {}


def pm_decision(
    analysis_run_id: str,
    *,
    action: str,
    note: str,
    pm_identity: str = "pm",
    db_path: str | None = None,
) -> dict[str, Any]:
    db_target = db_path or config.DATABASE_PATH
    run = database.get_analysis_run(analysis_run_id, db_path=db_target)
    if not run:
        raise ValueError("Analysis run does not exist.")
    if run.get("status") != config.ANALYSIS_STATUS_NEEDS_PM_APPROVAL:
        raise ValueError("PM decision requires completed analyst review.")
    decision = database.get_recommendation_decision(analysis_run_id, db_path=db_target) or {}
    effective = decision.get("effective_rating") or run.get("analyst_adjusted_recommendation")
    if action == "APPROVE":
        status = config.ANALYSIS_STATUS_PM_APPROVED
        pm_rating = effective
    elif action == "REJECT":
        status = config.ANALYSIS_STATUS_PM_REJECTED
        pm_rating = None
    elif action == "RETURN_FOR_REVISION":
        status = config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW
        pm_rating = None
    else:
        raise ValueError("Unsupported PM action.")
    database.update_recommendation_decision(
        analysis_run_id,
        {
            "pm_decision": action,
            "pm_identity": pm_identity,
            "pm_decision_at": database.utc_now_iso(),
            "pm_note": note,
        },
        db_path=db_target,
    )
    return database.update_analysis_run(
        analysis_run_id,
        {"status": status, "pm_approved_recommendation": pm_rating, "pm_notes": note},
        db_path=db_target,
    ) or {}
