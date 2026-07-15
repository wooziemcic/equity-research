from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import zipfile

import pytest

from app import config
from app.services.analysis_pipeline import retry_recommendation_generation
from app.services.narrative_candidate_service import candidate_fingerprint, claim_family, select_narrative_candidates
from app.services.document_download_service import create_public_documents_zip
from app.services.official_ir_service import approve_and_download_ir_material, classify_ir_material, investor_relevance
from app.services.openai_service import (
    ClosedCorpusAIReview,
    ModelThesisItem,
    OPENAI_OUTPUT_EMPTY,
    OPENAI_OUTPUT_INCOMPLETE,
    OPENAI_OUTPUT_REFUSED,
    OPENAI_STRUCTURED_OUTPUT_INVALID,
    OpenAIProviderError,
    RecommendationNarrative,
    RecommendationResponse,
    StructuredParseResult,
    ThesisRiskItem,
    ThesisRiskResponse,
    run_closed_corpus_ai_review,
    structured_parse,
)
from app.utils import database


def _evidence(
    evidence_id: str, claim: str, evidence_type: str = "reported_metric", *,
    value: float | None = 10.0, unit: str | None = "USD millions", period: str | None = "2025",
    direction: str = "POSITIVE", document: str = "VD-1", chunk: str = "CH-1",
) -> dict:
    return {
        "evidence_id": evidence_id, "version_id": "PV-1", "version_document_id": document,
        "evidence_type": evidence_type, "claim_text": claim, "metric_name": None, "value": value,
        "unit": unit, "currency": "USD", "period": period, "direction": direction,
        "source_locator_json": f'{{"chunk_id":"{chunk}","display_title":"QXO filing"}}',
        "verification_status": config.VERIFICATION_SUPPORTS, "confidence": "HIGH",
        "analyst_status": config.ANALYST_STATUS_UNREVIEWED, "extraction_method": "DETERMINISTIC",
    }


def test_candidate_filtering_is_bounded_diverse_and_audited(tmp_path: Path) -> None:
    db_path = tmp_path / "phase51.db"
    database.initialize_database(db_path)
    rows = [
        _evidence("E-REV-NEW", "Revenue increased to 10 million in 2025.", period="2025", chunk="C1"),
        _evidence("E-REV-OLD", "Revenue increased to 9 million in 2024.", value=9, period="2024", chunk="C2"),
        _evidence("E-REV-DUP", "Revenue increased to 10 million in 2025.", period="2025", chunk="C1"),
        _evidence("E-RISK", "Material integration risk could reduce earnings in 2025.", direction="NEGATIVE", chunk="C3"),
        _evidence("E-NOISY-N", "Revenue description 10", "description_numeric", chunk="C4"),
        _evidence("E-NOISY-T", "Revenue description text", "description_non_numeric", value=None, unit=None, period=None, chunk="C5"),
    ]
    for index in range(40):
        rows.append(_evidence(f"E-{index}", f"Revenue growth evidence item {index} for 2025.", value=index + 1, chunk=f"CX-{index}"))
    selection = select_narrative_candidates(
        attempt_id="ATT-1", analysis_run_id="AR-1", version_id="PV-1", processing_run_id="PR-1",
        evidence=rows,
        metrics=[
            {"metric_code": "REVENUE", "value": 9, "period": "2024"},
            {"metric_code": "REVENUE", "value": 10, "period": "2025"},
        ],
        conflicts=[], db_path=str(db_path),
    )
    assert len(selection.supporting) <= 30
    assert len(selection.risks) <= 12
    assert sum(item.claim_family == "revenue_growth" for item in selection.supporting) <= 2
    assert selection.metrics[0]["period"] == "2025"
    audit = {row["evidence_id"]: row for row in database.list_narrative_candidates("ATT-1", db_path=db_path)}
    assert audit["E-NOISY-N"]["exclusion_reason"] == "GENERIC_DESCRIPTION"
    assert audit["E-NOISY-T"]["exclusion_reason"] == "GENERIC_DESCRIPTION"
    assert audit["E-REV-DUP"]["exclusion_reason"] == "DUPLICATE_CANDIDATE_FINGERPRINT"
    assert audit["E-REV-NEW"]["rank_score"] > audit["E-REV-OLD"]["rank_score"]


class _Responses:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self.responses = _Responses(responses)


class _Tiny(SimpleNamespace):
    @classmethod
    def model_validate_json(cls, value: str):
        raise ValueError(value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (SimpleNamespace(status="incomplete", incomplete_details={"reason": "max_output_tokens"}, output_parsed=None, output_text="", usage={"output_tokens": 10}), OPENAI_OUTPUT_INCOMPLETE),
        (SimpleNamespace(status="completed", refusal="policy", output_parsed=None, output_text="", usage={}), OPENAI_OUTPUT_REFUSED),
        (SimpleNamespace(status="completed", output_parsed=None, output_text="", usage={}), OPENAI_OUTPUT_EMPTY),
    ],
)
def test_safe_output_categories_are_distinct(response: object, code: str) -> None:
    with pytest.raises(OpenAIProviderError) as raised:
        structured_parse(system_prompt="safe", user_payload={}, schema=_Tiny, client=_Client([response]), api_mode="responses")
    assert raised.value.code == code
    serialized = str(raised.value.diagnostics.to_dict())
    assert "safe" not in serialized
    assert "api_key" not in serialized.lower()


