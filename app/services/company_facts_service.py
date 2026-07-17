from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import requests

from app import config
from app.utils import database


CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "Revenues"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "diluted_eps": ("EarningsPerShareDiluted",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditures": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "cash_and_cash_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "short_term_investments": ("ShortTermInvestments", "MarketableSecuritiesCurrent"),
    "total_debt": ("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "LongTermDebtNoncurrent"),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "stockholders_equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "diluted_weighted_average_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "common_shares_outstanding": ("CommonStocksIncludingAdditionalPaidInCapital", "EntityCommonStockSharesOutstanding"),
}

DURATION_METRICS = {
    "revenue", "gross_profit", "operating_income", "net_income", "diluted_eps",
    "operating_cash_flow", "capital_expenditures", "diluted_weighted_average_shares",
}

UNIT_BY_METRIC = {
    "diluted_eps": "USD/shares",
    "diluted_weighted_average_shares": "shares",
    "common_shares_outstanding": "shares",
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}-{_hash(value)[:24].upper()}"


def _safe_cik(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        raise ValueError("A CIK is required to build SEC Company Facts.")
    return digits.zfill(10)


def _fetch_company_facts(cik: str) -> tuple[dict[str, Any], bytes, str]:
    if not config.sec_user_agent_is_configured():
        raise ValueError("SEC Company Facts requires a configured SEC user agent.")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}, timeout=config.HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    return payload, response.content, url


def _days(start: str | None, end: str) -> int | None:
    if not start:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _valid_period(metric: str, unit: dict[str, Any]) -> bool:
    start, end = unit.get("start"), unit.get("end")
    if not end:
        return False
    if metric not in DURATION_METRICS:
        return not start
    duration = _days(start, end)
    if duration is None:
        return False
    form = str(unit.get("form") or "").upper().replace("/A", "")
    frame = str(unit.get("frame") or "")
    if form == "10-K":
        return 250 <= duration <= 380
    if form == "10-Q":
        return 65 <= duration <= 115 and (not frame or re.search(r"Q[1-4]$", frame) is not None)
    return False


def _expected_unit(metric: str) -> str:
    return UNIT_BY_METRIC.get(metric, "USD")


def _candidate_score(unit: dict[str, Any], concept_rank: int) -> tuple[Any, ...]:
    form = str(unit.get("form") or "").upper()
    amended = int(form.endswith("/A"))
    return (
        str(unit.get("filed") or ""),
        amended,
        int(bool(unit.get("frame"))),
        -concept_rank,
        str(unit.get("accn") or ""),
    )


def _source_lineage(package_id: str, accession: str | None, db_path: Path | str) -> tuple[str | None, str | None]:
    if not accession:
        return None, None
    normalized = re.sub(r"\D", "", accession)
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """SELECT dc.downloaded_document_id, dc.metadata_json FROM discovered_candidates dc
               WHERE dc.package_id=? AND dc.downloaded_document_id IS NOT NULL""",
            (package_id,),
        ).fetchall()
        document_id = None
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                continue
            source = metadata.get("source_metadata", metadata)
            if re.sub(r"\D", "", str(source.get("accession_number") or "")) == normalized:
                document_id = row["downloaded_document_id"]
                break
        artifact = connection.execute(
            """SELECT artifact_id FROM package_artifacts
               WHERE package_id=? AND source_document_id=? AND artifact_status='CURRENT'
               ORDER BY CASE artifact_type WHEN 'SEC_READER_PDF' THEN 0 WHEN 'FULL_FILING' THEN 1 ELSE 2 END LIMIT 1""",
            (package_id, document_id),
        ).fetchone() if document_id else None
    return document_id, artifact[0] if artifact else None


