from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.collectors.ir_collector import (
    IrDocumentCandidate,
    discover_public_documents,
    download_selected_ir_documents,
)
from app.services.collectors.sec_collector import (
    FilingCandidate,
    discover_dividend_exhibits,
    download_dividend_exhibits,
    download_profile_inventory,
    preview_cutler_profile,
    store_official_y15,
)
from app.services.company_resolver import resolve_package_company
from app.services.document_download_service import DocumentDownloadError, create_public_documents_zip, get_document_download
from app.services.upload_service import (
    DOCUMENT_TYPE_OPTIONS,
    SOURCE_ALIASES,
    UploadCandidate,
    prepare_batch_review,
    store_reviewed_upload_batch,
)
from app.utils import database
from app.utils.database import DatabaseError

logger = logging.getLogger(__name__)


def _load_active_package() -> dict | None:
    package = st.session_state.get("active_package")
    active_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if package and package.get("package_id") == active_id:
        return package
    if active_id:
        loaded = database.get_package_by_package_id(active_id)
        if loaded:
            st.session_state["active_package"] = loaded
            return loaded
    return None


def _select_package_ui() -> dict | None:
    packages = database.list_packages()
    if not packages:
        render_empty_state(
            "No packages are available.",
            "Create a research package before starting public document collection.",
        )
        try:
            st.page_link("pages/1_New_Research_Package.py", label="Create New Research Package")
        except StreamlitPageNotFoundError:
            st.caption("Create New Research Package")
        return None
    labels = {
        f"{package['ticker']} - {package['package_id']} ({package['research_cutoff_date']})": package
        for package in packages
    }
    selected = st.selectbox("Select an existing package", options=list(labels.keys()))
    if st.button("Use Selected Package", type="primary"):
        package = labels[selected]
        st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
        st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
        st.session_state["active_package"] = package
        st.rerun()
    return None


def _refresh_active_package(package_id: str) -> dict:
    package = database.get_package_by_package_id(package_id)
    if package:
        st.session_state["active_package"] = package
    return package or {}


def _company_identity(package: dict) -> dict:
    st.subheader("Company Identity")
    if not config.sec_user_agent_is_configured():
        st.warning(
            "SEC_USER_AGENT is not configured. Set it before resolving companies or collecting SEC filings."
        )
    cols = st.columns(4)
    cols[0].metric("Ticker", package.get("ticker", ""))
    cols[1].metric("CIK", package.get("cik") or "Unresolved")
    cols[2].metric("Security", package.get("security_type", ""))
    cols[3].metric("Cutoff", package.get("research_cutoff_date", ""))
    identity_cols = st.columns(5)
    for column, (label, value) in zip(
        identity_cols,
        (
            ("Company", package.get("company_name") or "Pending"),
            ("Exchange", package.get("exchange") or "Not available"),
            ("Industry", package.get("industry_description") or "Not available"),
            ("Fiscal year end", package.get("fiscal_year_end") or "Not available"),
            ("Resolution", package.get("resolution_status") or "UNRESOLVED"),
        ),
        strict=False,
    ):
        column.caption(label)
        column.write(value)
    action_cols = st.columns(3)
    with action_cols[0]:
        if st.button("Resolve Company", disabled=not config.sec_user_agent_is_configured()):
            result = resolve_package_company(package)
            if result.status == "RESOLVED":
                st.success("Company metadata resolved and saved.")
                package = _refresh_active_package(package["package_id"])
            elif result.status == "MULTIPLE_MATCHES":
                st.session_state["company_resolution_candidates"] = result.candidates or []
                st.warning("Multiple SEC records matched this ticker. Select the intended company record below.")
            else:
                st.error(result.error or "Company could not be resolved.")
    candidates = st.session_state.get("company_resolution_candidates", [])
    if candidates:
        labels = {
            f"{row.get('ticker')} - {row.get('name')} - CIK {row.get('cik', row.get('cik_str'))}": row
            for row in candidates
        }
        selected_label = st.selectbox("SEC candidate", options=list(labels.keys()))
        if st.button("Resolve Selected SEC Record"):
            selected = labels[selected_label]
            result = resolve_package_company(
                package,
                selected_cik=str(selected.get("cik", selected.get("cik_str"))),
            )
            if result.status == "RESOLVED":
                st.session_state["company_resolution_candidates"] = []
                st.success("Selected SEC company metadata saved.")
                package = _refresh_active_package(package["package_id"])
            else:
                st.error(result.error or "Selected SEC record could not be resolved.")
    with action_cols[1]:
        if st.button("Refresh Company Metadata", disabled=not config.sec_user_agent_is_configured()):
            result = resolve_package_company(package, refresh=True)
            if result.status == "RESOLVED":
                st.success("Company metadata refreshed.")
                package = _refresh_active_package(package["package_id"])
            else:
                st.error(result.error or "Company metadata could not be refreshed.")
    with action_cols[2]:
        if st.button("Select Different Package"):
            st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = None
            st.session_state["active_package"] = None
            st.rerun()
    return package


