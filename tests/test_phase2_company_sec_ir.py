from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import requests

from app import config
from app.services.collectors.ir_collector import (
    IrDocumentCandidate,
    discover_public_documents,
    download_selected_ir_documents,
)
from app.services.collectors.sec_collector import (
    FilingCandidate,
    build_sec_document_url,
    download_selected_filings,
    preview_filings,
    standardized_sec_filename,
)
from app.services.company_resolver import load_ticker_mapping, normalize_cik, resolve_package_company
from app.services.http_client import request_with_retries, validate_public_http_url
from app.services.package_service import PackageInput, create_package
from app.utils import database


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "phase2.db"
    database.initialize_database(db_path)
    return db_path


@pytest.fixture()
def package(temp_db: Path) -> dict:
    package = create_package(
        PackageInput("qxo", "Common Equity", date(2026, 7, 13), 3, ""),
        db_path=temp_db,
    )
    return database.update_package_company_metadata(
        package["package_id"],
        {
            "ticker": "QXO",
            "company_name": "QXO, Inc.",
            "cik": "0001234567",
            "exchange": "NYSE",
            "sic": "7370",
            "industry_description": "Services",
            "fiscal_year_end": "1231",
            "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0001234567",
            "resolution_status": "RESOLVED",
            "resolution_source": "test",
            "resolution_timestamp": "2026-07-13T00:00:00+00:00",
        },
        db_path=temp_db,
    )


@pytest.fixture(autouse=True)
def phase2_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler Capital tests@example.test")
    monkeypatch.setattr(config, "SEC_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    monkeypatch.setattr(config, "HTTP_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "IR_MAX_PAGES", 3)
    monkeypatch.setattr(config, "IR_MAX_DEPTH", 1)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)


def test_company_resolution_exact_lowercase_and_cache(temp_db: Path) -> None:
    package = create_package(
        PackageInput("qxo", "Common Equity", date.today(), 3, ""),
        db_path=temp_db,
    )
    session = FakeSession(
        [
            FakeResponse(
                json_data={
                    "fields": ["cik", "name", "ticker", "exchange"],
                    "data": [[1234567, "QXO, Inc.", "QXO", "NYSE"]],
                }
            ),
            FakeResponse(json_data={"name": "QXO, Inc.", "sic": "7370", "sicDescription": "Services", "fiscalYearEnd": "1231"}),
        ]
    )
    result = resolve_package_company(package, session=session, db_path=temp_db)
    assert result.status == "RESOLVED"
    assert result.metadata["ticker"] == "QXO"
    assert result.metadata["cik"] == "0001234567"
    cached = load_ticker_mapping(session=FakeSession([]))
    assert cached[0]["ticker"] == "QXO"


