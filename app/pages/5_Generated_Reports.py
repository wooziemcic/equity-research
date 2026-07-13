from __future__ import annotations

from pathlib import Path

import streamlit as st

from app import config
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.utils import database


def main() -> None:
    bootstrap_page("PM Approval")
    render_sidebar()
    st.markdown('<div class="eyebrow">Phase 4 Exports</div>', unsafe_allow_html=True)
    st.title("Research Package Exports")
    st.warning("Buy/Sell/Hold investment reports begin in Phase 6. This page only lists package export ZIPs.")
    versions = database.list_package_versions(limit=50)
    if not versions:
        st.info("No package exports have been built yet.")
        return
    st.dataframe(
        [
            {
                "Version ID": version["version_id"],
                "Ticker": version["ticker"],
                "Created": version["created_at"],
                "Status": version["status"],
                "Document Count": version["document_count"],
                "Total Size": version["total_size_bytes"],
                "Integrity": version.get("integrity_status") or "",
                "ZIP": "Available" if version.get("zip_path") and Path(version["zip_path"]).exists() else "",
            }
            for version in versions
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {
        version["version_id"]: version
        for version in versions
        if version.get("zip_path") and Path(version["zip_path"]).exists()
    }
    if labels:
        selected = st.selectbox("Download package ZIP", options=list(labels.keys()))
        version = labels[selected]
        with Path(version["zip_path"]).open("rb") as handle:
            st.download_button(
                "Download Selected ZIP",
                data=handle.read(),
                file_name=Path(version["zip_path"]).name,
                mime="application/zip",
            )
        database.create_package_version_event(
            event_id=f"PVE-EXPORT-DOWNLOAD-{version['version_id']}-{len(database.list_package_version_events(version['parent_package_id'])) + 1}",
            parent_package_id=version["parent_package_id"],
            version_id=version["version_id"],
            event_type="DOWNLOAD_REQUESTED",
            event_details_json='{"ui":"exports"}',
        )


if __name__ == "__main__":
    main()
