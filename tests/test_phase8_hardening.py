from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.document_download_service import DocumentDownloadError, create_public_documents_zip, get_document_download
from app.services.openai_service import (
    ClosedCorpusAIReview,
    ExtractedClaim,
    ModelThesisItem,
    OPENAI_NOT_CONFIGURED,
    OpenAIProviderError,
    RecommendationNarrative,
    _error_code,
    _validate_citations,
    preflight_openai,
    responses_parse,
)
from app.services.package_service import PackageInput, create_package
from app.services.recommendation_engine import generate_recommendation, generate_thesis_items
from app.utils import database


def _new_package(db_path: Path, ticker: str = "QXO") -> dict:
    return create_package(PackageInput(ticker, "Common Equity", date(2026, 7, 13), 3, ""), db_path=db_path)


def _document(package: dict, path: Path, *, document_id: str, source_url: str, mime_type: str, db_path: Path) -> dict:
    content = path.read_bytes()
    return database.create_document_record(
        {
            "document_id": document_id,
            "package_id": package["package_id"],
            "ticker": package["ticker"],
            "category": "SEC Filing",
            "document_type": "SEC Filing",
            "title": path.name,
            "source_name": "SEC EDGAR",
            "source_url": source_url,
            "source_domain": "sec.gov",
            "local_filename": path.name,
            "local_path": str(path),
            "mime_type": mime_type,
            "file_size_bytes": len(content),
            "sha256_hash": __import__("hashlib").sha256(content).hexdigest(),
            "collection_method": "SEC",
            "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
            "is_public": True,
        },
        db_path=db_path,
    )


def test_package_version_ids_are_global_and_display_versions_are_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "versions.db"
    first_package = _new_package(db_path)
    second_package = _new_package(db_path)
    payload = {
        "ticker": "QXO",
        "company_name": "QXO, Inc.",
        "security_type": "Common Equity",
        "research_cutoff_date": "2026-07-13",
        "status": config.VERSION_STATUS_BUILDING,
    }
    first = database.allocate_package_version({**payload, "parent_package_id": first_package["package_id"]}, db_path=db_path)
    second = database.allocate_package_version({**payload, "parent_package_id": second_package["package_id"]}, db_path=db_path)
    assert first["version_id"].startswith("PV-")
    assert first["version_id"] != second["version_id"]
    assert first["display_version"] == second["display_version"] == "QXO-20260713-V001"


def test_package_version_allocation_is_transactional_under_concurrency(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent_versions.db"
    package = _new_package(db_path, ticker="CON")
    payload = {
        "parent_package_id": package["package_id"],
        "ticker": "CON",
        "company_name": "Concurrent Co.",
        "security_type": "Common Equity",
        "research_cutoff_date": "2026-07-13",
        "status": config.VERSION_STATUS_BUILDING,
    }
    with ThreadPoolExecutor(max_workers=6) as executor:
        versions = list(executor.map(lambda _: database.allocate_package_version(payload, db_path=db_path), range(6)))
    assert sorted(version["version_number"] for version in versions) == list(range(1, 7))
    assert len({version["version_id"] for version in versions}) == 6


def test_managed_document_downloads_are_package_scoped_and_preserve_mime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "files.db"
    managed = tmp_path / "downloaded"
    monkeypatch.setattr(config, "DOWNLOAD_DIR", managed)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    package = _new_package(db_path, ticker="FIL")
    html_path = managed / package["package_id"] / "sec" / "FIL_10-K.html"
    pdf_path = managed / package["package_id"] / "sec" / "FIL_10-Q.pdf"
    html_path.parent.mkdir(parents=True)
    html_path.write_bytes(b"<!doctype html><html><body>filing</body></html>")
    pdf_path.write_bytes(b"%PDF-1.7 test")
    html_doc = _document(package, html_path, document_id="DOC-HTML", source_url="https://sec.gov/html", mime_type="text/html", db_path=db_path)
    pdf_doc = _document(package, pdf_path, document_id="DOC-PDF", source_url="https://sec.gov/pdf", mime_type="application/pdf", db_path=db_path)
    html_download = get_document_download(package["package_id"], html_doc["document_id"], db_path=db_path)
    pdf_download = get_document_download(package["package_id"], pdf_doc["document_id"], db_path=db_path)
    assert html_download.filename == "FIL_10-K.html"
    assert html_download.mime_type == "text/html"
    assert pdf_download.filename == "FIL_10-Q.pdf"
    assert pdf_download.mime_type == "application/pdf"
    other = _new_package(db_path, ticker="OTH")
    with pytest.raises(DocumentDownloadError):
        get_document_download(other["package_id"], html_doc["document_id"], db_path=db_path)
    html_path.unlink()
    with pytest.raises(DocumentDownloadError, match="missing"):
        get_document_download(package["package_id"], html_doc["document_id"], db_path=db_path)


def test_public_document_zip_is_package_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "zip.db"
    managed = tmp_path / "downloaded"
    monkeypatch.setattr(config, "DOWNLOAD_DIR", managed)
    package = _new_package(db_path, ticker="ZIP")
    path = managed / package["package_id"] / "sec" / "filing.html"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"<html>zip</html>")
    _document(package, path, document_id="DOC-ZIP", source_url="https://sec.gov/zip", mime_type="text/html", db_path=db_path)
    content, filename, included, missing = create_public_documents_zip(package["package_id"], db_path=db_path)
    assert filename.endswith("_Public_Collected_Files.zip")
    assert included == 1
    assert missing == 0
    import zipfile
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(content)) as archive:
        assert archive.namelist() == ["Public_Documents/filing.html"]


