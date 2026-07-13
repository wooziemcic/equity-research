from __future__ import annotations

import logging
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.checklist_service import coverage_summary, ensure_package_checklist, recategorize_document, set_override
from app.services.taxonomy import category_options
from app.services.upload_service import remove_uploaded_document
from app.utils import database

logger = logging.getLogger(__name__)


def _load_or_select_package() -> dict | None:
    active_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if active_id:
        package = database.get_package_by_package_id(active_id)
        if package:
            st.session_state["active_package"] = package
            return package
    packages = database.list_packages()
    if not packages:
        render_empty_state("No packages available.", "Create a package before review.")
        return None
    labels = {f"{p['ticker']} - {p['package_id']}": p for p in packages}
    selected = st.selectbox("Select package for review", options=list(labels.keys()))
    if st.button("Review Selected Package", type="primary"):
        package = labels[selected]
        st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
        st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
        st.session_state["active_package"] = package
        st.rerun()
    return None


def _safe_link(page: str, label: str) -> None:
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def _package_summary(package: dict) -> None:
    counts = database.document_counts_for_package(package["package_id"])
    st.subheader("Package Summary")
    cols = st.columns(4)
    cols[0].metric("Ticker", package.get("ticker", ""))
    cols[1].metric("Public Documents", counts["public"])
    cols[2].metric("Licensed Documents", counts["licensed"])
    cols[3].metric("Total Files", counts["total"])
    cols2 = st.columns(4)
    cols2[0].metric("Total Size", f"{counts['bytes'] / (1024 * 1024):.2f} MB")
    cols2[1].metric("Duplicates", counts["duplicates"])
    cols2[2].metric("Failed Items", counts["failed"])
    cols2[3].metric("Security", package.get("security_type", ""))
    st.write(
        {
            "Company": package.get("company_name") or "Company resolution pending",
            "Package ID": package["package_id"],
            "Research Cutoff": package["research_cutoff_date"],
        }
    )


def _checklist(package: dict) -> list[dict]:
    st.subheader("Research Package Coverage")
    items = ensure_package_checklist(package)
    summary = coverage_summary(items)
    cols = st.columns(6)
    cols[0].metric("Core Available", summary["required_available"])
    cols[1].metric("Recommended Available", summary["recommended_available"])
    cols[2].metric("Optional Available", summary["optional_available"])
    cols[3].metric("Missing", summary["missing"])
    cols[4].metric("Needs Review", summary["needs_review"])
    cols[5].metric("Not Applicable", summary["not_applicable"])
    st.caption("Research Package Coverage is a document coverage measure, not investment confidence.")
    groups = sorted({item["checklist_group"] for item in items})
    for group in groups:
        with st.expander(group, expanded=True):
            for item in [entry for entry in items if entry["checklist_group"] == group]:
                cols = st.columns([2.5, 1.1, 1.2, 1.1, 2.0])
                cols[0].write(item["display_name"])
                cols[1].write(item["requirement_level"].title())
                cols[2].write(item["effective_status"].replace("_", " ").title())
                cols[3].write(item["matched_document_count"])
                note = cols[4].text_input(
                    "Note",
                    value=item.get("analyst_note") or "",
                    key=f"note_{item['checklist_item_id']}",
                    label_visibility="collapsed",
                )
                action = st.selectbox(
                    f"Action for {item['display_name']}",
                    options=[
                        "",
                        config.CHECKLIST_STATUS_NOT_AVAILABLE,
                        config.CHECKLIST_STATUS_NOT_APPLICABLE,
                        config.CHECKLIST_STATUS_NEEDS_REVIEW,
                        config.CHECKLIST_STATUS_STALE,
                        "RESTORE_AUTOMATIC",
                    ],
                    format_func=lambda value: "No change" if not value else value.replace("_", " ").title(),
                    key=f"override_{item['checklist_item_id']}",
                )
                if action and st.button("Apply", key=f"apply_{item['checklist_item_id']}"):
                    set_override(
                        package["package_id"],
                        item["checklist_item_id"],
                        None if action == "RESTORE_AUTOMATIC" else action,
                        note,
                    )
                    st.rerun()
    return items


