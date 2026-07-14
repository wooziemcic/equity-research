from __future__ import annotations

import csv
import json
import logging
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import streamlit as st
from openpyxl import Workbook

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.analysis.scenario_analysis import set_scenario_probabilities
from app.services.analysis_pipeline import create_analysis_run, validate_analysis_eligibility
from app.services.evidence_service import create_analyst_evidence_from_chunk, verify_evidence_record
from app.services.evidence_taxonomy import evidence_type_options
from app.services.processing_pipeline import (
    processing_performance_summary,
    resume_processing_run,
    retry_failed_documents,
    run_processing_pipeline,
    validate_processing_eligibility,
)
from app.services.openai_service import preflight_openai
from app.services.recommendation_engine import complete_analyst_review, override_scorecard_item, pm_decision
from app.services.reporting.investment_report import generate_investment_report
from app.services.retrieval_service import search_chunks
from app.utils import database

logger = logging.getLogger(__name__)


def _load_or_select_package() -> dict[str, Any] | None:
    active_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if active_id:
        package = database.get_package_by_package_id(active_id)
        if package:
            st.session_state["active_package"] = package
            return package
    packages = database.list_packages()
    if not packages:
        render_empty_state("No packages available.", "Create and lock a package version before document intelligence.")
        return None
    labels = {f"{package['ticker']} - {package['package_id']}": package for package in packages}
    selected = st.selectbox("Package", options=list(labels.keys()))
    if st.button("Use Selected Package", type="primary"):
        package = labels[selected]
        st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
        st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
        st.session_state["active_package"] = package
        st.rerun()
    return None


def _locked_versions(package: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        version
        for version in database.list_package_versions(package["package_id"])
        if version.get("status") == config.VERSION_STATUS_LOCKED
        and version.get("integrity_status") in {config.INTEGRITY_VERIFIED, config.INTEGRITY_VERIFIED_WITH_WARNINGS}
    ]


def _select_locked_version(package: dict[str, Any]) -> dict[str, Any] | None:
    versions = _locked_versions(package)
    st.subheader("Locked Version")
    if not versions:
        render_empty_state("No locked verified version.", "Build and lock a package version in Package Review.")
        return None
    labels = {f"{version.get('display_version') or version['version_id']} [{version['version_id']}] ({version.get('integrity_status')})": version for version in versions}
    selected = st.selectbox("Version", options=list(labels.keys()))
    version = labels[selected]
    docs = database.list_package_version_documents(version["version_id"])
    cols = st.columns(5)
    cols[0].metric("Display Version", version.get("display_version") or version["version_id"])
    cols[1].metric("Cutoff", version["research_cutoff_date"])
    cols[2].metric("Documents", len(docs))
    cols[3].metric("Integrity", version.get("integrity_status") or "")
    cols[4].metric("Status", version["status"])
    return version


def _processing_controls(version: dict[str, Any]) -> None:
    st.subheader("Processing")
    eligibility = validate_processing_eligibility(version["version_id"], record_event=False)
    if eligibility.errors:
        st.error("Processing blocked")
        for error in eligibility.errors:
            st.write(f"- {error}")
    if eligibility.warnings:
        st.warning("Integrity warnings")
        for warning in eligibility.warnings:
            st.write(f"- {warning}")
    cols = st.columns([1, 1, 1.2])
    with cols[0]:
        ocr_enabled = st.checkbox("OCR enabled", value=config.OCR_ENABLED)
    with cols[1]:
        retrieval_mode = st.selectbox("Retrieval", options=["keyword", "hybrid"], index=0 if config.RETRIEVAL_MODE != "hybrid" else 1)
    with cols[2]:
        st.metric("Pipeline", config.PROCESSING_PIPELINE_VERSION)
    if st.button("Start Processing Run", type="primary", disabled=bool(eligibility.errors)):
        try:
            progress = st.progress(0.0)
            detail = st.empty()

            def update_progress(payload: dict[str, Any]) -> None:
                total = max(int(payload.get("total") or 0), 1)
                completed = int(payload.get("completed") or 0)
                progress.progress(min(completed / total, 1.0))
                eta = payload.get("estimated_remaining")
                suffix = f"; about {eta:.0f} seconds of work remaining" if eta is not None and completed else ""
                detail.write(
                    f"{payload.get('stage')}; reused {payload.get('reused', 0)}; failed {payload.get('failed', 0)}; "
                    f"elapsed {float(payload.get('elapsed') or 0):.1f} seconds{suffix}"
                )

            run = run_processing_pipeline(
                version["version_id"],
                ocr_enabled=ocr_enabled,
                retrieval_mode=retrieval_mode,
                progress_callback=update_progress,
            )
            st.success(f"Processing run {run['processing_run_id']} finished with status {run['status']}.")
            st.rerun()
        except Exception as exc:
            logger.exception("Processing failed")
            st.error(f"Processing failed: {exc}")


