from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from app import config
from app.services.document_classifier import ClassificationSuggestion, classify_document
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


def validate_upload_batch(candidates: list[UploadCandidate], *, source_type: str) -> list[FileValidationResult]:
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
    by_original = {candidate.original_filename: candidate for candidate in candidates}
    for result in validations:
        candidate = by_original[result.original_filename]
        details = metadata_by_name.get(result.original_filename, {})
        document_id = f"DOC-UPLOAD-{secrets.token_hex(8).upper()}"
        if not result.is_valid:
            database.create_document_record(
                _document_record(package, result, document_id, source_type, config.DOCUMENT_STATUS_FAILED, details=details, error=result.error),
                db_path=db_path,
            )
            _audit(package["package_id"], "UPLOAD_FAILED", {"filename": result.original_filename, "error": result.error}, document_id, db_path=db_path)
            summary["failed"] += 1
            continue
        if database.document_exists_by_hash(package["package_id"], result.sha256_hash, db_path=db_path):
            database.create_document_record(
                _document_record(package, result, document_id, source_type, config.DOCUMENT_STATUS_DUPLICATE, details=details, content=None, error="Duplicate file hash in this package."),
                db_path=db_path,
            )
            _audit(package["package_id"], "DUPLICATE_DETECTED", {"filename": result.original_filename, "sha256": result.sha256_hash}, document_id, db_path=db_path)
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
        "source_name": config.LICENSED_SOURCE_TYPES.get(source_type, "Licensed Upload"),
        "source_url": f"local-upload://{result.sanitized_filename}",
        "source_domain": "local",
        "publication_date": details.get("publication_date"),
        "local_filename": local_path.name if local_path else result.sanitized_filename,
        "local_path": str(local_path) if local_path else None,
        "mime_type": mimetypes.guess_type(result.sanitized_filename)[0] or "application/octet-stream",
        "file_size_bytes": result.file_size_bytes,
        "sha256_hash": result.sha256_hash,
        "collection_method": "LICENSED_UPLOAD",
        "collection_status": status,
        "is_public": False,
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
