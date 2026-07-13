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
    st.markdown('<div class="eyebrow">Phase 6 Reports</div>', unsafe_allow_html=True)
    st.title("Generated Reports")
    st.caption("Package exports and closed-corpus investment reports. Final reports require PM approval and do not execute trades.")
    st.subheader("Research Package Exports")
    versions = database.list_package_versions(limit=50)
    if not versions:
        st.info("No package exports have been built yet.")
    else:
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
    st.divider()
    st.subheader("Investment Reports")
    reports = database.list_generated_reports(limit=100)
    if not reports:
        st.info("No investment reports have been generated yet.")
        return
    versions_by_id = {version["version_id"]: version for version in database.list_package_versions(limit=500)}
    analysis_by_id = {run["analysis_run_id"]: run for run in database.list_analysis_runs(limit=500)}
    st.dataframe(
        [
            {
                "Ticker": versions_by_id.get(report["version_id"], {}).get("ticker", ""),
                "Package Version": report["version_id"],
                "Analysis Run": report["analysis_run_id"],
                "Recommendation": report.get("recommendation") or "",
                "Confidence": report.get("confidence") or "",
                "Analyst Review": analysis_by_id.get(report["analysis_run_id"], {}).get("analyst_adjusted_recommendation") or "",
                "PM Approval": analysis_by_id.get(report["analysis_run_id"], {}).get("pm_approved_recommendation") or "",
                "Report Status": report["report_status"],
                "Created": report["created_at"],
                "DOCX Hash": (report.get("docx_sha256") or "")[:12],
                "PDF Hash": (report.get("pdf_sha256") or "")[:12],
            }
            for report in reports
        ],
        hide_index=True,
        use_container_width=True,
    )
    labels = {f"V{report['report_version']:03d} - {report['analysis_run_id']} - {report['report_status']}": report for report in reports}
    selected = st.selectbox("Download investment report", options=list(labels.keys()))
    report = labels[selected]
    cols = st.columns(2)
    if report.get("docx_path") and Path(report["docx_path"]).exists():
        with Path(report["docx_path"]).open("rb") as handle:
            cols[0].download_button(
                "Download DOCX",
                data=handle.read(),
                file_name=Path(report["docx_path"]).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    if report.get("pdf_path") and Path(report["pdf_path"]).exists():
        with Path(report["pdf_path"]).open("rb") as handle:
            cols[1].download_button(
                "Download PDF",
                data=handle.read(),
                file_name=Path(report["pdf_path"]).name,
                mime="application/pdf",
            )


if __name__ == "__main__":
    main()
