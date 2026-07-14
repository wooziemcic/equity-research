from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app import config
from app.services.collection_profile import CUTLER_EQUITY_INTERN_GUIDE, normalize_sec_form
from app.services.package_service import PackageInput, create_package
from app.utils import database


DISPLAY_FAMILIES = ("10-K", "10-Q", "8-K", "S-3", "S-4", "DEF 14A", "144")


@dataclass(frozen=True)
class SecCollectionAudit:
    total_sec_inventory: int
    profile_eligible_filings: int
    selected_filings: int
    already_collected_filings: int
    excluded_by_profile_filings: int
    awaiting_selection_filings: int
    unique_accession_numbers: int
    duplicate_accession_numbers: int
    unique_source_urls: int
    duplicate_content_hashes: int
    amendments: int
    legacy_without_profile_metadata: int
    eligible_for_next_build: int
    family_breakdown: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _audit_documents(
    package_id: str,
    version_id: str | None,
    *,
    db_path: Path | str,
) -> list[dict[str, Any]]:
    if not version_id:
        return [
            doc for doc in database.list_documents_by_package(package_id, db_path=db_path)
            if doc.get("collection_method") == "SEC" or doc.get("accession_number")
        ]
    rows: list[dict[str, Any]] = []
    for version_doc in database.list_package_version_documents(version_id, db_path=db_path):
        original = database.get_document_by_document_id(version_doc["original_document_id"], db_path=db_path)
        if not original or not original.get("accession_number"):
            continue
        rows.append({**original, "version_document_id": version_doc["document_id"], "version_hash": version_doc.get("sha256_hash")})
    return rows


