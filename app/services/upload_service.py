from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from app import config
from app.services.document_classifier import ClassificationSuggestion, classify_document
from app.services.collection_profile import normalize_sec_form
from app.services.taxonomy import CATEGORIES, category_display
from app.services.workspace_service import (
    atomic_write_bytes,
    safe_licensed_document_path,
    sanitize_filename,
)
from app.utils import database


@dataclass(frozen=True)
class UploadCandidate:
    original_filename: str
    content: bytes
    browser_mime_type: str = ""


@dataclass(frozen=True)
class FileValidationResult:
    is_valid: bool
    original_filename: str
    sanitized_filename: str
    extension: str
    detected_file_type: str
    file_size_bytes: int
    sha256_hash: str
    error: str = ""
    warning: str = ""
    classification: ClassificationSuggestion | None = None


@dataclass(frozen=True)
class ZipEntryInspection:
    filename: str
    file_size: int
    compress_size: int
    is_safe: bool
    reason: str = ""


SIGNATURES = {
    ".pdf": (b"%PDF",),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xlsm": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}

TEXT_EXTENSIONS = {".csv", ".txt"}


def _detect_file_type(extension: str) -> str:
    return extension.lstrip(".").upper() or "UNKNOWN"


def validate_upload_candidate(
    candidate: UploadCandidate,
    *,
    source_type: str = "other",
) -> FileValidationResult:
    """Validate uploaded bytes and return a classification suggestion."""
    original = Path(candidate.original_filename).name
    sanitized = sanitize_filename(original)
    extension = Path(sanitized).suffix.lower()
    content = candidate.content
    sha = hashlib.sha256(content).hexdigest() if content else ""
    base = {
        "original_filename": candidate.original_filename,
        "sanitized_filename": sanitized,
        "extension": extension,
        "detected_file_type": _detect_file_type(extension),
        "file_size_bytes": len(content),
        "sha256_hash": sha,
    }
    if not content:
        return FileValidationResult(False, **base, error="File is empty.")
    if extension not in config.SUPPORTED_UPLOAD_EXTENSIONS:
        return FileValidationResult(False, **base, error="Unsupported file type.")
    max_bytes = config.MAX_UPLOAD_FILE_MB * 1024 * 1024
    if len(content) > max_bytes:
        return FileValidationResult(False, **base, error="File exceeds the per-file upload limit.")
    signatures = SIGNATURES.get(extension)
    if signatures and not content.startswith(signatures):
        return FileValidationResult(False, **base, error="File signature does not match its extension.")
    if extension in TEXT_EXTENSIONS and b"\x00" in content[:1024]:
        return FileValidationResult(False, **base, error="Text file appears to contain binary data.")
    classification = classify_document(original, source_type=source_type)
    warning = "Low-confidence classification requires analyst confirmation." if classification.confidence == "Low" else ""
    return FileValidationResult(True, **base, classification=classification, warning=warning)


def validate_upload_batch(candidates: list[UploadCandidate], *, source_type: str = "other") -> list[FileValidationResult]:
    """Validate a batch and apply total batch-size limits."""
    results = [validate_upload_candidate(candidate, source_type=source_type) for candidate in candidates]
    total = sum(result.file_size_bytes for result in results)
    if total > config.MAX_UPLOAD_BATCH_MB * 1024 * 1024:
        return [
            FileValidationResult(
                False,
                original_filename=result.original_filename,
                sanitized_filename=result.sanitized_filename,
                extension=result.extension,
                detected_file_type=result.detected_file_type,
                file_size_bytes=result.file_size_bytes,
                sha256_hash=result.sha256_hash,
                error="Upload batch exceeds the configured total size limit.",
                classification=result.classification,
            )
            for result in results
        ]
    return results


UNKNOWN_SOURCE = "UNKNOWN_SOURCE"

SOURCE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Bloomberg", ("bloomberg",)),
    ("Morningstar", ("morningstar",)),
    ("FactSet", ("factset",)),
    ("Raymond James", ("raymond james", "raymond_james", "raymond-james", "rj equity")),
    ("Piper Sandler", ("piper sandler", "piper_sandler", "piper-sandler")),
    ("Gimme Credit", ("gimme credit", "gimme_credit", "gimme-credit")),
    ("Hovde Group", ("hovde group", "hovde_group", "hovde-group")),
    ("Janney", ("janney",)),
    ("Arctic Securities", ("arctic securities", "arctic_securities", "arctic-securities", "arctic")),
    ("Cutler Internal", ("cutler", "internal research", "internal_research")),
    ("Earnings Call Transcript", ("seeking alpha", "earnings call transcript", "earnings_transcript", "transcript provider")),
)

