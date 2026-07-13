from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, Field
from dotenv import dotenv_values

from app import config
from app.utils import database

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - requirements install the current SDK.
    OpenAI = None  # type: ignore[assignment,misc]


OPENAI_NOT_CONFIGURED = "OPENAI_NOT_CONFIGURED"
OPENAI_AUTH_FAILED = "OPENAI_AUTH_FAILED"
OPENAI_RATE_LIMITED = "OPENAI_RATE_LIMITED"
OPENAI_QUOTA_EXCEEDED = "OPENAI_QUOTA_EXCEEDED"
OPENAI_MODEL_UNAVAILABLE = "OPENAI_MODEL_UNAVAILABLE"
OPENAI_REQUEST_FAILED = "OPENAI_REQUEST_FAILED"


class OpenAIProviderError(RuntimeError):
    """Safe provider failure that never contains a key or raw provider response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class ExtractedClaim(BaseModel):
    evidence_ids: list[str] = Field(default_factory=list)
    claim_text: str
    citation_locators: list[str] = Field(default_factory=list)
    material: bool = True
    abstain: bool = False


class ModelThesisItem(BaseModel):
    item_type: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    citation_locators: list[str] = Field(default_factory=list)
    confidence: str = "INSUFFICIENT"
    abstain: bool = False


class RecommendationNarrative(BaseModel):
    rationale: str
    why_not_buy: str
    why_not_hold: str
    why_not_sell: str
    abstention_reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    citation_locators: list[str] = Field(default_factory=list)


class ClosedCorpusAIReview(BaseModel):
    extracted_claims: list[ExtractedClaim] = Field(default_factory=list)
    thesis_items: list[ModelThesisItem] = Field(default_factory=list)
    conflict_explanations: list[ExtractedClaim] = Field(default_factory=list)
    recommendation: RecommendationNarrative


@dataclass(frozen=True)
class OpenAIPreflightResult:
    configured: bool
    connected: bool
    model: str
    code: str | None = None
    message: str | None = None


_preflight_cache: tuple[float, str] | None = None


def get_openai_api_key() -> str:
    """Read local env or Streamlit secrets without retaining or displaying the value."""
    local_env = dotenv_values(config.PROJECT_ROOT / ".env")
    if "OPENAI_API_KEY" in local_env:
        return str(local_env.get("OPENAI_API_KEY") or "").strip()
    value = os.getenv("OPENAI_API_KEY", "").strip()
    if value:
        return value
    try:
        import streamlit as st

        return str(st.secrets.get("OPENAI_API_KEY", "") or "").strip()
    except Exception:
        return ""


def _client(api_key: str | None = None) -> Any:
    if OpenAI is None:
        raise OpenAIProviderError(OPENAI_NOT_CONFIGURED, "The current OpenAI Python SDK is not installed.")
    key = api_key or get_openai_api_key()
    if not key:
        raise OpenAIProviderError(OPENAI_NOT_CONFIGURED, "OpenAI is required for AI analysis, but no API key is configured.")
    return OpenAI(api_key=key)


def _error_code(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None)
    if "authentication" in name or status in {401, 403}:
        return OPENAI_AUTH_FAILED
    if "ratelimit" in name or status == 429:
        body = str(getattr(exc, "body", "") or "").lower()
        return OPENAI_QUOTA_EXCEEDED if "quota" in body or "billing" in body else OPENAI_RATE_LIMITED
    if "notfound" in name or status == 404:
        return OPENAI_MODEL_UNAVAILABLE
    return OPENAI_REQUEST_FAILED


def _safe_provider_error(exc: Exception) -> OpenAIProviderError:
    code = _error_code(exc)
    messages = {
        OPENAI_AUTH_FAILED: "OpenAI authentication failed. Check the configured key.",
        OPENAI_RATE_LIMITED: "OpenAI rate limit reached. Retry the analysis later.",
        OPENAI_QUOTA_EXCEEDED: "OpenAI quota is unavailable for this account. Check billing or quota before retrying.",
        OPENAI_MODEL_UNAVAILABLE: "The configured OpenAI model is unavailable to this account.",
        OPENAI_REQUEST_FAILED: "The OpenAI request failed. Retry the analysis later.",
    }
    return OpenAIProviderError(code, messages[code])


def preflight_openai(*, force: bool = False, client: Any | None = None) -> OpenAIPreflightResult:
    """Make one explicit, package-free model-access check and cache success briefly."""
    global _preflight_cache
    now = time.monotonic()
    if not force and _preflight_cache and _preflight_cache[1] == config.OPENAI_MODEL and now - _preflight_cache[0] < config.OPENAI_PREFLIGHT_CACHE_SECONDS:
        return OpenAIPreflightResult(True, True, config.OPENAI_MODEL)
    try:
        active_client = client or _client()
        active_client.models.retrieve(config.OPENAI_MODEL)
        _preflight_cache = (now, config.OPENAI_MODEL)
        return OpenAIPreflightResult(True, True, config.OPENAI_MODEL)
    except OpenAIProviderError as exc:
        return OpenAIPreflightResult(False, False, config.OPENAI_MODEL, exc.code, exc.safe_message)
    except Exception as exc:
        error = _safe_provider_error(exc)
        return OpenAIPreflightResult(True, False, config.OPENAI_MODEL, error.code, error.safe_message)


T = TypeVar("T", bound=BaseModel)


def responses_parse(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    client: Any | None = None,
    max_output_tokens: int = 4000,
) -> T:
    """Call Responses with no tools and validate the parsed Pydantic result."""
    try:
        response = (client or _client()).responses.parse(
            model=config.OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
            ],
            text_format=schema,
            tools=[],
            store=False,
            reasoning={"effort": config.OPENAI_REASONING_EFFORT},
            max_output_tokens=max_output_tokens,
        )
        parsed = getattr(response, "output_parsed", None)
        if isinstance(parsed, schema):
            return parsed
        output_text = getattr(response, "output_text", "")
        return schema.model_validate_json(output_text)
    except OpenAIProviderError:
        raise
    except Exception as exc:
        raise _safe_provider_error(exc) from exc


def _validate_citations(review: ClosedCorpusAIReview, evidence: list[dict[str, Any]]) -> None:
    allowed_ids = {str(item["evidence_id"]) for item in evidence}
    locators_by_id = {
        str(item["evidence_id"]): str(item.get("source_locator_json") or "")
        for item in evidence
        if item.get("source_locator_json")
    }
    claims: list[ExtractedClaim | ModelThesisItem | RecommendationNarrative] = [
        *review.extracted_claims,
        *review.thesis_items,
        *review.conflict_explanations,
        review.recommendation,
    ]
    for claim in claims:
        if any(identifier not in allowed_ids for identifier in claim.evidence_ids):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned a claim outside the selected package evidence.")
        material = getattr(claim, "material", True)
        abstain = getattr(claim, "abstain", False)
        if isinstance(claim, (ExtractedClaim, ModelThesisItem)) and material and not abstain and not claim.evidence_ids:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned an unsupported material claim.")
        if isinstance(claim, RecommendationNarrative) and not claim.evidence_ids and any(
            str(getattr(claim, field) or "").strip()
            for field in ("rationale", "why_not_buy", "why_not_hold", "why_not_sell")
        ) and not claim.abstention_reason:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned recommendation narrative without evidence.")
        if claim.evidence_ids and not claim.citation_locators:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned evidence IDs without document locators.")
        if claim.citation_locators and any(locator not in locators_by_id.values() for locator in claim.citation_locators):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned a citation locator outside the selected package.")
        expected_locators = {locators_by_id[identifier] for identifier in claim.evidence_ids if identifier in locators_by_id}
        if expected_locators and not expected_locators.issubset(set(claim.citation_locators)):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI omitted an exact locator for selected evidence.")


def run_closed_corpus_ai_review(
    *,
    version: dict[str, Any],
    processing_run_id: str,
    evidence: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    db_path: str,
    client: Any | None = None,
) -> ClosedCorpusAIReview:
    """Extract and interpret only evidence and chunks from one locked version."""
    if version.get("status") != config.VERSION_STATUS_LOCKED:
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis requires a locked package version.")
    if any(item.get("version_id") not in {None, version["version_id"]} for item in evidence):
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis received evidence outside the selected package version.")
    if config.OPENAI_REQUIRED:
        result = preflight_openai(client=client)
        if not result.connected:
            raise OpenAIProviderError(result.code or OPENAI_REQUEST_FAILED, result.message or "OpenAI preflight failed.")
    chunks = database.list_document_chunks(processing_run_id, version_id=version["version_id"], db_path=db_path)
    evidence_payload = [
        {
            "evidence_id": item["evidence_id"],
            "evidence_type": item.get("evidence_type"),
            "claim_text": item.get("claim_text"),
            "source_locator_json": item.get("source_locator_json"),
            "verification_status": item.get("verification_status"),
            "value": item.get("value"),
            "period": item.get("period"),
        }
        for item in evidence
    ]
    chunk_payload = [
        {
            "chunk_id": chunk["chunk_id"],
            "chunk_text": str(chunk.get("chunk_text") or "")[:5000],
            "source_locator_json": chunk.get("source_locator_json"),
        }
        for chunk in chunks[:100]
    ]
    payload = {
        "locked_version_id": version["version_id"],
        "processing_run_id": processing_run_id,
        "evidence": evidence_payload,
        "chunks": chunk_payload,
        "deterministic_metrics": metrics,
        "conflicts": conflicts,
    }
    system_prompt = (
        "You are the closed-corpus research analyst. Use only the evidence, chunks, and deterministic metrics in the user payload. "
        "Do not use web search, external tools, model memory, live prices, or facts from another package. "
        "Every material claim must include one or more exact evidence_ids and the corresponding source_locator_json string. "
        "Abstain instead of guessing. Preserve deterministic arithmetic and recommendation thresholds; provide narrative, interpretation, "
        "conflict explanations, and thesis wording only."
    )
    review = responses_parse(
        system_prompt=system_prompt,
        user_payload=payload,
        schema=ClosedCorpusAIReview,
        client=client,
    )
    _validate_citations(review, evidence)
    return review
