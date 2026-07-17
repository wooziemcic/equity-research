from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.services.package_recipe_service import board_payload
from app.utils import database


STAGES = (
    "READINESS_REVIEW",
    "DOCUMENT_RENDERING",
    "SECTION_EXTRACTION",
    "COMPANY_FACTS_BUILD",
    "CORPUS_PROCESSING",
    "FINAL_ANALYSIS_SNAPSHOT",
    "FINAL_RECOMMENDATION",
    "CHECKLIST_FINALIZATION",
    "WORKING_PACKAGE_BUILD",
    "AUDIT_PACKAGE_BUILD",
    "FINAL_QA",
    "READY_TO_LOCK",
    "LOCKED",
    "DELIVERED",
)

FAILURE_STATES = {
    "DOCUMENT_RENDERING": "DOCUMENT_RENDERING_FAILED",
    "SECTION_EXTRACTION": "SECTION_EXTRACTION_FAILED",
    "COMPANY_FACTS_BUILD": "COMPANY_FACTS_FAILED",
    "CORPUS_PROCESSING": "CORPUS_PROCESSING_FAILED",
    "FINAL_ANALYSIS_SNAPSHOT": "SNAPSHOT_FAILED",
    "FINAL_RECOMMENDATION": "RECOMMENDATION_FAILED",
    "WORKING_PACKAGE_BUILD": "PACKAGE_BUILD_FAILED",
    "AUDIT_PACKAGE_BUILD": "AUDIT_BUILD_FAILED",
    "FINAL_QA": "FINAL_QA_FAILED",
    "LOCKED": "LOCK_FAILED",
}


