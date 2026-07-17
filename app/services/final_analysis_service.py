from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import fitz

from app import config
from app.services.finalization_service import evaluate_readiness, get_finalization_run
from app.services.package_artifact_service import list_package_artifacts
from app.services.preliminary_recommendation_service import latest_preliminary_report
from app.services.recommendation_engine import complete_analyst_review, pm_decision
from app.services.reporting.investment_report import generate_investment_report
from app.utils import database


ALLOWED_RATINGS = {"BUY", "HOLD", "SELL", "ANALYST_REVIEW_REQUIRED"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


def _loads(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return sorted(str(item) for item in parsed) if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _snapshot_payload(run: dict[str, Any], *, db_path: Path | str) -> dict[str, Any]:
    package_id = run["package_id"]
    artifacts = [row for row in list_package_artifacts(package_id, db_path=db_path) if row.get("analysis_eligible")]
    with database.get_connection(db_path) as connection:
        assignments = connection.execute(
            """SELECT assignment_id, document_id FROM slot_document_assignments
               WHERE package_id=? AND assignment_status='APPROVED' AND selected_for_package=1 ORDER BY assignment_id""",
            (package_id,),
        ).fetchall()
        base = connection.execute(
            """SELECT * FROM analysis_corpus_snapshots WHERE package_id=? AND status='READY'
               ORDER BY finalized_at DESC LIMIT 1""", (package_id,)
        ).fetchone()
        facts = connection.execute(
            "SELECT financial_fact_id FROM normalized_financial_facts WHERE package_version_id=? AND selected=1 ORDER BY financial_fact_id",
            (run["package_version_id"],),
        ).fetchall()
        fact_conflicts = connection.execute(
            "SELECT fact_conflict_id FROM financial_fact_conflicts WHERE package_version_id=? ORDER BY fact_conflict_id",
            (run["package_version_id"],),
        ).fetchall()
        waivers = connection.execute(
            """SELECT waiver_id FROM analyst_waivers
               WHERE package_version_id=? AND status='ACTIVE' AND confirmation_status='CONFIRMED'
               ORDER BY waiver_id""",
            (run["package_version_id"],),
        ).fetchall()
    if not base:
        raise ValueError("A finalized closed-corpus analysis snapshot is required.")
    return {
        "package_id": package_id,
        "package_version_id": run["package_version_id"],
        "base_analysis_snapshot_id": base["snapshot_id"],
        "assignment_ids": [row["assignment_id"] for row in assignments],
        "artifact_ids": sorted(row["artifact_id"] for row in artifacts),
        "document_ids": sorted({row["document_id"] for row in assignments}),
        "evidence_ids": _loads(base["evidence_ids_json"]),
        "fact_ids": [row[0] for row in facts],
        "metric_ids": _loads(base["metric_ids_json"]),
        "conflict_ids": sorted(_loads(base["conflict_ids_json"]) + [row[0] for row in fact_conflicts]),
        "waiver_ids": [row[0] for row in waivers],
        "configuration": {
            "processing_pipeline_version": config.PROCESSING_PIPELINE_VERSION,
            "parser_config_version": config.PARSER_CONFIG_VERSION,
            "analysis_configuration_version": config.ANALYSIS_CONFIGURATION_VERSION,
            "report_template_version": config.REPORT_TEMPLATE_VERSION,
            "reader_renderer_version": config.SEC_READER_RENDERER_VERSION,
            "section_extraction_version": config.SECTION_EXTRACTION_VERSION,
            "company_facts_version": config.COMPANY_FACTS_VERSION,
        },
    }


def create_final_snapshot(run_id: str, *, actor: str, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = get_finalization_run(run_id, db_path=db_path)
    if not run:
        raise ValueError("Finalization run does not exist.")
    readiness = evaluate_readiness(run["package_id"], package_version_id=run["package_version_id"], db_path=db_path)
    if not readiness.ready:
        raise ValueError("Final analysis snapshot is blocked: " + " ".join(readiness.blockers))
    from app.services.sec_document_production_service import assign_source_roles

    assign_source_roles(run["package_id"], db_path=db_path)
    payload = _snapshot_payload(run, db_path=db_path)
    snapshot_hash = _hash(payload)
    with database.get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM final_analysis_snapshots WHERE snapshot_hash=?", (snapshot_hash,)
        ).fetchone()
        if existing:
            return dict(existing)
        snapshot_id = _id("FSNAP")
        now = database.utc_now_iso()
        connection.execute(
            """INSERT INTO final_analysis_snapshots VALUES (
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?
               )""",
            (snapshot_id, run["package_id"], run["package_version_id"],
             _canonical(payload["artifact_ids"]), _canonical(payload["document_ids"]),
             _canonical(payload["evidence_ids"]), _canonical(payload["fact_ids"]),
             _canonical(payload["metric_ids"]), _canonical(payload["conflict_ids"]),
             _canonical(payload["waiver_ids"]), _canonical(payload["configuration"]), snapshot_hash,
             actor, now, now),
        )
        row = connection.execute("SELECT * FROM final_analysis_snapshots WHERE final_snapshot_id=?", (snapshot_id,)).fetchone()
    return dict(row)


def validate_final_snapshot(snapshot_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    with database.get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM final_analysis_snapshots WHERE final_snapshot_id=?", (snapshot_id,)).fetchone()
        if not row:
            raise ValueError("Final snapshot does not exist.")
        run = connection.execute(
            "SELECT * FROM finalization_runs WHERE package_version_id=?", (row["package_version_id"],)
        ).fetchone()
    current = _snapshot_payload(dict(run), db_path=db_path)
    if _hash(current) != row["snapshot_hash"]:
        raise ValueError("Final snapshot inputs changed after snapshot creation.")
    return {"status": "PASSED", "snapshot_hash": row["snapshot_hash"]}


def prepare_final_recommendation(run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = get_finalization_run(run_id, db_path=db_path)
    if not run:
        raise ValueError("Finalization run does not exist.")
    with database.get_connection(db_path) as connection:
        snapshot = connection.execute(
            "SELECT * FROM final_analysis_snapshots WHERE package_version_id=? AND status='READY' ORDER BY created_at DESC LIMIT 1",
            (run["package_version_id"],),
        ).fetchone()
    if not snapshot:
        raise ValueError("Create the final analysis snapshot first.")
    validate_final_snapshot(snapshot["final_snapshot_id"], db_path=db_path)
    preliminary = latest_preliminary_report(run["package_id"], db_path=db_path)
    if not preliminary or not preliminary.get("analysis_run_id"):
        raise ValueError("A completed closed-corpus analysis run is required.")
    decision = database.get_recommendation_decision(preliminary["analysis_run_id"], db_path=db_path) or {}
    ai_rating = str(decision.get("preliminary_rating") or decision.get("effective_rating") or "ANALYST_REVIEW_REQUIRED").upper()
    if ai_rating not in ALLOWED_RATINGS:
        ai_rating = "ANALYST_REVIEW_REQUIRED"
    with database.get_connection(db_path) as connection:
        valuation = connection.execute(
            """SELECT 1 FROM package_artifacts WHERE package_id=? AND artifact_status='CURRENT'
               AND analysis_eligible=1 AND source_role='VALUATION_MODEL' LIMIT 1""", (run["package_id"],)
        ).fetchone()
        if not valuation and ai_rating != "ANALYST_REVIEW_REQUIRED":
            ai_rating = "ANALYST_REVIEW_REQUIRED"
        approval_id = _id("FREC")
        now = database.utc_now_iso()
        connection.execute(
            """INSERT INTO final_recommendation_approvals(
               approval_id, package_id, package_version_id, final_snapshot_id, analysis_run_id,
               ai_recommendation, ai_confidence, status, qa_status, qa_result_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'AWAITING_ANALYST_APPROVAL', 'PENDING', '{}', ?)
               ON CONFLICT(package_version_id, final_snapshot_id) DO NOTHING""",
            (approval_id, run["package_id"], run["package_version_id"], snapshot["final_snapshot_id"],
             preliminary["analysis_run_id"], ai_rating, preliminary.get("confidence"), now),
        )
        row = connection.execute(
            "SELECT * FROM final_recommendation_approvals WHERE package_version_id=? AND final_snapshot_id=?",
            (run["package_version_id"], snapshot["final_snapshot_id"]),
        ).fetchone()
    return dict(row)


def approve_final_recommendation(
    approval_id: str,
    *,
    analyst_rating: str,
    reason: str,
    actor: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    analyst_rating = analyst_rating.upper()
    if analyst_rating not in ALLOWED_RATINGS:
        raise ValueError("Unsupported final rating.")
    with database.get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM final_recommendation_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if not row:
        raise ValueError("Final recommendation approval does not exist.")
    if row["status"] == "APPROVED":
        return dict(row)
    if analyst_rating != row["ai_recommendation"] and not reason.strip():
        raise ValueError("An analyst override reason is required.")
    analysis_run_id = row["analysis_run_id"]
    complete_analyst_review(analysis_run_id, decision=analyst_rating, note=reason,
                            analyst_identity=actor, db_path=str(db_path))
    pm_decision(analysis_run_id, action="APPROVE", note=reason or "Approved for final package.",
                pm_identity=actor, db_path=str(db_path))
    report = generate_investment_report(analysis_run_id, final=True, db_path=db_path)
    with fitz.open(report["pdf_path"]) as document:
        page_count = document.page_count
    qa = {
        "exactly_one_page": page_count == 1,
        "memo_quality_status": report.get("memo_quality_status"),
        "citation_audit_status": report.get("citation_audit_status"),
        "passed": page_count == 1 and report.get("memo_quality_status") == "PASSED" and report.get("citation_audit_status") == "PASSED",
    }
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE package_artifacts SET artifact_status='SUPERSEDED', working_package_inclusion=0,
               analysis_eligible=0, superseded_at=? WHERE package_id=?
               AND artifact_type='PRELIMINARY_RECOMMENDATION' AND artifact_status='CURRENT'""",
            (now, row["package_id"]),
        )
        if analyst_rating != row["ai_recommendation"]:
            connection.execute(
                "INSERT INTO analyst_rating_overrides VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_id("ROVR"), approval_id, row["ai_recommendation"], analyst_rating, reason.strip(), actor, now),
            )
        connection.execute(
            """UPDATE final_recommendation_approvals SET report_id=?, analyst_recommendation=?,
               status=?, qa_status=?, qa_result_json=?, approved_by=?, approved_at=? WHERE approval_id=?""",
            (report["report_id"], analyst_rating, "APPROVED" if qa["passed"] else "QA_FAILED",
             "PASSED" if qa["passed"] else "FAILED", _canonical(qa), actor, now, approval_id),
        )
        artifact_id = "ART-" + hashlib.sha256(f"{row['package_id']}|FINAL_RECOMMENDATION|{report['report_id']}".encode()).hexdigest()[:20].upper()
        path = Path(report["pdf_path"])
        connection.execute(
            """INSERT INTO package_artifacts(
               artifact_id, package_id, artifact_type, display_filename, purpose_label,
               working_package_inclusion, audit_package_inclusion, analysis_eligible,
               conversion_status, artifact_status, created_at, generated_path, generated_sha256,
               generated_size_bytes, page_count, qa_status, qa_result_json, source_role, package_version_id
               ) VALUES (?, ?, 'FINAL_RECOMMENDATION', ?, 'Final AI Recommendation', 1, 1, 0,
               'FINAL_REPORT_READY', 'CURRENT', ?, ?, ?, ?, ?, ?, ?, 'ANALYST_NOTE', ?)""",
            (artifact_id, row["package_id"], f"{database.get_package_by_package_id(row['package_id'], db_path=db_path)['ticker']} Final AI Recommendation.pdf",
             now, str(path), report["pdf_sha256"], path.stat().st_size, page_count,
             "PASSED" if qa["passed"] else "FAILED", _canonical(qa), row["package_version_id"]),
        )
        refreshed = connection.execute("SELECT * FROM final_recommendation_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    return dict(refreshed)
