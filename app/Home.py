from __future__ import annotations

import html
import logging

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.layout import bootstrap_page
from app.services.company_resolver import resolve_ticker_metadata
from app.services.research_workflow_service import (
    get_or_create_research_package,
    normalize_ticker_input,
    resolve_search_ticker,
    validate_search_ticker,
)
from app.utils import database

logger = logging.getLogger(__name__)


def _switch_to_research() -> None:
    try:
        st.switch_page("pages/0_Research_Workspace.py")
    except (StreamlitPageNotFoundError, AttributeError):
        st.page_link("pages/0_Research_Workspace.py", label="Open Research Workspace")


def _select_active_package(package: dict) -> None:
    st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
    st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "Research"
    st.session_state["active_package"] = package


def _company_confirmation() -> None:
    result = st.session_state.get("search_resolution")
    if not result:
        return
    status = result.get("status")
    if status == "RESOLVED":
        metadata = result["metadata"]
        st.markdown(
            f"""
            <div class="confirm-panel">
                <div class="eyebrow">SEC Company Match</div>
                <div class="confirm-ticker">{html.escape(metadata.get("ticker", ""))}</div>
                <div class="confirm-company">{html.escape(metadata.get("company_name") or "Company name unavailable")}</div>
                <div class="confirm-grid">
                    <div><span>Exchange</span><strong>{html.escape(str(metadata.get("exchange") or "Not provided"))}</strong></div>
                    <div><span>CIK</span><strong>{html.escape(str(metadata.get("cik") or ""))}</strong></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Confirm Company And Open Research Workspace", type="primary", use_container_width=True):
            package, created = get_or_create_research_package(metadata)
            _select_active_package(package)
            message = "New research package created." if created else "Existing working package reopened."
            st.success(message)
            _switch_to_research()
    elif status == "MULTIPLE_MATCHES":
        candidates = result.get("candidates") or []
        labels = {
            f"{row.get('ticker')} - {row.get('name') or row.get('title')} - CIK {row.get('cik', row.get('cik_str'))}": row
            for row in candidates
        }
        if labels:
            selected_label = st.selectbox("Select the SEC company record", options=list(labels.keys()))
            if st.button("Resolve Selected SEC Record", use_container_width=True):
                selected = labels[selected_label]
                selected_result = resolve_ticker_metadata(
                    normalize_ticker_input(selected.get("ticker")),
                    selected_cik=str(selected.get("cik", selected.get("cik_str"))),
                )
                st.session_state["search_resolution"] = {
                    "status": selected_result.status,
                    "metadata": selected_result.metadata,
                    "candidates": selected_result.candidates,
                    "error": selected_result.error,
                }
                st.rerun()
    elif status == "CONFIGURATION_REQUIRED":
        st.markdown(
            """
            <div class="search-inline-message search-inline-error">
                SEC company verification is not configured.
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander("Setup hint", expanded=False):
            st.caption("Configure SEC_USER_AGENT in your shell before searching. Environment values are not displayed here.")
    else:
        st.error("Ticker could not be verified in the supported SEC company database.")


def _recent_research_panel() -> None:
    packages = database.list_packages(limit=6)
    if not packages:
        st.caption("No recent research packages yet.")
        return
    rows = []
    for package in packages:
        latest_version = database.latest_package_version(package["package_id"]) or {}
        analyses = database.list_analysis_runs(package_id=package["package_id"], limit=1)
        decision = database.get_recommendation_decision(analyses[0]["analysis_run_id"]) if analyses else None
        rows.append(
            {
                "Ticker": package["ticker"],
                "Company": package.get("company_name") or "Company resolution pending",
                "Latest Version": latest_version.get("version_id", ""),
                "Recommendation": (decision or {}).get("effective_rating", ""),
                "Status": (analyses[0].get("status") if analyses else package.get("status")) or "",
                "Cutoff": package.get("research_cutoff_date"),
                "Updated": package.get("updated_at"),
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)
    labels = {f"{package['ticker']} - {package.get('company_name') or package['package_id']}": package for package in packages}
    selected = st.selectbox("Reopen package", options=list(labels.keys()))
    if st.button("Open Selected Research", use_container_width=True):
        _select_active_package(labels[selected])
        _switch_to_research()


def main() -> None:
    bootstrap_page("Search")
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "Search"

    st.markdown(
        """
        <main class="landing-hero" aria-label="Cutler Equity Research search">
            <section class="landing-content">
                <h1 class="cutler-wordmark">
                    <span>CUTLER</span>
                    <span class="brand-red">EQUITY</span>
                    <span>RESEARCH</span>
                </h1>
                <p>Document-grounded institutional research.</p>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="ticker-search-wrap">', unsafe_allow_html=True)
    with st.form("ticker_search_form", clear_on_submit=False):
        input_col, button_col = st.columns([56, 15], gap="small")
        with input_col:
            ticker = st.text_input(
                "Ticker",
                value=st.session_state.get("search_ticker", ""),
                placeholder="Enter ticker — e.g. QXO",
                label_visibility="collapsed",
                key="search_ticker",
            )
        with button_col:
            submitted = st.form_submit_button("Search", type="primary", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        normalized = normalize_ticker_input(ticker)
        st.session_state["search_submitted"] = True
        validation = validate_search_ticker(normalized)
        if not validation.is_valid:
            st.session_state["search_resolution"] = {
                "status": "UNRESOLVED",
                "metadata": None,
                "candidates": None,
                "error": validation.error,
            }
        else:
            try:
                result = resolve_search_ticker(normalized)
                st.session_state["search_resolution"] = {
                    "status": result.status,
                    "metadata": result.metadata,
                    "candidates": result.candidates,
                    "error": result.error,
                }
            except Exception as exc:
                logger.exception("Ticker resolution failed")
                st.session_state["search_resolution"] = {
                    "status": "UNRESOLVED",
                    "metadata": None,
                    "candidates": None,
                    "error": str(exc),
                }

    _company_confirmation()

    st.markdown('<nav class="landing-footer-links" aria-label="Secondary navigation">', unsafe_allow_html=True)
    link_cols = st.columns(2)
    with link_cols[0]:
        st.page_link("pages/7_Research_History.py", label="Recent Research")
    with link_cols[1]:
        st.page_link("pages/1_New_Research_Package.py", label="Advanced Workbench")
    st.markdown("</nav>", unsafe_allow_html=True)
    st.markdown("</section></main>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