def test_split_review_retries_invalid_thesis_once_on_same_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "split.db"
    database.initialize_database(db_path)
    support = _evidence("E-S", "Revenue increased to 10 million in 2025.")
    risk = _evidence("E-R", "Material integration risk could reduce earnings in 2025.", direction="NEGATIVE", chunk="C2")
    support_id = "NC-" + candidate_fingerprint("PV-1", support, claim_family(support) or "")[:16].upper()
    risk_id = "NC-" + candidate_fingerprint("PV-1", risk, claim_family(risk) or "")[:16].upper()
    responses = [
        SimpleNamespace(status="completed", output_parsed=None, output_text='{"bad":', usage={"output_tokens": 5}),
        SimpleNamespace(
            status="completed",
            output_parsed=ThesisRiskResponse(
                supporting_items=[ThesisRiskItem(candidate_id=support_id, concise_claim=support["claim_text"], confidence="HIGH")],
                risk_items=[ThesisRiskItem(candidate_id=risk_id, concise_claim=risk["claim_text"], confidence="HIGH")],
            ),
            output_text="", usage={"output_tokens": 100},
        ),
        SimpleNamespace(
            status="completed",
            output_parsed=RecommendationResponse(
                recommendation_summary="Evidence supports a provisional view.",
                primary_positive="Revenue increased.", primary_risk="Integration risk remains.",
            ),
            output_text="", usage={"output_tokens": 80},
        ),
    ]
    client = _Client(responses)
    result = run_closed_corpus_ai_review(
        version={"version_id": "PV-1", "status": config.VERSION_STATUS_LOCKED},
        processing_run_id="PR-1", evidence=[support, risk], metrics=[], conflicts=[],
        db_path=str(db_path), client=client, with_endpoint=True, analysis_run_id="AR-1", attempt_id="ATT-1",
    )
    assert isinstance(result, StructuredParseResult)
    assert len(client.responses.calls) == 3
    assert all(call["model"] == config.OPENAI_MODEL for call in client.responses.calls)
    assert result.diagnostics["openai_call_count"] == 3
    assert result.parsed.recommendation.rationale.startswith("ANALYST_REVIEW_REQUIRED")
    assert result.parsed.recommendation.why_not_buy == ""


def test_recommendation_retry_reuses_upstream_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "retry.db"
    database.initialize_database(db_path)
    run = {"analysis_run_id": "AR-1", "version_id": "PV-1", "processing_run_id": "PR-1", "package_id": "PKG-1", "openai_diagnostics_json": "{}"}
    evidence = [_evidence("E-1", "Revenue increased to 10 million in 2025.")]
    metrics = [{"metric_code": "REVENUE", "value": 10, "period": "2025"}]
    scorecard = [{"weighted_score": 6.0}]
    review = ClosedCorpusAIReview(
        thesis_items=[ModelThesisItem(item_type="BULL_CASE", claim=evidence[0]["claim_text"], evidence_ids=["E-1"])],
        recommendation=RecommendationNarrative(rationale="Provisional view", evidence_ids=["E-1"]),
    )
    monkeypatch.setattr(database, "get_analysis_run", lambda *a, **k: run)
    monkeypatch.setattr(database, "get_package_version", lambda *a, **k: {"version_id": "PV-1", "parent_package_id": "PKG-1", "status": config.VERSION_STATUS_LOCKED})
    monkeypatch.setattr(database, "get_processing_run", lambda *a, **k: {"processing_run_id": "PR-1", "status": config.PROCESSING_STATUS_COMPLETED})
    monkeypatch.setattr(database, "list_evidence_records", lambda *a, **k: evidence)
    monkeypatch.setattr(database, "list_analysis_metrics", lambda *a, **k: metrics)
    monkeypatch.setattr(database, "list_scorecard_items", lambda *a, **k: scorecard)
    monkeypatch.setattr(database, "list_claim_conflicts", lambda *a, **k: [])
    monkeypatch.setattr(database, "update_analysis_run", lambda *a, **k: run)
    monkeypatch.setattr("app.services.analysis_pipeline._record_event", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.analysis_pipeline.run_closed_corpus_ai_review",
        lambda **kwargs: StructuredParseResult(review, "Responses API", diagnostics={"openai_call_count": 2}),
    )
    monkeypatch.setattr("app.services.analysis_pipeline.generate_thesis_items", lambda *a, **k: [{}])
    monkeypatch.setattr(
        "app.services.analysis_pipeline.generate_recommendation",
        lambda *a, **k: {"preliminary_rating": "ANALYST_REVIEW_REQUIRED", "confidence": "LOW"},
    )
    monkeypatch.setattr(
        "app.services.reporting.investment_report.generate_investment_report",
        lambda *a, **k: {"report_id": "REP-1"},
    )
    result = retry_recommendation_generation("AR-1", db_path=db_path)
    assert result["processing_run_id"] == "PR-1"
    assert result["metrics_reused"] == 1
    assert result["evidence_reused"] == 1
    assert result["report"]["report_id"] == "REP-1"


