from __future__ import annotations

import hashlib
import re
import secrets
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from app import config
from app.services.http_client import (
    HttpClientError,
    request_with_retries,
    response_bytes_with_limit,
    validate_public_http_url,
)
from app.services.workspace_service import atomic_write_bytes, safe_document_path, sanitize_filename, write_metadata_json
from app.utils import database

DOCUMENT_KEYWORDS = {
    "earnings": "Earnings Release",
    "presentation": "Investor Presentation",
    "annual": "Annual Report",
    "investor day": "Investor Day",
    "supplement": "Supplemental Financials",
    "press": "Press Release",
    "sustainability": "ESG / Sustainability",
    "esg": "ESG / Sustainability",
}


@dataclass(frozen=True)
class IrDocumentCandidate:
    title: str
    url: str
    filename: str
    suggested_category: str
    apparent_date: str
    confidence: str


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._current_href = attrs_dict.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = " ".join(part.strip() for part in self._text if part.strip())
            self.links.append((self._current_href, text))
            self._current_href = None
            self._text = []


def same_domain(base_url: str, candidate_url: str) -> bool:
    return (urlparse(base_url).hostname or "").lower() == (
        urlparse(candidate_url).hostname or ""
    ).lower()


def suggest_category(text: str, url: str) -> tuple[str, str]:
    haystack = f"{text} {url}".lower()
    for keyword, category in DOCUMENT_KEYWORDS.items():
        if keyword in haystack:
            return category, "Medium"
    if urlparse(url).path.lower().endswith(".pdf"):
        return "Public PDF", "Low"
    return "Public Document", "Low"


def apparent_date(text: str, url: str) -> str:
    match = re.search(r"(20\d{2})[-_/\.](0[1-9]|1[0-2])[-_/\.]([0-3]\d)", f"{text} {url}")
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    year = re.search(r"\b(20\d{2})\b", f"{text} {url}")
    return year.group(1) if year else ""


def _robots_allows(url: str, *, session: requests.Session | None = None) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        response = request_with_retries(robots_url, timeout=5, max_retries=1, session=session)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(config.APP_NAME, url)
    except Exception:
        return True


def discover_public_documents(
    start_url: str,
    *,
    session: requests.Session | None = None,
) -> tuple[list[IrDocumentCandidate], str]:
    """Discover same-domain public PDF/document links without browser automation."""
    validation = validate_public_http_url(start_url)
    if not validation.is_valid:
        return [], validation.error
    if not _robots_allows(start_url, session=session):
        return [], "Automatic discovery was not available for this site. Download the public file manually and add it during Phase 3."

    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    discovered: dict[str, IrDocumentCandidate] = {}
    blocked_message = ""

    while queue and len(visited) < config.IR_MAX_PAGES:
        url, depth = queue.popleft()
        if url in visited or depth > config.IR_MAX_DEPTH:
            continue
        visited.add(url)
        try:
            response = request_with_retries(url, session=session)
        except Exception:
            blocked_message = "Automatic discovery was not available for this site. Download the public file manually and add it during Phase 3."
            continue
        content_type = response.headers.get("Content-Type", "").lower()
        if response.status_code >= 400:
            blocked_message = "Automatic discovery was not available for this site. Download the public file manually and add it during Phase 3."
            continue
        if "pdf" in content_type or urlparse(url).path.lower().endswith(".pdf"):
            title = Path(urlparse(url).path).name or "Public PDF"
            category, confidence = suggest_category(title, url)
            discovered[url] = IrDocumentCandidate(
                title=title,
                url=url,
                filename=sanitize_filename(title),
                suggested_category=category,
                apparent_date=apparent_date(title, url),
                confidence=confidence,
            )
            continue
        if "html" not in content_type and "text" not in content_type:
            continue
        extractor = LinkExtractor()
        extractor.feed(response.text)
        if not extractor.links and "<script" in response.text.lower():
            blocked_message = "Automatic discovery was not available for this site. Download the public file manually and add it during Phase 3."
        for href, text in extractor.links:
            absolute = urljoin(url, href)
            if validate_public_http_url(absolute).is_valid is False:
                continue
            if not same_domain(start_url, absolute):
                continue
            path = urlparse(absolute).path.lower()
            category, confidence = suggest_category(text, absolute)
            looks_relevant = path.endswith((".pdf", ".htm", ".html")) and (
                path.endswith(".pdf") or confidence != "Low"
            )
            if path.endswith(".pdf"):
                filename = sanitize_filename(Path(path).name or "public_document.pdf")
                discovered[absolute] = IrDocumentCandidate(
                    title=text or filename,
                    url=absolute,
                    filename=filename,
                    suggested_category=category,
                    apparent_date=apparent_date(text, absolute),
                    confidence=confidence,
                )
            elif looks_relevant and depth < config.IR_MAX_DEPTH:
                queue.append((absolute, depth + 1))

    return list(discovered.values()), blocked_message


