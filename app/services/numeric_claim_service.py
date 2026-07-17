from __future__ import annotations

import json
import math
import re
import secrets
from pathlib import Path
from typing import Any, Iterable

from app import config
from app.services.analysis_snapshot_service import get_analysis_snapshot, validate_analysis_snapshot
from app.utils import database


BLOCKING_STATUSES = {"PERIOD_AMBIGUOUS", "SOURCE_MISSING", "VALUE_MISMATCH", "UNSUPPORTED"}
VALID_STATUSES = {
    "VERIFIED_REPORTED", "VERIFIED_DERIVED", "ROUNDING_ACCEPTED",
    "PERIOD_AMBIGUOUS", "SOURCE_MISSING", "VALUE_MISMATCH", "UNSUPPORTED",
}


def _numbers(value: Any) -> list[str]:
    return re.findall(r"(?<![A-Za-z])[-+]?\$?\d[\d,]*(?:\.\d+)?%?", str(value or ""))


def _numeric(value: str) -> float | None:
    try:
        return float(value.replace("$", "").replace("%", "").replace(",", ""))
    except ValueError:
        return None


def validate_reported_numeric_claim(
    *,
    display_value: str,
    reported_value: float | None,
    exact_excerpt: str,
    period: str | None,
    source_document_id: str | None,
    source_artifact_id: str | None,
    evidence_id: str | None,
    tolerance: float = 0.005,
) -> tuple[str, str]:
    if not source_document_id or not source_artifact_id or not evidence_id or not exact_excerpt:
        return "SOURCE_MISSING", "The numeric statement lacks current source, artifact, or evidence lineage."
    if not period:
        return "PERIOD_AMBIGUOUS", "The numeric statement does not have a validated reporting period."
    normalized = _numeric(display_value)
    if normalized is None:
        return "UNSUPPORTED", "The display value could not be normalized."
    excerpt_values = {_numeric(token) for token in _numbers(exact_excerpt)}
    if normalized in excerpt_values:
        return "VERIFIED_REPORTED", "The displayed value occurs in the exact approved evidence excerpt."
    if reported_value is not None and math.isclose(normalized, float(reported_value), rel_tol=tolerance, abs_tol=tolerance):
        return "ROUNDING_ACCEPTED", "The displayed value is within the configured rounding tolerance."
    return "VALUE_MISMATCH", "The displayed value does not match the approved evidence value or excerpt."


def validate_derived_numeric_claim(
    *,
    display_value: str,
    derived_value: float | None,
    formula: str | None,
    input_evidence_ids: Iterable[str],
    available_evidence_ids: Iterable[str],
    period: str | None,
    tolerance: float = 0.005,
) -> tuple[str, str]:
    inputs = set(input_evidence_ids)
    if not formula or not inputs or not inputs <= set(available_evidence_ids):
        return "SOURCE_MISSING", "The derived value lacks a formula or complete current input-evidence lineage."
    if not period:
        return "PERIOD_AMBIGUOUS", "The derived value does not have a validated reporting period."
    normalized = _numeric(display_value)
    if normalized is None or derived_value is None:
        return "UNSUPPORTED", "The derived value could not be normalized."
    if math.isclose(normalized, float(derived_value), rel_tol=tolerance, abs_tol=tolerance):
        return "VERIFIED_DERIVED", "The displayed value matches the deterministic derived metric and its inputs."
    return "VALUE_MISMATCH", "The displayed value does not match the deterministic derived metric."


def _candidate_map(candidates: Iterable[Any]) -> dict[str, Any]:
    return {str(candidate.candidate_id): candidate for candidate in candidates}


def _artifact_for_candidate(candidate: Any, snapshot: dict[str, Any], *, db_path: Path | str) -> tuple[str | None, str | None]:
    with database.get_connection(db_path) as connection:
        version = connection.execute(
            """SELECT pvd.original_document_id FROM package_version_documents pvd
               JOIN analysis_runs ar ON ar.version_id=pvd.version_id
               WHERE ar.analysis_run_id=? AND pvd.document_id=?""",
            (snapshot.get("analysis_run_id"), candidate.version_document_id),
        ).fetchone()
        if not version:
            return None, None
        artifact_ids = json.loads(snapshot.get("artifact_ids_json") or "[]")
        if not artifact_ids:
            return version["original_document_id"], None
        artifact = connection.execute(
            """SELECT artifact_id FROM package_artifacts
               WHERE source_document_id=? AND artifact_id IN ({}) AND artifact_status='CURRENT'
               ORDER BY CASE artifact_type WHEN 'FILING_SECTION_REFERENCE' THEN 0 ELSE 1 END LIMIT 1""".format(
                    ",".join("?" for _ in artifact_ids)
                ),
            (version["original_document_id"], *artifact_ids),
        ).fetchone()
    return version["original_document_id"], artifact["artifact_id"] if artifact else None


