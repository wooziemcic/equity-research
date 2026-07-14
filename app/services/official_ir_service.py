from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests

from app import config
from app.services.company_resolver import sec_headers
from app.services.http_client import HttpClientError, request_with_retries, response_bytes_with_limit, validate_public_http_url
from app.services.workspace_service import atomic_write_bytes, safe_document_path, sanitize_filename
from app.utils import database


BLOCKED_OFFICIAL_DOMAINS = {
    "finance.yahoo.com", "yahoo.com", "bloomberg.com", "reuters.com", "marketwatch.com",
    "seekingalpha.com", "linkedin.com", "facebook.com", "x.com", "twitter.com", "youtube.com",
}
IR_PATHS = ("/investors", "/investor-relations", "/ir", "/news", "/financials", "/quarterly-results", "/events-and-presentations")
IR_LINK_TERMS = ("investor relations", "investors", "financial results", "quarterly results", "events and presentations")
CATEGORY_RULES = (
    ("Merger / Acquisition Presentation", ("merger", "acquisition presentation", "transaction presentation")),
    ("Earnings Presentation", ("earnings presentation", "results presentation")),
    ("Earnings Release", ("earnings release", "financial results", "quarterly results")),
    ("Investor Day", ("investor day",)),
    ("Quarterly Supplement", ("quarterly supplement", "supplemental financial")),
    ("Financial Supplement", ("financial supplement",)),
    ("ESG / Sustainability", ("sustainability", "esg report")),
    ("Annual Report", ("annual report",)),
    ("Official Transcript", ("transcript",)),
    ("Investor Presentation", ("investor presentation", "corporate presentation", "presentation")),
)


@dataclass(frozen=True)
class OfficialWebsiteCandidate:
    url: str
    domain: str
    discovery_source: str
    confidence: str
    validation_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    analyst_confirmation_status: str = "UNREVIEWED"
    is_verified: bool = False


@dataclass(frozen=True)
class OfficialIrMaterial:
    title: str
    source_url: str
    canonical_url: str
    official_domain: str
    category: str
    publication_date: str
    document_date: str
    mime_type: str
    file_extension: str
    discovery_page: str
    discovery_method: str
    confidence: str
    cutoff_eligibility: str
    download_status: str
    selected: bool
    rejection_reason: str = ""


class SearchProvider(Protocol):
    def search(self, queries: list[str], *, max_results: int) -> list[str]: ...


class BraveSearchProvider:
    def __init__(self, api_key: str, *, session: requests.Session | None = None) -> None:
        self.api_key = api_key
        self.session = session

    def search(self, queries: list[str], *, max_results: int) -> list[str]:
        if not self.api_key:
            return []
        urls: list[str] = []
        for query in queries:
            response = request_with_retries(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                session=_QuerySession(self.session, query, max_results),
            )
            if response.status_code != 200:
                continue
            urls.extend(str(row.get("url") or "") for row in response.json().get("web", {}).get("results", []))
            if len(urls) >= max_results:
                break
        return list(dict.fromkeys(url for url in urls if url))[:max_results]


class _QuerySession:
    def __init__(self, session: requests.Session | None, query: str, count: int) -> None:
        self.session = session or requests.Session()
        self.query = query
        self.count = count

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs["params"] = {"q": self.query, "count": self.count}
        return self.session.get(url, **kwargs)


class HtmlMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.canonical: str = ""
        self.feeds: list[str] = []
        self.json_ld: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._json_ld = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and values.get("href"):
            self._href = values["href"]
            self._text = []
        if tag.lower() == "link":
            rel = values.get("rel", "").lower()
            if "canonical" in rel:
                self.canonical = values.get("href", "")
            if "alternate" in rel and any(term in values.get("type", "").lower() for term in ("rss", "atom")):
                self.feeds.append(values.get("href", ""))
        if tag.lower() == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_ld = True
            self._json_parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)
        if self._json_ld:
            self._json_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(part.strip() for part in self._text if part.strip())))
            self._href = None
            self._text = []
        if tag.lower() == "script" and self._json_ld:
            self.json_ld.append("".join(self._json_parts))
            self._json_ld = False