def _run_history(version: dict[str, Any]) -> dict[str, Any] | None:
    runs = database.list_processing_runs(version["version_id"])
    st.subheader("Processing Run History")
    if not runs:
        st.info("No processing runs for this locked version.")
        return None
    st.dataframe(
        [
            {
                "Run ID": run["processing_run_id"],
                "Status": run["status"],
                "Started": run["started_at"],
                "Completed": run.get("completed_at") or "",
                "Documents": run["total_documents"],
                "Processed": run["successful_documents"],
                "Partial": run["partial_documents"],
                "Failed": run["failed_documents"],
                "Chunks": run["chunks_created"],
                "Evidence": run["evidence_records_created"],
            }
            for run in runs
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {run["processing_run_id"]: run for run in runs}
    selected = st.selectbox("Selected processing run", options=list(labels.keys()))
    return labels[selected]


def _resume_controls(run: dict[str, Any]) -> None:
    items = database.list_processing_document_items(run["processing_run_id"])
    pending = sum(item.get("status") in {config.PROCESSING_STATUS_PENDING, config.PROCESSING_STATUS_RUNNING} for item in items)
    failed = sum(item.get("status") == "FAILED" for item in items)
    cols = st.columns(3)
    if cols[0].button("Resume Processing", disabled=not pending, use_container_width=True):
        resume_processing_run(run["processing_run_id"])
        st.rerun()
    if cols[1].button("Retry Failed Documents", disabled=not failed, use_container_width=True):
        retry_failed_documents(run["processing_run_id"])
        st.rerun()
    if cols[2].button("View Slowest Documents", disabled=not items, use_container_width=True):
        st.session_state[f"show_slowest_{run['processing_run_id']}"] = True
    if st.session_state.get(f"show_slowest_{run['processing_run_id']}"):
        summary = processing_performance_summary(run["processing_run_id"])
        st.caption(f"Slowest parser type: {summary['slowest_parser_type']}")
        st.dataframe(
            [
                {
                    "Document": item.get("version_document_id"),
                    "Parser": item.get("parser_used"),
                    "Seconds": item.get("parse_duration_seconds") or 0,
                    "Size": item.get("file_size_bytes") or 0,
                    "Characters": item.get("extracted_character_count") or 0,
                    "Pages": item.get("page_count") or 0,
                    "Chunks": item.get("chunk_count") or 0,
                    "Evidence": item.get("evidence_count") or 0,
                    "Warnings": item.get("warning_count") or 0,
                    "Error": item.get("error_message") or "",
                    "Reuse": item.get("reuse_status") or "",
                }
                for item in summary["slowest_documents"]
            ],
            hide_index=True,
            use_container_width=True,
        )


def _summary(run: dict[str, Any]) -> None:
    cols = st.columns(8)
    cols[0].metric("Processed", run["successful_documents"])
    cols[1].metric("Partial", run["partial_documents"])
    cols[2].metric("Failed", run["failed_documents"])
    cols[3].metric("Pages", run["pages_processed"])
    cols[4].metric("Sheets", run["sheets_processed"])
    cols[5].metric("Tables", run["tables_detected"])
    cols[6].metric("Chunks", run["chunks_created"])
    cols[7].metric("Evidence", run["evidence_records_created"])


def _document_explorer(version: dict[str, Any], run: dict[str, Any]) -> None:
    results = database.list_document_processing_results(run["processing_run_id"])
    version_docs = {doc["document_id"]: doc for doc in database.list_package_version_documents(version["version_id"])}
    st.dataframe(
        [
            {
                "Document": version_docs.get(result["version_document_id"], {}).get("title") or result["version_document_id"],
                "Status": result["processing_status"],
                "Parser": result["parser_used"],
                "Pages": result["page_count"],
                "Sheets": result["sheet_count"],
                "Characters": result["extracted_character_count"],
                "OCR Required": bool(result["ocr_required"]),
                "Warnings": result["warning_count"],
                "Error": result.get("error_message") or "",
            }
            for result in results
        ],
        hide_index=True,
        use_container_width=True,
    )
    if not results:
        st.info("No document-processing rows for this run.")
        return
    doc_labels = {
        f"{version_docs.get(result['version_document_id'], {}).get('title') or result['version_document_id']} - {result['version_document_id']}": result["version_document_id"]
        for result in results
    }
    selected_doc_label = st.selectbox("Document detail", options=list(doc_labels.keys()))
    version_document_id = doc_labels[selected_doc_label]
    pages = database.list_document_pages(run["processing_run_id"], version_document_id)
    sheets = database.list_document_sheets(run["processing_run_id"], version_document_id)
    page_col, sheet_col = st.columns(2)
    with page_col:
        st.write("Pages")
        st.dataframe(
            [
                {
                    "Page": page["page_label"],
                    "Method": page["extraction_method"],
                    "Native Chars": page["native_text_character_count"],
                    "OCR Chars": page["ocr_text_character_count"],
                    "OCR Confidence": page.get("ocr_confidence"),
                    "Warnings": ", ".join(json.loads(page.get("processing_warnings_json") or "[]")),
                }
                for page in pages
            ],
            hide_index=True,
            use_container_width=True,
        )
    with sheet_col:
        st.write("Sheets")
        st.dataframe(
            [
                {
                    "Sheet": sheet["sheet_name"],
                    "Hidden": sheet.get("hidden_state") or "",
                    "Used Range": sheet.get("used_range") or "",
                    "Formulas": sheet["formula_cell_count"],
                    "Cached Formula Values": sheet["cached_value_cell_count"],
                    "External Links": sheet["external_link_count"],
                    "Warnings": sheet.get("warning_flags") or "",
                }
                for sheet in sheets
            ],
            hide_index=True,
            use_container_width=True,
        )
    st.write("Search")
    query = st.text_input("Search query")
    public_filter = st.selectbox("Source access", options=["All", "Public", "Licensed"])
    public_only = None if public_filter == "All" else public_filter == "Public"
    if query:
        results = search_chunks(
            version_id=version["version_id"],
            processing_run_id=run["processing_run_id"],
            query=query,
            public_only=public_only,
        )
        st.dataframe(
            [
                {
                    "Score": item.score,
                    "Document": version_docs.get(item.version_document_id, {}).get("title") or item.version_document_id,
                    "Page": item.page_number or "",
                    "Sheet": item.sheet_name or "",
                    "Rows": item.row_range or "",
                    "Section": item.section_heading or "",
                    "Text": item.chunk_text[:500],
                }
                for item in results
            ],
            hide_index=True,
            use_container_width=True,
        )


def _evidence_ledger(version: dict[str, Any], run: dict[str, Any]) -> None:
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=version["version_id"])
    version_docs = {doc["document_id"]: doc for doc in database.list_package_version_documents(version["version_id"])}
    if not evidence:
        st.info("No evidence records for this run.")
        _analyst_evidence_form(run)
        return
    evidence_types = ["All"] + sorted({item["evidence_type"] for item in evidence})
    verification_statuses = ["All"] + sorted({item["verification_status"] for item in evidence})
    analyst_statuses = ["All"] + sorted({item["analyst_status"] for item in evidence})
    cols = st.columns(3)
    type_filter = cols[0].selectbox("Evidence type", options=evidence_types)
    verification_filter = cols[1].selectbox("Verification status", options=verification_statuses)
    analyst_filter = cols[2].selectbox("Analyst status", options=analyst_statuses)
    filtered = [
        item
        for item in evidence
        if (type_filter == "All" or item["evidence_type"] == type_filter)
        and (verification_filter == "All" or item["verification_status"] == verification_filter)
        and (analyst_filter == "All" or item["analyst_status"] == analyst_filter)
    ]
    st.dataframe(
        [_evidence_row(item, version_docs) for item in filtered],
        hide_index=True,
        use_container_width=True,
    )
    labels = {f"{item['evidence_type']} - {item['evidence_id']}": item for item in filtered}
    if labels:
        selected_label = st.selectbox("Selected evidence", options=list(labels.keys()))
        selected = labels[selected_label]
        note = st.text_area("Analyst note", value=selected.get("analyst_note") or "")
        cols = st.columns(4)
        if cols[0].button("Accept"):
            database.update_evidence_analyst_status(selected["evidence_id"], config.ANALYST_STATUS_ACCEPTED, note)
            st.rerun()
        if cols[1].button("Reject"):
            database.update_evidence_analyst_status(selected["evidence_id"], config.ANALYST_STATUS_REJECTED, note)
            st.rerun()
        if cols[2].button("Needs Review"):
            database.update_evidence_analyst_status(selected["evidence_id"], config.ANALYST_STATUS_NEEDS_REVIEW, note)
            st.rerun()
        if cols[3].button("Verify Citation"):
            refreshed = database.get_evidence_record(selected["evidence_id"])
            if refreshed:
                verification = verify_evidence_record(refreshed)
                st.success(f"Verification: {verification['support_status']} ({verification['support_score']:.2f})")
                st.rerun()
    st.divider()
    _analyst_evidence_form(run)


def _evidence_row(item: dict[str, Any], version_docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    doc = version_docs.get(item["version_document_id"], {})
    locator = json.loads(item.get("source_locator_json") or "{}")
    citation = _citation(doc, item, locator)
    return {
        "Claim": item["claim_text"],
        "Type": item["evidence_type"],
        "Source": doc.get("title") or item["version_document_id"],
        "Citation": citation,
        "Period": item.get("period") or "",
        "Value": item.get("value") if item.get("value") is not None else "",
        "Unit": item.get("unit") or item.get("currency") or "",
        "Confidence": item["confidence"],
        "Verification": item["verification_status"],
        "Analyst Status": item["analyst_status"],
    }


def _citation(doc: dict[str, Any], item: dict[str, Any], locator: dict[str, Any]) -> str:
    title = doc.get("title") or locator.get("display_title") or item["version_document_id"]
    parts: list[str] = [str(title)]
    if item.get("page_number"):
        parts.append(f"p. {item['page_number']}")
    if item.get("sheet_name"):
        parts.append(str(item["sheet_name"]))
    if item.get("cell_or_row_range"):
        parts.append(str(item["cell_or_row_range"]))
    if item.get("section_heading"):
        parts.append(str(item["section_heading"]))
    return "[" + ", ".join(parts) + "]"


def _analyst_evidence_form(run: dict[str, Any]) -> None:
    st.write("Analyst-Added Evidence")
    chunks = database.list_document_chunks(run["processing_run_id"], version_id=run["version_id"])
    if not chunks:
        st.caption("No chunks are available for analyst-added evidence.")
        return
    labels = {f"{chunk['version_document_id']} chunk {chunk['chunk_index']} - {chunk['chunk_text'][:80]}": chunk for chunk in chunks[:500]}
    selected_chunk = labels[st.selectbox("Source chunk", options=list(labels.keys()))]
    type_options = evidence_type_options()
    type_labels = {display: code for code, display in type_options}
    evidence_label = st.selectbox("Evidence type", options=list(type_labels.keys()))
    claim = st.text_area("Claim")
    metric = st.text_input("Metric")
    period = st.text_input("Period")
    value_text = st.text_input("Value")
    unit = st.text_input("Unit")
    note = st.text_area("Analyst evidence note")
    if st.button("Add Evidence"):
        value = None
        if value_text.strip():
            try:
                value = float(value_text.replace(",", ""))
            except ValueError:
                st.error("Value must be numeric when supplied.")
                return
        create_analyst_evidence_from_chunk(
            chunk=selected_chunk,
            evidence_type=type_labels[evidence_label],
            claim_text=claim,
            metric_name=metric or None,
            value=value,
            unit=unit or None,
            period=period or None,
            analyst_note=note,
        )
        st.success("Analyst evidence added with pending verification.")
        st.rerun()


def _conflicts(run: dict[str, Any]) -> None:
    conflicts = database.list_claim_conflicts(run["processing_run_id"])
    if not conflicts:
        st.info("No conflicts detected.")
        return
    evidence = {item["evidence_id"]: item for item in database.list_evidence_records(run["processing_run_id"])}
    st.dataframe(
        [
            {
                "Type": conflict["conflict_type"],
                "Severity": conflict["severity"],
                "Metric": conflict.get("metric") or "",
                "Period": conflict.get("period") or "",
                "Evidence A": evidence.get(conflict["evidence_id_a"], {}).get("claim_text", ""),
                "Evidence B": evidence.get(conflict["evidence_id_b"], {}).get("claim_text", ""),
                "Explanation": conflict["explanation"],
                "Analyst Status": conflict["analyst_status"],
            }
            for conflict in conflicts
        ],
        hide_index=True,
        use_container_width=True,
    )


def _duplicates(run: dict[str, Any]) -> None:
    groups = database.list_duplicate_groups(run["processing_run_id"])
    if not groups:
        st.info("No duplicate content groups detected.")
        return
    st.dataframe(
        [
            {
                "Group": group["duplicate_group_id"],
                "Type": group["duplicate_type"],
                "Members": group["member_count"],
                "Explanation": group.get("explanation") or "",
                "Member IDs": group["member_chunk_ids_json"],
            }
            for group in groups
        ],
        hide_index=True,
        use_container_width=True,
    )


def _exports(run: dict[str, Any]) -> None:
    evidence = database.list_evidence_records(run["processing_run_id"])
    conflicts = database.list_claim_conflicts(run["processing_run_id"])
    summary = {
        **run,
        "warnings": json.loads(run.get("warnings_json") or "[]"),
        "errors": json.loads(run.get("errors_json") or "[]"),
    }
    st.download_button(
        "Evidence CSV",
        data=_rows_to_csv(evidence),
        file_name=f"{run['processing_run_id']}_evidence.csv",
        mime="text/csv",
        disabled=not evidence,
    )
    st.download_button(
        "Evidence XLSX",
        data=_rows_to_xlsx(evidence),
        file_name=f"{run['processing_run_id']}_evidence.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=not evidence,
    )
    st.download_button(
        "Conflicts CSV",
        data=_rows_to_csv(conflicts),
        file_name=f"{run['processing_run_id']}_conflicts.csv",
        mime="text/csv",
        disabled=not conflicts,
    )
    st.download_button(
        "Run Summary JSON",
        data=json.dumps(summary, indent=2, sort_keys=True),
        file_name=f"{run['processing_run_id']}_summary.json",
        mime="application/json",
    )


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _rows_to_xlsx(rows: list[dict[str, Any]]) -> bytes:
    output = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Evidence"
    if rows:
        headers = list(rows[0].keys())
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(header) for header in headers])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    workbook.save(output)
    return output.getvalue()


