from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from app import config
from app.services.document_processing import normalize_text, sha256_text
from app.utils import database


VALUE_PATTERN = re.compile(
    r"(?P<currency>\$)?(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>%|percent|bps|x|million|billion|thousand|mm|bn|m|b)?",
    re.IGNORECASE,
)
PERIOD_PATTERN = re.compile(r"\b((?:FY|Q[1-4]|fiscal\s+year|quarter)\s*20\d{2}|20\d{2})\b", re.IGNORECASE)

RULES: tuple[tuple[str, str, str], ...] = (
    (r"\b(price target|target price|pt)\b", "PRICE_TARGET", "price_target"),
    (r"\b(revenue|sales)\b", "REPORTED_REVENUE", "revenue"),
    (r"\b(growth|grew|declined|increase|decrease)\b", "REPORTED_GROWTH", "growth"),
    (r"\b(margin|gross margin|ebitda margin)\b", "REPORTED_MARGIN", "margin"),
    (r"\b(eps|earnings per share)\b", "REPORTED_EPS", "eps"),
    (r"\b(cash flow|free cash flow|fcf)\b", "REPORTED_CASH_FLOW", "cash_flow"),
    (r"\b(debt|net debt|gross debt|leverage)\b", "REPORTED_DEBT", "debt"),
    (r"\b(liquidity|cash|cash equivalents)\b", "REPORTED_LIQUIDITY", "liquidity"),
    (r"\b(guidance|outlook|expects|expected|forecast)\b", "MANAGEMENT_GUIDANCE", "guidance"),
    (r"\b(estimate|consensus|street)\b", "ANALYST_ESTIMATE", "estimate"),
    (r"\b(rating|rated|outperform|underperform|overweight|underweight|neutral|buy|hold|sell)\b", "ANALYST_RATING", "rating"),
    (r"\b(credit rating|rated ba|rated b|moody|s&p|fitch)\b", "CREDIT_RATING", "credit_rating"),
    (r"\b(covenant|restricted payment|incurrence)\b", "COVENANT", "covenant"),
    (r"\b(coupon|maturity|conversion price|conversion premium)\b", "CONVERTIBLE_TERM", "convertible_term"),
    (r"\b(risk|lawsuit|regulatory|investigation)\b", "RISK", "risk"),
)


def _evidence_id() -> str:
    return f"EVD-{secrets.token_hex(8).upper()}"


def _verification_id() -> str:
    return f"CVER-{secrets.token_hex(8).upper()}"


def _duplicate_group_id() -> str:
    return f"DUP-{secrets.token_hex(8).upper()}"


def _conflict_id() -> str:
    return f"CNF-{secrets.token_hex(8).upper()}"


def split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [normalize_text(item) for item in raw if normalize_text(item)]


def _match_rule(sentence: str) -> tuple[str, str] | None:
    for pattern, evidence_type, metric_name in RULES:
        if re.search(pattern, sentence, re.IGNORECASE):
            return evidence_type, metric_name
    if len(sentence) > 40 and re.search(r"\b(company|business|segment|operates|provides)\b", sentence, re.IGNORECASE):
        return "COMPANY_DESCRIPTION", "description"
    return None


def _extract_value(sentence: str) -> tuple[float | None, str | None, str | None]:
    match = VALUE_PATTERN.search(sentence)
    if not match:
        return None, None, None
    raw_value = match.group("value").replace(",", "")
    try:
        value = float(raw_value)
    except ValueError:
        return None, None, None
    unit = match.group("unit")
    currency = "USD" if match.group("currency") else None
    if unit:
        unit = unit.lower()
        if unit == "percent":
            unit = "%"
        elif unit == "mm":
            unit = "million"
        elif unit in {"bn", "b"}:
            unit = "billion"
        elif unit == "m":
            unit = "million"
    return value, unit, currency


def _extract_period(sentence: str) -> str | None:
    match = PERIOD_PATTERN.search(sentence)
    return normalize_text(match.group(1)).upper() if match else None


def _direction(sentence: str) -> str | None:
    lowered = sentence.lower()
    if any(word in lowered for word in ("increase", "increased", "grew", "up", "higher")):
        return "UP"
    if any(word in lowered for word in ("decrease", "declined", "down", "lower")):
        return "DOWN"
    return None


def _normalized_subject(chunk: dict[str, Any], locator: dict[str, Any]) -> str:
    title = locator.get("display_title") or chunk.get("version_document_id") or "document"
    return normalize_text(title).lower()[:120]


