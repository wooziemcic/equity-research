from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
from pathlib import Path
from typing import Any

from app import config
from app.services.checklist_service import ensure_package_checklist
from app.services.collectors.ir_collector import IrDocumentCandidate
from app.services.collectors.sec_collector import FilingCandidate, standardized_sec_filename
from app.services.workspace_service import package_workspace
from app.utils import database


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _audit(package_id: str, event_type: str, details: dict[str, Any], document_id: str | None, *, db_path: Path | str) -> None:
    database.create_audit_event(
        event_id=f"AUD-{secrets.token_hex(8).upper()}",
        package_id=package_id,
        document_id=document_id,
        event_type=event_type,
        event_details_json=json.dumps(details, sort_keys=True),
        db_path=db_path,
    )


def _record_outcome(
    summary: dict[str, Any],
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    if before and before.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
        summary["existing_records_reused"] += 1
    elif before:
        summary["records_repaired"] += 1
    else:
        summary["records_repaired"] += 1
    if before and before.get("document_id") == after.get("document_id") and before.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
        summary["duplicates_skipped"] += 1


def _sec_record(package: dict[str, Any], filing: FilingCandidate, path: Path, content: bytes) -> dict[str, Any]:
    sha = hashlib.sha256(content).hexdigest()
    return {
        "document_id": database.generate_document_id("DOC-SEC"),
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": "SEC Filing",
        "document_type": filing.form_type,
        "title": filing.title,
        "source_name": "SEC EDGAR",
        "source_url": filing.primary_document_url,
        "source_domain": "sec.gov",
        "accession_number": filing.accession_number,
        "form_type": filing.form_type,
        "publication_date": filing.filing_date,
        "report_period": filing.report_period,
        "local_filename": path.name,
        "local_path": str(path),
        "mime_type": mimetypes.guess_type(path.name)[0] or "text/html",
        "file_size_bytes": len(content),
        "sha256_hash": sha,
        "collection_method": "SEC",
        "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
        "is_public": True,
        "error_message": None,
    }


def _ir_record(package: dict[str, Any], candidate: IrDocumentCandidate, path: Path, content: bytes) -> dict[str, Any]:
    return {
        "document_id": database.generate_document_id("DOC-IR"),
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": candidate.suggested_category,
        "document_type": "PDF",
        "title": candidate.title,
        "source_name": "Investor Relations",
        "source_url": candidate.url,
        "source_domain": "",
        "publication_date": candidate.apparent_date,
        "local_filename": path.name,
        "local_path": str(path),
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/pdf",
        "file_size_bytes": len(content),
        "sha256_hash": hashlib.sha256(content).hexdigest(),
        "collection_method": "INVESTOR_RELATIONS",
        "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
        "is_public": True,
        "error_message": None,
    }


def _generic_record(package: dict[str, Any], path: Path, *, source: str, is_public: bool) -> dict[str, Any]:
    sha = _sha256(path)
    category = "SEC Filing" if source == "SEC" else "Public Document" if is_public else "Other"
    return {
        "document_id": database.generate_document_id("DOC-REPAIR"),
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": category,
        "document_type": path.suffix.lstrip(".").upper() or "FILE",
        "title": path.stem,
        "source_name": source,
        "source_url": f"repaired-file://{path.name}",
        "source_domain": "local",
        "publication_date": None,
        "local_filename": path.name,
        "local_path": str(path),
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "file_size_bytes": path.stat().st_size,
        "sha256_hash": sha,
        "source_identity_key": f"{'public' if is_public else 'upload'}:{sha}",
        "collection_method": "RECONCILIATION",
        "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
        "is_public": is_public,
        "error_message": None,
    }


def repair_package_document_records(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Repair package document rows from files already present in its workspace."""
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        raise ValueError("Package does not exist.")

    root = package_workspace(package_id)
    metadata_dir = root / "metadata"
    summary: dict[str, Any] = {
        "files_found": 0,
        "records_repaired": 0,
        "existing_records_reused": 0,
        "duplicates_skipped": 0,
        "items_still_failed": 0,
        "checklist_items_recalculated": 0,
        "superseded_failed_records": 0,
    }
    seen_paths: set[Path] = set()

    sec_payload = _load_json(metadata_dir / "sec_filing_inventory.json")
    for filing_data in sec_payload.get("filings", []) if isinstance(sec_payload.get("filings"), list) else []:
        try:
            filing = FilingCandidate(**filing_data)
        except TypeError:
            continue
        path = root / "sec" / standardized_sec_filename(package["ticker"], filing)
        if not path.exists():
            continue
        seen_paths.add(path.resolve())
        content = path.read_bytes()
        summary["files_found"] += 1
        before = database.get_document_by_accession(package_id, filing.accession_number, db_path=db_path) or database.get_document_by_url(package_id, filing.primary_document_url, db_path=db_path)
        after = database.create_document_record(_sec_record(package, filing, path, content), db_path=db_path)
        summary["superseded_failed_records"] += database.mark_failed_documents_superseded(
            package_id,
            accession_number=filing.accession_number,
            source_url=filing.primary_document_url,
            sha256_hash=after.get("sha256_hash"),
            source_identity_key_value=after.get("source_identity_key"),
            winning_document_id=after.get("document_id"),
            db_path=db_path,
        )
        _record_outcome(summary, before=before, after=after)
        _audit(package_id, "DOCUMENT_RECORD_REPAIRED", {"source": "SEC", "filename": path.name}, after.get("document_id"), db_path=db_path)

    ir_payload = _load_json(metadata_dir / "ir_discovery_results.json")
    for candidate_data in ir_payload.get("documents", []) if isinstance(ir_payload.get("documents"), list) else []:
        try:
            candidate = IrDocumentCandidate(**candidate_data)
        except TypeError:
            continue
        path = root / "investor_relations" / candidate.filename
        if not path.exists():
            continue
        seen_paths.add(path.resolve())
        content = path.read_bytes()
        summary["files_found"] += 1
        before = database.get_document_by_url(package_id, candidate.url, db_path=db_path)
        after = database.create_document_record(_ir_record(package, candidate, path, content), db_path=db_path)
        summary["superseded_failed_records"] += database.mark_failed_documents_superseded(
            package_id,
            source_url=candidate.url,
            sha256_hash=after.get("sha256_hash"),
            source_identity_key_value=after.get("source_identity_key"),
            winning_document_id=after.get("document_id"),
            db_path=db_path,
        )
        _record_outcome(summary, before=before, after=after)
        _audit(package_id, "DOCUMENT_RECORD_REPAIRED", {"source": "IR", "filename": path.name}, after.get("document_id"), db_path=db_path)

    for directory, source, is_public in (
        (root / "sec", "SEC", True),
        (root / "investor_relations", "Investor Relations", True),
        (root / "licensed", "Licensed Upload", False),
    ):
        if not directory.exists():
            continue
        for path in [item for item in directory.rglob("*") if item.is_file()]:
            if path.resolve() in seen_paths or path.suffix.lower() == ".json":
                continue
            summary["files_found"] += 1
            sha = _sha256(path)
            before = database.get_document_by_hash(package_id, sha, db_path=db_path)
            if before and before.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
                summary["existing_records_reused"] += 1
                summary["duplicates_skipped"] += 1
                continue
            after = database.create_document_record(_generic_record(package, path, source=source, is_public=is_public), db_path=db_path)
            summary["superseded_failed_records"] += database.mark_failed_documents_superseded(
                package_id,
                sha256_hash=after.get("sha256_hash"),
                source_identity_key_value=after.get("source_identity_key"),
                winning_document_id=after.get("document_id"),
                db_path=db_path,
            )
            _record_outcome(summary, before=before, after=after)
            _audit(package_id, "DOCUMENT_RECORD_REPAIRED", {"source": source, "filename": path.name}, after.get("document_id"), db_path=db_path)

    checklist = ensure_package_checklist(database.get_package_by_package_id(package_id, db_path=db_path) or package, db_path=db_path)
    summary["checklist_items_recalculated"] = len(checklist)
    summary["items_still_failed"] = database.document_counts_for_package(package_id, db_path=db_path)["failed"]
    return summary