def _document_inventory(package: dict) -> None:
    st.subheader("Document Inventory")
    documents = database.list_documents_by_package(package["package_id"])
    if not documents:
        render_empty_state("No documents are associated with this package.", "Use Public Collection or Licensed File Uploads to add documents.")
        return
    category_lookup = dict(category_options())
    public_filter = st.selectbox("Public / Licensed", options=["All", "Public", "Licensed"])
    filtered = documents
    if public_filter == "Public":
        filtered = [doc for doc in filtered if int(doc.get("is_public", 0))]
    elif public_filter == "Licensed":
        filtered = [doc for doc in filtered if not int(doc.get("is_public", 0))]
    st.dataframe(
        [
            {
                "Title": doc["title"],
                "Public": bool(doc["is_public"]),
                "Category": doc.get("category"),
                "Source": doc.get("source_name"),
                "Type": doc.get("document_type"),
                "Status": doc.get("collection_status"),
                "Date": doc.get("publication_date") or doc.get("document_date") or "",
                "Hash": (doc.get("sha256_hash") or "")[:12],
            }
            for doc in filtered
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.write("Edit licensed document metadata")
    editable = [doc for doc in filtered if not int(doc.get("is_public", 0)) and doc.get("collection_status") != "DELETED"]
    if not editable:
        st.caption("No editable licensed documents in the current filter.")
        return
    labels = {f"{doc['title']} - {doc['document_id']}": doc for doc in editable}
    selected_label = st.selectbox("Document", options=list(labels.keys()))
    document = labels[selected_label]
    title = st.text_input("Title", value=document.get("title") or "")
    category_codes = list(category_lookup.keys())
    current = document.get("final_category_code") or "other"
    final_code = st.selectbox(
        "Category",
        options=category_codes,
        index=category_codes.index(current) if current in category_codes else category_codes.index("other"),
        format_func=lambda code: category_lookup[code],
    )
    source_institution = st.text_input("Source institution", value=document.get("source_institution") or "")
    analyst_notes = st.text_area("Analyst notes", value=document.get("analyst_notes") or "")
    if st.button("Save Metadata Changes"):
        recategorize_document(
            package,
            document["document_id"],
            final_code,
            title=title,
            source_institution=source_institution,
            analyst_notes=analyst_notes,
        )
        st.success("Document metadata updated and checklist recalculated.")
        st.rerun()
    confirm_delete = st.checkbox(f"Confirm deletion of {document.get('stored_filename') or document.get('local_filename')}")
    if st.button("Delete Uploaded File", disabled=not confirm_delete):
        remove_uploaded_document(document, confirm=confirm_delete)
        ensure_package_checklist(package)
        st.warning("Uploaded file deleted and audit event recorded.")
        st.rerun()


def _missing_panel(package: dict, items: list[dict]) -> None:
    st.subheader("Missing Documents")
    missing = [
        item
        for item in items
        if item["effective_status"] == config.CHECKLIST_STATUS_MISSING
        and item["requirement_level"] in {"required", "recommended"}
    ]
    if not missing:
        st.success("No missing core or recommended checklist items.")
        return
    st.dataframe(
        [
            {
                "Item": item["display_name"],
                "Level": item["requirement_level"],
                "Group": item["checklist_group"],
            }
            for item in missing
        ],
        hide_index=True,
        use_container_width=True,
    )
    col1, col2 = st.columns(2)
    with col1:
        _safe_link("pages/2_Document_Collection.py", "Go to Public Collection / Add Licensed Files")
    with col2:
        st.caption("Use checklist actions above to mark Not Available or Not Applicable with notes.")


def main() -> None:
    bootstrap_page("Package Review")
    render_sidebar()
    st.markdown('<div class="eyebrow">Phase 3</div>', unsafe_allow_html=True)
    st.title("Package Review")
    package = _load_or_select_package()
    if not package:
        return
    _package_summary(package)
    st.divider()
    items = _checklist(package)
    st.divider()
    _document_inventory(package)
    st.divider()
    _missing_panel(package, items)


if __name__ == "__main__":
    main()
