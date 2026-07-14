from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.services.document_processing import normalize_text, sha256_text
from app.services.evidence_service import verify_evidence_record
from app.services.openai_service import StructuredParseResult, structured_parse
from app.utils import database


EXTRACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "sales", "net sales"),
    "earnings and margins": ("earnings", "eps", "margin", "ebitda", "operating income", "gross profit"),
    "cash flow": ("cash flow", "free cash flow", "operating cash", "capital expenditure", "capex"),
    "cash and debt": ("cash and cash equivalents", "liquidity", "debt", "borrowings", "leverage"),
    "guidance": ("guidance", "outlook", "expects", "forecast", "target range"),
    "valuation and reference price": ("price target", "reference price", "valuation", "multiple", "enterprise value"),
    "operating drivers": ("volume", "pricing", "backlog", "customers", "segment", "organic growth"),
    "risks": ("risk", "uncertainty", "litigation", "regulatory", "competition", "impairment"),
    "catalysts": ("catalyst", "acquisition", "divestiture", "launch", "synergy", "capital allocation"),
    "management commentary": ("management", "chief executive", "chief financial", "commentary", "strategy"),
}


class OpenAIExtractedEvidence(BaseModel):
    chunk_id: str
    verbatim_quote: str
    claim_text: str
    evidence_type: str
    metric_name: str | None = None
    numeric_value: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    confidence: str = "Medium"
    abstain: bool = False


class OpenAIEvidenceBatch(BaseModel):
    items: list[OpenAIExtractedEvidence] = Field(default_factory=list)


class OpenAIEvidenceValidationError(ValueError):
    pass


@dataclass
class OpenAIEvidenceExtractionResult:
    chunks_available: int = 0
    chunks_examined: int = 0
    items_returned: int = 0
    evidence_created: int = 0
    evidence_reused: int = 0
    evidence_rejected: int = 0
    verified_records: int = 0
    verified_numeric_records: int = 0
    endpoints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(lowered.count(keyword) for keyword in keywords)