class _FakeModels:
    def __init__(self) -> None:
        self.model = None

    def retrieve(self, model: str) -> object:
        self.model = model
        return object()


class _FakeResponses:
    def __init__(self, parsed: object) -> None:
        self.parsed = parsed
        self.kwargs: dict = {}

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return type("Response", (), {"output_parsed": self.parsed, "output_text": ""})()


class _FakeClient:
    def __init__(self, parsed: object | None = None) -> None:
        self.models = _FakeModels()
        self.responses = _FakeResponses(parsed)


def test_openai_preflight_and_responses_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-5.6-terra")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert preflight_openai(force=True).code == OPENAI_NOT_CONFIGURED
    client = _FakeClient()
    monkeypatch.setattr("app.services.openai_service._client", lambda api_key=None: client)
    result = preflight_openai(force=True, client=client)
    assert result.connected

    locator = '{"display_title":"Filing","page_number":1}'
    model = ClosedCorpusAIReview(
        extracted_claims=[ExtractedClaim(evidence_ids=["E-1"], claim_text="Supported claim", citation_locators=[locator])],
        thesis_items=[ModelThesisItem(item_type="BULL_THESIS", claim="Supported thesis", evidence_ids=["E-1"], citation_locators=[locator])],
        recommendation=RecommendationNarrative(rationale="Supported rationale", why_not_buy="", why_not_hold="", why_not_sell="", evidence_ids=["E-1"], citation_locators=[locator]),
    )
    response_client = _FakeClient(parsed=model)
    parsed = responses_parse(system_prompt="closed corpus", user_payload={"evidence_id": "E-1"}, schema=ClosedCorpusAIReview, client=response_client)
    assert parsed.thesis_items[0].evidence_ids == ["E-1"]
    assert response_client.responses.kwargs["tools"] == []
    assert response_client.responses.kwargs["text_format"] is ClosedCorpusAIReview


def test_openai_provider_errors_are_safe_and_claim_boundary_is_enforced() -> None:
    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        status_code = 429
        body = {"message": "quota"}

    class NotFoundError(Exception):
        status_code = 404

    assert _error_code(AuthenticationError("secret must not be returned")) == "OPENAI_AUTH_FAILED"
    assert _error_code(RateLimitError()) == "OPENAI_QUOTA_EXCEEDED"
    assert _error_code(NotFoundError()) == "OPENAI_MODEL_UNAVAILABLE"
    evidence = [{"evidence_id": "E-1", "source_locator_json": '{"page_number":1}'}]
    review = ClosedCorpusAIReview(
        recommendation=RecommendationNarrative(rationale="", why_not_buy="", why_not_hold="", why_not_sell=""),
        thesis_items=[ModelThesisItem(item_type="RISK", claim="Unsupported", evidence_ids=["E-OTHER"], citation_locators=["x"])],
    )
    with pytest.raises(OpenAIProviderError, match="outside"):
        _validate_citations(review, evidence)


def test_required_openai_mode_has_no_deterministic_ai_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "OPENAI_REQUIRED", True)
    with pytest.raises(ValueError, match="OpenAI is required for thesis drafting"):
        generate_thesis_items("ANALYSIS-1", evidence=[], db_path=tmp_path / "no-fallback.db")
    with pytest.raises(ValueError, match="OpenAI is required for recommendation narrative"):
        generate_recommendation(
            "ANALYSIS-1",
            evidence=[],
            metrics=[],
            scorecard_items=[],
            conflicts=[],
            db_path=tmp_path / "no-fallback.db",
        )


def test_material_claim_without_citation_is_rejected() -> None:
    review = ClosedCorpusAIReview(
        extracted_claims=[ExtractedClaim(claim_text="Unsupported material claim")],
        recommendation=RecommendationNarrative(rationale="", why_not_buy="", why_not_hold="", why_not_sell=""),
    )
    with pytest.raises(OpenAIProviderError, match="unsupported material claim"):
        _validate_citations(review, [])


def test_citation_set_must_be_exact_for_selected_evidence() -> None:
    locator = '{"page_number":1}'
    review = ClosedCorpusAIReview(
        extracted_claims=[
            ExtractedClaim(
                claim_text="Supported claim",
                evidence_ids=["E-1"],
                citation_locators=[locator, "{\"page_number\":99}"],
            )
        ],
        recommendation=RecommendationNarrative(
            rationale="",
            why_not_buy="",
            why_not_hold="",
            why_not_sell="",
        ),
    )
    with pytest.raises(OpenAIProviderError, match="outside"):
        _validate_citations(review, [{"evidence_id": "E-1", "source_locator_json": locator}])