def canonicalize_url(url: str) -> str:
    return database.normalize_source_url(urldefrag(url.strip())[0]).replace("%2B", "+").replace("%2b", "+")


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _root_domain(domain: str) -> str:
    parts = domain.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _company_root_candidate(url: str) -> str | None:
    domain = _domain(url)
    first_label = domain.split(".", 1)[0]
    if first_label not in {"investors", "investor", "ir"}:
        return None
    return f"https://{_root_domain(domain)}"


def is_blocked_aggregator(domain: str) -> bool:
    normalized = domain.lower().removeprefix("www.")
    return any(normalized == blocked or normalized.endswith(f".{blocked}") for blocked in BLOCKED_OFFICIAL_DOMAINS)


def ir_domain_allowed(company_domain: str, candidate_domain: str, *, directly_linked: bool = False, analyst_confirmed: bool = False) -> bool:
    if is_blocked_aggregator(candidate_domain):
        return False
    if _root_domain(company_domain) == _root_domain(candidate_domain):
        return True
    return bool(directly_linked or analyst_confirmed)


def _response_text(response: Any) -> str:
    value = getattr(response, "text", None)
    if value is not None:
        return str(value)
    return bytes(getattr(response, "content", b"")).decode("utf-8", errors="replace")


def _is_ir_navigation(title: str, url: str) -> bool:
    haystack = f"{title} {urlparse(url).path}".lower().replace("-", " ").replace("_", " ")
    return any(term in haystack for term in IR_LINK_TERMS)


def _bounded_response_text(response: Any) -> str:
    return response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES).decode("utf-8", errors="replace")


def _validate_redirect_chain(response: Any, origin_domain: str) -> str | None:
    history = list(getattr(response, "history", []) or [])
    if len(history) > config.IR_MAX_REDIRECTS:
        return f"Redirect limit exceeded ({config.IR_MAX_REDIRECTS})."
    for hop in [*history, response]:
        hop_url = str(getattr(hop, "url", "") or "")
        if hop_url and not ir_domain_allowed(origin_domain, _domain(hop_url)):
            return "Redirect left the reasonable official-domain path."
    return None


def _json_ld_urls(base_url: str, values: list[str]) -> list[str]:
    urls: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"url", "contenturl", "embedurl", "sameas"}:
                    visit(child)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.strip():
            candidate = canonicalize_url(urljoin(base_url, value.strip()))
            if urlparse(candidate).scheme in {"http", "https"}:
                urls.append(candidate)

    for raw in values:
        try:
            visit(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return list(dict.fromkeys(urls))


def _robots_allows(url: str, *, session: requests.Session | None, cache: dict[str, RobotFileParser]) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            response = request_with_retries(
                parser.url,
                delay_seconds=config.IR_REQUEST_DELAY_SECONDS,
                session=session,
            )
            parser.parse(_bounded_response_text(response).splitlines() if response.status_code == 200 else [])
        except Exception:
            parser.parse([])
        cache[origin] = parser
    return cache[origin].can_fetch("CutlerEquityResearch/1.0", url)


def validate_official_website_candidate(
    package: dict[str, Any],
    url: str,
    *,
    discovery_source: str,
    session: requests.Session | None = None,
) -> OfficialWebsiteCandidate:
    reasons: list[str] = []
    rejections: list[str] = []
    parsed = urlparse(url)
    domain = _domain(url)
    validation = validate_public_http_url(url)
    if not validation.is_valid:
        rejections.append(validation.error)
    if parsed.scheme.lower() != "https":
        rejections.append("Official websites must use HTTPS.")
    if is_blocked_aggregator(domain):
        rejections.append("Aggregator, social-network, or news domains are not official company sites.")
    if not rejections:
        try:
            response = request_with_retries(url, delay_seconds=config.IR_REQUEST_DELAY_SECONDS, session=session)
            if response.status_code >= 400:
                rejections.append(f"Website returned HTTP {response.status_code}.")
            final_url = str(getattr(response, "url", url) or url)
            final_domain = _domain(final_url)
            redirect_error = _validate_redirect_chain(response, domain)
            if redirect_error:
                rejections.append(redirect_error)
            text = _response_text(response).lower()
            company_tokens = [str(package.get("ticker") or "").lower()]
            company_tokens.extend(token for token in re.findall(r"[a-z0-9]+", str(package.get("company_name") or "").lower()) if len(token) >= 3)
            if not any(token and token in text for token in company_tokens):
                rejections.append("Company name or ticker was not found on the page.")
            else:
                reasons.append("Company name or ticker appears on the page.")
            cik = str(package.get("cik") or "").lstrip("0")
            if cik and ("cik" not in text or cik in text):
                reasons.append("No issuer identity conflict with the selected SEC CIK was detected.")
            reasons.append("Public HTTPS destination validated.")
        except Exception as exc:
            rejections.append(str(exc))
    verified = not rejections
    return OfficialWebsiteCandidate(
        url=url,
        domain=domain,
        discovery_source=discovery_source,
        confidence="HIGH" if verified and discovery_source.startswith("SEC") else "MEDIUM" if verified else "LOW",
        validation_reasons=tuple(reasons),
        rejection_reasons=tuple(rejections),
        is_verified=verified,
    )


def _sec_submission_urls(package: dict[str, Any], *, session: requests.Session | None) -> list[tuple[str, str]]:
    cik = package.get("cik")
    if not cik:
        return []
    response = request_with_retries(
        config.SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik), headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS, session=session,
    )
    if response.status_code != 200:
        return []
    payload = response.json()
    return [
        (str(payload.get(field) or ""), f"SEC submissions metadata:{field}")
        for field in ("website", "investorWebsite")
        if payload.get(field)
    ]


