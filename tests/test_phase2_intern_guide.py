from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.checklist_service import ensure_package_checklist
from app.services.collection_profile import (
    CUTLER_EQUITY_INTERN_GUIDE,
    is_profile_eligible,
    normalize_sec_form,
)
from app.services.collectors.sec_collector import (
    FilingCandidate,
    discover_dividend_exhibits,
    download_dividend_exhibits,
    download_profile_inventory,
    form_144_preselected,
    preview_cutler_profile,
    store_official_y15,
)
from app.services.package_service import PackageInput, create_package
from app.services.upload_service import (
    UNKNOWN_SOURCE,
    UploadCandidate,
    infer_document_type,
    infer_research_source,
    prepare_batch_review,
    standardized_upload_filename,
    store_reviewed_upload_batch,
)
from app.utils import database


class FakeResponse:
    def __init__(self, *, json_data: dict | None = None, content: bytes = b"", status_code: int = 200) -> None:
        self._json_data = json_data or {}
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_: object) -> FakeResponse:
        self.calls.append(url)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def phase2_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Cutler tests tests@cutler.example")
    monkeypatch.setattr(config, "SEC_REQUEST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(config, "FORM_144_AUTO_SELECT_ENABLED", False)
    monkeypatch.setattr(config, "FORM_144_MIN_SHARES", None)
    monkeypatch.setattr(config, "FORM_144_MIN_MARKET_VALUE", None)


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase2_intern.db"
    database.initialize_database(path)
    return path


@pytest.fixture()
def package(temp_db: Path) -> dict:
    created = create_package(PackageInput("QXO", "Common Equity", date(2026, 7, 13), 3), db_path=temp_db)
    return database.update_package_company_metadata(
        created["package_id"],
        {
            "ticker": "QXO", "company_name": "QXO, Inc.", "cik": "0001234567", "exchange": "NYSE",
            "sic": "7370", "industry_description": "Services", "fiscal_year_end": "1231",
            "sec_company_url": "https://www.sec.gov/edgar/browse/?CIK=0001234567",
            "resolution_status": "RESOLVED", "resolution_source": "test", "resolution_timestamp": "2026-07-13T00:00:00Z",
        }, db_path=temp_db,
    )


def filing(form: str = "144", *, selected: bool = False) -> FilingCandidate:
    return FilingCandidate(
        "0001234567-26-000001", form, "2026-07-10", "2026-06-30", "doc.htm",
        "https://www.sec.gov/Archives/doc.htm", "https://www.sec.gov/index.htm", f"QXO {form}",
        normalized_form_family=normalize_sec_form(form), inventory_status="AWAITING_SELECTION" if form.startswith("144") else "ELIGIBLE",
        selected=selected,
    )


def submissions(forms: list[str]) -> dict:
    count = len(forms)
    return {"filings": {"recent": {
        "form": forms,
        "filingDate": ["2026-07-10"] * count,
        "reportDate": ["2026-06-30"] * count,
        "accessionNumber": [f"0001234567-26-{index:06d}" for index in range(count)],
        "primaryDocument": [f"doc-{index}.htm" for index in range(count)],
    }}}


def test_common_equity_defaults_to_named_profile(package: dict) -> None:
    assert package["collection_profile_name"] == CUTLER_EQUITY_INTERN_GUIDE
    assert CUTLER_EQUITY_INTERN_GUIDE in package["collection_profile_snapshot_json"]


@pytest.mark.parametrize(
    ("original", "family"),
    [("10-K/A", "10-K"), ("10-Q/A", "10-Q"), ("8-K/A", "8-K"), ("S-3/A", "S-3"),
     ("S-3ASR", "S-3"), ("S-4/A", "S-4"), ("144/A", "144")],
)
def test_form_family_normalization(original: str, family: str) -> None:
    assert normalize_sec_form(original) == family
    assert is_profile_eligible(original, include_form_144=family == "144")


def test_profile_inventory_excludes_unapproved_forms_without_failure(package: dict, temp_db: Path) -> None:
    forms = ["10-K/A", "10-Q", "8-K/A", "S-3ASR", "S-4/A", "DEF 14A", "144", "4", "13D", "424B3", "EFFECT"]
    inventory = preview_cutler_profile(package, session=FakeSession([FakeResponse(json_data=submissions(forms))]), db_path=temp_db)
    statuses = {item.form_type: item.inventory_status for item in inventory}
    assert statuses["S-3ASR"] == "ELIGIBLE"
    assert statuses["144"] == "AWAITING_SELECTION"
    assert all(statuses[form] == "EXCLUDED_BY_PROFILE" for form in ("4", "13D", "424B3", "EFFECT"))
    assert not any(item.selected for item in inventory if item.form_type == "144")
    assert len(database.list_sec_filing_inventory(package["package_id"], db_path=temp_db)) == len(forms)


def test_form_144_requires_selection_and_has_no_implicit_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = filing()
    assert not form_144_preselected(candidate)
    monkeypatch.setattr(config, "FORM_144_AUTO_SELECT_ENABLED", True)
    assert not form_144_preselected(candidate)
    monkeypatch.setattr(config, "FORM_144_MIN_SHARES", 100.0)
    with_values = FilingCandidate(**{**candidate.__dict__, "shares": 150.0})
    assert form_144_preselected(with_values)


def test_only_manually_selected_form_144_downloads(package: dict, temp_db: Path) -> None:
    unselected = download_profile_inventory(package, [filing(selected=False)], session=FakeSession([]), db_path=temp_db)
    assert unselected["downloaded_now"] == 0
    selected = download_profile_inventory(
        package, [filing(selected=True)], session=FakeSession([FakeResponse(content=b"<html>filing</html>")]), db_path=temp_db,
    )
    assert selected["downloaded_now"] == 1


def test_existing_accession_is_not_downloaded_twice(package: dict, temp_db: Path) -> None:
    candidate = FilingCandidate(**{**filing("10-K", selected=True).__dict__, "inventory_status": "ELIGIBLE"})
    first = download_profile_inventory(package, [candidate], session=FakeSession([FakeResponse(content=b"<html>10-k</html>")]), db_path=temp_db)
    second = download_profile_inventory(package, [candidate], session=FakeSession([]), db_path=temp_db)
    assert first["downloaded_now"] == 1
    assert second["already_collected"] == 1


def test_dividend_exhibit_detection_parent_and_deduplication(package: dict, temp_db: Path) -> None:
    parent = filing("8-K", selected=True)
    index = {"directory": {"item": [
        {"name": "ex991.htm", "description": "Quarterly dividend declaration"},
        {"name": "ex992.htm", "description": "Earnings release"},
    ]}}
    exhibits = discover_dividend_exhibits(package, parent, session=FakeSession([FakeResponse(json_data=index)]))
    assert [item.filename for item in exhibits] == ["ex991.htm"]
    assert exhibits[0].parent_accession_number == parent.accession_number
    first = download_dividend_exhibits(package, exhibits, session=FakeSession([FakeResponse(content=b"<html>dividend</html>")]), db_path=temp_db)
    second = download_dividend_exhibits(package, exhibits, session=FakeSession([]), db_path=temp_db)
    assert first["downloaded_now"] == 1 and second["already_collected"] == 1
    doc = database.list_documents_by_package(package["package_id"], db_path=temp_db)[0]
    assert doc["parent_accession_number"] == parent.accession_number


def test_y15_upload_recognition_official_link_and_optional_checklist(package: dict, temp_db: Path) -> None:
    candidate = UploadCandidate("QXO_Y-15_2026.pdf", b"%PDF-1.4 Y-15 regulatory report")
    assert infer_document_type(candidate)[0] == "y15_regulatory_report"
    stored = store_official_y15(
        package, "https://www.federalreserve.gov/reports/qxo-y15.pdf",
        session=FakeSession([FakeResponse(content=b"%PDF-1.4 official")]), db_path=temp_db,
    )
    assert stored["normalized_form_family"] == "Y-15"
    other = create_package(PackageInput("ABC", "Common Equity", date(2026, 7, 13), 3), db_path=temp_db)
    y15 = next(item for item in ensure_package_checklist(other, db_path=temp_db) if item["checklist_item_id"] == "y15_report")
    assert y15["effective_status"] == config.CHECKLIST_STATUS_OPTIONAL_NOT_DISCOVERED


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("Raymond_James_QXO_Initiation.pdf", "Raymond James"), ("Piper-Sandler_QXO.pdf", "Piper Sandler"),
     ("Gimme_Credit_QXO.pdf", "Gimme Credit"), ("Hovde_Group_QXO.pdf", "Hovde Group"),
     ("Janney_QXO.pdf", "Janney"), ("Arctic_QXO.pdf", "Arctic Securities")],
)
def test_known_research_firms_are_inferred(filename: str, expected: str) -> None:
    assert infer_research_source(UploadCandidate(filename, b"%PDF-1.4"))[0] == expected


