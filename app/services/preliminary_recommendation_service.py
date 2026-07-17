from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from app import config
from app.services.analysis_snapshot_service import create_analysis_snapshot, get_analysis_snapshot
from app.services.package_assembly_service import package_snapshot, public_package_summary, selected_document_ids
from app.services.package_artifact_service import register_preliminary_report_artifact
from app.services.package_recipe_service import list_assignments, list_slot_instances
from app.services.research_workflow_service import run_research_workflow
from app.utils import database


REPORT_STATUSES = {
    "NOT_READY", "PRELIMINARY_READY", "PRELIMINARY_GENERATED", "FINAL_READY_LATER_PHASE", "FAILED",
}


def preliminary_report_gate(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    slots = {row["package_slot_instance_id"]: row for row in list_slot_instances(package_id, db_path=db_path)}
    approved = [
        row for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED"
        and row.get("selected_for_package")
        and row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
        and row.get("sha256_hash")
    ]
    selected_types = {slots[row["package_slot_instance_id"]]["normalized_slot_type"] for row in approved}
    has_filing = "most_recent_10_q_and_10_k" in selected_types
    has_earnings = bool(selected_types & {
        "latest_earnings_release", "available_supplemental_or_earnings_presentation",
        "latest_earnings_call_transcript",
    })
    errors = []
    if not has_filing:
        errors.append("Select at least the latest 10-K or 10-Q.")
    if not has_earnings:
        errors.append("Select at least one earnings or financial-results document.")
    if any(not row.get("local_path") or not Path(row["local_path"]).is_file() for row in approved):
        errors.append("Every selected document must pass managed-file integrity checks.")
    if len(approved) < 2:
        errors.append("At least two approved package documents are required.")
    completion = public_package_summary(package_id, db_path=db_path)
    return {
        "status": "PRELIMINARY_READY" if not errors else "NOT_READY",
        "ready": not errors,
        "errors": errors,
        "selected_document_ids": sorted({row["document_id"] for row in approved}),
        "selected_evidence_count": len(approved),
        "package_incomplete": completion["public_package"]["missing"] > 0 or completion["manual_package"]["missing"] > 0,
    }


def latest_preliminary_report(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM preliminary_package_reports WHERE package_id=? ORDER BY created_at DESC LIMIT 1",
            (package_id,),
        ).fetchone()
    return dict(row) if row else None


def generate_preliminary_recommendation(
    package_id: str,
    *,
    actor: str,
    retry: bool = False,
    refresh_snapshot: bool = False,
    analysis_snapshot_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    gate = preliminary_report_gate(package_id, db_path=db_path)
    if not gate["ready"]:
        raise ValueError("Preliminary recommendation is not ready: " + " ".join(gate["errors"]))
    previous_report = latest_preliminary_report(package_id, db_path=db_path)
    if retry and (not previous_report or not previous_report.get("analysis_run_id")):
        with database.get_connection(db_path) as connection:
            row = connection.execute(
                """SELECT * FROM preliminary_package_reports
                   WHERE package_id=? AND analysis_run_id IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (package_id,),
            ).fetchone()
        previous_report = dict(row) if row else previous_report
    analysis_snapshot = None
    if analysis_snapshot_id:
        analysis_snapshot = get_analysis_snapshot(analysis_snapshot_id, db_path=db_path)
        if not analysis_snapshot or analysis_snapshot.get("package_id") != package_id:
            raise ValueError("The selected analysis snapshot does not belong to this package.")
    if retry and not refresh_snapshot and previous_report and previous_report.get("analysis_snapshot_id"):
        analysis_snapshot = get_analysis_snapshot(previous_report["analysis_snapshot_id"], db_path=db_path)
    if not analysis_snapshot:
        analysis_snapshot = create_analysis_snapshot(package_id, db_path=db_path)
    snapshot = package_snapshot(package_id, db_path=db_path)
    snapshot["analysis_snapshot_id"] = analysis_snapshot["snapshot_id"]
    snapshot["analysis_snapshot_hash"] = analysis_snapshot["snapshot_hash"]
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    report_id = f"PRPT-{secrets.token_hex(8).upper()}"
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        if not retry:
            existing = connection.execute(
                """SELECT * FROM preliminary_package_reports
                   WHERE package_id=? AND status='PRELIMINARY_GENERATED'
                     AND json_extract(package_snapshot_json, '$.fingerprint')=?
                   ORDER BY created_at DESC LIMIT 1""",
                (package_id, fingerprint),
            ).fetchone()
            if existing:
                return dict(existing)
        snapshot["fingerprint"] = fingerprint
        connection.execute(
            """INSERT INTO preliminary_package_reports(
                preliminary_report_id, package_id, status, package_snapshot_json,
                selected_document_ids_json, workflow_run_id, analysis_run_id,
                requested_by, requested_at, created_at, updated_at, analysis_snapshot_id
            ) VALUES (?, ?, 'PRELIMINARY_READY', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report_id, package_id, json.dumps(snapshot, sort_keys=True, default=str),
             json.dumps(gate["selected_document_ids"]),
             (previous_report or {}).get("workflow_run_id") if retry else None,
             (previous_report or {}).get("analysis_run_id") if retry else None,
             actor, now, now, now, analysis_snapshot["snapshot_id"]),
        )
    try:
        if retry and previous_report and previous_report.get("analysis_run_id"):
            from app.services.reporting.investment_report import generate_investment_report

            generated = generate_investment_report(
                previous_report["analysis_run_id"], final=False,
                preliminary_package_view=True, db_path=db_path,
            )
            workflow = {
                "workflow_run_id": previous_report.get("workflow_run_id"),
                "analysis_run_id": previous_report["analysis_run_id"],
                "report_id": generated["report_id"],
            }
        else:
            workflow = run_research_workflow(
                package_id,
                idempotency_key=f"PHASE6B1-PRELIM-{fingerprint}",
                retry_failed=retry,
                preliminary_package_view=True,
                analysis_snapshot_id=analysis_snapshot["snapshot_id"],
                db_path=db_path,
            )
            generated = next(
                (row for row in database.list_generated_reports(workflow.get("analysis_run_id"), db_path=db_path)
                 if row.get("report_id") == workflow.get("report_id")),
                None,
            ) if workflow.get("analysis_run_id") else None
        decision = database.get_recommendation_decision(workflow.get("analysis_run_id"), db_path=db_path) if workflow.get("analysis_run_id") else None
        quality = database.latest_memo_quality_audit(workflow.get("analysis_run_id"), db_path=db_path) if workflow.get("analysis_run_id") else None
        with database.get_connection(db_path) as connection:
            repairs = [dict(row) for row in connection.execute(
                "SELECT * FROM report_repair_audits WHERE analysis_run_id=? ORDER BY repair_number",
                (workflow.get("analysis_run_id"),),
            ).fetchall()] if workflow.get("analysis_run_id") else []
        status = "PRELIMINARY_GENERATED" if generated else "FAILED"
        recommendation = str((decision or {}).get("effective_rating") or "ANALYST_REVIEW_REQUIRED").upper()
        if gate["package_incomplete"]:
            recommendation = "ANALYST_REVIEW_REQUIRED"
        with database.get_connection(db_path) as connection:
            connection.execute(
                """UPDATE preliminary_package_reports SET status=?, workflow_run_id=?, analysis_run_id=?,
                   generated_report_id=?, recommendation=?, confidence=?, quality_result_json=?,
                   repair_result_json=?, safe_error_message=?, completed_at=?, updated_at=? WHERE preliminary_report_id=?""",
                (status, workflow.get("workflow_run_id"), workflow.get("analysis_run_id"), workflow.get("report_id"),
                 recommendation, (decision or {}).get("confidence"), json.dumps(quality or {}, sort_keys=True),
                 json.dumps(repairs, sort_keys=True),
                 None if generated else "The existing analysis workflow did not produce a releasable one-page memo.",
                 database.utc_now_iso(), database.utc_now_iso(), report_id),
            )
        if generated:
            register_preliminary_report_artifact(package_id, generated, db_path=db_path)
    except Exception as exc:
        with database.get_connection(db_path) as connection:
            connection.execute(
                """UPDATE preliminary_package_reports SET status='FAILED', safe_error_message=?,
                   completed_at=?, updated_at=? WHERE preliminary_report_id=?""",
                (f"{type(exc).__name__}: preliminary recommendation failed safely.", database.utc_now_iso(), database.utc_now_iso(), report_id),
            )
    return latest_preliminary_report(package_id, db_path=db_path) or {}


def preliminary_report_files(report: dict[str, Any], *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    generated_id = report.get("generated_report_id")
    analysis_id = report.get("analysis_run_id")
    generated = next(
        (row for row in database.list_generated_reports(analysis_id, db_path=db_path) if row["report_id"] == generated_id),
        None,
    ) if generated_id and analysis_id else None
    return generated or {}
