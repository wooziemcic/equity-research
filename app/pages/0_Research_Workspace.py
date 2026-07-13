from __future__ import annotations

import html
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.services.analysis_pipeline import load_analysis_diagnostics
from app.services.checklist_service import ensure_package_checklist, normalize_checklist_status, normalize_requirement_level
from app.services.document_reconciliation_service import repair_package_document_records
from app.services.package_builder import validate_package_readiness
from app.services.research_workflow_service import (
    collection_timeline,
    package_coverage_summary,
    planned_collection_preview,
    reconcile_failed_workflow,
    run_research_workflow,
    start_automated_collection,
    update_research_settings,
    workflow_idempotency_key,
    workflow_stage_rows,
)
from app.services.taxonomy import category_options
from app.services.upload_service import (
    UploadCandidate,
    inspect_zip_upload,
    store_uploaded_files,
    validate_upload_batch,
)
from app.utils import database

logger = logging.getLogger(__name__)


def _safe_page_link(page: str, label: str) -> None:
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def _switch_to_result() -> None:
    try:
        st.switch_page("pages/6_Investment_Result.py")
    except (StreamlitPageNotFoundError, AttributeError):
        st.page_link("pages/6_Investment_Result.py", label="Open Investment Result")


def _sync_active_package(package: dict[str, Any]) -> None:
    st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
    st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "Research"
    st.session_state["active_package"] = package


def _load_active_package() -> dict[str, Any] | None:
    package_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if package_id:
        package = database.get_package_by_package_id(package_id)
        if package:
            _sync_active_package(package)
            return package

    packages = database.list_packages(limit=50)
    if not packages:
        render_empty_state("No research package selected.", "Search for a ticker to create or reopen a research workspace.")
        _safe_page_link("Home.py", "Go To Search")
        return None

    st.info("Select a research package to resume.")
    labels = {f"{package['ticker']} - {package.get('company_name') or package['package_id']}": package for package in packages}
    selected = st.selectbox("Research package", options=list(labels.keys()))
    if st.button("Resume Research Workspace", type="primary"):
        package = labels[selected]
        _sync_active_package(package)
        st.rerun()
    return None


