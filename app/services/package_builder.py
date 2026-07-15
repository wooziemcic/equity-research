from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app import config
from app.services.checklist_service import coverage_summary, ensure_package_checklist
from app.services.research_window import window_from_package
from app.services.taxonomy import CATEGORIES
from app.services.workspace_service import ensure_inside, sanitize_filename
from app.utils import database


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    errors: list[str]
    warnings: list[str]
    notices: list[str]


FOLDER_MAP = {
    "annual_filing": "01_SEC_Filings",
    "quarterly_filing": "01_SEC_Filings",
    "current_report": "01_SEC_Filings",
    "proxy_statement": "01_SEC_Filings",
    "earnings_release": "02_Company_Materials",
    "earnings_presentation": "02_Company_Materials",
    "investor_presentation": "02_Company_Materials",
    "investor_day": "02_Company_Materials",
    "company_press_release": "02_Company_Materials",
    "executive_compensation": "02_Company_Materials",
    "esg_sustainability": "02_Company_Materials",
    "earnings_transcript": "03_Earnings_Transcripts",
    "bloomberg_des": "04_Bloomberg",
    "bloomberg_fa": "04_Bloomberg",
    "bloomberg_anr": "04_Bloomberg",
    "bloomberg_drsk": "04_Bloomberg",
    "bloomberg_credit": "04_Bloomberg",
    "bloomberg_other": "04_Bloomberg",
    "sell_side_research": "05_Sell_Side_Research",
    "sell_side_initiation": "05_Sell_Side_Research",
    "credit_research": "06_Credit_Research",
    "rating_agency": "06_Credit_Research",
    "debt_analysis": "06_Credit_Research",
    "industry_research": "07_Industry_Research",
    "activist_research": "08_Activist_and_Bear_Research",
    "short_seller_research": "08_Activist_and_Bear_Research",
    "financial_model": "09_Financial_Models",
    "convertible_analysis": "09_Financial_Models",
    "historical_valuation": "09_Financial_Models",
    "internal_notes": "10_Internal_Analyst_Materials",
    "legal_regulatory": "11_Other",
    "other": "11_Other",
}