@pytest.mark.parametrize("name", ["National_New_Account_2026__3_.pdf", "customer_application.pdf", "vendor_form.pdf"])
def test_non_investor_ir_forms_are_rejected(name: str) -> None:
    relevant, reason = investor_relevance(name, f"https://go.qxo.com/forms/{name}")
    assert not relevant
    assert "Excluded" in reason
    assert classify_ir_material(name, f"https://go.qxo.com/forms/{name}") == ("Non-Investor Material", "NONE")


def test_verified_investor_materials_are_accepted() -> None:
    assert classify_ir_material("Q2 Earnings Presentation", "https://investors.qxo.com/q2-earnings.pdf") == ("Earnings Presentation", "HIGH")
    assert classify_ir_material("2025 Investor Presentation", "https://investors.qxo.com/investor-presentation.pdf") == ("Investor Presentation", "HIGH")


class _DownloadResponse:
    def __init__(self, content: bytes, content_type: str, url: str) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self.url = url
        self.history: list[object] = []


class _DownloadSession:
    def __init__(self, response: _DownloadResponse) -> None:
        self.response = response

    def get(self, url: str, **kwargs):
        del kwargs
        self.response.url = url
        return self.response


def _ir_package_and_candidate(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_id: str, url: str) -> dict:
    package = database.create_package_record(
        package_id="PKG-QXO", ticker="QXO", company_name="QXO, Inc.", security_type="Common Equity",
        status=config.STATUS_IN_PROGRESS, research_cutoff_date="2026-07-01", filing_history_years=1,
        analyst_notes="", db_path=db_path,
    )
    package["official_website_domain"] = "qxo.com"
    package["official_ir_domain"] = "investors.qxo.com"
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "IR_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    database.upsert_ir_material_candidate(
        {
            "candidate_id": candidate_id, "package_id": package["package_id"], "discovery_run_id": "IRDISC-1",
            "title": "Q2 Earnings Presentation", "source_url": url, "canonical_url": url,
            "official_domain": "investors.qxo.com", "category": "Earnings Presentation",
            "publication_date": None, "document_date": None, "mime_type": "application/pdf", "file_extension": ".pdf",
            "discovery_page": "https://investors.qxo.com", "discovery_method": "html_link",
            "confidence": "LOW", "cutoff_eligibility": "NEEDS_DATE_REVIEW",
            "download_status": "NEEDS_MANUAL_REVIEW", "selected": 0,
            "rejection_reason": "Date review required.", "created_at": database.utc_now_iso(),
        },
        db_path=db_path,
    )
    return package


def test_approved_pdf_is_validated_stored_and_enters_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "ir.db"
    database.initialize_database(db_path)
    url = "https://investors.qxo.com/q2-earnings-presentation.pdf"
    package = _ir_package_and_candidate(db_path, tmp_path, monkeypatch, "IR-1", url)
    result = approve_and_download_ir_material(
        package, "IR-1", session=_DownloadSession(_DownloadResponse(b"%PDF-1.4\nfixture", "application/pdf", url)), db_path=db_path,
    )
    assert result["final_download_result"] == "DOWNLOADED_NOW"
    candidate = database.list_ir_material_candidates(package["package_id"], db_path=db_path)[0]
    assert candidate["analyst_approved"] == 1
    assert candidate["approval_timestamp"]
    assert candidate["original_confidence"] == "LOW"
    documents = database.list_documents_by_package(package["package_id"], db_path=db_path)
    assert len(documents) == 1
    assert "investor_relations" in documents[0]["local_path"]
    archive, _, included, missing = create_public_documents_zip(package["package_id"], db_path=db_path)
    assert (included, missing) == (1, 0)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert any(name.endswith(".pdf") for name in bundle.namelist())


def test_approval_does_not_bypass_pdf_mime_or_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "mime.db"
    database.initialize_database(db_path)
    url = "https://investors.qxo.com/q2-earnings-presentation.pdf"
    package = _ir_package_and_candidate(db_path, tmp_path, monkeypatch, "IR-2", url)
    result = approve_and_download_ir_material(
        package, "IR-2", session=_DownloadSession(_DownloadResponse(b"<html>not pdf</html>", "text/html", url)), db_path=db_path,
    )
    assert result["final_download_result"] == "FAILED"
    assert database.list_documents_by_package(package["package_id"], db_path=db_path) == []


def test_document_collection_defines_safe_manual_navigation() -> None:
    source = (Path(__file__).parents[1] / "app" / "pages" / "2_Document_Collection.py").read_text(encoding="utf-8")
    assert "def _safe_page_link" in source
    assert "Approve And Download" in source
    assert "Upload Downloaded File" in source