@dataclass(frozen=True)
class FinalReadiness:
    ready: bool
    slots: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    fingerprint: str


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def is_final_locked(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> bool:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        return connection.execute(
            "SELECT 1 FROM final_package_locks WHERE package_id=?", (package_id,)
        ).fetchone() is not None


def assert_not_final_locked(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> None:
    if is_final_locked(package_id, db_path=db_path):
        raise ValueError("Final package is locked. Create a new package version to make changes.")


def _waiver_slots(package_version_id: str, db_path: Path | str, *, confirmed: bool) -> set[str]:
    status_clause = "confirmation_status='CONFIRMED'" if confirmed else "confirmation_status!='CONFIRMED'"
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            f"SELECT slot_instance_id FROM analyst_waivers WHERE package_version_id=? AND status='ACTIVE' AND {status_clause}",
            (package_version_id,),
        ).fetchall()
    return {row[0] for row in rows}


def evaluate_readiness(
    package_id: str,
    *,
    package_version_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> FinalReadiness:
    database.initialize_database(db_path)
    payload = board_payload(package_id, db_path=db_path)
    if payload.get("legacy"):
        return FinalReadiness(False, (), ("A Phase 6 recipe instance is required.",), (), "")
    slots = payload["slots"]
    assignments = payload["assignments"]
    approved_by_slot: dict[str, list[dict[str, Any]]] = {}
    selected = []
    for assignment in assignments:
        if assignment["assignment_status"] == "APPROVED" and int(assignment["selected_for_package"] or 0):
            approved_by_slot.setdefault(assignment["package_slot_instance_id"], []).append(assignment)
            selected.append(assignment)
    waived = _waiver_slots(package_version_id, db_path, confirmed=True) if package_version_id else set()
    pending_waivers = _waiver_slots(package_version_id, db_path, confirmed=False) if package_version_id else set()
    classified: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for slot in slots:
        slot_id = slot["package_slot_instance_id"]
        approved_count = len(approved_by_slot.get(slot_id, []))
        required = str(slot.get("requirement_snapshot") or "OPTIONAL").upper() == "REQUIRED"
        state = "FILLED" if approved_count else "MISSING_REQUIRED" if required else "MISSING_OPTIONAL"
        if slot.get("applicability_status") == "NOT_APPLICABLE":
            state = "NOT_APPLICABLE"
        elif slot.get("completion_status") == "NOT_AVAILABLE" and slot_id in waived:
            state = "ACKNOWLEDGED_UNAVAILABLE"
        elif slot.get("completion_status") == "NOT_AVAILABLE" and (int(slot.get("analyst_acknowledged") or 0) or slot_id in pending_waivers):
            state = "WAIVER_CONFIRMATION_REQUIRED"
        elif approved_count and str(slot.get("completion_status")) not in {"COMPLETE", "FILLED"}:
            state = "PARTIALLY_FILLED"
        if required and state not in {"FILLED", "ACKNOWLEDGED_UNAVAILABLE", "NOT_APPLICABLE"}:
            blockers.append(f"Required slot is unresolved: {slot['display_name_snapshot']}")
        elif not required and state == "MISSING_OPTIONAL":
            warnings.append(f"Optional material is missing: {slot['display_name_snapshot']}")
        classified.append({
            "slot_instance_id": slot_id,
            "display_name": slot["display_name_snapshot"],
            "required": required,
            "status": state,
            "approved_document_count": approved_count,
            "waiver_confirmed": slot_id in waived,
            "waiver_pending": slot_id in pending_waivers,
        })

    with database.get_connection(db_path) as connection:
        awaiting = connection.execute(
            """SELECT COUNT(*) FROM discovered_candidates dc
               JOIN slot_discovery_runs sdr ON sdr.slot_discovery_run_id=dc.slot_discovery_run_id
               WHERE sdr.package_id=? AND dc.candidate_status IN ('AWAITING_REVIEW', 'REVIEW_REQUIRED')""",
            (package_id,),
        ).fetchone()[0]
        duplicate_count = connection.execute(
            """SELECT COUNT(*) FROM (
                 SELECT package_slot_instance_id, document_id, COUNT(*) count FROM slot_document_assignments
                 WHERE package_id=? AND assignment_status='APPROVED' AND selected_for_package=1
                 GROUP BY package_slot_instance_id, document_id HAVING COUNT(*) > 1
               )""",
            (package_id,),
        ).fetchone()[0]
        docs = connection.execute(
            """SELECT d.* FROM documents d JOIN slot_document_assignments a ON a.document_id=d.document_id
               WHERE a.package_id=? AND a.assignment_status='APPROVED' AND a.selected_for_package=1""",
            (package_id,),
        ).fetchall()
        anchor = connection.execute(
            """SELECT * FROM earnings_cycle_anchors WHERE package_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (package_id,),
        ).fetchone()
    if awaiting:
        blockers.append(f"{awaiting} discovered candidate(s) still await review.")
    if duplicate_count:
        blockers.append("Duplicate approved document assignments exist.")
    for row in docs:
        path = Path(row["local_path"] or "")
        if not path.is_file():
            blockers.append(f"Selected document is missing: {row['original_filename']}")
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if row["sha256_hash"] and actual_hash != row["sha256_hash"]:
            blockers.append(f"Selected document failed its integrity check: {row['original_filename']}")
        if int(row["audit_package_inclusion"] or 0) and not int(row["working_package_inclusion"] or 0):
            blockers.append(f"Audit-only document is selected: {row['original_filename']}")
    package = payload["package"]
    if not package.get("research_cutoff_date"):
        blockers.append("Research cutoff must be confirmed.")
    if not anchor or anchor["validation_status"] not in {"VALID", "VALIDATED", "CONFIRMED", "APPROVED"}:
        blockers.append("Earnings cycle must be confirmed.")
    source = {
        "package": {k: package.get(k) for k in ("package_id", "research_cutoff_date")},
        "earnings_anchor": dict(anchor) if anchor else None,
        "slots": classified,
        "assignments": sorted((a["assignment_id"], a["document_id"]) for a in selected),
        "documents": sorted((row["document_id"], row["sha256_hash"]) for row in docs),
        "waivers": sorted(waived),
    }
    return FinalReadiness(not blockers, tuple(classified), tuple(dict.fromkeys(blockers)), tuple(warnings), _fingerprint(source))


def start_finalization(
    package_id: str,
    *,
    actor: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    database.initialize_database(db_path)
    assert_not_final_locked(package_id, db_path=db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        raise ValueError("Package does not exist.")
    with database.get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM finalization_runs WHERE package_id=? ORDER BY created_at DESC LIMIT 1", (package_id,)
        ).fetchone()
    if existing:
        return dict(existing)
    version = database.allocate_package_version({
        "parent_package_id": package_id,
        "ticker": package["ticker"],
        "company_name": package.get("company_name"),
        "security_type": package["security_type"],
        "research_cutoff_date": package["research_cutoff_date"],
        "status": "FINALIZING",
        "created_by": actor,
        "notes": "Phase 6C finalization version",
    }, db_path=db_path)
    readiness = evaluate_readiness(package_id, package_version_id=version["version_id"], db_path=db_path)
    run_id = _id("FIN")
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO finalization_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (run_id, package_id, version["version_id"], "DRAFT", "READINESS_REVIEW", readiness.fingerprint, actor, now, now),
        )
        for stage in STAGES:
            connection.execute(
                """INSERT INTO finalization_stage_status(
                   stage_status_id, finalization_run_id, stage_name, status, result_json, attempt_count
                   ) VALUES (?, ?, ?, 'PENDING', '{}', 0)""",
                (_id("STG"), run_id, stage),
            )
    return get_finalization_run(run_id, db_path=db_path) or {}


def get_finalization_run(run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM finalization_runs WHERE finalization_run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_finalization(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM finalization_runs WHERE package_id=? ORDER BY created_at DESC LIMIT 1", (package_id,)
        ).fetchone()
    return dict(row) if row else None


def list_stage_statuses(run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM finalization_stage_status WHERE finalization_run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def record_stage(
    run_id: str,
    stage: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    input_fingerprint: str | None = None,
    output_fingerprint: str | None = None,
    error_message: str | None = None,
    duration_ms: float | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> None:
    if stage not in STAGES:
        raise ValueError("Unknown finalization stage.")
    now = database.utc_now_iso()
    persisted_status = FAILURE_STATES.get(stage, f"{stage}_FAILED") if status == "FAILED" else status
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE finalization_stage_status SET status=?, input_fingerprint=?, output_fingerprint=?,
               result_json=?, error_message=?, started_at=COALESCE(started_at, ?), completed_at=?,
               duration_ms=?, attempt_count=attempt_count+1
               WHERE finalization_run_id=? AND stage_name=?""",
            (persisted_status, input_fingerprint, output_fingerprint, _canonical(result or {}), error_message, now,
             now if status in {"COMPLETED", "FAILED"} else None, duration_ms, run_id, stage),
        )
        connection.execute(
            "UPDATE finalization_runs SET current_stage=?, status=?, updated_at=? WHERE finalization_run_id=?",
            (stage, persisted_status, now, run_id),
        )


def create_waiver(
    run_id: str,
    slot_instance_id: str,
    *,
    reason: str,
    actor: str,
    confirmed: bool = True,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("A waiver reason is required.")
    run = get_finalization_run(run_id, db_path=db_path)
    if not run:
        raise ValueError("Finalization run does not exist.")
    assert_not_final_locked(run["package_id"], db_path=db_path)
    waiver_id = _id("WVR")
    now = database.utc_now_iso()
    confirmation_status = "CONFIRMED" if confirmed else "PENDING"
    with database.get_connection(db_path) as connection:
        slot = connection.execute(
            "SELECT * FROM package_slot_instances WHERE package_slot_instance_id=? AND package_id=?",
            (slot_instance_id, run["package_id"]),
        ).fetchone()
        if not slot:
            raise ValueError("Slot does not belong to this package.")
        connection.execute(
            """INSERT INTO analyst_waivers(
               waiver_id, package_id, package_version_id, slot_instance_id, reason, created_by, created_at, status
               , confirmation_status, confirmed_by, confirmed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)""",
            (waiver_id, run["package_id"], run["package_version_id"], slot_instance_id, reason.strip(), actor, now,
             confirmation_status, actor if confirmed else None, now if confirmed else None),
        )
        connection.execute(
            """INSERT INTO phase6a_audit_events(
               event_id, event_type, actor, package_id, package_slot_instance_id, event_details_json, created_at
               ) VALUES (?, 'FINALIZATION_WAIVER_CREATED', ?, ?, ?, ?, ?)""",
            (_id("AUD"), actor, run["package_id"], slot_instance_id,
             _canonical({"waiver_id": waiver_id, "package_version_id": run["package_version_id"], "reason": reason.strip()}), now),
        )
        row = connection.execute("SELECT * FROM analyst_waivers WHERE waiver_id=?", (waiver_id,)).fetchone()
    return dict(row)


def confirm_waiver(
    waiver_id: str,
    *,
    actor: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    database.initialize_database(db_path)
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        waiver = connection.execute("SELECT * FROM analyst_waivers WHERE waiver_id=?", (waiver_id,)).fetchone()
        if not waiver:
            raise ValueError("Waiver does not exist.")
        assert_not_final_locked(waiver["package_id"], db_path=db_path)
        connection.execute(
            """UPDATE analyst_waivers SET confirmation_status='CONFIRMED', confirmed_by=?, confirmed_at=?
               WHERE waiver_id=?""",
            (actor, now, waiver_id),
        )
        connection.execute(
            """INSERT INTO phase6a_audit_events(
               event_id, event_type, actor, package_id, package_slot_instance_id, event_details_json, created_at
               ) VALUES (?, 'FINALIZATION_WAIVER_CONFIRMED', ?, ?, ?, ?, ?)""",
            (_id("AUD"), actor, waiver["package_id"], waiver["slot_instance_id"],
             _canonical({"waiver_id": waiver_id, "package_version_id": waiver["package_version_id"]}), now),
        )
        row = connection.execute("SELECT * FROM analyst_waivers WHERE waiver_id=?", (waiver_id,)).fetchone()
    return dict(row)


def sqlite_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.IntegrityError) and "Final package is locked" in str(exc)
