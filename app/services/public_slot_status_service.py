from __future__ import annotations

import hashlib
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.services.package_recipe_service import list_assignments, list_slot_instances
from app.services.slot_policy_service import effective_document_counts
from app.utils import database


TERMINAL_STATES = {
    "FILLED", "PARTIALLY_FILLED", "NO_CANDIDATE_FOUND", "CANDIDATES_REQUIRE_REVIEW",
    "ACKNOWLEDGED_UNAVAILABLE", "FAILED", "NOT_APPLICABLE",
}


MISSING_REASONS = {
    "available_supplemental_or_earnings_presentation": "No official earnings presentation was located for the current earnings cycle.",
    "latest_earnings_call_transcript": "No downloadable official transcript or prepared remarks were safely resolved.",
    "latest_earnings_call_audio": "No safely downloadable official webcast or earnings-call audio was resolved.",
    "investor_presentations": "No investor presentation inside the configured relevance window was approved.",
    "material_company_press_releases_since_last_earnings_release": "No material official company release since earnings was approved.",
    "latest_earnings_release": "No authoritative earnings release was approved.",
    "liquidity_and_capital_resources": "No applicable SEC filing was approved for liquidity and capital resources.",
    "description_of_business_and_risk": "No applicable SEC filing was approved for business and risk factors.",
    "executive_compensation_information": "No applicable proxy statement was approved for executive compensation.",
    "most_recent_10_q_and_10_k": "The latest original 10-Q and 10-K were not both approved.",
}


def _state_id(package_id: str, slot_id: str, run_id: str | None) -> str:
    digest = hashlib.sha256(f"{package_id}|{slot_id}|{run_id or 'CURRENT'}".encode()).hexdigest()[:20].upper()
    return f"PSS-{digest}"


def _public_types(*, db_path: Path | str) -> set[str]:
    with database.get_connection(db_path) as connection:
        return {
            row["normalized_slot_type"]
            for row in connection.execute(
                "SELECT normalized_slot_type FROM slot_search_profiles WHERE status='ACTIVE' AND enabled=1"
            ).fetchall()
        }


