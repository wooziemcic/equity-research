from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.services.package_discovery_service import get_earnings_anchor, latest_discovery_run, list_active_search_profiles
from app.services.package_artifact_service import list_package_artifacts, sync_package_artifacts
from app.services.package_recipe_service import list_assignments, list_slot_instances, recalculate_completion
from app.services.public_slot_status_service import public_slot_diagnostics
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
        awaiting_uploads = int(connection.execute(
            """SELECT COUNT(*) FROM slot_document_assignments
               WHERE package_id=? AND assignment_status IN ('SUGGESTED','NEEDS_REVIEW')""",
            (package_id,),
        ).fetchone()[0])
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
    artifacts = sync_package_artifacts(package_id, db_path=db_path)
    diagnostics = public_slot_diagnostics(package_id, db_path=db_path)
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
            "artifacts": len([row for row in artifacts if row.get("working_package_inclusion")]),
            "terminal_states": diagnostics["summary"],
        },
        "manual_package": {
            "filled": sum(slot["completion_status"] == "COMPLETE" for slot in manual_slots),
            "total": len(manual_slots),
            "missing": sum(slot["completion_status"] not in {"COMPLETE", "NOT_AVAILABLE"} for slot in manual_slots),
            "files_awaiting_approval": awaiting_uploads,
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
    artifacts = sync_package_artifacts(package_id, db_path=db_path)
    contents = [
        {
            "artifact_id": row["artifact_id"],
            "display_filename": row["display_filename"],
            "artifact_type": row["artifact_type"],
            "checklist_item": row.get("checklist_item") or row["purpose_label"],
            "source": row.get("source_institution") or row.get("source_name") or "Cutler",
            "document_date": row.get("document_date") or row.get("publication_date"),
            "file_type": Path(row["display_filename"]).suffix.lstrip(".").upper(),
            "status": "Included" if row.get("source_document_id") else "Generated on download",
            "size": int(row.get("file_size_bytes") or 0),
            "analysis_eligible": bool(row.get("analysis_eligible")),
            "conversion_status": row["conversion_status"],
            "document_id": row.get("source_document_id"),
            "local_path": row.get("local_path"),
            "source_url": row.get("source_url"),
        }
        for row in artifacts if row.get("working_package_inclusion")
    ]
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
    artifacts = sync_package_artifacts(package_id, db_path=db_path)
    return {
        "package_id": package_id,
        "earnings_anchor": get_earnings_anchor(package_id, db_path=db_path),
        "completion": public_package_summary(package_id, db_path=db_path),
        "document_ids": selected_document_ids(package_id, db_path=db_path),
        "artifact_ids": [row["artifact_id"] for row in artifacts if row.get("analysis_eligible")],
        "contents": [{key: value for key, value in row.items() if key not in {"local_path"}} for row in package_contents(package_id, db_path=db_path)],
    }