def _document_record(
    package: dict[str, Any],
    candidate: IrDocumentCandidate,
    *,
    status: str,
    category: str,
    document_id: str,
    local_path: Path | None = None,
    content: bytes | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    parsed = urlparse(candidate.url)
    return {
        "document_id": document_id,
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": category,
        "document_type": "PDF",
        "title": candidate.title,
        "source_name": "Investor Relations",
        "source_url": candidate.url,
        "source_domain": parsed.hostname,
        "publication_date": candidate.apparent_date,
        "local_filename": local_path.name if local_path else candidate.filename,
        "local_path": str(local_path) if local_path else None,
        "mime_type": "application/pdf",
        "file_size_bytes": len(content) if content else None,
        "sha256_hash": hashlib.sha256(content).hexdigest() if content else None,
        "collection_method": "INVESTOR_RELATIONS",
        "collection_status": status,
        "is_public": True,
        "error_message": error_message,
    }


def download_selected_ir_documents(
    package: dict[str, Any],
    selections: list[tuple[IrDocumentCandidate, str]],
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Download selected public IR PDFs and record every result."""
    run_id = f"RUN-IR-{secrets.token_hex(8).upper()}"
    database.create_collection_run(
        run_id=run_id,
        package_id=package["package_id"],
        source_type="INVESTOR_RELATIONS",
        status=config.COLLECTION_STATUS_RUNNING,
        db_path=db_path,
    )
    summary = {
        "discovered": len(selections),
        "downloaded": 0,
        "downloaded_now": 0,
        "already_collected": 0,
        "duplicate": 0,
        "skipped": 0,
        "failed": 0,
        "not_found": 0,
    }
    for candidate, category in selections:
        existing = database.get_document_by_url(package["package_id"], candidate.url, db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["already_collected"] += 1
            summary["skipped"] += 1
            continue
        try:
            response = request_with_retries(candidate.url, session=session)
            if response.status_code != 200:
                raise HttpClientError(f"IR site returned HTTP {response.status_code}.")
            content_type = response.headers.get("Content-Type", "").lower()
            content = response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES)
            if "pdf" not in content_type and not candidate.url.lower().split("?")[0].endswith(".pdf"):
                raise HttpClientError("Selected investor-relations file is not a PDF.")
            if not content.startswith(b"%PDF"):
                raise HttpClientError("Downloaded file did not contain a valid PDF signature.")
            sha = hashlib.sha256(content).hexdigest()
            existing_hash = database.get_document_by_hash(package["package_id"], sha, db_path=db_path)
            if existing_hash and existing_hash.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
                summary["duplicate"] += 1
                summary["skipped"] += 1
                continue
            path = safe_document_path(package["package_id"], "investor_relations", candidate.filename)
            atomic_write_bytes(path, content)
            database.create_document_record(
                _document_record(
                    package,
                    candidate,
                    status=config.DOCUMENT_STATUS_DOWNLOADED,
                    category=category,
                    document_id=database.generate_document_id("DOC-IR"),
                    local_path=path,
                    content=content,
                ),
                db_path=db_path,
            )
            summary["downloaded"] += 1
            summary["downloaded_now"] += 1
        except Exception as exc:
            database.create_document_record(
                _document_record(
                    package,
                    candidate,
                    status=config.DOCUMENT_STATUS_FAILED,
                    category=category,
                    document_id=database.generate_document_id("DOC-IR"),
                    error_message=str(exc),
                ),
                db_path=db_path,
            )
            if "HTTP 404" in str(exc):
                summary["not_found"] += 1
            else:
                summary["failed"] += 1
    run_status = (
        config.COLLECTION_STATUS_COMPLETE
        if summary["failed"] == 0 and summary["not_found"] == 0
        else config.COLLECTION_STATUS_PARTIAL
        if summary["downloaded"] or summary["skipped"]
        else config.COLLECTION_STATUS_FAILED
    )
    database.update_collection_run(
        run_id,
        status=run_status,
        documents_discovered=summary["discovered"],
        documents_downloaded=summary["downloaded"],
        documents_skipped=summary["skipped"],
        documents_failed=summary["failed"],
        documents_already_collected=summary["already_collected"],
        documents_duplicated=summary["duplicate"],
        documents_not_found=summary["not_found"],
        db_path=db_path,
    )
    database.update_package_collection_state(
        package["package_id"],
        config.STATUS_PUBLIC_COLLECTION_PARTIAL if summary["failed"] else config.STATUS_PUBLIC_COLLECTION,
        db_path=db_path,
    )
    write_metadata_json(
        package["package_id"],
        "ir_discovery_results.json",
        {"documents": [candidate.__dict__ for candidate, _ in selections]},
    )
    return summary
