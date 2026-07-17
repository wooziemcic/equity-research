from __future__ import annotations

import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Iterable

import fitz
from bs4 import BeautifulSoup, Comment

from app import config
from app.services.package_artifact_service import list_package_artifacts, sync_package_artifacts
from app.utils import database


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN = 44
SOURCE_ROLES = {
    "SEC_PRIMARY", "COMPANY_PRIMARY", "LICENSED_SELL_SIDE", "LICENSED_CREDIT",
    "LICENSED_INDUSTRY", "LICENSED_MORNINGSTAR", "BLOOMBERG_OUTPUT", "INTERNAL_CUTLER",
    "VALUATION_MODEL", "ANALYST_NOTE",
}

SECTION_RULES = {
    "Liquidity and Capital Resources": {
        "starts": (r"^liquidity and capital resources$", r"^item\s+7\.?\s+management"),
        "ends": (r"^critical accounting", r"^item\s+7a\.?", r"^item\s+3\.?\s+quantitative", r"^quantitative and qualitative"),
    },
    "Business and Risk Factors": {
        "starts": (r"^item\s+1\.?\s+business", r"^business$"),
        "ends": (r"^item\s+2\.?\s+properties",),
    },
    "Executive Compensation": {
        "starts": (
            r"^executive compensation",
            r"^compensation discussion and analysis",
            r"^summary compensation table",
        ),
        "ends": (
            r"^(?:\d{4}\s+)?director compensation(?:\s+table)?",
            r"^pay versus performance",
            r"^equity compensation plan",
            r"^security ownership",
            r"^proposal\s+\d+",
            r"^audit committee",
        ),
    },
    "Financial Statements": {
        "starts": (
            r"^item\s+8\.?\s+financial statements",
            r"^financial statements and supplementary data",
            r"^consolidated statements",
        ),
        "ends": (
            r"^item\s+9\.?",
            r"^changes in and disagreements",
            r"^controls and procedures",
            r"^signatures",
        ),
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token(*parts: str) -> str:
    return "ART-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20].upper()


def _normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ").replace("\u200b", " ")
    return re.sub(r"\s+", " ", value).strip()


def clean_sec_html(html_bytes: bytes) -> tuple[str, list[tuple[str, str]]]:
    """Return safe normalized text blocks without executing or fetching content."""
    if not html_bytes.strip():
        raise ValueError("SEC HTML is empty.")
    soup = BeautifulSoup(html_bytes, "html.parser")
    for node in soup.find_all(["script", "style", "nav", "noscript", "iframe", "object", "embed", "svg"]):
        node.decompose()
    for node in soup.find_all(attrs={"style": re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)}):
        node.decompose()
    for node in soup.find_all(lambda tag: (getattr(tag, "name", "") or "").lower().endswith("hidden")):
        node.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    title = _normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "SEC Filing"
    blocks: list[tuple[str, str]] = []
    emitted: set[int] = set()
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "p", "div", "table", "pre"]):
        if any(id(parent) in emitted for parent in node.parents):
            continue
        if node.name == "div" and node.find(["h1", "h2", "h3", "h4", "h5", "p", "div", "table", "pre"]):
            continue
        if node.name == "table":
            rows = []
            for tr in node.find_all("tr"):
                cells = [_normalize_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
                cells = [cell for cell in cells if cell]
                if cells:
                    rows.append(" | ".join(cells))
            text = "\n".join(rows)
            kind = "table"
        else:
            text = _normalize_text(node.get_text(" ", strip=True))
            kind = "heading" if node.name.startswith("h") or re.match(r"^(part|item)\s+[ivx0-9]+[a-z]?\.?\b", text, re.I) else "paragraph"
        if not text or len(text) < 2:
            continue
        if blocks and text == blocks[-1][1]:
            continue
        blocks.append((kind, text))
        emitted.add(id(node))
    if sum(len(text) for _, text in blocks) < 100:
        raise ValueError("SEC HTML did not contain enough readable filing text.")
    return title[:300], blocks


def _new_page(document: fitz.Document, page_number: int, footer: str) -> tuple[fitz.Page, float]:
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((MARGIN, PAGE_HEIGHT - 24), f"{footer}  |  Page {page_number}", fontsize=7, color=(0.35, 0.35, 0.35))
    return page, float(MARGIN)


def _write_lines(
    document: fitz.Document,
    blocks: Iterable[tuple[str, str]],
    *,
    footer: str,
    source_note: list[str],
) -> None:
    page, y = _new_page(document, 1, footer)
    page_number = 1
    all_blocks = [("heading", source_note[0])] + [("paragraph", line) for line in source_note[1:]] + list(blocks)
    for kind, text in all_blocks:
        font_size = 13 if kind == "heading" else 8 if kind == "table" else 9
        line_height = font_size * 1.35
        width = 70 if kind == "heading" else 104 if kind == "table" else 94
        source_lines = text.splitlines() if kind == "table" else [text]
        lines: list[str] = []
        for source_line in source_lines:
            lines.extend(textwrap.wrap(source_line, width=width, break_long_words=False, replace_whitespace=True) or [""])
        required = line_height * (len(lines) + 1)
        if y + min(required, line_height * 3) > PAGE_HEIGHT - 42:
            page_number += 1
            page, y = _new_page(document, page_number, footer)
        if kind == "heading":
            y += 6
        for line in lines:
            if y + line_height > PAGE_HEIGHT - 42:
                page_number += 1
                page, y = _new_page(document, page_number, footer)
            page.insert_text((MARGIN, y), line, fontsize=font_size, fontname="helv", color=(0, 0, 0))
            y += line_height
        y += 5 if kind == "heading" else 3


def _write_pdf(
    path: Path,
    *,
    title: str,
    blocks: list[tuple[str, str]],
    metadata: dict[str, Any],
    source_note: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    _write_lines(document, blocks, footer=str(metadata.get("ticker") or "SEC Filing"), source_note=source_note)
    document.set_metadata({
        "title": title,
        "author": "Cutler Equity Research Workbench",
        "subject": f"Searchable SEC reader PDF; source SHA-256 {metadata.get('source_sha256', '')}",
        "keywords": "SEC filing, reader PDF, source lineage",
        "creator": f"Cutler Phase 6C renderer {config.SEC_READER_RENDERER_VERSION}",
        "producer": "PyMuPDF static renderer",
        "creationDate": "D:20000101000000Z",
        "modDate": "D:20000101000000Z",
    })
    document.save(path, garbage=4, deflate=True, clean=True)
    document.close()


def qa_reader_pdf(path: Path, *, source_sha256: str, required_text: Iterable[str] = ()) -> dict[str, Any]:
    checks: dict[str, Any] = {"pdf_signature": path.is_file() and path.read_bytes()[:5] == b"%PDF-"}
    try:
        with fitz.open(path) as document:
            texts = [page.get_text("text").strip() for page in document]
            metadata = document.metadata or {}
            checks.update({
                "opens": True,
                "page_count": document.page_count,
                "searchable": sum(len(text) for text in texts) >= 100,
                "blank_page_count": sum(not text for text in texts),
                "source_hash_recorded": source_sha256 in str(metadata.get("subject") or ""),
                "required_text_present": all(any(term.casefold() in text.casefold() for text in texts) for term in required_text),
            })
    except Exception as exc:
        checks.update({"opens": False, "safe_error": type(exc).__name__})
    checks["passed"] = bool(
        checks.get("pdf_signature") and checks.get("opens") and checks.get("searchable")
        and checks.get("blank_page_count", 1) == 0 and checks.get("source_hash_recorded")
        and checks.get("required_text_present", True)
    )
    return checks


def classify_source_role(artifact: dict[str, Any]) -> str:
    artifact_type = str(artifact.get("artifact_type") or "")
    source = " ".join(str(artifact.get(key) or "") for key in ("source_name", "source_institution", "purpose_label", "display_filename")).casefold()
    if artifact_type in {"FULL_FILING", "SEC_READER_PDF", "FILING_SECTION_REFERENCE", "FILING_SECTION_PDF"} or "sec" in source:
        return "SEC_PRIMARY"
    if artifact_type == "INTERNAL_UPLOAD" or "cutler" in source:
        return "VALUATION_MODEL" if any(token in source for token in ("valuation", "model", "multiple")) else "INTERNAL_CUTLER"
    if "morningstar" in source:
        return "LICENSED_MORNINGSTAR"
    if "bloomberg" in source or re.search(r"\b(des|dvd|hds|anr|drsk)\b", source):
        return "BLOOMBERG_OUTPUT"
    if "credit" in source or "moody" in source or "s&p" in source:
        return "LICENSED_CREDIT"
    if "industry" in source:
        return "LICENSED_INDUSTRY"
    if artifact_type == "LICENSED_UPLOAD":
        return "LICENSED_SELL_SIDE"
    if artifact_type == "ANALYST_NOTE":
        return "ANALYST_NOTE"
    return "COMPANY_PRIMARY"


def assign_source_roles(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    artifacts = list_package_artifacts(package_id, include_audit_only=True, db_path=db_path)
    with database.get_connection(db_path) as connection:
        for artifact in artifacts:
            role = classify_source_role(artifact)
            if role not in SOURCE_ROLES:
                raise ValueError(f"Unsupported source role: {role}")
            connection.execute("UPDATE package_artifacts SET source_role=? WHERE artifact_id=?", (role, artifact["artifact_id"]))
    return list_package_artifacts(package_id, include_audit_only=True, db_path=db_path)


def _candidate_metadata(document_id: str, db_path: Path | str) -> dict[str, Any]:
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM discovered_candidates WHERE downloaded_document_id=? ORDER BY updated_at DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except json.JSONDecodeError:
        value = {}
    return value.get("source_metadata", value) if isinstance(value, dict) else {}


def _register_generated(
    parent: dict[str, Any], path: Path, *, artifact_type: str, purpose: str,
    conversion_status: str, qa: dict[str, Any], analysis_eligible: bool,
    db_path: Path | str,
) -> dict[str, Any]:
    artifact_id = _token(parent["package_id"], parent["artifact_id"], artifact_type, purpose)
    now = database.utc_now_iso()
    role = classify_source_role({**parent, "artifact_type": artifact_type, "purpose_label": purpose})
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE package_artifacts SET working_package_inclusion=0, analysis_eligible=0,
               conversion_status=?, qa_status=?, qa_result_json=? WHERE artifact_id=?""",
            (conversion_status, "PASSED" if qa.get("passed") else "FAILED", json.dumps(qa, sort_keys=True), parent["artifact_id"]),
        )
        connection.execute(
            """INSERT INTO package_artifacts(
               artifact_id, source_document_id, package_id, slot_instance_id, assignment_id,
               artifact_type, display_filename, purpose_label, source_section,
               working_package_inclusion, audit_package_inclusion, analysis_eligible,
               conversion_status, artifact_status, created_at, parent_artifact_id, generated_path,
               generated_sha256, generated_size_bytes, page_count, qa_status, qa_result_json,
               source_role, renderer_version, extraction_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'CURRENT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                 display_filename=excluded.display_filename, generated_path=excluded.generated_path,
                 generated_sha256=excluded.generated_sha256, generated_size_bytes=excluded.generated_size_bytes,
                 page_count=excluded.page_count, qa_status=excluded.qa_status,
                 qa_result_json=excluded.qa_result_json, source_role=excluded.source_role,
                 renderer_version=excluded.renderer_version,
                 extraction_version=excluded.extraction_version,
                 working_package_inclusion=excluded.working_package_inclusion,
                 analysis_eligible=excluded.analysis_eligible, conversion_status=excluded.conversion_status,
                 artifact_status='CURRENT', superseded_at=NULL""",
            (artifact_id, parent.get("source_document_id"), parent["package_id"], parent.get("slot_instance_id"),
             parent.get("assignment_id"), artifact_type, path.name, purpose, purpose if artifact_type == "FILING_SECTION_PDF" else None,
             int(qa.get("passed")), int(analysis_eligible and qa.get("passed")), conversion_status, now,
             parent["artifact_id"], str(path), _sha256(path), path.stat().st_size, qa.get("page_count"),
             "PASSED" if qa.get("passed") else "FAILED", json.dumps(qa, sort_keys=True), role,
             config.SEC_READER_RENDERER_VERSION if artifact_type == "SEC_READER_PDF" else None,
             config.SECTION_EXTRACTION_VERSION if artifact_type == "FILING_SECTION_PDF" else None),
        )
        row = connection.execute("SELECT * FROM package_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return dict(row)


def render_sec_reader_pdfs(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> list[dict[str, Any]]:
    sync_package_artifacts(package_id, db_path=db_path)
    artifacts = list_package_artifacts(package_id, include_audit_only=True, db_path=db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path) or {}
    output_dir = config.PACKAGE_DIR / package_id / "phase6c" / "reader_pdfs"
    results: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact["artifact_type"] != "FULL_FILING":
            continue
        source = Path(artifact.get("local_path") or "")
        if not source.is_file():
            continue
        if source.suffix.casefold() == ".pdf" or str(artifact.get("mime_type") or "") == "application/pdf":
            with database.get_connection(db_path) as connection:
                connection.execute(
                    "UPDATE package_artifacts SET source_role='SEC_PRIMARY', conversion_status='SOURCE_PDF_READY' WHERE artifact_id=?",
                    (artifact["artifact_id"],),
                )
            results.append({**artifact, "conversion_status": "SOURCE_PDF_READY"})
            continue
        output = output_dir / f"{Path(artifact['display_filename']).stem}.pdf"
        existing = next((row for row in list_package_artifacts(package_id, include_audit_only=True, db_path=db_path)
                         if row.get("parent_artifact_id") == artifact["artifact_id"] and row["artifact_type"] == "SEC_READER_PDF"), None)
        if existing and Path(existing.get("generated_path") or "").is_file() and existing.get("renderer_version") == config.SEC_READER_RENDERER_VERSION:
            results.append(existing)
            continue
        title, blocks = clean_sec_html(source.read_bytes())
        metadata = _candidate_metadata(artifact["source_document_id"], db_path)
        source_hash = artifact.get("sha256_hash") or _sha256(source)
        source_note = [
            title,
            f"Company: {package.get('company_name') or package.get('ticker')} | Ticker: {package.get('ticker')} | CIK: {package.get('cik') or 'Not recorded'}",
            f"Form: {metadata.get('form_type') or artifact.get('purpose_label')} | Accession: {metadata.get('accession_number') or 'Not recorded'}",
            f"Filing date: {metadata.get('filing_date') or artifact.get('publication_date') or 'Not recorded'} | Report period: {metadata.get('report_period') or 'Not recorded'}",
            f"Original SEC URL: {artifact.get('source_url') or 'Not recorded'}",
            f"Source document SHA-256: {source_hash}",
        ]
        _write_pdf(output, title=title, blocks=blocks, metadata={"ticker": package.get("ticker"), "source_sha256": source_hash}, source_note=source_note)
        qa = qa_reader_pdf(output, source_sha256=source_hash, required_text=(str(package.get("ticker") or ""),))
        status = "READER_PDF_GENERATED" if qa["passed"] else "READER_PDF_QA_FAILED"
        results.append(_register_generated(artifact, output, artifact_type="SEC_READER_PDF", purpose=artifact["purpose_label"],
                                           conversion_status=status, qa=qa, analysis_eligible=True, db_path=db_path))
    return results


def _find_boundary(blocks: list[tuple[str, str]], patterns: tuple[str, ...], start: int = 0) -> int | None:
    for index in range(start, len(blocks)):
        normalized = _normalize_text(blocks[index][1]).casefold()
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            return index
    return None


def set_section_extraction_override(
    package_id: str,
    package_version_id: str,
    source_artifact_id: str,
    section_key: str,
    *,
    start_text: str,
    end_text: str | None = None,
    reason: str,
    actor: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    if not start_text.strip() or not reason.strip():
        raise ValueError("Section override requires start text and a reason.")
    override_id = _token(package_id, package_version_id, source_artifact_id, section_key, start_text.strip(), end_text or "")
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO section_extraction_overrides(
               override_id, package_id, package_version_id, source_artifact_id, section_key,
               start_text, end_text, reason, created_by, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(override_id) DO UPDATE SET reason=excluded.reason, created_by=excluded.created_by,
                 created_at=excluded.created_at""",
            (override_id, package_id, package_version_id, source_artifact_id, section_key, start_text.strip(),
             (end_text or "").strip() or None, reason.strip(), actor, now),
        )
        row = connection.execute("SELECT * FROM section_extraction_overrides WHERE override_id=?", (override_id,)).fetchone()
    return dict(row)


def _latest_override(
    package_version_id: str | None,
    source_artifact_id: str,
    section_key: str,
    db_path: Path | str,
) -> dict[str, Any] | None:
    if not package_version_id:
        return None
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            """SELECT * FROM section_extraction_overrides
               WHERE package_version_id=? AND source_artifact_id=? AND section_key=?
               ORDER BY created_at DESC LIMIT 1""",
            (package_version_id, source_artifact_id, section_key),
        ).fetchone()
    return dict(row) if row else None


def _find_text_boundary(blocks: list[tuple[str, str]], text: str | None, start: int = 0) -> int | None:
    if not text:
        return None
    needle = _normalize_text(text).casefold()
    for index in range(start, len(blocks)):
        haystack = _normalize_text(blocks[index][1]).casefold()
        if needle and needle in haystack:
            return index
    return None


def extract_section_pdfs(
    package_id: str,
    *,
    package_version_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    artifacts = list_package_artifacts(package_id, include_audit_only=True, db_path=db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path) or {}
    output_dir = config.PACKAGE_DIR / package_id / "phase6c" / "section_pdfs"
    results: list[dict[str, Any]] = []
    for reference in artifacts:
        if reference["artifact_type"] != "FILING_SECTION_REFERENCE":
            continue
        source = Path(reference.get("local_path") or "")
        if not source.is_file() or source.suffix.casefold() == ".pdf":
            continue
        title, blocks = clean_sec_html(source.read_bytes())
        rule = SECTION_RULES.get(reference.get("source_section") or reference.get("purpose_label"))
        if not rule:
            continue
        section_key = reference.get("source_section") or reference.get("purpose_label")
        override = _latest_override(package_version_id, reference["artifact_id"], section_key, db_path)
        warnings: list[str] = []
        if override:
            start = _find_text_boundary(blocks, override["start_text"])
            end = _find_text_boundary(blocks, override.get("end_text"), (start or 0) + 1) if start is not None else None
            confidence = "OVERRIDE_CONFIRMED" if start is not None and end is not None else "LOW"
            warnings.append("Analyst section boundary override applied.")
        else:
            start = _find_boundary(blocks, rule["starts"])
            end = _find_boundary(blocks, rule["ends"], (start or 0) + 1) if start is not None else None
            confidence = "HIGH" if start is not None and end is not None else "LOW"
        selected = blocks[start:end] if start is not None and end is not None else []
        if not selected:
            qa = {"passed": False, "confidence": confidence, "warnings": warnings,
                  "reason": "Deterministic section boundaries were not found."}
            with database.get_connection(db_path) as connection:
                connection.execute(
                    "UPDATE package_artifacts SET working_package_inclusion=0, analysis_eligible=0, conversion_status='SECTION_REVIEW_REQUIRED', qa_status='REVIEW_REQUIRED', qa_result_json=? WHERE artifact_id=?",
                    (json.dumps(qa, sort_keys=True), reference["artifact_id"]),
                )
            results.append({**reference, "qa_result": qa})
            continue
        output = output_dir / f"{Path(reference['display_filename']).stem}.pdf"
        source_hash = reference.get("sha256_hash") or _sha256(source)
        source_note = [
            f"{package.get('ticker')} {reference['purpose_label']}",
            f"Extracted from: {title}",
            f"Source document SHA-256: {source_hash}",
            "This PDF contains the deterministically extracted filing section, not the complete filing.",
        ]
        _write_pdf(output, title=reference["purpose_label"], blocks=selected,
                   metadata={"ticker": package.get("ticker"), "source_sha256": source_hash}, source_note=source_note)
        qa = qa_reader_pdf(output, source_sha256=source_hash, required_text=(selected[0][1][:40],))
        qa.update({"confidence": confidence, "warnings": warnings, "start_heading": selected[0][1], "end_heading": blocks[end][1]})
        results.append(_register_generated(reference, output, artifact_type="FILING_SECTION_PDF",
                                           purpose=reference["purpose_label"], conversion_status="SECTION_PDF_GENERATED" if qa["passed"] else "SECTION_PDF_QA_FAILED",
                                           qa=qa, analysis_eligible=True, db_path=db_path))
    return results