def _sec_collection(package: dict) -> None:
    st.subheader("Cutler collection scope")
    st.caption(package.get("collection_profile_name") or "CUTLER_EQUITY_INTERN_GUIDE")
    scope_cols = st.columns(2)
    scope_cols[0].markdown("**Required**  \n10-K · 10-Q · 8-K · S-3 · S-4 · DEF 14A")
    scope_cols[1].markdown("**Conditional**  \nSelected Form 144 · Dividend announcements · Y-15 when discovered")
    if not package.get("cik"):
        st.info("Resolve the company before previewing SEC filings.")
        return
    enabled = {"10-K", "10-Q", "8-K", "S-3", "S-4", "DEF 14A", "144"}
    with st.expander("Advanced"):
        adjusted = st.multiselect(
            "Included form families",
            options=list(config.SEC_SUPPORTED_FORMS),
            default=list(config.SEC_SUPPORTED_FORMS),
        )
        enabled = set(adjusted)
        y15_url = st.text_input("Official Y-15 direct link", placeholder="https://www.federalreserve.gov/...")
        if st.button("Store Official Y-15", disabled=not y15_url):
            try:
                store_official_y15(package, y15_url)
                st.success("Y-15 regulatory report stored from the official source.")
            except Exception as exc:
                st.error(f"Y-15 could not be stored: {exc}")
    st.caption(
        f"Allowed date range is derived from the package: {package['filing_history_years']} year(s) through {package['research_cutoff_date']}."
    )
    if st.button("Retrieve Filing Inventory", disabled=not enabled):
        try:
            st.session_state["sec_preview"] = preview_cutler_profile(package, enabled_families=enabled)
        except Exception as exc:
            logger.exception("SEC preview failed")
            st.error(f"SEC filings could not be previewed: {exc}")

    candidates: list[FilingCandidate] = st.session_state.get("sec_preview", [])
    if not candidates:
        return
    regular = [item for item in candidates if item.normalized_form_family != "144" and item.inventory_status != "EXCLUDED_BY_PROFILE"]
    excluded = [item for item in candidates if item.inventory_status == "EXCLUDED_BY_PROFILE"]
    form_144 = [item for item in candidates if item.normalized_form_family == "144"]
    metric_cols = st.columns(4)
    metric_cols[0].metric("Discovered", len(candidates))
    metric_cols[1].metric("Eligible", len(regular) + len(form_144))
    metric_cols[2].metric("Already collected", sum(item.inventory_status == "ALREADY_COLLECTED" for item in candidates))
    metric_cols[3].metric("Excluded by profile", len(excluded))
    rows = [
        {
            "Selected": item.selected,
            "Form family": item.normalized_form_family,
            "Original form": item.form_type,
            "Filing Date": item.filing_date,
            "Collection source": "SEC EDGAR",
            "Conditional rule": "Included by profile",
            "Already collected": item.inventory_status == "ALREADY_COLLECTED",
            "Newly discoverable": item.inventory_status == "ELIGIBLE",
        }
        for item in regular
    ]
    edited_regular = st.data_editor(rows, hide_index=True, use_container_width=True, disabled=[key for key in rows[0] if key != "Selected"] if rows else True)
    selected_regular = edited_regular.to_dict("records") if hasattr(edited_regular, "to_dict") else edited_regular
    selection_by_identity = {
        (item.accession_number, item.primary_document): bool(row["Selected"])
        for item, row in zip(regular, selected_regular, strict=False)
    }
    if form_144:
        st.markdown("**Form 144 selection**")
        rows_144 = [
            {"Selected": item.selected, "Filing date": item.filing_date, "Reporting person": item.reporting_person,
             "Security": item.security, "Shares": item.shares, "Aggregate market value": item.aggregate_market_value,
             "Issuer": item.issuer, "Source link": item.primary_document_url}
            for item in form_144
        ]
        edited_144 = st.data_editor(rows_144, hide_index=True, use_container_width=True, disabled=[key for key in rows_144[0] if key != "Selected"])
        selected_144 = edited_144.to_dict("records") if hasattr(edited_144, "to_dict") else edited_144
        selection_by_identity.update(
            {
                (item.accession_number, item.primary_document): bool(row["Selected"])
                for item, row in zip(form_144, selected_144, strict=False)
            }
        )
    if excluded:
        with st.expander(f"Inventory diagnostics · {len(excluded)} excluded"):
            st.dataframe(
                [{"Original form": item.form_type, "Filing date": item.filing_date, "Status": "Excluded by profile", "Accession": item.accession_number} for item in excluded],
                hide_index=True, use_container_width=True,
            )
    candidates = [
        replace(item, selected=selection_by_identity.get((item.accession_number, item.primary_document), item.selected))
        for item in candidates
    ]
    selected_count = sum(item.selected for item in candidates)
    if st.button("Download Selected SEC Filings", disabled=not selected_count):
        summary = download_profile_inventory(package, candidates)
        dividend_summary = {"downloaded_now": 0}
        for filing in candidates:
            if filing.selected and filing.normalized_form_family == "8-K":
                exhibits = discover_dividend_exhibits(package, filing)
                result = download_dividend_exhibits(package, exhibits)
                dividend_summary["downloaded_now"] += result["downloaded_now"]
        st.success(
            f"SEC run: {summary['downloaded_now']} downloaded now, {summary['already_collected']} already collected, "
            f"{summary['excluded_by_profile']} excluded by profile, {summary['awaiting_form_144_selection']} awaiting Form 144 selection, "
            f"{summary['duplicate']} duplicate, {summary['failed']} failed, {summary['not_found']} not found; "
            f"{dividend_summary['downloaded_now']} dividend exhibit(s)."
        )
        _refresh_active_package(package["package_id"])


