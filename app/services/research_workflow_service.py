from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests

from app import config
from app.services.analysis_pipeline import AnalysisPipelineError, create_analysis_run, load_analysis_diagnostics
from app.services.checklist_service import coverage_summary, ensure_package_checklist
from app.services.collectors.ir_collector import discover_public_documents, download_selected_ir_documents
from app.services.collectors.sec_collector import download_selected_filings, preview_filings
from app.services.company_resolver import ResolutionResult, resolve_ticker_metadata
from app.services.package_builder import build_package_version, lock_version, validate_package_readiness, verify_snapshot
from app.services.package_service import PackageInput, create_package, find_existing_ticker_packages
from app.services.processing_pipeline import run_processing_pipeline
from app.services.reporting.investment_report import generate_investment_report
from app.services.workspace_service import ensure_inside, sanitize_filename
from app.utils import database
from app.utils.validation import validate_cutoff_date, validate_ticker


TIMELINE_WAITING = "Waiting"
TIMELINE_RUNNING = "Running"
TIMELINE_COMPLETED = "Completed"
TIMELINE_WARNINGS = "Completed with warnings"
TIMELINE_NOT_FOUND = "Not found"
TIMELINE_FAILED = "Failed"
TIMELINE_SKIPPED = "Skipped"

WORKFLOW_STAGES = (
    "Building package",
    "Creating manifest",
    "Verifying integrity",
    "Locking package",
    "Processing documents",
    "Extracting evidence",
    "Verifying citations",
    "Calculating metrics",
    "Generating recommendation",
    "Creating report",
)


@dataclass(frozen=True)
class CollectionResult:
    sec_summary: dict[str, int]
    ir_summary: dict[str, int]
    warnings: list[str]
    errors: list[str]


def normalize_ticker_input(raw_ticker: str | None) -> str:
    """Normalize ticker input for the Phase 7 search field."""
    return "".join(str(raw_ticker or "").split()).upper()


def validate_search_ticker(raw_ticker: str | None) -> Any:
    return validate_ticker(normalize_ticker_input(raw_ticker))


def resolve_search_ticker(
    raw_ticker: str,
    *,
    refresh: bool = False,
    session: requests.Session | None = None,
) -> ResolutionResult:
    """Resolve the normalized ticker through the supported SEC company database."""
    ticker = normalize_ticker_input(raw_ticker)
    validation = validate_ticker(ticker)
    if not validation.is_valid:
        return ResolutionResult("UNRESOLVED", error=validation.error)
    return resolve_ticker_metadata(validation.value, refresh=refresh, session=session)


