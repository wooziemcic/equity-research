from __future__ import annotations

import html
import logging
from datetime import date
from pathlib import Path

import streamlit as st

from app import config
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.package_discovery_service import (
    CompletenessAgent,
    RecipePlannerAgent,
    approve_and_download_candidate,
    decide_candidate,
    discovery_audit_details,
    discovery_preview,
    get_earnings_anchor,
    latest_discovery_run,
    list_discovery_candidates,
    resume_discovery,
    run_discovery,
    stop_discovery,
)
from app.services.package_recipe_service import (
    add_slot_note,
    assign_document,
    board_payload,
    clone_legacy_package,
    export_checklist_xlsx,
    export_package_snapshot,
    mark_slot,
    replace_assignment,
    set_highlighted,
    suggest_document_assignments,
    update_assignment,
    update_package_header,
)
from app.services.upload_service import UploadCandidate, prepare_batch_review, store_reviewed_upload_batch
from app.utils import database


logger = logging.getLogger(__name__)


@st.cache_data(show_spinner=False)
def _cached_checklist(package_id: str, state_signature: tuple, actor: str) -> bytes:
    return export_checklist_xlsx(package_id, actor=actor)


@st.cache_data(show_spinner=False)
def _cached_snapshot(package_id: str, state_signature: tuple) -> bytes:
    return export_package_snapshot(package_id)


def _actor() -> str:
    return st.session_state.get("assembly_actor", "") or "analyst"


def _active_package() -> dict | None:
    package_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if package_id:
        package = database.get_package_by_package_id(package_id)
        if package:
            st.session_state["active_package"] = package
            return package
    return None


def _select_package() -> dict | None:
    packages = database.list_packages(limit=100)
    if not packages:
        st.info("No research package is available.")
        return None
    labels = {f"{row['ticker']} - {row.get('company_name') or row['package_id']} - {row['package_id']}": row for row in packages}
    selected = st.selectbox("Open package", list(labels))
    if st.button("Open Package", type="primary"):
        package = labels[selected]
        st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
        st.session_state["active_package"] = package
        st.rerun()
    return None


