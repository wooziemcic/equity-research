from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from app import config
from app.services.document_processing import sha256_text
from app.services.openai_evidence_service import (
    OpenAIEvidenceBatch,
    OpenAIEvidenceValidationError,
    OpenAIExtractedEvidence,
    create_verified_openai_evidence,
    run_openai_evidence_extraction,
)
from app.services.openai_service import (
    ENDPOINT_CHAT_COMPLETIONS,
    ENDPOINT_RESPONSES,
    OPENAI_AUTH_FAILED,
    OPENAI_QUOTA_EXCEEDED,
    OPENAI_RATE_LIMITED,
    OpenAIProviderError,
    _safe_provider_error,
    preflight_openai,
    structured_parse,
)
from app.services.package_service import PackageInput, create_package
from app.services.research_workflow_service import planned_collection_preview
from app.utils import database


class _TinyResult(BaseModel):
    ok: bool


class _ProviderFailure(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = "req-safe-test"
        self.body = {"error": {"code": code, "message": message, "param": param}}


class _FakeModels:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def retrieve(self, model: str) -> object:
        self.calls.append(model)
        return object()


class _FakeResponses:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        schema = kwargs["text_format"]
        if schema.__name__ == "_StructuredConnectivity":
            parsed = schema(ok=True)
        elif schema is OpenAIEvidenceBatch:
            parsed = schema(items=[])
        else:
            parsed = schema(ok=True)
        return type("Response", (), {"output_parsed": parsed, "output_text": ""})()


class _FakeChatCompletions:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        parsed = kwargs["response_format"](ok=True)
        message = type("Message", (), {"parsed": parsed})()
        return type("ChatResponse", (), {"choices": [type("Choice", (), {"message": message})()]})()


class _FakeClient:
    def __init__(self, responses_failure: Exception | None = None, chat_failure: Exception | None = None) -> None:
        self.models = _FakeModels()
        self.responses = _FakeResponses(responses_failure)
        self.chat = type("Chat", (), {"completions": _FakeChatCompletions(chat_failure)})()


def test_gpt_41_mini_omits_reasoning_unless_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4.1-mini")
    monkeypatch.setattr(config, "OPENAI_USE_REASONING", False)
    structured_parse(system_prompt="test", user_payload={"ok": True}, schema=_TinyResult, client=client)
    assert "reasoning" not in client.responses.calls[-1]

    monkeypatch.setattr(config, "OPENAI_USE_REASONING", True)
    monkeypatch.setattr(config, "OPENAI_REASONING_EFFORT", "low")
    structured_parse(system_prompt="test", user_payload={"ok": True}, schema=_TinyResult, client=client)
    assert client.responses.calls[-1]["reasoning"] == {"effort": "low"}


def test_structured_preflight_uses_production_responses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4.1-mini")
    monkeypatch.setattr(config, "OPENAI_API_MODE", "auto")
    result = preflight_openai(force=True, client=client)
    assert result.connected
    assert result.structured_output_verified
    assert result.endpoint == ENDPOINT_RESPONSES
    assert client.models.calls == ["gpt-4.1-mini"]
    assert client.responses.calls[0]["max_output_tokens"] == 64
    assert client.chat.completions.calls == []


def test_auto_falls_back_only_for_endpoint_or_parameter_incompatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OPENAI_API_MODE", "auto")
    incompatible = _ProviderFailure(400, "unsupported_parameter", "Unsupported parameter for this endpoint", param="text_format")
    client = _FakeClient(incompatible)
    result = structured_parse(system_prompt="test", user_payload={"ok": True}, schema=_TinyResult, client=client)
    assert result.endpoint == ENDPOINT_CHAT_COMPLETIONS
    assert len(client.responses.calls) == 1
    assert len(client.chat.completions.calls) == 1
    assert client.chat.completions.calls[0]["response_format"] is _TinyResult


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (_ProviderFailure(401, "invalid_api_key", "Authentication failed"), OPENAI_AUTH_FAILED),
        (_ProviderFailure(429, "rate_limit_exceeded", "Rate limit reached"), OPENAI_RATE_LIMITED),
        (_ProviderFailure(429, "insufficient_quota", "Billing quota exceeded"), OPENAI_QUOTA_EXCEEDED),
    ],
)
def test_auto_does_not_fallback_for_auth_rate_or_quota(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    monkeypatch.setattr(config, "OPENAI_API_MODE", "auto")
    client = _FakeClient(failure)
    with pytest.raises(OpenAIProviderError) as caught:
        structured_parse(system_prompt="test", user_payload={"ok": True}, schema=_TinyResult, client=client)
    assert caught.value.code == expected_code
    assert client.chat.completions.calls == []


def test_safe_diagnostics_exclude_credentials_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4.1-mini")
    failure = _ProviderFailure(400, "unsupported_parameter", "Unsupported parameter", param="reasoning")
    safe = _safe_provider_error(failure, endpoint=ENDPOINT_RESPONSES, pipeline_stage="evidence_extraction")
    serialized = json.dumps(safe.diagnostics.to_dict(), sort_keys=True)
    assert set(safe.diagnostics.to_dict()) == {
        "provider_error_category",
        "http_status",
        "openai_error_code",
        "rejected_parameter",
        "request_id",
        "endpoint",
        "model",
        "pipeline_stage",
    }
    assert "authorization" not in serialized.lower()
    assert "document" not in serialized.lower()
    assert safe.diagnostics.rejected_parameter == "reasoning"


def _locked_chunk_database(db_path: Path) -> tuple[dict, dict, dict]:
    package = create_package(PackageInput("QXO", "Common Equity", date(2026, 7, 13), 3, ""), db_path=db_path)
    version = database.create_package_version(
        {
            "version_id": "PV-QXO-STABILIZATION",
            "parent_package_id": package["package_id"],
            "version_number": 1,
            "ticker": "QXO",
            "company_name": "QXO, Inc.",
            "security_type": "Common Equity",
            "research_cutoff_date": "2026-07-13",
            "status": config.VERSION_STATUS_LOCKED,
            "created_by": "test",
        },
        db_path=db_path,
    )
    run = database.create_processing_run(
        {
            "processing_run_id": "RUN-PROC-QXO-STABILIZATION",
            "version_id": version["version_id"],
            "package_id": package["package_id"],
            "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "embedding_config_json": "{}",
            "ocr_config_json": "{}",
            "retrieval_config_json": "{}",
            "started_at": database.utc_now_iso(),
            "completed_at": database.utc_now_iso(),
            "total_documents": 1,
            "successful_documents": 1,
            "partial_documents": 0,
            "failed_documents": 0,
            "pages_processed": 1,
            "tables_detected": 0,
            "sheets_processed": 0,
            "chunks_created": 1,
            "evidence_records_created": 0,
            "warnings_json": "[]",
            "errors_json": "[]",
            "created_by": "test",
            "status": config.PROCESSING_STATUS_COMPLETED,
        },
        db_path=db_path,
    )
    text = "Revenue for fiscal 2026 was 125 million dollars and operating margin improved."
    chunk = database.create_document_chunk(
        {
            "chunk_id": "CHK-QXO-1",
            "processing_run_id": run["processing_run_id"],
            "version_id": version["version_id"],
            "version_document_id": "VDOC-QXO-1",
            "page_number": 1,
            "sheet_name": None,
            "row_range": None,
            "section_heading": "Results",
            "chunk_index": 0,
            "chunk_text": text,
            "character_count": len(text),
            "token_estimate": 20,
            "extraction_method": "HTML_TEXT",
            "source_locator_json": json.dumps({"display_title": "QXO filing", "page_number": 1}),
            "chunk_hash": sha256_text(text),
            "duplicate_group_id": None,
            "created_at": database.utc_now_iso(),
        },
        db_path=db_path,
    )
    return version, run, chunk


def test_openai_evidence_requires_trusted_chunk_quote_and_version_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.db"
    version, run, chunk = _locked_chunk_database(db_path)
    valid = OpenAIExtractedEvidence(
        chunk_id=chunk["chunk_id"],
        verbatim_quote="Revenue for fiscal 2026 was 125 million dollars",
        claim_text="Fiscal 2026 revenue was 125 million dollars.",
        evidence_type="REPORTED_FINANCIAL",
        metric_name="revenue",
        numeric_value=125,
        unit="million",
        currency="USD",
        period="2026",
    )
    created, was_created = create_verified_openai_evidence(
        valid,
        selected_chunks={chunk["chunk_id"]: chunk},
        processing_run_id=run["processing_run_id"],
        version_id=version["version_id"],
        db_path=db_path,
    )
    repeated, repeated_created = create_verified_openai_evidence(
        valid,
        selected_chunks={chunk["chunk_id"]: chunk},
        processing_run_id=run["processing_run_id"],
        version_id=version["version_id"],
        db_path=db_path,
    )
    assert was_created and not repeated_created
    assert created["evidence_id"] == repeated["evidence_id"]
    assert created["extraction_method"] == "OPENAI_STRUCTURED"
    assert len(database.list_evidence_records(run["processing_run_id"], db_path=db_path)) == 1

    with pytest.raises(OpenAIEvidenceValidationError, match="invented"):
        create_verified_openai_evidence(
            valid.model_copy(update={"chunk_id": "CHK-INVENTED"}),
            selected_chunks={chunk["chunk_id"]: chunk},
            processing_run_id=run["processing_run_id"],
            version_id=version["version_id"],
            db_path=db_path,
        )
    with pytest.raises(OpenAIEvidenceValidationError, match="quote"):
        create_verified_openai_evidence(
            valid.model_copy(update={"verbatim_quote": "A quote that is not in the source"}),
            selected_chunks={chunk["chunk_id"]: chunk},
            processing_run_id=run["processing_run_id"],
            version_id=version["version_id"],
            db_path=db_path,
        )
    with pytest.raises(OpenAIEvidenceValidationError, match="version"):
        create_verified_openai_evidence(
            valid,
            selected_chunks={chunk["chunk_id"]: {**chunk, "version_id": "PV-OTHER"}},
            processing_run_id=run["processing_run_id"],
            version_id=version["version_id"],
            db_path=db_path,
        )


def test_zero_selected_evidence_is_warning_not_provider_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "zero.db"
    version, run, _chunk = _locked_chunk_database(db_path)
    with database.get_connection(db_path) as connection:
        connection.execute("DELETE FROM document_chunks WHERE processing_run_id = ?", (run["processing_run_id"],))
    result = run_openai_evidence_extraction(version=version, processing_run_id=run["processing_run_id"], db_path=db_path)
    assert result.evidence_created == 0
    assert result.warnings


def test_pipeline_source_orders_openai_evidence_before_metrics() -> None:
    source = (Path(__file__).parents[1] / "app" / "services" / "analysis_pipeline.py").read_text(encoding="utf-8")
    assert source.index("run_openai_evidence_extraction(", source.index("def create_analysis_run")) < source.index(
        "calculate_metrics(", source.index("def create_analysis_run")
    )


def test_primary_css_and_collection_preview_are_structured() -> None:
    root = Path(__file__).parents[1]
    css = (root / "app" / "styles" / "main.css").read_text(encoding="utf-8")
    page = (root / "app" / "pages" / "0_Research_Workspace.py").read_text(encoding="utf-8")
    assert 'button[kind="primary"]:not(:disabled)' in css
    assert 'button[data-testid="stBaseButton-primary"]:not(:disabled)' in css
    assert "color: #ffffff !important;" in css
    assert 'button[data-testid="stBaseButton-primary"]:disabled' in css
    assert "collection-plan-row" in page
    assert "st.write(plan_rows)" not in page
    plan = planned_collection_preview(["Earnings releases"], ir_url="https://investors.example.test")
    assert len(plan) == 9
    assert all(set(row) == {"source", "collection_method", "selected", "ir_url_available"} for row in plan)

