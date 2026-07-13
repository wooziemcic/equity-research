from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.services.checklist_service import ensure_package_checklist, recategorize_document, set_override
from app.services.document_classifier import classify_document
from app.services.package_service import PackageInput, create_package
from app.services.taxonomy import CHECKLIST_PROFILES
from app.services.upload_service import (
    UploadCandidate,
    inspect_zip_upload,
    remove_uploaded_document,
    store_uploaded_files,
    validate_upload_batch,
    validate_upload_candidate,
)
from app.services.workspace_service import sanitize_filename
from app.utils import database


@pytest.fixture(autouse=True)
def phase3_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "downloaded")
    monkeypatch.setattr(config, "MAX_UPLOAD_FILE_MB", 1)
    monkeypatch.setattr(config, "MAX_UPLOAD_BATCH_MB", 3)
    monkeypatch.setattr(config, "MAX_ZIP_ENTRIES", 5)
    monkeypatch.setattr(config, "MAX_ZIP_UNCOMPRESSED_MB", 1)


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    path = tmp_path / "phase3.db"
    database.initialize_database(path)
    return path


@pytest.fixture()
def package(temp_db: Path) -> dict:
    return create_package(
        PackageInput("QXO", "Common Equity", date(2026, 7, 13), 3, ""),
        db_path=temp_db,
    )


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_file_validation_supported_pdf_and_rejections() -> None:
    valid = validate_upload_candidate(UploadCandidate("report.pdf", b"%PDF-1.4 data"))
    assert valid.is_valid
    assert valid.detected_file_type == "PDF"
    assert not validate_upload_candidate(UploadCandidate("evil.exe", b"MZ")).is_valid
    assert not validate_upload_candidate(UploadCandidate("empty.pdf", b"")).is_valid
    assert not validate_upload_candidate(UploadCandidate("fake.pdf", b"<html>no</html>")).is_valid
    oversized = validate_upload_candidate(UploadCandidate("large.txt", b"a" * (2 * 1024 * 1024)))
    assert not oversized.is_valid


def test_file_validation_xlsx_zip_images_and_filename_safety() -> None:
    assert validate_upload_candidate(UploadCandidate("model.xlsx", b"PK\x03\x04abc")).is_valid
    assert validate_upload_candidate(UploadCandidate("macro_model.xlsm", b"PK\x03\x04abc")).is_valid
    assert validate_upload_candidate(UploadCandidate("archive.zip", b"PK\x03\x04abc")).is_valid
    assert validate_upload_candidate(UploadCandidate("chart.png", b"\x89PNG\r\n\x1a\nabc")).is_valid
    assert validate_upload_candidate(UploadCandidate("photo.jpg", b"\xff\xd8\xffabc")).is_valid
    assert sanitize_filename("../../Bloomberg DES?.pdf") == "Bloomberg_DES_.pdf"


def test_upload_batch_limit_and_duplicate_hash(package: dict, temp_db: Path) -> None:
    too_big = [
        UploadCandidate("a.txt", b"a" * (2 * 1024 * 1024)),
        UploadCandidate("b.txt", b"b" * (2 * 1024 * 1024)),
    ]
    assert all(not result.is_valid for result in validate_upload_batch(too_big, source_type="other"))
    candidate = UploadCandidate("Bloomberg DES.pdf", b"%PDF-1.4 same")
    first = store_uploaded_files(
        package,
        [candidate],
        source_type="bloomberg",
        authorization_confirmed=True,
        metadata_by_name={"Bloomberg DES.pdf": {"final_category_code": "bloomberg_des"}},
        db_path=temp_db,
    )
    second = store_uploaded_files(
        package,
        [candidate],
        source_type="bloomberg",
        authorization_confirmed=True,
        metadata_by_name={"Bloomberg DES.pdf": {"final_category_code": "bloomberg_des"}},
        db_path=temp_db,
    )
    assert first["uploaded"] == 1
    assert second["duplicated"] == 1
    docs = database.list_documents_by_package(package["package_id"], db_path=temp_db)
    assert len(docs) == 1
    assert any(not int(doc["is_public"]) for doc in docs)
    assert docs[0]["source_identity_key"] == f"upload:{docs[0]['sha256_hash']}"