def validate_memo_numeric_claims(
    memo: dict[str, Any],
    candidates: Iterable[Any],
    *,
    snapshot_id: str,
    analysis_run_id: str,
    report_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Persist and validate every numeric token in memo narrative and fact rows."""
    validate_analysis_snapshot(snapshot_id, db_path=db_path)
    snapshot = get_analysis_snapshot(snapshot_id, db_path=db_path) or {}
    if snapshot.get("analysis_run_id") != analysis_run_id:
        raise ValueError("Numeric validation snapshot does not belong to this analysis run.")
    candidate_by_id = _candidate_map(candidates)
    rows: list[tuple[str, str | None]] = []
    for item in [*memo.get("supporting_facts", []), *memo.get("risks", [])]:
        rows.append((str(item.get("claim") or ""), str(item.get("candidate_id") or "") or None))
    for field in ("investment_view", "conclusion"):
        rows.append((str(memo.get(field) or ""), None))
    records: list[dict[str, Any]] = []
    all_candidates = list(candidate_by_id.values())
    for sentence, candidate_id in rows:
        for display in _numbers(sentence):
            candidate = candidate_by_id.get(candidate_id or "")
            if not candidate:
                candidate = next(
                    (item for item in all_candidates if _numeric(display) in {_numeric(token) for token in _numbers(item.supporting_quote)}),
                    None,
                )
            if candidate:
                document_id, artifact_id = _artifact_for_candidate(candidate, snapshot, db_path=db_path)
                status, reason = validate_reported_numeric_claim(
                    display_value=display, reported_value=candidate.numeric_value,
                    exact_excerpt=candidate.supporting_quote, period=candidate.reporting_period,
                    source_document_id=document_id, source_artifact_id=artifact_id,
                    evidence_id=candidate.evidence_id,
                )
                locator = {
                    "section": candidate.section_heading, "page": candidate.page_number,
                    "version_document_id": candidate.version_document_id,
                }
                metric_name, unit, period = candidate.metric_name, candidate.unit, candidate.reporting_period
                evidence_id, excerpt = candidate.evidence_id, candidate.supporting_quote
            else:
                document_id = artifact_id = evidence_id = metric_name = unit = period = None
                excerpt, locator = "", {}
                status, reason = "UNSUPPORTED", "No approved evidence candidate supports this numeric token."
            records.append({
                "numeric_claim_id": f"NCL-{secrets.token_hex(8).upper()}", "snapshot_id": snapshot_id,
                "analysis_run_id": analysis_run_id, "report_id": report_id,
                "normalized_numeric_value": _numeric(display), "display_value": display,
                "unit": unit, "period": period, "metric_name": metric_name,
                "source_document_id": document_id, "source_artifact_id": artifact_id,
                "evidence_id": evidence_id, "exact_excerpt": excerpt,
                "source_locator_json": json.dumps(locator, sort_keys=True), "claim_type": "REPORTED",
                "derivation_formula": None, "input_evidence_ids_json": "[]",
                "validation_status": status, "validation_reason": reason,
                "created_at": database.utc_now_iso(),
            })
    with database.get_connection(db_path) as connection:
        connection.execute(
            "DELETE FROM numeric_claims WHERE snapshot_id=? AND analysis_run_id=? AND report_id IS ?",
            (snapshot_id, analysis_run_id, report_id),
        )
        for record in records:
            columns = list(record)
            connection.execute(
                f"INSERT INTO numeric_claims ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                tuple(record[column] for column in columns),
            )
    counts = {status: sum(row["validation_status"] == status for row in records) for status in VALID_STATUSES}
    blocked = [row for row in records if row["validation_status"] in BLOCKING_STATUSES]
    return {"status": "PASSED" if not blocked else "FAILED", "claims": records, "counts": counts, "blocked": blocked}
