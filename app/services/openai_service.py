from __future__ import annotations

import json
import os
import re
import secrets
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
OPENAI_OUTPUT_INCOMPLETE = "OPENAI_OUTPUT_INCOMPLETE"
OPENAI_OUTPUT_REFUSED = "OPENAI_OUTPUT_REFUSED"
OPENAI_OUTPUT_EMPTY = "OPENAI_OUTPUT_EMPTY"
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
    response_status: str | None = None
    incomplete_reason: str | None = None
    refusal_present: bool = False
    output_parsed_available: bool = False
    output_text_available: bool = False
    output_tokens: int | None = None
    validation_fields: tuple[str, ...] = ()
    response_metadata_captured: bool = False

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        captured = values.pop("response_metadata_captured")
        if not captured:
            for key in (
                "response_status", "incomplete_reason", "refusal_present", "output_parsed_available",
                "output_text_available", "output_tokens", "validation_fields",
            ):
                values.pop(key)
        return values


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
    rationale: str = ""
    why_not_buy: str = ""
    why_not_hold: str = ""
    why_not_sell: str = ""
    abstention_reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    citation_locators: list[str] = Field(default_factory=list)


class ClosedCorpusAIReview(BaseModel):
    extracted_claims: list[ExtractedClaim] = Field(default_factory=list)
    thesis_items: list[ModelThesisItem] = Field(default_factory=list)
    conflict_explanations: list[ExtractedClaim] = Field(default_factory=list)
    recommendation: RecommendationNarrative


class ThesisRiskItem(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    concise_claim: str = Field(min_length=1, max_length=600)
    confidence: str = Field(default="INSUFFICIENT", max_length=32)


class ThesisRiskResponse(BaseModel):
    supporting_items: list[ThesisRiskItem] = Field(default_factory=list, max_length=12)
    risk_items: list[ThesisRiskItem] = Field(default_factory=list, max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=8)


class RecommendationResponse(BaseModel):
    recommendation_summary: str = Field(min_length=1, max_length=1200)
    why_provisional: str = Field(default="", max_length=600)
    primary_positive: str = Field(default="", max_length=600)
    primary_risk: str = Field(default="", max_length=600)
    abstention_reason: str = Field(default="", max_length=600)


class _StructuredConnectivity(BaseModel):
    ok: bool


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StructuredParseResult(Generic[T]):
    parsed: T
    endpoint: str
    diagnostics: dict[str, Any] | None = None
    attempt_number: int = 1


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


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _usage_metadata(response: Any) -> dict[str, int]:
    usage = _value(response, "usage", {}) or {}
    details = _value(usage, "input_tokens_details", {}) or _value(usage, "prompt_tokens_details", {}) or {}
    input_tokens = int(_value(usage, "input_tokens", _value(usage, "prompt_tokens", 0)) or 0)
    output_tokens = int(_value(usage, "output_tokens", _value(usage, "completion_tokens", 0)) or 0)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": int(_value(details, "cached_tokens", 0) or 0),
        "output_tokens": output_tokens,
        "total_tokens": int(_value(usage, "total_tokens", input_tokens + output_tokens) or input_tokens + output_tokens),
    }


def _output_state(response: Any) -> dict[str, Any]:
    status = str(_value(response, "status", "") or "") or None
    incomplete = _value(response, "incomplete_details", {}) or {}
    reason = str(_value(incomplete, "reason", "") or "") or None
    parsed = _value(response, "output_parsed")
    output_text = str(_value(response, "output_text", "") or "")
    refusal = bool(_value(response, "refusal", None))
    choices = _value(response, "choices", []) or []
    if choices:
        message = _value(choices[0], "message", {}) or {}
        refusal = refusal or bool(_value(message, "refusal", None))
        parsed = parsed or _value(message, "parsed")
        output_text = output_text or str(_value(message, "content", "") or "")
    return {
        "response_status": status,
        "incomplete_reason": reason,
        "refusal_present": refusal,
        "output_parsed_available": parsed is not None,
        "output_text_available": bool(output_text.strip()),
        "output_tokens": _usage_metadata(response)["output_tokens"],
    }