@pytest.mark.parametrize(
    ("filename", "source", "expected", "confidence"),
    [
        ("Bloomberg_DES_QXO.pdf", "bloomberg", "bloomberg_des", "High"),
        ("Bloomberg_FA_QXO.pdf", "bloomberg", "bloomberg_fa", "High"),
        ("QXO_ANR.pdf", "bloomberg", "bloomberg_anr", "High"),
        ("QXO_DRSK.pdf", "bloomberg", "bloomberg_drsk", "High"),
        ("QXO_10-K_2026.htm", "company_materials", "annual_filing", "High"),
        ("QXO_10-Q_2026.htm", "company_materials", "quarterly_filing", "High"),
        ("QXO earnings transcript.pdf", "transcripts", "earnings_transcript", "High"),
        ("QXO initiation report.pdf", "sell_side", "sell_side_initiation", "High"),
        ("QXO credit report.pdf", "credit_research", "credit_research", "Medium"),
        ("Moodys QXO report.pdf", "credit_research", "rating_agency", "High"),
        ("QXO short report.pdf", "activist_bear_research", "short_seller_research", "High"),
        ("QXO model.xlsm", "financial_models", "financial_model", "Medium"),
        ("unknown_file.pdf", "other", "other", "Low"),
    ],
)
def test_classification_rules(filename: str, source: str, expected: str, confidence: str) -> None:
    suggestion = classify_document(filename, source_type=source)
    assert suggestion.category_code == expected
    assert suggestion.confidence == confidence


def test_analyst_correction_overrides_suggestion(package: dict, temp_db: Path) -> None:
    store_uploaded_files(
        package,
        [UploadCandidate("unknown_file.pdf", b"%PDF-1.4 data")],
        source_type="other",
        authorization_confirmed=True,
        metadata_by_name={"unknown_file.pdf": {"final_category_code": "sell_side_research"}},
        db_path=temp_db,
    )
    doc = database.list_documents_by_package(package["package_id"], db_path=temp_db)[0]
    assert doc["suggested_category_code"] == "other"
    assert doc["final_category_code"] == "sell_side_research"


def test_database_upload_run_checklist_audit_and_counts(package: dict, temp_db: Path) -> None:
    store_uploaded_files(
        package,
        [UploadCandidate("QXO model.xlsm", b"PK\x03\x04model")],
        source_type="financial_models",
        authorization_confirmed=True,
        metadata_by_name={"QXO model.xlsm": {"final_category_code": "financial_model"}},
        db_path=temp_db,
    )
    assert database.list_recent_upload_runs(package["package_id"], db_path=temp_db)
    assert database.list_audit_events(package["package_id"], db_path=temp_db)
    counts = database.document_counts_for_package(package["package_id"], db_path=temp_db)
    assert counts["licensed"] == 1
    items = ensure_package_checklist(package, db_path=temp_db)
    assert items
    item = next(entry for entry in items if entry["checklist_item_id"] == "latest_annual")
    assert item["effective_status"] == config.CHECKLIST_STATUS_MISSING