def normalize_company_facts(
    payload: dict[str, Any],
    *,
    package_id: str,
    package_version_id: str,
    research_cutoff: str,
    response_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    facts = payload.get("facts", {}).get("us-gaap", {})
    candidates: list[dict[str, Any]] = []
    for metric, concepts in CONCEPTS.items():
        expected_unit = _expected_unit(metric)
        for concept_rank, concept in enumerate(concepts):
            fact = facts.get(concept)
            if not fact:
                continue
            units = fact.get("units", {}).get(expected_unit, [])
            for item in units:
                if str(item.get("filed") or "") > research_cutoff or not _valid_period(metric, item):
                    continue
                record = {
                    "taxonomy_concept": concept,
                    "normalized_metric": metric,
                    "value": float(item["val"]),
                    "unit": expected_unit,
                    "period_start": item.get("start"),
                    "period_end": item["end"],
                    "fiscal_year": item.get("fy"),
                    "fiscal_quarter": item.get("fp"),
                    "form": item.get("form"),
                    "filing_date": item.get("filed"),
                    "accession": item.get("accn"),
                    "frame": item.get("frame"),
                    "concept_rank": concept_rank,
                }
                record["financial_fact_id"] = _id("FACT", record)
                candidates.append(record)

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault((candidate["normalized_metric"], candidate["period_end"], candidate["fiscal_quarter"] or ""), []).append(candidate)
    selected_ids: set[str] = set()
    conflict_rows: list[dict[str, Any]] = []
    for key, rows in groups.items():
        selected = max(rows, key=lambda row: _candidate_score(row, row["concept_rank"]))
        selected_ids.add(selected["financial_fact_id"])
        values = {row["value"] for row in rows}
        if len(values) > 1:
            conflict_rows.append({
                "fact_conflict_id": _id("FCON", [key, sorted(row["financial_fact_id"] for row in rows)]),
                "normalized_metric": key[0], "period_end": key[1],
                "candidate_fact_ids": sorted(row["financial_fact_id"] for row in rows),
                "selected_fact_id": selected["financial_fact_id"],
                "material": int(max(values) - min(values) > max(1.0, abs(selected["value"]) * 0.01)),
                "explanation": "Selected deterministically by filing date, amendment status, SEC frame, concept priority, and accession.",
            })

    now = database.utc_now_iso()
    lineage_cache: dict[str, tuple[str | None, str | None]] = {}
    for row in candidates:
        accession = str(row.get("accession") or "")
        if accession not in lineage_cache:
            lineage_cache[accession] = _source_lineage(package_id, accession, db_path)
    with database.get_connection(db_path) as connection:
        for row in candidates:
            document_id, artifact_id = lineage_cache[str(row.get("accession") or "")]
            selected = row["financial_fact_id"] in selected_ids
            connection.execute(
                """INSERT OR IGNORE INTO normalized_financial_facts(
                   financial_fact_id, package_id, package_version_id, response_id, taxonomy_concept,
                   normalized_metric, value, unit, period_start, period_end, fiscal_year, fiscal_quarter,
                   form, filing_date, accession, source_document_id, source_artifact_id, fact_status,
                   derivation_formula, source_fact_ids_json, validation_status, selected, selection_reason, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REPORTED', NULL, '[]', 'VALID', ?, ?, ?)""",
                (row["financial_fact_id"], package_id, package_version_id, response_id, row["taxonomy_concept"],
                 row["normalized_metric"], row["value"], row["unit"], row["period_start"], row["period_end"],
                 row["fiscal_year"], row["fiscal_quarter"], row["form"], row["filing_date"], row["accession"],
                 document_id, artifact_id, int(selected),
                 "Deterministic best candidate." if selected else "Preserved alternate candidate.", now),
            )
        for conflict in conflict_rows:
            connection.execute(
                """INSERT OR REPLACE INTO financial_fact_conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                (conflict["fact_conflict_id"], package_id, package_version_id, conflict["normalized_metric"],
                 conflict["period_end"], json.dumps(conflict["candidate_fact_ids"]), conflict["selected_fact_id"],
                 conflict["material"], conflict["explanation"], now),
            )
    derived = _derive_metrics(package_id, package_version_id, response_id, db_path=db_path)
    return {
        "reported_count": len(candidates),
        "selected_count": len(selected_ids) + len(derived),
        "conflict_count": len(conflict_rows),
        "derived_count": len(derived),
    }


def _derive_metrics(package_id: str, package_version_id: str, response_id: str, *, db_path: Path | str) -> list[str]:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """SELECT * FROM normalized_financial_facts WHERE package_version_id=? AND selected=1
               ORDER BY period_end DESC""", (package_version_id,)
        ).fetchall()
    by_period: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        by_period.setdefault(row["period_end"], {})[row["normalized_metric"]] = row
    formulas = {
        "gross_margin": ("gross_profit", "revenue", "gross_profit / revenue"),
        "operating_margin": ("operating_income", "revenue", "operating_income / revenue"),
        "net_margin": ("net_income", "revenue", "net_income / revenue"),
        "current_ratio": ("current_assets", "current_liabilities", "current_assets / current_liabilities"),
        "debt_to_equity": ("total_debt", "stockholders_equity", "total_debt / stockholders_equity"),
    }
    created: list[str] = []
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        for period, metrics in by_period.items():
            for metric, (numerator, denominator, formula) in formulas.items():
                if numerator not in metrics or denominator not in metrics or metrics[denominator]["value"] == 0:
                    continue
                sources = [metrics[numerator]["financial_fact_id"], metrics[denominator]["financial_fact_id"]]
                value = metrics[numerator]["value"] / metrics[denominator]["value"]
                fact_id = _id("FACT", [package_version_id, metric, period, sources, value])
                connection.execute(
                    """INSERT OR IGNORE INTO normalized_financial_facts(
                       financial_fact_id, package_id, package_version_id, response_id, taxonomy_concept,
                       normalized_metric, value, unit, period_start, period_end, fiscal_year, fiscal_quarter,
                       form, filing_date, accession, source_document_id, source_artifact_id, fact_status,
                       derivation_formula, source_fact_ids_json, validation_status, selected, selection_reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ratio', '', ?, NULL, NULL, NULL, NULL, '', NULL, NULL,
                       'DERIVED', ?, ?, 'VALID', 1, 'Derived only from selected deterministic SEC facts.', ?)""",
                    (fact_id, package_id, package_version_id, response_id, f"DERIVED:{metric}", metric, value,
                     period, formula, json.dumps(sources), now),
                )
                created.append(fact_id)
    return created


def build_company_facts(
    package_id: str,
    package_version_id: str,
    *,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    database.initialize_database(db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        raise ValueError("Package does not exist.")
    cik = _safe_cik(package.get("cik") or "")
    if payload is None:
        payload, raw, source_url = _fetch_company_facts(cik)
    else:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        source_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response_hash = hashlib.sha256(raw).hexdigest()
    response_id = _id("CFR", [package_version_id, response_hash])
    path = config.PACKAGE_DIR / package_id / "phase6c" / "company_facts" / f"CIK{cik}-{response_hash[:12]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO company_facts_responses(
               company_facts_response_id, package_id, package_version_id, cik, source_url,
               response_sha256, local_path, fetched_at, sec_submission_updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (response_id, package_id, package_version_id, cik, source_url, response_hash, str(path),
             database.utc_now_iso(), payload.get("updated") or payload.get("lastUpdated")),
        )
    summary = normalize_company_facts(payload, package_id=package_id, package_version_id=package_version_id,
                                      research_cutoff=package["research_cutoff_date"], response_id=response_id, db_path=db_path)
    return {"response_id": response_id, "response_sha256": response_hash, "source_url": source_url, **summary}


def list_selected_facts(package_version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM normalized_financial_facts WHERE package_version_id=? AND selected=1 ORDER BY period_end DESC, normalized_metric",
            (package_version_id,),
        ).fetchall()
    return [dict(row) for row in rows]
