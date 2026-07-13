from __future__ import annotations

import html

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.status_badge import badge_html


WORKFLOW_STAGES = (
    "Company Setup",
    "Public Collection",
    "Licensed Uploads",
    "Package Review",
    "Investment Analysis",
    "PM Approval",
)


def _stage_status(stage: str, current_step: str) -> tuple[str, str]:
    if stage == "Company Setup":
        if current_step == stage:
            return "ACTIVE", "Active"
        return config.STATUS_COMPLETE, "Available"
    if stage == "Public Collection":
        if current_step == stage:
            return "ACTIVE", "Active"
        return config.STATUS_PUBLIC_COLLECTION, "Available"
    if stage in {"Licensed Uploads", "Package Review"}:
        if current_step == stage:
            return "ACTIVE", "Active"
        return config.STATUS_LICENSED_UPLOADS if stage == "Licensed Uploads" else config.STATUS_PACKAGE_REVIEW, "Available"
    if stage == current_step:
        return "UNAVAILABLE", "Future Phase"
    return config.STATUS_UPCOMING, "Upcoming"


def _safe_page_link(page: str, label: str) -> None:
    """Render a page link without crashing when a page is executed directly."""
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def render_sidebar() -> None:
    """Render the shared application sidebar."""
    current_step = st.session_state.get(
        config.SESSION_CURRENT_WORKFLOW_STEP,
        "Company Setup",
    )

    with st.sidebar:
        st.markdown(
            f"""
            <div class="sidebar-brand">
                <div class="sidebar-title">{html.escape(config.APP_NAME)}</div>
                <div class="sidebar-subtitle">{html.escape(config.APP_SUBTITLE)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        _safe_page_link("Home.py", "Dashboard")
        _safe_page_link("pages/1_New_Research_Package.py", "New Research Package")
        _safe_page_link("pages/2_Document_Collection.py", "Document Collection")
        _safe_page_link("pages/3_Package_Review.py", "Package Review")
        _safe_page_link("pages/4_Investment_Analysis.py", "Evidence Intelligence")
        _safe_page_link("pages/5_Generated_Reports.py", "Generated Reports")

        st.markdown('<div class="sidebar-section-title">Workflow</div>', unsafe_allow_html=True)
        for index, stage in enumerate(WORKFLOW_STAGES, start=1):
            status, label = _stage_status(stage, current_step)
            active_class = " workflow-active" if stage == current_step else ""
            st.markdown(
                f"""
                <div class="workflow-row{active_class}">
                    <div class="workflow-index">{index}</div>
                    <div class="workflow-copy">
                        <div class="workflow-name">{html.escape(stage)}</div>
                        {badge_html(status, label)}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown('<div class="sidebar-section-title">Current Package</div>', unsafe_allow_html=True)
        package = st.session_state.get("active_package")
        if package:
            st.markdown(
                f"""
                <div class="sidebar-package">
                    <div><span>Ticker</span><strong>{html.escape(str(package.get("ticker", "")))}</strong></div>
                    <div><span>Package</span><strong>{html.escape(str(package.get("package_id", "")))}</strong></div>
                    <div><span>Security</span><strong>{html.escape(str(package.get("security_type", "")))}</strong></div>
                    <div><span>Status</span>{badge_html(str(package.get("status", "")))}</div>
                    <div><span>Cutoff</span><strong>{html.escape(str(package.get("research_cutoff_date", "")))}</strong></div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.info("No active package selected. Start with Company Setup to create one.")

        st.caption("Phase 5 processes locked corpora for evidence only. Recommendations and final reports remain future phases.")