def test_checklist_profiles_overrides_recalculation_and_multiple_docs(package: dict, temp_db: Path) -> None:
    assert CHECKLIST_PROFILES["Common Equity"]
    convertible = create_package(PackageInput("ABC", "Convertible Security", date.today(), 3, ""), db_path=temp_db)
    credit = create_package(PackageInput("XYZ", "Credit / Debt", date.today(), 3, ""), db_path=temp_db)
    assert any(item["category_code"] == "convertible_analysis" for item in ensure_package_checklist(convertible, db_path=temp_db))
    assert any(item["category_code"] == "debt_analysis" for item in ensure_package_checklist(credit, db_path=temp_db))
    store_uploaded_files(
        package,
        [
            UploadCandidate("QXO 10-K.pdf", b"%PDF-1.4 a"),
            UploadCandidate("QXO 10-K copy.pdf", b"%PDF-1.4 b"),
        ],
        source_type="company_materials",
        authorization_confirmed=True,
        metadata_by_name={
            "QXO 10-K.pdf": {"final_category_code": "annual_filing"},
            "QXO 10-K copy.pdf": {"final_category_code": "annual_filing"},
        },
        db_path=temp_db,
    )
    items = ensure_package_checklist(package, db_path=temp_db)
    annual = next(item for item in items if item["checklist_item_id"] == "latest_annual")
    assert annual["effective_status"] == config.CHECKLIST_STATUS_AVAILABLE
    assert annual["matched_document_count"] == 2
    set_override(package["package_id"], "latest_annual", config.CHECKLIST_STATUS_NOT_APPLICABLE, "Not relevant", db_path=temp_db)
    overridden = database.get_checklist_item(package["package_id"], "latest_annual", db_path=temp_db)
    assert overridden["effective_status"] == config.CHECKLIST_STATUS_NOT_APPLICABLE
    set_override(package["package_id"], "latest_annual", config.CHECKLIST_STATUS_NOT_AVAILABLE, "Unavailable", db_path=temp_db)
    assert database.get_checklist_item(package["package_id"], "latest_annual", db_path=temp_db)["effective_status"] == config.CHECKLIST_STATUS_NOT_AVAILABLE
    set_override(package["package_id"], "latest_annual", config.CHECKLIST_STATUS_NEEDS_REVIEW, "Review", db_path=temp_db)
    assert database.get_checklist_item(package["package_id"], "latest_annual", db_path=temp_db)["effective_status"] == config.CHECKLIST_STATUS_NEEDS_REVIEW
    set_override(package["package_id"], "latest_annual", None, "", db_path=temp_db)
    doc = database.list_documents_by_package(package["package_id"], db_path=temp_db)[0]
    recategorize_document(package, doc["document_id"], "other", db_path=temp_db)
    recalculated = ensure_package_checklist(package, db_path=temp_db)
    assert next(item for item in recalculated if item["checklist_item_id"] == "latest_annual")["matched_document_count"] == 1
    remove_uploaded_document(doc, confirm=True, db_path=temp_db)
    after_delete = ensure_package_checklist(package, db_path=temp_db)
    assert next(item for item in after_delete if item["checklist_item_id"] == "latest_annual")["matched_document_count"] == 1


def test_zip_safety_inspection() -> None:
    safe = inspect_zip_upload(zip_bytes({"folder/report.pdf": b"%PDF-1.4"}))
    assert safe[0].is_safe
    traversal = inspect_zip_upload(zip_bytes({"../evil.pdf": b"%PDF-1.4"}))
    assert not traversal[0].is_safe
    absolute = inspect_zip_upload(zip_bytes({"/evil.pdf": b"%PDF-1.4"}))
    assert not absolute[0].is_safe
    unsupported = inspect_zip_upload(zip_bytes({"evil.exe": b"MZ"}))
    assert not unsupported[0].is_safe


def test_zip_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MAX_ZIP_ENTRIES", 1)
    too_many = inspect_zip_upload(zip_bytes({"a.pdf": b"%PDF", "b.pdf": b"%PDF"}))
    assert not too_many[0].is_safe
    monkeypatch.setattr(config, "MAX_ZIP_ENTRIES", 5)
    monkeypatch.setattr(config, "MAX_ZIP_UNCOMPRESSED_MB", 0)
    too_large = inspect_zip_upload(zip_bytes({"a.pdf": b"%PDF"}))
    assert not too_large[0].is_safe
