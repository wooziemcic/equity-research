from __future__ import annotations

import json
import logging

import streamlit as st

from app import config
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.package_recipe_service import (
    activate_recipe,
    approve_recipe,
    create_draft_version,
    database_audit_details,
    get_recipe,
    import_package_snapshot,
    list_recipe_slots,
    list_recipes,
    update_draft_slot,
)
from app.services.recipe_import_service import import_common_equity_recipe
from app.utils import database


logger = logging.getLogger(__name__)
SECTION_OPTIONS = {
    "Company Snapshot": "COMPANY_SNAPSHOT",
    "Licensed and Third-Party Research": "LICENSED_RESEARCH",
    "Earnings and Company Materials": "COMPANY_MATERIALS",
    "SEC Filings and Extracted Sections": "SEC_FILINGS",
    "Internal Cutler Analysis": "INTERNAL_ANALYSIS",
    "Final Memo": "FINAL_MEMO",
}
REQUIREMENT_OPTIONS = ["REQUIRED", "RECOMMENDED", "OPTIONAL", "CONDITIONAL", "MANUAL_ONLY", "REVIEW_REQUIRED"]


def _actor() -> str:
    return st.session_state.get("recipe_admin_actor", "") or "administrator"


def _import_panel() -> None:
    st.subheader("Workbook Import")
    st.caption("Imports a normalized draft. The source workbook is never modified and is not reopened during normal package work.")
    st.text_input("Administrator", key="recipe_admin_actor", placeholder="Name or initials")
    if st.button("Import Common Equity Recipe", type="primary"):
        try:
            result = import_common_equity_recipe(imported_by=_actor())
            st.session_state["selected_recipe_id"] = result["recipe_id"]
            st.success(f"Imported recipe v{result['version']} with {len(result['slots'])} normalized slots. Approval is still required.")
            st.rerun()
        except Exception as exc:
            logger.exception("Recipe import failed")
            st.error(f"Recipe import failed: {exc}")


def _import_details(recipe: dict) -> None:
    with database.get_connection(config.DATABASE_PATH) as connection:
        imported = connection.execute("SELECT * FROM recipe_imports WHERE import_id=?", (recipe.get("import_id"),)).fetchone()
    if not imported:
        return
    report = json.loads(imported["import_report_json"] or "{}")
    metrics = st.columns(4)
    metrics[0].metric("Raw rows", report.get("rows_imported", 0))
    metrics[1].metric("Normalized slots", report.get("normalized_slot_count", len(list_recipe_slots(recipe["recipe_id"]))))
    metrics[2].metric("Warnings", len(report.get("warnings", [])))
    metrics[3].metric("Unnumbered", report.get("unnumbered_supplemental_slots", 0))
    st.caption(f"Workbook: {imported['workbook_filename']} | SHA-256: {imported['workbook_sha256']} | Importer: {imported['importer_version']}")
    st.write("Available sheets:", ", ".join(json.loads(imported["available_sheets_json"])))
    st.write("Selected sources:", ", ".join(json.loads(imported["selected_sheets_json"])))
    warnings = report.get("warnings", [])
    if warnings:
        st.dataframe(warnings, hide_index=True, use_container_width=True)


