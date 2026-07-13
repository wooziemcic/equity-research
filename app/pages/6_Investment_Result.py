from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from app import config
from app.components.cards import render_empty_state
from app.components.layout import bootstrap_page
from app.services.analysis_pipeline import load_analysis_diagnostics
from app.services.combined_export_service import create_combined_export
from app.services.research_workflow_service import package_coverage_summary
from app.utils import database


def _safe_page_link(page: str, label: str) -> None:
    try:
        st.page_link(page, label=label)
    except StreamlitPageNotFoundError:
        st.caption(label)


def _load_result_context() -> dict[str, Any] | None:
    analysis_run_id = st.session_state.get(config.SESSION_ACTIVE_ANALYSIS_RUN_ID)
    analysis = database.get_analysis_run(analysis_run_id) if analysis_run_id else None

    package_id = st.session_state.get(config.SESSION_ACTIVE_PACKAGE_ID)
    if not analysis and package_id:
        workflow = database.latest_research_workflow_run(package_id)
        if workflow and workflow.get("analysis_run_id"):
            analysis = database.get_analysis_run(workflow["analysis_run_id"])
    if not analysis and package_id:
        runs = database.list_analysis_runs(package_id=package_id, limit=1)
        analysis = runs[0] if runs else None
    if not analysis:
        runs = database.list_analysis_runs(limit=25)
        if not runs:
            render_empty_state("No investment result is available.", "Build a package and generate analysis from the Research Workspace.")
            _safe_page_link("pages/0_Research_Workspace.py", "Open Research Workspace")
            return None
        labels = {f"{run['analysis_run_id']} - {run['package_id']} - {run.get('preliminary_recommendation') or 'Pending'}": run for run in runs}
        selected = st.selectbox("Open analysis run", options=list(labels.keys()))
        if st.button("Open Result", type="primary"):
            analysis = labels[selected]
        else:
            return None

    package = database.get_package_by_package_id(analysis["package_id"]) or {}
    version = database.get_package_version(analysis["version_id"]) or {}
    processing_run = database.get_processing_run(analysis["processing_run_id"]) or {}
    decision = database.get_recommendation_decision(analysis["analysis_run_id"]) or {}
    reports = database.list_generated_reports(analysis["analysis_run_id"])
    report = reports[0] if reports else {}
    st.session_state[config.SESSION_ACTIVE_PACKAGE_ID] = analysis["package_id"]
    st.session_state[config.SESSION_ACTIVE_VERSION_ID] = analysis["version_id"]
    st.session_state[config.SESSION_ACTIVE_PROCESSING_RUN_ID] = analysis["processing_run_id"]
    st.session_state[config.SESSION_ACTIVE_ANALYSIS_RUN_ID] = analysis["analysis_run_id"]
    st.session_state[config.SESSION_ACTIVE_REPORT_ID] = report.get("report_id")
    st.session_state[config.SESSION_ACTIVE_TICKER] = version.get("ticker") or package.get("ticker")
    st.session_state["active_package"] = package
    return {
        "package": package,
        "version": version,
        "processing_run": processing_run,
        "analysis": analysis,
        "decision": decision,
        "report": report,
    }


def _signal(analysis: dict[str, Any], decision: dict[str, Any]) -> tuple[str, str]:
    if analysis.get("pm_approved_recommendation"):
        return "PM Approved Signal", analysis["pm_approved_recommendation"]
    if analysis.get("analyst_adjusted_recommendation"):
        return "Analyst Reviewed Signal", analysis["analyst_adjusted_recommendation"]
    rating = decision.get("effective_rating") or analysis.get("preliminary_recommendation") or "ANALYST_REVIEW_REQUIRED"
    return "Preliminary Signal", rating


