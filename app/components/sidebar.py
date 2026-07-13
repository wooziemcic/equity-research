from __future__ import annotations

import html

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.status_badge import badge_html


def _safe_page_link(page: str, label: str) -> None:
    """Render a page link without crashing when a page is executed directly."""
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def render_sidebar() -> None:
    """Render the shared application sidebar."""
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

        st.markdown('<div class="sidebar-section-title">Primary Workflow</div>', unsafe_allow_html=True)
        _safe_page_link("Home.py", "Search")
        _safe_page_link("pages/0_Research_Workspace.py", "Research")
        _safe_page_link("pages/6_Investment_Result.py", "Result")

        st.markdown('<div class="sidebar-section-title">Secondary</div>', unsafe_allow_html=True)
        _safe_page_link("pages/7_Research_History.py", "Dashboard / History")
        with st.expander("Advanced Workbench", expanded=False):
            _safe_page_link("pages/1_New_Research_Package.py", "Package Setup")
            _safe_page_link("pages/2_Document_Collection.py", "Public Collection / Uploads")
            _safe_page_link("pages/3_Package_Review.py", "Package Review")
            _safe_page_link("pages/4_Investment_Analysis.py", "Evidence And Analysis Review")
            _safe_page_link("pages/5_Generated_Reports.py", "Generated Reports")

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
            st.info("No active package selected. Start with Search to create or reopen one.")

        st.caption("Closed-corpus research workflow. Analyst and PM governance remain in Advanced Workbench. No trades are executed.")
