from __future__ import annotations

import html

import streamlit as st


def render_document_placeholder(title: str, description: str, phase: str) -> None:
    """Render a future document capability placeholder."""
    st.markdown(
        f"""
        <div class="document-card">
            <div class="document-card-title">{html.escape(title)}</div>
            <div class="document-card-body">{html.escape(description)}</div>
            <div class="document-card-meta">{html.escape(phase)} planned capability</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
