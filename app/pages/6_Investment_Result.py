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
from app.services.conflict_audit_service import audit_historical_conflicts
from app.services.processing_pipeline import processing_performance_summary
from app.services.research_workflow_service import (
    package_coverage_summary,
    recommendation_retry_available,
    retry_recommendation_workflow,
)
from app.services.reporting.investment_report import generate_investment_report
from app.services.reporting.memo_quality import MemoGenerationError
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
            st.session_state[config.SESSION_ACTIVE_ANALYSIS_RUN_ID] = labels[selected]["analysis_run_id"]
            st.rerun()
        else:
            return None

    package = database.get_package_by_package_id(analysis["package_id"]) or {}
    version = database.get_package_version(analysis["version_id"]) or {}
    processing_run = database.get_processing_run(analysis["processing_run_id"]) or {}
    decision = database.get_recommendation_decision(analysis["analysis_run_id"]) or {}
    reports = database.list_generated_reports(analysis["analysis_run_id"])
    report = next(
        (
            item
            for item in reports
            if item.get("report_mode") == config.REPORT_MODE
            and item.get("template_version") == config.REPORT_TEMPLATE_VERSION
        ),
        {},
    )
    latest_attempt = database.latest_memo_generation_attempt(analysis["analysis_run_id"])
    if (
        report
        and latest_attempt
        and latest_attempt.get("status") == "MEMO_GENERATION_FAILED"
        and str(latest_attempt.get("created_at") or "") >= str(report.get("created_at") or "")
    ):
        report = {}
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
    recommendation_failed = (
        str(analysis.get("status") or "") == config.ANALYSIS_STATUS_FAILED
        and bool(diagnostics.get("metrics_successfully_calculated"))
    )
    heading = "Why recommendation generation could not complete" if recommendation_failed else "Why metric calculation could not complete"
    with st.expander(heading, expanded=False):
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
    report = context["report"]
    with st.expander("Audit Details", expanded=False):
        if not processing_run:
            st.info("No processing run is available for this analysis record.")
            return
        st.write(f"Package version: `{analysis.get('version_id') or ''}`")
        st.write(f"Processing run: `{processing_run.get('processing_run_id') or ''}`")
        st.write(f"Analysis run: `{analysis.get('analysis_run_id') or ''}`")
        counts = st.columns(4)
        counts[0].metric("Documents", processing_run.get("total_documents") or 0)
        counts[1].metric("Reused", processing_run.get("reused_documents") or 0)
        counts[2].metric("Chunks", processing_run.get("chunks_created") or 0)
        counts[3].metric("Evidence", processing_run.get("evidence_records_created") or 0)
        st.write(f"Citation audit: {report.get('citation_audit_status') or 'Not available'}")
        st.write(f"Evidence coverage: {_percent(analysis.get('evidence_coverage'))}")
        st.write(f"Model / endpoint: {analysis.get('ai_model') or 'Not available'} / {analysis.get('ai_endpoint') or 'Not available'}")
        recommendation_attempts = database.list_recommendation_attempts(analysis["analysis_run_id"])
        if recommendation_attempts:
            latest_recommendation = recommendation_attempts[0]
            st.markdown("**Recommendation generation**")
            st.write(f"Status: {latest_recommendation.get('status') or 'Not available'}")
            st.write(f"Safe failure category: {latest_recommendation.get('failure_category') or 'None'}")
            st.write(f"OpenAI calls: {latest_recommendation.get('openai_call_count') or 0}")
            st.write(
                f"Candidates: {latest_recommendation.get('eligible_candidate_count') or 0} eligible; "
                f"{latest_recommendation.get('supporting_candidate_count') or 0} support; "
                f"{latest_recommendation.get('risk_candidate_count') or 0} risk."
            )
        usage = database.list_openai_usage(processing_run_id=analysis.get("processing_run_id"))
        if usage:
            stage_costs: dict[str, float] = {}
            for row in usage:
                stage = str(row.get("pipeline_stage") or "unknown")
                stage_costs[stage] = stage_costs.get(stage, 0.0) + float(row.get("estimated_cost_usd") or 0)
            st.markdown("**OpenAI usage and estimated cost**")
            labels = {
                "evidence_extraction": "Extraction cost", "thesis_risk_generation": "Thesis-generation cost",
                "recommendation_generation": "Recommendation cost", "investment_memo_synthesis": "Memo-generation cost",
            }
            for stage, label in labels.items():
                st.write(f"{label}: ${stage_costs.get(stage, 0.0):.6f}")
            st.write(f"Total cost for this workflow: ${sum(stage_costs.values()):.6f}")
            reused_extraction_cost = stage_costs.get("evidence_extraction", 0.0) if recommendation_attempts else 0.0
            st.write(f"Total cost saved through reuse: ${reused_extraction_cost:.6f}")
            st.caption("Reuse savings use recorded provider usage only; no document-count estimate is used.")
        attempt = database.latest_memo_generation_attempt(analysis["analysis_run_id"])
        memo_audit = database.latest_memo_quality_audit(analysis["analysis_run_id"])
        if attempt:
            st.markdown("**Memo generation**")
            st.write(f"Status: {attempt.get('status') or 'Not available'}")
            st.write(f"Model / endpoint: {attempt.get('model') or 'Not available'} / {attempt.get('endpoint') or 'Not available'}")
            if attempt.get("error_code"):
                st.write(f"Failure code: {attempt['error_code']}")
            if attempt.get("error_message"):
                st.write(f"Validation reason: {attempt['error_message']}")
            selected_ids = set(json.loads(attempt.get("selected_candidate_ids_json") or "[]"))
            candidates = database.list_memo_evidence_candidates(attempt["attempt_id"])
            if candidates:
                st.dataframe(
                    [
                        {
                            "Selected": row["candidate_id"] in selected_ids,
                            "Kind": row.get("candidate_kind"),
                            "Family": row.get("claim_family"),
                            "Source date": row.get("filing_or_publication_date"),
                            "Citation": row.get("citation"),
                            "Decision": "Eligible" if row.get("eligible_for_memo") else "Rejected",
                            "Reasons": ", ".join(json.loads(row.get("rejection_reasons_json") or "[]")),
                        }
                        for row in candidates
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
        if memo_audit and attempt and memo_audit.get("attempt_id") == attempt.get("attempt_id"):
            st.write(f"Memo quality audit: {memo_audit.get('status') or 'Not available'}")
            quality_checks = {
                key: value
                for key, value in memo_audit.items()
                if key.endswith("_check")
            }
            st.dataframe(
                [{"Check": key.replace("_", " ").title(), "Status": value} for key, value in quality_checks.items()],
                hide_index=True,
                use_container_width=True,
            )
        elif attempt and attempt.get("status") == "MEMO_GENERATION_FAILED":
            st.write("Memo quality audit: Not completed because synthesis validation failed.")
        st.write(f"Workflow duration: {processing_run.get('duration_seconds') or 0:.2f} seconds")
        conflict_summary = database.get_conflict_analysis_summary(processing_run["processing_run_id"])
        if not conflict_summary:
            historical = audit_historical_conflicts(processing_run["processing_run_id"])
            conflict_summary = {
                "valid_unresolved_conflicts": historical.get("valid_unresolved_conflicts", 0),
                "excluded_incomparable_records": historical.get("excluded_incomparable_records", 0),
            }
        st.markdown("**Filtered conflict summary**")
        st.dataframe(
            [{"Measure": key.replace("_", " ").title(), "Count": value} for key, value in conflict_summary.items() if key not in {"processing_run_id", "created_at", "pairs_examined"}],
            hide_index=True,
            use_container_width=True,
        )
        performance = processing_performance_summary(processing_run["processing_run_id"])
        st.markdown("**Processing performance**")
        st.dataframe(
            [{"Stage": key.replace("_", " ").title(), "Seconds": round(value, 3)} for key, value in performance["stage_seconds"].items()],
            hide_index=True,
            use_container_width=True,
        )
        st.write(f"Slowest parser type: {performance['slowest_parser_type']}")
        nav = st.columns(2)
        with nav[0]:
            _safe_page_link("pages/4_Investment_Analysis.py", "Open evidence and review workspace")
        with nav[1]:
            _safe_page_link("pages/5_Generated_Reports.py", "Open report history")


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


def _compact_memo_result(context: dict[str, Any]) -> None:
    memo = _memo_model(context)
    st.title(f"{memo['ticker']} - Equity Research Summary")
    st.caption(memo["company_name"])
    st.markdown(
        f'<span class="result-status-badge">{html.escape(memo["recommendation"])}</span>',
        unsafe_allow_html=True,
    )
    st.write(f"**Confidence:** {memo['confidence']}")
    st.write(f"**Research cutoff:** {memo['research_cutoff']}")
    st.subheader("Investment View")
    st.write(memo["investment_view"])
    for citation in memo.get("investment_view_citations", []):
        st.caption(citation)
    _memo_items("Key Supporting Facts", memo.get("supporting_facts", memo.get("supporting_evidence", [])))
    _memo_items("Key Risks", memo["risks"])
    st.subheader("Important Missing Information")
    for limitation in memo.get("missing_information", memo.get("limitations", [])):
        st.write(limitation)
    st.subheader("Conclusion")
    st.write(memo["conclusion"])


def _memo_items(title: str, rows: list[dict[str, str]]) -> None:
    st.subheader(title)
    if not rows:
        st.write("No sufficiently supported items were available in the locked corpus.")
        return
    for row in rows:
        st.write(row["claim"])
        st.caption(row["citation"])


def _memo_downloads(context: dict[str, Any]) -> None:
    analysis = context["analysis"]
    report = context["report"]
    if not report:
        st.info("Generate the investment memo before downloading result files.")
        return
    st.subheader("Investment Memo")
    cols = st.columns([1.25, 1, 1])
    pdf_path = Path(report.get("pdf_path") or "")
    docx_path = Path(report.get("docx_path") or "")
    if pdf_path.exists():
        cols[0].download_button("Download Investment Memo PDF", pdf_path.read_bytes(), file_name=pdf_path.name, mime="application/pdf", type="primary", use_container_width=True)
    if docx_path.exists():
        cols[1].download_button("Download Investment Memo DOCX", docx_path.read_bytes(), file_name=docx_path.name, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)
    exports = [
        export
        for export in database.list_combined_exports(analysis["analysis_run_id"], limit=25)
        if export.get("report_id") == report.get("report_id")
    ][:1]
    if not exports:
        try:
            exports = [create_combined_export(analysis["analysis_run_id"], report_id=report.get("report_id"))]
        except Exception as exc:
            st.caption(f"Full audit package is not available: {exc}")
    if exports:
        zip_path = Path(exports[0]["zip_path"])
        if zip_path.exists():
            cols[2].download_button("Download Full Audit Package ZIP", zip_path.read_bytes(), file_name=zip_path.name, mime="application/zip", use_container_width=True)


def _memo_model(context: dict[str, Any]) -> dict[str, Any]:
    report = context.get("report") or {}
    try:
        stored = json.loads(report.get("memo_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    return stored


def _memo_review_state(context: dict[str, Any]) -> None:
    analysis_run_id = context["analysis"]["analysis_run_id"]
    attempt = database.latest_memo_generation_attempt(analysis_run_id)
    workflow = database.latest_research_workflow_run(context["analysis"]["package_id"])
    if recommendation_retry_available(workflow):
        st.title("Recommendation generation requires retry")
        st.write("Stored package, processing, evidence, metrics, and conflicts are ready to reuse.")
        if st.button("Retry Recommendation Generation", type="primary"):
            try:
                retry_recommendation_workflow(workflow["workflow_run_id"])
            except Exception as exc:
                st.error(f"Recommendation retry failed safely: {exc}")
            else:
                st.rerun()
        return
    st.title("Memo requires review before release")
    if attempt and attempt.get("status") == "MEMO_GENERATION_FAILED":
        st.write("The candidate, synthesis, or quality checks did not produce a releasable one-page memo.")
    else:
        st.write("A current-template investment memo has not been generated for this analysis run.")
    if st.button("Retry Memo Generation", type="primary"):
        try:
            report = generate_investment_report(analysis_run_id)
        except MemoGenerationError:
            st.error("Memo generation failed validation. Review Audit Details, then retry the memo only.")
        except Exception:
            st.error("Memo generation could not be completed. No research processing was rerun.")
        else:
            st.session_state[config.SESSION_ACTIVE_REPORT_ID] = report["report_id"]
            st.rerun()


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

    if context["report"]:
        _compact_memo_result(context)
        st.divider()
        _memo_downloads(context)
    else:
        _memo_review_state(context)
    st.divider()
    _advanced_review(context)


if __name__ == "__main__":
    main()