def test_unknown_source_is_not_fabricated() -> None:
    source, confidence, _ = infer_research_source(UploadCandidate("miscellaneous.pdf", b"%PDF-1.4"))
    assert source == UNKNOWN_SOURCE and confidence == "LOW"


def test_document_type_inference_normalizes_filename_separators() -> None:
    code, confidence, _, _ = infer_document_type(
        UploadCandidate("QXO_Gimme_Credit_2026-07-10_Credit_Update.txt", b"research")
    )
    assert code == "credit_research"
    assert confidence == "HIGH"


def test_thirty_file_batch_corrections_idempotency_and_single_checklist(
    package: dict, temp_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [UploadCandidate(f"QXO_Raymond_James_2026-07-{(index % 28) + 1:02d}_Initiation_{index}.txt", f"equity research {index}".encode()) for index in range(30)]
    rows = prepare_batch_review(package, candidates, db_path=temp_db)
    assert len(rows) == 30 and all(row["Inferred source"] == "Raymond James" for row in rows)
    rows[0]["Final source"] = "Cutler Internal"
    rows[0]["Final document type"] = "Internal Research"
    calls = 0
    original = ensure_package_checklist

    def counted(*args: object, **kwargs: object) -> list[dict]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("app.services.checklist_service.ensure_package_checklist", counted)
    first = store_reviewed_upload_batch(package, candidates, rows, authorization_confirmed=True, db_path=temp_db)
    assert first["uploaded"] == 30 and calls == 1
    docs = database.list_documents_by_package(package["package_id"], db_path=temp_db)
    corrected = next(doc for doc in docs if doc["original_filename"] == candidates[0].original_filename)
    assert corrected["inferred_source"] == "Raymond James"
    assert corrected["final_source"] == "Cutler Internal"
    assert corrected["analyst_corrected_category_code"] == "internal_research"
    repeated_rows = prepare_batch_review(package, candidates, db_path=temp_db)
    assert all(not row["Include"] and row["Duplicate status"] != "Unique" for row in repeated_rows)
    second = store_reviewed_upload_batch(package, candidates, repeated_rows, authorization_confirmed=True, db_path=temp_db)
    assert second["uploaded"] == 0 and len(database.list_documents_by_package(package["package_id"], db_path=temp_db)) == 30


def test_invalid_file_does_not_fail_valid_batch_and_authorization_is_required(package: dict, temp_db: Path) -> None:
    candidates = [UploadCandidate("QXO_Janney_2026-07-10.txt", b"equity research"), UploadCandidate("bad.exe", b"MZ")]
    rows = prepare_batch_review(package, candidates, db_path=temp_db)
    with pytest.raises(ValueError, match="Authorization"):
        store_reviewed_upload_batch(package, candidates, rows, authorization_confirmed=False, db_path=temp_db)
    summary = store_reviewed_upload_batch(package, candidates, rows, authorization_confirmed=True, db_path=temp_db)
    assert summary["uploaded"] == 1 and summary["failed"] == 1


def test_standardized_filename_uses_unknown_tokens_and_collision_suffix() -> None:
    plain = standardized_upload_filename("QXO", UNKNOWN_SOURCE, "", "Unsafe Report?.PDF")
    collision = standardized_upload_filename("QXO", UNKNOWN_SOURCE, "", "Unsafe Report?.PDF", sha256_hash="abcdef1234", destination_exists=True)
    assert "UNKNOWN-SOURCE" in plain and "UNKNOWN-DATE" in plain
    assert collision.endswith("_abcdef12.pdf")


def test_collection_page_uses_one_batch_editor_without_required_source_dropdown() -> None:
    source = (Path(__file__).parents[1] / "app" / "pages" / "2_Document_Collection.py").read_text(encoding="utf-8")
    upload_section = source[source.index("def _licensed_uploads"):source.index("def _collected_documents")]
    assert upload_section.count("st.data_editor(") == 1
    assert 'st.selectbox(\n        "Source type"' not in upload_section