def evidence_from_chunk(chunk: dict[str, Any], *, max_records: int = 5) -> list[dict[str, Any]]:
    locator = json.loads(chunk["source_locator_json"])
    records: list[dict[str, Any]] = []
    for sentence in split_sentences(chunk["chunk_text"]):
        rule = _match_rule(sentence)
        value, unit, currency = _extract_value(sentence)
        if not rule and value is None:
            continue
        evidence_type, metric_name = rule or ("OTHER_FACT", "numeric_fact")
        if evidence_type == "PRICE_TARGET" and currency is None:
            continue
        source_locator = dict(locator)
        source_locator.update({"chunk_id": chunk["chunk_id"], "chunk_hash": chunk["chunk_hash"]})
        ocr_numeric = "OCR" in str(chunk.get("extraction_method", "")).upper() and value is not None
        records.append(
            {
                "evidence_id": _evidence_id(),
                "processing_run_id": chunk["processing_run_id"],
                "version_id": chunk["version_id"],
                "version_document_id": chunk["version_document_id"],
                "evidence_type": evidence_type,
                "claim_text": sentence,
                "normalized_subject": _normalized_subject(chunk, locator),
                "metric_name": metric_name,
                "value": value,
                "unit": unit,
                "currency": currency,
                "period": _extract_period(sentence),
                "scenario": "ESTIMATE" if evidence_type == "ANALYST_ESTIMATE" else None,
                "direction": _direction(sentence),
                "source_text": sentence,
                "page_number": chunk.get("page_number"),
                "sheet_name": chunk.get("sheet_name"),
                "cell_or_row_range": chunk.get("row_range") or locator.get("cell_range") or locator.get("line_range"),
                "section_heading": chunk.get("section_heading"),
                "extraction_method": "DETERMINISTIC_RULE",
                "confidence": "Needs Review" if ocr_numeric else "High" if rule and value is not None else "Medium",
                "verification_status": config.VERIFICATION_PENDING,
                "analyst_status": config.ANALYST_STATUS_NEEDS_REVIEW if ocr_numeric else config.ANALYST_STATUS_UNREVIEWED,
                "analyst_note": "",
                "source_locator_json": json.dumps(source_locator, sort_keys=True),
                "source_text_hash": sha256_text(sentence),
                "created_by": "system",
                "created_at": database.utc_now_iso(),
                "updated_at": database.utc_now_iso(),
            }
        )
        if len(records) >= max_records:
            break
    return records