def get_or_create_research_package(
    company_metadata: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[dict[str, Any], bool]:
    """Reuse the newest editable ticker package or create a Common Equity package."""
    ticker_result = validate_ticker(company_metadata.get("ticker"))
    if not ticker_result.is_valid:
        raise ValueError(ticker_result.error)
    existing = find_existing_ticker_packages(ticker_result, db_path=db_path)
    for package in existing:
        if package.get("status") != config.STATUS_PACKAGE_LOCKED:
            updated = database.update_package_company_metadata(
                package["package_id"],
                {**company_metadata, "ticker": ticker_result.value},
                db_path=db_path,
            )
            return updated or package, False

    package = create_package(
        PackageInput(
            ticker=ticker_result.value,
            security_type="Common Equity",
            research_cutoff_date=date.today(),
            filing_history_years=3,
            analyst_notes="",
        ),
        db_path=db_path,
    )
    updated = database.update_package_company_metadata(
        package["package_id"],
        {**company_metadata, "ticker": ticker_result.value},
        db_path=db_path,
    )
    return updated or package, True


def update_research_settings(
    package_id: str,
    *,
    filing_history_years: int,
    research_cutoff_date: date,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Persist editable automated-research settings on the working package."""
    if filing_history_years not in config.FILING_HISTORY_OPTIONS.values():
        raise ValueError("Select a supported filing history period.")
    cutoff_result = validate_cutoff_date(research_cutoff_date)
    if not cutoff_result.is_valid:
        raise ValueError(cutoff_result.error)
    package = database.update_package_research_settings(
        package_id,
        filing_history_years=filing_history_years,
        research_cutoff_date=cutoff_result.value,
        db_path=db_path,
    )
    if not package:
        raise ValueError("Package does not exist.")
    return package


def planned_collection_preview(public_materials: list[str] | None = None) -> list[str]:
    materials = public_materials or []
    preview = [
        "SEC filings",
        "SEC submissions metadata",
        "Investor-relations materials",
    ]
    if materials:
        preview.append("Public company presentations and earnings materials when discoverable")
    return preview


def start_automated_collection(
    package: dict[str, Any],
    *,
    filing_types: list[str],
    ir_url: str | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> CollectionResult:
    """Run the existing SEC and optional IR public-document collectors."""
    warnings: list[str] = []
    errors: list[str] = []
    sec_summary = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    ir_summary = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    supported_forms = [form for form in filing_types if form in config.SEC_SUPPORTED_FORMS]
    if not supported_forms:
        warnings.append("No supported SEC filing types were selected.")
    elif not package.get("cik"):
        errors.append("Company must be verified with an SEC CIK before SEC collection.")
    else:
        try:
            candidates = preview_filings(package, supported_forms, session=session)
            if not candidates:
                warnings.append("No matching SEC filings were found for the selected date range and form types.")
            else:
                sec_summary = download_selected_filings(
                    package,
                    candidates,
                    session=session,
                    db_path=db_path,
                )
        except Exception as exc:
            errors.append(f"SEC collection failed: {exc}")

    if ir_url:
        try:
            candidates, message = discover_public_documents(ir_url, session=session)
            if message:
                warnings.append(message)
            if not candidates:
                warnings.append("No public investor-relations PDFs were discovered within the conservative crawl limits.")
            else:
                selections = [(candidate, candidate.suggested_category) for candidate in candidates]
                ir_summary = download_selected_ir_documents(
                    package,
                    selections,
                    session=session,
                    db_path=db_path,
                )
        except Exception as exc:
            errors.append(f"Investor-relations collection failed: {exc}")
    else:
        warnings.append("Investor-relations discovery skipped because no IR URL was provided.")

    refreshed = database.get_package_by_package_id(package["package_id"], db_path=db_path) or package
    ensure_package_checklist(refreshed, db_path=db_path)
    return CollectionResult(sec_summary, ir_summary, warnings, errors)


def collection_timeline(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Build a real-state collection timeline from package, run, document, and checklist rows."""
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        return [{"stage": "Package selected", "status": TIMELINE_FAILED, "detail": "Package does not exist."}]
    collection_runs = database.list_recent_collection_runs(package_id, limit=20, db_path=db_path)
    upload_runs = database.list_recent_upload_runs(package_id, limit=20, db_path=db_path)
    counts = database.document_counts_for_package(package_id, db_path=db_path)
    checklist = ensure_package_checklist(package, db_path=db_path)
    readiness = validate_package_readiness(package, db_path=db_path)

    sec_runs = [run for run in collection_runs if run.get("source_type") == "SEC"]
    ir_runs = [run for run in collection_runs if run.get("source_type") == "INVESTOR_RELATIONS"]
    latest_sec = sec_runs[0] if sec_runs else None
    latest_ir = ir_runs[0] if ir_runs else None
    latest_upload = upload_runs[0] if upload_runs else None

    return [
        {
            "stage": "Company verified",
            "status": TIMELINE_COMPLETED if package.get("resolution_status") == "RESOLVED" and package.get("cik") else TIMELINE_WAITING,
            "detail": package.get("company_name") or "Company resolution pending",
        },
        {
            "stage": "SEC filing inventory loaded",
            "status": _collection_run_status(latest_sec, empty_status=TIMELINE_WAITING),
            "detail": _collection_run_detail(latest_sec),
        },
        {
            "stage": "Selected SEC filings downloaded",
            "status": _download_status(latest_sec),
            "detail": _download_detail(latest_sec),
        },
        {
            "stage": "Investor-relations documents discovered",
            "status": _collection_run_status(latest_ir, empty_status=TIMELINE_SKIPPED),
            "detail": _collection_run_detail(latest_ir) or "Skipped until an IR URL is provided.",
        },
        {
            "stage": "Public documents downloaded",
            "status": TIMELINE_COMPLETED if counts["public"] else TIMELINE_WAITING,
            "detail": f"{counts['public']} public file(s) collected.",
        },
        {
            "stage": "Uploaded files validated",
            "status": _upload_status(latest_upload),
            "detail": _upload_detail(latest_upload, counts),
        },
        {
            "stage": "Package checklist recalculated",
            "status": TIMELINE_WARNINGS if any(item["effective_status"] != config.CHECKLIST_STATUS_AVAILABLE for item in checklist) else TIMELINE_COMPLETED,
            "detail": f"{len(checklist)} checklist item(s) evaluated.",
        },
        {
            "stage": "Package readiness checked",
            "status": TIMELINE_COMPLETED if readiness.status == config.READINESS_READY else TIMELINE_WARNINGS if readiness.status == config.READINESS_READY_WITH_WARNINGS else TIMELINE_FAILED,
            "detail": "; ".join(readiness.errors[:3] or readiness.warnings[:3] or readiness.notices[:3]),
        },
    ]


def package_coverage_summary(
    package: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    checklist = ensure_package_checklist(package, db_path=db_path)
    coverage = coverage_summary(checklist)
    counts = database.document_counts_for_package(package["package_id"], db_path=db_path)
    return {
        "public_files": counts["public"],
        "licensed_files": counts["licensed"],
        "core_available": coverage.get("available_required", coverage.get("required_available", 0)),
        "missing_core": coverage.get("missing_required", 0),
        "recommended_missing": coverage.get("missing_recommended", 0),
        "not_available": coverage.get("not_available", 0),
        "failed_items": counts["failed"],
        "duplicate_items": counts["duplicates"],
        "total_size_bytes": counts["bytes"],
    }


def workflow_idempotency_key(
    package: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> str:
    documents = database.list_documents_by_package(package["package_id"], db_path=db_path)
    payload = {
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "research_cutoff_date": package["research_cutoff_date"],
        "filing_history_years": package["filing_history_years"],
        "checklist_reviewed": package.get("checklist_reviewed"),
        "missing_core_acknowledged": package.get("missing_core_acknowledged"),
        "stale_documents_acknowledged": package.get("stale_documents_acknowledged"),
        "needs_review_acknowledged": package.get("needs_review_acknowledged"),
        "documents": [
            {
                "document_id": doc["document_id"],
                "status": doc.get("collection_status"),
                "sha256_hash": doc.get("sha256_hash"),
                "category": doc.get("final_category_code") or doc.get("category"),
            }
            for doc in sorted(documents, key=lambda item: item["document_id"])
            if doc.get("collection_status") != "DELETED"
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"PHASE7-{digest}"


def run_research_workflow(
    package_id: str,
    *,
    idempotency_key: str | None = None,
    retry_failed: bool = False,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Build, lock, process, analyze, and draft-report the active package."""
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        raise ValueError("Package does not exist.")
    key = idempotency_key or workflow_idempotency_key(package, db_path=db_path)
    existing = database.get_research_workflow_by_key(key, db_path=db_path)
    if existing and not (retry_failed and existing["status"] == config.WORKFLOW_STATUS_FAILED):
        return existing

    now = database.utc_now_iso()
    workflow = existing or database.create_research_workflow_run(
        {
            "workflow_run_id": f"WF-{secrets.token_hex(8).upper()}",
            "package_id": package_id,
            "ticker": package["ticker"],
            "status": config.WORKFLOW_STATUS_RUNNING,
            "current_step": WORKFLOW_STAGES[0],
            "idempotency_key": key,
            "version_id": None,
            "processing_run_id": None,
            "analysis_run_id": None,
            "report_id": None,
            "stage_statuses_json": json.dumps(_initial_stage_statuses(), sort_keys=True),
            "warnings_json": json.dumps([], sort_keys=True),
            "errors_json": json.dumps([], sort_keys=True),
            "error_message": None,
            "created_by": "analyst",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
        },
        db_path=db_path,
    )
    if existing:
        workflow = database.update_research_workflow_run(
            existing["workflow_run_id"],
            {
                "status": config.WORKFLOW_STATUS_RUNNING,
                "current_step": _resume_step(existing),
                "error_message": None,
                "errors_json": json.dumps([], sort_keys=True),
                "completed_at": None,
            },
            db_path=db_path,
        ) or existing

    workflow_id = workflow["workflow_run_id"]
    stage_statuses = _load_stage_statuses(workflow)
    warnings = json.loads(workflow.get("warnings_json") or "[]")
    errors: list[str] = []

    try:
        readiness = validate_package_readiness(package, db_path=db_path)
        if readiness.errors:
            _mark_stage(stage_statuses, "Building package", "Failed")
            return database.update_research_workflow_run(
                workflow_id,
                {
                    "status": config.WORKFLOW_STATUS_BLOCKED,
                    "current_step": "Readiness blocked",
                    "stage_statuses_json": json.dumps(stage_statuses, sort_keys=True),
                    "warnings_json": json.dumps(readiness.warnings, sort_keys=True),
                    "errors_json": json.dumps(readiness.errors, sort_keys=True),
                    "error_message": "; ".join(readiness.errors),
                    "completed_at": database.utc_now_iso(),
                },
                db_path=db_path,
            ) or workflow
        warnings.extend(readiness.warnings)

        version = _existing_version(workflow, db_path=db_path)
        if version and version.get("status") == config.VERSION_STATUS_BUILDING:
            database.update_package_version(
                version["version_id"],
                {"status": config.VERSION_STATUS_BUILD_FAILED, "error_message": "Incomplete package build was superseded by a retry."},
                db_path=db_path,
            )
            version = None
        if not version:
            _persist_stage(workflow_id, stage_statuses, "Building package", "Running", db_path=db_path)
            version = build_package_version(package, notes="Phase 7 workflow build", db_path=db_path)
            workflow = database.update_research_workflow_run(
                workflow_id,
                {"version_id": version["version_id"]},
                db_path=db_path,
            ) or workflow
        _mark_stage(stage_statuses, "Building package", "Completed")
        _mark_stage(stage_statuses, "Creating manifest", "Completed")
        _mark_stage(
            stage_statuses,
            "Verifying integrity",
            "Completed with warnings" if version.get("integrity_status") == config.INTEGRITY_VERIFIED_WITH_WARNINGS else "Completed",
        )

        if version.get("status") != config.VERSION_STATUS_LOCKED:
            _persist_stage(workflow_id, stage_statuses, "Locking package", "Running", db_path=db_path)
            version = lock_version(version["version_id"], db_path=db_path)
        _mark_stage(stage_statuses, "Locking package", "Completed")

        processing_run = _existing_processing_run(workflow, version, db_path=db_path)
        if not processing_run:
            _persist_stage(workflow_id, stage_statuses, "Processing documents", "Running", db_path=db_path)
            processing_run = run_processing_pipeline(version["version_id"], db_path=db_path)
            workflow = database.update_research_workflow_run(
                workflow_id,
                {"processing_run_id": processing_run["processing_run_id"]},
                db_path=db_path,
            ) or workflow
        if processing_run.get("status") not in {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}:
            raise RuntimeError(f"Processing run ended with technical status {processing_run.get('status')}.")
        processing_status = "Completed with warnings" if processing_run.get("status") == config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS else "Completed"
        _mark_stage(stage_statuses, "Processing documents", processing_status)
        _mark_stage(stage_statuses, "Extracting evidence", processing_status)
        _mark_stage(stage_statuses, "Verifying citations", processing_status)

        analysis_run = _existing_analysis_run(workflow, version, processing_run, db_path=db_path)
        if not analysis_run:
            _persist_stage(workflow_id, stage_statuses, "Calculating metrics", "Running", db_path=db_path)
            analysis_run = create_analysis_run(
                version["version_id"],
                processing_run["processing_run_id"],
                db_path=db_path,
            )
            workflow = database.update_research_workflow_run(
                workflow_id,
                {"analysis_run_id": analysis_run["analysis_run_id"]},
                db_path=db_path,
            ) or workflow
        analysis_warning_messages = _analysis_warning_messages(analysis_run, db_path=db_path)
        analysis_has_warnings = bool(analysis_warning_messages)
        _mark_stage(stage_statuses, "Calculating metrics", TIMELINE_WARNINGS if analysis_has_warnings else TIMELINE_COMPLETED)
        _mark_stage(stage_statuses, "Generating recommendation", "Completed")

        report = _existing_report(workflow, analysis_run, db_path=db_path)
        if not report:
            _persist_stage(workflow_id, stage_statuses, "Creating report", "Running", db_path=db_path)
            report = generate_investment_report(analysis_run["analysis_run_id"], final=False, db_path=db_path)
            workflow = database.update_research_workflow_run(
                workflow_id,
                {"report_id": report["report_id"]},
                db_path=db_path,
            ) or workflow
        _mark_stage(stage_statuses, "Creating report", "Completed")
        if analysis_has_warnings:
            warnings.extend(analysis_warning_messages)

        return database.update_research_workflow_run(
            workflow_id,
            {
                "status": config.WORKFLOW_STATUS_COMPLETED_WITH_WARNINGS if analysis_has_warnings else config.WORKFLOW_STATUS_COMPLETED,
                "current_step": "Completed with warnings" if analysis_has_warnings else "Completed",
                "version_id": version["version_id"],
                "processing_run_id": processing_run["processing_run_id"],
                "analysis_run_id": analysis_run["analysis_run_id"],
                "report_id": report["report_id"],
                "stage_statuses_json": json.dumps(stage_statuses, sort_keys=True),
                "warnings_json": json.dumps(sorted(set(warnings)), sort_keys=True),
                "errors_json": json.dumps([], sort_keys=True),
                "completed_at": database.utc_now_iso(),
            },
            db_path=db_path,
        ) or workflow
    except AnalysisPipelineError as exc:
        errors.append(exc.safe_message)
        _mark_current_failed(stage_statuses)
        analysis_id = exc.analysis_run_id or workflow.get("analysis_run_id")
        if analysis_id and exc.diagnostics.get("provider_code"):
            database.update_analysis_run(
                analysis_id,
                {"ai_review_status": exc.diagnostics["provider_code"]},
                db_path=db_path,
            )
        return database.update_research_workflow_run(
            workflow_id,
            {
                "status": config.WORKFLOW_STATUS_FAILED,
                "current_step": "Calculating metrics",
                "analysis_run_id": analysis_id,
                "stage_statuses_json": json.dumps(stage_statuses, sort_keys=True),
                "warnings_json": json.dumps(sorted(set(warnings)), sort_keys=True),
                "errors_json": json.dumps(errors, sort_keys=True),
                "error_message": exc.safe_message,
                "completed_at": database.utc_now_iso(),
            },
            db_path=db_path,
        ) or workflow
    except Exception as exc:
        safe_message = _safe_error_message(exc)
        errors.append(safe_message)
        _mark_current_failed(stage_statuses)
        return database.update_research_workflow_run(
            workflow_id,
            {
                "status": config.WORKFLOW_STATUS_FAILED,
                "stage_statuses_json": json.dumps(stage_statuses, sort_keys=True),
                "warnings_json": json.dumps(sorted(set(warnings)), sort_keys=True),
                "errors_json": json.dumps(errors, sort_keys=True),
                "error_message": safe_message,
                "completed_at": database.utc_now_iso(),
            },
            db_path=db_path,
        ) or workflow


def workflow_stage_rows(workflow: dict[str, Any] | None) -> list[dict[str, str]]:
    statuses = _load_stage_statuses(workflow or {})
    return [{"Stage": stage, "Status": statuses.get(stage, "Waiting")} for stage in WORKFLOW_STAGES]


def reconcile_failed_workflow(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Repair only abandoned build staging and resume the current package workflow."""
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    workflow = database.latest_research_workflow_run(package_id, db_path=db_path)
    if not package or not workflow:
        raise ValueError("No current workflow exists for this package.")
    statuses = _load_stage_statuses(workflow)
    building_failed = workflow.get("status") == config.WORKFLOW_STATUS_FAILED and statuses.get("Building package") == "Failed"
    if not building_failed:
        return {
            "workflow": workflow,
            "existing_version_reused": False,
            "new_version_created": False,
            "temporary_artifacts_cleaned": 0,
            "next_stage_resumed": workflow.get("current_step") or "Unknown",
            "remaining_error": workflow.get("error_message"),
        }

    existing_version = _existing_version(workflow, db_path=db_path)
    reused = False
    if existing_version and existing_version.get("status") in {config.VERSION_STATUS_BUILT, config.VERSION_STATUS_LOCKED}:
        reused = True
    elif existing_version and existing_version.get("status") == config.VERSION_STATUS_BUILDING:
        manifest_path = Path(existing_version.get("manifest_path") or "")
        root = manifest_path.resolve().parents[1] if manifest_path.exists() else None
        docs = database.list_package_version_documents(existing_version["version_id"], db_path=db_path)
        if root and root.exists() and docs:
            integrity = verify_snapshot(root, docs, existing_version.get("manifest_sha256"))
            if integrity["overall_integrity_status"] in {config.INTEGRITY_VERIFIED, config.INTEGRITY_VERIFIED_WITH_WARNINGS}:
                database.update_package_version(
                    existing_version["version_id"],
                    {"status": config.VERSION_STATUS_BUILT, "integrity_status": integrity["overall_integrity_status"]},
                    db_path=db_path,
                )
                reused = True

    package_root = config.PACKAGE_DIR / sanitize_filename(package_id)
    ensure_inside(config.PACKAGE_DIR, package_root)
    cleaned = 0
    if package_root.exists():
        for child in package_root.iterdir():
            if not child.name.startswith(".") or not child.is_dir():
                continue
            shutil.rmtree(child)
            cleaned += 1

    key = workflow.get("idempotency_key") or workflow_idempotency_key(package, db_path=db_path)
    resumed = run_research_workflow(package_id, idempotency_key=key, retry_failed=True, db_path=db_path)
    new_version = bool(resumed.get("version_id") and resumed.get("version_id") != workflow.get("version_id"))
    return {
        "workflow": resumed,
        "existing_version_reused": reused,
        "new_version_created": new_version,
        "temporary_artifacts_cleaned": cleaned,
        "next_stage_resumed": resumed.get("current_step") or "Building package",
        "remaining_error": resumed.get("error_message"),
    }


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", message)
    message = re.sub(r"(?i)(api[_-]?key|password|secret|token)=\S+", r"\1=[redacted]", message)
    return message[:500]


def _analysis_warning_messages(
    analysis_run: dict[str, Any],
    *,
    db_path: Path | str,
) -> list[str]:
    messages: list[str] = []
    decision = database.get_recommendation_decision(analysis_run["analysis_run_id"], db_path=db_path)
    if decision and decision.get("preliminary_rating") in {
        config.RECOMMENDATION_INSUFFICIENT_EVIDENCE,
        config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED,
    }:
        reason = decision.get("abstention_reason") or "Recommendation requires additional analyst review."
        messages.append(f"Analysis abstained: {reason}")
    diagnostics = load_analysis_diagnostics(analysis_run)
    limitations = diagnostics.get("limitations") if isinstance(diagnostics, dict) else None
    if isinstance(limitations, list):
        messages.extend(str(item) for item in limitations[:5])
    return sorted(set(messages))


def _collection_run_status(run: dict[str, Any] | None, *, empty_status: str) -> str:
    if not run:
        return empty_status
    if run.get("status") == config.COLLECTION_STATUS_RUNNING:
        return TIMELINE_RUNNING
    if run.get("status") == config.COLLECTION_STATUS_COMPLETE:
        return TIMELINE_COMPLETED
    if run.get("status") == config.COLLECTION_STATUS_PARTIAL:
        return TIMELINE_WARNINGS
    if run.get("status") == config.COLLECTION_STATUS_FAILED:
        return TIMELINE_FAILED
    return empty_status


def _download_status(run: dict[str, Any] | None) -> str:
    if not run:
        return TIMELINE_WAITING
    if int(run.get("documents_failed") or 0):
        return TIMELINE_WARNINGS if int(run.get("documents_downloaded") or 0) else TIMELINE_FAILED
    if int(run.get("documents_downloaded") or 0):
        return TIMELINE_COMPLETED
    if int(run.get("documents_already_collected") or 0):
        return TIMELINE_COMPLETED
    if int(run.get("documents_duplicated") or 0):
        return TIMELINE_COMPLETED
    return _collection_run_status(run, empty_status=TIMELINE_WAITING)


def _collection_run_detail(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    return (
        f"{run.get('documents_discovered', 0)} discovered, "
        f"{run.get('documents_downloaded', 0)} downloaded now, "
        f"{run.get('documents_already_collected', 0)} already collected, "
        f"{run.get('documents_duplicated', 0)} duplicate, "
        f"{run.get('documents_failed', 0)} failed, "
        f"{run.get('documents_not_found', 0)} not found."
    )


def _download_detail(run: dict[str, Any] | None) -> str:
    return _collection_run_detail(run) if run else "No collection run has completed yet."


def _upload_status(run: dict[str, Any] | None) -> str:
    if not run:
        return TIMELINE_WAITING
    if run.get("status") == config.UPLOAD_STATUS_COMPLETED:
        return TIMELINE_COMPLETED
    if run.get("status") == config.UPLOAD_STATUS_COMPLETED_WITH_ERRORS:
        return TIMELINE_WARNINGS
    if run.get("status") == config.UPLOAD_STATUS_FAILED:
        return TIMELINE_FAILED
    return TIMELINE_RUNNING


def _upload_detail(run: dict[str, Any] | None, counts: dict[str, int]) -> str:
    if not run:
        return f"{counts['licensed']} licensed file(s) available."
    return f"{run.get('number_uploaded', 0)} uploaded, {run.get('number_duplicated', 0)} duplicates, {run.get('number_failed', 0)} failed."


def _initial_stage_statuses() -> dict[str, str]:
    return {stage: "Waiting" for stage in WORKFLOW_STAGES}


def _load_stage_statuses(workflow: dict[str, Any]) -> dict[str, str]:
    try:
        payload = json.loads(workflow.get("stage_statuses_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    statuses = _initial_stage_statuses()
    statuses.update({stage: str(status) for stage, status in payload.items() if stage in statuses})
    return statuses


def _mark_stage(stage_statuses: dict[str, str], stage: str, status: str) -> None:
    stage_statuses[stage] = status


def _persist_stage(
    workflow_id: str,
    stage_statuses: dict[str, str],
    stage: str,
    status: str,
    *,
    db_path: Path | str,
) -> None:
    _mark_stage(stage_statuses, stage, status)
    database.update_research_workflow_run(
        workflow_id,
        {
            "current_step": stage,
            "stage_statuses_json": json.dumps(stage_statuses, sort_keys=True),
        },
        db_path=db_path,
    )


def _mark_current_failed(stage_statuses: dict[str, str]) -> None:
    for stage in WORKFLOW_STAGES:
        if stage_statuses.get(stage) == "Running":
            stage_statuses[stage] = "Failed"
            return


def _resume_step(workflow: dict[str, Any]) -> str:
    statuses = _load_stage_statuses(workflow)
    for stage in WORKFLOW_STAGES:
        if statuses.get(stage) in {"Waiting", "Running", "Failed"}:
            return stage
    return WORKFLOW_STAGES[-1]


def _existing_version(workflow: dict[str, Any], *, db_path: Path | str) -> dict[str, Any] | None:
    version_id = workflow.get("version_id")
    if not version_id:
        return None
    version = database.get_package_version(version_id, db_path=db_path)
    if version and version.get("status") == config.VERSION_STATUS_BUILD_FAILED:
        return None
    return version


def _existing_processing_run(
    workflow: dict[str, Any],
    version: dict[str, Any],
    *,
    db_path: Path | str,
) -> dict[str, Any] | None:
    run_id = workflow.get("processing_run_id")
    if run_id:
        run = database.get_processing_run(run_id, db_path=db_path)
        if run and run.get("status") in {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}:
            return run
    runs = database.list_processing_runs(version["version_id"], db_path=db_path)
    for run in runs:
        if run.get("status") in {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}:
            return run
    return None


def _existing_analysis_run(
    workflow: dict[str, Any],
    version: dict[str, Any],
    processing_run: dict[str, Any],
    *,
    db_path: Path | str,
) -> dict[str, Any] | None:
    run_id = workflow.get("analysis_run_id")
    if run_id:
        run = database.get_analysis_run(run_id, db_path=db_path)
        if run and run.get("status") != config.ANALYSIS_STATUS_FAILED:
            return run
    runs = database.list_analysis_runs(
        version["version_id"],
        processing_run_id=processing_run["processing_run_id"],
        db_path=db_path,
    )
    for run in runs:
        if run.get("status") != config.ANALYSIS_STATUS_FAILED:
            return run
    return None


def _existing_report(
    workflow: dict[str, Any],
    analysis_run: dict[str, Any],
    *,
    db_path: Path | str,
) -> dict[str, Any] | None:
    report_id = workflow.get("report_id")
    reports = database.list_generated_reports(analysis_run["analysis_run_id"], db_path=db_path)
    for report in reports:
        if report.get("report_id") == report_id:
            return report
    return reports[0] if reports else None
