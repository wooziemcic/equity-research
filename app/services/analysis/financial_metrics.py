from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app import config
from app.utils import database


@dataclass(frozen=True)
class MetricResult:
    metric_code: str
    display_name: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    period: str | None
    scenario: str | None
    calculation_method: str
    formula_description: str
    source_evidence_ids: list[str]
    confidence: str
    verification_status: str
    warning: str | None = None


NULL_VALUE_STRINGS = {"", "null", "none", "n/a", "na", "--", "-"}
METRIC_NAME_ALIASES = {
    "reported_revenue": "revenue",
    "revenue": "revenue",
    "revenues": "revenue",
    "sales": "revenue",
    "net_sales": "revenue",
    "reported_growth": "growth",
    "growth": "growth",
    "reported_margin": "margin",
    "margin": "margin",
    "reported_eps": "eps",
    "eps": "eps",
    "earnings_per_share": "eps",
    "reported_cash_flow": "cash_flow",
    "cash_flow": "cash_flow",
    "free_cash_flow": "cash_flow",
    "fcf": "cash_flow",
    "reported_debt": "debt",
    "debt": "debt",
    "gross_debt": "debt",
    "total_debt": "debt",
    "reported_liquidity": "liquidity",
    "liquidity": "liquidity",
    "cash": "liquidity",
    "cash_and_cash_equivalents": "liquidity",
    "ebitda": "ebitda",
    "adjusted_ebitda": "adjusted_ebitda",
    "adj_ebitda": "adjusted_ebitda",
    "price_target": "price_target",
    "target_price": "price_target",
    "reference_price": "reference_price",
    "share_price": "share_price",
    "stock_price": "stock_price",
    "valuation_multiple": "valuation_multiple",
    "multiple": "valuation_multiple",
}
EVIDENCE_TYPE_METRIC_ALIASES = {
    "REPORTED_REVENUE": "revenue",
    "REPORTED_GROWTH": "growth",
    "REPORTED_MARGIN": "margin",
    "REPORTED_EPS": "eps",
    "REPORTED_CASH_FLOW": "cash_flow",
    "REPORTED_DEBT": "debt",
    "REPORTED_LIQUIDITY": "liquidity",
    "PRICE_TARGET": "price_target",
    "VALUATION_MULTIPLE": "valuation_multiple",
}


def _metric_id() -> str:
    return f"MET-{secrets.token_hex(8).upper()}"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = str(value).strip()
    if text.lower() in NULL_VALUE_STRINGS:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    text = text.replace("$", "").replace(",", "").replace("%", "")
    text = re.sub(r"(?i)\b(usd|eur|gbp|cad|aud|dollars?)\b", "", text)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        parsed = Decimal(match.group(0))
    except (InvalidOperation, ValueError):
        return None
    return -abs(parsed) if negative else parsed


def quantize(value: Decimal | None, places: str = "0.01") -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def scale_multiplier(unit: str | None) -> Decimal:
    normalized = (unit or "").strip().lower()
    if normalized in {"billion", "billions", "bn", "b"}:
        return Decimal("1000000000")
    if normalized in {"million", "millions", "mm", "m"}:
        return Decimal("1000000")
    if normalized in {"thousand", "thousands", "k"}:
        return Decimal("1000")
    return Decimal("1")


def _unit_from_value_text(value: Any) -> str | None:
    text = str(value or "").lower()
    if "%" in text or "percent" in text:
        return "%"
    if re.search(r"\b(billion|billions|bn)\b", text):
        return "billion"
    if re.search(r"\b(million|millions|mm)\b", text):
        return "million"
    if re.search(r"\b(thousand|thousands)\b", text):
        return "thousand"
    return None


def normalized_value(evidence: dict[str, Any]) -> Decimal | None:
    value = decimal_or_none(evidence.get("value"))
    if value is None:
        return None
    unit = evidence.get("unit") or _unit_from_value_text(evidence.get("value"))
    if str(unit or "").strip().lower() in {"%", "percent"}:
        return value / Decimal("100")
    return value * scale_multiplier(unit)


def confidence_from_evidence(records: list[dict[str, Any]]) -> str:
    if not records:
        return config.CONFIDENCE_INSUFFICIENT
    if any(str(record.get("confidence", "")).lower() in {"needs review", "low"} for record in records):
        return config.CONFIDENCE_LOW
    if any(str(record.get("extraction_method", "")).upper().find("OCR") >= 0 for record in records):
        return config.CONFIDENCE_LOW
    if any("CACHED_FORMULA" in str(record.get("source_text", "")).upper() for record in records):
        return config.CONFIDENCE_LOW
    if all(record.get("verification_status") == config.VERIFICATION_SUPPORTS for record in records):
        return config.CONFIDENCE_HIGH
    return config.CONFIDENCE_MEDIUM


