from __future__ import annotations

import json
import secrets
from collections import defaultdict
from pathlib import Path
from typing import Any

from app import config
from app.services.taxonomy import CHECKLIST_PROFILES
from app.utils import database


PUBLIC_FORM_CATEGORY_MAP = {
    "10-K": "annual_filing",
    "20-F": "annual_filing",
    "10-Q": "quarterly_filing",
    "8-K": "current_report",
    "6-K": "current_report",
    "DEF 14A": "proxy_statement",
}

MISSING_COVERAGE_STATUSES = {
    config.CHECKLIST_STATUS_MISSING,
    config.CHECKLIST_STATUS_STALE,
    config.CHECKLIST_STATUS_NEEDS_REVIEW,
}


def normalize_requirement_level(value: Any) -> str:
    """Normalize checklist requirement levels for storage and comparison."""
    return str(value or "").strip().lower()


def normalize_checklist_status(value: Any) -> str:
    """Normalize checklist status values for storage and comparison."""
    return str(value or "").strip().upper()


def _document_category_code(document: dict[str, Any]) -> str:
    if document.get("final_category_code"):
        return document["final_category_code"]
    if document.get("form_type") in PUBLIC_FORM_CATEGORY_MAP:
        return PUBLIC_FORM_CATEGORY_MAP[document["form_type"]]
    category = (document.get("category") or "").lower()
    if "earnings release" in category:
        return "earnings_release"
    if "presentation" in category:
        return "investor_presentation"
    if "transcript" in category:
        return "earnings_transcript"
    return ""


def ensure_package_checklist(
    package: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Create checklist rows for a package and recalculate automatic statuses."""
    profile = CHECKLIST_PROFILES.get(package.get("security_type"), CHECKLIST_PROFILES["Other"])
    documents = [
        doc
        for doc in database.list_documents_by_package(package["package_id"], db_path=db_path)
        if doc.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
    ]
    docs_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for document in documents:
        code = _document_category_code(document)
        if code:
            docs_by_category[code].append(document)
    links: list[dict[str, str]] = []
    for item in profile:
        matches = docs_by_category.get(item["category_code"], [])
        automatic = (
            config.CHECKLIST_STATUS_AVAILABLE
            if matches
            else config.CHECKLIST_STATUS_MISSING
        )
        latest = max(
            [
                value
                for doc in matches
                for value in (doc.get("publication_date"), doc.get("document_date"))
                if value
            ],
            default=None,
        )
        existing = database.get_checklist_item(
            package["package_id"],
            item["id"],
            db_path=db_path,
        )
        override = normalize_checklist_status(existing.get("analyst_override_status")) if existing and existing.get("analyst_override_status") else None
        note = existing.get("analyst_note") if existing else None
        effective = override or automatic
        database.upsert_checklist_item(
            {
                "checklist_item_id": item["id"],
                "package_id": package["package_id"],
                "category_code": item["category_code"],
                "display_name": item["display_name"],
                "requirement_level": normalize_requirement_level(item["requirement_level"]),
                "checklist_group": item["group"],
                "applicability": "APPLICABLE",
                "automatic_status": normalize_checklist_status(automatic),
                "analyst_override_status": override,
                "effective_status": normalize_checklist_status(effective),
                "analyst_note": note,
                "matched_document_count": len(matches),
                "latest_document_date": latest,
            },
            db_path=db_path,
        )
        for document in matches[:1]:
            links.append(
                {
                    "document_id": document["document_id"],
                    "checklist_item_id": item["id"],
                    "link_method": "CATEGORY",
                }
            )
    database.replace_document_checklist_links(package["package_id"], links, db_path=db_path)
    return database.list_checklist_items(package["package_id"], db_path=db_path)


def set_override(
    package_id: str,
    checklist_item_id: str,
    override_status: str | None,
    note: str | None,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any] | None:
    """Apply or clear an analyst checklist override."""
    normalized_override = normalize_checklist_status(override_status) if override_status else None
    item = database.set_checklist_override(
        package_id,
        checklist_item_id,
        normalized_override,
        note,
        db_path=db_path,
    )
    database.create_audit_event(
        event_id=f"AUD-{secrets.token_hex(8).upper()}",
        package_id=package_id,
        event_type="CHECKLIST_OVERRIDE_APPLIED" if override_status else "CHECKLIST_OVERRIDE_REMOVED",
        event_details_json=json.dumps(
            {
                "checklist_item_id": checklist_item_id,
                "override_status": normalized_override,
                "note": note,
            },
            sort_keys=True,
        ),
        db_path=db_path,
    )
    return item


def coverage_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Return grouped checklist counters for review metrics."""
    summary = {
        "required_available": 0,
        "recommended_available": 0,
        "optional_available": 0,
        "missing": 0,
        "needs_review": 0,
        "stale": 0,
        "not_available": 0,
        "not_applicable": 0,
        "available_required": 0,
        "missing_required": 0,
        "missing_recommended": 0,
    }
    for item in items:
        level = normalize_requirement_level(item.get("requirement_level"))
        status = normalize_checklist_status(
            item.get("effective_status")
            or item.get("analyst_override_status")
            or item.get("automatic_status")
        )
        if status == config.CHECKLIST_STATUS_AVAILABLE:
            if level == "required":
                summary["required_available"] += 1
                summary["available_required"] += 1
            elif level == "recommended":
                summary["recommended_available"] += 1
            else:
                summary["optional_available"] += 1
        if level == "required" and status in MISSING_COVERAGE_STATUSES:
            summary["missing_required"] += 1
        if level == "recommended" and status in MISSING_COVERAGE_STATUSES:
            summary["missing_recommended"] += 1
        if status == config.CHECKLIST_STATUS_MISSING:
            summary["missing"] += 1
        if status == config.CHECKLIST_STATUS_NEEDS_REVIEW:
            summary["needs_review"] += 1
        if status == config.CHECKLIST_STATUS_STALE:
            summary["stale"] += 1
        if status == config.CHECKLIST_STATUS_NOT_AVAILABLE:
            summary["not_available"] += 1
        if status == config.CHECKLIST_STATUS_NOT_APPLICABLE:
            summary["not_applicable"] += 1
    return summary


def recategorize_document(
    package: dict[str, Any],
    document_id: str,
    category_code: str,
    *,
    title: str | None = None,
    source_institution: str | None = None,
    publication_date: str | None = None,
    document_date: str | None = None,
    analyst_notes: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any] | None:
    """Update editable metadata and recalculate checklist links."""
    from app.services.taxonomy import category_display

    updates: dict[str, Any] = {
        "final_category_code": category_code,
        "category": category_display(category_code),
    }
    if title is not None:
        updates["title"] = title
        updates["document_title"] = title
    if source_institution is not None:
        updates["source_institution"] = source_institution
    if publication_date is not None:
        updates["publication_date"] = publication_date
    if document_date is not None:
        updates["document_date"] = document_date
    if analyst_notes is not None:
        updates["analyst_notes"] = analyst_notes
    document = database.update_document_metadata(document_id, updates, db_path=db_path)
    database.create_audit_event(
        event_id=f"AUD-{secrets.token_hex(8).upper()}",
        package_id=package["package_id"],
        document_id=document_id,
        event_type="CATEGORY_CHANGED",
        event_details_json=json.dumps({"category_code": category_code}, sort_keys=True),
        db_path=db_path,
    )
    ensure_package_checklist(package, db_path=db_path)
    return document