def _response_error(
    code: str, message: str, *, response: Any, endpoint: str, stage: str,
    validation_fields: tuple[str, ...] = (),
) -> OpenAIProviderError:
    state = _output_state(response)
    return OpenAIProviderError(
        code,
        message,
        diagnostics=SafeOpenAIDiagnostics(
            provider_error_category=code, http_status=_value(response, "status_code", None), openai_error_code=None,
            rejected_parameter=None, request_id=str(_value(response, "id", "") or "")[:160] or None,
            endpoint=endpoint, model=config.OPENAI_MODEL, pipeline_stage=stage,
            validation_fields=validation_fields, response_metadata_captured=True, **state,
        ),
    )


def _record_usage(
    response: Any, *, endpoint: str, pipeline_stage: str, attempt_number: int,
    usage_context: dict[str, Any] | None, db_path: str | None,
) -> None:
    if not usage_context or not db_path:
        return
    from app.utils import database

    usage = _usage_metadata(response)
    pricing = config.OPENAI_MODEL_PRICING.get(config.OPENAI_MODEL, {})
    uncached = max(0, usage["input_tokens"] - usage["cached_input_tokens"])
    cost = (
        uncached * float(pricing.get("input", 0))
        + usage["cached_input_tokens"] * float(pricing.get("cached_input", 0))
        + usage["output_tokens"] * float(pricing.get("output", 0))
    ) / 1_000_000
    state = _output_state(response)
    database.create_openai_usage_record(
        {
            "usage_id": f"OAIU-{secrets.token_hex(8).upper()}",
            "analysis_run_id": usage_context.get("analysis_run_id"),
            "processing_run_id": usage_context.get("processing_run_id"),
            "workflow_run_id": usage_context.get("workflow_run_id"),
            "attempt_id": usage_context.get("attempt_id"),
            "pipeline_stage": pipeline_stage, "attempt_number": attempt_number,
            "model": config.OPENAI_MODEL, "endpoint": endpoint,
            **usage, "estimated_cost_usd": round(cost, 8),
            "output_status": state["response_status"] or ("PARSED" if state["output_parsed_available"] else "UNKNOWN"),
            "created_at": database.utc_now_iso(),
        },
        db_path=db_path,
    )


def _parsed_response(response: Any, schema: type[T], *, endpoint: str, stage: str) -> T:
    state = _output_state(response)
    if state["refusal_present"]:
        raise _response_error(OPENAI_OUTPUT_REFUSED, "OpenAI refused the structured-output request.", response=response, endpoint=endpoint, stage=stage)
    if state["response_status"] == "incomplete" or state["incomplete_reason"]:
        raise _response_error(OPENAI_OUTPUT_INCOMPLETE, "OpenAI returned an incomplete structured output.", response=response, endpoint=endpoint, stage=stage)
    parsed = getattr(response, "output_parsed", None)
    if endpoint == ENDPOINT_CHAT_COMPLETIONS:
        choices = getattr(response, "choices", [])
        parsed = getattr(getattr(choices[0], "message", None), "parsed", None) if choices else None
    if isinstance(parsed, schema):
        return parsed
    output_text = getattr(response, "output_text", "")
    if endpoint == ENDPOINT_CHAT_COMPLETIONS and not output_text:
        choices = getattr(response, "choices", [])
        output_text = getattr(getattr(choices[0], "message", None), "content", "") if choices else ""
    if not str(output_text or "").strip():
        raise _response_error(OPENAI_OUTPUT_EMPTY, "OpenAI returned no structured output.", response=response, endpoint=endpoint, stage=stage)
    try:
        return schema.model_validate_json(output_text)
    except ValidationError as exc:
        fields = tuple(sorted({".".join(str(part) for part in error.get("loc", ()))[:120] for error in exc.errors()}))
        raise _response_error(
            OPENAI_STRUCTURED_OUTPUT_INVALID, "Structured output validation failed.", response=response,
            endpoint=endpoint, stage=stage, validation_fields=fields,
        ) from exc


