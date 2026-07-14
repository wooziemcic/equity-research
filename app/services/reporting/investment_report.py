from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.services.package_builder import sha256_file
from app.services.reporting.docx_generator import build_docx_report
from app.services.reporting.pdf_generator import build_pdf_report
from app.services.workspace_service import ensure_inside, sanitize_filename
from app.utils import database


VERIFIED = {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
FINANCIAL_CONTEXT = (
    "revenue", "sales", "income", "ebitda", "margin", "cash", "debt", "liquidity",
    "earnings", "expense", "profit", "loss", "flow", "capital", "volume", "backlog",
)
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "net sales", "sales"),
    "sales": ("revenue", "net sales", "sales"),
    "ebitda": ("ebitda",),
    "margin": ("margin",),
    "cash flow": ("cash flow", "operating activities", "investing activities", "financing activities"),
    "liquidity": ("liquidity", "cash", "capital resources", "debt"),
    "debt": ("debt", "borrowings", "notes"),
    "income": ("income", "earnings", "profit", "loss"),
}
AMOUNT_METRIC_TERMS = ("revenue", "sales", "ebitda", "income", "cash", "debt", "liquidity", "flow", "capex", "profit", "loss", "expense")


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
        temp_path.unlink(missing_ok=True)


def citation_audit(analysis_run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    thesis_items = database.list_thesis_items(analysis_run_id, db_path=db_path)
    unsupported = [
        item for item in thesis_items
        if item.get("citation_status") not in VERIFIED and json.loads(item.get("evidence_ids_json") or "[]")
    ]
    uncited_material = [
        item for item in thesis_items
        if item.get("confidence") != config.CONFIDENCE_INSUFFICIENT and not json.loads(item.get("evidence_ids_json") or "[]")
    ]
    return {
        "status": "PASSED" if not unsupported and not uncited_material else "FAILED",
        "unsupported": [item["thesis_item_id"] for item in unsupported],
        "uncited_material": [item["thesis_item_id"] for item in uncited_material],
    }


def build_compact_memo(analysis_run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise ValueError("Analysis run does not exist.")
    version = database.get_package_version(run["version_id"], db_path=db_path) or {}
    package = database.get_package_by_package_id(run["package_id"], db_path=db_path) or {}
    decision = database.get_recommendation_decision(analysis_run_id, db_path=db_path) or {}
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=run["version_id"], db_path=db_path)
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    docs = _document_lookup(run["version_id"], db_path=db_path)
    supporting = _supporting_facts(evidence, docs)
    thesis = database.list_thesis_items(analysis_run_id, db_path=db_path)
    risks = _thesis_facts(thesis, "RISK", evidence_by_id, docs, limit=4)
    limitations = _limitations(run, decision, supporting)
    recommendation = _recommendation(decision, run)
    review_required = recommendation in {"Analyst Review Required", "Insufficient Evidence"}
    investment_view = (
        _review_explanation(run, decision)
        if review_required
        else f"The verified locked-corpus evidence supports a {recommendation} recommendation. The view remains subject to the cited facts, risks, and missing information below."
    )
    return {
        "mode": config.REPORT_MODE,
        "company_name": version.get("company_name") or package.get("company_name") or version.get("ticker") or "Company",
        "ticker": version.get("ticker") or package.get("ticker") or "",
        "research_cutoff": _readable_date(run.get("research_cutoff") or version.get("research_cutoff_date")),
        "recommendation": recommendation,
        "confidence": str(decision.get("confidence") or run.get("confidence") or "Not available").title(),
        "investment_view": _limit_words(investment_view, 120),
        "supporting_facts": supporting[:5],
        "risks": risks[:4],
        "missing_information": limitations[:3],
        "conclusion": _limit_words(_conclusion(recommendation, limitations), 80),
    }


def memo_to_sections(memo: dict[str, Any]) -> list[dict[str, Any]]:
    header = (
        f"Recommendation: {memo['recommendation']}\n"
        f"Confidence: {memo['confidence']}\n"
        f"Research cutoff: {memo['research_cutoff']}"
    )
    sections: list[dict[str, Any]] = [
        {"title": f"{memo['company_name']} ({memo['ticker']}) - Equity Research Summary", "paragraphs": [header]},
        {"title": "Investment View", "paragraphs": [memo["investment_view"]]},
        {"title": "Key Supporting Facts", "paragraphs": _fact_paragraphs(memo.get("supporting_facts", memo.get("supporting_evidence", []))[:5])},
    ]
    sections.extend(
        [
            {"title": "Key Risks", "paragraphs": _fact_paragraphs(memo.get("risks", [])[:4]) or ["No sufficiently supported material risks were extracted from the locked corpus."]},
            {"title": "Important Missing Information", "paragraphs": memo.get("missing_information", memo.get("limitations", []))[:3]},
            {
                "title": "Conclusion",
                "paragraphs": [
                    memo["conclusion"],
                    "Closed-corpus memo: conclusions are limited to the locked research package and its verified citations.",
                ],
            },
        ]
    )
    return sections


def _fact_paragraphs(items: list[dict[str, str]]) -> list[str]:
    paragraphs: list[str] = []
    for item in items:
        paragraphs.extend((_limit_words(item["claim"], 45), item["citation"]))
    return paragraphs


def _document_lookup(version_id: str, *, db_path: Path | str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for version_doc in database.list_package_version_documents(version_id, db_path=db_path):
        original = database.get_document_by_document_id(version_doc.get("original_document_id"), db_path=db_path) or {}
        rows[version_doc["document_id"]] = {**original, **version_doc}
    return rows


def _supporting_facts(evidence: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[tuple[str, str, int, str, dict[str, str]]] = []
    seen: set[str] = set()
    for item in evidence:
        if not _financial_evidence_is_reportable(item, docs):
            continue
        key = str(item.get("source_text_hash") or "") + "|" + str(item.get("metric_name") or "").casefold()
        if key in seen:
            continue
        seen.add(key)
        claim = _limit_words(_source_claim(item), 45)
        claim_key = re.sub(r"\W+", " ", claim.casefold()).strip()
        if claim_key in seen:
            continue
        seen.add(claim_key)
        citation = _citation(item, docs[item["version_document_id"]])
        doc = docs[item["version_document_id"]]
        source_date = doc.get("publication_date") or doc.get("filing_date") or doc.get("document_date")
        candidates.append((_sort_date(source_date), _sort_date(item.get("period")), _source_priority(doc), _claim_family(item.get("metric_name")), {"claim": claim, "citation": citation}))
    candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    selected: list[dict[str, str]] = []
    family_counts: dict[str, int] = {}
    for _, _, _, family, fact in candidates:
        if family_counts.get(family, 0):
            continue
        selected.append(fact)
        family_counts[family] = 1
        if len(selected) == 5:
            return selected
    for _, _, _, family, fact in candidates:
        if fact in selected or family_counts.get(family, 0) >= 2:
            continue
        selected.append(fact)
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(selected) == 5:
            break
    return selected


def _financial_evidence_is_reportable(item: dict[str, Any], docs: dict[str, dict[str, Any]]) -> bool:
    if item.get("verification_status") not in VERIFIED or item.get("value") is None:
        return False
    required = (item.get("metric_name"), item.get("unit"), item.get("period"), item.get("source_text"), item.get("source_locator_json"))
    if not all(required) or item.get("version_document_id") not in docs:
        return False
    doc = docs[item["version_document_id"]]
    if not (doc.get("publication_date") or doc.get("filing_date") or doc.get("document_date")):
        return False
    context = str(item.get("source_text") or "").lower()
    if not any(term in context for term in FINANCIAL_CONTEXT):
        return False
    metric_name = str(item.get("metric_name") or "").strip().lower()
    if not _metric_context_matches(metric_name, context):
        return False
    if ("revenue" in metric_name or "sales" in metric_name) and "industry" in context and "company" not in context and "qxo" not in context:
        return False
    unit = str(item.get("unit") or "").lower()
    if ("%" in unit or "percent" in unit) and len(re.findall(r"\d+(?:\.\d+)?\s*%", context)) > 2:
        return False
    if any(term in metric_name for term in AMOUNT_METRIC_TERMS) and (not item.get("currency") or "%" in unit or "percent" in unit):
        return False
    if any(term in unit for term in ("dollar", "usd", "$", "million", "billion")) and not item.get("currency") and "usd" not in unit and "$" not in unit:
        return False
    value_text = f"{float(item['value']):g}"
    if not re.search(rf"(?<!\d){re.escape(value_text)}(?:0+)?(?!\d)", context.replace(",", "")):
        return False
    period_year = re.search(r"20\d{2}", str(item.get("period") or ""))
    if period_year and period_year.group(0) not in context:
        return False
    locator = json.loads(item.get("source_locator_json") or "{}")
    return bool(locator and (item.get("page_number") or item.get("section_heading") or item.get("cell_or_row_range") or locator.get("display_title")))


def _source_claim(item: dict[str, Any]) -> str:
    text = _clean_text(item.get("claim_text") or item.get("source_text") or "")
    metric_name = str(item.get("metric_name") or "").strip()
    if "ebitda" in metric_name.lower():
        value = f"{float(item['value']):g}"
        currency = str(item.get("currency") or "")
        unit = str(item.get("unit") or "")
        if currency and "dollar" in unit.lower():
            unit = re.sub(r"\bdollars?\b", "", unit, flags=re.I).strip()
        amount = " ".join(part for part in (currency, value, unit) if part)
        return f"{metric_name} was {amount} for {item['period']}."
    if len(text) <= 360:
        return text.rstrip(".") + "."
    value_text = f"{float(item['value']):g}"
    aliases = next(
        (values for key, values in METRIC_ALIASES.items() if key in str(item.get("metric_name") or "").lower()),
        (),
    )
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        lowered = sentence.lower()
        if value_text in sentence.replace(",", "") and any(alias in lowered for alias in aliases):
            return _truncate_claim(sentence)
    return _truncate_claim(text)


def _metric_context_matches(metric_name: str, context: str) -> bool:
    if "revenue" in metric_name or "sales" in metric_name:
        cleaned = context.replace("internal revenue code", "")
        return "net sales" in cleaned or "sales revenue" in cleaned or bool(re.search(r"\brevenue\b", cleaned))
    aliases = next((values for key, values in METRIC_ALIASES.items() if key in metric_name), (metric_name,))
    return any(alias and alias in context for alias in aliases)


def _truncate_claim(value: str, limit: int = 320) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned.rstrip(".") + "."
    shortened = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,;.")
    return shortened + "..."


def _claim_family(value: Any) -> str:
    metric = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if "ebitda" in metric:
        return "ebitda"
    if "revenue" in metric or "sales" in metric:
        return "revenue"
    if "debt" in metric:
        return "debt"
    if "liquidity" in metric or "cash" in metric:
        return "liquidity"
    if "margin" in metric:
        return "margin"
    return metric


def _thesis_facts(
    thesis: list[dict[str, Any]], item_type: str, evidence_by_id: dict[str, dict[str, Any]],
    docs: dict[str, dict[str, Any]], *, limit: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in thesis:
        if item.get("item_type") != item_type or item.get("citation_status") not in VERIFIED:
            continue
        for evidence_id in json.loads(item.get("evidence_ids_json") or "[]"):
            evidence = evidence_by_id.get(evidence_id)
            if not evidence or evidence.get("verification_status") not in VERIFIED or evidence.get("version_document_id") not in docs:
                continue
            claim = _clean_text(item.get("claim") or "")
            key = re.sub(r"\W+", " ", claim.casefold()).strip()
            if not claim or key in seen:
                continue
            seen.add(key)
            rows.append({"claim": _limit_words(claim, 45), "citation": _citation(evidence, docs[evidence["version_document_id"]])})
            break
        if len(rows) >= limit:
            break
    return rows


def _citation(evidence: dict[str, Any], doc: dict[str, Any]) -> str:
    form = str(doc.get("form_type") or "").replace("/A", " amendment")
    title = str(doc.get("title") or doc.get("document_title") or "Official company material")
    ticker = str(doc.get("ticker") or "").strip()
    source = f"{ticker} {form}".strip() if form else title
    filed = doc.get("publication_date") or doc.get("filing_date") or doc.get("document_date")
    filing_source = bool(form)
    pieces = [source, f"{'filed' if filing_source else 'dated'} {_readable_date(filed)}" if filed else ""]
    section = str(evidence.get("section_heading") or "").strip()
    if section and re.search(r"[A-Za-z]", section) and not re.fullmatch(r"FORM\s+\d+-?[A-Z]?", section, flags=re.I):
        pieces.append(section)
    elif evidence.get("page_number"):
        pieces.append(f"page {evidence['page_number']}")
    return f"[From: {', '.join(piece for piece in pieces if piece)}]"


def _limitations(run: dict[str, Any], decision: dict[str, Any], facts: list[dict[str, str]]) -> list[str]:
    rows: list[str] = []
    if run.get("reference_price") is None:
        rows.append("A current reference price and package-contained valuation evidence were unavailable.")
    if not facts:
        rows.append("No financial facts met every verification, unit, period, context, and source-locator requirement.")
    if decision.get("abstention_reason"):
        rows.append(_limit_words(_clean_text(decision["abstention_reason"]), 45))
    rows.append("The memo uses only evidence available in the locked research package as of the cutoff date.")
    return list(dict.fromkeys(rows)) or ["The memo is limited to evidence available in the locked research corpus as of the cutoff date."]


def _recommendation(decision: dict[str, Any], run: dict[str, Any]) -> str:
    value = str(decision.get("effective_rating") or run.get("preliminary_recommendation") or "ANALYST_REVIEW_REQUIRED").upper()
    mapping = {
        "BUY": "Buy", "HOLD": "Hold", "SELL": "Sell",
        "ANALYST_REVIEW_REQUIRED": "Analyst Review Required",
        "INSUFFICIENT_EVIDENCE": "Insufficient Evidence",
        "NEEDS_ANALYST_REVIEW": "Analyst Review Required",
        "ABSTAIN": "Insufficient Evidence",
    }
    return mapping.get(value, "Analyst Review Required")


def _conclusion(recommendation: str, limitations: list[str]) -> str:
    if recommendation in {"Analyst Review Required", "Insufficient Evidence"}:
        return f"{limitations[0]} Analyst review is required before assigning a final recommendation."
    return f"The verified evidence supports a {recommendation} recommendation, subject to the risks and limitations above."


def _review_explanation(run: dict[str, Any], decision: dict[str, Any]) -> str:
    if run.get("reference_price") is None:
        return "Analyst review is required because the locked package does not contain a current reference price or sufficient valuation evidence."
    if decision.get("abstention_reason"):
        return _limit_words(_clean_text(decision["abstention_reason"]), 120)
    return "Analyst review is required because the locked evidence does not support a sufficiently confident final recommendation."


def _source_priority(document: dict[str, Any]) -> int:
    form = str(document.get("form_type") or "").upper()
    title = str(document.get("title") or "").lower()
    if form.startswith(("10-Q", "10-K")):
        return 6
    if "earnings" in title and ("release" in title or "presentation" in title):
        return 5
    if document.get("is_public"):
        return 4
    return 3


def _limit_words(value: Any, limit: int) -> str:
    words = _clean_text(value).split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(" ,;:") + "."


def _clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\b(?:EVD|PV|RUN|PKG|DOC|RPT)-[A-Z0-9-]+\b", "", text, flags=re.I)
    text = re.sub(r"\b[a-f0-9]{64}\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _readable_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%B %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(value or "Not available")


def _sort_date(value: Any) -> str:
    match = re.search(r"(20\d{2})(?:[-/]?(\d{2}))?(?:[-/]?(\d{2}))?", str(value or ""))
    return "" if not match else f"{match.group(1)}-{match.group(2) or '00'}-{match.group(3) or '00'}"


def _report_fingerprint(run: dict[str, Any], decision: dict[str, Any], memo: dict[str, Any], *, db_path: Path | str) -> str:
    evidence = database.list_evidence_records(run["processing_run_id"], version_id=run["version_id"], db_path=db_path)
    metrics = database.list_analysis_metrics(run["analysis_run_id"], db_path=db_path)
    payload = {
        "evidence": _stable_rows(
            (row.get("source_text_hash"), row.get("verification_status"), row.get("updated_at"))
            for row in evidence
        ),
        "metrics": _stable_rows(
            (row.get("metric_code"), row.get("value"), row.get("unit"), row.get("period"), row.get("source_evidence_ids_json"))
            for row in metrics
        ),
        "recommendation": decision,
        "analyst_notes": run.get("analyst_notes"),
        "pm_notes": run.get("pm_notes"),
        "template": config.REPORT_TEMPLATE_VERSION,
        "mode": config.REPORT_MODE,
        "memo": memo,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _stable_rows(rows: Any) -> list[tuple[Any, ...]]:
    return sorted(rows, key=lambda row: json.dumps(row, default=str, separators=(",", ":")))


def fit_memo_to_one_page(memo: dict[str, Any]) -> dict[str, Any]:
    """Remove lower-priority memo items until the professional PDF layout is one page."""
    fitted = json.loads(json.dumps(memo))
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".memo-fit.", suffix=".pdf", dir=config.REPORT_DIR)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        for _ in range(12):
            build_pdf_report(temp_path, memo_to_sections(fitted))
            if _pdf_page_count(temp_path) == 1:
                return fitted
            if len(fitted.get("supporting_facts", [])) > 3:
                fitted["supporting_facts"].pop()
            elif len(fitted.get("risks", [])) > 2:
                fitted["risks"].pop()
            elif len(fitted.get("missing_information", [])) > 1:
                fitted["missing_information"].pop()
            elif len(str(fitted.get("investment_view") or "").split()) > 80:
                fitted["investment_view"] = _limit_words(fitted["investment_view"], 80)
            elif len(str(fitted.get("conclusion") or "").split()) > 55:
                fitted["conclusion"] = _limit_words(fitted["conclusion"], 55)
            else:
                break
    finally:
        temp_path.unlink(missing_ok=True)
    raise ValueError("The investment memo could not fit one readable PDF page within the content limits.")


def _pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def generate_investment_report(
    analysis_run_id: str, *, final: bool = False, db_path: Path | str = config.DATABASE_PATH,
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
        raise ValueError("Final report citation audit failed.")
    memo = fit_memo_to_one_page(build_compact_memo(analysis_run_id, db_path=db_path))
    fingerprint = _report_fingerprint(run, decision, memo, db_path=db_path)
    desired_status = config.REPORT_STATUS_FINAL if final else config.REPORT_STATUS_DRAFT
    for existing in database.list_generated_reports(analysis_run_id, db_path=db_path):
        if existing.get("report_status") == desired_status and existing.get("input_fingerprint") == fingerprint:
            return existing

    report_version = database.next_report_version(analysis_run_id, db_path=db_path)
    root = _report_root(run["version_id"], analysis_run_id)
    suffix = "PM_APPROVED" if final else "DRAFT"
    base_name = sanitize_filename(f"{memo['ticker']}_Investment_Memo_V{report_version:03d}_{suffix}")
    docx_path, pdf_path = root / f"{base_name}.docx", root / f"{base_name}.pdf"
    sections = memo_to_sections(memo)
    started = perf_counter()
    _atomic_build(docx_path, build_docx_report, sections)
    _atomic_build(pdf_path, build_pdf_report, sections)
    if _pdf_page_count(pdf_path) != 1:
        raise ValueError("Investment memo PDF must contain exactly one page.")
    report = {
        "report_id": _report_id(), "analysis_run_id": analysis_run_id, "package_id": run["package_id"],
        "version_id": run["version_id"], "processing_run_id": run["processing_run_id"],
        "report_version": report_version, "report_kind": "INVESTMENT_MEMO", "report_status": desired_status,
        "recommendation": decision.get("effective_rating"), "confidence": decision.get("confidence"),
        "docx_path": str(docx_path), "docx_sha256": sha256_file(docx_path),
        "pdf_path": str(pdf_path), "pdf_sha256": sha256_file(pdf_path),
        "template_version": config.REPORT_TEMPLATE_VERSION, "citation_audit_status": audit["status"],
        "warnings_json": json.dumps([] if audit["status"] == "PASSED" else ["Draft memo contains citation audit warnings."]),
        "input_fingerprint": fingerprint, "report_mode": config.REPORT_MODE,
        "memo_json": json.dumps(memo, sort_keys=True), "duration_seconds": round(perf_counter() - started, 6),
        "created_at": database.utc_now_iso(),
    }
    database.create_generated_report(report, db_path=db_path)
    database.create_package_version_event(
        event_id=_event_id(), parent_package_id=run["package_id"], version_id=run["version_id"],
        event_type="REPORT_GENERATED",
        event_details_json=json.dumps({"analysis_run_id": analysis_run_id, "report_id": report["report_id"], "status": desired_status}, sort_keys=True),
        db_path=db_path,
    )
    return report
