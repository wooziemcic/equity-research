from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.services.package_discovery_service import get_earnings_anchor, latest_discovery_run, list_active_search_profiles
from app.services.package_naming_service import generate_package_display_filename
from app.services.package_recipe_service import list_assignments, list_slot_instances, recalculate_completion
from app.services.slot_policy_service import effective_document_counts
from app.utils import database


CONTENT_ORDER = {
    "most_recent_10_q_and_10_k": 10,
    "latest_earnings_release": 20,
    "available_supplemental_or_earnings_presentation": 21,
    "latest_earnings_call_transcript": 22,
    "latest_earnings_call_audio": 23,
    "investor_presentations": 30,
    "material_company_press_releases_since_last_earnings_release": 40,
    "executive_compensation_information": 50,
    "liquidity_and_capital_resources": 51,
    "description_of_business_and_risk": 52,
    "sell_side_reports": 60,
    "initiated_coverage_report": 61,
    "credit_reports": 62,
    "industry_report": 63,
    "morningstar_report_and_most_recent_model": 64,
    "cast_summary_chart": 70,
    "drsk_default_risk": 71,
    "ccm_historical_multiples_valuation": 72,
    "bbg_fa": 73,
    "bbg_fa_credit_ratios": 74,
}


def public_package_summary(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    started = perf_counter()
    recalculate_completion(package_id, db_path=db_path)
    slots = list_slot_instances(package_id, db_path=db_path)
    assignments = [
        row for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED" and row.get("selected_for_package")
    ]
    public_types = {row["normalized_slot_type"] for row in list_active_search_profiles(db_path=db_path)}
    public_slots = [slot for slot in slots if slot["normalized_slot_type"] in public_types]
    manual_slots = [slot for slot in slots if slot["normalized_slot_type"] not in public_types and slot["completion_status"] != "NOT_APPLICABLE"]
    run = latest_discovery_run(package_id, db_path=db_path)
    with database.get_connection(db_path) as connection:
        search_counts = connection.execute(
            """SELECT COUNT(*) AS planned,
                      SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                      SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed
               FROM slot_discovery_runs WHERE discovery_run_id=?""",
            ((run or {}).get("discovery_run_id"),),
        ).fetchone() if run else {"planned": 0, "completed": 0, "failed": 0}
        candidate_counts = connection.execute(
            """SELECT COUNT(*) AS discovered,
                      SUM(CASE WHEN candidate_status='NEEDS_ANALYST_REVIEW' THEN 1 ELSE 0 END) AS review,
                      SUM(CASE WHEN candidate_status IN ('REJECTED','NON_INVESTOR_MATERIAL','MIME_MISMATCH','COMPANY_MISMATCH','FAILED') THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN candidate_status IN ('AUTO_SELECTED','DOWNLOADED','ALREADY_COLLECTED') THEN 1 ELSE 0 END) AS selected
               FROM discovered_candidates WHERE package_id=?""",
            (package_id,),
        ).fetchone()
    public_filled = sum(slot["completion_status"] == "COMPLETE" for slot in public_slots)
    required_public = [slot for slot in public_slots if slot["requirement_snapshot"] == "REQUIRED"]
    public_document_ids = {
        row["document_id"] for row in assignments
        if any(slot["package_slot_instance_id"] == row["package_slot_instance_id"] for slot in public_slots)
    }
    downloaded = len({
        row["document_id"] for row in assignments
        if row["document_id"] in public_document_ids and row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
    })
    return {
        "discovery": {
            "planned": int(search_counts["planned"] or 0),
            "completed": int(search_counts["completed"] or 0),
            "failed": int(search_counts["failed"] or 0),
            "queries_executed": int((run or {}).get("queries_executed") or 0),
            "candidates_discovered": int(candidate_counts["discovered"] or 0),
            "candidates_requiring_review": int(candidate_counts["review"] or 0),
        },
        "public_package": {
            "filled": public_filled,
            "total": len(public_slots),
            "required_filled": sum(slot["completion_status"] == "COMPLETE" for slot in required_public),
            "required_total": len(required_public),
            "missing": len(public_slots) - public_filled,
            "documents_downloaded": downloaded,
            "documents_assigned": len(public_document_ids),
        },
        "manual_package": {
            "filled": sum(slot["completion_status"] == "COMPLETE" for slot in manual_slots),
            "total": len(manual_slots),
            "missing": sum(slot["completion_status"] not in {"COMPLETE", "NOT_AVAILABLE"} for slot in manual_slots),
        },
        "candidate_funnel": {
            "selected": int(candidate_counts["selected"] or 0),
            "review": int(candidate_counts["review"] or 0),
            "rejected": int(candidate_counts["rejected"] or 0),
        },
        "load_ms": round((perf_counter() - started) * 1000, 1),
    }


def package_contents(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    started = perf_counter()
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        return []
    slots = {slot["package_slot_instance_id"]: slot for slot in list_slot_instances(package_id, db_path=db_path)}
    documents = {row["document_id"]: row for row in database.list_documents_by_package(package_id, db_path=db_path)}
    anchor = get_earnings_anchor(package_id, db_path=db_path)
    used_names: list[str] = []
    contents: list[dict[str, Any]] = []
    contents_by_document: dict[str, dict[str, Any]] = {}
    approved = [
        row for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED" and row.get("selected_for_package")
    ]
    approved.sort(key=lambda row: (CONTENT_ORDER.get(slots[row["package_slot_instance_id"]]["normalized_slot_type"], 80), row.get("display_order") or 0))
    for assignment in approved:
        document = documents.get(assignment["document_id"])
        slot = slots.get(assignment["package_slot_instance_id"])
        if not document or not slot or document.get("collection_status") != config.DOCUMENT_STATUS_DOWNLOADED:
            continue
        if document["document_id"] in contents_by_document:
            existing = contents_by_document[document["document_id"]]
            labels = [part.strip() for part in existing["checklist_item"].split(";")]
            if slot["display_name_snapshot"] not in labels:
                existing["checklist_item"] += f"; {slot['display_name_snapshot']}"
            continue
        filename = document.get("package_display_filename") or generate_package_display_filename(
            ticker=package["ticker"], slot_type=slot["normalized_slot_type"], document=document,
            anchor=anchor, existing_names=used_names,
        )
        used_names.append(filename)
        if document.get("package_display_filename") != filename or not document.get("working_package_inclusion"):
            database.update_document_metadata(
                document["document_id"],
                {"package_display_filename": filename, "working_package_inclusion": 1, "audit_package_inclusion": 1},
                db_path=db_path,
            )
        item = {
            "display_filename": filename,
            "checklist_item": slot["display_name_snapshot"],
            "normalized_slot_type": slot["normalized_slot_type"],
            "source": document.get("source_institution") or document.get("source_name") or "Unknown",
            "document_date": document.get("document_date") or document.get("publication_date"),
            "file_type": Path(filename).suffix.lstrip(".").upper(),
            "status": "Included",
            "size": int(document.get("file_size_bytes") or 0),
            "highlighted": bool(assignment.get("highlighted_research")),
            "document_id": document["document_id"],
            "local_path": document.get("local_path"),
            "source_url": document.get("source_url"),
        }
        contents.append(item)
        contents_by_document[document["document_id"]] = item
    if contents:
        contents[0]["preview_load_ms"] = round((perf_counter() - started) * 1000, 1)
    return contents


def selected_document_ids(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[str]:
    return sorted({
        row["document_id"] for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED"
        and row.get("selected_for_package")
        and row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
        and row.get("sha256_hash")
    })


def package_snapshot(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    return {
        "package_id": package_id,
        "earnings_anchor": get_earnings_anchor(package_id, db_path=db_path),
        "completion": public_package_summary(package_id, db_path=db_path),
        "document_ids": selected_document_ids(package_id, db_path=db_path),
        "contents": [{key: value for key, value in row.items() if key not in {"local_path"}} for row in package_contents(package_id, db_path=db_path)],
    }
