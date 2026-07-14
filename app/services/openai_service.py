from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Generic, TypeVar

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError

from app import config

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - requirements install the current SDK.
    OpenAI = None  # type: ignore[assignment,misc]


OPENAI_NOT_CONFIGURED = "OPENAI_NOT_CONFIGURED"
OPENAI_AUTH_FAILED = "OPENAI_AUTH_FAILED"
OPENAI_RATE_LIMITED = "OPENAI_RATE_LIMITED"
OPENAI_QUOTA_EXCEEDED = "OPENAI_QUOTA_EXCEEDED"
OPENAI_MODEL_UNAVAILABLE = "OPENAI_MODEL_UNAVAILABLE"
OPENAI_STRUCTURED_OUTPUT_INVALID = "OPENAI_STRUCTURED_OUTPUT_INVALID"
OPENAI_CONTEXT_LIMIT = "OPENAI_CONTEXT_LIMIT"
OPENAI_REQUEST_FAILED = "OPENAI_REQUEST_FAILED"

ENDPOINT_RESPONSES = "Responses API"
ENDPOINT_CHAT_COMPLETIONS = "Chat Completions"


@dataclass(frozen=True)
class SafeOpenAIDiagnostics:
    provider_error_category: str
    http_status: int | None
    openai_error_code: str | None
    rejected_parameter: str | None
    request_id: str | None
    endpoint: str
    model: str
    pipeline_stage: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OpenAIProviderError(RuntimeError):
    """Safe provider failure that never retains request content or credentials."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: SafeOpenAIDiagnostics | None = None,
        compatibility_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.diagnostics = diagnostics
        self.compatibility_failure = compatibility_failure


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


class _StructuredConnectivity(BaseModel):
    ok: bool


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StructuredParseResult(Generic[T]):
    parsed: T
    endpoint: str


@dataclass(frozen=True)
class OpenAIPreflightResult:
    configured: bool
    connected: bool
    model: str
    endpoint: str | None = None
    structured_output_verified: bool = False
    code: str | None = None
    message: str | None = None
    diagnostics: dict[str, Any] | None = None


_preflight_cache: tuple[float, str, str, str] | None = None


def get_openai_api_key() -> str:
    """Read local configuration, Streamlit secrets, or environment without displaying it."""
    local_value = str(dotenv_values(config.PROJECT_ROOT / ".env").get("OPENAI_API_KEY") or "").strip()
    if local_value:
        return local_value
    try:
        import streamlit as st

        secret_value = str(st.secrets.get("OPENAI_API_KEY", "") or "").strip()
        if secret_value:
            return secret_value
    except Exception:
        pass
    return os.getenv("OPENAI_API_KEY", "").strip()


def _client(api_key: str | None = None) -> Any:
    if OpenAI is None:
        raise OpenAIProviderError(OPENAI_NOT_CONFIGURED, "The current OpenAI Python SDK is not installed.")
    key = api_key or get_openai_api_key()
    if not key:
        raise OpenAIProviderError(OPENAI_NOT_CONFIGURED, "OpenAI is required for AI analysis, but no API key is configured.")
    return OpenAI(api_key=key)


def _body_error(exc: Exception) -> dict[str, Any]:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else body


def _request_id(exc: Exception) -> str | None:
    request_id = getattr(exc, "request_id", None)
    if request_id:
        return str(request_id)[:160]
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers and hasattr(headers, "get"):
        value = headers.get("x-request-id")
        return str(value)[:160] if value else None
    return None


def _error_code(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None)
    details = _body_error(exc)
    provider_code = str(details.get("code") or "").lower()
    message = str(exc).lower()
    if isinstance(exc, ValidationError):
        return OPENAI_STRUCTURED_OUTPUT_INVALID
    if "authentication" in name or status in {401, 403}:
        return OPENAI_AUTH_FAILED
    if "ratelimit" in name or status == 429:
        detail_text = json.dumps(details, sort_keys=True).lower()
        return OPENAI_QUOTA_EXCEEDED if "quota" in detail_text or "billing" in detail_text else OPENAI_RATE_LIMITED
    if provider_code in {"context_length_exceeded", "context_window_exceeded"} or "context length" in message:
        return OPENAI_CONTEXT_LIMIT
    if provider_code in {"model_not_found", "model_not_available"}:
        return OPENAI_MODEL_UNAVAILABLE
    if "notfound" in name or status == 404:
        return OPENAI_MODEL_UNAVAILABLE
    return OPENAI_REQUEST_FAILED


def _safe_provider_error(
    exc: Exception,
    *,
    endpoint: str = ENDPOINT_RESPONSES,
    pipeline_stage: str = "structured_generation",
) -> OpenAIProviderError:
    code = _error_code(exc)
    details = _body_error(exc)
    provider_code = str(details.get("code") or details.get("type") or "").strip() or None
    parameter = str(details.get("param") or "").strip() or None
    raw_message = str(details.get("message") or exc).lower()
    compatibility_terms = (
        "unsupported parameter",
        "unsupported endpoint",
        "does not support",
        "unknown parameter",
        "unrecognized request",
        "not supported on this endpoint",
    )
    compatibility_failure = (
        code in {OPENAI_REQUEST_FAILED, OPENAI_MODEL_UNAVAILABLE}
        and provider_code not in {"model_not_found", "model_not_available"}
        and getattr(exc, "status_code", None) in {400, 404, 405, 422}
        and (bool(parameter) or any(term in raw_message for term in compatibility_terms))
    )
    if parameter == "reasoning":
        message = "The configured model does not support the reasoning parameter."
    elif code == OPENAI_STRUCTURED_OUTPUT_INVALID:
        message = "Structured output validation failed."
    elif code == OPENAI_CONTEXT_LIMIT:
        message = "The request exceeded the model context limit."
    else:
        message = {
            OPENAI_AUTH_FAILED: "OpenAI authentication failed.",
            OPENAI_RATE_LIMITED: "OpenAI rate limit was reached.",
            OPENAI_QUOTA_EXCEEDED: "OpenAI quota or billing is unavailable.",
            OPENAI_MODEL_UNAVAILABLE: "The configured OpenAI model is unavailable to this account.",
            OPENAI_REQUEST_FAILED: "The OpenAI request failed. Retry the analysis later.",
        }[code]
    diagnostics = SafeOpenAIDiagnostics(
        provider_error_category=code,
        http_status=getattr(exc, "status_code", None),
        openai_error_code=provider_code,
        rejected_parameter=parameter,
        request_id=_request_id(exc),
        endpoint=endpoint,
        model=config.OPENAI_MODEL,
        pipeline_stage=pipeline_stage,
    )
    return OpenAIProviderError(
        code,
        message,
        diagnostics=diagnostics,
        compatibility_failure=compatibility_failure,
    )


def _parsed_response(response: Any, schema: type[T], *, endpoint: str, stage: str) -> T:
    parsed = getattr(response, "output_parsed", None)
    if endpoint == ENDPOINT_CHAT_COMPLETIONS:
        choices = getattr(response, "choices", [])
        parsed = getattr(getattr(choices[0], "message", None), "parsed", None) if choices else None
    if isinstance(parsed, schema):
        return parsed
    output_text = getattr(response, "output_text", "")
    try:
        return schema.model_validate_json(output_text)
    except Exception as exc:
        raise _safe_provider_error(exc, endpoint=endpoint, pipeline_stage=stage) from exc


def _responses_request(
    *,
    client: Any,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    max_output_tokens: int,
    pipeline_stage: str,
) -> StructuredParseResult[T]:
    kwargs: dict[str, Any] = {
        "model": config.OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
        ],
        "text_format": schema,
        "store": False,
        "max_output_tokens": max_output_tokens,
    }
    if config.OPENAI_USE_REASONING:
        kwargs["reasoning"] = {"effort": config.OPENAI_REASONING_EFFORT}
    try:
        response = client.responses.parse(**kwargs)
        return StructuredParseResult(
            _parsed_response(response, schema, endpoint=ENDPOINT_RESPONSES, stage=pipeline_stage),
            ENDPOINT_RESPONSES,
        )
    except OpenAIProviderError:
        raise
    except Exception as exc:
        raise _safe_provider_error(exc, endpoint=ENDPOINT_RESPONSES, pipeline_stage=pipeline_stage) from exc


def _chat_completions_request(
    *,
    client: Any,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    pipeline_stage: str,
) -> StructuredParseResult[T]:
    try:
        response = client.chat.completions.parse(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
            ],
            response_format=schema,
        )
        return StructuredParseResult(
            _parsed_response(response, schema, endpoint=ENDPOINT_CHAT_COMPLETIONS, stage=pipeline_stage),
            ENDPOINT_CHAT_COMPLETIONS,
        )
    except OpenAIProviderError:
        raise
    except Exception as exc:
        raise _safe_provider_error(exc, endpoint=ENDPOINT_CHAT_COMPLETIONS, pipeline_stage=pipeline_stage) from exc


def structured_parse(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    client: Any | None = None,
    max_output_tokens: int = 4000,
    api_mode: str | None = None,
    pipeline_stage: str = "structured_generation",
) -> StructuredParseResult[T]:
    """Parse Pydantic output through the configured OpenAI compatibility path."""
    active_client = client or _client()
    mode = (api_mode or config.OPENAI_API_MODE).strip().lower()
    if mode == "chat_completions":
        return _chat_completions_request(
            client=active_client,
            system_prompt=system_prompt,
            user_payload=user_payload,
            schema=schema,
            pipeline_stage=pipeline_stage,
        )
    try:
        return _responses_request(
            client=active_client,
            system_prompt=system_prompt,
            user_payload=user_payload,
            schema=schema,
            max_output_tokens=max_output_tokens,
            pipeline_stage=pipeline_stage,
        )
    except OpenAIProviderError as responses_error:
        if mode != "auto" or not responses_error.compatibility_failure:
            raise
        try:
            return _chat_completions_request(
                client=active_client,
                system_prompt=system_prompt,
                user_payload=user_payload,
                schema=schema,
                pipeline_stage=pipeline_stage,
            )
        except OpenAIProviderError as chat_error:
            failure = OpenAIProviderError(
                chat_error.code,
                "The Responses endpoint was unavailable and the compatibility endpoint also failed.",
                diagnostics=chat_error.diagnostics,
            )
            raise failure from chat_error


def responses_parse(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    client: Any | None = None,
    max_output_tokens: int = 4000,
) -> T:
    """Compatibility wrapper returning only the validated Pydantic object."""
    return structured_parse(
        system_prompt=system_prompt,
        user_payload=user_payload,
        schema=schema,
        client=client,
        max_output_tokens=max_output_tokens,
    ).parsed


def preflight_openai(*, force: bool = False, client: Any | None = None) -> OpenAIPreflightResult:
    """Verify model visibility and the production structured-output path without documents."""
    global _preflight_cache
    now = time.monotonic()
    cache_key = (config.OPENAI_MODEL, config.OPENAI_API_MODE)
    if (
        not force
        and _preflight_cache
        and _preflight_cache[1:3] == cache_key
        and now - _preflight_cache[0] < config.OPENAI_PREFLIGHT_CACHE_SECONDS
    ):
        return OpenAIPreflightResult(True, True, config.OPENAI_MODEL, _preflight_cache[3], True)
    try:
        active_client = client or _client()
        active_client.models.retrieve(config.OPENAI_MODEL)
        result = structured_parse(
            system_prompt="Return the requested connectivity object.",
            user_payload={"ok": True},
            schema=_StructuredConnectivity,
            client=active_client,
            max_output_tokens=64,
            pipeline_stage="structured_preflight",
        )
        if not result.parsed.ok:
            raise OpenAIProviderError(OPENAI_STRUCTURED_OUTPUT_INVALID, "Structured output validation failed.")
        _preflight_cache = (now, config.OPENAI_MODEL, config.OPENAI_API_MODE, result.endpoint)
        return OpenAIPreflightResult(True, True, config.OPENAI_MODEL, result.endpoint, True)
    except OpenAIProviderError as exc:
        return OpenAIPreflightResult(
            exc.code != OPENAI_NOT_CONFIGURED,
            False,
            config.OPENAI_MODEL,
            exc.diagnostics.endpoint if exc.diagnostics else None,
            False,
            exc.code,
            exc.safe_message,
            exc.diagnostics.to_dict() if exc.diagnostics else None,
        )
    except Exception as exc:
        error = _safe_provider_error(exc, endpoint=ENDPOINT_RESPONSES, pipeline_stage="model_lookup")
        return OpenAIPreflightResult(
            True,
            False,
            config.OPENAI_MODEL,
            error.diagnostics.endpoint if error.diagnostics else None,
            False,
            error.code,
            error.safe_message,
            error.diagnostics.to_dict() if error.diagnostics else None,
        )


def _validate_citations(review: ClosedCorpusAIReview, evidence: list[dict[str, Any]]) -> None:
    def canonical_locator(value: str) -> str:
        try:
            return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return value

    allowed_ids = {str(item["evidence_id"]) for item in evidence}
    locators_by_id = {
        str(item["evidence_id"]): canonical_locator(str(item.get("source_locator_json") or ""))
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
        abstain = getattr(claim, "abstain", False)
        if isinstance(claim, ExtractedClaim) and claim.material and not abstain and not claim.evidence_ids:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned an unsupported material claim.")
        if isinstance(claim, ModelThesisItem) and not abstain and not claim.evidence_ids:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned an unsupported material claim.")
        if isinstance(claim, RecommendationNarrative) and not claim.evidence_ids and any(
            str(getattr(claim, field) or "").strip()
            for field in ("rationale", "why_not_buy", "why_not_hold", "why_not_sell")
        ) and not claim.abstention_reason:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned recommendation narrative without evidence.")
        if claim.evidence_ids and not claim.citation_locators:
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned evidence IDs without document locators.")
        returned_locators = {canonical_locator(locator) for locator in claim.citation_locators}
        if returned_locators and any(locator not in locators_by_id.values() for locator in returned_locators):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned a citation locator outside the selected package.")
        expected_locators = {locators_by_id[identifier] for identifier in claim.evidence_ids if identifier in locators_by_id}
        if expected_locators and not expected_locators.issubset(returned_locators):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI omitted an exact locator for selected evidence.")


def _attach_trusted_citation_locators(review: ClosedCorpusAIReview, evidence: list[dict[str, Any]]) -> None:
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
        if any(identifier not in locators_by_id for identifier in claim.evidence_ids):
            claim.evidence_ids = []
            claim.citation_locators = []
            if isinstance(claim, RecommendationNarrative):
                claim.rationale = ""
                claim.why_not_buy = ""
                claim.why_not_hold = ""
                claim.why_not_sell = ""
                claim.abstention_reason = "The model response referenced evidence outside the selected package."
            else:
                claim.abstain = True
            continue
        claim.citation_locators = [locators_by_id[identifier] for identifier in claim.evidence_ids]


def run_closed_corpus_ai_review(
    *,
    version: dict[str, Any],
    processing_run_id: str,
    evidence: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    db_path: str,
    client: Any | None = None,
    with_endpoint: bool = False,
) -> ClosedCorpusAIReview | StructuredParseResult[ClosedCorpusAIReview]:
    """Draft narrative only from verified evidence and deterministic metrics."""
    del db_path
    if version.get("status") != config.VERSION_STATUS_LOCKED:
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis requires a locked package version.")
    if any(item.get("version_id") not in {None, version["version_id"]} for item in evidence):
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis received evidence outside the selected package version.")
    verified_evidence = [
        item
        for item in evidence
        if item.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
    ]
    verified_evidence.sort(
        key=lambda item: (
            item.get("extraction_method") != "OPENAI_STRUCTURED",
            item.get("value") is None,
            str(item.get("evidence_id") or ""),
        )
    )
    selected_evidence: list[dict[str, Any]] = []
    seen_evidence: set[tuple[str, str]] = set()
    for item in verified_evidence:
        fingerprint = (str(item.get("claim_text") or "").casefold(), str(item.get("source_locator_json") or ""))
        if fingerprint in seen_evidence:
            continue
        seen_evidence.add(fingerprint)
        selected_evidence.append(item)
        if len(selected_evidence) >= config.OPENAI_MAX_NARRATIVE_EVIDENCE:
            break
    evidence_payload = [
        {
            "evidence_id": item["evidence_id"],
            "evidence_type": item.get("evidence_type"),
            "claim_text": item.get("claim_text"),
            "source_locator_json": item.get("source_locator_json"),
            "verification_status": item.get("verification_status"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "period": item.get("period"),
        }
        for item in selected_evidence
    ]
    payload = {
        "locked_version_id": version["version_id"],
        "processing_run_id": processing_run_id,
        "verified_evidence": evidence_payload,
        "deterministic_metrics": metrics,
        "conflicts": conflicts[: config.OPENAI_MAX_NARRATIVE_CONFLICTS],
    }
    system_prompt = (
        "You are the closed-corpus research analyst. Use only verified_evidence, deterministic_metrics, and conflicts in the payload. "
        "Do not extract new evidence, use web search, external tools, model memory, live prices, or facts from another package. "
        "Leave extracted_claims empty. Every material narrative claim must include evidence_ids from the payload. "
        "Leave citation_locators empty because the application attaches trusted locators from those evidence IDs. "
        "Abstain when evidence is insufficient. Do not perform trusted arithmetic or alter deterministic recommendation thresholds."
    )
    result = structured_parse(
        system_prompt=system_prompt,
        user_payload=payload,
        schema=ClosedCorpusAIReview,
        client=client,
        pipeline_stage="narrative_generation",
    )
    _attach_trusted_citation_locators(result.parsed, evidence)
    _validate_citations(result.parsed, evidence)
    return result if with_endpoint else result.parsed