DOCUMENT_TYPE_OPTIONS = {
    "sell_side_research": "Sell-Side Equity Research",
    "credit_research": "Credit Research",
    "analyst_data_sheet": "Analyst Data Sheet",
    "earnings_transcript": "Earnings Transcript",
    "financial_model": "Financial Model",
    "internal_research": "Internal Research",
    "industry_research": "Industry Research",
    "activist_research": "Activist Material",
    "dividend_announcement": "Dividend Announcement",
    "y15_regulatory_report": "Y-15 Regulatory Report",
    "annual_filing": "10-K Public Filing",
    "quarterly_filing": "10-Q Public Filing",
    "current_report": "8-K Public Filing",
    "registration_s3": "S-3 Public Filing",
    "registration_s4": "S-4 Public Filing",
    "proxy_statement": "DEF 14A Public Filing",
    "other_research": "Other Research",
}

PUBLIC_FORM_CATEGORIES = {
    "10-K": "annual_filing",
    "10-Q": "quarterly_filing",
    "8-K": "current_report",
    "S-3": "registration_s3",
    "S-4": "registration_s4",
    "DEF 14A": "proxy_statement",
    "144": "form_144",
}


def _inspection_text(candidate: UploadCandidate) -> str:
    """Return safe, bounded metadata/first-page text for deterministic local inference."""
    parts = [Path(candidate.original_filename).stem]
    if Path(candidate.original_filename).suffix.lower() in TEXT_EXTENSIONS:
        parts.append(candidate.content[:12000].decode("utf-8", errors="replace"))
    elif Path(candidate.original_filename).suffix.lower() == ".pdf":
        try:
            from io import BytesIO
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(candidate.content))
            parts.extend(str(value) for value in (reader.metadata or {}).values() if value)
            if reader.pages:
                parts.append((reader.pages[0].extract_text() or "")[:12000])
        except Exception:
            pass
    return " ".join(parts)


def infer_research_source(candidate: UploadCandidate) -> tuple[str, str, str]:
    text = _inspection_text(candidate).lower()
    for source, aliases in SOURCE_ALIASES:
        matched = next((alias for alias in aliases if alias in text), None)
        if matched:
            location = "filename" if matched in candidate.original_filename.lower() else "metadata or first-page text"
            return source, "HIGH", f"Matched {matched!r} in {location}."
    return UNKNOWN_SOURCE, "LOW", "No reliable known-source alias was found."


def _detect_public_form(text: str) -> str | None:
    upper = text.upper().replace("_", " ").replace("-", "-")
    patterns = (
        r"\b10-K(?:/A)?\b", r"\b10-Q(?:/A)?\b", r"\b8-K(?:/A)?\b",
        r"\bS-3ASR\b", r"\bS-3(?:/A)?\b", r"\bS-4(?:/A)?\b", r"\bDEF\s+14A\b",
    )
    for pattern in patterns:
        match = re.search(pattern, upper)
        if match:
            return normalize_sec_form(match.group(0).replace("  ", " "))
    return None


