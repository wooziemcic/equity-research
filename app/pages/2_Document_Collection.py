from __future__ import annotations

import logging
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
    download_selected_filings,
    preview_filings,
)
from app.services.company_resolver import resolve_package_company
from app.services.document_download_service import DocumentDownloadError, create_public_documents_zip, get_document_download
from app.services.taxonomy import category_options
from app.services.upload_service import (
    UploadCandidate,
    inspect_zip_upload,
    store_uploaded_files,
    validate_upload_batch,
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
    st.write(
        {
            "Company": package.get("company_name") or "Company resolution pending",
            "Exchange": package.get("exchange") or "",
            "Industry": package.get("industry_description") or "",
            "Fiscal Year End": package.get("fiscal_year_end") or "",
            "Resolution": package.get("resolution_status") or "UNRESOLVED",
        }
    )
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
    st.subheader("SEC Collection")
    if not package.get("cik"):
        st.info("Resolve the company before previewing SEC filings.")
        return
    forms = st.multiselect(
        "Filing types",
        options=list(config.SEC_SUPPORTED_FORMS),
        default=["10-K", "10-Q", "8-K"],
    )
    st.caption(
        f"Allowed date range is derived from the package: {package['filing_history_years']} year(s) through {package['research_cutoff_date']}."
    )
    if st.button("Preview Available SEC Filings", disabled=not forms):
        try:
            st.session_state["sec_preview"] = preview_filings(package, forms)
        except Exception as exc:
            logger.exception("SEC preview failed")
            st.error(f"SEC filings could not be previewed: {exc}")

    candidates: list[FilingCandidate] = st.session_state.get("sec_preview", [])
    if not candidates:
        return
    rows = [
        {
            "Accession": item.accession_number,
            "Form": item.form_type,
            "Filing Date": item.filing_date,
            "Report Period": item.report_period,
            "Primary Document": item.primary_document,
        }
        for item in candidates
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    labels = {f"{item.form_type} {item.filing_date} {item.accession_number}": item for item in candidates}
    selected = st.multiselect("Select filings to download", options=list(labels.keys()))
    if st.button("Download Selected SEC Filings", disabled=not selected):
        summary = download_selected_filings(package, [labels[label] for label in selected])
        st.success(
            f"SEC run complete: {summary['downloaded_now']} downloaded now, {summary['already_collected']} already collected, {summary['duplicate']} duplicate, {summary['failed']} failed, {summary['not_found']} not found."
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
    st.subheader("Licensed File Uploads")
    st.info(
        "Uploaded licensed materials remain within this local research workspace and are not analyzed by an external model in Phase 3."
    )
    source_type = st.selectbox(
        "Source type",
        options=list(config.LICENSED_SOURCE_TYPES.keys()),
        format_func=lambda value: config.LICENSED_SOURCE_TYPES[value],
    )
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
    validations = validate_upload_batch(candidates, source_type=source_type)
    category_lookup = dict(category_options())
    st.write("Upload preview")
    st.dataframe(
        [
            {
                "File": result.original_filename,
                "Size": result.file_size_bytes,
                "Valid": result.is_valid,
                "Detected": result.detected_file_type,
                "Suggested": result.classification.category_display if result.classification else "",
                "Confidence": result.classification.confidence if result.classification else "",
                "Error": result.error,
            }
            for result in validations
        ],
        hide_index=True,
        use_container_width=True,
    )
    metadata_by_name: dict[str, dict] = {}
    for result in validations:
        if not result.is_valid:
            continue
        st.markdown(f"**{result.original_filename}**")
        default_category = result.classification.category_code if result.classification else "other"
        category_codes = list(category_lookup.keys())
        selected_code = st.selectbox(
            "Final category",
            options=category_codes,
            index=category_codes.index(default_category) if default_category in category_codes else 0,
            format_func=lambda code: category_lookup[code],
            key=f"upload_category_{result.sha256_hash}",
        )
        col1, col2 = st.columns(2)
        with col1:
            title = st.text_input("Document title", value=Path(result.original_filename).stem, key=f"title_{result.sha256_hash}")
            publication_date = st.date_input("Publication date", value=None, key=f"pub_{result.sha256_hash}")
        with col2:
            source_institution = st.text_input("Source institution", value=config.LICENSED_SOURCE_TYPES[source_type], key=f"source_inst_{result.sha256_hash}")
            document_date = st.date_input("Document as-of date", value=None, key=f"docdate_{result.sha256_hash}")
        notes = st.text_area("Analyst notes", key=f"notes_{result.sha256_hash}")
        metadata_by_name[result.original_filename] = {
            "final_category_code": selected_code,
            "title": title,
            "publication_date": publication_date.isoformat() if publication_date else None,
            "source_institution": source_institution,
            "document_date": document_date.isoformat() if document_date else None,
            "analyst_notes": notes,
        }
        if result.extension == ".zip":
            try:
                st.caption("ZIP inspection")
                st.dataframe(
                    [entry.__dict__ for entry in inspect_zip_upload(candidates[[c.original_filename for c in candidates].index(result.original_filename)].content)],
                    hide_index=True,
                    use_container_width=True,
                )
                st.caption("Phase 3 stores ZIP files archive-only after inspection. Automatic extraction is not enabled.")
            except Exception as exc:
                st.warning(f"ZIP inspection failed: {exc}")
    authorized = st.checkbox(
        "I confirm that these files are authorized for internal use and that their storage complies with Cutler Capital's vendor entitlements."
    )
    if st.button("Upload Accepted Research Files", type="primary", disabled=not authorized):
        summary = store_uploaded_files(
            package,
            candidates,
            source_type=source_type,
            authorization_confirmed=authorized,
            metadata_by_name=metadata_by_name,
        )
        st.success(
            f"Upload run complete: {summary['uploaded']} uploaded, {summary['duplicated']} duplicate, {summary['failed']} failed."
        )
        from app.services.checklist_service import ensure_package_checklist

        ensure_package_checklist(_refresh_active_package(package["package_id"]))


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
                "Downloaded now": run["documents_downloaded"],
                "Already collected": run.get("documents_already_collected", 0),
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
        _ir_collection(package)
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