def _header(payload: dict) -> None:
    package, recipe = payload["package"], payload["recipe"]
    anchor = get_earnings_anchor(package["package_id"])
    st.markdown(
        f"""
        <section class="assembly-header">
          <div><div class="eyebrow">Comprehensive Equity Research Package</div>
          <h1>{html.escape(str(package.get('company_name') or package['ticker']))}</h1>
          <div class="assembly-ticker">{html.escape(package['ticker'])}</div></div>
          <div class="assembly-status">{html.escape(str(payload['instance']['status']).replace('_', ' '))}</div>
        </section>
        <div class="assembly-meta">
          <div><span>Compilation date</span><strong>{html.escape(str(package.get('compilation_date') or 'Not set'))}</strong></div>
          <div><span>Research cutoff</span><strong>{html.escape(str(package.get('research_cutoff_date') or ''))}</strong></div>
          <div><span>CIK</span><strong>{html.escape(str(package.get('cik') or 'Not available'))}</strong></div>
          <div><span>Latest earnings cycle</span><strong>{html.escape(str((anchor or {}).get('fiscal_quarter') or 'Not determined'))} {html.escape(str((anchor or {}).get('fiscal_year') or ''))}</strong></div>
          <div><span>Compiled by</span><strong>{html.escape(str(package.get('compiled_by') or 'Not set'))}</strong></div>
          <div><span>Security</span><strong>{html.escape(str(package.get('security_type') or ''))}</strong></div>
          <div><span>Recipe</span><strong>{html.escape(str(recipe.get('recipe_name') or ''))} v{html.escape(str(recipe.get('version') or ''))}</strong></div>
          <div><span>Package ID</span><strong>{html.escape(package['package_id'])}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Edit Package Metadata", expanded=False):
        with st.form("package_header_form"):
            compilation = st.date_input("Compilation date", value=date.fromisoformat(package.get("compilation_date") or date.today().isoformat()))
            cutoff = st.date_input("Research cutoff", value=date.fromisoformat(str(package["research_cutoff_date"])[:10]))
            compiled_by = st.text_input("Compiled by", value=package.get("compiled_by") or "")
            if st.form_submit_button("Save Metadata"):
                update_package_header(package["package_id"], compilation_date=compilation, research_cutoff=cutoff, compiled_by=compiled_by)
                st.rerun()
    with st.expander("Latest Completed Earnings Cycle", expanded=False):
        if anchor:
            st.dataframe(
                [{
                    "Fiscal quarter": anchor.get("fiscal_quarter"), "Fiscal year": anchor.get("fiscal_year"),
                    "Period end": anchor.get("reporting_period_end"), "Release date": anchor.get("earnings_release_date"),
                    "Source": anchor.get("anchor_source"), "Confidence": anchor.get("confidence"),
                    "Override": bool(anchor.get("analyst_override")),
                }],
                hide_index=True,
                use_container_width=True,
            )
            st.caption("Use Refresh Public Discovery to recalculate. Analyst overrides are recorded in Audit Details.")
        else:
            st.info("The latest completed earnings cycle will be determined during public discovery.")


def _summary(payload: dict) -> None:
    summary = payload["summary"]
    cols = st.columns(6)
    cols[0].metric("Overall", f"{summary['overall_percent']}%", f"{summary['overall_complete']}/{summary['overall_total']}")
    cols[1].metric("Required", f"{summary['required_complete']}/{summary['required_total']}")
    cols[2].metric("Recommended", f"{summary['recommended_complete']}/{summary['recommended_total']}")
    cols[3].metric("Optional", f"{summary['optional_complete']}/{summary['optional_total']}")
    cols[4].metric("Manual / Missing", summary["manual_uploads_required"])
    cols[5].metric("Needs review", summary["needs_review"])
    st.markdown(
        f'<div class="guidance-panel"><strong>Recipe Readiness: {html.escape(summary["readiness"])}</strong><span>{html.escape(payload["guidance"])}</span></div>',
        unsafe_allow_html=True,
    )
    st.caption("Recipe-gated analysis becomes enforceable in Phase 6C. Existing analysis controls remain in Advanced Workbench.")


def _brave_status() -> str:
    if config.SEARCH_PROVIDER != "brave":
        return "Disabled"
    return "Configured" if config.brave_search_api_key() else "Not configured"


def _discovery_controls(payload: dict) -> None:
    package_id = payload["package"]["package_id"]
    latest = latest_discovery_run(package_id)
    st.subheader("Public Discovery")
    status_cols = st.columns(4)
    status_cols[0].metric("Discovery", (latest or {}).get("status", "NOT STARTED").replace("_", " "))
    status_cols[1].metric("Brave", _brave_status())
    status_cols[2].metric("Queries", int((latest or {}).get("queries_executed") or 0))
    status_cols[3].metric("Review", int((latest or {}).get("candidates_needing_review") or 0))
    actions = st.columns(5)
    if actions[0].button("Find All Missing Public Items", type="primary", use_container_width=True):
        st.session_state["assembly_discovery_preview"] = discovery_preview(package_id)
    if actions[1].button("Refresh Public Discovery", use_container_width=True):
        st.session_state["assembly_discovery_preview"] = discovery_preview(package_id)
    if actions[2].button("Review All Candidates", use_container_width=True):
        st.session_state["assembly_review_all_candidates"] = True
    resumable = bool(latest and latest["status"] in {"PARTIAL", "FAILED", "INTERRUPTED", "COMPLETED_WITH_WARNINGS"})
    if actions[3].button("Resume Discovery", disabled=not resumable, use_container_width=True):
        with st.spinner("Resuming incomplete discovery slots..."):
            resume_discovery(package_id, actor=_actor())
        st.rerun()
    if actions[4].button("Stop Discovery", disabled=not bool(latest and latest["status"] in {"PENDING", "RUNNING"}), use_container_width=True):
        stop_discovery(package_id, actor=_actor())
        st.rerun()
    preview = st.session_state.get("assembly_discovery_preview")
    if preview:
        with st.expander("Discovery Preview", expanded=True):
            metrics = st.columns(4)
            metrics[0].metric("Slots to search", len(preview["slots_to_search"]))
            metrics[1].metric("Slots skipped", len(preview["slots_skipped"]))
            metrics[2].metric("Maximum Brave queries", preview["maximum_possible_brave_queries"])
            metrics[3].metric("Manual-only remaining", preview["manual_only_slots_remaining"])
            st.dataframe(
                [{"Research item": row["display_name"], "Source priority": " > ".join(row["source_priority"]), "Maximum queries": row["maximum_queries"]} for row in preview["slots_to_search"]],
                hide_index=True,
                use_container_width=True,
            )
            confirmed = st.checkbox("Run bounded discovery for these missing public items.", key="assembly_discovery_confirm")
            if st.button("Confirm Public Discovery", type="primary", disabled=not confirmed):
                with st.spinner("Discovering authoritative public materials slot by slot..."):
                    run_discovery(package_id, actor=_actor())
                st.session_state.pop("assembly_discovery_preview", None)
                st.rerun()
    if latest:
        st.caption(
            f"{latest.get('slot_count_completed', 0)}/{latest.get('slot_count_requested', 0)} slots complete | "
            f"{latest.get('results_considered', 0)} results considered | "
            f"{latest.get('candidates_created', 0)} candidates | {latest.get('candidates_rejected', 0)} rejected"
        )
    st.caption(CompletenessAgent().guidance(package_id))


def _candidate_review(package_id: str, slot_id: str, *, expanded: bool = False) -> None:
    candidates = list_discovery_candidates(package_id, slot_instance_id=slot_id)
    with st.expander("Review Candidates", expanded=expanded):
        if not candidates:
            st.info("No probable investor-material candidates are available for this item.")
            return
        st.dataframe(
            [{
                "Title": row["title"], "Source": row["source_route"], "Publication date": row.get("publication_date"),
                "File type": row.get("mime_type") or row.get("file_extension"), "Relevance": row["slot_relevance_score"],
                "Authority": row["source_authority_score"], "Freshness": row["freshness_score"],
                "Status": row["candidate_status"], "Host": row["domain"], "Reason": row.get("rejection_reason") or "",
            } for row in candidates],
            hide_index=True,
            use_container_width=True,
        )
        labels = {f"{row['title']} | {row['domain']} | {row['candidate_status']}": row for row in candidates}
        selected = labels[st.selectbox("Candidate", list(labels), key=f"candidate_select_{slot_id}")]
        cols = st.columns(4)
        if cols[0].button("Approve And Download", type="primary", key=f"candidate_approve_{selected['candidate_id']}"):
            try:
                approve_and_download_candidate(selected["candidate_id"], actor=_actor())
                st.success("Candidate validated, downloaded, and assigned to this research item.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if cols[1].button("Reject", key=f"candidate_reject_{selected['candidate_id']}"):
            decide_candidate(selected["candidate_id"], "REJECT", actor=_actor(), reason_code="ANALYST_REJECTED")
            st.rerun()
        if cols[2].button("Defer", key=f"candidate_defer_{selected['candidate_id']}"):
            decide_candidate(selected["candidate_id"], "DEFER", actor=_actor())
            st.rerun()
        if selected.get("canonical_url"):
            cols[3].link_button("Open Official Source", selected["canonical_url"])


def _order(slot: dict) -> str:
    if slot["order_number"] is None:
        return "Supplemental"
    return f"{slot['order_number']}.{slot['suborder']}" if slot["suborder"] else str(slot["order_number"])


def _board(payload: dict) -> None:
    rows = []
    cards = []
    current_section = None
    for slot in payload["slots"]:
        if slot["section_snapshot"] != current_section:
            current_section = slot["section_snapshot"]
            rows.append(f'<tr class="assembly-section-row"><th colspan="9">{html.escape(current_section)}</th></tr>')
        assignments = [item for item in payload["assignments_by_slot"].get(slot["package_slot_instance_id"], []) if item["assignment_status"] == "APPROVED"]
        document = "; ".join(item.get("document_title") or item.get("original_filename") or item["document_id"] for item in assignments) or "None selected"
        document_date = "; ".join(str(item.get("document_date") or item.get("publication_date") or "") for item in assignments)
        source = "; ".join(__import__("json").loads(slot["preferred_sources_snapshot_json"] or "[]")) or "Not specified"
        status_class = slot["completion_status"].lower().replace("_", "-")
        row_values = [
            _order(slot), slot["display_name_snapshot"], slot["requirement_snapshot"], slot["completion_status"],
            source, document, document_date, slot.get("analyst_notes") or "", "Select below",
        ]
        rows.append("<tr>" + "".join(
            f'<td class="{("status-cell " + status_class) if index == 3 else ""}">{html.escape(str(value))}</td>'
            for index, value in enumerate(row_values)
        ) + "</tr>")
        cards.append(
            f'<article class="assembly-slot-card"><div class="slot-card-top"><strong>{html.escape(_order(slot))} {html.escape(slot["display_name_snapshot"])}</strong>'
            f'<span class="status-cell {status_class}">{html.escape(slot["completion_status"])}</span></div>'
            f'<div class="slot-card-meta">{html.escape(slot["requirement_snapshot"])} | {html.escape(source)}</div>'
            f'<div>{html.escape(document)}</div><small>Select this item in Slot Actions below.</small></article>'
        )
    rows.append(
        '<tr class="assembly-section-row"><th colspan="9">Final Memo</th></tr>'
        '<tr><td>-</td><td>Recipe-gated investment memo</td><td>REVIEW_REQUIRED</td>'
        '<td class="status-cell not-started">NOT_STARTED</td><td>Approved recipe corpus</td>'
        '<td>Deferred to Phase 6C</td><td></td><td></td><td>Advanced Workbench</td></tr>'
    )
    cards.append(
        '<article class="assembly-slot-card"><div class="slot-card-top"><strong>Final Memo</strong>'
        '<span class="status-cell not-started">NOT_STARTED</span></div>'
        '<div class="slot-card-meta">REVIEW_REQUIRED | Deferred to Phase 6C</div></article>'
    )
    st.markdown(
        '<div class="assembly-table-wrap"><table class="assembly-table"><thead><tr>'
        + "".join(f"<th>{header}</th>" for header in ("Order", "Research Item", "Requirement", "Status", "Preferred Source", "Selected Document", "Document Date", "Notes", "Actions"))
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        + '<div class="assembly-mobile">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def _upload_action(package: dict, slots: list[dict]) -> None:
    files = st.file_uploader(
        "Upload authorized files to review",
        accept_multiple_files=True,
        type=[extension.lstrip(".") for extension in config.SUPPORTED_UPLOAD_EXTENSIONS],
        key="assembly_uploads",
    )
    if not files:
        return
    candidates = [UploadCandidate(file.name, file.getvalue(), getattr(file, "type", "")) for file in files]
    signature = tuple((candidate.original_filename, len(candidate.content)) for candidate in candidates)
    if st.session_state.get("assembly_upload_signature") != signature:
        st.session_state["assembly_upload_signature"] = signature
        st.session_state["assembly_upload_review"] = prepare_batch_review(package, candidates)
    reviews = st.session_state["assembly_upload_review"]
    visible_columns = [key for key in reviews[0] if not key.startswith("_")]
    edited = st.data_editor(
        [{key: row[key] for key in visible_columns} for row in reviews],
        hide_index=True,
        use_container_width=True,
        disabled=[column for column in visible_columns if column not in {"Include", "Final source", "Final document type", "Document date", "Notes"}],
        key="assembly_upload_editor",
    )
    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
    merged = [{**reviews[index], **row} for index, row in enumerate(edited_rows)]
    authorized = st.checkbox("I confirm these files are authorized for internal use.", key="assembly_upload_authorized")
    if st.button("Store And Suggest Slots", type="primary", disabled=not authorized):
        before = {row["document_id"] for row in database.list_documents_by_package(package["package_id"])}
        summary = store_reviewed_upload_batch(package, candidates, merged, authorization_confirmed=True)
        after = database.list_documents_by_package(package["package_id"])
        created = [row["document_id"] for row in after if row["document_id"] not in before]
        suggestions = suggest_document_assignments(package["package_id"], created, actor=_actor())
        st.session_state["assembly_suggestions"] = suggestions
        st.success(f"Stored {summary['uploaded']} file(s); generated {len(suggestions)} deterministic slot suggestion(s).")
        st.rerun()


def _slot_actions(payload: dict) -> None:
    st.subheader("Slot Actions")
    labels = {f"{_order(slot)} - {slot['display_name_snapshot']} [{slot['completion_status']}]": slot for slot in payload["slots"]}
    selected_label = st.selectbox("Research item", list(labels), key="assembly_selected_slot")
    slot = labels[selected_label]
    slot_id = slot["package_slot_instance_id"]
    plans = {plan.package_slot_instance_id: plan for plan in RecipePlannerAgent().plan(payload["package"]["package_id"])}
    plan = plans[slot_id]
    if plan.auto_searchable:
        if st.button("Find Automatically", type="primary", use_container_width=True, key=f"find_{slot_id}"):
            with st.spinner(f"Finding {slot['display_name_snapshot']}..."):
                run_discovery(payload["package"]["package_id"], slot_instance_ids=[slot_id], actor=_actor())
            st.rerun()
    else:
        st.caption(f"Manual upload required. {plan.reason}")
    _candidate_review(
        payload["package"]["package_id"],
        slot_id,
        expanded=bool(st.session_state.get("assembly_review_all_candidates")),
    )

    tabs = st.tabs(["Assign", "Upload", "Review", "Status / Notes", "Open / Download"])
    with tabs[0]:
        documents = database.list_documents_by_package(payload["package"]["package_id"])
        options = {
            f"{doc.get('title') or doc.get('original_filename') or doc['document_id']} | {doc.get('source_name') or ''} | {(doc.get('sha256_hash') or '')[:12]}": doc
            for doc in documents if doc.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
        }
        if options:
            chosen = st.multiselect("Existing package documents", list(options), key=f"assign_doc_{slot_id}")
            override = st.checkbox("Administrator-approved slot-cap override", key=f"cap_override_{slot_id}")
            override_reason = st.text_input("Override reason", key=f"cap_reason_{slot_id}", disabled=not override)
            if st.button("Assign Existing Documents", type="primary", key=f"assign_{slot_id}", disabled=not chosen):
                for label in chosen:
                    assign_document(slot_id, options[label]["document_id"], actor=_actor(), override_cap=override, override_reason=override_reason)
                st.rerun()
        else:
            st.info("No eligible existing documents are available.")
    with tabs[1]:
        _upload_action(payload["package"], payload["slots"])
    with tabs[2]:
        assignments = [item for item in payload["assignments_by_slot"].get(slot_id, []) if item["assignment_status"] not in {"REMOVED", "REPLACED"}]
        if assignments:
            high_confidence = [item for item in assignments if item["assignment_status"] == "SUGGESTED" and float(item.get("suggestion_confidence") or 0) >= 0.9]
            if high_confidence and st.button("Accept High-Confidence Suggestions", key=f"accept_high_{slot_id}"):
                for item in high_confidence:
                    update_assignment(item["assignment_id"], "approve", actor=_actor())
                st.rerun()
            st.dataframe(
                [{"Document": item.get("document_title") or item["document_id"], "Status": item["assignment_status"],
                  "Confidence": item.get("suggestion_confidence"), "Reason": item.get("suggestion_reason") or "Analyst assignment",
                  "Highlighted": bool(item["highlighted_research"])} for item in assignments],
                hide_index=True, use_container_width=True,
            )
            review_labels = {f"{item.get('document_title') or item['document_id']} - {item['assignment_status']}": item for item in assignments}
            reviewed = review_labels[st.selectbox("Assignment", list(review_labels), key=f"review_{slot_id}")]
            cols = st.columns(4)
            if cols[0].button("Approve", key=f"approve_{reviewed['assignment_id']}"):
                update_assignment(reviewed["assignment_id"], "approve", actor=_actor())
                st.rerun()
            if cols[1].button("Reject", key=f"reject_{reviewed['assignment_id']}"):
                update_assignment(reviewed["assignment_id"], "reject", actor=_actor())
                st.rerun()
            if cols[2].button("Remove", key=f"remove_{reviewed['assignment_id']}"):
                update_assignment(reviewed["assignment_id"], "remove", actor=_actor())
                st.rerun()
            highlighted = bool(reviewed["highlighted_research"])
            if cols[3].button("Unhighlight" if highlighted else "Highlight", key=f"highlight_{reviewed['assignment_id']}", disabled=reviewed["assignment_status"] != "APPROVED"):
                set_highlighted(reviewed["assignment_id"], not highlighted, actor=_actor())
                st.rerun()
            available = database.list_documents_by_package(payload["package"]["package_id"])
            replacement_options = {
                doc.get("title") or doc.get("original_filename") or doc["document_id"]: doc["document_id"]
                for doc in available if doc.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED and doc["document_id"] != reviewed["document_id"]
            }
            if reviewed["assignment_status"] == "APPROVED" and replacement_options:
                replacement = st.selectbox("Replacement document", list(replacement_options), key=f"replacement_{reviewed['assignment_id']}")
                replacement_reason = st.text_input("Replacement reason", key=f"replacement_reason_{reviewed['assignment_id']}")
                if st.button("Replace Assignment", key=f"replace_{reviewed['assignment_id']}"):
                    replace_assignment(reviewed["assignment_id"], replacement_options[replacement], actor=_actor(), reason=replacement_reason)
                    st.rerun()
        else:
            st.info("No assignments are awaiting review for this slot.")
    with tabs[3]:
        note = st.text_area("Reason or analyst note", value=slot.get("analyst_notes") or "", key=f"slot_note_{slot_id}")
        cols = st.columns(4)
        if cols[0].button("Mark Not Available", key=f"na_{slot_id}"):
            mark_slot(slot_id, "NOT_AVAILABLE", reason=note, actor=_actor())
            st.rerun()
        if cols[1].button("Mark Not Applicable", key=f"nap_{slot_id}"):
            mark_slot(slot_id, "NOT_APPLICABLE", reason=note, actor=_actor())
            st.rerun()
        if cols[2].button("Restore", key=f"restore_{slot_id}"):
            mark_slot(slot_id, "RESTORE", reason="", actor=_actor())
            st.rerun()
        if cols[3].button("Add Note", key=f"note_{slot_id}"):
            add_slot_note(slot_id, note, actor=_actor())
            st.rerun()
    with tabs[4]:
        approved = [item for item in payload["assignments_by_slot"].get(slot_id, []) if item["assignment_status"] == "APPROVED"]
        if approved:
            selected = approved[0]
            if selected.get("source_url"):
                st.link_button("Open Source", selected["source_url"])
            path = Path(selected.get("local_path") or "")
            if path.is_file():
                st.download_button("Download Selected Document", data=path.read_bytes(), file_name=path.name, mime=selected.get("mime_type") or "application/octet-stream")
            else:
                st.caption("The selected document is not available in managed local storage.")
        else:
            st.info("Approve an assignment before opening or downloading it here.")


def _exports(payload: dict) -> None:
    cols = st.columns(2)
    signature = tuple(
        (slot["package_slot_instance_id"], slot["completion_status"], slot["updated_at"])
        for slot in payload["slots"]
    ) + tuple(
        (item["assignment_id"], item["assignment_status"], item["highlighted_research"])
        for item in payload["assignments"]
    )
    xlsx = _cached_checklist(payload["package"]["package_id"], signature, _actor())
    cols[0].download_button("Download Current Checklist XLSX", data=xlsx, file_name=f"{payload['package']['ticker']}_Cutler_Checklist.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    snapshot = _cached_snapshot(payload["package"]["package_id"], signature)
    cols[1].download_button("Download Package Snapshot JSON", data=snapshot, file_name=f"{payload['package']['ticker']}_Package_Snapshot.json", mime="application/json", use_container_width=True)


def main() -> None:
    bootstrap_page("Package Assembly")
    render_sidebar()
    st.text_input("Analyst", key="assembly_actor", placeholder="Name or initials")
    package = _active_package()
    if not package:
        _select_package()
        return
    payload = board_payload(package["package_id"])
    if payload.get("legacy"):
        st.markdown('<div class="eyebrow">Legacy Workflow</div>', unsafe_allow_html=True)
        st.title(f"{package['ticker']} Legacy Research Package")
        st.info("This historical package has no recipe snapshot. Its collection, versions, analysis, reports, and exports remain unchanged.")
        if st.button("Clone Into Phase 6 Recipe Draft", type="primary"):
            cloned = clone_legacy_package(package["package_id"], created_by=_actor())
            st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = cloned["package_id"]
            st.session_state["active_package"] = cloned
            st.rerun()
        st.page_link("pages/0_Research_Workspace.py", label="Open Legacy Research Workspace")
        return
    _header(payload)
    _summary(payload)
    _discovery_controls(payload)
    _board(payload)
    _slot_actions(payload)
    if payload["highlighted"]:
        st.subheader("Highlighted Research")
        st.dataframe([{"Order": next((f"{slot['order_number']}" for slot in payload["slots"] if slot["package_slot_instance_id"] == item["package_slot_instance_id"]), ""), "Document": item.get("document_title") or item["document_id"], "Source": item.get("source_name") or ""} for item in payload["highlighted"]], hide_index=True, use_container_width=True)
    _exports(payload)
    with st.expander("Audit Details", expanded=False):
        st.write({"Board load milliseconds": payload["load_ms"], "Instance": payload["instance"]["package_recipe_instance_id"], "Recipe readiness": payload["summary"]["readiness"]})
        st.json(discovery_audit_details(package["package_id"]))


if __name__ == "__main__":
    main()