def _urls_from_sec_filings(package: dict[str, Any], *, db_path: Path | str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for doc in database.list_documents_by_package(package["package_id"], db_path=db_path):
        if doc.get("collection_method") != "SEC" or not doc.get("local_path"):
            continue
        path = Path(doc["local_path"])
        if not path.exists() or path.stat().st_size > config.MAX_DOWNLOAD_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for url in re.findall(r"https://[^\s\"'<>]+", text, flags=re.I):
            if _domain(url) not in {"sec.gov", "www.sec.gov"}:
                found.append((url.rstrip(".,);"), "Official URL in SEC filing"))
        if found:
            break
    return found[:10]


def resolve_official_company_website(
    package: dict[str, Any],
    *,
    analyst_url: str | None = None,
    search_provider: SearchProvider | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[OfficialWebsiteCandidate | None, list[OfficialWebsiteCandidate]]:
    raw: list[tuple[str, str]] = []
    raw.extend(_sec_submission_urls(package, session=session))
    if package.get("official_website_url"):
        raw.append((package["official_website_url"], "Existing verified company metadata"))
    filing_urls = _urls_from_sec_filings(package, db_path=db_path)
    for url, source in filing_urls:
        root_candidate = _company_root_candidate(url)
        if root_candidate:
            raw.append((root_candidate, f"{source} (company root inferred from official IR subdomain)"))
        raw.append((url, source))
    if analyst_url:
        raw.append((analyst_url, "Analyst-supplied official domain"))
    candidates: list[OfficialWebsiteCandidate] = []
    for url, source in raw:
        if not url or canonicalize_url(url) in {canonicalize_url(item.url) for item in candidates}:
            continue
        candidate = validate_official_website_candidate(package, url, discovery_source=source, session=session)
        candidates.append(candidate)
        _store_website_candidate(package["package_id"], candidate, db_path=db_path)
        if candidate.is_verified:
            database.update_package_official_sites(
                package["package_id"],
                {"official_website_url": candidate.url, "official_website_domain": candidate.domain,
                 "official_website_confidence": candidate.confidence, "official_website_source": candidate.discovery_source,
                 "official_website_checked_at": database.utc_now_iso()}, db_path=db_path,
            )
            return candidate, candidates
    if search_provider:
        queries = [
            f"{package.get('company_name') or ''} investor relations",
            f"{package.get('ticker') or ''} investor relations",
            f"{package.get('company_name') or ''} earnings presentations",
        ]
        for url in search_provider.search(queries, max_results=config.SEARCH_MAX_RESULTS):
            candidate = validate_official_website_candidate(package, url, discovery_source="Optional search provider", session=session)
            candidates.append(candidate)
            _store_website_candidate(package["package_id"], candidate, db_path=db_path)
            if candidate.is_verified:
                database.update_package_official_sites(
                    package["package_id"],
                    {"official_website_url": candidate.url, "official_website_domain": candidate.domain,
                     "official_website_confidence": candidate.confidence, "official_website_source": candidate.discovery_source,
                     "official_website_checked_at": database.utc_now_iso()}, db_path=db_path,
                )
                return candidate, candidates
    return None, candidates


def _store_website_candidate(package_id: str, candidate: OfficialWebsiteCandidate, *, db_path: Path | str) -> None:
    database.upsert_official_website_candidate(
        {"candidate_id": f"WEB-{secrets.token_hex(8).upper()}", "package_id": package_id, "url": candidate.url,
         "domain": candidate.domain, "discovery_source": candidate.discovery_source, "discovered_at": database.utc_now_iso(),
         "confidence": candidate.confidence, "validation_reasons_json": json.dumps(candidate.validation_reasons),
         "rejection_reasons_json": json.dumps(candidate.rejection_reasons),
         "analyst_confirmation_status": candidate.analyst_confirmation_status, "is_verified": int(candidate.is_verified)},
        db_path=db_path,
    )


def extract_ir_entry_points(base_url: str, html: str) -> list[str]:
    parser = HtmlMetadataParser()
    parser.feed(html)
    points = [
        urljoin(base_url, href)
        for href, text in parser.links
        if _is_ir_navigation(text, urljoin(base_url, href))
    ]
    points.extend(urljoin(base_url, path) for path in IR_PATHS)
    points.extend(urljoin(base_url, feed) for feed in parser.feeds)
    points.extend((urljoin(base_url, "/sitemap.xml"),))
    return list(
        dict.fromkeys(
            canonicalize_url(url) for url in points
            if url and urlparse(url).scheme in {"http", "https"}
        )
    )


def parse_sitemap(base_url: str, xml_text: str) -> list[str]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    return [canonicalize_url(urljoin(base_url, (node.text or "").strip())) for node in root.iter() if node.tag.lower().endswith("loc") and (node.text or "").strip()]


def parse_feed(base_url: str, xml_text: str) -> list[tuple[str, str]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    rows: list[tuple[str, str]] = []
    for entry in list(root.iter()):
        if not entry.tag.lower().endswith(("item", "entry")):
            continue
        title = next(((child.text or "").strip() for child in entry if child.tag.lower().endswith("title")), "")
        link = next(((child.text or child.attrib.get("href") or "").strip() for child in entry if child.tag.lower().endswith("link")), "")
        if link:
            rows.append((canonicalize_url(urljoin(base_url, link)), title))
    return rows


def classify_ir_material(title: str, url: str) -> tuple[str, str]:
    haystack = re.sub(r"[^a-z0-9]+", " ", unquote(f"{title} {url}").lower())
    for category, terms in CATEGORY_RULES:
        if any(term in haystack for term in terms):
            return category, "HIGH"
    return "Official Company Material", "LOW"


def _date_from_text(text: str) -> str:
    match = re.search(r"\b(20\d{2})[-_/](0[1-9]|1[0-2])[-_/]([0-3]\d)\b", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def discover_official_ir_materials(
    package: dict[str, Any],
    official_url: str,
    *,
    analyst_confirmed_ir_url: str | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    run_id = f"IRDISC-{secrets.token_hex(8).upper()}"
    started = database.utc_now_iso()
    company_domain = _domain(official_url)
    database.create_ir_discovery_run(
        {"discovery_run_id": run_id, "package_id": package["package_id"], "official_url": official_url,
         "official_domain": company_domain, "ir_url": analyst_confirmed_ir_url, "ir_domain": _domain(analyst_confirmed_ir_url or ""),
         "status": "RUNNING", "started_at": started, "completed_at": None, "duration_seconds": None,
         "pages_crawled": 0, "materials_discovered": 0, "materials_downloaded": 0,
         "materials_needing_review": 0, "warnings_json": "[]", "errors_json": "[]"}, db_path=db_path,
    )
    queue: deque[tuple[str, str]] = deque([(official_url, "homepage")])
    if analyst_confirmed_ir_url:
        queue.append((analyst_confirmed_ir_url, "analyst_confirmed"))
    visited: set[str] = set()
    materials: dict[str, OfficialIrMaterial] = {}
    warnings: list[str] = []
    errors: list[str] = []
    linked_domains: set[str] = set()
    robots_cache: dict[str, RobotFileParser] = {}
    ir_url = analyst_confirmed_ir_url or ""
    while queue and len(visited) < config.IR_MAX_PAGES:
        url, method = queue.popleft()
        url = canonicalize_url(url)
        if url in visited:
            continue
        candidate_domain = _domain(url)
        directly_linked = candidate_domain in linked_domains
        if not ir_domain_allowed(company_domain, candidate_domain, directly_linked=directly_linked, analyst_confirmed=bool(analyst_confirmed_ir_url and url.startswith(analyst_confirmed_ir_url))):
            continue
        validation = validate_public_http_url(url)
        if not validation.is_valid:
            errors.append(validation.error)
            continue
        if not _robots_allows(url, session=session, cache=robots_cache):
            warnings.append(f"ROBOTS_EXCLUDED: {url}")
            visited.add(url)
            continue
        visited.add(url)
        try:
            response = request_with_retries(url, delay_seconds=config.IR_REQUEST_DELAY_SECONDS, session=session)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if response.status_code >= 400:
            continue
        redirect_error = _validate_redirect_chain(response, candidate_domain)
        if redirect_error:
            errors.append(redirect_error)
            continue
        content_type = str(response.headers.get("Content-Type", "")).lower()
        extension = Path(urlparse(url).path).suffix.lower()
        if "pdf" in content_type or extension in {".pdf", ".ppt", ".pptx"}:
            title = Path(urlparse(url).path).name or "Official material"
            material = _material(package, title, url, url, method, content_type, company_domain)
            materials[material.canonical_url] = material
            continue
        try:
            text = _bounded_response_text(response)
        except HttpClientError as exc:
            errors.append(str(exc))
            continue
        if "xml" in content_type or url.lower().endswith((".xml", "/feed", "/rss")):
            sitemap_urls = parse_sitemap(url, text)
            feed_rows = parse_feed(url, text)
            for child in sitemap_urls:
                queue.append((child, "sitemap"))
            for child, title in feed_rows:
                category, confidence = classify_ir_material(title, child)
                if confidence != "LOW":
                    material = _material(package, title, child, url, "feed", mimetypes.guess_type(child)[0] or "text/html", company_domain)
                    materials[material.canonical_url] = material
                else:
                    queue.append((child, "feed"))
            continue
        parser = HtmlMetadataParser()
        parser.feed(text)
        if not parser.links and "<script" in text.lower():
            warnings.append(f"NEEDS_MANUAL_REVIEW: {url}")
        page_canonical = canonicalize_url(urljoin(url, parser.canonical)) if parser.canonical else url
        material_count_before = len(materials)
        for href, title in parser.links:
            child = canonicalize_url(urljoin(url, href))
            if urlparse(child).scheme not in {"http", "https"}:
                continue
            child_domain = _domain(child)
            if child_domain != candidate_domain:
                linked_domains.add(child_domain)
            is_ir_link = _is_ir_navigation(title, child)
            if is_ir_link and child_domain != company_domain and not ir_url:
                ir_url = child
            if is_ir_link and child not in visited:
                queue.appendleft((child, "ir_link"))
            category, confidence = classify_ir_material(title, child)
            child_ext = Path(urlparse(child).path).suffix.lower()
            is_file = child_ext in {".pdf", ".ppt", ".pptx"}
            is_relevant_html = confidence != "LOW" and child_ext in {"", ".htm", ".html", ".xhtml"}
            if (is_file or is_relevant_html) and ir_domain_allowed(company_domain, child_domain, directly_linked=True):
                material = _material(package, title or Path(urlparse(child).path).name, child, page_canonical, "html_link", mimetypes.guess_type(child)[0] or "text/html", child_domain)
                materials[material.canonical_url] = material
                if not ir_url:
                    ir_url = f"https://{child_domain}"
            elif confidence != "LOW" and child not in visited:
                queue.appendleft((child, "classified_link"))
        script_shell = any(marker in text.lower() for marker in ("evergreen.q4api", "__next_data__", "window.__data__", "id=\"__next\""))
        if script_shell and method in {"ir_link", "classified_link", "analyst_confirmed"} and len(materials) == material_count_before:
            warnings.append(f"NEEDS_MANUAL_REVIEW: {url}")
        for feed in parser.feeds:
            queue.append((urljoin(url, feed), "feed_link"))
        for child in _json_ld_urls(url, parser.json_ld):
            child_domain = _domain(child)
            if not ir_domain_allowed(company_domain, child_domain, directly_linked=child_domain in linked_domains):
                continue
            category, confidence = classify_ir_material("", child)
            if confidence != "LOW":
                material = _material(package, Path(urlparse(child).path).name, child, page_canonical, "json_ld", mimetypes.guess_type(child)[0] or "text/html", child_domain)
                materials[material.canonical_url] = material
            elif len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                queue.append((child, "json_ld"))
        if method == "homepage":
            for point in extract_ir_entry_points(url, text):
                if len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                    queue.append((point, "standard_path"))
    for material in materials.values():
        _store_material(package["package_id"], run_id, material, db_path=db_path)
    needs_review = sum(item.confidence == "LOW" or item.download_status == "NEEDS_MANUAL_REVIEW" for item in materials.values()) + len([w for w in warnings if w.startswith("NEEDS_MANUAL_REVIEW")])
    status = "NEEDS_MANUAL_REVIEW" if warnings and not materials else "COMPLETED_WITH_WARNINGS" if warnings or errors else "COMPLETED"
    completed = database.utc_now_iso()
    duration = max(0.0, (datetime.fromisoformat(completed) - datetime.fromisoformat(started)).total_seconds())
    database.update_ir_discovery_run(
        run_id, {"ir_url": ir_url or None, "ir_domain": _domain(ir_url), "status": status, "completed_at": completed,
                 "duration_seconds": duration, "pages_crawled": len(visited), "materials_discovered": len(materials),
                 "materials_needing_review": needs_review, "warnings_json": json.dumps(warnings), "errors_json": json.dumps(errors)}, db_path=db_path,
    )
    if ir_url:
        database.update_package_official_sites(
            package["package_id"], {"official_ir_url": ir_url, "official_ir_domain": _domain(ir_url),
                                    "official_ir_confirmed": int(bool(analyst_confirmed_ir_url))}, db_path=db_path,
        )
    return {"run_id": run_id, "status": status, "pages_crawled": len(visited), "materials": list(materials.values()), "warnings": warnings, "errors": errors, "ir_url": ir_url}


def _material(package: dict[str, Any], title: str, url: str, discovery_page: str, method: str, mime_type: str, domain: str) -> OfficialIrMaterial:
    category, confidence = classify_ir_material(title, url)
    apparent = _date_from_text(f"{title} {url}")
    cutoff = str(package.get("research_cutoff_date") or "")
    eligible = not apparent or len(apparent) < 10 or apparent <= cutoff
    return OfficialIrMaterial(
        title=title or "Official company material", source_url=url, canonical_url=canonicalize_url(url),
        official_domain=domain, category=category, publication_date=apparent, document_date=apparent,
        mime_type=mime_type.split(";", 1)[0] or "application/octet-stream",
        file_extension=Path(urlparse(url).path).suffix.lower() or (".html" if "html" in mime_type else ""),
        discovery_page=discovery_page, discovery_method=method, confidence=confidence,
        cutoff_eligibility="ELIGIBLE" if eligible else "AFTER_CUTOFF", download_status="DISCOVERED" if eligible else "EXCLUDED_CUTOFF",
        selected=eligible and confidence == "HIGH", rejection_reason="" if eligible else "Publication date is after research cutoff.",
    )


def _store_material(package_id: str, run_id: str, material: OfficialIrMaterial, *, db_path: Path | str) -> None:
    database.upsert_ir_material_candidate(
        {"candidate_id": f"IRM-{secrets.token_hex(8).upper()}", "package_id": package_id, "discovery_run_id": run_id,
         "title": material.title, "source_url": material.source_url, "canonical_url": material.canonical_url,
         "official_domain": material.official_domain, "category": material.category,
         "publication_date": material.publication_date or None, "document_date": material.document_date or None,
         "mime_type": material.mime_type, "file_extension": material.file_extension,
         "discovery_page": material.discovery_page, "discovery_method": material.discovery_method,
         "confidence": material.confidence, "cutoff_eligibility": material.cutoff_eligibility,
         "download_status": material.download_status, "selected": int(material.selected),
         "rejection_reason": material.rejection_reason or None, "created_at": database.utc_now_iso()}, db_path=db_path,
    )


def download_official_ir_materials(
    package: dict[str, Any],
    materials: list[OfficialIrMaterial | dict[str, Any]],
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    summary = {"selected": len(materials), "downloaded_now": 0, "already_collected": 0, "duplicate": 0, "failed": 0, "excluded": 0}
    for value in materials:
        item = value.__dict__ if isinstance(value, OfficialIrMaterial) else value
        if item.get("cutoff_eligibility") != "ELIGIBLE":
            summary["excluded"] += 1
            continue
        canonical = canonicalize_url(item.get("canonical_url") or item["source_url"])
        existing = database.get_document_by_url(package["package_id"], canonical, db_path=db_path) or database.get_document_by_url(package["package_id"], item["source_url"], db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["already_collected"] += 1
            continue
        try:
            response = request_with_retries(item["source_url"], delay_seconds=config.IR_REQUEST_DELAY_SECONDS, session=session)
            if response.status_code != 200:
                raise HttpClientError(f"Official IR source returned HTTP {response.status_code}.")
            final_domain = _domain(str(getattr(response, "url", item["source_url"]) or item["source_url"]))
            redirect_error = _validate_redirect_chain(response, item["official_domain"])
            if redirect_error:
                raise HttpClientError(redirect_error)
            content = response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES)
            content_type = str(response.headers.get("Content-Type", item.get("mime_type") or "")).lower()
            extension = str(item.get("file_extension") or Path(urlparse(item["source_url"]).path).suffix or (".html" if "html" in content_type else ""))
            if extension == ".pdf" and not content.startswith(b"%PDF"):
                raise HttpClientError("Official PDF failed signature validation.")
            if extension in {".html", ".htm", ".xhtml", ""} and "html" not in content_type and b"<html" not in content[:512].lower():
                raise HttpClientError("Official HTML material failed MIME validation.")
            sha = hashlib.sha256(content).hexdigest()
            if database.get_document_by_hash(package["package_id"], sha, db_path=db_path):
                summary["duplicate"] += 1
                continue
            filename = sanitize_filename(Path(urlparse(item["source_url"]).path).name or f"{package['ticker']}_{item['category']}{extension or '.html'}")
            path = safe_document_path(package["package_id"], "investor_relations", filename)
            atomic_write_bytes(path, content)
            created = database.create_document_record(
                {"document_id": database.generate_document_id("DOC-IR"), "package_id": package["package_id"], "ticker": package["ticker"],
                 "category": item["category"], "document_type": "HTML" if extension in {"", ".html", ".htm", ".xhtml"} else extension.lstrip(".").upper(),
                 "title": item["title"], "source_name": "Official Investor Relations", "source_url": canonical,
                 "source_domain": item["official_domain"], "publication_date": item.get("publication_date"),
                 "local_filename": path.name, "local_path": str(path), "mime_type": content_type.split(";", 1)[0] or mimetypes.guess_type(path.name)[0],
                 "file_size_bytes": len(content), "sha256_hash": sha, "collection_method": "INVESTOR_RELATIONS",
                 "collection_status": config.DOCUMENT_STATUS_DOWNLOADED, "is_public": True}, db_path=db_path,
            )
            database.update_document_metadata(
                created["document_id"], {"canonical_url": canonical, "official_domain": item["official_domain"],
                                         "discovery_page": item.get("discovery_page"), "discovery_method": item.get("discovery_method"),
                                         "discovery_confidence": item.get("confidence")}, db_path=db_path,
            )
            if item.get("candidate_id"):
                database.update_ir_material_candidate(item["candidate_id"], {"download_status": "DOWNLOADED"}, db_path=db_path)
            summary["downloaded_now"] += 1
        except Exception:
            summary["failed"] += 1
    run_ids = {
        str((value.__dict__ if isinstance(value, OfficialIrMaterial) else value).get("discovery_run_id") or "")
        for value in materials
    }
    for run_id in run_ids - {""}:
        run = next((row for row in database.list_ir_discovery_runs(package["package_id"], db_path=db_path) if row["discovery_run_id"] == run_id), None)
        if run:
            database.update_ir_discovery_run(
                run_id,
                {"materials_downloaded": int(run.get("materials_downloaded") or 0) + summary["downloaded_now"]},
                db_path=db_path,
            )
    return summary