PACKAGE_FOLDERS = (
    "00_Package_Manifest",
    "01_SEC_Filings",
    "02_Company_Materials",
    "03_Earnings_Transcripts",
    "04_Bloomberg",
    "05_Sell_Side_Research",
    "06_Credit_Research",
    "07_Industry_Research",
    "08_Activist_and_Bear_Research",
    "09_Financial_Models",
    "10_Internal_Analyst_Materials",
    "11_Other",
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_path(path_value: str) -> Path:
    path = Path(path_value)
    resolved = path.resolve()
    try:
        resolved.relative_to(config.DOWNLOAD_DIR.resolve())
    except ValueError as exc:
        raise ValueError("Document path is outside the managed data directory.") from exc
    return resolved


def included_documents(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    """Return working documents eligible for package build validation."""
    inclusion_rows = database.list_draft_document_inclusions(package_id, db_path=db_path)
    inclusion_by_document = {row["document_id"]: bool(row["included"]) for row in inclusion_rows}
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    window = window_from_package(package) if package else None
    return [
        doc
        for doc in database.list_documents_by_package(package_id, db_path=db_path)
        if doc.get("collection_status") != "DELETED"
        and inclusion_by_document.get(doc["document_id"], True)
        and (window is None or window.contains(doc.get("publication_date") or doc.get("document_date")))
    ]


def package_build_fingerprint(
    package: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> str:
    documents = included_documents(package["package_id"], db_path=db_path)
    payload = {
        "documents": sorted((doc["document_id"], doc.get("sha256_hash") or "") for doc in documents),
        "profile": package.get("collection_profile_snapshot_json") or package.get("collection_profile_name"),
        "research_cutoff": package.get("research_cutoff_date"),
        "research_window_fingerprint": package.get("research_window_fingerprint"),
        "selected_years_json": package.get("selected_years_json"),
        "selected_months_json": package.get("selected_months_json"),
        "configuration": {
            "security_type": package.get("security_type"),
            "filing_history_years": package.get("filing_history_years"),
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_package_readiness(
    package: dict[str, Any] | None,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> ReadinessResult:
    """Return structured readiness errors, warnings, notices, and status."""
    errors: list[str] = []
    warnings: list[str] = []
    notices: list[str] = []
    if not package:
        return ReadinessResult(config.READINESS_NOT_READY, ["Package does not exist."], [], [])
    if not package.get("company_name") or package.get("company_name") == "Company resolution pending":
        errors.append("Company setup is incomplete.")
    documents = included_documents(package["package_id"], db_path=db_path)
    if not documents:
        errors.append("At least one document is required.")
    seen_hashes: set[str] = set()
    for doc in documents:
        if doc.get("collection_status") == config.DOCUMENT_STATUS_FAILED:
            errors.append(f"Failed document cannot be included: {doc.get('title')}")
        if doc.get("collection_status") == config.DOCUMENT_STATUS_DUPLICATE:
            warnings.append(f"Duplicate record will not be included twice: {doc.get('title')}")
            continue
        if not doc.get("category") and not doc.get("final_category_code"):
            errors.append(f"Document is missing a category: {doc.get('title')}")
        if not doc.get("sha256_hash"):
            errors.append(f"Document is missing SHA-256 hash: {doc.get('title')}")
        if doc.get("sha256_hash") in seen_hashes:
            warnings.append(f"Duplicate content hash detected: {doc.get('title')}")
        seen_hashes.add(doc.get("sha256_hash") or "")
        try:
            path = _managed_path(doc.get("local_path") or "")
            if not path.exists():
                errors.append(f"Document file is missing: {doc.get('title')}")
        except Exception:
            errors.append(f"Document has an invalid managed path: {doc.get('title')}")
    checklist = ensure_package_checklist(package, db_path=db_path)
    missing_core = [
        item
        for item in checklist
        if item["requirement_level"] == "required"
        and item["effective_status"] == config.CHECKLIST_STATUS_MISSING
    ]
    needs_review = [item for item in checklist if item["effective_status"] == config.CHECKLIST_STATUS_NEEDS_REVIEW]
    stale = [item for item in checklist if item["effective_status"] == config.CHECKLIST_STATUS_STALE]
    if not int(package.get("checklist_reviewed") or 0):
        errors.append("Checklist review acknowledgement is required.")
    if missing_core and not int(package.get("missing_core_acknowledged") or 0):
        errors.append("Missing core checklist items require acknowledgement.")
    if needs_review and not int(package.get("needs_review_acknowledged") or 0):
        errors.append("Needs-review checklist items require acknowledgement.")
    if stale and not int(package.get("stale_documents_acknowledged") or 0):
        errors.append("Stale checklist items require acknowledgement.")
    for item in missing_core:
        warnings.append(f"Missing core item: {item['display_name']}")
    for item in needs_review:
        warnings.append(f"Needs review: {item['display_name']}")
    for item in stale:
        warnings.append(f"Stale item: {item['display_name']}")
    if not errors:
        notices.append(f"{len([d for d in documents if d.get('collection_status') == config.DOCUMENT_STATUS_DOWNLOADED])} documents are eligible for build.")
    status = config.READINESS_NOT_READY if errors else config.READINESS_READY_WITH_WARNINGS if warnings else config.READINESS_READY
    return ReadinessResult(status, errors, warnings, notices)


def _display_version(package: dict[str, Any], version_number: int) -> str:
    cutoff = str(package["research_cutoff_date"]).replace("-", "")
    return f"{sanitize_filename(package['ticker'])}-{cutoff}-V{version_number:03d}"


def _category_code(document: dict[str, Any]) -> str:
    if document.get("final_category_code"):
        return document["final_category_code"]
    form_map = {"10-K": "annual_filing", "20-F": "annual_filing", "10-Q": "quarterly_filing", "8-K": "current_report", "6-K": "current_report", "DEF 14A": "proxy_statement"}
    if document.get("form_type") in form_map:
        return form_map[document["form_type"]]
    return "other"


def _folder_for_document(document: dict[str, Any]) -> str:
    if document.get("collection_method") == "INVESTOR_RELATIONS":
        return "02_Investor_Relations"
    return FOLDER_MAP.get(_category_code(document), "11_Other")


def _package_filename(document: dict[str, Any], index: int, used: set[str]) -> str:
    source = document.get("original_filename") or document.get("local_filename") or f"document_{index}"
    clean = sanitize_filename(source)
    if not Path(clean).suffix and document.get("file_extension"):
        clean = f"{clean}{document['file_extension']}"
    candidate = clean
    stem = Path(clean).stem
    suffix = Path(clean).suffix
    counter = 1
    while candidate.lower() in used:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(encoded)
    os.replace(tmp, path)
    return hashlib.sha256(encoded).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _write_inventory_xlsx(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Document Inventory"
    sheet.append(fieldnames)
    for row in rows:
        sheet.append([row.get(field, "") for field in fieldnames])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[letter].width = min(max(max_length + 2, 12), 42)
    workbook.save(path)


def _version_root(package_id: str, version_id: str) -> Path:
    root = config.PACKAGE_DIR / sanitize_filename(package_id) / sanitize_filename(version_id)
    ensure_inside(config.PACKAGE_DIR, root)
    return root


def build_package_version(
    package: dict[str, Any],
    *,
    notes: str = "",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Build a versioned package snapshot and ZIP without locking it."""
    readiness = validate_package_readiness(package, db_path=db_path)
    database.create_package_version_event(
        event_id=f"PVE-{secrets.token_hex(8).upper()}",
        parent_package_id=package["package_id"],
        event_type="READINESS_REVIEW",
        event_details_json=json.dumps(readiness.__dict__, sort_keys=True),
        db_path=db_path,
    )
    if readiness.errors:
        database.create_package_version_event(
            event_id=f"PVE-{secrets.token_hex(8).upper()}",
            parent_package_id=package["package_id"],
            event_type="BUILD_BLOCKED",
            event_details_json=json.dumps({"errors": readiness.errors}, sort_keys=True),
            db_path=db_path,
        )
        raise ValueError("Package is not ready to build.")
    build_fingerprint = package_build_fingerprint(package, db_path=db_path)
    reusable = database.find_package_version_by_build_fingerprint(
        package["package_id"], build_fingerprint, db_path=db_path
    )
    if reusable:
        database.create_package_version_event(
            event_id=f"PVE-{secrets.token_hex(8).upper()}",
            parent_package_id=package["package_id"],
            version_id=reusable["version_id"],
            event_type="PACKAGE_SNAPSHOT_REUSED",
            event_details_json=json.dumps({"build_fingerprint": build_fingerprint}, sort_keys=True),
            db_path=db_path,
        )
        return reusable
    version = database.allocate_package_version(
        {
            "parent_package_id": package["package_id"],
            "ticker": package["ticker"],
            "company_name": package.get("company_name"),
            "security_type": package["security_type"],
            "research_cutoff_date": package["research_cutoff_date"],
            "status": config.VERSION_STATUS_BUILDING,
            "created_by": "analyst",
            "created_at": _now(),
            "notes": notes,
            "collection_profile_name": package.get("collection_profile_name"),
            "collection_profile_snapshot_json": package.get("collection_profile_snapshot_json"),
            "selected_years_json": package.get("selected_years_json"),
            "selected_months_json": package.get("selected_months_json"),
            "research_window_fingerprint": package.get("research_window_fingerprint"),
        },
        db_path=db_path,
    )
    version_id = version["version_id"]
    version = database.update_package_version(
        version_id, {"build_fingerprint": build_fingerprint}, db_path=db_path
    ) or version
    version_number = int(version["version_number"])
    database.create_package_version_event(
        event_id=f"PVE-{secrets.token_hex(8).upper()}",
        parent_package_id=package["package_id"],
        version_id=version_id,
        event_type="BUILD_STARTED",
        db_path=db_path,
    )
    final_root = _version_root(package["package_id"], version_id)
    staging_parent = final_root.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{version_id}.", dir=staging_parent))
    try:
        for folder in PACKAGE_FOLDERS:
            (staging / folder).mkdir(parents=True, exist_ok=True)
        documents = [
            doc for doc in included_documents(package["package_id"], db_path=db_path)
            if doc.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
        ]
        version_docs: list[dict[str, Any]] = []
        manifest_docs: list[dict[str, Any]] = []
        used_names_by_folder: dict[str, set[str]] = {}
        for index, doc in enumerate(sorted(documents, key=lambda item: item["document_id"]), start=1):
            source = _managed_path(doc["local_path"])
            folder = _folder_for_document(doc)
            used = used_names_by_folder.setdefault(folder, set())
            package_filename = _package_filename(doc, index, used)
            destination = staging / folder / package_filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            copied_hash = sha256_file(destination)
            if copied_hash != doc["sha256_hash"]:
                database.create_package_version_event(
                    event_id=f"PVE-{secrets.token_hex(8).upper()}",
                    parent_package_id=package["package_id"],
                    version_id=version_id,
                    event_type="HASH_MISMATCH",
                    event_details_json=json.dumps({"document_id": doc["document_id"]}, sort_keys=True),
                    db_path=db_path,
                )
                raise ValueError(f"Hash mismatch after copy for {doc['title']}")
            relative_path = destination.relative_to(staging).as_posix()
            category_code = _category_code(doc)
            category = CATEGORIES.get(category_code, CATEGORIES["other"])
            version_doc = {
                "version_id": version_id,
                "document_id": f"{version_id}-{index:04d}",
                "original_document_id": doc["document_id"],
                "category": doc.get("category") or category.display_name,
                "title": doc.get("title"),
                "source_name": doc.get("source_name"),
                "source_url": doc.get("source_url"),
                "publication_date": doc.get("publication_date") or doc.get("document_date"),
                "original_filename": doc.get("original_filename") or doc.get("local_filename"),
                "package_filename": package_filename,
                "relative_package_path": relative_path,
                "file_size": destination.stat().st_size,
                "sha256_hash": copied_hash,
                "mime_type": doc.get("mime_type"),
                "is_public": bool(doc.get("is_public")),
                "included_status": "INCLUDED",
            }
            database.create_package_version_document(version_doc, db_path=db_path)
            version_docs.append(version_doc)
            manifest_docs.append(_manifest_document_entry(doc, version_doc, category, package_filename, relative_path))
            database.create_package_version_event(
                event_id=f"PVE-{secrets.token_hex(8).upper()}",
                parent_package_id=package["package_id"],
                version_id=version_id,
                event_type="FILE_COPIED",
                event_details_json=json.dumps({"document_id": doc["document_id"], "path": relative_path}, sort_keys=True),
                db_path=db_path,
            )
        checklist = ensure_package_checklist(package, db_path=db_path)
        checklist_snapshot = _checklist_snapshot(checklist, version_docs)
        manifest_dir = staging / "00_Package_Manifest"
        inventory_rows = _inventory_rows(version_id, package, manifest_docs)
        inventory_fields = list(inventory_rows[0].keys()) if inventory_rows else [
            "package_version", "ticker", "document_id", "title", "category", "group", "public_or_licensed", "source", "institution", "publication_date", "report_period", "original_filename", "package_filename", "relative_path", "file_type", "file_size", "sha256", "status", "notes"
        ]
        _write_csv(manifest_dir / "document_inventory.csv", inventory_rows, inventory_fields)
        _write_inventory_xlsx(manifest_dir / "document_inventory.xlsx", inventory_rows, inventory_fields)
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="INVENTORY_CREATED", db_path=db_path)
        checklist_hash_payload = {"items": checklist_snapshot}
        _write_json(manifest_dir / "package_checklist.json", checklist_hash_payload)
        _write_csv(manifest_dir / "package_checklist.csv", checklist_snapshot, list(checklist_snapshot[0].keys()) if checklist_snapshot else ["checklist_item_id"])
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="CHECKLIST_SNAPSHOT_CREATED", db_path=db_path)
        manifest = _manifest(package, version, manifest_docs, checklist, readiness, staging)
        manifest_sha = _write_json(manifest_dir / "package_manifest.json", manifest)
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="MANIFEST_CREATED", db_path=db_path)
        integrity = verify_snapshot(staging, manifest_docs, manifest_sha)
        integrity_hash = _write_json(manifest_dir / "integrity_report.json", integrity)
        if integrity["overall_integrity_status"] == config.INTEGRITY_FAILED:
            raise ValueError("Integrity verification failed.")
        if final_root.exists():
            raise ValueError("Version directory already exists.")
        os.replace(staging, final_root)
        zip_path, zip_sha = create_package_zip(final_root, package, version_number)
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="ZIP_CREATED", event_details_json=json.dumps({"zip_path": str(zip_path)}, sort_keys=True), db_path=db_path)
        updated = database.update_package_version(
            version_id,
            {
                "status": config.VERSION_STATUS_BUILT,
                "document_count": len(version_docs),
                "public_document_count": sum(1 for doc in version_docs if doc["is_public"]),
                "licensed_document_count": sum(1 for doc in version_docs if not doc["is_public"]),
                "total_size_bytes": sum(int(doc["file_size"]) for doc in version_docs),
                "checklist_snapshot_json": json.dumps(checklist_snapshot, sort_keys=True),
                "manifest_path": str(final_root / "00_Package_Manifest" / "package_manifest.json"),
                "manifest_sha256": manifest_sha,
                "inventory_path": str(final_root / "00_Package_Manifest" / "document_inventory.csv"),
                "checklist_report_path": str(final_root / "00_Package_Manifest" / "package_checklist.json"),
                "integrity_report_path": str(final_root / "00_Package_Manifest" / "integrity_report.json"),
                "integrity_status": integrity["overall_integrity_status"],
                "zip_path": str(zip_path),
                "zip_sha256": zip_sha,
            },
            db_path=db_path,
        )
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="INTEGRITY_VERIFIED", event_details_json=json.dumps({"status": integrity["overall_integrity_status"], "integrity_hash": integrity_hash}, sort_keys=True), db_path=db_path)
        return updated or {}
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        database.update_package_version(version_id, {"status": config.VERSION_STATUS_BUILD_FAILED, "error_message": str(exc)}, db_path=db_path)
        database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=package["package_id"], version_id=version_id, event_type="BUILD_FAILED", event_details_json=json.dumps({"error": str(exc)}, sort_keys=True), db_path=db_path)
        raise


def _manifest_document_entry(doc: dict[str, Any], version_doc: dict[str, Any], category: Any, package_filename: str, relative_path: str) -> dict[str, Any]:
    return {
        "document_id": version_doc["document_id"],
        "original_document_id": doc["document_id"],
        "title": doc.get("title"),
        "category": version_doc.get("category"),
        "category_group": category.group,
        "public_or_licensed": "Public" if int(doc.get("is_public")) else "Licensed",
        "source_type": doc.get("source_type") or doc.get("collection_method"),
        "source_institution": doc.get("source_institution"),
        "source_url": doc.get("source_url"),
        "accession_number": doc.get("accession_number"),
        "publication_date": doc.get("publication_date"),
        "report_period": doc.get("report_period"),
        "as_of_date": doc.get("document_date"),
        "original_filename": version_doc.get("original_filename"),
        "package_filename": package_filename,
        "relative_path": relative_path,
        "file_extension": doc.get("file_extension") or Path(package_filename).suffix,
        "mime_type": doc.get("mime_type"),
        "file_size": version_doc["file_size"],
        "sha256": version_doc["sha256_hash"],
        "upload_or_collection_method": doc.get("collection_method"),
        "classification_confidence": doc.get("suggested_confidence"),
        "analyst_notes": doc.get("analyst_notes"),
        "authorization_acknowledgement": bool(doc.get("authorization_confirmed")),
        "staleness_status": "",
        "inclusion_timestamp": _now(),
    }


def _inventory_rows(version_id: str, package: dict[str, Any], docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "package_version": version_id,
            "ticker": package["ticker"],
            "document_id": doc["document_id"],
            "title": doc["title"],
            "category": doc["category"],
            "group": doc["category_group"],
            "public_or_licensed": doc["public_or_licensed"],
            "source": doc["source_type"],
            "institution": doc["source_institution"],
            "publication_date": doc["publication_date"],
            "report_period": doc["report_period"],
            "original_filename": doc["original_filename"],
            "package_filename": doc["package_filename"],
            "relative_path": doc["relative_path"],
            "file_type": doc["file_extension"],
            "file_size": doc["file_size"],
            "sha256": doc["sha256"],
            "status": "INCLUDED",
            "notes": doc["analyst_notes"],
        }
        for doc in docs
    ]


def _checklist_snapshot(items: list[dict[str, Any]], version_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category: dict[str, list[str]] = {}
    for doc in version_docs:
        by_category.setdefault(str(doc.get("category")), []).append(doc["document_id"])
    return [
        {
            "checklist_item_id": item["checklist_item_id"],
            "display_name": item["display_name"],
            "group": item["checklist_group"],
            "requirement_level": item["requirement_level"],
            "automatic_status": item["automatic_status"],
            "analyst_override": item.get("analyst_override_status"),
            "effective_status": item["effective_status"],
            "matched_document_count": item["matched_document_count"],
            "matched_document_ids": ";".join(by_category.get(item["display_name"], [])),
            "latest_document_date": item.get("latest_document_date"),
            "analyst_note": item.get("analyst_note"),
            "acknowledgement_state": "ACKNOWLEDGED" if item.get("analyst_override_status") else "AUTOMATIC",
        }
        for item in items
    ]


def _manifest(package: dict[str, Any], version: dict[str, Any], docs: list[dict[str, Any]], checklist: list[dict[str, Any]], readiness: ReadinessResult, root: Path) -> dict[str, Any]:
    category_counts: dict[str, int] = {}
    for doc in docs:
        category_counts[doc["category"]] = category_counts.get(doc["category"], 0) + 1
    missing = [item for item in checklist if item["effective_status"] == config.CHECKLIST_STATUS_MISSING]
    stale = [item for item in checklist if item["effective_status"] == config.CHECKLIST_STATUS_STALE]
    needs = [item for item in checklist if item["effective_status"] == config.CHECKLIST_STATUS_NEEDS_REVIEW]
    return {
        "product_name": config.APP_NAME,
        "schema_version": "4.0",
        "package_id": package["package_id"],
        "version_id": version["version_id"],
        "display_version": version.get("display_version"),
        "version_number": version["version_number"],
        "ticker": package["ticker"],
        "company_name": package.get("company_name"),
        "cik": package.get("cik"),
        "security_type": package["security_type"],
        "research_cutoff_date": package["research_cutoff_date"],
        "filing_history_years": package["filing_history_years"],
        "research_time_window": {
            "selected_years": json.loads(package.get("selected_years_json") or "[]"),
            "selected_months": json.loads(package.get("selected_months_json") or "[]"),
            "fingerprint": package.get("research_window_fingerprint"),
        },
        "created_timestamp": version["created_at"],
        "locked_timestamp": None,
        "analyst_review_acknowledgement": bool(package.get("checklist_reviewed")),
        "package_status": version["status"],
        "document_counts": {"total": len(docs), "public": sum(1 for doc in docs if doc["public_or_licensed"] == "Public"), "licensed": sum(1 for doc in docs if doc["public_or_licensed"] == "Licensed")},
        "category_counts": category_counts,
        "total_file_size": sum(int(doc["file_size"]) for doc in docs),
        "checklist_coverage_summary": coverage_summary(checklist),
        "missing_checklist_items": [item["display_name"] for item in missing],
        "stale_items": [item["display_name"] for item in stale],
        "needs_review_items": [item["display_name"] for item in needs],
        "build_warnings": readiness.warnings,
        "documents": sorted(docs, key=lambda doc: doc["document_id"]),
    }


def verify_snapshot(root: Path, manifest_docs: list[dict[str, Any]], manifest_sha: str | None = None) -> dict[str, Any]:
    missing: list[str] = []
    mismatches: list[str] = []
    size_mismatches: list[str] = []
    normalized_docs = [
        {
            **doc,
            "relative_path": doc.get("relative_path") or doc.get("relative_package_path"),
            "sha256": doc.get("sha256") or doc.get("sha256_hash"),
            "file_size": doc.get("file_size") or doc.get("file_size_bytes"),
        }
        for doc in manifest_docs
    ]
    expected = {doc["relative_path"]: doc for doc in normalized_docs}
    for relative, doc in expected.items():
        path = root / relative
        if not path.exists():
            missing.append(relative)
            continue
        if path.stat().st_size != int(doc["file_size"]):
            size_mismatches.append(relative)
        if sha256_file(path) != doc["sha256"]:
            mismatches.append(relative)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".tmp")
    }
    allowed_manifest_files = {
        "00_Package_Manifest/package_manifest.json",
        "00_Package_Manifest/document_inventory.csv",
        "00_Package_Manifest/document_inventory.xlsx",
        "00_Package_Manifest/package_checklist.json",
        "00_Package_Manifest/package_checklist.csv",
        "00_Package_Manifest/integrity_report.json",
    }
    unexpected = sorted(actual - set(expected) - allowed_manifest_files)
    status = config.INTEGRITY_FAILED if missing or mismatches or size_mismatches else config.INTEGRITY_VERIFIED_WITH_WARNINGS if unexpected else config.INTEGRITY_VERIFIED
    return {
        "manifest_hash": manifest_sha,
        "number_of_files_checked": len(expected),
        "number_passed": len(expected) - len(missing) - len(mismatches) - len(size_mismatches),
        "number_failed": len(missing) + len(mismatches) + len(size_mismatches),
        "missing_files": missing,
        "hash_mismatches": mismatches,
        "size_mismatches": size_mismatches,
        "unexpected_files": unexpected,
        "verification_timestamp": _now(),
        "overall_integrity_status": status,
    }


def create_package_zip(root: Path, package: dict[str, Any], version_number: int) -> tuple[Path, str]:
    zip_name = sanitize_filename(f"{package['ticker']}_Equity_Research_Package_{package['research_cutoff_date']}_V{version_number:03d}.zip")
    zip_path = root.parent / zip_name
    if zip_path.exists():
        raise ValueError("Package ZIP already exists and will not be overwritten.")
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() in {".db", ".env"} or path.name.endswith(".tmp") or Path(relative).is_absolute():
                continue
            archive.write(path, relative)
    with zipfile.ZipFile(tmp_zip) as archive:
        names = archive.namelist()
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise ValueError("Unsafe path detected in generated ZIP.")
    os.replace(tmp_zip, zip_path)
    return zip_path, sha256_file(zip_path)


def lock_version(version_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    version = database.get_package_version(version_id, db_path=db_path)
    if not version:
        raise ValueError("Version does not exist.")
    if version["status"] == config.VERSION_STATUS_LOCKED:
        return version
    if version["status"] != config.VERSION_STATUS_BUILT:
        raise ValueError("Only built versions can be locked.")
    if version.get("integrity_status") == config.INTEGRITY_FAILED:
        raise ValueError("Version with failed integrity cannot be locked.")
    locked = database.lock_package_version(version_id, db_path=db_path)
    database.create_package_version_event(event_id=f"PVE-{secrets.token_hex(8).upper()}", parent_package_id=version["parent_package_id"], version_id=version_id, event_type="PACKAGE_LOCKED", db_path=db_path)
    database.update_package_collection_state(version["parent_package_id"], config.STATUS_PACKAGE_LOCKED, db_path=db_path)
    return locked or {}


def compare_versions(version_a: str, version_b: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    docs_a = database.list_package_version_documents(version_a, db_path=db_path)
    docs_b = database.list_package_version_documents(version_b, db_path=db_path)
    by_original_a = {doc["original_document_id"]: doc for doc in docs_a}
    by_original_b = {doc["original_document_id"]: doc for doc in docs_b}
    hashes_a = {doc["sha256_hash"]: doc for doc in docs_a}
    hashes_b = {doc["sha256_hash"]: doc for doc in docs_b}
    added = [doc for key, doc in by_original_b.items() if key not in by_original_a and doc["sha256_hash"] not in hashes_a]
    removed = [doc for key, doc in by_original_a.items() if key not in by_original_b and doc["sha256_hash"] not in hashes_b]
    same_hash_renamed = [
        {"from": hashes_a[sha]["package_filename"], "to": hashes_b[sha]["package_filename"], "sha256_hash": sha}
        for sha in hashes_a.keys() & hashes_b.keys()
        if hashes_a[sha]["package_filename"] != hashes_b[sha]["package_filename"]
    ]
    recategorized = [
        {"document_id": key, "from": by_original_a[key]["category"], "to": by_original_b[key]["category"]}
        for key in by_original_a.keys() & by_original_b.keys()
        if by_original_a[key]["category"] != by_original_b[key]["category"]
    ]
    changed_hashes = [
        {"document_id": key, "from": by_original_a[key]["sha256_hash"], "to": by_original_b[key]["sha256_hash"]}
        for key in by_original_a.keys() & by_original_b.keys()
        if by_original_a[key]["sha256_hash"] != by_original_b[key]["sha256_hash"]
    ]
    va = database.get_package_version(version_a, db_path=db_path) or {}
    vb = database.get_package_version(version_b, db_path=db_path) or {}
    checklist_a = json.loads(va.get("checklist_snapshot_json") or "[]")
    checklist_b = json.loads(vb.get("checklist_snapshot_json") or "[]")
    status_a = {item["checklist_item_id"]: item["effective_status"] for item in checklist_a}
    status_b = {item["checklist_item_id"]: item["effective_status"] for item in checklist_b}
    checklist_changes = [
        {"checklist_item_id": key, "from": status_a.get(key), "to": status_b.get(key)}
        for key in sorted(set(status_a) | set(status_b))
        if status_a.get(key) != status_b.get(key)
    ]
    return {
        "documents_added": added,
        "documents_removed": removed,
        "same_hash_renamed": same_hash_renamed,
        "documents_recategorized": recategorized,
        "hash_changes": changed_hashes,
        "checklist_status_changes": checklist_changes,
        "research_cutoff_changed": va.get("research_cutoff_date") != vb.get("research_cutoff_date"),
        "public_count_change": int(vb.get("public_document_count") or 0) - int(va.get("public_document_count") or 0),
        "licensed_count_change": int(vb.get("licensed_document_count") or 0) - int(va.get("licensed_document_count") or 0),
        "total_size_change": int(vb.get("total_size_bytes") or 0) - int(va.get("total_size_bytes") or 0),
    }
