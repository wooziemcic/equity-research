from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app import config
from app.utils import database


FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("revenue_growth", ("revenue", "sales", "organic growth", "growth")),
    ("profitability", ("ebitda", "operating income", "operating margin", "gross margin", "earnings")),
    ("cash_flow", ("operating cash flow", "free cash flow", "cash from operations")),
    ("debt_liquidity", ("debt", "liquidity", "cash balance", "leverage", "credit facility")),
    ("guidance", ("guidance", "outlook", "forecast", "expects", "target")),
    ("strategy", ("acquisition", "transaction", "strategy", "integration", "capital allocation")),
    ("material_risk", ("risk", "litigation", "regulatory", "uncertainty", "decline", "adverse")),
    ("valuation", ("valuation", "price target", "reference price", "multiple", "enterprise value")),
)
NOISY_TYPES = {"description_numeric", "description_non_numeric", "description"}
ISOLATED_PATTERN = re.compile(r"^\s*(?:page\s*)?[\d,.$%()/-]+\s*$", re.IGNORECASE)
ACCESSION_PATTERN = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")


@dataclass(frozen=True)
class NarrativeCandidate:
    candidate_id: str
    evidence_id: str
    claim_family: str
    candidate_kind: str
    concise_claim: str
    value: float | None
    unit: str | None
    currency: str | None
    period: str | None
    rank_score: float
    source_locator_json: str
    fingerprint: str

    def model_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "claim_family": self.claim_family,
            "claim": self.concise_claim[:600],
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
        }


@dataclass(frozen=True)
class CandidateSelection:
    supporting: tuple[NarrativeCandidate, ...]
    risks: tuple[NarrativeCandidate, ...]
    metrics: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    considered: int
    eligible: int
    excluded: int

    @property
    def selected_count(self) -> int:
        return len(self.supporting) + len(self.risks)

    def smaller(self) -> "CandidateSelection":
        return CandidateSelection(
            self.supporting[:15], self.risks[:6], self.metrics[:6], self.conflicts[:5],
            self.considered, self.eligible, self.excluded,
        )


def claim_family(record: dict[str, Any]) -> str | None:
    haystack = " ".join(
        str(record.get(field) or "")
        for field in ("metric_name", "evidence_type", "normalized_subject", "claim_text", "section_heading")
    ).casefold()
    for family, terms in FAMILY_RULES:
        if any(term in haystack for term in terms):
            return family
    return None