def infer_document_type(candidate: UploadCandidate) -> tuple[str, str, str, str | None]:
    text = _inspection_text(candidate)
    lowered = text.lower()
    normalized_lowered = re.sub(r"[_-]+", " ", lowered)
    normalized_filename = re.sub(r"[_-]+", " ", candidate.original_filename.lower())
    public_family = _detect_public_form(text)
    if public_family:
        code = PUBLIC_FORM_CATEGORIES[public_family]
        return code, "HIGH", f"Recognized public filing family {public_family}.", public_family
    if re.search(r"\by\s*15\b", normalized_lowered):
        return "y15_regulatory_report", "HIGH", "Recognized Y-15 regulatory-report language.", "Y-15"
    rules = (
        ("dividend_announcement", ("dividend declaration", "quarterly dividend", "cash dividend")),
        ("earnings_transcript", ("earnings transcript", "earnings call transcript", "transcript")),
        ("financial_model", ("financial model", ".xlsx", ".xlsm")),
        ("analyst_data_sheet", ("data sheet", "datasheet")),
        ("credit_research", ("credit research", "credit update", "bond research", "gimme credit")),
        ("industry_research", ("industry research", "industry outlook")),
        ("activist_research", ("activist", "short seller", "short-seller")),
        ("internal_research", ("internal research", "cutler")),
        ("sell_side_research", ("initiation", "price target", "equity research", "raymond james", "piper sandler", "hovde", "janney", "arctic")),
    )
    for code, terms in rules:
        matched = next(
            (
                term
                for term in terms
                if term in lowered or term in normalized_lowered or term in normalized_filename
            ),
            None,
        )
        if matched:
            return code, "HIGH" if matched in normalized_filename else "MEDIUM", f"Matched {matched!r}.", None
    return "other_research", "LOW", "No reliable document-type rule matched.", None


