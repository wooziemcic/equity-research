from __future__ import annotations

import logging

import streamlit as st

from app import config

logger = logging.getLogger(__name__)


def configure_page() -> None:
    """Apply consistent Streamlit page configuration."""
    st.set_page_config(
        page_title=config.PAGE_TITLE,
        page_icon=config.PAGE_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )


def load_css() -> None:
    """Load shared CSS without breaking the page if the file is missing."""
    try:
        css = config.STYLE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("CSS file missing: %s", config.STYLE_PATH)
        st.warning("Shared styling could not be loaded. The app is still operational.")
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def initialize_session_state() -> None:
    """Set predictable session state defaults."""
    st.session_state.setdefault(config.SESSION_ACTIVE_PACKAGE_ID, None)
    st.session_state.setdefault(config.SESSION_ACTIVE_TICKER, None)
    st.session_state.setdefault(config.SESSION_CURRENT_WORKFLOW_STEP, "Company Setup")
    st.session_state.setdefault(config.SESSION_ACTIVE_VERSION_ID, None)
    st.session_state.setdefault(config.SESSION_ACTIVE_PROCESSING_RUN_ID, None)
    st.session_state.setdefault(config.SESSION_ACTIVE_ANALYSIS_RUN_ID, None)
    st.session_state.setdefault(config.SESSION_ACTIVE_REPORT_ID, None)
    st.session_state.setdefault(config.SESSION_PRIMARY_SCREEN, "Search")
    st.session_state.setdefault(config.SESSION_COLLECTION_STATE, {})
    st.session_state.setdefault(config.SESSION_WORKFLOW_STATE, {})


def bootstrap_page(current_step: str = "Company Setup") -> None:
    """Configure Streamlit, create directories, load CSS, and initialize session."""
    configure_page()
    config.ensure_directories()
    load_css()
    initialize_session_state()
    st.session_state[config.SESSION_CURRENT_WORKFLOW_STEP] = current_step