def _header(context: dict[str, Any]) -> None:
    package = context["package"]
    version = context["version"]
    analysis = context["analysis"]
    label, rating = _signal(analysis, context["decision"])
    st.markdown(
        f"""
        <div class="result-header">
            <div>
                <div class="eyebrow">Investment Result</div>
                <div class="workspace-ticker">{html.escape(version.get("ticker") or package.get("ticker") or "")}</div>
                <div class="workspace-company">{html.escape(version.get("company_name") or package.get("company_name") or "")}</div>
            </div>
            <div class="signal-panel">
                <span>{html.escape(label)}</span>
                <strong>{html.escape(str(rating).replace("_", " "))}</strong>
            </div>
        </div>
        <div class="result-meta">
            <div><span>Package Version</span><strong>{html.escape(analysis.get("version_id") or "")}</strong></div>
            <div><span>Research Cutoff</span><strong>{html.escape(analysis.get("research_cutoff") or version.get("research_cutoff_date") or "")}</strong></div>
            <div><span>Analysis Run</span><strong>{html.escape(analysis.get("analysis_run_id") or "")}</strong></div>
            <div><span>Recommendation Status</span><strong>{html.escape(analysis.get("status") or "")}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _summary_cards(context: dict[str, Any]) -> None:
    package = context["package"]
    analysis = context["analysis"]
    decision = context["decision"]
    report = context["report"]
    processing_run = context["processing_run"]
    coverage = package_coverage_summary(package) if package else {}
    conflicts = database.list_claim_conflicts(processing_run["processing_run_id"]) if processing_run else []
    scenarios = database.list_analysis_scenarios(analysis["analysis_run_id"])
    priced = [scenario for scenario in scenarios if scenario.get("upside_downside") is not None]
    upside = priced[0].get("upside_downside") if priced else None
    cols = st.columns(4)
    cols[0].metric("Recommendation", (decision.get("effective_rating") or analysis.get("preliminary_recommendation") or "").replace("_", " "))
    cols[1].metric("Confidence", decision.get("confidence") or analysis.get("confidence") or "")
    cols[2].metric("Evidence coverage", _percent(analysis.get("evidence_coverage")))
    cols[3].metric("Package coverage", _percent(analysis.get("package_coverage")))
    cols = st.columns(4)
    cols[0].metric("Reference price", _money(analysis.get("reference_price"), analysis.get("reference_price_currency")))
    cols[1].metric("Upside / downside", _percent(upside))
    cols[2].metric("Unresolved conflicts", len([item for item in conflicts if item.get("analyst_status") != "RESOLVED"]))
    cols[3].metric("Citation audit", report.get("citation_audit_status") or "Not generated")
    st.caption(
        f"Research package coverage: {coverage.get('public_files', 0)} public files, {coverage.get('licensed_files', 0)} licensed files, {coverage.get('missing_core', 0)} missing core items."
    )


def _report_sections(context: dict[str, Any]) -> None:
    analysis = context["analysis"]
    decision = context["decision"]
    metrics = database.list_analysis_metrics(analysis["analysis_run_id"])
    scorecard = database.list_scorecard_items(analysis["analysis_run_id"])
    scenarios = database.list_analysis_scenarios(analysis["analysis_run_id"])
    thesis = database.list_thesis_items(analysis["analysis_run_id"])
    evidence = database.list_evidence_records(analysis["processing_run_id"], version_id=analysis["version_id"])
    evidence_by_id = {item["evidence_id"]: item for item in evidence}

    st.subheader("Executive Summary")
    st.write(decision.get("recommendation_rationale") or "No recommendation rationale was generated.")
    st.caption(_citation_caption(thesis, evidence_by_id))

    st.subheader("Recommendation Rationale")
    st.write(f"Why not Buy: {decision.get('why_not_buy') or 'Not available'}")
    st.write(f"Why not Hold: {decision.get('why_not_hold') or 'Not available'}")
    st.write(f"Why not Sell: {decision.get('why_not_sell') or 'Not available'}")
    if decision.get("abstention_reason"):
        st.warning(decision["abstention_reason"])

    st.subheader("Company Overview")
    st.dataframe(
        [
            {
                "Document": doc.get("title"),
                "Category": doc.get("category"),
                "Path": doc.get("relative_package_path"),
                "SHA-256": doc.get("sha256_hash"),
            }
            for doc in database.list_package_version_documents(analysis["version_id"])
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Key Financial Evidence")
    st.dataframe(
        [
            {
                "Metric": metric["display_name"],
                "Value": metric.get("value"),
                "Unit": metric.get("unit") or metric.get("currency") or "",
                "Period": metric.get("period") or "",
                "Citation Evidence IDs": metric.get("source_evidence_ids_json") or "[]",
                "Warning": metric.get("warning") or "",
            }
            for metric in metrics
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Valuation")
    if analysis.get("reference_price") is None:
        st.info("No package-contained reference price was available, so live valuation was not fetched or invented.")
    st.dataframe(
        [
            {
                "Scenario": scenario["scenario_name"],
                "Implied Value": scenario.get("implied_value"),
                "Reference Price": scenario.get("reference_price"),
                "Upside / Downside": _percent(scenario.get("upside_downside")),
                "Probability": _percent(scenario.get("probability")),
                "Warnings": scenario.get("warnings_json") or "[]",
            }
            for scenario in scenarios
        ],
        hide_index=True,
        use_container_width=True,
    )

    _thesis_section("Bull Case", thesis, evidence_by_id, item_type="BULL_CASE")
    _thesis_section("Bear Case", thesis, evidence_by_id, item_type="BEAR_CASE")
    _thesis_section("Catalysts", thesis, evidence_by_id, item_type="CATALYST")
    _thesis_section("Risks", thesis, evidence_by_id, item_type="RISK")

    st.subheader("Scenario Analysis")
    st.dataframe(
        [
            {
                "Scenario": scenario["scenario_name"],
                "Assumptions": scenario.get("scenario_assumptions_json"),
                "Evidence IDs": scenario.get("evidence_ids_json"),
            }
            for scenario in scenarios
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("What Would Change The Rating")
    weak_items = [item for item in scorecard if float(item.get("effective_score") or 0) < 5]
    if weak_items:
        for item in weak_items[:6]:
            st.write(f"{item['pillar_name']}: {item['rationale']}")
    else:
        st.write("No low-scoring scorecard pillars were generated.")

    st.subheader("Evidence Limitations")
    st.write(f"Evidence coverage: {_percent(analysis.get('evidence_coverage'))}")
    st.write(f"Recommendation confidence: {decision.get('confidence') or analysis.get('confidence') or 'Not available'}")
    _metric_diagnostics_expander(analysis)

    st.subheader("Sources And Citations")
    _evidence_table(evidence)


def _metric_diagnostics_expander(analysis: dict[str, Any]) -> None:
    payload = load_analysis_diagnostics(analysis)
    diagnostics = payload.get("metric_diagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, dict):
        return
    with st.expander("Why metric calculation could not complete", expanded=False):
        if diagnostics.get("exception_type"):
            st.write(f"Exception type: `{diagnostics.get('exception_type')}`")
        if diagnostics.get("safe_error_message"):
            st.write(f"Safe error message: {diagnostics.get('safe_error_message')}")
        cols = st.columns(4)
        cols[0].metric("Evidence records", diagnostics.get("evidence_records", 0))
        cols[1].metric("Verified", diagnostics.get("verified_records", 0))
        cols[2].metric("Accepted", diagnostics.get("accepted_records", 0))
        cols[3].metric("Numeric values", diagnostics.get("numeric_value_records", 0))
        limitations = payload.get("limitations") if isinstance(payload, dict) else []
        for limitation in limitations or []:
            st.write(f"- {limitation}")


def _thesis_section(title: str, thesis: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], *, item_type: str) -> None:
    st.subheader(title)
    items = [item for item in thesis if item.get("item_type") == item_type]
    if not items:
        st.write("No section items were generated.")
        return
    for item in items:
        st.write(item.get("claim") or "")
        st.caption(_item_citations(item, evidence_by_id))


def _advanced_review(context: dict[str, Any]) -> None:
    analysis = context["analysis"]
    processing_run = context["processing_run"]
    with st.expander("Advanced Review", expanded=False):
        cols = st.columns(5)
        with cols[0]:
            _safe_page_link("pages/4_Investment_Analysis.py", "Evidence ledger")
        with cols[1]:
            _safe_page_link("pages/4_Investment_Analysis.py", "Citation verification")
        with cols[2]:
            _safe_page_link("pages/4_Investment_Analysis.py", "Conflicts")
        with cols[3]:
            _safe_page_link("pages/4_Investment_Analysis.py", "Analyst / PM review")
        with cols[4]:
            _safe_page_link("pages/5_Generated_Reports.py", "Reports")

        if not processing_run:
            st.info("No processing run is available for this analysis record.")
            return

        st.markdown("**Evidence ledger**")
        _evidence_table(database.list_evidence_records(processing_run["processing_run_id"], version_id=analysis["version_id"]))
        st.markdown("**Citation verification**")
        st.dataframe(database.list_citation_verifications(processing_run_id=processing_run["processing_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Conflicts**")
        st.dataframe(database.list_claim_conflicts(processing_run["processing_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Duplicate lineage**")
        st.dataframe(database.list_duplicate_groups(processing_run["processing_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Financial metrics**")
        st.dataframe(database.list_analysis_metrics(analysis["analysis_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Scorecard**")
        st.dataframe(database.list_scorecard_items(analysis["analysis_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Bull/base/bear scenarios**")
        st.dataframe(database.list_analysis_scenarios(analysis["analysis_run_id"]), hide_index=True, use_container_width=True)
        st.markdown("**Audit history**")
        st.dataframe(database.list_package_version_events(analysis["package_id"]), hide_index=True, use_container_width=True)


def _download(context: dict[str, Any]) -> None:
    analysis = context["analysis"]
    report = context["report"]
    st.subheader("Final Download")
    if not report:
        st.info("Generate a draft report before creating the combined package download.")
        return
    exports = database.list_combined_exports(analysis["analysis_run_id"], limit=5)
    if st.button("Download Research Package + AI Report", type="primary", use_container_width=True):
        try:
            export = create_combined_export(analysis["analysis_run_id"], report_id=report.get("report_id"))
            st.success(f"Combined export created: {Path(export['zip_path']).name}")
            exports = [export] + exports
        except Exception as exc:
            st.error(f"Combined export could not be created: {exc}")
    if exports:
        latest = exports[0]
        path = Path(latest["zip_path"])
        if path.exists():
            with path.open("rb") as handle:
                st.download_button(
                    "Download Latest ZIP",
                    data=handle.read(),
                    file_name=path.name,
                    mime="application/zip",
                    use_container_width=True,
                )
            st.caption(f"Export hash: `{latest['zip_sha256']}`")


def _evidence_table(evidence: list[dict[str, Any]]) -> None:
    st.dataframe(
        [
            {
                "Evidence ID": item["evidence_id"],
                "Claim": item.get("claim_text"),
                "Document": item.get("version_document_id"),
                "Page": item.get("page_number"),
                "Sheet": item.get("sheet_name"),
                "Cell / Row": item.get("cell_or_row_range"),
                "Verification": item.get("verification_status"),
                "Analyst Status": item.get("analyst_status"),
            }
            for item in evidence
        ],
        hide_index=True,
        use_container_width=True,
    )


def _citation_caption(thesis: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    citations = []
    for item in thesis[:5]:
        text = _item_citations(item, evidence_by_id)
        if text:
            citations.append(text)
    return " | ".join(citations) if citations else "No citations generated for this section."


def _item_citations(item: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    evidence_ids = json.loads(item.get("evidence_ids_json") or "[]")
    parts = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id, {})
        locator = json.loads(evidence.get("source_locator_json") or "{}")
        source = locator.get("display_title") or evidence.get("version_document_id") or evidence_id
        location = []
        if evidence.get("page_number"):
            location.append(f"p. {evidence['page_number']}")
        if evidence.get("sheet_name"):
            location.append(str(evidence["sheet_name"]))
        if evidence.get("cell_or_row_range"):
            location.append(str(evidence["cell_or_row_range"]))
        suffix = ", ".join(location)
        parts.append(f"[{source}{': ' + suffix if suffix else ''}]")
    return " ".join(parts)


def _percent(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _money(value: Any, currency: str | None) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        return f"{currency or ''} {float(value):,.2f}".strip()
    except (TypeError, ValueError):
        return str(value)


def main() -> None:
    bootstrap_page("Result")
    st.session_state[config.SESSION_PRIMARY_SCREEN] = "Result"

    context = _load_result_context()
    if not context:
        return

    _header(context)
    st.divider()
    _summary_cards(context)
    st.divider()
    _report_sections(context)
    st.divider()
    _advanced_review(context)
    st.divider()
    _download(context)


if __name__ == "__main__":
    main()