def public_discovery_preview(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    database.initialize_database(db_path)
    public_types = _public_types(db_path=db_path)
    slots = [row for row in list_slot_instances(package_id, db_path=db_path) if row["normalized_slot_type"] in public_types]
    profiles: dict[str, dict[str, Any]] = {}
    with database.get_connection(db_path) as connection:
        for row in connection.execute(
            "SELECT * FROM slot_search_profiles WHERE status='ACTIVE' AND enabled=1"
        ).fetchall():
            profiles[row["normalized_slot_type"]] = dict(row)
    satisfied = sum(row["completion_status"] == "COMPLETE" for row in slots)
    unavailable = sum(row["completion_status"] == "NOT_AVAILABLE" and row.get("analyst_acknowledged") for row in slots)
    confirmation = sum(row["completion_status"] in {"NEEDS_ANALYST_REVIEW", "PARTIAL"} for row in slots)
    requiring = [
        row for row in slots
        if row["completion_status"] not in {"COMPLETE", "NOT_AVAILABLE", "NOT_APPLICABLE"}
    ]
    max_requests = min(
        config.BRAVE_MAX_QUERIES_PER_PACKAGE,
        sum(int(profiles.get(row["normalized_slot_type"], {}).get("maximum_queries") or 0) for row in requiring),
    )
    return {
        "total_public_slots": len(slots),
        "slots_already_satisfied": satisfied,
        "slots_requiring_discovery": len(requiring),
        "slots_acknowledged_unavailable": unavailable,
        "slots_requiring_analyst_confirmation": confirmation,
        "estimated_maximum_brave_requests": max_requests,
        "slot_instance_ids": [row["package_slot_instance_id"] for row in requiring],
    }


def sync_public_slot_states(
    package_id: str,
    discovery_run_id: str | None,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Persist a terminal outcome for every active public-search profile."""
    database.initialize_database(db_path)
    public_types = _public_types(db_path=db_path)
    slots = [row for row in list_slot_instances(package_id, db_path=db_path) if row["normalized_slot_type"] in public_types]
    assignments = list_assignments(package_id, db_path=db_path)
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        for slot in slots:
            slot_id = slot["package_slot_instance_id"]
            approved = [
                row for row in assignments
                if row["package_slot_instance_id"] == slot_id
                and row["assignment_status"] == "APPROVED"
                and row.get("selected_for_package")
                and row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
            ]
            run = connection.execute(
                """SELECT * FROM slot_discovery_runs
                   WHERE discovery_run_id=? AND package_slot_instance_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (discovery_run_id, slot_id),
            ).fetchone() if discovery_run_id else None
            candidate = connection.execute(
                """SELECT COUNT(*) AS found,
                          SUM(CASE WHEN candidate_status IN ('REJECTED','NON_INVESTOR_MATERIAL','MIME_MISMATCH','COMPANY_MISMATCH','SOURCE_NOT_AUTHORITATIVE','UNSUPPORTED_FORMAT','FAILED') THEN 1 ELSE 0 END) AS rejected,
                          SUM(CASE WHEN candidate_status='NEEDS_ANALYST_REVIEW' THEN 1 ELSE 0 END) AS review,
                          COUNT(DISTINCT downloaded_document_id) AS downloaded
                   FROM discovered_candidates
                   WHERE package_id=? AND package_slot_instance_id=?
                     AND (? IS NULL OR discovery_run_id=?)""",
                (package_id, slot_id, discovery_run_id, discovery_run_id),
            ).fetchone()
            route = connection.execute(
                """SELECT selected_route FROM source_router_decisions
                   WHERE package_id=? AND package_slot_instance_id=?
                     AND (? IS NULL OR discovery_run_id=?)
                   ORDER BY created_at DESC LIMIT 1""",
                (package_id, slot_id, discovery_run_id, discovery_run_id),
            ).fetchone()
            count_rule = effective_document_counts(slot)
            count = len(approved)
            review_count = int(candidate["review"] or 0)
            found = int(candidate["found"] or 0)
            if slot["applicability_status"] == "NOT_APPLICABLE":
                state, reason, missing, action = "NOT_APPLICABLE", "The analyst marked this public item not applicable.", None, "No action required."
            elif slot["completion_status"] == "NOT_AVAILABLE" and slot.get("analyst_acknowledged"):
                state, reason, missing, action = "ACKNOWLEDGED_UNAVAILABLE", slot.get("analyst_notes") or "The analyst acknowledged that this material is unavailable.", slot.get("analyst_notes"), "Reopen the slot only if new information becomes available."
            elif count >= count_rule["minimum"]:
                state, reason, missing, action = "FILLED", f"{count} approved document(s) satisfy the configured minimum of {count_rule['minimum']}.", None, "Review or replace the approved assignment if needed."
            elif count:
                state, reason = "PARTIALLY_FILLED", f"{count} approved document(s) are present; {count_rule['minimum']} are required."
                missing, action = MISSING_REASONS.get(slot["normalized_slot_type"]), "Run discovery for the remaining document or acknowledge unavailability."
            elif run and run["status"] == "FAILED":
                state, reason = "FAILED", "The discovery task failed before a reliable terminal result was produced."
                missing, action = "Discovery failed safely; no assignment was created.", "Retry this failed public slot."
            elif review_count:
                state, reason = "CANDIDATES_REQUIRE_REVIEW", f"{review_count} candidate(s) require an explicit analyst decision."
                missing, action = MISSING_REASONS.get(slot["normalized_slot_type"]), "Review the pending candidates."
            elif run:
                state = "NO_CANDIDATE_FOUND"
                reason = MISSING_REASONS.get(slot["normalized_slot_type"], "No authoritative candidate was located.")
                missing, action = reason, "Refresh this slot or acknowledge that the material is unavailable."
            else:
                state, reason = "FAILED", "This public slot was not executed in the selected full-public run."
                missing, action = "The run did not create a terminal discovery task for this slot.", "Run or resume all missing public slots."
            if state not in TERMINAL_STATES:
                raise ValueError(f"Unsupported public-slot terminal state: {state}")
            state_id = _state_id(package_id, slot_id, discovery_run_id)
            values = (
                state_id, discovery_run_id, package_id, slot_id, slot["normalized_slot_type"], state,
                reason, missing, action, route["selected_route"] if route else (run["source_route"] if run else None),
                int(run["query_count"] or 0) if run else 0, found, int(candidate["rejected"] or 0),
                review_count, int(candidate["downloaded"] or 0), count, now, now,
            )
            connection.execute(
                """INSERT INTO public_slot_states(
                   public_slot_state_id, discovery_run_id, package_id, package_slot_instance_id,
                   normalized_slot_type, terminal_state, terminal_reason, missing_reason,
                   next_recommended_action, selected_route, queries_executed, candidates_found,
                   candidates_rejected, candidates_awaiting_review, documents_downloaded,
                   approved_document_count, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(package_id, package_slot_instance_id, discovery_run_id) DO UPDATE SET
                   terminal_state=excluded.terminal_state, terminal_reason=excluded.terminal_reason,
                   missing_reason=excluded.missing_reason, next_recommended_action=excluded.next_recommended_action,
                   selected_route=excluded.selected_route, queries_executed=excluded.queries_executed,
                   candidates_found=excluded.candidates_found, candidates_rejected=excluded.candidates_rejected,
                   candidates_awaiting_review=excluded.candidates_awaiting_review,
                   documents_downloaded=excluded.documents_downloaded,
                   approved_document_count=excluded.approved_document_count, updated_at=excluded.updated_at""",
                values,
            )
            if run:
                connection.execute(
                    """UPDATE slot_discovery_runs SET terminal_state=?, terminal_reason=?, missing_reason=?,
                       next_recommended_action=? WHERE slot_discovery_run_id=?""",
                    (state, reason, missing, action, run["slot_discovery_run_id"]),
                )
        if discovery_run_id:
            connection.execute(
                "UPDATE package_discovery_runs SET public_slots_total=? WHERE discovery_run_id=?",
                (len(slots), discovery_run_id),
            )
    return public_slot_diagnostics(package_id, discovery_run_id=discovery_run_id, db_path=db_path)["rows"]


def public_slot_diagnostics(
    package_id: str,
    *,
    discovery_run_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    started = perf_counter()
    database.initialize_database(db_path)
    if discovery_run_id is None:
        with database.get_connection(db_path) as connection:
            latest = connection.execute(
                "SELECT discovery_run_id FROM package_discovery_runs WHERE package_id=? ORDER BY started_at DESC LIMIT 1",
                (package_id,),
            ).fetchone()
        discovery_run_id = latest["discovery_run_id"] if latest else None
    if discovery_run_id:
        with database.get_connection(db_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM public_slot_states WHERE package_id=? AND discovery_run_id=?",
                (package_id, discovery_run_id),
            ).fetchone()[0]
        if not count:
            sync_public_slot_states(package_id, discovery_run_id, db_path=db_path)
    slots = {row["package_slot_instance_id"]: row for row in list_slot_instances(package_id, db_path=db_path)}
    with database.get_connection(db_path) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM public_slot_states WHERE package_id=? AND discovery_run_id IS ?
               ORDER BY package_slot_instance_id""",
            (package_id, discovery_run_id),
        ).fetchall()]
    for row in rows:
        slot = slots.get(row["package_slot_instance_id"], {})
        counts = effective_document_counts(slot) if slot else {"minimum": 0}
        row.update({
            "checklist_item": slot.get("display_name_snapshot") or row["normalized_slot_type"],
            "required": slot.get("requirement_snapshot") == "REQUIRED",
            "minimum_documents": counts["minimum"],
            "current_approved_count": row["approved_document_count"],
            "discovery_status": row["terminal_state"],
        })
    summary = {state: sum(row["terminal_state"] == state for row in rows) for state in TERMINAL_STATES}
    return {
        "discovery_run_id": discovery_run_id,
        "rows": rows,
        "summary": {
            "filled": summary["FILLED"], "partially_filled": summary["PARTIALLY_FILLED"],
            "awaiting_review": summary["CANDIDATES_REQUIRE_REVIEW"],
            "no_candidate_found": summary["NO_CANDIDATE_FOUND"], "failed": summary["FAILED"],
            "acknowledged_unavailable": summary["ACKNOWLEDGED_UNAVAILABLE"],
            "not_applicable": summary["NOT_APPLICABLE"],
        },
        "load_ms": round((perf_counter() - started) * 1000, 1),
    }