def usable_evidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
        and record.get("analyst_status") != config.ANALYST_STATUS_REJECTED
    ]


def _normalize_metric_token(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return METRIC_NAME_ALIASES.get(normalized, normalized)


def _record_metric_key(record: dict[str, Any]) -> str:
    metric = _normalize_metric_token(record.get("metric_name"))
    if metric and metric != "numeric_fact":
        return metric
    evidence_type = str(record.get("evidence_type") or "").strip().upper()
    return EVIDENCE_TYPE_METRIC_ALIASES.get(evidence_type, metric)


def _first_by_metric(records: list[dict[str, Any]], metric_names: set[str], period: str | None = None) -> dict[str, Any] | None:
    normalized_names = {_normalize_metric_token(name) for name in metric_names}
    candidates = [
        record
        for record in records
        if _record_metric_key(record) in normalized_names
        and normalized_value(record) is not None
        and (period is None or (record.get("period") or "") == period)
    ]
    return candidates[0] if candidates else None


def _records_by_metric(records: list[dict[str, Any]], metric_names: set[str]) -> list[dict[str, Any]]:
    normalized_names = {_normalize_metric_token(name) for name in metric_names}
    return [
        record
        for record in records
        if _record_metric_key(record) in normalized_names and normalized_value(record) is not None
    ]


def metric_input_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for record in records:
        key = _record_metric_key(record)
        if not key:
            continue
        suffix = "_numeric" if normalized_value(record) is not None else "_non_numeric"
        summary[f"{key}{suffix}"] = summary.get(f"{key}{suffix}", 0) + 1
    return dict(sorted(summary.items()))


def diagnose_metric_skips(records: list[dict[str, Any]], metrics: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    calculated = {
        metric.get("metric_code")
        for metric in metrics or []
        if metric.get("metric_code") and metric.get("value") is not None
    }
    skipped: list[dict[str, str]] = []

    def add(metric_code: str, reason: str) -> None:
        if metric_code not in calculated:
            skipped.append({"metric_code": metric_code, "reason": reason})

    revenue_records = _records_by_metric(records, {"revenue"})
    if not revenue_records:
        add("REVENUE", "No usable numeric revenue evidence was discovered.")
    if len({record.get("period") for record in revenue_records if record.get("period")}) < 2:
        add("REVENUE_GROWTH_CALCULATED", "Fewer than two revenue periods with usable numeric values were discovered.")
    if not _records_by_metric(records, {"reference_price", "share_price", "stock_price"}):
        add("REFERENCE_PRICE", "No package-contained numeric reference price evidence was discovered.")
    if not _records_by_metric(records, {"price_target"}):
        add("PRICE_TARGET", "No package-contained numeric price target evidence was discovered.")

    periods = sorted({record.get("period") for record in records if record.get("period")})
    if not any(_first_by_metric(records, {"revenue"}, period) and _first_by_metric(records, {"cash_flow"}, period) for period in periods):
        add("FCF_CONVERSION", "No same-period revenue and cash-flow evidence pair was discovered.")
    if not any(_first_by_metric(records, {"debt"}, period) and _first_by_metric(records, {"liquidity"}, period) for period in periods):
        add("NET_DEBT", "No same-period debt and cash/liquidity evidence pair was discovered.")
    if not any(_first_by_metric(records, {"debt"}, period) and _first_by_metric(records, {"ebitda", "adjusted_ebitda"}, period) for period in periods):
        add("DEBT_TO_EBITDA", "No same-period debt and EBITDA evidence pair was discovered.")
    return skipped


def calculate_revenue_growth(current: dict[str, Any], prior: dict[str, Any]) -> MetricResult:
    current_value = normalized_value(current)
    prior_value = normalized_value(prior)
    warning = None
    value = None
    if not current_value or not prior_value:
        warning = "Revenue growth abstained because one revenue input is missing or zero."
    elif current.get("currency") != prior.get("currency"):
        warning = "Revenue growth abstained because currencies do not match."
    elif current.get("period") == prior.get("period"):
        warning = "Revenue growth abstained because both inputs use the same period."
    else:
        value = quantize((current_value - prior_value) / prior_value, "0.0001")
    return MetricResult(
        "REVENUE_GROWTH_CALCULATED",
        "Revenue Growth",
        value,
        "%",
        None,
        current.get("period"),
        None,
        "DETERMINISTIC",
        "(current revenue - prior revenue) / prior revenue",
        [current["evidence_id"], prior["evidence_id"]],
        confidence_from_evidence([current, prior]) if value is not None else config.CONFIDENCE_INSUFFICIENT,
        config.VERIFICATION_SUPPORTS if value is not None else config.VERIFICATION_AMBIGUOUS,
        warning,
    )


def calculate_margin(numerator: dict[str, Any], denominator: dict[str, Any], metric_code: str, display_name: str) -> MetricResult:
    numerator_value = normalized_value(numerator)
    denominator_value = normalized_value(denominator)
    warning = None
    value = None
    if not numerator_value or not denominator_value:
        warning = f"{display_name} abstained because an input is missing or zero."
    elif numerator.get("period") != denominator.get("period"):
        warning = f"{display_name} abstained because periods do not match."
    elif numerator.get("currency") != denominator.get("currency"):
        warning = f"{display_name} abstained because currencies do not match."
    else:
        value = quantize(numerator_value / denominator_value, "0.0001")
    return MetricResult(
        metric_code,
        display_name,
        value,
        "%",
        None,
        numerator.get("period"),
        None,
        "DETERMINISTIC",
        f"{display_name} = numerator / revenue",
        [numerator["evidence_id"], denominator["evidence_id"]],
        confidence_from_evidence([numerator, denominator]) if value is not None else config.CONFIDENCE_INSUFFICIENT,
        config.VERIFICATION_SUPPORTS if value is not None else config.VERIFICATION_AMBIGUOUS,
        warning,
    )


def calculate_net_debt(gross_debt: dict[str, Any], cash: dict[str, Any]) -> MetricResult:
    debt_value = normalized_value(gross_debt)
    cash_value = normalized_value(cash)
    warning = None
    value = None
    if debt_value is None or cash_value is None:
        warning = "Net debt abstained because debt or cash is missing."
    elif gross_debt.get("currency") != cash.get("currency"):
        warning = "Net debt abstained because currencies do not match."
    elif gross_debt.get("period") != cash.get("period"):
        warning = "Net debt abstained because periods do not match."
    else:
        value = quantize(debt_value - cash_value)
    return MetricResult(
        "NET_DEBT",
        "Net Debt",
        value,
        "absolute",
        gross_debt.get("currency") or cash.get("currency"),
        gross_debt.get("period"),
        None,
        "DETERMINISTIC",
        "gross debt - cash",
        [gross_debt["evidence_id"], cash["evidence_id"]],
        confidence_from_evidence([gross_debt, cash]) if value is not None else config.CONFIDENCE_INSUFFICIENT,
        config.VERIFICATION_SUPPORTS if value is not None else config.VERIFICATION_AMBIGUOUS,
        warning,
    )


def calculate_leverage(debt: dict[str, Any], ebitda: dict[str, Any]) -> MetricResult:
    debt_value = normalized_value(debt)
    ebitda_value = normalized_value(ebitda)
    warning = None
    value = None
    if not debt_value or not ebitda_value:
        warning = "Debt/EBITDA abstained because debt or EBITDA is missing or zero."
    elif debt.get("period") != ebitda.get("period"):
        warning = "Debt/EBITDA abstained because periods do not match."
    else:
        value = quantize(debt_value / ebitda_value, "0.01")
    return MetricResult(
        "DEBT_TO_EBITDA",
        "Debt / EBITDA",
        value,
        "x",
        None,
        debt.get("period"),
        None,
        "DETERMINISTIC",
        "gross debt / EBITDA",
        [debt["evidence_id"], ebitda["evidence_id"]],
        confidence_from_evidence([debt, ebitda]) if value is not None else config.CONFIDENCE_INSUFFICIENT,
        config.VERIFICATION_SUPPORTS if value is not None else config.VERIFICATION_AMBIGUOUS,
        warning,
    )


def calculate_guidance_midpoint(low_value: Decimal, high_value: Decimal, evidence_ids: list[str], period: str | None, currency: str | None) -> MetricResult:
    midpoint = quantize((low_value + high_value) / Decimal("2"))
    return MetricResult(
        "GUIDANCE_MIDPOINT",
        "Guidance Midpoint",
        midpoint,
        "absolute",
        currency,
        period,
        None,
        "DETERMINISTIC",
        "(low guidance + high guidance) / 2",
        evidence_ids,
        config.CONFIDENCE_MEDIUM,
        config.VERIFICATION_SUPPORTS,
        None,
    )


def calculate_metrics(
    evidence_records: list[dict[str, Any]],
    *,
    analysis_run_id: str,
    db_path: str | None = None,
) -> list[MetricResult]:
    db_target = db_path or config.DATABASE_PATH
    records = usable_evidence(evidence_records)
    metrics: list[MetricResult] = []
    by_metric = sorted(_records_by_metric(records, {"revenue"}), key=lambda item: item.get("period") or "")
    for record in by_metric:
        metrics.append(
            MetricResult(
                "REVENUE",
                "Revenue",
                normalized_value(record),
                "absolute",
                record.get("currency"),
                record.get("period"),
                record.get("scenario"),
                "SOURCE_EVIDENCE",
                "reported revenue from evidence",
                [record["evidence_id"]],
                confidence_from_evidence([record]),
                record.get("verification_status") or config.VERIFICATION_PENDING,
                None,
            )
        )
    if len(by_metric) >= 2:
        metrics.append(calculate_revenue_growth(by_metric[-1], by_metric[-2]))
    for metric_names, code, display in (
        ({"margin"}, "REPORTED_MARGIN", "Reported Margin"),
        ({"eps"}, "EPS", "EPS"),
        ({"cash_flow"}, "CASH_FLOW", "Cash Flow"),
        ({"liquidity"}, "LIQUIDITY", "Liquidity"),
        ({"debt"}, "GROSS_DEBT", "Gross Debt"),
        ({"ebitda", "adjusted_ebitda"}, "EBITDA", "EBITDA"),
        ({"price_target"}, "PRICE_TARGET", "Analyst Price Target"),
        ({"reference_price", "share_price", "stock_price"}, "REFERENCE_PRICE", "Reference Price"),
    ):
        for record in _records_by_metric(records, metric_names):
            metrics.append(
                MetricResult(
                    code,
                    display,
                    normalized_value(record),
                    "%" if record.get("unit") == "%" else "absolute",
                    record.get("currency"),
                    record.get("period"),
                    record.get("scenario"),
                    "SOURCE_EVIDENCE",
                    f"{display} from evidence; no arithmetic transformation other than unit scaling.",
                    [record["evidence_id"]],
                    confidence_from_evidence([record]),
                    record.get("verification_status") or config.VERIFICATION_PENDING,
                    "Input may be lower confidence." if record.get("analyst_status") == config.ANALYST_STATUS_NEEDS_REVIEW else None,
                )
            )
    for period in sorted({record.get("period") for record in records if record.get("period")}):
        revenue = _first_by_metric(records, {"revenue"}, period)
        cash_flow = _first_by_metric(records, {"cash_flow"}, period)
        if revenue and cash_flow:
            metrics.append(calculate_margin(cash_flow, revenue, "FCF_CONVERSION", "Free-Cash-Flow Conversion"))
        debt = _first_by_metric(records, {"debt"}, period)
        cash = _first_by_metric(records, {"liquidity"}, period)
        if debt and cash:
            metrics.append(calculate_net_debt(debt, cash))
        ebitda = _first_by_metric(records, {"ebitda", "adjusted_ebitda"}, period)
        if debt and ebitda:
            metrics.append(calculate_leverage(debt, ebitda))
    for metric in metrics:
        database.create_analysis_metric(metric_to_record(metric, analysis_run_id), db_path=db_target)
    return metrics


def metric_to_record(metric: MetricResult, analysis_run_id: str) -> dict[str, Any]:
    return {
        "metric_id": _metric_id(),
        "analysis_run_id": analysis_run_id,
        "metric_code": metric.metric_code,
        "display_name": metric.display_name,
        "value": float(metric.value) if metric.value is not None else None,
        "unit": metric.unit,
        "currency": metric.currency,
        "period": metric.period,
        "scenario": metric.scenario,
        "calculation_method": metric.calculation_method,
        "formula_description": metric.formula_description,
        "source_evidence_ids_json": json.dumps(metric.source_evidence_ids, sort_keys=True),
        "confidence": metric.confidence,
        "verification_status": metric.verification_status,
        "warning": metric.warning,
        "created_at": database.utc_now_iso(),
    }