def _detected_date(text: str) -> str:
    match = re.search(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def _normalized_filename(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", Path(value).stem.lower()) + Path(value).suffix.lower()


def standardized_upload_filename(
    ticker: str,
    source: str,
    document_date: str,
    original_filename: str,
    *,
    sha256_hash: str = "",
    destination_exists: bool = False,
) -> str:
    extension = Path(original_filename).suffix.lower()
    source_part = source if source != UNKNOWN_SOURCE else "UNKNOWN-SOURCE"
    date_part = document_date or "UNKNOWN-DATE"
    title = re.sub(r"\b20\d{2}[-_]?\d{2}[-_]?\d{2}\b", "", Path(original_filename).stem, flags=re.I)
    title = sanitize_filename(title).strip("_- ")[:80] or "Document"
    filename = sanitize_filename(f"{ticker}_{source_part}_{date_part}_{title}{extension}")
    if destination_exists and sha256_hash:
        filename = f"{Path(filename).stem}_{sha256_hash[:8]}{extension}"
    return filename


def prepare_batch_review(
    package: dict[str, Any],
    candidates: list[UploadCandidate],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    validations = validate_upload_batch(candidates)
    existing = database.list_documents_by_package(package["package_id"], db_path=db_path)
    existing_names = {
        _normalized_filename(value)
        for doc in existing
        for value in (doc.get("original_filename"), doc.get("managed_filename"), doc.get("stored_filename"), doc.get("local_filename"))
        if value
    }
    seen_hashes: set[str] = set()
    rows: list[dict[str, Any]] = []
    for index, (candidate, validation) in enumerate(zip(candidates, validations, strict=False)):
        source, source_confidence, source_reason = infer_research_source(candidate)
        category, category_confidence, category_reason, form_family = infer_document_type(candidate)
        duplicate_reasons: list[str] = []
        if validation.sha256_hash in seen_hashes or database.get_document_by_hash(package["package_id"], validation.sha256_hash, db_path=db_path):
            duplicate_reasons.append("SHA-256")
        if _normalized_filename(validation.sanitized_filename) in existing_names:
            duplicate_reasons.append("normalized filename")
        seen_hashes.add(validation.sha256_hash)
        date_value = _detected_date(candidate.original_filename + " " + _inspection_text(candidate)[:1000])
        is_public = bool(form_family)
        duplicate = ", ".join(dict.fromkeys(duplicate_reasons))
        rows.append(
            {
                "Include": validation.is_valid and not duplicate,
                "Original filename": candidate.original_filename,
                "Detected ticker": package["ticker"] if package["ticker"].lower() in candidate.original_filename.lower() else "",
                "Inferred source": "SEC EDGAR" if is_public else source,
                "Final source": "SEC EDGAR" if is_public else source,
                "Inferred document type": DOCUMENT_TYPE_OPTIONS[category],
                "Final document type": DOCUMENT_TYPE_OPTIONS[category],
                "Document date": date_value,
                "File size": validation.file_size_bytes,
                "Duplicate status": duplicate or "Unique",
                "Validation status": "Valid" if validation.is_valid else validation.error,
                "Needs review": source_confidence == "LOW" or category_confidence == "LOW",
                "Notes": "",
                "_index": index,
                "_sha256": validation.sha256_hash,
                "_source_confidence": source_confidence,
                "_source_reason": source_reason,
                "_category_code": category,
                "_category_confidence": category_confidence,
                "_category_reason": category_reason,
                "_normalized_form_family": form_family,
                "_is_public": is_public,
            }
        )
    return rows


def store_reviewed_upload_batch(
    package: dict[str, Any],
    candidates: list[UploadCandidate],
    review_rows: list[dict[str, Any]],
    *,
    authorization_confirmed: bool,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Persist an analyst-reviewed batch; each row succeeds or fails independently."""
    if not authorization_confirmed:
        raise ValueError("Authorization acknowledgement is required before upload.")
    validations = validate_upload_batch(candidates)
    run_id = f"RUN-UPLOAD-{secrets.token_hex(8).upper()}"
    database.create_upload_run(
        run_id=run_id,
        package_id=package["package_id"],
        number_selected=len(candidates),
        status=config.UPLOAD_STATUS_STARTED,
        db_path=db_path,
    )
    display_to_code = {display: code for code, display in DOCUMENT_TYPE_OPTIONS.items()}
    summary = {"accepted": 0, "uploaded": 0, "duplicates": 0, "excluded": 0, "failed": 0, "bytes": 0}
    rows_by_index = {int(row.get("_index", index)): row for index, row in enumerate(review_rows)}
    for index, (candidate, validation) in enumerate(zip(candidates, validations, strict=False)):
        row = rows_by_index.get(index, {})
        if not validation.is_valid:
            summary["failed"] += 1
            _audit(package["package_id"], "UPLOAD_FAILED", {"filename": candidate.original_filename, "error": validation.error, "upload_batch_id": run_id}, db_path=db_path)
            continue
        if not bool(row.get("Include")):
            summary["excluded"] += 1
            _audit(package["package_id"], "UPLOAD_EXCLUDED", {"filename": candidate.original_filename, "upload_batch_id": run_id}, db_path=db_path)
            continue
        summary["accepted"] += 1
        existing = database.get_document_by_hash(package["package_id"], validation.sha256_hash, db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["duplicates"] += 1
            _audit(package["package_id"], "DUPLICATE_DETECTED", {"filename": candidate.original_filename, "existing_document_id": existing["document_id"], "upload_batch_id": run_id}, existing["document_id"], db_path=db_path)
            continue
        inferred_source = str(row.get("Inferred source") or UNKNOWN_SOURCE)
        final_source = str(row.get("Final source") or inferred_source)
        inferred_code = str(row.get("_category_code") or "other_research")
        final_code = display_to_code.get(str(row.get("Final document type")), inferred_code)
        document_date = str(row.get("Document date") or "")
        first_filename = standardized_upload_filename(
            package["ticker"], final_source, document_date, candidate.original_filename, sha256_hash=validation.sha256_hash
        )
        first_path = safe_licensed_document_path(package["package_id"], "other", first_filename)
        managed_filename = standardized_upload_filename(
            package["ticker"], final_source, document_date, candidate.original_filename,
            sha256_hash=validation.sha256_hash, destination_exists=first_path.exists(),
        )
        path = _unique_path(package["package_id"], "other", managed_filename)
        details = {
            "final_category_code": final_code,
            "title": Path(candidate.original_filename).stem,
            "document_date": document_date or None,
            "publication_date": document_date or None,
            "source_institution": final_source,
            "analyst_notes": row.get("Notes") or None,
            "is_public": bool(row.get("_is_public")),
            "final_source": final_source,
            "managed_filename": path.name,
        }
        try:
            atomic_write_bytes(path, candidate.content)
            created = database.create_document_record(
                _document_record(
                    package, validation, database.generate_document_id("DOC-UPLOAD"), "other",
                    config.DOCUMENT_STATUS_DOWNLOADED, details=details, local_path=path, content=candidate.content,
                ),
                db_path=db_path,
            )
            corrected_source = final_source if final_source != inferred_source else None
            corrected_category = final_code if final_code != inferred_code else None
            database.update_document_metadata(
                created["document_id"],
                {
                    "source_name": final_source,
                    "source_type": final_source,
                    "document_type": DOCUMENT_TYPE_OPTIONS.get(final_code, "Other Research"),
                    "category": category_display(final_code),
                    "final_category_code": final_code,
                    "normalized_form_family": row.get("_normalized_form_family"),
                    "inferred_source": inferred_source,
                    "source_confidence": row.get("_source_confidence"),
                    "source_inference_reason": row.get("_source_reason"),
                    "analyst_corrected_source": corrected_source,
                    "final_source": final_source,
                    "inferred_category_code": inferred_code,
                    "category_confidence": row.get("_category_confidence"),
                    "category_inference_reason": row.get("_category_reason"),
                    "analyst_corrected_category_code": corrected_category,
                    "upload_batch_id": run_id,
                    "managed_filename": path.name,
                    "is_public": bool(row.get("_is_public")),
                },
                db_path=db_path,
            )
            _audit(package["package_id"], "BATCH_FILE_UPLOADED", {"filename": candidate.original_filename, "managed_filename": path.name, "upload_batch_id": run_id}, created["document_id"], db_path=db_path)
            summary["uploaded"] += 1
            summary["bytes"] += validation.file_size_bytes
        except Exception as exc:
            if path.exists():
                path.unlink()
            summary["failed"] += 1
            _audit(package["package_id"], "UPLOAD_FAILED", {"filename": candidate.original_filename, "error": str(exc), "upload_batch_id": run_id}, db_path=db_path)
    status = config.UPLOAD_STATUS_COMPLETED if not summary["failed"] else config.UPLOAD_STATUS_COMPLETED_WITH_ERRORS if summary["uploaded"] else config.UPLOAD_STATUS_FAILED
    database.update_upload_run(
        run_id,
        status=status,
        number_uploaded=summary["uploaded"],
        number_duplicated=summary["duplicates"],
        number_skipped=summary["excluded"],
        number_failed=summary["failed"],
        total_bytes_uploaded=summary["bytes"],
        db_path=db_path,
    )
    database.update_package_collection_state(package["package_id"], config.STATUS_LICENSED_UPLOADS, db_path=db_path)
    from app.services.checklist_service import ensure_package_checklist

    refreshed = database.get_package_by_package_id(package["package_id"], db_path=db_path) or package
    ensure_package_checklist(refreshed, db_path=db_path)
    return summary


def inspect_zip_upload(content: bytes) -> list[ZipEntryInspection]:
    """Inspect ZIP entries without extracting them."""
    inspections: list[ZipEntryInspection] = []
    with zipfile.ZipFile(PathLikeBytes(content)) as archive:
        infos = archive.infolist()
        if len(infos) > config.MAX_ZIP_ENTRIES:
            return [
                ZipEntryInspection(
                    filename="(archive)",
                    file_size=sum(info.file_size for info in infos),
                    compress_size=sum(info.compress_size for info in infos),
                    is_safe=False,
                    reason="Archive exceeds the maximum entry count.",
                )
            ]
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > config.MAX_ZIP_UNCOMPRESSED_MB * 1024 * 1024:
            return [
                ZipEntryInspection(
                    filename="(archive)",
                    file_size=total_uncompressed,
                    compress_size=sum(info.compress_size for info in infos),
                    is_safe=False,
                    reason="Archive exceeds the maximum uncompressed size.",
                )
            ]
        for info in infos:
            name = info.filename
            path = PurePosixPath(name)
            reason = ""
            is_safe = True
            if path.is_absolute() or ".." in path.parts:
                is_safe = False
                reason = "Unsafe path traversal entry."
            elif info.is_dir():
                reason = "Directory entry."
            elif Path(name).suffix.lower() not in config.SUPPORTED_UPLOAD_EXTENSIONS:
                is_safe = False
                reason = "Unsupported archived file type."
            elif info.compress_size and info.file_size / max(info.compress_size, 1) > 100:
                is_safe = False
                reason = "Suspicious compression ratio."
            inspections.append(
                ZipEntryInspection(
                    filename=name,
                    file_size=info.file_size,
                    compress_size=info.compress_size,
                    is_safe=is_safe,
                    reason=reason,
                )
            )
    return inspections


class PathLikeBytes:
    """Tiny adapter so zipfile can inspect in-memory bytes without a temp file."""

    def __init__(self, content: bytes) -> None:
        import io

        self._buffer = io.BytesIO(content)

    def read(self, *args: Any) -> bytes:
        return self._buffer.read(*args)

    def seek(self, *args: Any) -> int:
        return self._buffer.seek(*args)

    def tell(self) -> int:
        return self._buffer.tell()

    def seekable(self) -> bool:
        return True


def _unique_path(package_id: str, source_type: str, filename: str) -> Path:
    path = safe_licensed_document_path(package_id, source_type, filename)
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Could not allocate a unique storage filename.")


def _audit(package_id: str, event_type: str, details: dict[str, Any], document_id: str | None = None, *, db_path: Path | str) -> None:
    database.create_audit_event(
        event_id=f"AUD-{secrets.token_hex(8).upper()}",
        package_id=package_id,
        document_id=document_id,
        event_type=event_type,
        event_details_json=json.dumps(details, sort_keys=True),
        db_path=db_path,
    )


def store_uploaded_files(
    package: dict[str, Any],
    candidates: list[UploadCandidate],
    *,
    source_type: str,
    authorization_confirmed: bool,
    metadata_by_name: dict[str, dict[str, Any]] | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Validate, store, and record manually uploaded licensed files."""
    if not authorization_confirmed:
        raise ValueError("Authorization acknowledgement is required before upload.")
    metadata_by_name = metadata_by_name or {}
    validations = validate_upload_batch(candidates, source_type=source_type)
    run_id = f"RUN-UPLOAD-{secrets.token_hex(8).upper()}"
    database.create_upload_run(
        run_id=run_id,
        package_id=package["package_id"],
        number_selected=len(candidates),
        status=config.UPLOAD_STATUS_STARTED,
        db_path=db_path,
    )
    summary = {"uploaded": 0, "duplicated": 0, "skipped": 0, "failed": 0, "bytes": 0}
    for result, candidate in zip(validations, candidates, strict=False):
        details = metadata_by_name.get(result.original_filename, {})
        document_id = database.generate_document_id("DOC-UPLOAD")
        if not result.is_valid:
            database.create_document_record(
                _document_record(package, result, document_id, source_type, config.DOCUMENT_STATUS_FAILED, details=details, error=result.error),
                db_path=db_path,
            )
            _audit(package["package_id"], "UPLOAD_FAILED", {"filename": result.original_filename, "error": result.error}, document_id, db_path=db_path)
            summary["failed"] += 1
            continue
        existing_hash = database.get_document_by_hash(package["package_id"], result.sha256_hash, db_path=db_path)
        if existing_hash and existing_hash.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            _audit(
                package["package_id"],
                "DUPLICATE_DETECTED",
                {"filename": result.original_filename, "sha256": result.sha256_hash, "existing_document_id": existing_hash["document_id"]},
                existing_hash["document_id"],
                db_path=db_path,
            )
            summary["duplicated"] += 1
            continue
        path = _unique_path(package["package_id"], source_type, result.sanitized_filename)
        atomic_write_bytes(path, candidate.content)
        database.create_document_record(
            _document_record(package, result, document_id, source_type, config.DOCUMENT_STATUS_DOWNLOADED, details=details, local_path=path, content=candidate.content),
            db_path=db_path,
        )
        if result.extension == ".zip":
            try:
                entries = [entry.__dict__ for entry in inspect_zip_upload(candidate.content)]
                _audit(package["package_id"], "ZIP_INSPECTED", {"filename": result.original_filename, "entries": entries}, document_id, db_path=db_path)
            except Exception as exc:
                _audit(package["package_id"], "ZIP_INSPECTION_FAILED", {"filename": result.original_filename, "error": str(exc)}, document_id, db_path=db_path)
        _audit(package["package_id"], "LICENSED_FILE_UPLOADED", {"filename": result.original_filename, "stored": path.name}, document_id, db_path=db_path)
        summary["uploaded"] += 1
        summary["bytes"] += result.file_size_bytes
    status = (
        config.UPLOAD_STATUS_COMPLETED
        if summary["failed"] == 0
        else config.UPLOAD_STATUS_COMPLETED_WITH_ERRORS
        if summary["uploaded"] or summary["duplicated"]
        else config.UPLOAD_STATUS_FAILED
    )
    database.update_upload_run(
        run_id,
        status=status,
        number_uploaded=summary["uploaded"],
        number_duplicated=summary["duplicated"],
        number_skipped=summary["skipped"],
        number_failed=summary["failed"],
        total_bytes_uploaded=summary["bytes"],
        db_path=db_path,
    )
    database.update_package_collection_state(package["package_id"], config.STATUS_LICENSED_UPLOADS, db_path=db_path)
    return summary


def _document_record(
    package: dict[str, Any],
    result: FileValidationResult,
    document_id: str,
    source_type: str,
    status: str,
    *,
    details: dict[str, Any],
    local_path: Path | None = None,
    content: bytes | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    classification = result.classification or classify_document(result.original_filename, source_type=source_type)
    final_category_code = details.get("final_category_code") or classification.category_code
    title = details.get("title") or Path(result.original_filename).stem
    return {
        "document_id": document_id,
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": category_display(final_category_code),
        "document_type": result.detected_file_type,
        "title": title,
        "source_name": details.get("final_source") or config.LICENSED_SOURCE_TYPES.get(source_type, "Licensed Upload"),
        "source_url": f"local-upload://{result.sanitized_filename}",
        "source_domain": "local",
        "publication_date": details.get("publication_date"),
        "local_filename": local_path.name if local_path else result.sanitized_filename,
        "local_path": str(local_path) if local_path else None,
        "mime_type": mimetypes.guess_type(result.sanitized_filename)[0] or "application/octet-stream",
        "file_size_bytes": result.file_size_bytes,
        "sha256_hash": result.sha256_hash,
        "collection_method": "BATCH_UPLOAD",
        "collection_status": status,
        "is_public": bool(details.get("is_public", False)),
        "error_message": error,
        "original_filename": result.original_filename,
        "stored_filename": local_path.name if local_path else result.sanitized_filename,
        "file_extension": result.extension,
        "detected_file_type": result.detected_file_type,
        "source_type": source_type,
        "source_institution": details.get("source_institution"),
        "suggested_category_code": classification.category_code,
        "suggested_category": classification.category_display,
        "suggested_confidence": classification.confidence,
        "final_category_code": final_category_code,
        "classification_method": classification.method,
        "classification_rules_matched": ",".join(classification.rules_matched),
        "document_title": title,
        "document_date": details.get("document_date"),
        "upload_method": "manual_streamlit_upload",
        "uploaded_by": "analyst",
        "analyst_notes": details.get("analyst_notes"),
        "authorization_confirmed": True,
        "upload_status": "UPLOADED" if status == config.DOCUMENT_STATUS_DOWNLOADED else status,
    }


def remove_uploaded_document(document: dict[str, Any], *, confirm: bool, db_path: Path | str = config.DATABASE_PATH) -> None:
    """Delete a manually uploaded file from disk and mark the DB record deleted."""
    if not confirm:
        raise ValueError("Deletion confirmation is required.")
    if int(document.get("is_public", 1)):
        raise ValueError("Public collection files cannot be deleted by the licensed upload workflow.")
    path_value = document.get("local_path")
    if path_value:
        path = Path(path_value)
        resolved = path.resolve()
        resolved.relative_to(config.DOWNLOAD_DIR.resolve())
        if resolved.exists():
            resolved.unlink()
    database.mark_document_deleted(document["document_id"], db_path=db_path)
    _audit(
        document["package_id"],
        "FILE_DELETED",
        {"document_id": document["document_id"], "filename": document.get("stored_filename")},
        document["document_id"],
        db_path=db_path,
    )