def _chunk_identity(record: dict[str, Any]) -> str:
    try:
        locator = json.loads(record.get("source_locator_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        locator = {}
    return str(locator.get("chunk_id") or locator.get("chunk_hash") or record.get("extraction_fingerprint") or "")


def candidate_fingerprint(version_id: str, record: dict[str, Any], family: str) -> str:
    normalized_value = "" if record.get("value") is None else f"{float(record['value']):.12g}"
    raw = "|".join(
        (
            version_id,
            str(record.get("version_document_id") or ""),
            _chunk_identity(record),
            family,
            normalized_value,
            str(record.get("unit") or "").casefold(),
            str(record.get("currency") or "").upper(),
            str(record.get("period") or ""),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def exclusion_reason(record: dict[str, Any], family: str | None) -> str | None:
    evidence_type = str(record.get("evidence_type") or "").strip().casefold()
    claim = re.sub(r"\s+", " ", str(record.get("claim_text") or "")).strip()
    if evidence_type in NOISY_TYPES or evidence_type.startswith("description_"):
        return "GENERIC_DESCRIPTION"
    if not claim or ISOLATED_PATTERN.fullmatch(claim):
        return "ISOLATED_NUMBER_OR_LABEL"
    if ACCESSION_PATTERN.search(claim):
        return "ACCESSION_NUMBER"
    if re.fullmatch(r"(?:page\s+)?\d+", claim, re.IGNORECASE):
        return "PAGE_NUMBER"
    if len(claim.split()) < 4 or claim.rstrip().endswith(":"):
        return "HEADING_WITHOUT_CLAIM"
    if not family:
        return "NO_CLAIM_FAMILY"
    if record.get("value") is not None and not record.get("unit"):
        return "NUMERIC_UNIT_MISSING"
    if record.get("value") is not None and not record.get("period"):
        return "NUMERIC_PERIOD_MISSING"
    return None


def _score(record: dict[str, Any], family: str) -> float:
    score = 30.0
    if record.get("verification_status") == config.VERIFICATION_SUPPORTS:
        score += 20
    if str(record.get("confidence") or "").upper() == "HIGH":
        score += 10
    if record.get("period"):
        score += 10
    if record.get("unit"):
        score += 5
    if record.get("currency"):
        score += 4
    if record.get("value") is not None:
        score += 8
    if record.get("analyst_status") == config.ANALYST_STATUS_ACCEPTED:
        score += 8
    if record.get("extraction_method") == "OPENAI_STRUCTURED":
        score += 3
    if family in {"valuation", "guidance", "material_risk"}:
        score += 4
    period = str(record.get("period") or "")
    year_match = re.search(r"20\d{2}", period)
    if year_match:
        score += min(8, max(0, int(year_match.group()) - 2018))
    return score


def _candidate(record: dict[str, Any], version_id: str, family: str) -> NarrativeCandidate:
    fingerprint = candidate_fingerprint(version_id, record, family)
    kind = "risk" if family == "material_risk" or str(record.get("direction") or "").upper() in {"NEGATIVE", "DOWN"} else "support"
    return NarrativeCandidate(
        candidate_id=f"NC-{fingerprint[:16].upper()}",
        evidence_id=str(record["evidence_id"]),
        claim_family=family,
        candidate_kind=kind,
        concise_claim=re.sub(r"\s+", " ", str(record.get("claim_text") or "")).strip(),
        value=float(record["value"]) if record.get("value") is not None else None,
        unit=str(record.get("unit") or "") or None,
        currency=str(record.get("currency") or "") or None,
        period=str(record.get("period") or "") or None,
        rank_score=_score(record, family),
        source_locator_json=str(record.get("source_locator_json") or ""),
        fingerprint=fingerprint,
    )


def _diverse(candidates: list[NarrativeCandidate], limit: int) -> tuple[NarrativeCandidate, ...]:
    selected: list[NarrativeCandidate] = []
    family_counts: dict[str, int] = {}
    source_keys: set[tuple[str, str]] = set()
    for item in sorted(candidates, key=lambda row: (-row.rank_score, row.fingerprint)):
        source_key = (item.claim_family, item.source_locator_json)
        if source_key in source_keys or family_counts.get(item.claim_family, 0) >= 2:
            continue
        selected.append(item)
        source_keys.add(source_key)
        family_counts[item.claim_family] = family_counts.get(item.claim_family, 0) + 1
        if len(selected) == limit:
            break
    return tuple(selected)


def _latest_metrics(metrics: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for metric in metrics:
        if metric.get("value") is None:
            continue
        key = (str(metric.get("metric_code") or ""), str(metric.get("scenario") or ""))
        if key not in latest or str(metric.get("period") or "") > str(latest[key].get("period") or ""):
            latest[key] = metric
    rows = sorted(latest.values(), key=lambda row: (str(row.get("metric_code") or ""), str(row.get("period") or "")), reverse=True)
    return tuple(
        {
            "metric_code": row.get("metric_code"), "value": row.get("value"), "unit": row.get("unit"),
            "currency": row.get("currency"), "period": row.get("period"), "confidence": row.get("confidence"),
        }
        for row in rows[:12]
    )


def _valid_conflicts(conflicts: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    valid = [
        row for row in conflicts
        if str(row.get("analyst_status") or "UNRESOLVED").upper() not in {"RESOLVED", "INVALID", "DISMISSED"}
        and str(row.get("comparability_status") or "VALID").upper() not in {"INVALID", "NOT_COMPARABLE"}
    ]
    return tuple(
        {
            "conflict_id": row.get("conflict_id"), "subject": row.get("subject"), "metric": row.get("metric"),
            "period": row.get("period"), "severity": row.get("severity"), "conflict_type": row.get("conflict_type"),
        }
        for row in valid[:10]
    )


def select_narrative_candidates(
    *, attempt_id: str, analysis_run_id: str, version_id: str, processing_run_id: str,
    evidence: list[dict[str, Any]], metrics: list[dict[str, Any]], conflicts: list[dict[str, Any]],
    db_path: str,
) -> CandidateSelection:
    candidates: list[NarrativeCandidate] = []
    audit_rows: list[tuple[dict[str, Any], NarrativeCandidate | None, str | None]] = []
    seen_fingerprints: set[str] = set()
    verified = {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
    for record in evidence:
        family = claim_family(record)
        reason = None if record.get("verification_status") in verified else "NOT_VERIFIED"
        reason = reason or exclusion_reason(record, family)
        item = _candidate(record, version_id, family) if family else None
        if item and not reason and item.fingerprint in seen_fingerprints:
            reason = "DUPLICATE_CANDIDATE_FINGERPRINT"
        if item and not reason:
            seen_fingerprints.add(item.fingerprint)
            candidates.append(item)
        audit_rows.append((record, item, reason))

    supporting = _diverse([item for item in candidates if item.candidate_kind == "support"], 30)
    risks = _diverse([item for item in candidates if item.candidate_kind == "risk"], 12)
    selected_ids = {item.candidate_id for item in (*supporting, *risks)}
    now = database.utc_now_iso()
    for record, item, reason in audit_rows:
        stored_family = item.claim_family if item else claim_family(record)
        database.create_narrative_candidate(
            {
                "attempt_id": attempt_id, "candidate_id": item.candidate_id if item else f"EX-{record['evidence_id']}",
                "analysis_run_id": analysis_run_id, "version_id": version_id, "processing_run_id": processing_run_id,
                "evidence_id": record["evidence_id"],
                "candidate_fingerprint": item.fingerprint if item else hashlib.sha256(f"{version_id}|{record['evidence_id']}".encode()).hexdigest(),
                "candidate_kind": item.candidate_kind if item else None, "claim_family": stored_family,
                "eligible": int(item is not None and reason is None),
                "selected": int(bool(item and item.candidate_id in selected_ids)),
                "exclusion_reason": reason if reason else (None if item and item.candidate_id in selected_ids else "DIVERSITY_OR_RANK_LIMIT"),
                "rank_score": item.rank_score if item else 0.0, "created_at": now,
            },
            db_path=db_path,
        )
    return CandidateSelection(
        supporting, risks, _latest_metrics(metrics), _valid_conflicts(conflicts),
        len(evidence), len(candidates), len(evidence) - len(candidates),
    )
