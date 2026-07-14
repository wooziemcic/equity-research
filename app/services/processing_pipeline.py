from __future__ import annotations

import json
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app import config
from app.services import processing_workspace
from app.services.document_processing import ParsedDocument, chunk_parsed_document, parse_version_document
from app.services.evidence_service import (
    detect_claim_conflicts,
    detect_duplicate_groups,
    evidence_from_chunk,
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
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
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
