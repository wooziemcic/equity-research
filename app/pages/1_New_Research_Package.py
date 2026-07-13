from __future__ import annotations

import logging
from datetime import date

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_package_summary
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services.package_service import (
    PackageInput,
    create_package,
    find_existing_ticker_packages,
)
from app.utils.database import DatabaseError
from app.utils.validation import (
    MAX_ANALYST_NOTES_LENGTH,
    sanitize_analyst_notes,
    validate_cutoff_date,
    validate_ticker,
)

logger = logging.getLogger(__name__)


def _selected_filing_years(label: str) -> int:
    return config.FILING_HISTORY_OPTIONS[label]


def _safe_page_link(page: str, label: str) -> None:
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def _show_existing_ticker_warning(raw_ticker: str) -> None:
    ticker_result = validate_ticker(raw_ticker)
    if not ticker_result.is_valid:
        return
    try:
        existing_packages = find_existing_ticker_packages(ticker_result)
    except DatabaseError:
        logger.exception("Unable to check existing ticker packages")
        st.warning("Existing packages for this ticker could not be checked.")
        return
    if existing_packages:
        st.warning(
            f"{len(existing_packages)} existing package record(s) found for {ticker_result.value}. "
            "You can still create another dated package version."
        )
        st.dataframe(
            [
                {
                    "Package ID": package["package_id"],
                    "Status": package["status"].replace("_", " ").title(),
                    "Cutoff Date": package["research_cutoff_date"],
                    "Created": package["created_at"],
                }
                for package in existing_packages
            ],
            hide_index=True,
            use_container_width=True,
        )


def _validate_form(
    ticker: str,
    cutoff_date: date,
    analyst_notes: str,
) -> list[str]:
    errors: list[str] = []
    ticker_result = validate_ticker(ticker)
    cutoff_result = validate_cutoff_date(cutoff_date)
    notes_result = sanitize_analyst_notes(analyst_notes)
    if not ticker_result.is_valid:
        errors.append(ticker_result.error)
    if not cutoff_result.is_valid:
        errors.append(cutoff_result.error)
    if not notes_result.is_valid:
        errors.append(notes_result.error)
    return errors


def main() -> None:
    bootstrap_page("Company Setup")
    render_sidebar()

    st.markdown('<div class="eyebrow">Company Setup</div>', unsafe_allow_html=True)
    st.title("New Research Package")
    st.write(
        "Create a persistent package workspace. Phase 1 does not resolve tickers or collect documents."
    )

    with st.form("new_research_package_form", clear_on_submit=False):
        ticker = st.text_input(
            "Ticker",
            max_chars=12,
            help="Common symbols such as QXO, BRK.B, BF-B, and GOOGL are accepted.",
        )
        security_type = st.selectbox(
            "Security type",
            options=list(config.SUPPORTED_SECURITY_TYPES),
            index=0,
        )
        cutoff_date = st.date_input(
            "Research cutoff date",
            value=date.today(),
            max_value=date.today(),
            help="Documents will eventually be collected through this date.",
        )
        filing_history_label = st.selectbox(
            "Filing history period",
            options=list(config.FILING_HISTORY_OPTIONS.keys()),
            index=2,
        )
        analyst_notes = st.text_area(
            "Analyst notes",
            max_chars=MAX_ANALYST_NOTES_LENGTH,
            help="Optional setup context. Do not enter confidential credentials or secrets.",
        )
        submitted = st.form_submit_button("Create Package", type="primary")

    _show_existing_ticker_warning(ticker)

    if not submitted:
        return

    errors = _validate_form(ticker, cutoff_date, analyst_notes)
    if errors:
        for error in errors:
            st.error(error)
        return

    normalized_ticker = validate_ticker(ticker).value
    signature = (
        normalized_ticker,
        security_type,
        cutoff_date.isoformat(),
        _selected_filing_years(filing_history_label),
        sanitize_analyst_notes(analyst_notes).value,
    )
    if (
        st.session_state.get("last_package_create_signature") == signature
        and st.session_state.get("last_created_package")
    ):
        st.info("This package was already created during the current form submission.")
        render_package_summary(st.session_state["last_created_package"])
        return

    try:
        package = create_package(
            PackageInput(
                ticker=normalized_ticker,
                security_type=security_type,
                research_cutoff_date=cutoff_date,
                filing_history_years=_selected_filing_years(filing_history_label),
                analyst_notes=analyst_notes,
            )
        )
    except (ValueError, DatabaseError):
        logger.exception("Package creation failed")
        st.error("The package could not be created. Review the fields and try again.")
        return

    st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = package["package_id"]
    st.session_state[config.SESSION_ACTIVE_TICKER] = package["ticker"]
    st.session_state["active_package"] = package
    st.session_state["last_package_create_signature"] = signature
    st.session_state["last_created_package"] = package

    st.success("Research package created.")
    render_package_summary(package)
    col1, col2 = st.columns(2)
    with col1:
        _safe_page_link("Home.py", "Return to Dashboard")
    with col2:
        _safe_page_link("pages/2_Document_Collection.py", "View Future Collection Page")


if __name__ == "__main__":
    main()
