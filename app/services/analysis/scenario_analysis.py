from __future__ import annotations

import json
import secrets
from statistics import median
from typing import Any

from app import config
from app.utils import database


SCENARIOS = ("Bear", "Base", "Bull")


def _scenario_id() -> str:
    return f"SCN-{secrets.token_hex(8).upper()}"


def _price_metric(metrics: list[dict[str, Any]], metric_code: str) -> list[dict[str, Any]]:
    return [metric for metric in metrics if metric.get("metric_code") == metric_code and metric.get("value") is not None]


def _evidence_ids(metrics: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for metric in metrics:
        ids.extend(json.loads(metric.get("source_evidence_ids_json") or "[]"))
    return sorted(set(ids))


def create_scenarios(
    analysis_run_id: str,
    *,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    db_target = db_path or config.DATABASE_PATH
    metrics = database.list_analysis_metrics(analysis_run_id, db_path=db_target)
    price_targets = _price_metric(metrics, "PRICE_TARGET")
    reference_prices = _price_metric(metrics, "REFERENCE_PRICE")
    reference_price = reference_prices[0]["value"] if reference_prices else None
    targets = sorted(float(metric["value"]) for metric in price_targets)
    generated: list[dict[str, Any]] = []
    for scenario_name in SCENARIOS:
        implied_value = None
        warnings: list[str] = []
        evidence_ids = _evidence_ids(price_targets + reference_prices)
        assumption_source = "PACKAGE_REPORTED"
        if not targets:
            warnings.append("No package-contained valuation target or multiple was available; scenario valuation abstained.")
        elif scenario_name == "Bear":
            implied_value = targets[0]
        elif scenario_name == "Base":
            implied_value = float(median(targets))
        elif scenario_name == "Bull":
            implied_value = targets[-1]
        if reference_price is None:
            warnings.append("No package-contained reference price was available; upside/downside abstained.")
        upside = ((implied_value - reference_price) / reference_price) if implied_value is not None and reference_price else None
        assumptions = {
            "source": assumption_source if implied_value is not None else "SYSTEM_ABSTAINED",
            "valuation_basis": "price target evidence" if price_targets else None,
            "probability_policy": "probabilities absent until analyst entry",
        }
        scenario = {
            "scenario_id": _scenario_id(),
            "analysis_run_id": analysis_run_id,
            "scenario_name": scenario_name,
            "scenario_assumptions_json": json.dumps(assumptions, sort_keys=True),
            "revenue_assumption": _metric_summary(metrics, "REVENUE"),
            "margin_assumption": _metric_summary(metrics, "REPORTED_MARGIN"),
            "earnings_assumption": _metric_summary(metrics, "EPS"),
            "multiple_assumption": _metric_summary(metrics, "VALUATION_MULTIPLE"),
            "implied_value": implied_value,
            "reference_price": reference_price,
            "upside_downside": upside,
            "probability": None,
            "evidence_ids_json": json.dumps(evidence_ids, sort_keys=True),
            "analyst_overrides_json": json.dumps({}, sort_keys=True),
            "warnings_json": json.dumps(warnings, sort_keys=True),
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
        }
        database.create_analysis_scenario(scenario, db_path=db_target)
        generated.append(scenario)
    return generated


def _metric_summary(metrics: list[dict[str, Any]], code: str) -> str | None:
    candidates = [metric for metric in metrics if metric.get("metric_code") == code and metric.get("value") is not None]
    if not candidates:
        return None
    metric = candidates[-1]
    unit = metric.get("unit") or ""
    currency = metric.get("currency") or ""
    period = metric.get("period") or ""
    return f"{metric['value']} {currency} {unit} {period}".strip()


def set_scenario_probabilities(
    analysis_run_id: str,
    probabilities: dict[str, float],
    *,
    rationale: str,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    if not rationale.strip():
        raise ValueError("Scenario probability changes require a rationale.")
    total = round(sum(probabilities.values()), 6)
    if total != 1:
        raise ValueError("Scenario probabilities must total 100%.")
    db_target = db_path or config.DATABASE_PATH
    scenarios = database.list_analysis_scenarios(analysis_run_id, db_path=db_target)
    updated: list[dict[str, Any]] = []
    for scenario in scenarios:
        name = scenario["scenario_name"]
        if name not in probabilities:
            continue
        overrides = json.loads(scenario.get("analyst_overrides_json") or "{}")
        overrides["probability_rationale"] = rationale
        updated_row = database.update_analysis_scenario(
            scenario["scenario_id"],
            {"probability": probabilities[name], "analyst_overrides_json": json.dumps(overrides, sort_keys=True)},
            db_path=db_target,
        )
        if updated_row:
            updated.append(updated_row)
    return updated