def _ir_collection(package: dict) -> None:
    st.subheader("Investor-Relations Collection")
    ir_url = st.text_input("Investor-relations URL", placeholder="https://investors.example.com")
    if st.button("Discover Public Documents", disabled=not ir_url):
        candidates, message = discover_public_documents(ir_url)
        st.session_state["ir_candidates"] = candidates
        if message:
            st.warning(message)
        elif not candidates:
            st.info("No public PDF documents were discovered within the conservative crawl limits.")

    candidates: list[IrDocumentCandidate] = st.session_state.get("ir_candidates", [])
    if not candidates:
        return
    st.dataframe(
        [
            {
                "Title": item.title,
                "Suggested Category": item.suggested_category,
                "Apparent Date": item.apparent_date,
                "Confidence": item.confidence,
                "URL": item.url,
            }
            for item in candidates
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {f"{item.title} - {item.url}": item for item in candidates}
    selected = st.multiselect("Select IR documents to download", options=list(labels.keys()))
    selections: list[tuple[IrDocumentCandidate, str]] = []
    for label in selected:
        item = labels[label]
        category = st.selectbox(
            f"Category for {item.title[:60]}",
            options=[
                item.suggested_category,
                "Earnings Release",
                "Investor Presentation",
                "Annual Report",
                "Investor Day",
                "Supplemental Financials",
                "Press Release",
                "ESG / Sustainability",
                "Public Document",
            ],
            key=f"ir_category_{item.url}",
        )
        selections.append((item, category))
    if st.button("Download Selected IR Documents", disabled=not selections):
        summary = download_selected_ir_documents(package, selections)
        st.success(
            f"IR run complete: {summary['downloaded_now']} downloaded now, {summary['already_collected']} already collected, {summary['duplicate']} duplicate, {summary['failed']} failed, {summary['not_found']} not found."
        )
        _refresh_active_package(package["package_id"])


def _licensed_uploads(package: dict) -> None:
    st.subheader("Additional Research")
    files = st.file_uploader(
        "Upload authorized files",
        accept_multiple_files=True,
        type=[ext.lstrip(".") for ext in config.SUPPORTED_UPLOAD_EXTENSIONS],
    )
    if not files:
        return
    candidates = [
        UploadCandidate(file.name, file.getvalue(), getattr(file, "type", ""))
        for file in files
    ]
    signature = tuple((item.original_filename, len(item.content)) for item in candidates)
    if st.session_state.get("batch_review_signature") != signature:
        st.session_state["batch_review_signature"] = signature
        st.session_state["batch_review_rows"] = prepare_batch_review(package, candidates)
    reviews = st.session_state["batch_review_rows"]
    valid_count = sum(row["Validation status"] == "Valid" for row in reviews)
    st.caption(f"{len(reviews)} selected · {valid_count} valid · {len(reviews) - valid_count} invalid")
    bulk = st.columns(5)
    if bulk[0].button("Include valid"):
        for row in reviews:
            row["Include"] = row["Validation status"] == "Valid"
    if bulk[1].button("Exclude duplicates"):
        for row in reviews:
            if row["Duplicate status"] != "Unique":
                row["Include"] = False
    source_values = [source for source, _ in SOURCE_ALIASES] + ["UNKNOWN_SOURCE"]
    bulk_source = bulk[2].selectbox("Source", source_values, label_visibility="collapsed")
    if bulk[2].button("Apply source"):
        for row in reviews:
            if row["Include"]:
                row["Final source"] = bulk_source
    type_values = list(DOCUMENT_TYPE_OPTIONS.values())
    bulk_type = bulk[3].selectbox("Type", type_values, label_visibility="collapsed")
    if bulk[3].button("Apply type"):
        for row in reviews:
            if row["Include"]:
                row["Final document type"] = bulk_type
    if bulk[4].button("Clear corrections"):
        for row in reviews:
            row["Final source"] = row["Inferred source"]
            row["Final document type"] = row["Inferred document type"]
            row["Document date"] = ""
            row["Notes"] = ""
    visible_columns = [key for key in reviews[0] if not key.startswith("_")]
    visible_rows = [{key: row[key] for key in visible_columns} for row in reviews]
    editable = {"Include", "Final source", "Final document type", "Document date", "Notes"}
    edited = st.data_editor(
        visible_rows,
        hide_index=True,
        use_container_width=True,
        disabled=[column for column in visible_columns if column not in editable],
        column_config={
            "Final source": st.column_config.SelectboxColumn(options=source_values),
            "Final document type": st.column_config.SelectboxColumn(options=type_values),
        },
    )
    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
    merged_reviews = [{**reviews[index], **row} for index, row in enumerate(edited_rows)]
    authorized = st.checkbox(
        "I confirm that these files are authorized for internal use and that their storage complies with Cutler Capital's vendor entitlements."
    )
    if st.button("Upload Accepted Research Files", type="primary", disabled=not authorized):
        summary = store_reviewed_upload_batch(
            package,
            candidates,
            merged_reviews,
            authorization_confirmed=authorized,
        )
        st.success(
            f"Batch complete: {summary['accepted']} accepted, {summary['uploaded']} uploaded, {summary['duplicates']} duplicates, "
            f"{summary['excluded']} excluded, {summary['failed']} failed, {summary['bytes']} bytes."
        )


def _collected_documents(package: dict) -> None:
    st.subheader("Collected Documents")
    documents = database.list_documents_by_package(package["package_id"])
    if not documents:
        render_empty_state(
            "No public documents have been collected for this package.",
            "Preview SEC filings or discover investor-relations PDFs to begin collection.",
        )
        return
    public_documents = [
        doc for doc in documents
        if int(doc.get("is_public") or 0) and doc.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
    ]
    if public_documents and st.button("Download All Collected Public Files", use_container_width=True):
        try:
            content, filename, included, missing = create_public_documents_zip(package["package_id"])
            st.download_button(
                "Download Public Files ZIP",
                data=content,
                file_name=filename,
                mime="application/zip",
                use_container_width=True,
            )
            if missing:
                st.warning(f"The ZIP omitted {missing} file(s) that are no longer present in managed storage.")
            st.caption(f"Prepared {included} public file(s) from this package.")
        except Exception as exc:
            logger.exception("Public document ZIP failed")
            st.error(f"Public files could not be prepared: {exc}")
    source_filter = st.multiselect(
        "Source filter",
        sorted({doc["source_name"] for doc in documents if doc.get("source_name")}),
    )
    category_filter = st.multiselect(
        "Category filter",
        sorted({doc["category"] for doc in documents if doc.get("category")}),
    )
    status_filter = st.multiselect(
        "Status filter",
        sorted({doc["collection_status"] for doc in documents if doc.get("collection_status")}),
    )
    filtered = documents
    if source_filter:
        filtered = [doc for doc in filtered if doc["source_name"] in source_filter]
    if category_filter:
        filtered = [doc for doc in filtered if doc["category"] in category_filter]
    if status_filter:
        filtered = [doc for doc in filtered if doc["collection_status"] in status_filter]
    st.dataframe(
        [
            {
                "Title": doc["title"],
                "Category": doc["category"],
                "Type": doc["document_type"],
                "Source": doc["source_name"],
                "Date": doc.get("publication_date") or "",
                "Size": doc.get("file_size_bytes") or "",
                "Status": doc["collection_status"],
                "Hash": (doc.get("sha256_hash") or "")[:12],
                "Local": "Available" if doc.get("local_path") and Path(doc["local_path"]).exists() else "",
                "Source URL": doc["source_url"],
            }
            for doc in filtered
        ],
        hide_index=True,
        use_container_width=True,
    )
    for document in filtered:
        if document.get("collection_status") != config.DOCUMENT_STATUS_DOWNLOADED:
            continue
        label = document.get("title") or document.get("local_filename") or document["document_id"]
        with st.expander(f"View / Preview: {label}", expanded=False):
            try:
                download = get_document_download(package["package_id"], document["document_id"])
                st.download_button(
                    "Download",
                    data=download.content,
                    file_name=download.filename,
                    mime=download.mime_type,
                    key=f"download_collected_{document['document_id']}",
                    use_container_width=True,
                )
                if download.source_url:
                    st.link_button("Open Original Source", download.source_url, use_container_width=True)
                if download.mime_type == "text/html":
                    st.caption("Safe source preview")
                    st.code(download.content[:12000].decode("utf-8", errors="replace"), language="html")
                elif download.mime_type == "application/pdf":
                    st.caption("PDF document. Use Download to open the original PDF file.")
                else:
                    st.caption(f"{download.filename} ({download.mime_type})")
            except DocumentDownloadError as exc:
                st.error(str(exc))


def _collection_history(package: dict) -> None:
    st.subheader("Collection History")
    runs = database.list_recent_collection_runs(package["package_id"])
    if not runs:
        st.info("No collection runs have been recorded for this package.")
        return
    st.dataframe(
        [
            {
                "Source": run["source_type"],
                "Started": run["started_at"],
                "Completed": run.get("completed_at") or "",
                "Status": run["status"],
                "Discovered": run["documents_discovered"],
                "Eligible": run.get("documents_eligible", 0),
                "Downloaded now": run["documents_downloaded"],
                "Already collected": run.get("documents_already_collected", 0),
                "Excluded by profile": run.get("documents_excluded_profile", 0),
                "Awaiting Form 144": run.get("documents_awaiting_selection", 0),
                "Duplicate": run.get("documents_duplicated", 0),
                "Not found": run.get("documents_not_found", 0),
                "Failed": run["documents_failed"],
            }
            for run in runs
        ],
        hide_index=True,
        use_container_width=True,
    )


def main() -> None:
    bootstrap_page("Public Collection")
    render_sidebar()
    st.markdown('<div class="eyebrow">Phase 2</div>', unsafe_allow_html=True)
    st.title("Public Document Collection")
    st.write("Resolve SEC identity, preview public filings, and collect approved public documents.")
    try:
        package = _load_active_package()
        if not package:
            st.info("Select an existing research package to begin public collection.")
            _select_package_ui()
            return
        package = _company_identity(package)
        st.divider()
        _sec_collection(package)
        st.divider()
        _licensed_uploads(package)
        st.divider()
        _collected_documents(package)
        st.divider()
        _collection_history(package)
    except DatabaseError:
        logger.exception("Document collection page failed")
        st.error("The collection workspace could not load. Check the database and try again.")


if __name__ == "__main__":
    main()
