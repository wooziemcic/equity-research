from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app import config
from app.components.cards import render_empty_state, render_metric_card
from app.components.layout import bootstrap_page
from app.components.sidebar import render_sidebar
from app.services import package_service
from app.utils.database import DatabaseError
from app.utils import database

logger = logging.getLogger(__name__)


def _status_label(status: str) -> str:
    return status.replace("_", " ").title()


def _render_recent_packages() -> None:
    st.subheader("Recent Packages")
    try:
        packages = package_service.list_recent_packages(limit=10)
    except DatabaseError:
        logger.exception("Unable to load recent packages")
        st.error("Recent packages could not be loaded. Please try again after restarting the app.")
        return

    if not packages:
        render_empty_state(
            "No research packages have been created.",
            "Start with a ticker to create the first package workspace.",
        )
        return

    rows = [
        {
            "Ticker": package["ticker"],
            "Company": package["company_name"] or "Company resolution pending",
            "Security Type": package["security_type"],
            "Status": _status_label(package["status"]),
            "Public Docs": database.count_documents_for_package(package["package_id"]),
            "Licensed Docs": database.document_counts_for_package(package["package_id"])["licensed"],
            "Needs Review": sum(
                1
                for item in database.list_checklist_items(package["package_id"])
                if item["effective_status"] in {"MISSING", "NEEDS_REVIEW", "STALE"}
            ),
            "Latest Version": (database.latest_package_version(package["package_id"]) or {}).get("version_id", ""),
            "Version Status": (database.latest_package_version(package["package_id"]) or {}).get("status", ""),
            "Version Docs": (database.latest_package_version(package["package_id"]) or {}).get("document_count", ""),
            "Last Build": (database.latest_package_version(package["package_id"]) or {}).get("created_at", ""),
            "Last Collection": package.get("last_collection_at") or "",
            "Resolution": package.get("resolution_status") or "Unresolved",
            "Research Cutoff": package["research_cutoff_date"],
            "Created": package["created_at"],
            "Last Updated": package["updated_at"],
        }
        for package in packages
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_roadmap() -> None:
    st.subheader("Phase Roadmap")
    st.markdown(
        """
        <div class="roadmap-panel">
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 1</strong>
                <span>Application shell, package setup, SQLite persistence, dashboard, validation, and tests.</span>
            </div>
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 2</strong>
                <span>SEC company resolution, public SEC filing collection, investor-relations PDF discovery, and collection tracking.</span>
            </div>
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 3</strong>
                <span>Manual licensed-file uploads, classification suggestions, document inventory, and research checklist review.</span>
            </div>
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 4</strong>
                <span>Readiness validation, manifests, inventory files, immutable versions, locking, and ZIP exports.</span>
            </div>
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 5</strong>
                <span>Closed-corpus document processing, retrieval, evidence extraction, citations, duplicates, and conflicts.</span>
            </div>
            <div class="roadmap-item roadmap-ready">
                <strong>Phase 6</strong>
                <span>Deterministic investment analysis, scorecards, recommendations, analyst review, PM approval, and reports.</span>
            </div>
            <div class="roadmap-item">
                <strong>Future Phases</strong>
                <span>Authentication, deployment, monitoring, and integrations. Trading remains out of scope.</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    bootstrap_page("Company Setup")
    render_sidebar()

    st.markdown('<div class="eyebrow">Internal Research Platform</div>', unsafe_allow_html=True)
    st.title(config.APP_NAME)
    st.write(
        "A controlled workbench for creating and tracking equity research package foundations."
    )
    st.page_link(
        "pages/1_New_Research_Package.py",
        label="Create New Research Package",
        icon="➕",
    )

    try:
        metrics = package_service.get_dashboard_metrics()
        collection_metrics = database.dashboard_public_collection_metrics()
        phase3_metrics = database.phase3_dashboard_metrics()
        phase4_metrics = database.phase4_dashboard_metrics()
        phase5_metrics = database.phase5_dashboard_metrics()
        phase6_metrics = database.phase6_dashboard_metrics()
    except DatabaseError:
        logger.exception("Unable to initialize dashboard metrics")
        st.error("The database could not be initialized. Check file permissions and try again.")
        metrics = {"total": 0, "draft": 0, "completed": 0, "awaiting_review": 0}
        collection_metrics = {"public_documents": 0, "resolved_packages": 0, "failed_items": 0}
        phase3_metrics = {"licensed_documents": 0, "packages_needing_review": 0, "missing_core_items": 0}
        phase4_metrics = {"built_versions": 0, "locked_versions": 0, "packages_ready_to_build": 0, "integrity_failures": 0}
        phase5_metrics = {"processing_runs": 0, "completed_processing_runs": 0, "evidence_records": 0, "claim_conflicts": 0}
        phase6_metrics = {"analysis_runs": 0, "pm_approved_runs": 0, "investment_reports": 0, "final_reports": 0}

    metric_columns = st.columns(4)
    with metric_columns[0]:
        render_metric_card("Total Packages", metrics["total"], "Persisted package records")
    with metric_columns[1]:
        render_metric_card("Draft Packages", metrics["draft"], "Draft or setup status")
    with metric_columns[2]:
        render_metric_card("Completed Packages", metrics["completed"], "Completed package records")
    with metric_columns[3]:
        render_metric_card(
            "Public Documents",
            collection_metrics["public_documents"],
            "Downloaded public documents",
        )
    collection_columns = st.columns(3)
    with collection_columns[0]:
        render_metric_card(
            "Resolved Companies",
            collection_metrics["resolved_packages"],
            "Packages with SEC identity",
        )
    with collection_columns[1]:
        render_metric_card(
            "Failed Collection Items",
            collection_metrics["failed_items"],
            "Visible failed document attempts",
        )
    with collection_columns[2]:
        render_metric_card(
            "Licensed Documents",
            phase3_metrics["licensed_documents"],
            "Authorized uploaded files",
        )
    review_columns = st.columns(3)
    with review_columns[0]:
        render_metric_card("Packages Needing Review", phase3_metrics["packages_needing_review"], "Checklist gaps or review statuses")
    with review_columns[1]:
        render_metric_card("Missing Core Items", phase3_metrics["missing_core_items"], "Required checklist items missing")
    with review_columns[2]:
        render_metric_card("Reports Awaiting Review", metrics["awaiting_review"], "Future PM review queue")
    export_columns = st.columns(4)
    with export_columns[0]:
        render_metric_card("Built Versions", phase4_metrics["built_versions"], "Package export snapshots")
    with export_columns[1]:
        render_metric_card("Locked Versions", phase4_metrics["locked_versions"], "Immutable package corpora")
    with export_columns[2]:
        render_metric_card("Ready To Build", phase4_metrics["packages_ready_to_build"], "Checklist reviewed packages")
    with export_columns[3]:
        render_metric_card("Integrity Failures", phase4_metrics["integrity_failures"], "Build or verification failures")
    evidence_columns = st.columns(4)
    with evidence_columns[0]:
        render_metric_card("Processing Runs", phase5_metrics["processing_runs"], "Locked corpus processing")
    with evidence_columns[1]:
        render_metric_card("Completed Runs", phase5_metrics["completed_processing_runs"], "Finished evidence pipelines")
    with evidence_columns[2]:
        render_metric_card("Evidence Records", phase5_metrics["evidence_records"], "Cited extracted facts")
    with evidence_columns[3]:
        render_metric_card("Claim Conflicts", phase5_metrics["claim_conflicts"], "Detected evidence disagreements")
    analysis_columns = st.columns(4)
    with analysis_columns[0]:
        render_metric_card("Analysis Runs", phase6_metrics["analysis_runs"], "Recommendation analysis drafts")
    with analysis_columns[1]:
        render_metric_card("PM Approved", phase6_metrics["pm_approved_runs"], "Approved recommendations")
    with analysis_columns[2]:
        render_metric_card("Investment Reports", phase6_metrics["investment_reports"], "DOCX/PDF outputs")
    with analysis_columns[3]:
        render_metric_card("Final Reports", phase6_metrics["final_reports"], "PM-approved final reports")

    st.divider()
    _render_recent_packages()
    st.divider()
    _render_roadmap()


if __name__ == "__main__":
    main()