def select_closed_corpus_chunks(chunks: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Choose bounded, deduplicated locked-corpus chunks with local keyword scoring."""
    max_chunks = config.OPENAI_MAX_EXTRACTION_CHUNKS
    per_group = max(1, math.ceil(max_chunks / len(EXTRACTION_GROUPS)))
    selected: list[tuple[str, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for group, keywords in EXTRACTION_GROUPS.items():
        ranked = sorted(
            ((_keyword_score(str(chunk.get("chunk_text") or ""), keywords), chunk) for chunk in chunks),
            key=lambda item: (-item[0], str(item[1].get("chunk_id") or "")),
        )
        added = 0
        for score, chunk in ranked:
            chunk_id = str(chunk.get("chunk_id") or "")
            chunk_hash = str(chunk.get("chunk_hash") or "")
            if score <= 0 or chunk_id in seen_ids or (chunk_hash and chunk_hash in seen_hashes):
                continue
            selected.append((group, chunk))
            seen_ids.add(chunk_id)
            if chunk_hash:
                seen_hashes.add(chunk_hash)
            added += 1
            if added >= per_group or len(selected) >= max_chunks:
                break
        if len(selected) >= max_chunks:
            break
    return selected


def _fingerprint(processing_run_id: str, chunk_id: str, quote: str, metric_name: str | None) -> str:
    payload = "|".join(
        (
            processing_run_id,
            chunk_id,
            normalize_text(quote).casefold(),
            str(metric_name or "").strip().casefold(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_evidence_type(value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").strip().upper()).strip("_")
    return cleaned[:80] or "OTHER_FACT"


def create_verified_openai_evidence(
    item: OpenAIExtractedEvidence,
    *,
    selected_chunks: dict[str, dict[str, Any]],
    processing_run_id: str,
    version_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[dict[str, Any], bool]:
    """Validate model output against trusted chunks and insert it idempotently."""
    chunk = selected_chunks.get(item.chunk_id)
    if not chunk:
        raise OpenAIEvidenceValidationError("OpenAI returned an invented or unselected chunk ID.")
    if chunk.get("processing_run_id") != processing_run_id:
        raise OpenAIEvidenceValidationError("OpenAI evidence referenced another processing run.")
    if chunk.get("version_id") != version_id:
        raise OpenAIEvidenceValidationError("OpenAI evidence referenced another package version.")
    quote = normalize_text(item.verbatim_quote)
    chunk_text = normalize_text(str(chunk.get("chunk_text") or ""))
    if not quote or quote not in chunk_text:
        raise OpenAIEvidenceValidationError("OpenAI evidence quote was not found in the selected chunk.")
    fingerprint = _fingerprint(processing_run_id, item.chunk_id, quote, item.metric_name)
    existing = database.get_evidence_by_fingerprint(processing_run_id, fingerprint, db_path=db_path)
    if existing:
        return existing, False
    locator = json.loads(chunk["source_locator_json"])
    locator.update({"chunk_id": chunk["chunk_id"], "chunk_hash": chunk["chunk_hash"]})
    subject = normalize_text(str(locator.get("display_title") or chunk.get("version_document_id") or "document")).lower()[:120]
    record = {
        "evidence_id": f"EVD-AI-{fingerprint[:20].upper()}",
        "processing_run_id": processing_run_id,
        "version_id": version_id,
        "version_document_id": chunk["version_document_id"],
        "evidence_type": _safe_evidence_type(item.evidence_type),
        "claim_text": normalize_text(item.claim_text) or quote,
        "normalized_subject": subject,
        "metric_name": str(item.metric_name or "").strip() or None,
        "value": item.numeric_value,
        "unit": str(item.unit or "").strip() or None,
        "currency": str(item.currency or "").strip() or None,
        "period": str(item.period or "").strip() or None,
        "scenario": None,
        "direction": None,
        "source_text": quote,
        "page_number": chunk.get("page_number"),
        "sheet_name": chunk.get("sheet_name"),
        "cell_or_row_range": chunk.get("row_range") or locator.get("cell_range") or locator.get("line_range"),
        "section_heading": chunk.get("section_heading"),
        "extraction_method": "OPENAI_STRUCTURED",
        "extraction_fingerprint": fingerprint,
        "confidence": str(item.confidence or "Medium")[:40],
        "verification_status": config.VERIFICATION_PENDING,
        "analyst_status": config.ANALYST_STATUS_UNREVIEWED,
        "analyst_note": "",
        "source_locator_json": json.dumps(locator, sort_keys=True),
        "source_text_hash": sha256_text(quote),
        "created_by": "openai_structured",
        "created_at": database.utc_now_iso(),
        "updated_at": database.utc_now_iso(),
    }
    created = database.create_evidence_record(record, db_path=db_path)
    verify_evidence_record(created, db_path=db_path)
    return database.get_evidence_record(created["evidence_id"], db_path=db_path) or created, True


def run_openai_evidence_extraction(
    *,
    version: dict[str, Any],
    processing_run_id: str,
    db_path: Path | str = config.DATABASE_PATH,
    client: Any | None = None,
) -> OpenAIEvidenceExtractionResult:
    """Extract evidence in bounded batches from one locked version and processing run."""
    result = OpenAIEvidenceExtractionResult()
    if version.get("status") != config.VERSION_STATUS_LOCKED:
        raise ValueError("OpenAI evidence extraction requires a locked package version.")
    processing_run = database.get_processing_run(processing_run_id, db_path=db_path)
    if not processing_run or processing_run.get("version_id") != version.get("version_id"):
        raise ValueError("OpenAI evidence extraction requires the selected version's processing run.")
    chunks = database.list_document_chunks(processing_run_id, version_id=version["version_id"], db_path=db_path)
    result.chunks_available = len(chunks)
    selected = select_closed_corpus_chunks(chunks)
    result.chunks_examined = len(selected)
    if not selected:
        result.warnings.append("No relevant trusted chunks were available for OpenAI evidence extraction.")
        return result
    selected_by_id = {str(chunk["chunk_id"]): chunk for _, chunk in selected}
    batch_size = config.OPENAI_EXTRACTION_BATCH_SIZE
    system_prompt = (
        "Extract evidence only from the supplied locked-package chunks. Return the exact chunk_id and an exact verbatim_quote. "
        "Do not invent locators, evidence IDs, arithmetic, facts, or values. Abstain when a claim is unsupported. "
        "numeric_value may identify a reported source value, but the application performs all calculations."
    )
    for start in range(0, len(selected), batch_size):
        batch = selected[start : start + batch_size]
        payload = {
            "chunks": [
                {
                    "selection_group": group,
                    "chunk_id": chunk["chunk_id"],
                    "text": str(chunk.get("chunk_text") or "")[: config.OPENAI_MAX_CHUNK_CHARACTERS],
                }
                for group, chunk in batch
            ]
        }
        parsed: StructuredParseResult[OpenAIEvidenceBatch] = structured_parse(
            system_prompt=system_prompt,
            user_payload=payload,
            schema=OpenAIEvidenceBatch,
            client=client,
            max_output_tokens=config.OPENAI_EXTRACTION_MAX_OUTPUT_TOKENS,
            pipeline_stage="evidence_extraction",
        )
        if parsed.endpoint not in result.endpoints:
            result.endpoints.append(parsed.endpoint)
        result.items_returned += len(parsed.parsed.items)
        batch_ids = {str(chunk["chunk_id"]) for _, chunk in batch}
        for item in parsed.parsed.items:
            if item.abstain:
                continue
            if item.chunk_id not in batch_ids:
                result.evidence_rejected += 1
                result.warnings.append("OpenAI returned an item outside its selected extraction batch.")
                continue
            try:
                evidence, created = create_verified_openai_evidence(
                    item,
                    selected_chunks=selected_by_id,
                    processing_run_id=processing_run_id,
                    version_id=version["version_id"],
                    db_path=db_path,
                )
            except OpenAIEvidenceValidationError as exc:
                result.evidence_rejected += 1
                result.warnings.append(str(exc))
                continue
            if created:
                result.evidence_created += 1
            else:
                result.evidence_reused += 1
            if evidence.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}:
                result.verified_records += 1
                if evidence.get("value") is not None:
                    result.verified_numeric_records += 1
    if result.evidence_created + result.evidence_reused == 0:
        result.warnings.append("OpenAI completed extraction but found no supported evidence in the selected chunks.")
    result.warnings = sorted(set(result.warnings))
    return result
