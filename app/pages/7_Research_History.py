from __future__ import annotations

from typing import Any

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.utils import database


def _latest_analysis(package_id: str) -> dict[str, Any] | None:
    runs = database.list_analysis_runs(package_id=package_id, limit=1)
    return runs[0] if runs else None


def _open_result(package: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
    st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
    st.session_state["active_package"] = package
    if analysis:
        st.session_state[config.SESSION_ACTIVE_ANALYSIS_RUN_ID] = analysis["analysis_run_id"]
        st.session_state[config.SESSION_ACTIVE_VERSION_ID] = analysis["version_id"]
        st.session_state[config.SESSION_ACTIVE_PROCESSING_RUN_ID] = analysis["processing_run_id"]
    try:
        st.switch_page("pages/6_Investment_Result.py" if analysis else "pages/0_Research_Workspace.py")
    except (StreamlitPageNotFoundError, AttributeError):
        st.page_link("pages/6_Investment_Result.py" if analysis else "pages/0_Research_Workspace.py", label="Open selected research")


def _safe_page_link(page: str, label: str) -> None:
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def main() -> None:
    bootstrap_page("History")
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "History"

    st.markdown('<div class="eyebrow">Dashboard / History</div>', unsafe_allow_html=True)
    st.title("Recent Research")
    packages = database.list_packages(limit=100)
    if not packages:
        render_empty_state("No research history yet.", "Search for a ticker to begin a new research workspace.")
        _safe_page_link("Home.py", "Search")
        return

    rows = []
    analyses_by_package: dict[str, dict[str, Any] | None] = {}
    for package in packages:
        version = database.latest_package_version(package["package_id"]) or {}
        analysis = _latest_analysis(package["package_id"])
        analyses_by_package[package["package_id"]] = analysis
        decision = database.get_recommendation_decision(analysis["analysis_run_id"]) if analysis else None
        reports = database.list_generated_reports(analysis["analysis_run_id"], limit=1) if analysis else []
        exports = database.list_combined_exports(analysis["analysis_run_id"], limit=1) if analysis else []
        rows.append(
            {
                "Ticker": package["ticker"],
                "Company": package.get("company_name") or "Company resolution pending",
                "Latest package version": version.get("version_id", ""),
                "Latest recommendation": (decision or {}).get("effective_rating", ""),
                "Recommendation status": (analysis or {}).get("status", ""),
                "Research cutoff": package.get("research_cutoff_date"),
                "Created": package.get("created_at"),
                "PM approval status": (analysis or {}).get("pm_approved_recommendation") or "Pending",
                "Download availability": "Combined ZIP" if exports else "Report" if reports else "Not yet",
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)

    labels = {f"{package['ticker']} - {package.get('company_name') or package['package_id']}": package for package in packages}
    selected = st.selectbox("Reopen research", options=list(labels.keys()))
    if st.button("Open Selected Research", type="primary"):
        package = labels[selected]
        _open_result(package, analyses_by_package.get(package["package_id"]))


if __name__ == "__main__":
    main()
