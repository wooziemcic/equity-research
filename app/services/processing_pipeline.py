from __future__ import annotations

import json
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable

from app import config
from app.services import processing_workspace
from app.services.document_processing import ParsedDocument, chunk_parsed_document, parse_version_document
from app.services.evidence_service import (
    detect_claim_conflicts,
    detect_duplicate_groups,
    evidence_from_chunk,
    prepare_evidence_verifications,
    verify_evidence_record,
    verify_evidence_records_batch,
)
from app.services.package_builder import sha256_file, verify_snapshot
from app.utils import database


def processing_fingerprint(
    version_id: str,
    *,
    ocr_config: dict[str, Any],
    retrieval_config: dict[str, Any],
    db_path: Path | str = config.DATABASE_PATH,
) -> str:
    documents = database.list_package_version_documents(version_id, db_path=db_path)
    payload = {
        "version_id": version_id,
        "document_hashes": sorted(doc.get("sha256_hash") or "" for doc in documents),
        "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
        "parser_version": config.PARSER_CONFIG_VERSION,
        "chunk_configuration": retrieval_config,
        "ocr_configuration": ocr_config,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProcessingEligibility:
    is_eligible: bool
    version: dict[str, Any] | None
    version_documents: list[dict[str, Any]]
    root: Path | None
    errors: list[str]
    warnings: list[str]
    integrity_report: dict[str, Any] | None = None


def _event_id() -> str:
    return f"PVE-{secrets.token_hex(8).upper()}"


def _run_id() -> str:
    return f"RUN-PROC-{secrets.token_hex(8).upper()}"


def _result_id() -> str:
    return f"DPR-{secrets.token_hex(8).upper()}"


def _page_id() -> str:
    return f"DPG-{secrets.token_hex(8).upper()}"


def _sheet_id() -> str:
    return f"DSH-{secrets.token_hex(8).upper()}"


def _chunk_id() -> str:
    return f"CHK-{secrets.token_hex(8).upper()}"


def _version_root(version: dict[str, Any]) -> Path | None:
    manifest_path = version.get("manifest_path")
    if manifest_path:
        return Path(manifest_path).resolve().parents[1]
    return None


def _record_version_event(
    *,
    version: dict[str, Any],
    event_type: str,
    details: dict[str, Any] | None = None,
    db_path: Path | str,
) -> None:
    database.create_package_version_event(
        event_id=_event_id(),
        parent_package_id=version["parent_package_id"],
        version_id=version["version_id"],
        event_type=event_type,
        event_details_json=json.dumps(details or {}, sort_keys=True),
        db_path=db_path,
    )


def validate_processing_eligibility(
    version_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
    record_event: bool = True,
) -> ProcessingEligibility:
    errors: list[str] = []
    warnings: list[str] = []
    version = database.get_package_version(version_id, db_path=db_path)
    if not version:
        return ProcessingEligibility(False, None, [], None, ["Package version does not exist."], [])
    if version.get("status") != config.VERSION_STATUS_LOCKED:
        errors.append("Only LOCKED package versions can be processed.")
    if version.get("integrity_status") not in {config.INTEGRITY_VERIFIED, config.INTEGRITY_VERIFIED_WITH_WARNINGS}:
        errors.append("Package version integrity status must be VERIFIED or VERIFIED_WITH_WARNINGS.")
    manifest_path = Path(version.get("manifest_path") or "")
    if not version.get("manifest_path") or not manifest_path.exists():
        errors.append("Package manifest is missing.")
    version_docs = database.list_package_version_documents(version_id, db_path=db_path)
    if not version_docs:
        errors.append("Package version has no version documents.")
    root = _version_root(version)
    integrity_report: dict[str, Any] | None = None
    if root is None or not root.exists():
        errors.append("Package version root directory is missing.")
    else:
        for doc in version_docs:
            source_path = (root / doc["relative_package_path"]).resolve()
            try:
                source_path.relative_to(root.resolve())
            except ValueError:
                errors.append(f"Document escapes the locked package root: {doc['relative_package_path']}")
                continue
            if not source_path.exists():
                errors.append(f"Document file is missing: {doc['relative_package_path']}")
                continue
            if source_path.stat().st_size != int(doc["file_size"]):
                errors.append(f"Document file size changed: {doc['relative_package_path']}")
            if sha256_file(source_path) != doc["sha256_hash"]:
                errors.append(f"Document hash changed: {doc['relative_package_path']}")
        if not errors:
            integrity_report = verify_snapshot(root, version_docs, version.get("manifest_sha256"))
            if integrity_report["overall_integrity_status"] == config.INTEGRITY_FAILED:
                errors.extend(integrity_report.get("missing_files", []))
                errors.extend(integrity_report.get("hash_mismatches", []))
                errors.extend(integrity_report.get("size_mismatches", []))
            if integrity_report.get("unexpected_files"):
                warnings.append("Locked package contains unexpected files recorded as integrity warnings.")
    eligible = not errors
    if not eligible and record_event:
        _record_version_event(
            version=version,
            event_type="PROCESSING_ELIGIBILITY_FAILED",
            details={"errors": errors, "warnings": warnings},
            db_path=db_path,
        )
    return ProcessingEligibility(eligible, version, version_docs, root, errors, warnings, integrity_report)


def run_processing_pipeline(
    version_id: str,
    *,
    ocr_enabled: bool | None = None,
    retrieval_mode: str | None = None,
    created_by: str = "analyst",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    return _run_resumable_processing(
        version_id,
        ocr_enabled=ocr_enabled,
        retrieval_mode=retrieval_mode,
        created_by=created_by,
        progress_callback=progress_callback,
        max_workers=max_workers,
        db_path=db_path,
    )
    eligibility = validate_processing_eligibility(version_id, db_path=db_path)
    if not eligibility.is_eligible or not eligibility.version or not eligibility.root:
        raise ValueError("Processing blocked: " + "; ".join(eligibility.errors))
    version = eligibility.version
    ocr_config = {
        "enabled": config.OCR_ENABLED if ocr_enabled is None else bool(ocr_enabled),
        "max_pages": config.MAX_OCR_PAGES,
        "confidence_threshold": config.OCR_CONFIDENCE_THRESHOLD,
    }
    retrieval_config = {
        "mode": retrieval_mode or config.RETRIEVAL_MODE,
        "result_count": config.RETRIEVAL_RESULT_COUNT,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
    }
    fingerprint = processing_fingerprint(
        version_id, ocr_config=ocr_config, retrieval_config=retrieval_config, db_path=db_path
    )
    existing_runs = database.list_processing_runs(version_id, db_path=db_path)
    reusable_statuses = {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}
    for existing in existing_runs:
        exact = existing.get("processing_fingerprint") == fingerprint
        legacy_compatible = (
            not existing.get("processing_fingerprint")
            and existing.get("pipeline_version") == config.PROCESSING_PIPELINE_VERSION
            and existing.get("parser_config_version") == config.PARSER_CONFIG_VERSION
            and json.loads(existing.get("ocr_config_json") or "{}") == ocr_config
            and json.loads(existing.get("retrieval_config_json") or "{}") == retrieval_config
        )
        if existing.get("status") in reusable_statuses and (exact or legacy_compatible):
            return existing
    run_id = _run_id()
    run = database.create_processing_run(
        {
            "processing_run_id": run_id,
            "version_id": version_id,
            "package_id": version["parent_package_id"],
            "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "embedding_config_json": json.dumps({"model": config.LOCAL_EMBEDDING_MODEL, "enabled": False}, sort_keys=True),
            "ocr_config_json": json.dumps(ocr_config, sort_keys=True),
            "retrieval_config_json": json.dumps(retrieval_config, sort_keys=True),
            "started_at": database.utc_now_iso(),
            "completed_at": None,
            "total_documents": len(eligibility.version_documents),
            "successful_documents": 0,
            "partial_documents": 0,
            "failed_documents": 0,
            "pages_processed": 0,
            "tables_detected": 0,
            "sheets_processed": 0,
            "chunks_created": 0,
            "evidence_records_created": 0,
            "warnings_json": json.dumps(eligibility.warnings, sort_keys=True),
            "errors_json": json.dumps([], sort_keys=True),
            "created_by": created_by,
            "status": config.PROCESSING_STATUS_RUNNING,
            "processing_fingerprint": fingerprint,
            "reused_from_processing_run_id": None,
            "duration_seconds": None,
        },
        db_path=db_path,
    )
    workspace = processing_workspace.processing_run_workspace(version_id, run_id)
    _record_version_event(version=version, event_type="PROCESSING_STARTED", details={"processing_run_id": run_id}, db_path=db_path)
    warnings = list(eligibility.warnings)
    errors: list[str] = []
    stats = {
        "successful_documents": 0,
        "partial_documents": 0,
        "failed_documents": 0,
        "pages_processed": 0,
        "tables_detected": 0,
        "sheets_processed": 0,
        "chunks_created": 0,
        "evidence_records_created": 0,
    }
    try:
        chunk_rows: list[dict[str, Any]] = []
        for version_doc in eligibility.version_documents:
            source_path = (eligibility.root / version_doc["relative_package_path"]).resolve()
            parsed = parse_version_document(
                version_doc=version_doc,
                source_path=source_path,
                version_id=version_id,
                processing_run_id=run_id,
                ocr_enabled=ocr_config["enabled"],
            )
            _store_document_result(parsed, run_id=run_id, version_id=version_id, version_doc=version_doc, db_path=db_path)
            warnings.extend(f"{version_doc['document_id']}: {warning}" for warning in parsed.warnings)
            if parsed.error_message:
                errors.append(f"{version_doc['document_id']}: {parsed.error_message}")
            _update_stats_for_document(stats, parsed)
            for page in parsed.pages:
                database.create_document_page(
                    {
                        "page_record_id": _page_id(),
                        "processing_run_id": run_id,
                        "version_document_id": version_doc["document_id"],
                        "page_number": page.page_number,
                        "page_label": page.page_label,
                        "extraction_method": page.extraction_method,
                        "native_text_character_count": page.native_text_character_count,
                        "ocr_text_character_count": page.ocr_text_character_count,
                        "ocr_confidence": page.ocr_confidence,
                        "page_text_path": page.page_text_path,
                        "normalized_text": page.text[:4000],
                        "image_render_path": page.image_render_path,
                        "processing_warnings_json": json.dumps(page.warnings, sort_keys=True),
                        "created_at": database.utc_now_iso(),
                    },
                    db_path=db_path,
                )
            for sheet in parsed.sheets:
                database.create_document_sheet(
                    {
                        "sheet_record_id": _sheet_id(),
                        "processing_run_id": run_id,
                        "version_document_id": version_doc["document_id"],
                        "sheet_name": sheet.sheet_name,
                        "sheet_index": sheet.sheet_index,
                        "hidden_state": sheet.hidden_state,
                        "used_range": sheet.used_range,
                        "formula_cell_count": sheet.formula_cell_count,
                        "cached_value_cell_count": sheet.cached_value_cell_count,
                        "external_link_count": sheet.external_link_count,
                        "warning_flags": ",".join(sheet.warning_flags),
                        "extracted_representation_path": sheet.extracted_representation_path,
                        "created_at": database.utc_now_iso(),
                    },
                    db_path=db_path,
                )
            for draft in chunk_parsed_document(parsed):
                locator = dict(draft.source_locator)
                locator.update({"version_id": version_id, "processing_run_id": run_id})
                chunk = {
                    "chunk_id": _chunk_id(),
                    "processing_run_id": run_id,
                    "version_id": version_id,
                    "version_document_id": version_doc["document_id"],
                    "page_number": draft.page_number,
                    "sheet_name": draft.sheet_name,
                    "row_range": draft.row_range,
                    "section_heading": draft.section_heading,
                    "chunk_index": draft.chunk_index,
                    "chunk_text": draft.chunk_text,
                    "character_count": draft.character_count,
                    "token_estimate": draft.token_estimate,
                    "extraction_method": draft.extraction_method,
                    "source_locator_json": json.dumps(locator, sort_keys=True),
                    "chunk_hash": draft.chunk_hash,
                    "duplicate_group_id": None,
                    "created_at": database.utc_now_iso(),
                }
                database.create_document_chunk(chunk, db_path=db_path)
                chunk_rows.append(chunk)
                for evidence in evidence_from_chunk(chunk):
                    database.create_evidence_record(evidence, db_path=db_path)
                    verify_evidence_record(evidence, db_path=db_path)
                    stats["evidence_records_created"] += 1
                stats["chunks_created"] += 1
        duplicate_groups = detect_duplicate_groups(processing_run_id=run_id, version_id=version_id, db_path=db_path)
        conflicts = detect_claim_conflicts(processing_run_id=run_id, db_path=db_path)
        summary = {
            "processing_run_id": run_id,
            "version_id": version_id,
            "package_id": version["parent_package_id"],
            "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "ocr_config": ocr_config,
            "retrieval_config": retrieval_config,
            "stats": stats,
            "duplicate_groups": len(duplicate_groups),
            "conflicts": len(conflicts),
            "warnings": warnings,
            "errors": errors,
        }
        processing_workspace.atomic_write_json(workspace / "chunks" / "chunks.json", chunk_rows)
        processing_workspace.atomic_write_json(workspace / "run_summary.json", summary)
        status = (
            config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS
            if warnings or stats["partial_documents"] or stats["failed_documents"]
            else config.PROCESSING_STATUS_COMPLETED
        )
        started_value = datetime.fromisoformat(run["started_at"])
        duration = max(0.0, (datetime.fromisoformat(database.utc_now_iso()) - started_value).total_seconds())
        run = database.update_processing_run(
            run_id,
            {
                **stats,
                "completed_at": database.utc_now_iso(),
                "warnings_json": json.dumps(warnings, sort_keys=True),
                "errors_json": json.dumps(errors, sort_keys=True),
                "status": status,
                "duration_seconds": duration,
            },
            db_path=db_path,
        ) or run
        _record_version_event(version=version, event_type="PROCESSING_COMPLETED", details=summary, db_path=db_path)
        return run
    except Exception as exc:
        errors.append(str(exc))
        database.update_processing_run(
            run_id,
            {
                **stats,
                "completed_at": database.utc_now_iso(),
                "warnings_json": json.dumps(warnings, sort_keys=True),
                "errors_json": json.dumps(errors, sort_keys=True),
                "status": config.PROCESSING_STATUS_FAILED,
            },
            db_path=db_path,
        )
        processing_workspace.atomic_write_json(
            workspace / "run_summary.json",
            {
                "processing_run_id": run_id,
                "version_id": version_id,
                "status": config.PROCESSING_STATUS_FAILED,
                "warnings": warnings,
                "errors": errors,
            },
        )
        _record_version_event(version=version, event_type="PROCESSING_FAILED", details={"processing_run_id": run_id, "error": str(exc)}, db_path=db_path)
        raise


def repair_processing_run(
    processing_run_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Parse only locked-version documents that still have no chunks, preserving the run."""
    return resume_processing_run(processing_run_id, retry_failed=True, db_path=db_path)
    run = database.get_processing_run(processing_run_id, db_path=db_path)
    if not run:
        raise ValueError("Processing run does not exist.")
    eligibility = validate_processing_eligibility(run["version_id"], db_path=db_path)
    if not eligibility.is_eligible or not eligibility.version or not eligibility.root:
        raise ValueError("Processing repair blocked: " + "; ".join(eligibility.errors))
    existing_chunks = database.list_document_chunks(processing_run_id, version_id=run["version_id"], db_path=db_path)
    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in existing_chunks:
        chunks_by_document.setdefault(chunk["version_document_id"], []).append(chunk)
    if int(run.get("failed_documents") or 0) > 0:
        repair_docs = list(eligibility.version_documents)
    else:
        repair_docs = [doc for doc in eligibility.version_documents if doc["document_id"] not in chunks_by_document]
    if not repair_docs:
        return run
    ocr_config = json.loads(run.get("ocr_config_json") or "{}")
    errors: list[str] = []
    warnings: list[str] = []
    for version_doc in repair_docs:
        source_path = (eligibility.root / version_doc["relative_package_path"]).resolve()
        parsed = parse_version_document(
            version_doc=version_doc,
            source_path=source_path,
            version_id=run["version_id"],
            processing_run_id=processing_run_id,
            ocr_enabled=bool(ocr_config.get("enabled", False)),
        )
        _store_document_result(parsed, run_id=processing_run_id, version_id=run["version_id"], version_doc=version_doc, db_path=db_path)
        warnings.extend(f"{version_doc['document_id']}: {warning}" for warning in parsed.warnings)
        if parsed.error_message:
            errors.append(f"{version_doc['document_id']}: {parsed.error_message}")
        for page in parsed.pages:
            database.create_document_page(
                {
                    "page_record_id": _page_id(),
                    "processing_run_id": processing_run_id,
                    "version_document_id": version_doc["document_id"],
                    "page_number": page.page_number,
                    "page_label": page.page_label,
                    "extraction_method": page.extraction_method,
                    "native_text_character_count": page.native_text_character_count,
                    "ocr_text_character_count": page.ocr_text_character_count,
                    "ocr_confidence": page.ocr_confidence,
                    "page_text_path": page.page_text_path,
                    "normalized_text": page.text[:4000],
                    "image_render_path": page.image_render_path,
                    "processing_warnings_json": json.dumps(page.warnings, sort_keys=True),
                    "created_at": database.utc_now_iso(),
                },
                db_path=db_path,
            )
        existing_keys = {
            (
                chunk["chunk_hash"],
                chunk.get("page_number"),
                chunk.get("sheet_name"),
                chunk.get("row_range"),
                chunk.get("section_heading"),
                chunk.get("chunk_index"),
            )
            for chunk in chunks_by_document.get(version_doc["document_id"], [])
        }
        new_chunks: list[dict[str, Any]] = []
        new_evidence: list[dict[str, Any]] = []
        for draft in chunk_parsed_document(parsed):
            draft_key = (
                draft.chunk_hash,
                draft.page_number,
                draft.sheet_name,
                draft.row_range,
                draft.section_heading,
                draft.chunk_index,
            )
            if draft_key in existing_keys:
                continue
            locator = dict(draft.source_locator)
            locator.update({"version_id": run["version_id"], "processing_run_id": processing_run_id})
            chunk = {
                "chunk_id": _chunk_id(),
                "processing_run_id": processing_run_id,
                "version_id": run["version_id"],
                "version_document_id": version_doc["document_id"],
                "page_number": draft.page_number,
                "sheet_name": draft.sheet_name,
                "row_range": draft.row_range,
                "section_heading": draft.section_heading,
                "chunk_index": draft.chunk_index,
                "chunk_text": draft.chunk_text,
                "character_count": draft.character_count,
                "token_estimate": draft.token_estimate,
                "extraction_method": draft.extraction_method,
                "source_locator_json": json.dumps(locator, sort_keys=True),
                "chunk_hash": draft.chunk_hash,
                "duplicate_group_id": None,
                "created_at": database.utc_now_iso(),
            }
            new_chunks.append(chunk)
            new_evidence.extend(evidence_from_chunk(chunk))
        database.create_document_chunks(new_chunks, db_path=db_path)
        database.create_evidence_records(new_evidence, db_path=db_path)
        verify_evidence_records_batch(
            new_evidence,
            {chunk["chunk_id"]: chunk for chunk in new_chunks},
            db_path=db_path,
        )
    results = database.list_document_processing_results(processing_run_id, db_path=db_path)
    latest_by_document = {item["version_document_id"]: item for item in results}
    chunks = database.list_document_chunks(processing_run_id, version_id=run["version_id"], db_path=db_path)
    evidence = database.list_evidence_records(processing_run_id, version_id=run["version_id"], db_path=db_path)
    statuses = [item.get("processing_status") for item in latest_by_document.values()]
    failed = sum(status == config.DOCUMENT_PROCESSING_FAILED for status in statuses)
    partial = sum(status in {config.DOCUMENT_PROCESSING_PARTIAL, config.DOCUMENT_PROCESSING_SKIPPED} for status in statuses)
    successful = sum(status == config.DOCUMENT_PROCESSING_SUCCESS for status in statuses)
    status = config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS if failed or partial or warnings else config.PROCESSING_STATUS_COMPLETED
    updated = database.update_processing_run(
        processing_run_id,
        {
            "completed_at": database.utc_now_iso(),
            "successful_documents": successful,
            "partial_documents": partial,
            "failed_documents": failed,
            "pages_processed": len(database.list_document_pages(processing_run_id, db_path=db_path)),
            "chunks_created": len(chunks),
            "evidence_records_created": len(evidence),
            "warnings_json": json.dumps(sorted(set(warnings)), sort_keys=True),
            "errors_json": json.dumps(sorted(set(errors)), sort_keys=True),
            "status": status,
        },
        db_path=db_path,
    ) or run
    _record_version_event(
        version=eligibility.version,
        event_type="PROCESSING_REPAIRED",
        details={
            "processing_run_id": processing_run_id,
            "documents_reprocessed": len(repair_docs),
            "chunks_available": len(chunks),
            "evidence_records": len(evidence),
        },
        db_path=db_path,
    )
    return updated


def _store_document_result(
    parsed: ParsedDocument,
    *,
    run_id: str,
    version_id: str,
    version_doc: dict[str, Any],
    db_path: Path | str,
) -> None:
    database.create_document_processing_result(
        {
            "result_id": _result_id(),
            "processing_run_id": run_id,
            "version_id": version_id,
            "version_document_id": version_doc["document_id"],
            "original_document_id": version_doc.get("original_document_id"),
            "parser_used": parsed.parser_used,
            "parser_version": parsed.parser_version,
            "processing_status": parsed.status,
            "detected_language": parsed.detected_language,
            "page_count": parsed.page_count,
            "sheet_count": parsed.sheet_count,
            "extracted_character_count": parsed.extracted_character_count,
            "ocr_required": int(parsed.ocr_required),
            "ocr_pages": parsed.ocr_pages,
            "table_count": parsed.table_count,
            "warning_count": len(parsed.warnings),
            "error_message": parsed.error_message,
            "extracted_content_path": parsed.full_text_path,
            "created_at": database.utc_now_iso(),
            "updated_at": database.utc_now_iso(),
        },
        db_path=db_path,
    )


def _update_stats_for_document(stats: dict[str, int], parsed: ParsedDocument) -> None:
    if parsed.status == config.DOCUMENT_PROCESSING_SUCCESS:
        stats["successful_documents"] += 1
    elif parsed.status == config.DOCUMENT_PROCESSING_PARTIAL or parsed.status == config.DOCUMENT_PROCESSING_SKIPPED:
        stats["partial_documents"] += 1
    else:
        stats["failed_documents"] += 1
    stats["pages_processed"] += parsed.page_count
    stats["sheets_processed"] += parsed.sheet_count
    stats["tables_detected"] += parsed.table_count


@dataclass(frozen=True)
class _PreparedDocument:
    version_doc: dict[str, Any]
    fingerprint: str
    parsed: ParsedDocument
    result: dict[str, Any]
    item: dict[str, Any]
    pages: list[dict[str, Any]]
    sheets: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    verifications: list[dict[str, Any]]
    chunking_seconds: float
    extraction_seconds: float


def document_processing_fingerprint(
    version_doc: dict[str, Any], *, ocr_config: dict[str, Any], retrieval_config: dict[str, Any]
) -> str:
    payload = {
        "version_document_id": version_doc["document_id"],
        "file_sha256": version_doc.get("sha256_hash"),
        "parser_version": config.PARSER_CONFIG_VERSION,
        "ocr_configuration": ocr_config,
        "chunk_size": retrieval_config.get("chunk_size"),
        "chunk_overlap": retrieval_config.get("chunk_overlap"),
        "extraction_configuration": config.PROCESSING_EXTRACTION_CONFIG_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _run_resumable_processing(
    version_id: str,
    *,
    ocr_enabled: bool | None,
    retrieval_mode: str | None,
    created_by: str,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    max_workers: int | None,
    db_path: Path | str,
) -> dict[str, Any]:
    eligibility = validate_processing_eligibility(version_id, db_path=db_path)
    if not eligibility.is_eligible or not eligibility.version:
        raise ValueError("Processing blocked: " + "; ".join(eligibility.errors))
    ocr_config = {
        "enabled": config.OCR_ENABLED if ocr_enabled is None else bool(ocr_enabled),
        "max_pages": config.MAX_OCR_PAGES,
        "confidence_threshold": config.OCR_CONFIDENCE_THRESHOLD,
    }
    retrieval_config = {
        "mode": retrieval_mode or config.RETRIEVAL_MODE,
        "result_count": config.RETRIEVAL_RESULT_COUNT,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
    }
    fingerprint = processing_fingerprint(
        version_id, ocr_config=ocr_config, retrieval_config=retrieval_config, db_path=db_path
    )
    reusable_statuses = {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}
    runs = database.list_processing_runs(version_id, db_path=db_path)
    for existing in runs:
        if existing.get("processing_fingerprint") == fingerprint and existing.get("status") in reusable_statuses:
            _emit_progress(
                progress_callback,
                stage="Package reused",
                total=int(existing.get("total_documents") or 0),
                completed=int(existing.get("successful_documents") or 0) + int(existing.get("partial_documents") or 0),
                reused=int(existing.get("total_documents") or 0),
                failed=int(existing.get("failed_documents") or 0),
                elapsed=float(existing.get("duration_seconds") or 0),
            )
            return existing
    for existing in runs:
        items = database.list_processing_document_items(existing["processing_run_id"], db_path=db_path)
        if (
            existing.get("processing_fingerprint") == fingerprint
            and existing.get("status") in {
                config.PROCESSING_STATUS_RUNNING,
                config.PROCESSING_STATUS_INTERRUPTED,
                config.PROCESSING_STATUS_PARTIAL,
            }
            and items
        ):
            return resume_processing_run(
                existing["processing_run_id"],
                retry_failed=False,
                progress_callback=progress_callback,
                max_workers=max_workers,
                db_path=db_path,
            )

    run_id = _run_id()
    started = database.utc_now_iso()
    run = database.create_processing_run(
        {
            "processing_run_id": run_id,
            "version_id": version_id,
            "package_id": eligibility.version["parent_package_id"],
            "pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "embedding_config_json": json.dumps({"model": config.LOCAL_EMBEDDING_MODEL, "enabled": False}, sort_keys=True),
            "ocr_config_json": json.dumps(ocr_config, sort_keys=True),
            "retrieval_config_json": json.dumps(retrieval_config, sort_keys=True),
            "started_at": started,
            "completed_at": None,
            "total_documents": len(eligibility.version_documents),
            "successful_documents": 0,
            "partial_documents": 0,
            "failed_documents": 0,
            "pages_processed": 0,
            "tables_detected": 0,
            "sheets_processed": 0,
            "chunks_created": 0,
            "evidence_records_created": 0,
            "warnings_json": json.dumps(eligibility.warnings, sort_keys=True),
            "errors_json": "[]",
            "created_by": created_by,
            "status": config.PROCESSING_STATUS_RUNNING,
            "processing_fingerprint": fingerprint,
            "reused_from_processing_run_id": None,
            "duration_seconds": None,
            "last_checkpoint_at": None,
            "resume_count": 0,
            "reused_documents": 0,
            "database_write_seconds": 0.0,
            "chunking_seconds": 0.0,
            "deterministic_extraction_seconds": 0.0,
            "conflict_analysis_seconds": 0.0,
            "openai_extraction_seconds": 0.0,
        },
        db_path=db_path,
    )
    database.initialize_processing_document_items(
        [
            {
                "processing_run_id": run_id,
                "version_id": version_id,
                "version_document_id": doc["document_id"],
                "processing_fingerprint": document_processing_fingerprint(
                    doc, ocr_config=ocr_config, retrieval_config=retrieval_config
                ),
                "status": config.PROCESSING_STATUS_PENDING,
                "attempt_count": 0,
                "reuse_status": "NEW",
                "parse_started_at": None,
                "parse_completed_at": None,
                "parse_duration_seconds": 0.0,
                "file_size_bytes": int(doc.get("file_size") or 0),
                "document_type": Path(str(doc.get("relative_package_path") or "")).suffix.lower().lstrip("."),
                "extracted_character_count": 0,
                "page_count": 0,
                "chunk_count": 0,
                "evidence_count": 0,
                "warning_count": 0,
                "error_message": None,
                "updated_at": started,
            }
            for doc in eligibility.version_documents
        ],
        db_path=db_path,
    )
    _record_version_event(
        version=eligibility.version,
        event_type="PROCESSING_STARTED",
        details={"processing_run_id": run_id, "document_checkpoints": True},
        db_path=db_path,
    )
    return resume_processing_run(
        run_id,
        retry_failed=False,
        progress_callback=progress_callback,
        max_workers=max_workers,
        db_path=db_path,
    )


def resume_processing_run(
    processing_run_id: str,
    *,
    retry_failed: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    run = database.get_processing_run(processing_run_id, db_path=db_path)
    if not run:
        raise ValueError("Processing run does not exist.")
    eligibility = validate_processing_eligibility(run["version_id"], db_path=db_path)
    if not eligibility.is_eligible or not eligibility.version or not eligibility.root:
        raise ValueError("Processing resume blocked: " + "; ".join(eligibility.errors))
    ocr_config = json.loads(run.get("ocr_config_json") or "{}")
    retrieval_config = json.loads(run.get("retrieval_config_json") or "{}")
    items = {
        item["version_document_id"]: item
        for item in database.list_processing_document_items(processing_run_id, db_path=db_path)
    }
    documents = sorted(eligibility.version_documents, key=lambda row: row["document_id"])
    pending: list[dict[str, Any]] = []
    reused = 0
    for document in documents:
        expected = document_processing_fingerprint(
            document, ocr_config=ocr_config, retrieval_config=retrieval_config
        )
        item = items.get(document["document_id"])
        completed = item and item.get("status") in {"COMPLETED", "PARTIAL"}
        fingerprint_matches = item and item.get("processing_fingerprint") == expected
        failed_retry = retry_failed and item and item.get("status") == "FAILED"
        if completed and fingerprint_matches:
            reused += 1
            continue
        if item and item.get("status") == "FAILED" and not retry_failed and fingerprint_matches:
            continue
        if failed_retry or not completed or not fingerprint_matches:
            pending.append(document)

    database.update_processing_run(
        processing_run_id,
        {
            "status": config.PROCESSING_STATUS_RUNNING,
            "completed_at": None,
            "resume_count": int(run.get("resume_count") or 0) + int(bool(items)),
            "reused_documents": reused,
        },
        db_path=db_path,
    )
    started_perf = perf_counter()
    worker_count = max_workers if max_workers is not None else config.PROCESSING_MAX_WORKERS
    worker_count = max(1, min(int(worker_count), 4))
    if not config.PROCESSING_CONCURRENCY_ENABLED or bool(ocr_config.get("enabled")):
        worker_count = 1
    completed_before = reused
    _emit_progress(
        progress_callback,
        stage=f"Processing {completed_before} of {len(documents)} documents",
        total=len(documents),
        completed=completed_before,
        reused=reused,
        failed=sum(1 for item in items.values() if item.get("status") == "FAILED"),
        elapsed=0.0,
    )
    try:
        for prepared in _prepare_documents_bounded(
            pending,
            workers=worker_count,
            root=eligibility.root,
            version_id=run["version_id"],
            run_id=processing_run_id,
            ocr_config=ocr_config,
            retrieval_config=retrieval_config,
            prior_items=items,
        ):
            write_started = perf_counter()
            database.commit_processed_document(
                result=prepared.result,
                item=prepared.item,
                pages=prepared.pages,
                sheets=prepared.sheets,
                chunks=prepared.chunks,
                evidence=prepared.evidence,
                verifications=prepared.verifications,
                db_path=db_path,
            )
            write_seconds = perf_counter() - write_started
            for stage, duration in (
                ("parse", float(prepared.item["parse_duration_seconds"])),
                ("chunking", prepared.chunking_seconds),
                ("deterministic_extraction", prepared.extraction_seconds),
                ("database_write", write_seconds),
            ):
                database.create_processing_stage_timing(
                    {
                        "timing_id": f"PST-{secrets.token_hex(8).upper()}",
                        "processing_run_id": processing_run_id,
                        "version_document_id": prepared.version_doc["document_id"],
                        "stage_name": stage,
                        "duration_seconds": round(duration, 6),
                        "details_json": json.dumps(
                            {
                                "document_type": prepared.item["document_type"],
                                "file_size_bytes": prepared.item["file_size_bytes"],
                                "pages": prepared.item["page_count"],
                                "chunks": prepared.item["chunk_count"],
                                "evidence_records": prepared.item["evidence_count"],
                                "reuse_status": prepared.item["reuse_status"],
                            },
                            sort_keys=True,
                        ),
                        "created_at": database.utc_now_iso(),
                    },
                    db_path=db_path,
                )
            items[prepared.version_doc["document_id"]] = prepared.item
            completed_now = sum(1 for item in items.values() if item.get("status") in {"COMPLETED", "PARTIAL", "FAILED"})
            failed_now = sum(1 for item in items.values() if item.get("status") == "FAILED")
            elapsed = perf_counter() - started_perf
            database.update_processing_run(
                processing_run_id,
                {"last_checkpoint_at": database.utc_now_iso()},
                db_path=db_path,
            )
            _emit_progress(
                progress_callback,
                stage=f"Processing {completed_now} of {len(documents)} documents",
                current_document=prepared.version_doc.get("title") or prepared.version_doc["document_id"],
                total=len(documents),
                completed=completed_now,
                reused=reused,
                failed=failed_now,
                elapsed=elapsed,
                estimated_remaining=(elapsed / max(completed_now - completed_before, 1)) * max(len(documents) - completed_now, 0),
            )

        duplicate_started = perf_counter()
        if not database.list_duplicate_groups(processing_run_id, db_path=db_path):
            detect_duplicate_groups(processing_run_id=processing_run_id, version_id=run["version_id"], db_path=db_path)
        database.create_processing_stage_timing(
            {
                "timing_id": f"PST-{secrets.token_hex(8).upper()}",
                "processing_run_id": processing_run_id,
                "version_document_id": None,
                "stage_name": "duplicate_analysis",
                "duration_seconds": round(perf_counter() - duplicate_started, 6),
                "details_json": None,
                "created_at": database.utc_now_iso(),
            },
            db_path=db_path,
        )
        conflict_started = perf_counter()
        detect_claim_conflicts(processing_run_id=processing_run_id, db_path=db_path)
        conflict_seconds = perf_counter() - conflict_started
        database.create_processing_stage_timing(
            {
                "timing_id": f"PST-{secrets.token_hex(8).upper()}",
                "processing_run_id": processing_run_id,
                "version_document_id": None,
                "stage_name": "conflict_analysis",
                "duration_seconds": round(conflict_seconds, 6),
                "details_json": json.dumps(database.get_conflict_analysis_summary(processing_run_id, db_path=db_path) or {}, sort_keys=True),
                "created_at": database.utc_now_iso(),
            },
            db_path=db_path,
        )
        return _finalize_resumable_run(
            run,
            eligibility.version,
            conflict_seconds=conflict_seconds,
            db_path=db_path,
        )
    except BaseException as exc:
        snapshot = _processing_run_stats(processing_run_id, db_path=db_path)
        database.update_processing_run(
            processing_run_id,
            {
                **snapshot,
                "status": config.PROCESSING_STATUS_INTERRUPTED,
                "completed_at": database.utc_now_iso(),
                "errors_json": json.dumps([str(exc)], sort_keys=True),
            },
            db_path=db_path,
        )
        _record_version_event(
            version=eligibility.version,
            event_type="PROCESSING_INTERRUPTED",
            details={"processing_run_id": processing_run_id, "completed_documents": snapshot["successful_documents"] + snapshot["partial_documents"]},
            db_path=db_path,
        )
        raise


def retry_failed_documents(
    processing_run_id: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    return resume_processing_run(
        processing_run_id,
        retry_failed=True,
        progress_callback=progress_callback,
        max_workers=max_workers,
        db_path=db_path,
    )


def processing_performance_summary(
    processing_run_id: str, *, db_path: Path | str = config.DATABASE_PATH
) -> dict[str, Any]:
    results = database.list_document_processing_results(processing_run_id, db_path=db_path)
    timings = database.list_processing_stage_timings(processing_run_id, db_path=db_path)
    slowest_documents = sorted(
        results,
        key=lambda row: float(row.get("parse_duration_seconds") or 0),
        reverse=True,
    )[:10]
    parser_totals: dict[str, list[float]] = {}
    for result in results:
        parser_totals.setdefault(str(result.get("parser_used") or "UNKNOWN"), []).append(float(result.get("parse_duration_seconds") or 0))
    slowest_parser = max(
        parser_totals,
        key=lambda name: sum(parser_totals[name]) / max(len(parser_totals[name]), 1),
        default="Not available",
    )
    stage_totals: dict[str, float] = {}
    for timing in timings:
        stage = str(timing.get("stage_name") or "unknown")
        stage_totals[stage] = stage_totals.get(stage, 0.0) + float(timing.get("duration_seconds") or 0)
    return {
        "slowest_documents": slowest_documents,
        "slowest_parser_type": slowest_parser,
        "stage_seconds": stage_totals,
        "document_count": len(results),
    }


def _prepare_documents_bounded(
    documents: list[dict[str, Any]],
    *,
    workers: int,
    root: Path,
    version_id: str,
    run_id: str,
    ocr_config: dict[str, Any],
    retrieval_config: dict[str, Any],
    prior_items: dict[str, dict[str, Any]],
) -> Iterable[_PreparedDocument]:
    if workers == 1:
        for document in documents:
            yield _prepare_document(
                document,
                root=root,
                version_id=version_id,
                run_id=run_id,
                ocr_config=ocr_config,
                retrieval_config=retrieval_config,
                prior_item=prior_items.get(document["document_id"]),
            )
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cutler-parse") as executor:
        iterator = iter(documents)
        queue: list[tuple[dict[str, Any], Any]] = []
        for _ in range(workers):
            document = next(iterator, None)
            if document is None:
                break
            queue.append(
                (
                    document,
                    executor.submit(
                        _prepare_document,
                        document,
                        root=root,
                        version_id=version_id,
                        run_id=run_id,
                        ocr_config=ocr_config,
                        retrieval_config=retrieval_config,
                        prior_item=prior_items.get(document["document_id"]),
                    ),
                )
            )
        while queue:
            _, future = queue.pop(0)
            yield future.result()
            document = next(iterator, None)
            if document is not None:
                queue.append(
                    (
                        document,
                        executor.submit(
                            _prepare_document,
                            document,
                            root=root,
                            version_id=version_id,
                            run_id=run_id,
                            ocr_config=ocr_config,
                            retrieval_config=retrieval_config,
                            prior_item=prior_items.get(document["document_id"]),
                        ),
                    )
                )


def _prepare_document(
    document: dict[str, Any],
    *,
    root: Path,
    version_id: str,
    run_id: str,
    ocr_config: dict[str, Any],
    retrieval_config: dict[str, Any],
    prior_item: dict[str, Any] | None,
) -> _PreparedDocument:
    parse_started_at = database.utc_now_iso()
    parse_started = perf_counter()
    parsed = parse_version_document(
        version_doc=document,
        source_path=(root / document["relative_package_path"]).resolve(),
        version_id=version_id,
        processing_run_id=run_id,
        ocr_enabled=bool(ocr_config.get("enabled")),
    )
    parse_duration = perf_counter() - parse_started
    parse_completed_at = database.utc_now_iso()
    fingerprint = document_processing_fingerprint(document, ocr_config=ocr_config, retrieval_config=retrieval_config)

    chunk_started = perf_counter()
    chunks: list[dict[str, Any]] = []
    for draft in chunk_parsed_document(
        parsed,
        chunk_size=int(retrieval_config.get("chunk_size") or config.CHUNK_SIZE),
        overlap=int(retrieval_config.get("chunk_overlap") or config.CHUNK_OVERLAP),
    ):
        locator = dict(draft.source_locator)
        locator.update({"version_id": version_id, "processing_run_id": run_id})
        chunks.append(
            {
                "chunk_id": _chunk_id(),
                "processing_run_id": run_id,
                "version_id": version_id,
                "version_document_id": document["document_id"],
                "page_number": draft.page_number,
                "sheet_name": draft.sheet_name,
                "row_range": draft.row_range,
                "section_heading": draft.section_heading,
                "chunk_index": draft.chunk_index,
                "chunk_text": draft.chunk_text,
                "character_count": draft.character_count,
                "token_estimate": draft.token_estimate,
                "extraction_method": draft.extraction_method,
                "source_locator_json": json.dumps(locator, sort_keys=True),
                "chunk_hash": draft.chunk_hash,
                "duplicate_group_id": None,
                "created_at": database.utc_now_iso(),
            }
        )
    chunking_seconds = perf_counter() - chunk_started
    extraction_started = perf_counter()
    evidence = [record for chunk in chunks for record in evidence_from_chunk(chunk)]
    verifications, _ = prepare_evidence_verifications(evidence, {chunk["chunk_id"]: chunk for chunk in chunks})
    extraction_seconds = perf_counter() - extraction_started
    status = (
        "COMPLETED"
        if parsed.status == config.DOCUMENT_PROCESSING_SUCCESS
        else "FAILED"
        if parsed.status == config.DOCUMENT_PROCESSING_FAILED
        else "PARTIAL"
    )
    attempt_count = int((prior_item or {}).get("attempt_count") or 0) + 1
    document_type = Path(str(document.get("relative_package_path") or "")).suffix.lower().lstrip(".") or str(document.get("mime_type") or "unknown")
    warnings_json = json.dumps(parsed.warnings, sort_keys=True)
    errors_json = json.dumps([parsed.error_message] if parsed.error_message else [], sort_keys=True)
    item = {
        "processing_run_id": run_id,
        "version_id": version_id,
        "version_document_id": document["document_id"],
        "processing_fingerprint": fingerprint,
        "status": status,
        "attempt_count": attempt_count,
        "reuse_status": "REPROCESSED" if prior_item else "PROCESSED",
        "parse_started_at": parse_started_at,
        "parse_completed_at": parse_completed_at,
        "parse_duration_seconds": round(parse_duration, 6),
        "file_size_bytes": int(document.get("file_size") or 0),
        "document_type": document_type,
        "extracted_character_count": parsed.extracted_character_count,
        "page_count": parsed.page_count,
        "chunk_count": len(chunks),
        "evidence_count": len(evidence),
        "warning_count": len(parsed.warnings),
        "error_message": parsed.error_message,
        "updated_at": database.utc_now_iso(),
    }
    result = {
        "result_id": _result_id(),
        "processing_run_id": run_id,
        "version_id": version_id,
        "version_document_id": document["document_id"],
        "original_document_id": document.get("original_document_id"),
        "parser_used": parsed.parser_used,
        "parser_version": parsed.parser_version,
        "processing_status": parsed.status,
        "detected_language": parsed.detected_language,
        "page_count": parsed.page_count,
        "sheet_count": parsed.sheet_count,
        "extracted_character_count": parsed.extracted_character_count,
        "ocr_required": int(parsed.ocr_required),
        "ocr_pages": parsed.ocr_pages,
        "table_count": parsed.table_count,
        "warning_count": len(parsed.warnings),
        "error_message": parsed.error_message,
        "extracted_content_path": parsed.full_text_path,
        "created_at": database.utc_now_iso(),
        "updated_at": database.utc_now_iso(),
        "processing_fingerprint": fingerprint,
        "parse_started_at": parse_started_at,
        "parse_completed_at": parse_completed_at,
        "parse_duration_seconds": round(parse_duration, 6),
        "document_type": document_type,
        "file_size_bytes": int(document.get("file_size") or 0),
        "normalized_character_reduction": int(parsed.metadata.get("normalized_character_reduction") or 0),
        "chunk_count": len(chunks),
        "evidence_count": len(evidence),
        "reuse_status": item["reuse_status"],
        "warnings_json": warnings_json,
        "errors_json": errors_json,
        "chunking_duration_seconds": round(chunking_seconds, 6),
        "extraction_duration_seconds": round(extraction_seconds, 6),
        "database_write_duration_seconds": 0.0,
        "attempt_count": attempt_count,
    }
    pages = [
        {
            "page_record_id": _page_id(),
            "processing_run_id": run_id,
            "version_document_id": document["document_id"],
            "page_number": page.page_number,
            "page_label": page.page_label,
            "extraction_method": page.extraction_method,
            "native_text_character_count": page.native_text_character_count,
            "ocr_text_character_count": page.ocr_text_character_count,
            "ocr_confidence": page.ocr_confidence,
            "page_text_path": page.page_text_path,
            "normalized_text": page.text[:4000],
            "image_render_path": page.image_render_path,
            "processing_warnings_json": json.dumps(page.warnings, sort_keys=True),
            "created_at": database.utc_now_iso(),
        }
        for page in parsed.pages
    ]
    sheets = [
        {
            "sheet_record_id": _sheet_id(),
            "processing_run_id": run_id,
            "version_document_id": document["document_id"],
            "sheet_name": sheet.sheet_name,
            "sheet_index": sheet.sheet_index,
            "hidden_state": sheet.hidden_state,
            "used_range": sheet.used_range,
            "formula_cell_count": sheet.formula_cell_count,
            "cached_value_cell_count": sheet.cached_value_cell_count,
            "external_link_count": sheet.external_link_count,
            "warning_flags": ",".join(sheet.warning_flags),
            "extracted_representation_path": sheet.extracted_representation_path,
            "created_at": database.utc_now_iso(),
        }
        for sheet in parsed.sheets
    ]
    return _PreparedDocument(
        version_doc=document,
        fingerprint=fingerprint,
        parsed=parsed,
        result=result,
        item=item,
        pages=pages,
        sheets=sheets,
        chunks=chunks,
        evidence=evidence,
        verifications=verifications,
        chunking_seconds=chunking_seconds,
        extraction_seconds=extraction_seconds,
    )


def _processing_run_stats(processing_run_id: str, *, db_path: Path | str) -> dict[str, Any]:
    items = database.list_processing_document_items(processing_run_id, db_path=db_path)
    results = database.list_document_processing_results(processing_run_id, db_path=db_path)
    chunks = database.list_document_chunks(processing_run_id, db_path=db_path)
    evidence = database.list_evidence_records(processing_run_id, db_path=db_path)
    timings = database.list_processing_stage_timings(processing_run_id, db_path=db_path)
    stage_totals: dict[str, float] = {}
    for timing in timings:
        stage = str(timing.get("stage_name") or "")
        stage_totals[stage] = stage_totals.get(stage, 0.0) + float(timing.get("duration_seconds") or 0)
    return {
        "successful_documents": sum(item.get("status") == "COMPLETED" for item in items),
        "partial_documents": sum(item.get("status") == "PARTIAL" for item in items),
        "failed_documents": sum(item.get("status") == "FAILED" for item in items),
        "pages_processed": sum(int(result.get("page_count") or 0) for result in results),
        "tables_detected": sum(int(result.get("table_count") or 0) for result in results),
        "sheets_processed": sum(int(result.get("sheet_count") or 0) for result in results),
        "chunks_created": len(chunks),
        "evidence_records_created": len(evidence),
        "database_write_seconds": stage_totals.get("database_write", 0.0),
        "chunking_seconds": stage_totals.get("chunking", 0.0),
        "deterministic_extraction_seconds": stage_totals.get("deterministic_extraction", 0.0),
    }


def _finalize_resumable_run(
    run: dict[str, Any], version: dict[str, Any], *, conflict_seconds: float, db_path: Path | str
) -> dict[str, Any]:
    stats = _processing_run_stats(run["processing_run_id"], db_path=db_path)
    results = database.list_document_processing_results(run["processing_run_id"], db_path=db_path)
    warnings = [
        warning
        for result in results
        for warning in json.loads(result.get("warnings_json") or "[]")
    ]
    errors = [result["error_message"] for result in results if result.get("error_message")]
    status = (
        config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS
        if warnings or errors or stats["partial_documents"] or stats["failed_documents"]
        else config.PROCESSING_STATUS_COMPLETED
    )
    completed_at = database.utc_now_iso()
    duration = max(0.0, (datetime.fromisoformat(completed_at) - datetime.fromisoformat(run["started_at"])).total_seconds())
    updated = database.update_processing_run(
        run["processing_run_id"],
        {
            **stats,
            "completed_at": completed_at,
            "last_checkpoint_at": completed_at,
            "warnings_json": json.dumps(sorted(set(warnings)), sort_keys=True),
            "errors_json": json.dumps(sorted(set(errors)), sort_keys=True),
            "status": status,
            "duration_seconds": duration,
            "conflict_analysis_seconds": conflict_seconds,
        },
        db_path=db_path,
    ) or run
    workspace = processing_workspace.processing_run_workspace(run["version_id"], run["processing_run_id"])
    processing_workspace.atomic_write_json(
        workspace / "run_summary.json",
        {
            "processing_run_id": run["processing_run_id"],
            "version_id": run["version_id"],
            "status": status,
            "stats": stats,
            "conflict_summary": database.get_conflict_analysis_summary(run["processing_run_id"], db_path=db_path) or {},
            "performance": processing_performance_summary(run["processing_run_id"], db_path=db_path),
            "warnings": sorted(set(warnings)),
            "errors": sorted(set(errors)),
        },
    )
    _record_version_event(
        version=version,
        event_type="PROCESSING_COMPLETED",
        details={"processing_run_id": run["processing_run_id"], "status": status, "document_checkpoints": True},
        db_path=db_path,
    )
    return updated


def _emit_progress(callback: Callable[[dict[str, Any]], None] | None, **payload: Any) -> None:
    if callback:
        callback(payload)
