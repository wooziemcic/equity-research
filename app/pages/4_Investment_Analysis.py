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
from app.services.evidence_service import create_analyst_evidence_from_chunk, verify_evidence_record
from app.services.evidence_taxonomy import evidence_type_options
from app.services.processing_pipeline import run_processing_pipeline, validate_processing_eligibility
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
    labels = {f"{version['version_id']} ({version.get('integrity_status')})": version for version in versions}
    selected = st.selectbox("Version", options=list(labels.keys()))
    version = labels[selected]
    docs = database.list_package_version_documents(version["version_id"])
    cols = st.columns(5)
    cols[0].metric("Version ID", version["version_id"])
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
            run = run_processing_pipeline(version["version_id"], ocr_enabled=ocr_enabled, retrieval_mode=retrieval_mode)
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


def main() -> None:
    bootstrap_page("Investment Analysis")
    render_sidebar()
    st.markdown('<div class="eyebrow">Phase 5</div>', unsafe_allow_html=True)
    st.title("Evidence & Document Intelligence")
    st.caption("Closed-corpus processing of locked package versions. Investment recommendations begin in Phase 6.")
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
    tab_docs, tab_evidence, tab_conflicts, tab_duplicates, tab_exports = st.tabs(
        ["Document Explorer", "Evidence Ledger", "Conflicts", "Duplicate Lineage", "Export"]
    )
    with tab_docs:
        _document_explorer(version, run)
    with tab_evidence:
        _evidence_ledger(version, run)
    with tab_conflicts:
        _conflicts(run)
    with tab_duplicates:
        _duplicates(run)
    with tab_exports:
        _exports(run)


if __name__ == "__main__":
    main()
