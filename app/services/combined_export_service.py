from __future__ import annotations

import csv
import json
import os
import secrets
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app import config
from app.services.package_builder import sha256_file
from app.services.reporting.investment_report import citation_audit
from app.services.workspace_service import ensure_inside, sanitize_filename
from app.utils import database


EXCLUDED_SUFFIXES = {".db", ".env", ".log", ".tmp"}
EXCLUDED_NAMES = {".env", "secrets.toml"}
EXCLUDED_PARTS = {"database", "logs", "tmp", "__pycache__"}


def create_combined_export(
    analysis_run_id: str,
    *,
    report_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Create a versioned ZIP combining the locked package and selected report artifacts."""
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise ValueError("Analysis run does not exist.")
    version = database.get_package_version(run["version_id"], db_path=db_path)
    if not version:
        raise ValueError("Package version does not exist.")
    if version.get("status") != config.VERSION_STATUS_LOCKED:
        raise ValueError("Combined export requires a locked package version.")
    processing_run = database.get_processing_run(run["processing_run_id"], db_path=db_path)
    if not processing_run:
        raise ValueError("Processing run does not exist.")
    report = _selected_report(analysis_run_id, report_id=report_id, db_path=db_path)
    if not report:
        raise ValueError("Generate a draft or final investment report before creating the combined export.")

    version_root = _version_root(version)
    version_docs = database.list_package_version_documents(version["version_id"], db_path=db_path)
    _verify_locked_documents(version_root, version_docs)
    _verify_manifest(version_root, version)
    _verify_report_file(report.get("docx_path"), report.get("docx_sha256"), "DOCX report")
    _verify_report_file(report.get("pdf_path"), report.get("pdf_sha256"), "PDF report")

    export_id = f"EXP-{secrets.token_hex(8).upper()}"
    export_dir = config.REPORT_DIR / sanitize_filename(version["version_id"]) / sanitize_filename(analysis_run_id) / "combined_exports"
    ensure_inside(config.REPORT_DIR, export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    export_version = database.next_combined_export_version(analysis_run_id, db_path=db_path)
    while True:
        zip_path = export_dir / sanitize_filename(
            f"{version['ticker']}_Research_Package_AI_Report_V{export_version:03d}.zip"
        )
        if not zip_path.exists():
            break
        export_version += 1

    top_folder = sanitize_filename(f"{version['ticker']}_Research_Package_V{int(version['version_number']):03d}")
    included_names: list[str] = []
    warnings: list[str] = []
    staging = Path(tempfile.mkdtemp(prefix=".combined_export.", dir=export_dir))
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    try:
        evidence_path = staging / "evidence_ledger.xlsx"
        conflicts_path = staging / "conflicts.csv"
        metrics_path = staging / "metrics.csv"
        citation_path = staging / "citation_audit.json"
        inventory_path = staging / "source_inventory.csv"
        workflow_path = staging / "workflow_audit.json"
        _write_evidence_ledger(evidence_path, run, db_path=db_path)
        _write_conflicts_csv(conflicts_path, run, db_path=db_path)
        _write_metrics_csv(metrics_path, run, db_path=db_path)
        citation_path.write_text(json.dumps(citation_audit(analysis_run_id, db_path=db_path), indent=2, sort_keys=True), encoding="utf-8")
        _write_source_inventory(inventory_path, version_docs)
        _write_workflow_audit(workflow_path, run, db_path=db_path)

        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(version_root.rglob("*")):
                if not path.is_file() or _excluded(path, version_root):
                    if path.is_file():
                        warnings.append(f"Excluded unsafe file: {path.name}")
                    continue
                relative = path.relative_to(version_root).as_posix()
                archive_name = f"{top_folder}/{relative}"
                _assert_relative_archive_name(archive_name)
                archive.write(path, archive_name)
                included_names.append(archive_name)

            analysis_folder = f"{top_folder}/12_Final_Analysis"
            report_files = [
                (Path(report["pdf_path"]), f"{analysis_folder}/Investment_Report.pdf"),
                (Path(report["docx_path"]), f"{analysis_folder}/Investment_Report.docx"),
                (evidence_path, f"{analysis_folder}/evidence_ledger.xlsx"),
                (metrics_path, f"{analysis_folder}/metrics.csv"),
                (citation_path, f"{analysis_folder}/citation_audit.json"),
                (inventory_path, f"{analysis_folder}/source_inventory.csv"),
                (workflow_path, f"{analysis_folder}/workflow_audit.json"),
                (conflicts_path, f"{analysis_folder}/conflicts.csv"),
            ]
            for source, archive_name in report_files:
                if not source.exists():
                    raise ValueError(f"Required export file is missing: {source.name}")
                _assert_relative_archive_name(archive_name)
                archive.write(source, archive_name)
                included_names.append(archive_name)

        with zipfile.ZipFile(tmp_zip) as archive:
            names = archive.namelist()
            for name in names:
                _assert_relative_archive_name(name)
            if any(_archive_name_excluded(name) for name in names):
                raise ValueError("Unsafe file detected in combined export.")
        os.replace(tmp_zip, zip_path)
    except Exception:
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise
    finally:
        for child in staging.glob("*"):
            child.unlink(missing_ok=True)
        staging.rmdir()

    metadata = {
        "schema_version": "7.0",
        "analysis_run_id": analysis_run_id,
        "package_id": run["package_id"],
        "version_id": run["version_id"],
        "processing_run_id": run["processing_run_id"],
        "report_id": report.get("report_id"),
        "top_folder": top_folder,
        "included_files": included_names,
    }
    export = {
        "export_id": export_id,
        "analysis_run_id": analysis_run_id,
        "package_id": run["package_id"],
        "version_id": run["version_id"],
        "processing_run_id": run["processing_run_id"],
        "report_id": report.get("report_id"),
        "export_version": export_version,
        "zip_path": str(zip_path),
        "zip_sha256": sha256_file(zip_path),
        "file_count": len(included_names),
        "total_size_bytes": zip_path.stat().st_size,
        "status": config.COMBINED_EXPORT_STATUS_CREATED,
        "metadata_json": json.dumps(metadata, sort_keys=True),
        "warnings_json": json.dumps(warnings, sort_keys=True),
        "created_at": database.utc_now_iso(),
    }
    return database.create_combined_export(export, db_path=db_path)


def _selected_report(
    analysis_run_id: str,
    *,
    report_id: str | None,
    db_path: Path | str,
) -> dict[str, Any] | None:
    reports = database.list_generated_reports(analysis_run_id, db_path=db_path)
    if report_id:
        for report in reports:
            if report.get("report_id") == report_id:
                return report
        return None
    return reports[0] if reports else None


def _version_root(version: dict[str, Any]) -> Path:
    manifest_path = Path(version.get("manifest_path") or "")
    if not manifest_path.exists():
        raise ValueError("Locked package manifest is missing.")
    root = manifest_path.resolve().parents[1]
    ensure_inside(config.PACKAGE_DIR, root)
    if not root.exists():
        raise ValueError("Locked package root is missing.")
    return root


def _verify_locked_documents(root: Path, docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        relative = Path(doc["relative_package_path"])
        _assert_relative_archive_name(relative.as_posix())
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
        if not path.exists():
            raise ValueError(f"Locked package file is missing: {doc['relative_package_path']}")
        if path.stat().st_size != int(doc["file_size"]):
            raise ValueError(f"Locked package file size changed: {doc['relative_package_path']}")
        if sha256_file(path) != doc["sha256_hash"]:
            raise ValueError(f"Locked package file hash changed: {doc['relative_package_path']}")


def _verify_manifest(root: Path, version: dict[str, Any]) -> None:
    manifest_path = root / "00_Package_Manifest" / "package_manifest.json"
    if not manifest_path.exists():
        raise ValueError("Package manifest file is missing.")
    expected_hash = version.get("manifest_sha256")
    if expected_hash and sha256_file(manifest_path) != expected_hash:
        raise ValueError("Package manifest hash changed.")


def _verify_report_file(path_value: str | None, expected_hash: str | None, label: str) -> None:
    if not path_value:
        raise ValueError(f"{label} path is missing.")
    path = Path(path_value)
    ensure_inside(config.REPORT_DIR, path)
    if not path.exists():
        raise ValueError(f"{label} file is missing.")
    if expected_hash and sha256_file(path) != expected_hash:
        raise ValueError(f"{label} hash changed.")


def _write_evidence_ledger(path: Path, run: dict[str, Any], *, db_path: Path | str) -> None:
    evidence = database.list_evidence_records(
        run["processing_run_id"],
        version_id=run["version_id"],
        db_path=db_path,
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Evidence Ledger"
    fields = [
        "evidence_id",
        "evidence_type",
        "claim_text",
        "metric_name",
        "value",
        "unit",
        "currency",
        "period",
        "version_document_id",
        "page_number",
        "sheet_name",
        "cell_or_row_range",
        "verification_status",
        "analyst_status",
        "source_text_hash",
    ]
    sheet.append(fields)
    for item in evidence:
        sheet.append([item.get(field) for field in fields])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(path)


def _write_conflicts_csv(path: Path, run: dict[str, Any], *, db_path: Path | str) -> None:
    conflicts = database.list_claim_conflicts(run["processing_run_id"], db_path=db_path)
    fields = [
        "conflict_id",
        "subject",
        "metric",
        "period",
        "evidence_id_a",
        "evidence_id_b",
        "conflict_type",
        "severity",
        "explanation",
        "analyst_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for conflict in conflicts:
            writer.writerow({field: conflict.get(field, "") for field in fields})


def _write_metrics_csv(path: Path, run: dict[str, Any], *, db_path: Path | str) -> None:
    metrics = database.list_analysis_metrics(run["analysis_run_id"], db_path=db_path)
    fields = ["metric_code", "display_name", "value", "unit", "currency", "period", "confidence", "verification_status", "warning"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for metric in metrics:
            writer.writerow({field: metric.get(field, "") for field in fields})


def _write_source_inventory(path: Path, version_docs: list[dict[str, Any]]) -> None:
    fields = ["document_id", "title", "category", "source_type", "relative_package_path", "sha256_hash"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for document in version_docs:
            writer.writerow({field: document.get(field, "") for field in fields})


def _write_workflow_audit(path: Path, run: dict[str, Any], *, db_path: Path | str) -> None:
    workflow = database.latest_research_workflow_run(run["package_id"], db_path=db_path)
    package = database.get_package_by_package_id(run["package_id"], db_path=db_path) or {}
    payload = {
        "workflow": workflow or {},
        "performance": database.list_workflow_stage_performance(
            workflow_run_id=workflow.get("workflow_run_id") if workflow else None,
            package_id=None if workflow else run["package_id"],
            db_path=db_path,
        ),
        "package_events": database.list_package_version_events(run["package_id"], db_path=db_path),
        "research_time_window": {
            "selected_years": json.loads(package.get("selected_years_json") or "[]"),
            "selected_months": json.loads(package.get("selected_months_json") or "[]"),
            "research_cutoff_date": package.get("research_cutoff_date"),
            "fingerprint": package.get("research_window_fingerprint"),
        },
        "document_processing_performance": database.list_processing_stage_timings(
            run["processing_run_id"], db_path=db_path
        ),
        "conflict_summary": database.get_conflict_analysis_summary(
            run["processing_run_id"], db_path=db_path
        ) or {},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = {part.lower() for part in relative.parts}
    if parts & EXCLUDED_PARTS:
        return True
    if path.name.lower() in EXCLUDED_NAMES:
        return True
    suffixes = {suffix.lower() for suffix in path.suffixes}
    return bool(suffixes & EXCLUDED_SUFFIXES) or path.name.endswith(".tmp")


def _archive_name_excluded(name: str) -> bool:
    path = Path(name)
    parts = {part.lower() for part in path.parts}
    suffixes = {suffix.lower() for suffix in path.suffixes}
    return bool(parts & EXCLUDED_PARTS) or path.name.lower() in EXCLUDED_NAMES or bool(suffixes & EXCLUDED_SUFFIXES)


def _assert_relative_archive_name(name: str) -> None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe archive path detected.")