def _responses_request(
    *,
    client: Any,
    system_prompt: str,
    user_payload: dict[str, Any],
    schema: type[T],
    max_output_tokens: int,
    pipeline_stage: str,
    attempt_number: int = 1,
    usage_context: dict[str, Any] | None = None,
    db_path: str | None = None,
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
        _record_usage(response, endpoint=ENDPOINT_RESPONSES, pipeline_stage=pipeline_stage, attempt_number=attempt_number, usage_context=usage_context, db_path=db_path)
        return StructuredParseResult(
            _parsed_response(response, schema, endpoint=ENDPOINT_RESPONSES, stage=pipeline_stage),
            ENDPOINT_RESPONSES,
            attempt_number=attempt_number,
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
    attempt_number: int = 1,
    usage_context: dict[str, Any] | None = None,
    db_path: str | None = None,
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
        _record_usage(response, endpoint=ENDPOINT_CHAT_COMPLETIONS, pipeline_stage=pipeline_stage, attempt_number=attempt_number, usage_context=usage_context, db_path=db_path)
        return StructuredParseResult(
            _parsed_response(response, schema, endpoint=ENDPOINT_CHAT_COMPLETIONS, stage=pipeline_stage),
            ENDPOINT_CHAT_COMPLETIONS,
            attempt_number=attempt_number,
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
    attempt_number: int = 1,
    usage_context: dict[str, Any] | None = None,
    db_path: str | None = None,
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
            attempt_number=attempt_number,
            usage_context=usage_context,
            db_path=db_path,
        )
    try:
        return _responses_request(
            client=active_client,
            system_prompt=system_prompt,
            user_payload=user_payload,
            schema=schema,
            max_output_tokens=max_output_tokens,
            pipeline_stage=pipeline_stage,
            attempt_number=attempt_number,
            usage_context=usage_context,
            db_path=db_path,
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
                attempt_number=attempt_number,
                usage_context=usage_context,
                db_path=db_path,
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
    analysis_run_id: str | None = None,
    attempt_id: str | None = None,
    scorecard_result: dict[str, Any] | None = None,
) -> ClosedCorpusAIReview | StructuredParseResult[ClosedCorpusAIReview]:
    """Run bounded thesis and recommendation calls against selected locked-corpus candidates."""
    from app.services.narrative_candidate_service import CandidateSelection, select_narrative_candidates

    if version.get("status") != config.VERSION_STATUS_LOCKED:
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis requires a locked package version.")
    if any(item.get("version_id") not in {None, version["version_id"]} for item in evidence):
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "AI analysis received evidence outside the selected package version.")
    active_attempt = attempt_id or f"RECAT-{secrets.token_hex(8).upper()}"
    active_analysis = analysis_run_id or f"UNBOUND-{version['version_id']}"
    selection = select_narrative_candidates(
        attempt_id=active_attempt, analysis_run_id=active_analysis, version_id=version["version_id"],
        processing_run_id=processing_run_id, evidence=evidence, metrics=metrics, conflicts=conflicts,
        db_path=db_path,
    )
    usage_context = {
        "analysis_run_id": analysis_run_id, "processing_run_id": processing_run_id, "attempt_id": active_attempt,
    }

    def record_model_attempt(stage: str, number: int, candidate_count: int, status: str, endpoint: str | None) -> None:
        if not analysis_run_id:
            return
        from app.utils import database

        database.create_recommendation_stage_event(
            {
                "attempt_id": active_attempt, "analysis_run_id": analysis_run_id,
                "stage_name": f"{stage} provider attempt", "status": status,
                "detail_json": json.dumps(
                    {
                        "attempt_number": number, "endpoint": endpoint or ENDPOINT_RESPONSES,
                        "model": config.OPENAI_MODEL, "candidate_count": candidate_count,
                        "output_status": status,
                    },
                    sort_keys=True,
                ),
                "created_at": database.utc_now_iso(),
            },
            db_path=db_path,
        )

    def thesis_payload(selected: CandidateSelection, *, repair: bool) -> dict[str, Any]:
        return {
            "supporting_candidates": [item.model_payload() for item in selected.supporting],
            "risk_candidates": [item.model_payload() for item in selected.risks],
            "valid_unresolved_conflicts": list(selected.conflicts),
            "instruction": "Return only concise fields matching the schema." if repair else "Synthesize only supplied candidate IDs.",
        }

    retryable = {OPENAI_OUTPUT_INCOMPLETE, OPENAI_STRUCTURED_OUTPUT_INVALID, OPENAI_OUTPUT_EMPTY}
    thesis_result: StructuredParseResult[ThesisRiskResponse] | None = None
    active_selection = selection
    for number in (1, 2):
        try:
            thesis_result = structured_parse(
                system_prompt=(
                    "Use only the supplied locked-corpus candidates. Every item must reference exactly one supplied candidate_id. "
                    "Do not add facts, numbers, evidence identifiers, citations, or external information. Abstain through missing_information."
                ),
                user_payload=thesis_payload(active_selection, repair=number == 2), schema=ThesisRiskResponse,
                client=client, max_output_tokens=4000, pipeline_stage="thesis_risk_generation",
                attempt_number=number, usage_context=usage_context, db_path=db_path,
            )
            record_model_attempt(
                "Thesis", number, len(active_selection.supporting) + len(active_selection.risks),
                "COMPLETED", thesis_result.endpoint,
            )
            break
        except OpenAIProviderError as exc:
            record_model_attempt(
                "Thesis", number, len(active_selection.supporting) + len(active_selection.risks),
                exc.code, exc.diagnostics.endpoint if exc.diagnostics else None,
            )
            if number == 2 or exc.code not in retryable:
                raise
            active_selection = selection.smaller()
    if thesis_result is None:  # pragma: no cover - loop either returns or raises.
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "Thesis generation did not produce a response.")

    candidate_by_id = {item.candidate_id: item for item in (*active_selection.supporting, *active_selection.risks)}
    thesis = thesis_result.parsed
    returned_items = [*thesis.supporting_items, *thesis.risk_items]
    if any(item.candidate_id not in candidate_by_id for item in returned_items):
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned a thesis item outside the selected candidate set.")
    for item in returned_items:
        supplied = candidate_by_id[item.candidate_id]
        returned_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", item.concise_claim))
        supplied_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", supplied.concise_claim))
        if not returned_numbers.issubset(supplied_numbers):
            raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "OpenAI returned an unsupported number in thesis synthesis.")

    validated_thesis = [
        {"candidate_id": item.candidate_id, "claim": item.concise_claim, "confidence": item.confidence}
        for item in thesis.supporting_items
    ]
    validated_risks = [
        {"candidate_id": item.candidate_id, "claim": item.concise_claim, "confidence": item.confidence}
        for item in thesis.risk_items
    ]
    has_valuation = any(row.get("metric_code") in {"PRICE_TARGET", "VALUATION", "IMPLIED_VALUE"} for row in active_selection.metrics)
    has_reference = any(row.get("metric_code") == "REFERENCE_PRICE" for row in active_selection.metrics)

    def recommendation_payload(selected: CandidateSelection, *, repair: bool) -> dict[str, Any]:
        keep_ids = {item.candidate_id for item in (*selected.supporting, *selected.risks)}
        return {
            "validated_supporting_items": [row for row in validated_thesis if row["candidate_id"] in keep_ids],
            "validated_risk_items": [row for row in validated_risks if row["candidate_id"] in keep_ids],
            "latest_metrics": list(selected.metrics), "deterministic_scorecard": scorecard_result or {},
            "valuation_available": has_valuation, "reference_price_available": has_reference,
            "instruction": "Return only the five bounded narrative fields." if repair else "Draft a provisional recommendation narrative.",
        }

    recommendation_result: StructuredParseResult[RecommendationResponse] | None = None
    recommendation_selection = active_selection
    for number in (1, 2):
        try:
            recommendation_result = structured_parse(
                system_prompt=(
                    "Use only validated thesis, risks, compact metrics, and deterministic availability flags. "
                    "Do not return IDs or citations. Do not invent numbers. A summary is required and at least one of primary_positive "
                    "or primary_risk must be nonempty. Use abstention_reason when evidence is insufficient."
                ),
                user_payload=recommendation_payload(recommendation_selection, repair=number == 2),
                schema=RecommendationResponse, client=client, max_output_tokens=3000,
                pipeline_stage="recommendation_generation", attempt_number=number,
                usage_context=usage_context, db_path=db_path,
            )
            if not (recommendation_result.parsed.primary_positive.strip() or recommendation_result.parsed.primary_risk.strip()):
                raise OpenAIProviderError(OPENAI_STRUCTURED_OUTPUT_INVALID, "Recommendation omitted both its primary positive and primary risk.")
            allowed_numbers = set(
                re.findall(
                    r"(?<![A-Za-z])\d+(?:\.\d+)?%?",
                    " ".join(row["claim"] for row in (*validated_thesis, *validated_risks)),
                )
            )
            allowed_numbers.update(
                f"{float(row['value']):g}" for row in recommendation_selection.metrics if row.get("value") is not None
            )
            returned_numbers = set(
                re.findall(
                    r"(?<![A-Za-z])\d+(?:\.\d+)?%?",
                    " ".join(
                        (
                            recommendation_result.parsed.recommendation_summary,
                            recommendation_result.parsed.why_provisional,
                            recommendation_result.parsed.primary_positive,
                            recommendation_result.parsed.primary_risk,
                            recommendation_result.parsed.abstention_reason,
                        )
                    ),
                )
            )
            if not returned_numbers.issubset(allowed_numbers):
                raise OpenAIProviderError(OPENAI_STRUCTURED_OUTPUT_INVALID, "Recommendation included an unsupported number.")
            record_model_attempt(
                "Recommendation", number,
                len(recommendation_selection.supporting) + len(recommendation_selection.risks),
                "COMPLETED", recommendation_result.endpoint,
            )
            break
        except OpenAIProviderError as exc:
            record_model_attempt(
                "Recommendation", number,
                len(recommendation_selection.supporting) + len(recommendation_selection.risks),
                exc.code, exc.diagnostics.endpoint if exc.diagnostics else None,
            )
            if number == 2 or exc.code not in retryable:
                raise
            recommendation_selection = active_selection.smaller()
    if recommendation_result is None:  # pragma: no cover
        raise OpenAIProviderError(OPENAI_REQUEST_FAILED, "Recommendation generation did not produce a response.")

    response = recommendation_result.parsed
    evidence_ids = list(dict.fromkeys(candidate_by_id[item.candidate_id].evidence_id for item in returned_items))
    rationale = response.recommendation_summary
    abstention = response.abstention_reason or response.why_provisional or None
    if not has_valuation or not has_reference:
        rationale = f"ANALYST_REVIEW_REQUIRED: {rationale}"
        abstention = abstention or "Valuation or package-contained reference price is unavailable."
    review = ClosedCorpusAIReview(
        thesis_items=[
            ModelThesisItem(
                item_type="BULL_CASE" if item in thesis.supporting_items else "RISK",
                claim=item.concise_claim, evidence_ids=[candidate_by_id[item.candidate_id].evidence_id],
                confidence=item.confidence,
            )
            for item in returned_items
        ],
        recommendation=RecommendationNarrative(
            rationale=rationale, abstention_reason=abstention, evidence_ids=evidence_ids,
        ),
    )
    _attach_trusted_citation_locators(review, evidence)
    _validate_citations(review, evidence)
    diagnostics = {
        "original_evidence_count": selection.considered,
        "eligible_candidate_count": selection.eligible,
        "selected_supporting_count": len(selection.supporting),
        "selected_risk_count": len(selection.risks),
        "metric_count": len(selection.metrics), "conflict_count": len(selection.conflicts),
        "openai_call_count": thesis_result.attempt_number + recommendation_result.attempt_number,
    }
    result = StructuredParseResult(review, recommendation_result.endpoint, diagnostics=diagnostics)
    return result if with_endpoint else result.parsed
