from __future__ import annotations

import json
import logging
from pathlib import Path

import streamlit as st

from app import config
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.company_facts_service import build_company_facts, list_selected_facts
from app.services.final_analysis_service import (
    approve_final_recommendation,
    create_final_snapshot,
    prepare_final_recommendation,
)
from app.services.final_delivery_service import (
    build_audit_zip,
    build_final_checklist,
    build_working_zip,
    create_new_package_version,
    lock_final_package,
    mark_delivered,
    run_final_qa,
)
from app.services.finalization_service import (
    confirm_waiver,
    create_waiver,
    evaluate_readiness,
    latest_finalization,
    list_stage_statuses,
    record_stage,
    start_finalization,
)
from app.services.package_recipe_service import list_slot_instances, mark_slot
from app.services.preliminary_recommendation_service import generate_preliminary_recommendation, latest_preliminary_report
from app.services.sec_document_production_service import (
    assign_source_roles,
    extract_section_pdfs,
    render_sec_reader_pdfs,
)
from app.utils import database


logger = logging.getLogger(__name__)


def _actor() -> str:
    return st.session_state.get("finalization_actor", "").strip() or "analyst"


def _package() -> dict | None:
    package_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    package = database.get_package_by_package_id(package_id) if package_id else None
    if package:
        st.session_state["active_package"] = package
        return package
    packages = database.list_packages(limit=100)
    if not packages:
        st.info("No package is available for finalization.")
        return None
    labels = {f"{row['ticker']} - {row.get('company_name') or row['package_id']} - {row['package_id']}": row for row in packages}
    selected = labels[st.selectbox("Package", list(labels))]
    if st.button("Open Package", type="primary"):
        st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = selected["package_id"]
        st.session_state["active_package"] = selected
        st.rerun()
    return None


def _stage_result(action, run: dict, stage: str, success_message: str):
    try:
        with st.spinner(success_message):
            result = action()
        record_stage(run["finalization_run_id"], stage, status="COMPLETED", result=result if isinstance(result, dict) else {"count": len(result)})
        st.success(success_message)
        return result
    except Exception as exc:
        logger.exception("Phase 6C stage failed: %s", stage)
        record_stage(run["finalization_run_id"], stage, status="FAILED", error_message=f"{type(exc).__name__}: {exc}")
        st.error(str(exc))
        return None


def _readiness(package: dict, run: dict) -> None:
    readiness = evaluate_readiness(package["package_id"], package_version_id=run["package_version_id"])
    cols = st.columns(3)
    cols[0].metric("Required gate", "Ready" if readiness.ready else "Blocked")
    cols[1].metric("Slots", len(readiness.slots))
    cols[2].metric("Blockers", len(readiness.blockers))
    st.dataframe(list(readiness.slots), hide_index=True, use_container_width=True)
    if readiness.blockers:
        for blocker in readiness.blockers:
            st.warning(blocker)
    for warning in readiness.warnings:
        st.caption(warning)
    missing = [row for row in readiness.slots if row["status"] == "MISSING_REQUIRED"]
    if missing:
        with st.expander("Acknowledge Required Material As Unavailable", expanded=False):
            selected = st.selectbox("Required slot", missing, format_func=lambda row: row["display_name"])
            reason = st.text_area("Analyst reason", key="waiver_reason")
            if st.button("Record Waiver", disabled=not reason.strip()):
                mark_slot(selected["slot_instance_id"], "NOT_AVAILABLE", reason=reason, actor=_actor())
                create_waiver(run["finalization_run_id"], selected["slot_instance_id"], reason=reason, actor=_actor())
                st.rerun()
    with database.get_connection() as connection:
        pending_waivers = [dict(row) for row in connection.execute(
            """SELECT w.*, s.display_name_snapshot FROM analyst_waivers w
               JOIN package_slot_instances s ON s.package_slot_instance_id=w.slot_instance_id
               WHERE w.package_version_id=? AND w.status='ACTIVE' AND w.confirmation_status!='CONFIRMED'
               ORDER BY w.created_at""",
            (run["package_version_id"],),
        ).fetchall()]
    if pending_waivers:
        with st.expander("Confirm Pending Final Waivers", expanded=True):
            selected = st.selectbox("Pending waiver", pending_waivers, format_func=lambda row: row["display_name_snapshot"])
            st.caption(f"Recorded by {selected['created_by']} at {selected['created_at']}")
            st.write(selected["reason"])
            if st.button("Confirm Final Waiver", type="primary"):
                confirm_waiver(selected["waiver_id"], actor=_actor())
                st.rerun()
    if readiness.ready and st.button("Complete Readiness Review", type="primary"):
        record_stage(run["finalization_run_id"], "READINESS_REVIEW", status="COMPLETED",
                     result={"ready": True, "warnings": list(readiness.warnings)}, input_fingerprint=readiness.fingerprint)
        st.success("Readiness review completed.")


