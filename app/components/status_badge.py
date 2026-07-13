from __future__ import annotations

import html

import streamlit as st

from app import config


STATUS_LABELS = {
    config.STATUS_DRAFT: "Draft",
    config.STATUS_SETUP: "Setup",
    config.STATUS_IN_PROGRESS: "In Progress",
    config.STATUS_COMPLETE: "Complete",
    config.STATUS_WARNING: "Warning",
    config.STATUS_AWAITING_REVIEW: "Awaiting Review",
    config.STATUS_UPCOMING: "Upcoming",
    config.STATUS_PUBLIC_COLLECTION: "Public Collection",
    config.STATUS_PUBLIC_COLLECTION_PARTIAL: "Public Collection Partial",
    config.STATUS_PUBLIC_COLLECTION_COMPLETE: "Public Collection Complete",
    config.STATUS_LICENSED_UPLOADS: "Licensed Uploads",
    config.STATUS_PACKAGE_REVIEW: "Package Review",
    config.STATUS_PACKAGE_REVIEW_INCOMPLETE: "Review Incomplete",
    config.STATUS_PACKAGE_READY_FOR_BUILD: "Ready For Build",
    config.STATUS_PACKAGE_LOCKED: "Package Locked",
    config.VERSION_STATUS_BUILDING: "Building",
    config.VERSION_STATUS_BUILD_FAILED: "Build Failed",
    config.VERSION_STATUS_BUILT: "Built",
    config.VERSION_STATUS_LOCKED: "Locked",
    config.VERSION_STATUS_SUPERSEDED: "Superseded",
    config.VERSION_STATUS_ARCHIVED: "Archived",
    config.DOCUMENT_STATUS_DISCOVERED: "Discovered",
    config.DOCUMENT_STATUS_DOWNLOADED: "Downloaded",
    config.DOCUMENT_STATUS_DUPLICATE: "Duplicate",
    config.DOCUMENT_STATUS_FAILED: "Failed",
    config.DOCUMENT_STATUS_SKIPPED: "Skipped",
    "ACTIVE": "Active",
    "UNAVAILABLE": "Unavailable",
}

STATUS_CLASSES = {
    config.STATUS_DRAFT: "badge-warning",
    config.STATUS_SETUP: "badge-active",
    config.STATUS_IN_PROGRESS: "badge-active",
    config.STATUS_COMPLETE: "badge-complete",
    config.STATUS_WARNING: "badge-warning",
    config.STATUS_AWAITING_REVIEW: "badge-warning",
    config.STATUS_UPCOMING: "badge-upcoming",
    config.STATUS_PUBLIC_COLLECTION: "badge-active",
    config.STATUS_PUBLIC_COLLECTION_PARTIAL: "badge-warning",
    config.STATUS_PUBLIC_COLLECTION_COMPLETE: "badge-complete",
    config.STATUS_LICENSED_UPLOADS: "badge-active",
    config.STATUS_PACKAGE_REVIEW: "badge-active",
    config.STATUS_PACKAGE_REVIEW_INCOMPLETE: "badge-warning",
    config.STATUS_PACKAGE_READY_FOR_BUILD: "badge-complete",
    config.STATUS_PACKAGE_LOCKED: "badge-complete",
    config.VERSION_STATUS_BUILDING: "badge-active",
    config.VERSION_STATUS_BUILD_FAILED: "badge-risk",
    config.VERSION_STATUS_BUILT: "badge-complete",
    config.VERSION_STATUS_LOCKED: "badge-complete",
    config.VERSION_STATUS_SUPERSEDED: "badge-upcoming",
    config.VERSION_STATUS_ARCHIVED: "badge-upcoming",
    config.DOCUMENT_STATUS_DISCOVERED: "badge-active",
    config.DOCUMENT_STATUS_DOWNLOADED: "badge-complete",
    config.DOCUMENT_STATUS_DUPLICATE: "badge-warning",
    config.DOCUMENT_STATUS_FAILED: "badge-risk",
    config.DOCUMENT_STATUS_SKIPPED: "badge-upcoming",
    "ACTIVE": "badge-active",
    "UNAVAILABLE": "badge-upcoming",
}


def badge_html(status: str, label: str | None = None) -> str:
    """Return a reusable status badge HTML fragment."""
    normalized = status.upper()
    display_label = label or STATUS_LABELS.get(normalized, normalized.replace("_", " ").title())
    css_class = STATUS_CLASSES.get(normalized, "badge-upcoming")
    return (
        f'<span class="status-badge {css_class}">'
        f"{html.escape(display_label)}"
        "</span>"
    )


def render_status_badge(status: str, label: str | None = None) -> None:
    """Render a consistent status badge."""
    st.markdown(badge_html(status, label), unsafe_allow_html=True)