def audit_sec_collection(
    package_id: str,
    *,
    version_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> SecCollectionAudit:
    documents = _audit_documents(package_id, version_id, db_path=db_path)
    inventory = [] if version_id else database.list_sec_filing_inventory(package_id, db_path=db_path)
    inclusions = {
        row["document_id"]: bool(row["included"])
        for row in database.list_draft_document_inclusions(package_id, db_path=db_path)
    }
    accessions = [str(doc.get("accession_number") or "") for doc in documents if doc.get("accession_number")]
    urls = [str(doc.get("source_url") or "") for doc in documents if doc.get("source_url")]
    hashes = [str(doc.get("sha256_hash") or doc.get("version_hash") or "") for doc in documents if doc.get("sha256_hash") or doc.get("version_hash")]
    family_counts: Counter[str] = Counter()
    eligible = 0
    legacy = 0
    for doc in documents:
        original_form = str(doc.get("form_type") or "")
        family = doc.get("normalized_form_family") or normalize_sec_form(original_form)
        if not doc.get("normalized_form_family"):
            legacy += 1
        display = family if family in DISPLAY_FAMILIES else "EXCLUDED_OR_UNKNOWN"
        family_counts[display] += 1
        if family in DISPLAY_FAMILIES:
            eligible += 1
    for family in (*DISPLAY_FAMILIES, "EXCLUDED_OR_UNKNOWN"):
        family_counts.setdefault(family, 0)
    excluded_inventory = sum(row.get("inventory_status") in {"EXCLUDED_BY_PROFILE", "EXCLUDED_8K_MODE"} for row in inventory)
    awaiting_inventory = sum(row.get("inventory_status") in {"AWAITING_SELECTION", "AWAITING_8K_SELECTION"} for row in inventory)
    selected_inventory = sum(bool(row.get("selected")) for row in inventory)
    already_inventory = sum(row.get("inventory_status") == "ALREADY_COLLECTED" for row in inventory)
    total = len(inventory) if inventory else len(documents)
    return SecCollectionAudit(
        total_sec_inventory=total,
        profile_eligible_filings=sum(row.get("normalized_form_family") in DISPLAY_FAMILIES and row.get("inventory_status") not in {"EXCLUDED_BY_PROFILE", "EXCLUDED_8K_MODE"} for row in inventory) if inventory else eligible,
        selected_filings=selected_inventory if inventory else len(documents),
        already_collected_filings=already_inventory if inventory else len(documents),
        excluded_by_profile_filings=excluded_inventory if inventory else family_counts["EXCLUDED_OR_UNKNOWN"],
        awaiting_selection_filings=awaiting_inventory,
        unique_accession_numbers=len(set(accessions)),
        duplicate_accession_numbers=sum(count - 1 for count in Counter(accessions).values() if count > 1),
        unique_source_urls=len(set(urls)),
        duplicate_content_hashes=sum(count - 1 for count in Counter(hashes).values() if count > 1),
        amendments=sum("/A" in str(doc.get("form_type") or "").upper() for doc in documents),
        legacy_without_profile_metadata=legacy,
        eligible_for_next_build=sum(
            (doc.get("normalized_form_family") or normalize_sec_form(doc.get("form_type"))) in DISPLAY_FAMILIES
            and inclusions.get(doc["document_id"], True)
            for doc in documents
        ),
        family_breakdown=dict(family_counts),
    )


def reconcile_draft_with_current_profile(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        raise ValueError("Package does not exist.")
    documents = database.list_documents_by_package(package_id, db_path=db_path)
    rows: list[dict[str, Any]] = []
    included = 0
    excluded = 0
    for doc in documents:
        family = doc.get("normalized_form_family") or normalize_sec_form(doc.get("form_type"))
        is_sec = bool(doc.get("accession_number") or doc.get("collection_method") == "SEC")
        approved = not is_sec or family in DISPLAY_FAMILIES
        reason = None if approved else "SEC form is outside CUTLER_EQUITY_INTERN_GUIDE."
        rows.append({"document_id": doc["document_id"], "included": approved, "reason": reason, "profile_name": CUTLER_EQUITY_INTERN_GUIDE})
        included += int(approved)
        excluded += int(not approved)
    database.replace_draft_document_inclusions(package_id, rows, db_path=db_path)
    database.create_audit_event(
        event_id=f"AUD-RECONCILE-{database.utc_now_iso()}-{package_id}",
        package_id=package_id,
        event_type="DRAFT_RECONCILED_WITH_COLLECTION_PROFILE",
        event_details_json=json.dumps({"included": included, "excluded": excluded, "profile": CUTLER_EQUITY_INTERN_GUIDE}, sort_keys=True),
        db_path=db_path,
    )
    return {"included": included, "excluded": excluded}


def create_new_draft_from_current_profile(
    *,
    source_version_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    version = database.get_package_version(source_version_id, db_path=db_path)
    if not version or version.get("status") != config.VERSION_STATUS_LOCKED:
        raise ValueError("A locked source version is required.")
    source_package = database.get_package_by_package_id(version["parent_package_id"], db_path=db_path) or {}
    draft = create_package(
        PackageInput(
            ticker=version["ticker"], security_type=version["security_type"],
            research_cutoff_date=date.fromisoformat(version["research_cutoff_date"]),
            filing_history_years=int(source_package.get("filing_history_years") or 3),
            analyst_notes=f"Draft created from locked version {source_version_id} using {CUTLER_EQUITY_INTERN_GUIDE}.",
        ),
        db_path=db_path,
    )
    company_fields = {
        key: source_package.get(key)
        for key in (
            "ticker", "company_name", "cik", "exchange", "sic", "industry_description", "fiscal_year_end",
            "sec_company_url", "resolution_status", "resolution_source", "resolution_timestamp",
        )
    }
    draft = database.update_package_company_metadata(draft["package_id"], company_fields, db_path=db_path) or draft
    copied = 0
    for version_doc in database.list_package_version_documents(source_version_id, db_path=db_path):
        original = database.get_document_by_document_id(version_doc["original_document_id"], db_path=db_path)
        if not original:
            continue
        family = original.get("normalized_form_family") or normalize_sec_form(original.get("form_type"))
        is_sec = bool(original.get("accession_number") or original.get("collection_method") == "SEC")
        if is_sec and family not in DISPLAY_FAMILIES:
            continue
        clone = {key: value for key, value in original.items() if key not in {"id", "created_at", "updated_at", "source_identity_key"}}
        clone["document_id"] = database.generate_document_id("DOC-DRAFT")
        clone["package_id"] = draft["package_id"]
        clone["ticker"] = draft["ticker"]
        created = database.create_document_record(clone, db_path=db_path)
        database.update_document_metadata(
            created["document_id"], {"normalized_form_family": family}, db_path=db_path
        )
        copied += 1
    return {**draft, "documents_reused": copied, "source_version_id": source_version_id}
