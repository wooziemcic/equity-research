from __future__ import annotations

import html
from typing import Any

import streamlit as st

from app.components.status_badge import badge_html


def render_metric_card(label: str, value: int | str, help_text: str) -> None:
    """Render a compact dashboard metric."""
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{html.escape(label)}</div>
            <div class="metric-value">{html.escape(str(value))}</div>
            <div class="metric-help">{html.escape(help_text)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(title: str, body: str) -> None:
    """Render a clear empty-state panel."""
    st.markdown(
        f"""
        <div class="empty-state">
            <div class="empty-state-title">{html.escape(title)}</div>
            <div class="empty-state-body">{html.escape(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_package_summary(package: dict[str, Any]) -> None:
    """Render a compact package summary after creation."""
    st.markdown(
        f"""
        <div class="package-summary">
            <div class="package-summary-header">
                <div>
                    <div class="eyebrow">Research Package</div>
                    <h3>{html.escape(str(package["ticker"]))}</h3>
                </div>
                {badge_html(str(package["status"]))}
            </div>
            <div class="summary-grid">
                <div><span>Package ID</span><strong>{html.escape(str(package["package_id"]))}</strong></div>
                <div><span>Security Type</span><strong>{html.escape(str(package["security_type"]))}</strong></div>
                <div><span>Cutoff Date</span><strong>{html.escape(str(package["research_cutoff_date"]))}</strong></div>
                <div><span>Filing History</span><strong>{html.escape(str(package["filing_history_years"]))} years</strong></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