def test_company_resolution_missing_multiple_and_missing_user_agent(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = create_package(PackageInput("QXO", "Common Equity", date.today(), 3, ""), db_path=temp_db)
    missing = resolve_package_company(
        package,
        session=FakeSession([FakeResponse(json_data={"fields": ["cik", "name", "ticker"], "data": []})]),
        refresh=True,
        db_path=temp_db,
    )
    assert missing.status == "UNRESOLVED"
    multiple = resolve_package_company(
        package,
        session=FakeSession([FakeResponse(json_data={"fields": ["cik", "name", "ticker"], "data": [[1, "A", "QXO"], [2, "B", "QXO"]]})]),
        refresh=True,
        db_path=temp_db,
    )
    assert multiple.status == "MULTIPLE_MATCHES"
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler Capital Research Workbench research@example.com")
    configured = resolve_package_company(package, refresh=True, session=FakeSession([]), db_path=temp_db)
    assert configured.status == "CONFIGURATION_REQUIRED"


def test_expired_cache_refreshes(tmp_path: Path) -> None:
    cache = config.CACHE_DIR / "sec_company_tickers_exchange.json"
    cache.write_text("[]", encoding="utf-8")
    old = 1_700_000_000
    cache.touch()
    import os

    os.utime(cache, (old, old))
    session = FakeSession([
        FakeResponse(json_data={"fields": ["cik", "name", "ticker"], "data": [[1, "A", "AAA"]]})
    ])
    mapping = load_ticker_mapping(session=session)
    assert mapping[0]["ticker"] == "AAA"


def test_sec_preview_filters_dates_forms_and_urls(package: dict) -> None:
    session = FakeSession([
        FakeResponse(json_data={"filings": {"recent": {
            "form": ["10-K", "10-Q", "8-K", "S-1"],
            "filingDate": ["2026-02-27", "2027-01-01", "2022-01-01", "2026-01-01"],
            "reportDate": ["2025-12-31", "2026-09-30", "2021-12-31", "2025-12-31"],
            "accessionNumber": ["0001234567-26-000001", "0002", "0003", "0004"],
            "primaryDocument": ["qxo-10k.htm", "qxo-10q.htm", "qxo-8k.htm", "s1.htm"],
        }}})
    ])
    filings = preview_filings(package, ["10-K", "10-Q", "8-K"], session=session)
    assert len(filings) == 1
    assert filings[0].primary_document_url == build_sec_document_url("0001234567", "0001234567-26-000001", "qxo-10k.htm")


def test_sec_download_duplicate_failure_retry_and_hash(package: dict, temp_db: Path) -> None:
    filing = FilingCandidate(
        accession_number="0001234567-26-000001",
        form_type="10-K",
        filing_date="2026-02-27",
        report_period="2025-12-31",
        primary_document="qxo-10k.htm",
        primary_document_url="https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/qxo-10k.htm",
        filing_index_url="https://www.sec.gov/index.html",
        title="QXO 10-K",
    )
    assert standardized_sec_filename("QXO", filing) == "QXO_10-K_2026-02-27_0001234567-26-000001.htm"
    flaky = FakeSession([
        requests.Timeout("slow"),
        FakeResponse(content=b"<html>filing</html>", headers={"Content-Type": "text/html"}),
    ])
    summary = download_selected_filings(package, [filing], session=flaky, db_path=temp_db)
    assert summary["downloaded"] == 1
    docs = database.list_documents_by_package(package["package_id"], db_path=temp_db)
    assert docs[0]["sha256_hash"]
    dup = download_selected_filings(package, [filing], session=FakeSession([]), db_path=temp_db)
    assert dup["skipped"] == 1
    bad = FilingCandidate(
        **{
            **filing.__dict__,
            "accession_number": "0001234567-26-000002",
            "primary_document_url": "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000002/qxo-10k.htm",
        }
    )
    failed = download_selected_filings(
        package,
        [bad],
        session=FakeSession([FakeResponse(status_code=404, text="missing")]),
        db_path=temp_db,
    )
    assert failed["failed"] == 1


def test_request_retry_behavior() -> None:
    session = FakeSession([requests.Timeout("slow"), FakeResponse(text="ok")])
    response = request_with_retries("https://sec.gov/test", session=session, delay_seconds=0, max_retries=2)
    assert response.text == "ok"
    assert len(session.calls) == 2


def test_ir_url_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    assert validate_public_http_url("https://example.com/investors").is_valid
    assert not validate_public_http_url("ftp://example.com").is_valid
    assert not validate_public_http_url("https://localhost/investors").is_valid
    assert not validate_public_http_url("http://192.168.1.4/file.pdf").is_valid


def test_ir_discovery_relative_links_same_domain_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    html = '<a href="/files/2026-earnings-presentation.pdf">Earnings presentation 2026</a><a href="https://other.com/x.pdf">Other</a>'
    session = FakeSession([
        FakeResponse(status_code=404, text=""),
        FakeResponse(text=html, headers={"Content-Type": "text/html"}),
    ])
    docs, message = discover_public_documents("https://example.com/investors", session=session)
    assert not message
    assert len(docs) == 1
    assert docs[0].url == "https://example.com/files/2026-earnings-presentation.pdf"
    assert docs[0].suggested_category == "Earnings Release"


def test_ir_fake_pdf_rejection_duplicate_url_and_hash(package: dict, temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    candidate = IrDocumentCandidate(
        title="Investor presentation",
        url="https://example.com/investor-presentation.pdf",
        filename="investor-presentation.pdf",
        suggested_category="Investor Presentation",
        apparent_date="2026",
        confidence="Medium",
    )
    fake = download_selected_ir_documents(
        package,
        [(candidate, "Investor Presentation")],
        session=FakeSession([FakeResponse(content=b"<html>error</html>", headers={"Content-Type": "application/pdf"})]),
        db_path=temp_db,
    )
    assert fake["failed"] == 1
    good = download_selected_ir_documents(
        package,
        [(IrDocumentCandidate(**{**candidate.__dict__, "url": "https://example.com/good.pdf"}), "Investor Presentation")],
        session=FakeSession([FakeResponse(content=b"%PDF-1.4 ok", headers={"Content-Type": "application/pdf"})]),
        db_path=temp_db,
    )
    assert good["downloaded"] == 1
    duplicate = download_selected_ir_documents(
        package,
        [(IrDocumentCandidate(**{**candidate.__dict__, "url": "https://example.com/good.pdf"}), "Investor Presentation")],
        session=FakeSession([]),
        db_path=temp_db,
    )
    assert duplicate["skipped"] == 1
    duplicate_hash = download_selected_ir_documents(
        package,
        [(IrDocumentCandidate(**{**candidate.__dict__, "url": "https://example.com/good-copy.pdf"}), "Investor Presentation")],
        session=FakeSession([FakeResponse(content=b"%PDF-1.4 ok", headers={"Content-Type": "application/pdf"})]),
        db_path=temp_db,
    )
    assert duplicate_hash["skipped"] == 1


def test_ir_javascript_heavy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostbyname", lambda host: "93.184.216.34")
    session = FakeSession([
        FakeResponse(status_code=404, text=""),
        FakeResponse(text="<html><script>app()</script></html>", headers={"Content-Type": "text/html"}),
    ])
    docs, message = discover_public_documents("https://example.com/investors", session=session)
    assert docs == []
    assert "Automatic discovery was not available" in message


def test_normalize_cik() -> None:
    assert normalize_cik(123) == "0000000123"