def _review_table(recipe: dict) -> None:
    slots = list_recipe_slots(recipe["recipe_id"])
    editable = recipe["status"] in {"IMPORTED", "NEEDS_REVIEW"}
    rows = []
    for slot in slots:
        raw = slot.get("raw_import") or {}
        rows.append(
            {
                "Source Sheet": slot["source_sheet"],
                "Source Row": slot["source_row"],
                "Raw Order": raw.get("raw_order"),
                "Raw Name": raw.get("raw_name"),
                "Normalized Slot": slot["display_name"],
                "Section": slot["section_name"],
                "Requirement": slot["required_level"],
                "Applicability": "Conditional review" if slot["required_level"] == "CONDITIONAL" else "Long and short",
                "Preferred Source": "; ".join(slot.get("preferred_sources", [])),
                "Instructions": slot.get("instructions") or "",
                "Min": slot["minimum_documents"],
                "Max": slot["maximum_documents"],
                "Warning": slot.get("import_warning") or "",
                "Include": bool(slot["enabled"]),
                "_slot_id": slot["slot_id"],
            }
        )
    visible = [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]
    edited = st.data_editor(
        visible,
        hide_index=True,
        use_container_width=True,
        disabled=not editable,
        column_config={
            "Section": st.column_config.SelectboxColumn(options=list(SECTION_OPTIONS)),
            "Requirement": st.column_config.SelectboxColumn(options=REQUIREMENT_OPTIONS),
            "Min": st.column_config.NumberColumn(min_value=0, max_value=50),
            "Max": st.column_config.NumberColumn(min_value=1, max_value=100),
        },
        key=f"recipe_editor_{recipe['recipe_id']}",
    )
    source_comparison = []
    for slot in slots:
        for source_sheet, source in (slot.get("raw_import") or {}).get("source_differences", {}).items():
            source_comparison.append(
                {
                    "Source Sheet": source_sheet,
                    "Source Row": source.get("row"),
                    "Raw Name": source.get("name"),
                    "Normalized Slot": slot["display_name"],
                    "Raw Requirement": source.get("requirement"),
                    "Preferred Source": source.get("source"),
                    "Instructions": source.get("instructions"),
                }
            )
    with st.expander("Template / Instructions / MDT Source Differences", expanded=False):
        st.dataframe(source_comparison, hide_index=True, use_container_width=True)
    if editable and st.button("Save Review Edits"):
        records = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
        for original, changed in zip(rows, records, strict=False):
            section_name = str(changed["Section"])
            update_draft_slot(
                original["_slot_id"],
                {
                    "display_name": str(changed["Normalized Slot"]).strip(),
                    "section_code": SECTION_OPTIONS[section_name],
                    "section_name": section_name,
                    "required_level": str(changed["Requirement"]),
                    "preferred_sources_json": json.dumps([str(changed["Preferred Source"]).strip()] if str(changed["Preferred Source"]).strip() else []),
                    "instructions": str(changed["Instructions"]).strip(),
                    "minimum_documents": int(changed["Min"]),
                    "maximum_documents": max(int(changed["Min"]), int(changed["Max"])),
                    "enabled": int(bool(changed["Include"])),
                },
                actor=_actor(),
            )
        st.success("Draft recipe edits saved.")
        st.rerun()


def _recipe_actions(recipe: dict) -> None:
    cols = st.columns(3)
    if recipe["status"] in {"IMPORTED", "NEEDS_REVIEW"} and cols[0].button("Approve Recipe", type="primary"):
        approve_recipe(recipe["recipe_id"], approver=_actor())
        st.success("Recipe approved and made immutable.")
        st.rerun()
    if recipe["status"] == "APPROVED" and cols[1].button("Activate Recipe", type="primary"):
        activate_recipe(recipe["recipe_id"], actor=_actor())
        st.success("Recipe activated. New common-equity packages will snapshot this version.")
        st.rerun()
    if recipe["status"] in {"APPROVED", "ACTIVE", "SUPERSEDED", "ARCHIVED"} and cols[2].button("Create New Draft Version"):
        draft = create_draft_version(recipe["recipe_id"], created_by=_actor())
        st.session_state["selected_recipe_id"] = draft["recipe_id"]
        st.rerun()


def main() -> None:
    bootstrap_page("Recipe Administration")
    render_sidebar()
    st.markdown('<div class="eyebrow">Administrator Workflow</div>', unsafe_allow_html=True)
    st.title("Recipe Administration")
    st.write("Review workbook provenance, normalize ordered slots, and explicitly approve versioned recipes.")
    _import_panel()
    recipes = list_recipes()
    if recipes:
        labels = {f"{row['recipe_name']} v{row['version']} - {row['status']}": row["recipe_id"] for row in recipes}
        default_id = st.session_state.get("selected_recipe_id")
        default = list(labels.values()).index(default_id) if default_id in labels.values() else 0
        selected_label = st.selectbox("Recipe version", list(labels), index=default)
        recipe = get_recipe(labels[selected_label])
        if recipe:
            st.session_state["selected_recipe_id"] = recipe["recipe_id"]
            _import_details(recipe)
            _review_table(recipe)
            _recipe_actions(recipe)
    else:
        st.info("No recipe has been imported yet.")
    with st.expander("Import Package Snapshot As Separate Draft", expanded=False):
        snapshot = st.file_uploader("Package snapshot JSON", type=["json"], key="admin_snapshot_import")
        if snapshot and st.button("Import Snapshot Draft"):
            package = import_package_snapshot(snapshot.getvalue(), imported_by=_actor())
            st.success(f"Created separate draft package {package['package_id']}. The source package was not altered.")
    with st.expander("Audit Details", expanded=False):
        details = database_audit_details()
        st.json(details)
        brave = "Disabled" if config.SEARCH_PROVIDER != "brave" else "Configured" if config.brave_search_api_key() else "Not configured"
        st.write(f"Brave Search: {brave}")
        st.caption("No Brave request is available in Phase 6A.")


if __name__ == "__main__":
    main()