def create_analyst_evidence_from_chunk(
    *,
    chunk: dict[str, Any],
    evidence_type: str,
    claim_text: str,
    metric_name: str | None = None,
    value: float | None = None,
    unit: str | None = None,
    currency: str | None = None,
    period: str | None = None,
    analyst_note: str = "",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    locator = json.loads(chunk["source_locator_json"])
    locator.update({"chunk_id": chunk["chunk_id"], "chunk_hash": chunk["chunk_hash"], "analyst_created": True})
    record = {
        "evidence_id": _evidence_id(),
        "processing_run_id": chunk["processing_run_id"],
        "version_id": chunk["version_id"],
        "version_document_id": chunk["version_document_id"],
        "evidence_type": evidence_type,
        "claim_text": normalize_text(claim_text),
        "normalized_subject": _normalized_subject(chunk, locator),
        "metric_name": metric_name,
        "value": value,
        "unit": unit,
        "currency": currency,
        "period": period,
        "scenario": None,
        "direction": None,
        "source_text": chunk["chunk_text"],
        "page_number": chunk.get("page_number"),
        "sheet_name": chunk.get("sheet_name"),
        "cell_or_row_range": chunk.get("row_range") or locator.get("cell_range") or locator.get("line_range"),
        "section_heading": chunk.get("section_heading"),
        "extraction_method": "ANALYST_CREATED",
        "confidence": "Analyst",
        "verification_status": config.VERIFICATION_PENDING,
        "analyst_status": config.ANALYST_STATUS_NEEDS_REVIEW,
        "analyst_note": analyst_note,
        "source_locator_json": json.dumps(locator, sort_keys=True),
        "source_text_hash": sha256_text(chunk["chunk_text"]),
        "created_by": "analyst",
        "created_at": database.utc_now_iso(),
        "updated_at": database.utc_now_iso(),
    }
    database.create_evidence_record(record, db_path=db_path)
    return database.get_evidence_record(record["evidence_id"], db_path=db_path) or record


def verify_evidence_record(
    evidence: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    locator = json.loads(evidence.get("source_locator_json") or "{}")
    status = config.VERIFICATION_AMBIGUOUS
    score = 0.0
    note = "Citation could not be evaluated."
    if evidence.get("source_text_hash") and sha256_text(evidence.get("source_text") or "") != evidence["source_text_hash"]:
        status = config.VERIFICATION_HASH_MISMATCH
        note = "Stored source text hash no longer matches the evidence source text."
    else:
        chunk = _get_cited_chunk(evidence, locator, db_path=db_path)
        if not chunk:
            status = config.VERIFICATION_SOURCE_MISSING
            note = "Cited chunk could not be found in the selected processing run."
        else:
            source_text = normalize_text(evidence.get("source_text") or "")
            chunk_text = normalize_text(chunk["chunk_text"])
            if source_text and source_text.lower() not in chunk_text.lower():
                status = config.VERIFICATION_DOES_NOT_SUPPORT
                score = 0.1
                note = "Stored source text is not present in the cited chunk."
            else:
                status, score, note = _deterministic_support(evidence, chunk_text)
    verification = {
        "verification_id": _verification_id(),
        "evidence_id": evidence["evidence_id"],
        "citation_locator_json": json.dumps(locator, sort_keys=True),
        "verification_method": "DETERMINISTIC_SOURCE_TEXT",
        "support_status": status,
        "support_score": score,
        "verifier_note": note,
        "created_at": database.utc_now_iso(),
    }
    database.create_citation_verification(verification, db_path=db_path)
    database.update_evidence_record(evidence["evidence_id"], {"verification_status": status}, db_path=db_path)
    return verification


def verify_evidence_records_batch(
    evidence_records: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Apply the single-record citation rules in one database transaction."""
    if not evidence_records:
        return []
    verifications: list[dict[str, Any]] = []
    updates: list[tuple[str, str]] = []
    for evidence in evidence_records:
        locator = json.loads(evidence.get("source_locator_json") or "{}")
        status = config.VERIFICATION_AMBIGUOUS
        score = 0.0
        note = "Citation could not be evaluated."
        if evidence.get("source_text_hash") and sha256_text(evidence.get("source_text") or "") != evidence["source_text_hash"]:
            status = config.VERIFICATION_HASH_MISMATCH
            note = "Stored source text hash no longer matches the evidence source text."
        else:
            chunk = chunks_by_id.get(str(locator.get("chunk_id") or ""))
            if (
                not chunk
                or chunk.get("processing_run_id") != evidence.get("processing_run_id")
                or chunk.get("version_id") != evidence.get("version_id")
                or chunk.get("version_document_id") != evidence.get("version_document_id")
            ):
                status = config.VERIFICATION_SOURCE_MISSING
                note = "Cited chunk could not be found in the selected processing run."
            else:
                source_text = normalize_text(evidence.get("source_text") or "")
                chunk_text = normalize_text(chunk["chunk_text"])
                if source_text and source_text.lower() not in chunk_text.lower():
                    status = config.VERIFICATION_DOES_NOT_SUPPORT
                    score = 0.1
                    note = "Stored source text is not present in the cited chunk."
                else:
                    status, score, note = _deterministic_support(evidence, chunk_text)
        verifications.append(
            {
                "verification_id": _verification_id(),
                "evidence_id": evidence["evidence_id"],
                "citation_locator_json": json.dumps(locator, sort_keys=True),
                "verification_method": "DETERMINISTIC_SOURCE_TEXT",
                "support_status": status,
                "support_score": score,
                "verifier_note": note,
                "created_at": database.utc_now_iso(),
            }
        )
        updates.append((status, evidence["evidence_id"]))
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO citation_verifications (
                verification_id, evidence_id, citation_locator_json, verification_method,
                support_status, support_score, verifier_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["verification_id"],
                    item["evidence_id"],
                    item["citation_locator_json"],
                    item["verification_method"],
                    item["support_status"],
                    item["support_score"],
                    item["verifier_note"],
                    item["created_at"],
                )
                for item in verifications
            ],
        )
        connection.executemany("UPDATE evidence_records SET verification_status = ? WHERE evidence_id = ?", updates)
    return verifications


def _get_cited_chunk(
    evidence: dict[str, Any],
    locator: dict[str, Any],
    *,
    db_path: Path | str,
) -> dict[str, Any] | None:
    chunk_id = locator.get("chunk_id")
    chunks = database.list_document_chunks(
        evidence["processing_run_id"],
        version_id=evidence["version_id"],
        version_document_id=evidence["version_document_id"],
        db_path=db_path,
    )
    if chunk_id:
        return next((chunk for chunk in chunks if chunk["chunk_id"] == chunk_id), None)
    return next((chunk for chunk in chunks if evidence.get("source_text") and evidence["source_text"] in chunk["chunk_text"]), None)


def _deterministic_support(evidence: dict[str, Any], chunk_text: str) -> tuple[str, float, str]:
    claim = evidence.get("claim_text") or ""
    value = evidence.get("value")
    metric = evidence.get("metric_name")
    period = evidence.get("period")
    if value is not None and not _numeric_value_appears(value, chunk_text):
        return config.VERIFICATION_DOES_NOT_SUPPORT, 0.2, "Numeric value in the claim was not found in the cited source."
    partial_reasons: list[str] = []
    if metric and metric.lower().replace("_", " ") not in chunk_text.lower() and metric.lower() not in chunk_text.lower():
        partial_reasons.append("metric keyword not found")
    if period and period.lower() not in chunk_text.lower():
        partial_reasons.append("period not found")
    claim_tokens = {token for token in re.findall(r"[A-Za-z]{4,}", claim.lower())}
    source_tokens = set(re.findall(r"[A-Za-z]{4,}", chunk_text.lower()))
    overlap = len(claim_tokens & source_tokens) / max(len(claim_tokens), 1)
    if partial_reasons:
        return config.VERIFICATION_PARTIALLY_SUPPORTS, max(0.45, min(0.75, overlap)), "; ".join(partial_reasons)
    if overlap < 0.25:
        return config.VERIFICATION_AMBIGUOUS, 0.4, "Claim shares little text with the cited source."
    return config.VERIFICATION_SUPPORTS, 0.95 if value is not None else 0.85, "Claim text is present in the cited source region."


def _numeric_value_appears(value: Any, text: str) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    variants = {
        f"{numeric:g}",
        f"{numeric:,.0f}",
        f"{numeric:.1f}",
        f"{numeric:,.1f}",
        str(int(numeric)) if numeric.is_integer() else f"{numeric:g}",
    }
    cleaned = text.replace(",", "")
    return any(variant.replace(",", "") in cleaned for variant in variants)


def detect_duplicate_groups(
    *,
    processing_run_id: str,
    version_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    chunks = database.list_document_chunks(processing_run_id, version_id=version_id, db_path=db_path)
    groups: list[dict[str, Any]] = []
    by_hash: dict[str, list[dict[str, Any]]] = {}
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        by_hash.setdefault(chunk["chunk_hash"], []).append(chunk)
        fingerprint = re.sub(r"[^a-z]+", "", chunk["chunk_text"].lower())[:220]
        if len(fingerprint) >= 80:
            by_fingerprint.setdefault(fingerprint, []).append(chunk)
    for chunk_hash, members in by_hash.items():
        if len(members) < 2:
            continue
        group = _store_duplicate_group(
            processing_run_id=processing_run_id,
            version_id=version_id,
            duplicate_type="EXACT_CHUNK_DUPLICATE",
            canonical_hash=chunk_hash,
            members=members,
            explanation="Identical normalized chunk text appears in multiple source regions.",
            db_path=db_path,
        )
        groups.append(group)
    exact_hashes = {group["canonical_chunk_hash"] for group in groups}
    for fingerprint, members in by_fingerprint.items():
        if len(members) < 2 or members[0]["chunk_hash"] in exact_hashes:
            continue
        group = _store_duplicate_group(
            processing_run_id=processing_run_id,
            version_id=version_id,
            duplicate_type="NEAR_IDENTICAL_TEXT",
            canonical_hash=members[0]["chunk_hash"],
            members=members,
            explanation="Near-identical normalized text appears in multiple source regions.",
            db_path=db_path,
        )
        groups.append(group)
    version_docs = database.list_package_version_documents(version_id, db_path=db_path)
    by_file_hash: dict[str, list[dict[str, Any]]] = {}
    for version_doc in version_docs:
        by_file_hash.setdefault(version_doc["sha256_hash"], []).append(version_doc)
    for file_hash, members in by_file_hash.items():
        if len(members) < 2:
            continue
        group = {
            "duplicate_group_id": _duplicate_group_id(),
            "processing_run_id": processing_run_id,
            "version_id": version_id,
            "duplicate_type": "EXACT_FILE_DUPLICATE",
            "canonical_chunk_hash": file_hash,
            "member_count": len(members),
            "member_chunk_ids_json": json.dumps([member["document_id"] for member in members], sort_keys=True),
            "explanation": "Locked package includes multiple documents with the same SHA-256 file hash.",
            "created_at": database.utc_now_iso(),
        }
        database.create_duplicate_group(group, db_path=db_path)
        groups.append(group)
    return groups


def _store_duplicate_group(
    *,
    processing_run_id: str,
    version_id: str,
    duplicate_type: str,
    canonical_hash: str,
    members: list[dict[str, Any]],
    explanation: str,
    db_path: Path | str,
) -> dict[str, Any]:
    group = {
        "duplicate_group_id": _duplicate_group_id(),
        "processing_run_id": processing_run_id,
        "version_id": version_id,
        "duplicate_type": duplicate_type,
        "canonical_chunk_hash": canonical_hash,
        "member_count": len(members),
        "member_chunk_ids_json": json.dumps([member["chunk_id"] for member in members], sort_keys=True),
        "explanation": explanation,
        "created_at": database.utc_now_iso(),
    }
    database.create_duplicate_group(group, db_path=db_path)
    for member in members:
        database.update_document_chunk_duplicate_group(member["chunk_id"], group["duplicate_group_id"], db_path=db_path)
    return group


def detect_claim_conflicts(
    *,
    processing_run_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    evidence = database.list_evidence_records(processing_run_id, db_path=db_path)
    conflicts: list[dict[str, Any]] = []
    existing = database.list_claim_conflicts(processing_run_id, db_path=db_path)
    existing_keys = {
        (
            *sorted((str(item.get("evidence_id_a") or ""), str(item.get("evidence_id_b") or ""))),
            str(item.get("conflict_type") or ""),
        )
        for item in existing
    }
    if len(existing_keys) >= config.MAX_DETECTED_CLAIM_CONFLICTS:
        return []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in evidence:
        metric = record.get("metric_name")
        if not metric:
            continue
        key = (
            record.get("normalized_subject") or "",
            metric,
            record.get("period") or "",
        )
        grouped.setdefault(key, []).append(record)
    for (subject, metric, period), records in grouped.items():
        for index, left in enumerate(records):
            for right in records[index + 1 :]:
                conflict_type = _conflict_type(left, right)
                if not conflict_type:
                    continue
                conflict_key = (*sorted((left["evidence_id"], right["evidence_id"])), conflict_type)
                if conflict_key in existing_keys:
                    continue
                conflict = {
                    "conflict_id": _conflict_id(),
                    "processing_run_id": processing_run_id,
                    "subject": subject,
                    "metric": metric,
                    "period": period,
                    "evidence_id_a": left["evidence_id"],
                    "evidence_id_b": right["evidence_id"],
                    "conflict_type": conflict_type,
                    "severity": "HIGH" if conflict_type in {"FACTUAL_CONTRADICTION", "GAAP_ADJUSTED_MISMATCH"} else "MEDIUM",
                    "explanation": _conflict_explanation(conflict_type, left, right),
                    "analyst_status": config.ANALYST_STATUS_UNREVIEWED,
                    "created_at": database.utc_now_iso(),
                }
                database.create_claim_conflict(conflict, db_path=db_path)
                conflicts.append(conflict)
                existing_keys.add(conflict_key)
                if len(existing_keys) >= config.MAX_DETECTED_CLAIM_CONFLICTS:
                    return conflicts
    return conflicts


def _conflict_type(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    left_value = left.get("value")
    right_value = right.get("value")
    if left_value is not None and right_value is not None and abs(float(left_value) - float(right_value)) > 0.0001:
        if left.get("unit") != right.get("unit"):
            return "UNIT_MISMATCH"
        if left.get("evidence_type") == "ANALYST_ESTIMATE" or right.get("evidence_type") == "ANALYST_ESTIMATE":
            return "FORECAST_DISAGREEMENT"
        return "VALUE_DIFFERENCE"
    combined = f"{left.get('claim_text', '')} {right.get('claim_text', '')}".lower()
    if "gaap" in combined and "adjusted" in combined:
        return "GAAP_ADJUSTED_MISMATCH"
    return None


def _conflict_explanation(conflict_type: str, left: dict[str, Any], right: dict[str, Any]) -> str:
    if conflict_type == "UNIT_MISMATCH":
        return f"Evidence uses different units: {left.get('unit')} versus {right.get('unit')}."
    if conflict_type == "FORECAST_DISAGREEMENT":
        return f"Analyst estimates differ: {left.get('value')} versus {right.get('value')}."
    if conflict_type == "GAAP_ADJUSTED_MISMATCH":
        return "Evidence mixes GAAP and adjusted measures for the same metric/period."
    return f"Evidence values differ: {left.get('value')} versus {right.get('value')}."