def _documents(package: dict, run: dict) -> None:
    st.caption("Approved SEC HTML is rendered locally. No browser automation or remote assets are used.")
    cols = st.columns(3)
    if cols[0].button("Generate Reader PDFs", type="primary", use_container_width=True):
        _stage_result(lambda: render_sec_reader_pdfs(package["package_id"]), run, "DOCUMENT_RENDERING", "Reader PDF production completed.")
    if cols[1].button("Extract Filing Sections", use_container_width=True):
        _stage_result(lambda: extract_section_pdfs(package["package_id"], package_version_id=run["package_version_id"]),
                      run, "SECTION_EXTRACTION", "Section PDF production completed.")
    if cols[2].button("Build SEC Company Facts", use_container_width=True):
        _stage_result(lambda: build_company_facts(package["package_id"], run["package_version_id"], actor=_actor()),
                      run, "COMPANY_FACTS_BUILD", "SEC Company Facts store completed.")
    with database.get_connection() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT display_filename, artifact_type, conversion_status, qa_status, source_role
               FROM package_artifacts WHERE package_id=? AND artifact_status='CURRENT'
               ORDER BY artifact_type, display_filename""", (package["package_id"],)).fetchall()]
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
    facts = list_selected_facts(run["package_version_id"])
    if facts:
        st.dataframe([{key: row.get(key) for key in ("normalized_metric", "value", "unit", "period_end", "form", "validation_status")} for row in facts],
                     hide_index=True, use_container_width=True)


def _corpus(package: dict, run: dict) -> None:
    report = latest_preliminary_report(package["package_id"])
    if report and report.get("analysis_run_id"):
        st.success(f"Closed-corpus analysis is available: {report['analysis_run_id']}")
        if st.button("Confirm Corpus Processing Reuse", type="primary"):
            assign_source_roles(package["package_id"])
            record_stage(run["finalization_run_id"], "CORPUS_PROCESSING", status="COMPLETED",
                         result={"analysis_run_id": report["analysis_run_id"], "reused": True})
    elif st.button("Process Approved Corpus", type="primary"):
        _stage_result(lambda: generate_preliminary_recommendation(package["package_id"], actor=_actor()),
                      run, "CORPUS_PROCESSING", "Approved corpus processing completed.")
    else:
        st.info("No completed package analysis is available yet.")


def _analysis(run: dict) -> None:
    if st.button("Create Immutable Final Snapshot", type="primary"):
        _stage_result(lambda: create_final_snapshot(run["finalization_run_id"], actor=_actor()),
                      run, "FINAL_ANALYSIS_SNAPSHOT", "Final analysis snapshot created.")
    with database.get_connection() as connection:
        snapshot = connection.execute(
            "SELECT * FROM final_analysis_snapshots WHERE package_version_id=? ORDER BY created_at DESC LIMIT 1",
            (run["package_version_id"],)).fetchone()
    if snapshot:
        st.json({"Snapshot ID": snapshot["final_snapshot_id"], "Status": snapshot["status"], "Hash": snapshot["snapshot_hash"]})


def _recommendation(run: dict) -> None:
    if st.button("Prepare Final Recommendation", type="primary"):
        _stage_result(lambda: prepare_final_recommendation(run["finalization_run_id"]),
                      run, "FINAL_RECOMMENDATION", "Final recommendation prepared for analyst approval.")
    with database.get_connection() as connection:
        approval = connection.execute(
            "SELECT * FROM final_recommendation_approvals WHERE package_version_id=? ORDER BY created_at DESC LIMIT 1",
            (run["package_version_id"],)).fetchone()
    if not approval:
        return
    st.json({"AI Recommendation": approval["ai_recommendation"], "Analyst Recommendation": approval["analyst_recommendation"],
             "Status": approval["status"], "QA": approval["qa_status"]})
    if approval["status"] != "APPROVED":
        rating = st.segmented_control("Analyst rating", ["BUY", "HOLD", "SELL", "ANALYST_REVIEW_REQUIRED"],
                                      default=approval["ai_recommendation"])
        reason = st.text_area("Approval or override rationale", key="final_rating_reason")
        if st.button("Approve Final Rating And Generate Report", type="primary", disabled=not rating):
            _stage_result(lambda: approve_final_recommendation(approval["approval_id"], analyst_rating=rating,
                                                               reason=reason, actor=_actor()),
                          run, "FINAL_RECOMMENDATION", "Final one-page recommendation approved.")


def _exports(run: dict) -> None:
    cols = st.columns(3)
    if cols[0].button("Generate Final Checklist", type="primary", use_container_width=True):
        _stage_result(lambda: build_final_checklist(run["finalization_run_id"], actor=_actor()),
                      run, "CHECKLIST_FINALIZATION", "Final checklist generated.")
    if cols[1].button("Build Working ZIP", use_container_width=True):
        _stage_result(lambda: build_working_zip(run["finalization_run_id"]),
                      run, "WORKING_PACKAGE_BUILD", "Working package ZIP built.")
    if cols[2].button("Build Audit ZIP", use_container_width=True):
        _stage_result(lambda: build_audit_zip(run["finalization_run_id"]),
                      run, "AUDIT_PACKAGE_BUILD", "Audit package ZIP built.")
    with database.get_connection() as connection:
        outputs = [dict(row) for row in connection.execute(
            "SELECT * FROM final_zip_outputs WHERE package_version_id=? ORDER BY zip_type", (run["package_version_id"],)).fetchall()]
    for output in outputs:
        path = Path(output["local_path"])
        if path.is_file():
            st.download_button(f"Download {output['zip_type'].title()} ZIP", path.read_bytes(), file_name=path.name,
                               mime="application/zip", key=f"download_{output['zip_output_id']}")


def _lock(package: dict, run: dict) -> None:
    if st.button("Run Final QA", type="primary"):
        result = _stage_result(lambda: run_final_qa(run["finalization_run_id"], actor=_actor()),
                               run, "FINAL_QA", "Final QA completed.")
        if result and result["status"] == "PASSED":
            record_stage(run["finalization_run_id"], "READY_TO_LOCK", status="COMPLETED", result=result)
    with database.get_connection() as connection:
        qa = connection.execute("SELECT * FROM final_qa_results WHERE package_version_id=? ORDER BY created_at DESC LIMIT 1", (run["package_version_id"],)).fetchone()
        lock = connection.execute("SELECT * FROM final_package_locks WHERE package_version_id=?", (run["package_version_id"],)).fetchone()
    if qa:
        st.json({"Status": qa["status"], "Checks": json.loads(qa["checks_json"])})
    if not lock:
        confirmed = st.checkbox("I approve locking this final package and understand that changes require a new package version.")
        if st.button("Lock Final Package", type="primary", disabled=not confirmed):
            _stage_result(lambda: lock_final_package(run["finalization_run_id"], actor=_actor(), analyst_confirmed=confirmed),
                          run, "LOCKED", "Final package locked.")
    else:
        st.success(f"Locked at {lock['locked_at']} by {lock['locked_by']}.")
        note = st.text_input("Delivery note")
        cols = st.columns(2)
        if cols[0].button("Mark Delivered", use_container_width=True):
            _stage_result(lambda: mark_delivered(run["finalization_run_id"], actor=_actor(), note=note),
                          run, "DELIVERED", "Package marked delivered.")
        if cols[1].button("Create New Package Version", use_container_width=True):
            cloned = create_new_package_version(package["package_id"], actor=_actor())
            st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = cloned["package_id"]
            st.session_state["active_package"] = cloned
            st.rerun()


def main() -> None:
    bootstrap_page("Finalize Package")
    render_sidebar()
    st.title("Finalize Package")
    st.caption("Controlled document production, final analysis, delivery QA, and immutable locking.")
    st.text_input("Analyst", key="finalization_actor", placeholder="Name or initials")
    package = _package()
    if not package:
        return
    run = latest_finalization(package["package_id"])
    if not run:
        if st.button("Start Finalization", type="primary"):
            start_finalization(package["package_id"], actor=_actor())
            st.rerun()
        return
    st.markdown(f"**{package['ticker']}** · {package.get('company_name') or ''} · `{run['package_version_id']}` · {run['status']}")
    tabs = st.tabs(["1 Readiness", "2 Documents", "3 Corpus", "4 Analysis", "5 Recommendation", "6 Package", "7 Lock"])
    with tabs[0]: _readiness(package, run)
    with tabs[1]: _documents(package, run)
    with tabs[2]: _corpus(package, run)
    with tabs[3]: _analysis(run)
    with tabs[4]: _recommendation(run)
    with tabs[5]: _exports(run)
    with tabs[6]: _lock(package, run)
    with st.expander("Stage Audit", expanded=False):
        st.dataframe(list_stage_statuses(run["finalization_run_id"]), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
