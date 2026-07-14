from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app import config
from app.utils import database


def normalize_claim_family(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def normalize_period(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def conflict_fingerprint(
    processing_run_id: str,
    metric_or_claim_family: str,
    period: str,
    evidence_id_a: str,
    evidence_id_b: str,
    conflict_type: str,
) -> str:
    pair = sorted((evidence_id_a, evidence_id_b))
    payload = "|".join(
        (
            processing_run_id,
            normalize_claim_family(metric_or_claim_family),
            normalize_period(period),
            pair[0], pair[1], conflict_type.strip().upper(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_comparable(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
    if normalize_claim_family(left.get("metric_name")) != normalize_claim_family(right.get("metric_name")):
        return False, "DIFFERENT_METRIC"
    if normalize_period(left.get("period")) != normalize_period(right.get("period")):
        return False, "DIFFERENT_PERIOD"
    left_unit = str(left.get("unit") or "").strip().lower()
    right_unit = str(right.get("unit") or "").strip().lower()
    if left_unit and right_unit and left_unit != right_unit:
        return False, "INCOMPATIBLE_UNIT"
    left_currency = str(left.get("currency") or "").strip().upper()
    right_currency = str(right.get("currency") or "").strip().upper()
    if left_currency and right_currency and left_currency != right_currency:
        return False, "INCOMPATIBLE_CURRENCY"
    if left.get("source_text_hash") and left.get("source_text_hash") == right.get("source_text_hash"):
        return False, "DUPLICATE_EVIDENCE"
    if left.get("version_document_id") == right.get("version_document_id"):
        return False, "SAME_SOURCE_DOCUMENT"
    return True, "COMPARABLE"


def audit_historical_conflicts(
    processing_run_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    conflicts = database.list_claim_conflicts(processing_run_id, db_path=db_path)
    evidence = {
        row["evidence_id"]: row
        for row in database.list_evidence_records(processing_run_id, db_path=db_path)
    }
    fingerprints: list[str] = []
    incomparable = 0
    valid_unresolved = 0
    for item in conflicts:
        fingerprint = conflict_fingerprint(
            processing_run_id,
            item.get("metric") or item.get("subject") or "",
            item.get("period") or "",
            item.get("evidence_id_a") or "",
            item.get("evidence_id_b") or "",
            item.get("conflict_type") or "",
        )
        fingerprints.append(fingerprint)
        left = evidence.get(item.get("evidence_id_a") or "", {})
        right = evidence.get(item.get("evidence_id_b") or "", {})
        comparable, _ = evidence_comparable(left, right) if left and right else (False, "MISSING_EVIDENCE")
        incomparable += int(not comparable)
        valid_unresolved += int(comparable and item.get("analyst_status") != config.DOCUMENT_STATUS_RESOLVED)
    counts = Counter(fingerprints)
    unique = len(counts)
    return {
        "historical_total": len(conflicts),
        "unique_conflict_fingerprints": unique,
        "likely_duplicate_conflicts": sum(count - 1 for count in counts.values() if count > 1),
        "valid_unresolved_conflicts": valid_unresolved,
        "excluded_incomparable_records": incomparable,
    }
