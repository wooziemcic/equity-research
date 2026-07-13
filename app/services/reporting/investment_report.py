from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from app import config
from app.services.package_builder import sha256_file
from app.services.reporting.docx_generator import build_docx_report
from app.services.reporting.pdf_generator import build_pdf_report
from app.services.workspace_service import ensure_inside, sanitize_filename
from app.utils import database


def _report_id() -> str:
    return f"RPT-{secrets.token_hex(8).upper()}"


def _event_id() -> str:
    return f"PVE-{secrets.token_hex(8).upper()}"


def _report_root(version_id: str, analysis_run_id: str) -> Path:
    root = config.REPORT_DIR / sanitize_filename(version_id) / sanitize_filename(analysis_run_id)
    ensure_inside(config.REPORT_DIR, root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _atomic_build(path: Path, builder: Any, sections: list[dict[str, Any]]) -> None:
    ensure_inside(config.REPORT_DIR, path)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        builder(temp_path, sections)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def citation_audit(analysis_run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    thesis_items = database.list_thesis_items(analysis_run_id, db_path=db_path)
    unsupported = [
        item
        for item in thesis_items
        if item.get("citation_status") not in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
        and json.loads(item.get("evidence_ids_json") or "[]")
    ]
    uncited_material = [
        item
        for item in thesis_items
        if item.get("confidence") != config.CONFIDENCE_INSUFFICIENT and not json.loads(item.get("evidence_ids_json") or "[]")
    ]
    status = "PASSED" if not unsupported and not uncited_material else "FAILED"
    return {
        "status": status,
        "unsupported": [item["thesis_item_id"] for item in unsupported],
        "uncited_material": [item["thesis_item_id"] for item in uncited_material],
    }


def generate_investment_report(
    analysis_run_id: str,
    *,
    final: bool = False,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise ValueError("Analysis run does not exist.")
    if config.OPENAI_REQUIRED and run.get("ai_review_status") != config.AI_REVIEW_STATUS_COMPLETED:
        raise ValueError("OpenAI analysis is required before report narrative generation.")
    if final and run.get("status") != config.ANALYSIS_STATUS_PM_APPROVED:
        raise ValueError("Final report generation requires PM approval.")
    decision = database.get_recommendation_decision(analysis_run_id, db_path=db_path)
    if not decision:
        raise ValueError("Recommendation decision is required before report generation.")
    audit = citation_audit(analysis_run_id, db_path=db_path)
    if final and audit["status"] != "PASSED":
        version = database.get_package_version(run["version_id"], db_path=db_path)
        if version:
            database.create_package_version_event(
                event_id=_event_id(),
                parent_package_id=run["package_id"],
                version_id=run["version_id"],
                event_type="CITATION_AUDIT_FAILED",
                event_details_json=json.dumps({"analysis_run_id": analysis_run_id, "audit": audit}, sort_keys=True),
                db_path=db_path,
            )
        raise ValueError("Final report citation audit failed.")
    existing_reports = database.list_generated_reports(analysis_run_id, db_path=db_path)
    desired_status = config.REPORT_STATUS_FINAL if final else config.REPORT_STATUS_DRAFT
    for existing in existing_reports:
        if existing.get("report_status") == desired_status:
            return existing
    report_version = database.next_report_version(analysis_run_id, db_path=db_path)
    report_status = config.REPORT_STATUS_FINAL if final else config.REPORT_STATUS_DRAFT
    root = _report_root(run["version_id"], analysis_run_id)
    ticker = _ticker_for_run(run, db_path=db_path)
    suffix = "PM_APPROVED" if final else "DRAFT"
    base_name = sanitize_filename(f"{ticker}_Investment_Report_V{report_version:03d}_{suffix}")
    docx_path = root / f"{base_name}.docx"
    pdf_path = root / f"{base_name}.pdf"
    sections = _report_sections(run, decision, audit, db_path=db_path)
    _atomic_build(docx_path, build_docx_report, sections)
    _atomic_build(pdf_path, build_pdf_report, sections)
    report = {
        "report_id": _report_id(),
        "analysis_run_id": analysis_run_id,
        "package_id": run["package_id"],
        "version_id": run["version_id"],
        "processing_run_id": run["processing_run_id"],
        "report_version": report_version,
        "report_kind": "INVESTMENT_REPORT",
        "report_status": report_status,
        "recommendation": decision.get("effective_rating"),
        "confidence": decision.get("confidence"),
        "docx_path": str(docx_path),
        "docx_sha256": sha256_file(docx_path),
        "pdf_path": str(pdf_path),
        "pdf_sha256": sha256_file(pdf_path),
        "template_version": config.REPORT_TEMPLATE_VERSION,
        "citation_audit_status": audit["status"],
        "warnings_json": json.dumps([] if audit["status"] == "PASSED" else ["Draft report contains citation audit warnings."], sort_keys=True),
        "created_at": database.utc_now_iso(),
    }
    database.create_generated_report(report, db_path=db_path)
    database.create_package_version_event(
        event_id=_event_id(),
        parent_package_id=run["package_id"],
        version_id=run["version_id"],
        event_type="REPORT_GENERATED",
        event_details_json=json.dumps({"analysis_run_id": analysis_run_id, "report_id": report["report_id"], "status": report_status}, sort_keys=True),
        db_path=db_path,
    )
    return report


def _ticker_for_run(run: dict[str, Any], *, db_path: Path | str) -> str:
    version = database.get_package_version(run["version_id"], db_path=db_path) or {}
    return version.get("ticker") or "Research"


def _report_sections(run: dict[str, Any], decision: dict[str, Any], audit: dict[str, Any], *, db_path: Path | str) -> list[dict[str, Any]]:
    metrics = database.list_analysis_metrics(run["analysis_run_id"], db_path=db_path)
    scorecard = database.list_scorecard_items(run["analysis_run_id"], db_path=db_path)
    scenarios = database.list_analysis_scenarios(run["analysis_run_id"], db_path=db_path)
    thesis = database.list_thesis_items(run["analysis_run_id"], db_path=db_path)
    version_docs = database.list_package_version_documents(run["version_id"], db_path=db_path)
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=run["version_id"], db_path=db_path)
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    return [
        {
            "title": "Cover Page",
            "paragraphs": [
                "Cutler Research AI Investment Report",
                f"Package version: {run['version_id']}",
                f"Processing run: {run['processing_run_id']}",
                f"Analysis run: {run['analysis_run_id']}",
                f"Research cutoff: {run.get('research_cutoff')}",
                f"Recommendation status: {run.get('status')}",
                f"PM approval exists: {run.get('status') == config.ANALYSIS_STATUS_PM_APPROVED}",
                "Closed-corpus limitation: no external sources, web search, or live market data were used.",
                "This system produces an evidence-grounded research draft for analyst and portfolio-manager review. It does not independently execute investment decisions or trades.",
            ],
            "page_break": True,
        },
        {
            "title": "Investment Conclusion",
            "paragraphs": [
                f"Preliminary recommendation: {decision.get('preliminary_rating')}",
                f"Effective recommendation: {decision.get('effective_rating')}",
                f"Confidence: {decision.get('confidence')}",
                decision.get("recommendation_rationale") or "",
            ],
        },
        {
            "title": "Why The Other Ratings Were Not Selected",
            "paragraphs": [
                f"Why not Buy: {decision.get('why_not_buy')}",
                f"Why not Hold: {decision.get('why_not_hold')}",
                f"Why not Sell: {decision.get('why_not_sell')}",
                f"Abstention reason: {decision.get('abstention_reason') or 'None'}",
            ],
        },
        {
            "title": "Financial Metrics",
            "tables": [
                {
                    "rows": [["Metric", "Value", "Unit", "Period", "Evidence IDs", "Warning"]]
                    + [
                        [
                            metric["display_name"],
                            metric.get("value"),
                            metric.get("unit") or metric.get("currency") or "",
                            metric.get("period") or "",
                            metric.get("source_evidence_ids_json") or "[]",
                            metric.get("warning") or "",
                        ]
                        for metric in metrics
                    ]
                }
            ],
        },
        {
            "title": "Scorecard",
            "tables": [
                {
                    "rows": [["Pillar", "Score", "Weight", "Effective", "Rationale"]]
                    + [
                        [item["pillar_name"], item["score"], item["weight"], item["effective_score"], item["rationale"]]
                        for item in scorecard
                    ]
                }
            ],
        },
        {
            "title": "Bull / Base / Bear Scenarios",
            "tables": [
                {
                    "rows": [["Scenario", "Implied Value", "Reference Price", "Upside/Downside", "Probability", "Warnings"]]
                    + [
                        [
                            scenario["scenario_name"],
                            scenario.get("implied_value"),
                            scenario.get("reference_price"),
                            scenario.get("upside_downside"),
                            scenario.get("probability"),
                            scenario.get("warnings_json"),
                        ]
                        for scenario in scenarios
                    ]
                }
            ],
        },
        {
            "title": "Thesis, Catalysts, And Risks",
            "paragraphs": [_thesis_paragraph(item, evidence_by_id) for item in thesis],
        },
        {
            "title": "Evidence Coverage And Citation Audit",
            "paragraphs": [
                f"Evidence coverage: {run.get('evidence_coverage')}",
                f"Recommendation confidence: {run.get('confidence')}",
                f"Citation audit status: {audit['status']}",
                f"Unsupported thesis items: {audit['unsupported']}",
                f"Uncited material items: {audit['uncited_material']}",
            ],
        },
        {
            "title": "Source Inventory",
            "tables": [
                {
                    "rows": [["Version Document ID", "Title", "Path", "SHA-256"]]
                    + [[doc["document_id"], doc.get("title") or "", doc["relative_package_path"], doc["sha256_hash"]] for doc in version_docs]
                }
            ],
        },
        {
            "title": "Analyst Review And PM Approval",
            "paragraphs": [
                f"Analyst recommendation: {run.get('analyst_adjusted_recommendation') or 'Pending'}",
                f"Analyst notes: {run.get('analyst_notes') or ''}",
                f"PM recommendation: {run.get('pm_approved_recommendation') or 'Pending'}",
                f"PM notes: {run.get('pm_notes') or ''}",
            ],
        },
    ]


def _thesis_paragraph(item: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    evidence_ids = json.loads(item.get("evidence_ids_json") or "[]")
    citations = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id, {})
        locator = json.loads(evidence.get("source_locator_json") or "{}")
        title = locator.get("display_title") or evidence.get("version_document_id") or evidence_id
        pieces = [str(title)]
        if evidence.get("page_number"):
            pieces.append(f"p. {evidence['page_number']}")
        if evidence.get("sheet_name"):
            pieces.append(str(evidence["sheet_name"]))
        if evidence.get("cell_or_row_range"):
            pieces.append(str(evidence["cell_or_row_range"]))
        citations.append("[" + ", ".join(pieces) + "]")
    return f"{item['item_type']}: {item['claim']} {' '.join(citations)}"