def _analysis_controls(version: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    st.subheader("Analysis Run")
    eligibility = validate_analysis_eligibility(version["version_id"], run["processing_run_id"], record_event=False)
    if eligibility.errors:
        st.error("Analysis blocked")
        for error in eligibility.errors:
            st.write(f"- {error}")
    if eligibility.warnings:
        st.warning("Analysis warnings")
        for warning in eligibility.warnings:
            st.write(f"- {warning}")
    if eligibility.limitations:
        st.info("Evidence limitations")
        for limitation in eligibility.limitations:
            st.write(f"- {limitation}")
    cols = st.columns([1, 1, 1])
    cols[0].metric("Analysis Pipeline", config.ANALYSIS_PIPELINE_VERSION)
    cols[1].metric("Scorecard", config.SCORECARD_VERSION)
    cols[2].metric("Valuation Config", config.VALUATION_CONFIGURATION_VERSION)
    if config.OPENAI_REQUIRED:
        if st.button("Test OpenAI Connectivity"):
            result = preflight_openai(force=True)
            if result.connected:
                st.success("OpenAI structured-output preflight passed.")
                st.write(f"Model: {result.model}")
                st.write(f"Endpoint: {result.endpoint}")
                st.write(f"Structured output verified: {'Yes' if result.structured_output_verified else 'No'}")
                st.write("AI extraction: enabled")
                st.write("AI narrative: enabled")
            else:
                st.error(result.message or "OpenAI connectivity failed.")
    if st.button("Create Analysis Run", type="primary", disabled=bool(eligibility.errors)):
        try:
            created = create_analysis_run(version["version_id"], run["processing_run_id"])
            st.success(f"Analysis run {created['analysis_run_id']} created: {created.get('preliminary_recommendation')}")
            st.rerun()
        except Exception as exc:
            logger.exception("Analysis failed")
            st.error(f"Analysis failed: {exc}")
    runs = database.list_analysis_runs(version["version_id"], processing_run_id=run["processing_run_id"])
    if not runs:
        st.info("No analysis runs for this processing run.")
        return None
    st.dataframe(
        [
            {
                "Analysis Run": item["analysis_run_id"],
                "Status": item["status"],
                "Preliminary": item.get("preliminary_recommendation") or "",
                "Analyst": item.get("analyst_adjusted_recommendation") or "",
                "PM": item.get("pm_approved_recommendation") or "",
                "Confidence": item.get("confidence") or "",
                "Evidence Coverage": item.get("evidence_coverage"),
                "Reference Price": item.get("reference_price"),
                "AI Review": item.get("ai_review_status") or "",
                "Endpoint": item.get("ai_endpoint") or "",
                "Updated": item.get("updated_at"),
            }
            for item in runs
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {item["analysis_run_id"]: item for item in runs}
    selected = st.selectbox("Selected analysis run", options=list(labels.keys()))
    selected_run = labels[selected]
    if config.OPENAI_REQUIRED and selected_run.get("status") == config.ANALYSIS_STATUS_FAILED:
        if st.button("Retry OpenAI Analysis"):
            try:
                retried = create_analysis_run(version["version_id"], run["processing_run_id"])
                st.success(f"OpenAI analysis retry created {retried['analysis_run_id']}.")
                st.rerun()
            except Exception as exc:
                logger.exception("OpenAI analysis retry failed")
                st.error(f"OpenAI analysis retry failed: {exc}")
    return selected_run


def _analysis_metrics_tab(analysis_run: dict[str, Any]) -> None:
    metrics = database.list_analysis_metrics(analysis_run["analysis_run_id"])
    if not metrics:
        st.info("No calculated metrics for this analysis run.")
        return
    st.dataframe(
        [
            {
                "Metric": metric["display_name"],
                "Value": metric.get("value"),
                "Unit": metric.get("unit") or metric.get("currency") or "",
                "Period": metric.get("period") or "",
                "Method": metric["calculation_method"],
                "Formula": metric["formula_description"],
                "Evidence IDs": metric["source_evidence_ids_json"],
                "Confidence": metric["confidence"],
                "Warning": metric.get("warning") or "",
            }
            for metric in metrics
        ],
        hide_index=True,
        use_container_width=True,
    )


def _scorecard_tab(analysis_run: dict[str, Any]) -> None:
    items = database.list_scorecard_items(analysis_run["analysis_run_id"])
    if not items:
        st.info("No scorecard rows for this analysis run.")
        return
    st.dataframe(
        [
            {
                "Pillar": item["pillar_name"],
                "Score": item["score"],
                "Weight": item["weight"],
                "Weighted": item["weighted_score"],
                "Evidence Quality": item["evidence_quality"],
                "Effective": item["effective_score"],
                "Override": item.get("analyst_override_score"),
                "Rationale": item["rationale"],
            }
            for item in items
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {f"{item['pillar_name']} - {item['item_id']}": item for item in items}
    selected = labels[st.selectbox("Override scorecard item", options=list(labels.keys()))]
    score = st.number_input("Override score", min_value=0.0, max_value=10.0, value=float(selected.get("effective_score") or 0), step=0.25)
    rationale = st.text_area("Override rationale", key="score_override_rationale")
    if st.button("Apply Score Override"):
        try:
            override_scorecard_item(selected["item_id"], override_score=score, rationale=rationale)
            st.success("Score override recorded.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _scenarios_tab(analysis_run: dict[str, Any]) -> None:
    scenarios = database.list_analysis_scenarios(analysis_run["analysis_run_id"])
    if not scenarios:
        st.info("No scenarios for this analysis run.")
        return
    st.dataframe(
        [
            {
                "Scenario": scenario["scenario_name"],
                "Implied Value": scenario.get("implied_value"),
                "Reference Price": scenario.get("reference_price"),
                "Upside/Downside": scenario.get("upside_downside"),
                "Probability": scenario.get("probability"),
                "Evidence IDs": scenario["evidence_ids_json"],
                "Warnings": scenario.get("warnings_json") or "[]",
            }
            for scenario in scenarios
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.write("Scenario probabilities")
    bear = st.number_input("Bear probability", min_value=0.0, max_value=1.0, value=float(next((s.get("probability") for s in scenarios if s["scenario_name"] == "Bear" and s.get("probability") is not None), 0.0)), step=0.05)
    base = st.number_input("Base probability", min_value=0.0, max_value=1.0, value=float(next((s.get("probability") for s in scenarios if s["scenario_name"] == "Base" and s.get("probability") is not None), 0.0)), step=0.05)
    bull = st.number_input("Bull probability", min_value=0.0, max_value=1.0, value=float(next((s.get("probability") for s in scenarios if s["scenario_name"] == "Bull" and s.get("probability") is not None), 0.0)), step=0.05)
    rationale = st.text_area("Probability rationale")
    if st.button("Save Probabilities"):
        try:
            set_scenario_probabilities(analysis_run["analysis_run_id"], {"Bear": bear, "Base": base, "Bull": bull}, rationale=rationale)
            st.success("Scenario probabilities saved.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _thesis_tab(analysis_run: dict[str, Any]) -> None:
    items = database.list_thesis_items(analysis_run["analysis_run_id"])
    if not items:
        st.info("No thesis items for this analysis run.")
        return
    st.dataframe(
        [
            {
                "Type": item["item_type"],
                "Claim": item["claim"],
                "Evidence IDs": item["evidence_ids_json"],
                "Citation Status": item["citation_status"],
                "Confidence": item["confidence"],
                "Analyst Status": item["analyst_status"],
            }
            for item in items
        ],
        hide_index=True,
        use_container_width=True,
    )


def _recommendation_tab(analysis_run: dict[str, Any]) -> None:
    decision = database.get_recommendation_decision(analysis_run["analysis_run_id"])
    if not decision:
        st.info("No recommendation decision for this analysis run.")
        return
    cols = st.columns(5)
    cols[0].metric("Preliminary", decision["preliminary_rating"])
    cols[1].metric("Effective", decision["effective_rating"])
    cols[2].metric("Confidence", decision["confidence"])
    cols[3].metric("Evidence Coverage", f"{float(decision['evidence_coverage']):.0%}")
    cols[4].metric("Status", analysis_run["status"])
    st.write(decision["recommendation_rationale"])
    st.write({"Why not Buy": decision["why_not_buy"], "Why not Hold": decision["why_not_hold"], "Why not Sell": decision["why_not_sell"]})


def _analyst_review_tab(analysis_run: dict[str, Any]) -> None:
    decision = database.get_recommendation_decision(analysis_run["analysis_run_id"])
    if not decision:
        st.info("Create an analysis run before analyst review.")
        return
    choice = st.selectbox(
        "Analyst recommendation",
        options=[
            decision["effective_rating"],
            config.RECOMMENDATION_BUY,
            config.RECOMMENDATION_HOLD,
            config.RECOMMENDATION_SELL,
            config.RECOMMENDATION_INSUFFICIENT_EVIDENCE,
            config.RECOMMENDATION_ANALYST_REVIEW_REQUIRED,
        ],
    )
    note = st.text_area("Analyst review note", value=analysis_run.get("analyst_notes") or "")
    if st.button("Mark Ready For PM Review"):
        try:
            complete_analyst_review(analysis_run["analysis_run_id"], decision=choice, note=note)
            st.success("Analyst review completed.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _pm_approval_tab(analysis_run: dict[str, Any]) -> None:
    st.write(
        "PM approval is separate from package locking and does not execute trades."
    )
    note = st.text_area("PM note", value=analysis_run.get("pm_notes") or "")
    cols = st.columns(3)
    for label, action in (("Approve", "APPROVE"), ("Reject", "REJECT"), ("Return For Revision", "RETURN_FOR_REVISION")):
        if cols[["Approve", "Reject", "Return For Revision"].index(label)].button(
            label,
            key=f"pm_{action.lower()}_{analysis_run['analysis_run_id']}",
        ):
            try:
                pm_decision(analysis_run["analysis_run_id"], action=action, note=note)
                st.success(f"PM action recorded: {action}.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def _analysis_reports_tab(analysis_run: dict[str, Any]) -> None:
    st.write("Reports")
    col1, col2 = st.columns(2)
    if col1.button("Generate Draft DOCX/PDF", type="primary"):
        try:
            report = generate_investment_report(analysis_run["analysis_run_id"], final=False)
            st.success(f"Draft report generated: V{report['report_version']:03d}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if col2.button("Generate Final DOCX/PDF", type="primary"):
        try:
            report = generate_investment_report(analysis_run["analysis_run_id"], final=True)
            st.success(f"Final report generated: V{report['report_version']:03d}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    reports = database.list_generated_reports(analysis_run["analysis_run_id"])
    if reports:
        st.dataframe(
            [
                {
                    "Version": report["report_version"],
                    "Status": report["report_status"],
                    "Recommendation": report.get("recommendation") or "",
                    "Confidence": report.get("confidence") or "",
                    "Citation Audit": report["citation_audit_status"],
                    "DOCX Hash": (report.get("docx_sha256") or "")[:12],
                    "PDF Hash": (report.get("pdf_sha256") or "")[:12],
                    "Created": report["created_at"],
                }
                for report in reports
            ],
            hide_index=True,
            use_container_width=True,
        )
        for report in reports:
            cols = st.columns(2)
            if report.get("docx_path") and Path(report["docx_path"]).exists():
                with Path(report["docx_path"]).open("rb") as handle:
                    cols[0].download_button(
                        f"DOCX V{report['report_version']:03d}",
                        data=handle.read(),
                        file_name=Path(report["docx_path"]).name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
            if report.get("pdf_path") and Path(report["pdf_path"]).exists():
                with Path(report["pdf_path"]).open("rb") as handle:
                    cols[1].download_button(
                        f"PDF V{report['report_version']:03d}",
                        data=handle.read(),
                        file_name=Path(report["pdf_path"]).name,
                        mime="application/pdf",
                    )


def main() -> None:
    bootstrap_page("Investment Analysis")
    render_sidebar()
    st.markdown('<div class="eyebrow">Phase 6</div>', unsafe_allow_html=True)
    st.title("Investment Analysis")
    st.caption("Closed-corpus evidence, deterministic analysis, analyst review, PM approval, and versioned reports. No trades are executed.")
    package = _load_or_select_package()
    if not package:
        return
    version = _select_locked_version(package)
    if not version:
        return
    _processing_controls(version)
    run = _run_history(version)
    if not run:
        return
    _summary(run)
    _resume_controls(run)
    st.divider()
    analysis_run = _analysis_controls(version, run)
    tabs = st.tabs(
        [
            "Evidence",
            "Financial Metrics",
            "Scorecard",
            "Scenarios",
            "Bull / Bear",
            "Recommendation",
            "Analyst Review",
            "PM Approval",
            "Reports",
            "Export",
        ]
    )
    with tabs[0]:
        evidence_tabs = st.tabs(["Document Explorer", "Evidence Ledger", "Conflicts", "Duplicate Lineage"])
        with evidence_tabs[0]:
            _document_explorer(version, run)
        with evidence_tabs[1]:
            _evidence_ledger(version, run)
        with evidence_tabs[2]:
            _conflicts(run)
        with evidence_tabs[3]:
            _duplicates(run)
    if not analysis_run:
        for tab in tabs[1:9]:
            with tab:
                st.info("Create an analysis run to use this section.")
    else:
        with tabs[1]:
            _analysis_metrics_tab(analysis_run)
        with tabs[2]:
            _scorecard_tab(analysis_run)
        with tabs[3]:
            _scenarios_tab(analysis_run)
        with tabs[4]:
            _thesis_tab(analysis_run)
        with tabs[5]:
            _recommendation_tab(analysis_run)
        with tabs[6]:
            _analyst_review_tab(analysis_run)
        with tabs[7]:
            _pm_approval_tab(analysis_run)
        with tabs[8]:
            _analysis_reports_tab(analysis_run)
    with tabs[9]:
        _exports(run)


if __name__ == "__main__":
    main()
