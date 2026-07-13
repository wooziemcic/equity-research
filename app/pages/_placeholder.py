from __future__ import annotations

import streamlit as st

from app.components.document_card import render_document_placeholder


def render_phase_placeholder(
    *,
    page_name: str,
    phase: str,
    future_capabilities: list[str],
) -> None:
    """Render a consistent placeholder for future workflow pages."""
    st.markdown(f'<div class="eyebrow">{phase}</div>', unsafe_allow_html=True)
    st.title(page_name)
    st.warning(
        f"{page_name} is not operational in Phase 1. This page is reserved for future implementation."
    )
    st.write("Planned functionality:")
    for capability in future_capabilities:
        render_document_placeholder(page_name, capability, phase)