def _header(package: dict[str, Any]) -> None:
    st.markdown(
        f"""
        <div class="workspace-header">
            <div>
                <div class="eyebrow">Research Workspace</div>
                <div class="workspace-ticker">{html.escape(package["ticker"])}</div>
                <div class="workspace-company">{html.escape(package.get("company_name") or "Company resolution pending")}</div>
            </div>
            <div class="workspace-meta">
                <div><span>Status</span><strong>{html.escape(package.get("status") or "")}</strong></div>
                <div><span>Research Cutoff</span><strong>{html.escape(package.get("research_cutoff_date") or "")}</strong></div>
                <div><span>Package</span><strong>{html.escape(package.get("package_id") or "")}</strong></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _automated_research_card(package: dict[str, Any]) -> dict[str, Any]:
    st.markdown('<div class="workflow-card-title">Automated Research</div>', unsafe_allow_html=True)
    st.caption("Collect supported SEC filings and optional public company materials from an analyst-supplied IR URL.")

    filing_options = list(config.FILING_HISTORY_OPTIONS.values())
    current_years = int(package.get("filing_history_years") or 3)
    if current_years not in filing_options:
        current_years = 3
    years = st.radio(
        "Filing history",
        options=filing_options,
        index=filing_options.index(current_years),
        format_func=lambda value: f"{value} year" if value == 1 else f"{value} years",
        horizontal=True,
        key=f"filing_years_{package['package_id']}",
    )
    cutoff = st.date_input(
        "Research cutoff date",
        value=date.fromisoformat(str(package["research_cutoff_date"])),
        max_value=date.today(),
        key=f"cutoff_{package['package_id']}",
    )
    filing_types = st.multiselect(
        "Filing types",
        options=list(config.SEC_SUPPORTED_FORMS),
        default=["10-K", "10-Q", "8-K"] if package.get("security_type") == "Common Equity" else ["20-F", "6-K", "10-K", "10-Q"],
        key=f"filing_types_{package['package_id']}",
    )
    public_materials = st.multiselect(
        "Public company materials",
        options=[
            "Earnings releases",
            "Earnings presentations",
            "Investor presentations",
            "Annual reports",
            "Investor-day materials",
            "Public supplemental materials",
            "Public ESG or sustainability reports",
        ],
        default=["Earnings releases", "Earnings presentations", "Investor presentations"],
        key=f"public_materials_{package['package_id']}",
    )
    ir_url = st.text_input(
        "Investor-relations URL",
        placeholder="https://investors.example.com",
        key=f"ir_url_{package['package_id']}",
    )

    if st.button("Save Automated Research Settings", use_container_width=True):
        try:
            package = update_research_settings(
                package["package_id"],
                filing_history_years=int(years),
                research_cutoff_date=cutoff,
            )
            _sync_active_package(package)
            st.success("Research settings updated.")
        except Exception as exc:
            st.error(str(exc))

    st.markdown("**Planned collection preview**")
    st.write(planned_collection_preview(public_materials))
    if st.button("Start Research Collection", type="primary", use_container_width=True, disabled=not filing_types):
        refreshed = database.get_package_by_package_id(package["package_id"]) or package
        try:
            with st.spinner("Running public collection through existing collectors..."):
                result = start_automated_collection(
                    refreshed,
                    filing_types=list(filing_types),
                    ir_url=ir_url.strip() or None,
                )
            st.session_state[config.SESSION_COLLECTION_STATE] = {
                "sec_summary": result.sec_summary,
                "ir_summary": result.ir_summary,
                "warnings": result.warnings,
                "errors": result.errors,
            }
            if result.errors:
                st.error("; ".join(result.errors))
            else:
                st.success(
                    f"Collection finished: SEC {result.sec_summary['downloaded']} downloaded, IR {result.ir_summary['downloaded']} downloaded."
                )
            for warning in result.warnings:
                st.warning(warning)
        except Exception as exc:
            logger.exception("Research collection failed")
            st.error(f"Research collection failed: {exc}")
        st.rerun()

    return {
        "filing_years": years,
        "cutoff": cutoff,
        "filing_types": filing_types,
        "public_materials": public_materials,
        "ir_url": ir_url,
    }


def _additional_research_card(package: dict[str, Any]) -> None:
    st.markdown('<div class="workflow-card-title">Additional Research</div>', unsafe_allow_html=True)
    st.caption(
        "Add authorized Bloomberg, Morningstar, FactSet, sell-side, credit, transcript, model, activist, or internal research files."
    )
    source_type = st.selectbox(
        "Source type",
        options=list(config.LICENSED_SOURCE_TYPES.keys()),
        format_func=lambda value: config.LICENSED_SOURCE_TYPES[value],
        key=f"upload_source_{package['package_id']}",
    )
    files = st.file_uploader(
        "Upload authorized files",
        accept_multiple_files=True,
        type=[ext.lstrip(".") for ext in config.SUPPORTED_UPLOAD_EXTENSIONS],
        key=f"uploads_{package['package_id']}",
    )
    if not files:
        counts = database.document_counts_for_package(package["package_id"])
        st.info(f"Uploads are optional. Current licensed files: {counts['licensed']} ({_format_bytes(counts['bytes'])} total package size).")
        return

    candidates = [UploadCandidate(file.name, file.getvalue(), getattr(file, "type", "")) for file in files]
    validations = validate_upload_batch(candidates, source_type=source_type)
    st.dataframe(
        [
            {
                "File": result.original_filename,
                "Size": result.file_size_bytes,
                "Valid": result.is_valid,
                "Detected": result.detected_file_type,
                "Suggested Classification": result.classification.category_display if result.classification else "",
                "Confidence": result.classification.confidence if result.classification else "",
                "Status": result.error or result.warning or "Ready",
            }
            for result in validations
        ],
        hide_index=True,
        use_container_width=True,
    )

    category_lookup = dict(category_options())
    metadata_by_name: dict[str, dict[str, Any]] = {}
    for result, candidate in zip(validations, candidates, strict=False):
        if not result.is_valid:
            continue
        st.markdown(f"**{html.escape(result.original_filename)}**")
        category_codes = list(category_lookup.keys())
        suggested = result.classification.category_code if result.classification else "other"
        selected_code = st.selectbox(
            "Final category",
            options=category_codes,
            index=category_codes.index(suggested) if suggested in category_codes else category_codes.index("other"),
            format_func=lambda code: category_lookup[code],
            key=f"phase7_upload_category_{result.sha256_hash}",
        )
        cols = st.columns(2)
        with cols[0]:
            title = st.text_input("Document title", value=Path(result.original_filename).stem, key=f"phase7_title_{result.sha256_hash}")
            publication_date = st.date_input("Publication date", value=None, key=f"phase7_pub_{result.sha256_hash}")
        with cols[1]:
            source_institution = st.text_input(
                "Source institution",
                value=config.LICENSED_SOURCE_TYPES[source_type],
                key=f"phase7_source_{result.sha256_hash}",
            )
            document_date = st.date_input("Document as-of date", value=None, key=f"phase7_docdate_{result.sha256_hash}")
        notes = st.text_area("Analyst notes", key=f"phase7_notes_{result.sha256_hash}")
        metadata_by_name[result.original_filename] = {
            "final_category_code": selected_code,
            "title": title,
            "publication_date": publication_date.isoformat() if publication_date else None,
            "source_institution": source_institution,
            "document_date": document_date.isoformat() if document_date else None,
            "analyst_notes": notes,
        }
        if result.extension == ".zip":
            with st.expander(f"ZIP inspection: {result.original_filename}", expanded=False):
                try:
                    st.dataframe([entry.__dict__ for entry in inspect_zip_upload(candidate.content)], hide_index=True, use_container_width=True)
                    st.caption("ZIP files are stored archive-only after inspection. Automatic extraction is not enabled.")
                except Exception as exc:
                    st.warning(f"ZIP inspection failed: {exc}")

    authorized = st.checkbox(
        "I confirm these files are authorized for internal use and comply with vendor entitlements.",
        key=f"phase7_authorized_{package['package_id']}",
    )
    if st.button("Upload Accepted Licensed Files", type="primary", disabled=not authorized, use_container_width=True):
        try:
            summary = store_uploaded_files(
                package,
                candidates,
                source_type=source_type,
                authorization_confirmed=authorized,
                metadata_by_name=metadata_by_name,
            )
            ensure_package_checklist(database.get_package_by_package_id(package["package_id"]) or package)
            st.success(
                f"Upload run complete: {summary['uploaded']} uploaded, {summary['duplicated']} duplicate, {summary['failed']} failed."
            )
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _timeline(package: dict[str, Any]) -> None:
    st.subheader("Collection Timeline")
    rows = collection_timeline(package["package_id"])
    st.markdown('<div class="timeline-list">', unsafe_allow_html=True)
    for row in rows:
        status_class = row["status"].lower().replace(" ", "-")
        st.markdown(
            f"""
            <div class="timeline-row timeline-{status_class}">
                <div class="timeline-dot"></div>
                <div>
                    <strong>{html.escape(row["stage"])}</strong>
                    <span>{html.escape(row["status"])}</span>
                    <p>{html.escape(row.get("detail") or "")}</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
    cols = st.columns(4)
    with cols[0]:
        _safe_page_link("pages/2_Document_Collection.py", "Review discovered filings")
    with cols[1]:
        _safe_page_link("pages/2_Document_Collection.py", "Review IR documents")
    with cols[2]:
        _safe_page_link("pages/2_Document_Collection.py", "Add more files")
    with cols[3]:
        _safe_page_link("pages/3_Package_Review.py", "Review missing items")


def _coverage(package: dict[str, Any]) -> None:
    st.subheader("Research Package Coverage")
    summary = package_coverage_summary(package)
    cols = st.columns(4)
    cols[0].metric("Public files", summary["public_files"])
    cols[1].metric("Licensed files", summary["licensed_files"])
    cols[2].metric("Core available", summary["core_available"])
    cols[3].metric("Missing core", summary["missing_core"])
    cols = st.columns(4)
    cols[0].metric("Recommended missing", summary["recommended_missing"])
    cols[1].metric("Not available", summary["not_available"])
    cols[2].metric("Failed items", summary["failed_items"])
    cols[3].metric("Duplicate items", summary["duplicate_items"])
    st.metric("Total size", _format_bytes(summary["total_size_bytes"]))

    with st.expander("Detailed checklist", expanded=False):
        checklist = ensure_package_checklist(package)
        st.dataframe(
            [
                {
                    "Item": item["display_name"],
                    "Requirement": item["requirement_level"],
                    "Status": item["effective_status"],
                    "Matched Docs": item["matched_document_count"],
                    "Latest Date": item.get("latest_document_date") or "",
                    "Note": item.get("analyst_note") or "",
                }
                for item in checklist
            ],
            hide_index=True,
            use_container_width=True,
        )
    if summary["failed_items"]:
        st.warning("This package has unresolved failed document records. If files were downloaded before the failure, repair can recreate missing package-document rows without downloading or modifying locked versions.")
        if st.button("Repair Document Records", use_container_width=True):
            try:
                result = repair_package_document_records(package["package_id"])
                st.success("Document record repair completed.")
                st.dataframe(
                    [
                        {"Metric": "Files found", "Count": result["files_found"]},
                        {"Metric": "Records repaired", "Count": result["records_repaired"]},
                        {"Metric": "Existing records reused", "Count": result["existing_records_reused"]},
                        {"Metric": "Duplicates skipped", "Count": result["duplicates_skipped"]},
                        {"Metric": "Items still failed", "Count": result["items_still_failed"]},
                        {"Metric": "Checklist items recalculated", "Count": result["checklist_items_recalculated"]},
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
                st.rerun()
            except Exception as exc:
                logger.exception("Document record repair failed")
                st.error(f"Document record repair failed: {exc}")


def _proceed(package: dict[str, Any]) -> None:
    st.subheader("Build Package And Generate Analysis")
    checklist = ensure_package_checklist(package)
    needs_missing = any(
        normalize_requirement_level(item.get("requirement_level")) == "required"
        and normalize_checklist_status(item.get("effective_status")) == config.CHECKLIST_STATUS_MISSING
        for item in checklist
    )
    needs_stale = any(normalize_checklist_status(item.get("effective_status")) == config.CHECKLIST_STATUS_STALE for item in checklist)
    needs_review = any(normalize_checklist_status(item.get("effective_status")) == config.CHECKLIST_STATUS_NEEDS_REVIEW for item in checklist)

    st.caption("This action validates readiness, builds and locks the package version, processes documents, extracts evidence, runs analysis, and generates a draft report.")
    reviewed = st.checkbox(
        "I reviewed the package checklist and understand that missing, stale, unavailable, or not-applicable research may affect later analysis.",
        value=bool(package.get("checklist_reviewed")),
        key=f"phase7_reviewed_{package['package_id']}",
    )
    missing_ack = st.checkbox(
        "I acknowledge missing required checklist items.",
        value=bool(package.get("missing_core_acknowledged")),
        disabled=not needs_missing,
        key=f"phase7_missing_ack_{package['package_id']}",
    )
    stale_ack = st.checkbox(
        "I acknowledge stale checklist items.",
        value=bool(package.get("stale_documents_acknowledged")),
        disabled=not needs_stale,
        key=f"phase7_stale_ack_{package['package_id']}",
    )
    review_ack = st.checkbox(
        "I acknowledge needs-review checklist items.",
        value=bool(package.get("needs_review_acknowledged")),
        disabled=not needs_review,
        key=f"phase7_review_ack_{package['package_id']}",
    )
    if st.button("Save Checklist Acknowledgement", use_container_width=True):
        package = database.update_package_review_acknowledgement(
            package["package_id"],
            checklist_reviewed=reviewed,
            reviewed_by="analyst",
            review_note="Phase 7 workspace acknowledgement",
            missing_core_acknowledged=missing_ack or not needs_missing,
            stale_documents_acknowledged=stale_ack or not needs_stale,
            needs_review_acknowledged=review_ack or not needs_review,
        ) or package
        _sync_active_package(package)
        st.success("Checklist acknowledgement saved.")
        st.rerun()

    refreshed = database.get_package_by_package_id(package["package_id"]) or package
    readiness = validate_package_readiness(refreshed)
    if readiness.errors:
        st.error("Blocking requirements")
        for error in readiness.errors:
            st.write(f"- {error}")
    if readiness.warnings:
        st.warning("Ready with warnings after acknowledgement, or currently waiting for acknowledgement.")
        for warning in readiness.warnings[:8]:
            st.write(f"- {warning}")
    for notice in readiness.notices:
        st.info(notice)

    workflow = database.latest_research_workflow_run(package["package_id"])
    if workflow:
        st.write(f"Latest workflow: `{workflow['workflow_run_id']}` - {workflow['status']}")
        st.dataframe(workflow_stage_rows(workflow), hide_index=True, use_container_width=True)
        errors = json.loads(workflow.get("errors_json") or "[]")
        if errors:
            st.error("; ".join(str(error) for error in errors))
        _metric_diagnostics_expander(workflow)
        if workflow.get("status") == config.WORKFLOW_STATUS_FAILED:
            if st.button("Retry From Failed Stage", use_container_width=True):
                try:
                    with st.spinner("Resuming the failed workflow stage..."):
                        retried = run_research_workflow(
                            package["package_id"],
                            idempotency_key=workflow.get("idempotency_key") or workflow_idempotency_key(package),
                            retry_failed=True,
                        )
                    st.session_state[config.SESSION_WORKFLOW_STATE] = retried
                    st.session_state[config.SESSION_ACTIVE_VERSION_ID] = retried.get("version_id")
                    st.session_state[config.SESSION_ACTIVE_PROCESSING_RUN_ID] = retried.get("processing_run_id")
                    st.session_state[config.SESSION_ACTIVE_ANALYSIS_RUN_ID] = retried.get("analysis_run_id")
                    st.session_state[config.SESSION_ACTIVE_REPORT_ID] = retried.get("report_id")
                    st.rerun()
                except Exception as exc:
                    logger.exception("Research workflow retry failed")
                    st.error(f"Workflow retry failed: {exc}")
            stage_statuses = json.loads(workflow.get("stage_statuses_json") or "{}")
            if stage_statuses.get("Building package") == "Failed" and st.button("Repair Current Failed Build", use_container_width=True):
                try:
                    with st.spinner("Checking completed package artifacts and cleaning abandoned staging..."):
                        repair = reconcile_failed_workflow(package["package_id"])
                    st.info(
                        f"Existing version reused: {repair['existing_version_reused']}. "
                        f"New version created: {repair['new_version_created']}. "
                        f"Temporary artifacts cleaned: {repair['temporary_artifacts_cleaned']}. "
                        f"Next stage resumed: {repair['next_stage_resumed']}."
                    )
                    if repair.get("remaining_error"):
                        st.warning(f"Remaining error: {repair['remaining_error']}")
                    st.rerun()
                except Exception as exc:
                    logger.exception("Failed workflow reconciliation failed")
                    st.error(f"Failed workflow reconciliation failed: {exc}")

    can_proceed = readiness.status in {config.READINESS_READY, config.READINESS_READY_WITH_WARNINGS}
    if st.button("Build Package and Generate Analysis", type="primary", disabled=not can_proceed, use_container_width=True):
        try:
            with st.spinner("Running research workflow against the locked corpus..."):
                refreshed = database.get_package_by_package_id(package["package_id"]) or package
                workflow = run_research_workflow(
                    package["package_id"],
                    idempotency_key=workflow_idempotency_key(refreshed),
                )
            st.session_state[config.SESSION_WORKFLOW_STATE] = workflow
            st.session_state[config.SESSION_ACTIVE_VERSION_ID] = workflow.get("version_id")
            st.session_state[config.SESSION_ACTIVE_PROCESSING_RUN_ID] = workflow.get("processing_run_id")
            st.session_state[config.SESSION_ACTIVE_ANALYSIS_RUN_ID] = workflow.get("analysis_run_id")
            st.session_state[config.SESSION_ACTIVE_REPORT_ID] = workflow.get("report_id")
            if workflow["status"] == config.WORKFLOW_STATUS_COMPLETED:
                st.success("Research package built and draft analysis generated.")
                _switch_to_result()
            elif workflow["status"] == config.WORKFLOW_STATUS_COMPLETED_WITH_WARNINGS:
                st.warning("Research package built and draft analysis generated with evidence limitations.")
                _switch_to_result()
            else:
                st.warning(f"Workflow ended with status {workflow['status']}.")
                st.dataframe(workflow_stage_rows(workflow), hide_index=True, use_container_width=True)
        except Exception as exc:
            logger.exception("Research workflow failed")
            st.error(f"Research workflow failed: {exc}")


def _metric_diagnostics_expander(workflow: dict[str, Any]) -> None:
    analysis_run_id = workflow.get("analysis_run_id")
    analysis = database.get_analysis_run(analysis_run_id) if analysis_run_id else None
    payload = load_analysis_diagnostics(analysis)
    diagnostics = payload.get("metric_diagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, dict):
        return
    with st.expander("Why metric calculation could not complete", expanded=workflow.get("status") == config.WORKFLOW_STATUS_FAILED):
        if diagnostics.get("exception_type"):
            st.write(f"Exception type: `{diagnostics.get('exception_type')}`")
        if diagnostics.get("safe_error_message"):
            st.write(f"Safe error message: {diagnostics.get('safe_error_message')}")
        st.write(f"Analysis run ID: `{diagnostics.get('analysis_run_id') or 'N/A'}`")
        st.write(f"Processing run ID: `{diagnostics.get('processing_run_id') or 'N/A'}`")
        cols = st.columns(4)
        cols[0].metric("Evidence records", diagnostics.get("evidence_records", 0))
        cols[1].metric("Verified", diagnostics.get("verified_records", 0))
        cols[2].metric("Accepted", diagnostics.get("accepted_records", 0))
        cols[3].metric("Numeric values", diagnostics.get("numeric_value_records", 0))
        st.write("Metric inputs discovered")
        st.json(diagnostics.get("metric_inputs_discovered") or {})
        calculated = diagnostics.get("metrics_successfully_calculated") or []
        st.write("Metrics successfully calculated")
        st.write(", ".join(calculated) if calculated else "None")
        skipped = diagnostics.get("metrics_skipped") or []
        if skipped:
            st.write("Metrics skipped")
            st.dataframe(skipped, hide_index=True, use_container_width=True)
        warnings = payload.get("warnings") if isinstance(payload, dict) else None
        limitations = payload.get("limitations") if isinstance(payload, dict) else None
        if warnings:
            st.write("Warnings")
            for warning in warnings:
                st.write(f"- {warning}")
        if limitations:
            st.write("Limitations")
            for limitation in limitations:
                st.write(f"- {limitation}")


def _format_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main() -> None:
    bootstrap_page("Research")
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "Research"

    package = _load_active_package()
    if not package:
        return

    _header(package)
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            _automated_research_card(package)
    with right:
        with st.container(border=True):
            _additional_research_card(package)

    st.divider()
    _timeline(database.get_package_by_package_id(package["package_id"]) or package)
    st.divider()
    refreshed = database.get_package_by_package_id(package["package_id"]) or package
    _coverage(refreshed)
    st.divider()
    _proceed(refreshed)


if __name__ == "__main__":
    main()
