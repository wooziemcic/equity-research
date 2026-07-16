from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
from collections import deque
from dataclasses import dataclass, replace
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
from app.services.research_window import window_from_package
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
INVESTOR_POSITIVE_SIGNALS = (
    "earnings", "quarterly results", "annual results", "financial results", "investor relations", "investor presentation",
    "earnings presentation", "annual report", "financial supplement", "investor day", "guidance",
    "transcript", "shareholder", "sustainability", "esg report", "acquisition presentation",
    "transaction presentation", "10 k", "10 q", "financial statements",
)
INVESTOR_EXCLUSION_SIGNALS = (
    "new account", "account application", "credit application", "customer application", "customer form",
    "vendor form", "supplier form", "employment application", "w 9", "tax form", "banking instructions",
    "privacy policy", "terms and conditions", "login", "registration form", "marketing brochure",
    "product catalogue", "sales form", "order form",
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


@dataclass(frozen=True)
class OfficialIrCollectionResult:
    official_website_url: str | None
    official_ir_url: str | None
    resolution_status: str
    pages_crawled: int
    materials_discovered: int
    downloaded_now: int
    already_collected: int
    duplicate: int
    not_selected: int
    outside_selected_window: int
    date_review_required: int
    needs_manual_review: int
    failed: int
    warnings: tuple[str, ...] = ()

    def to_summary(self) -> dict[str, Any]:
        return {
            "official_website": self.official_website_url,
            "official_ir_site": self.official_ir_url,
            "resolution_status": self.resolution_status,
            "pages_crawled": self.pages_crawled,
            "discovered": self.materials_discovered,
            "materials_discovered": self.materials_discovered,
            "downloaded": self.downloaded_now,
            "downloaded_now": self.downloaded_now,
            "already_collected": self.already_collected,
            "duplicate": self.duplicate,
            "not_selected": self.not_selected,
            "outside_selected_window": self.outside_selected_window,
            "date_review_required": self.date_review_required,
            "needs_manual_review": self.needs_manual_review,
            "failed": self.failed,
            "skipped": self.not_selected + self.outside_selected_window + self.date_review_required + self.needs_manual_review,
        }


WORKSPACE_CATEGORY_MAP: dict[str, frozenset[str]] = {
    "Earnings releases": frozenset({"Earnings Release"}),
    "Earnings presentations": frozenset({"Earnings Presentation"}),
    "Investor presentations": frozenset({"Investor Presentation"}),
    "Annual reports": frozenset({"Annual Report"}),
    "Investor-day materials": frozenset({"Investor Day"}),
    "Public supplemental materials": frozenset({"Quarterly Supplement", "Financial Supplement", "Official Company Material"}),
    "Public ESG or sustainability reports": frozenset({"ESG / Sustainability"}),
}
IR_CATEGORY_CODE_MAP = {
    "Earnings Release": "earnings_release",
    "Earnings Presentation": "earnings_presentation",
    "Investor Presentation": "investor_presentation",
    "Annual Report": "annual_filing",
    "Investor Day": "investor_day",
    "ESG / Sustainability": "esg_sustainability",
    "Official Transcript": "earnings_transcript",
    "Quarterly Supplement": "company_press_release",
    "Financial Supplement": "company_press_release",
    "Merger / Acquisition Presentation": "investor_presentation",
    "Official Company Material": "company_press_release",
}

Q4_PUBLIC_ROOT_DOMAINS = {"q4inc.com", "q4api.com"}
Q4_PRIVATE_PATH_TERMS = ("/admin", "/private", "/login", "/signin", "/oauth", "/graphql")


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
        self.scripts: list[str] = []
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
        if tag.lower() == "script" and values.get("src"):
            self.scripts.append(values["src"])

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


def _urls_from_verified_package_metadata(
    package: dict[str, Any], *, db_path: Path | str
) -> list[tuple[str, str]]:
    """Reuse official websites verified for another package of the same SEC issuer."""
    ticker = str(package.get("ticker") or "").upper()
    cik = str(package.get("cik") or "").lstrip("0")
    rows: list[tuple[str, str]] = []
    for peer in database.list_packages_by_ticker(ticker, db_path=db_path):
        if peer.get("package_id") == package.get("package_id"):
            continue
        peer_cik = str(peer.get("cik") or "").lstrip("0")
        if cik and peer_cik != cik:
            continue
        url = str(peer.get("official_website_url") or "").strip()
        if not url or str(peer.get("official_website_confidence") or "").upper() not in {"HIGH", "MEDIUM"}:
            continue
        rows.append((url, "Existing verified package metadata"))
    return rows


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
    raw.extend(_urls_from_verified_package_metadata(package, db_path=db_path))
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


def extract_q4_public_endpoints(base_url: str, script_text: str, *, company_domain: str) -> list[str]:
    """Return public Q4/static endpoints explicitly exposed by an official page or script."""
    raw = re.findall(r"https?://[^\s\"'<>\\]+|(?:/[^\s\"'<>\\]+(?:\.json|\.xml|\.rss|\.pdf|\.pptx?))", script_text, flags=re.I)
    raw.extend(
        match.group(1)
        for match in re.finditer(
            r"[\"']([^\"']*(?:evergreen\.q4api|PressRelease|Event|Presentation|FinancialReport|NewsFeed)[^\"']*)[\"']",
            script_text,
            flags=re.I,
        )
    )
    endpoints: list[str] = []
    for value in raw:
        candidate = canonicalize_url(urljoin(base_url, value.strip()))
        if _q4_endpoint_allowed(company_domain, candidate):
            endpoints.append(candidate)
    return list(dict.fromkeys(endpoints))


def _q4_endpoint_allowed(company_domain: str, url: str) -> bool:
    parsed = urlparse(url)
    domain = _domain(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if parsed.scheme != "https" or is_blocked_aggregator(domain):
        return False
    if any(term in path for term in Q4_PRIVATE_PATH_TERMS) or any(term in query for term in ("token=", "apikey=", "api_key=", "secret=")):
        return False
    related = _root_domain(domain) == _root_domain(company_domain) or _root_domain(domain) in Q4_PUBLIC_ROOT_DOMAINS
    if not related:
        return False
    return any(
        term in f"{domain}{path}".lower()
        for term in ("q4api", "q4inc", "pressrelease", "news", "event", "presentation", "financial", ".json", ".xml", ".rss", ".pdf", ".ppt")
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


def investor_relevance(title: str, url: str, *, context: str = "", document_text: str = "") -> tuple[bool, str]:
    haystack = re.sub(r"[^a-z0-9]+", " ", unquote(f"{title} {url} {context} {document_text[:12000]}").lower())
    exclusion = next((term for term in INVESTOR_EXCLUSION_SIGNALS if term in haystack), None)
    if exclusion:
        return False, f"Excluded non-investor material signal: {exclusion}."
    positive = next((term for term in INVESTOR_POSITIVE_SIGNALS if term in haystack), None)
    if not positive:
        return False, "No strong investor-relevance signal was found."
    domain = _domain(url)
    if domain.startswith(("go.", "marketing.", "info.")) and not positive:
        return False, "Marketing subdomains require strong investor-document evidence."
    return True, f"Investor-relevance signal: {positive}."


def classify_ir_material(title: str, url: str, *, context: str = "", document_text: str = "") -> tuple[str, str]:
    relevant, _ = investor_relevance(title, url, context=context, document_text=document_text)
    if not relevant:
        return "Non-Investor Material", "NONE"
    haystack = re.sub(r"[^a-z0-9]+", " ", unquote(f"{title} {url} {context} {document_text[:12000]}").lower())
    for category, terms in CATEGORY_RULES:
        if any(term in haystack for term in terms):
            return category, "HIGH"
    if "10 k" in haystack or "10 q" in haystack or "financial statements" in haystack:
        return "Financial Statements", "HIGH"
    if "guidance" in haystack or "shareholder" in haystack:
        return "Investor Update", "HIGH"
    return "Investor Material", "MEDIUM"


def _bounded_prefix_bytes(response: Any, limit: int = 262_144) -> bytes:
    if hasattr(response, "iter_content"):
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=16_384):
            if not chunk:
                continue
            remaining = limit - total
            chunks.append(bytes(chunk[:remaining]))
            total += min(len(chunk), remaining)
            if total >= limit:
                break
        return b"".join(chunks)
    return bytes(getattr(response, "content", b"") or b"")[:limit]


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
        directly_linked = candidate_domain in linked_domains or (
            method.startswith("q4_public") and _q4_endpoint_allowed(company_domain, url)
        )
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
            prefix = _bounded_prefix_bytes(response)
            relevant, relevance_reason = investor_relevance(title, url, document_text=prefix.decode("latin-1", errors="ignore"))
            if extension == ".pdf" and ("html" in content_type or not prefix.startswith(b"%PDF")):
                material = _rejected_material(
                    package, title, url, url, method, content_type, candidate_domain,
                    "MIME_MISMATCH", "A .pdf URL did not return validated PDF content.",
                )
            elif not relevant:
                material = _rejected_material(
                    package, title, url, url, method, content_type, candidate_domain,
                    "NON_INVESTOR_MATERIAL", relevance_reason,
                )
            else:
                material = _material(
                    package, title, url, url, method, content_type, candidate_domain,
                    context=relevance_reason, document_text=prefix.decode("latin-1", errors="ignore"),
                )
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
                if confidence != "NONE":
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
            is_relevant_html = confidence != "NONE" and child_ext in {"", ".htm", ".html", ".xhtml"}
            relevant, relevance_reason = investor_relevance(title, child, context=text[:4000])
            if is_file and not relevant:
                material = _rejected_material(
                    package, title or Path(urlparse(child).path).name, child, page_canonical, "html_link",
                    mimetypes.guess_type(child)[0] or "application/octet-stream", child_domain,
                    "NON_INVESTOR_MATERIAL", relevance_reason,
                )
                materials[material.canonical_url] = material
            elif (is_file or is_relevant_html) and relevant and ir_domain_allowed(company_domain, child_domain, directly_linked=True):
                if is_file and len(visited) >= config.IR_MAX_PAGES:
                    pending = _material(
                        package, title or Path(urlparse(child).path).name, child, page_canonical,
                        "html_link", mimetypes.guess_type(child)[0] or "application/octet-stream", child_domain,
                        context=text[:4000],
                    )
                    pending = replace(
                        pending, cutoff_eligibility="NEEDS_DATE_REVIEW", download_status="NEEDS_MANUAL_REVIEW",
                        selected=False,
                        rejection_reason="The crawl limit was reached before MIME and signature inspection; analyst approval still runs full validation.",
                    )
                    materials[pending.canonical_url] = pending
                else:
                    queue.appendleft((child, "classified_file" if is_file else "classified_link"))
                if not ir_url:
                    ir_url = f"https://{child_domain}"
            elif confidence != "NONE" and child not in visited:
                queue.appendleft((child, "classified_link"))
        script_shell = any(marker in text.lower() for marker in ("evergreen.q4api", "__next_data__", "window.__data__", "id=\"__next\""))
        if script_shell and method in {"ir_link", "classified_link", "analyst_confirmed"} and len(materials) == material_count_before:
            warnings.append(f"NEEDS_MANUAL_REVIEW: {url}")
        for feed in parser.feeds:
            queue.append((urljoin(url, feed), "feed_link"))
        for script in parser.scripts:
            script_url = canonicalize_url(urljoin(url, script))
            same_company_script = _root_domain(_domain(script_url)) == _root_domain(company_domain)
            if (_q4_endpoint_allowed(company_domain, script_url) or (script_shell and same_company_script and urlparse(script_url).path.lower().endswith(".js"))) and len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                queue.append((script_url, "q4_public_script"))
        for endpoint in extract_q4_public_endpoints(url, text, company_domain=company_domain):
            category, confidence = classify_ir_material("", endpoint)
            extension = Path(urlparse(endpoint).path).suffix.lower()
            if confidence != "NONE" and extension in {".pdf", ".ppt", ".pptx"}:
                queue.appendleft((endpoint, "q4_public_endpoint"))
            elif len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                queue.append((endpoint, "q4_public_endpoint"))
        for child in _json_ld_urls(url, parser.json_ld):
            child_domain = _domain(child)
            if not ir_domain_allowed(company_domain, child_domain, directly_linked=child_domain in linked_domains):
                continue
            category, confidence = classify_ir_material("", child)
            if confidence != "NONE":
                queue.appendleft((child, "json_ld"))
            elif len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                queue.append((child, "json_ld"))
        if method == "homepage":
            for point in extract_ir_entry_points(url, text):
                if len(visited) + len(queue) < config.IR_MAX_PAGES * 2:
                    queue.append((point, "standard_path"))
        if any(warning == f"NEEDS_MANUAL_REVIEW: {url}" for warning in warnings) and len(materials) == material_count_before:
            manual = _manual_review_material(package, url, page_canonical, company_domain)
            materials[manual.canonical_url] = manual
    for material in materials.values():
        _store_material(package["package_id"], run_id, material, db_path=db_path)
    needs_review = sum(item.download_status in {"NEEDS_MANUAL_REVIEW", "NEEDS_JS_MANUAL_REVIEW", "NEEDS_DATE_REVIEW", "DATE_REVIEW_REQUIRED"} for item in materials.values())
    review_only = bool(materials) and all(item.download_status in {"NEEDS_MANUAL_REVIEW", "NEEDS_JS_MANUAL_REVIEW", "NEEDS_DATE_REVIEW", "DATE_REVIEW_REQUIRED", "NON_INVESTOR_MATERIAL", "MIME_MISMATCH"} for item in materials.values())
    status = "NEEDS_MANUAL_REVIEW" if warnings and (not materials or review_only) else "COMPLETED_WITH_WARNINGS" if warnings or errors else "COMPLETED"
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


def _material(
    package: dict[str, Any], title: str, url: str, discovery_page: str, method: str,
    mime_type: str, domain: str, *, context: str = "", document_text: str = "",
) -> OfficialIrMaterial:
    category, confidence = classify_ir_material(title, url, context=context, document_text=document_text)
    apparent = _date_from_text(f"{title} {url}")
    eligible = bool(apparent) and window_from_package(package).contains(apparent)
    status = "INVESTOR_RELEVANT" if eligible else config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW if apparent else "NEEDS_DATE_REVIEW"
    reason = "" if eligible else "Publication date is outside the selected research time window." if apparent else "Publication date could not be verified automatically."
    return OfficialIrMaterial(
        title=title or "Official company material", source_url=url, canonical_url=canonicalize_url(url),
        official_domain=domain, category=category, publication_date=apparent, document_date=apparent,
        mime_type=mime_type.split(";", 1)[0] or "application/octet-stream",
        file_extension=Path(urlparse(url).path).suffix.lower() or (".html" if "html" in mime_type else ""),
        discovery_page=discovery_page, discovery_method=method, confidence=confidence,
        cutoff_eligibility="ELIGIBLE" if eligible else config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW if apparent else "NEEDS_DATE_REVIEW",
        download_status=status,
        selected=eligible and confidence == "HIGH",
        rejection_reason=reason,
    )


def _manual_review_material(package: dict[str, Any], url: str, discovery_page: str, domain: str) -> OfficialIrMaterial:
    return OfficialIrMaterial(
        title=f"{package.get('ticker') or 'Company'} official investor-relations page",
        source_url=url,
        canonical_url=canonicalize_url(url),
        official_domain=domain,
        category="Investor Relations Page",
        publication_date="",
        document_date="",
        mime_type="text/html",
        file_extension=".html",
        discovery_page=discovery_page,
        discovery_method="javascript_manual_review",
        confidence="LOW",
        cutoff_eligibility="NEEDS_DATE_REVIEW",
        download_status="NEEDS_JS_MANUAL_REVIEW",
        selected=False,
        rejection_reason="The official page is JavaScript-loaded and no safe public static material endpoint was available.",
    )


def _rejected_material(
    package: dict[str, Any], title: str, url: str, discovery_page: str, method: str,
    mime_type: str, domain: str, status: str, reason: str,
) -> OfficialIrMaterial:
    del package
    return OfficialIrMaterial(
        title=title or "Rejected corporate material", source_url=url, canonical_url=canonicalize_url(url),
        official_domain=domain, category="Non-Investor Material", publication_date="", document_date="",
        mime_type=mime_type.split(";", 1)[0] or "application/octet-stream",
        file_extension=Path(urlparse(url).path).suffix.lower(), discovery_page=discovery_page,
        discovery_method=method, confidence="NONE", cutoff_eligibility=status,
        download_status=status, selected=False, rejection_reason=reason,
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
    summary = {
        "selected": len(materials), "downloaded_now": 0, "already_collected": 0,
        "duplicate": 0, "failed": 0, "excluded": 0,
    }
    for value in materials:
        item = value.__dict__ if isinstance(value, OfficialIrMaterial) else value
        if item.get("cutoff_eligibility") != "ELIGIBLE":
            summary["excluded"] += 1
            continue
        canonical = canonicalize_url(item.get("canonical_url") or item["source_url"])
        existing = database.get_document_by_url(package["package_id"], canonical, db_path=db_path) or database.get_document_by_url(package["package_id"], item["source_url"], db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["already_collected"] += 1
            _set_ir_candidate_status(item, "ALREADY_COLLECTED", db_path=db_path)
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
            if extension == ".pdf" and "pdf" not in content_type and "octet-stream" not in content_type:
                raise HttpClientError("Official PDF failed MIME validation.")
            if extension == ".pdf" and not content.startswith(b"%PDF"):
                raise HttpClientError("Official PDF failed signature validation.")
            if extension in {".html", ".htm", ".xhtml", ""} and "html" not in content_type and b"<html" not in content[:512].lower():
                raise HttpClientError("Official HTML material failed MIME validation.")
            sha = hashlib.sha256(content).hexdigest()
            if database.get_document_by_hash(package["package_id"], sha, db_path=db_path):
                summary["duplicate"] += 1
                _set_ir_candidate_status(item, "DUPLICATE", db_path=db_path)
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
                                         "discovery_confidence": item.get("confidence"),
                                         "selected_window_status": "ELIGIBLE",
                                         "final_category_code": IR_CATEGORY_CODE_MAP.get(str(item.get("category") or ""), "company_press_release")}, db_path=db_path,
            )
            _set_ir_candidate_status(item, "DOWNLOADED_NOW", db_path=db_path)
            summary["downloaded_now"] += 1
        except Exception as exc:
            summary["failed"] += 1
            _set_ir_candidate_status(item, "FAILED", reason=str(exc), db_path=db_path)
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


def approve_and_download_ir_material(
    package: dict[str, Any], candidate_id: str, *, analyst_identity: str = "analyst",
    session: requests.Session | None = None, db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Approve a likely investor PDF while retaining every normal download security check."""
    del analyst_identity  # Identity is intentionally not expanded into a new user directory in Phase 5.1.
    candidate = next(
        (row for row in database.list_ir_material_candidates(package["package_id"], db_path=db_path) if row["candidate_id"] == candidate_id),
        None,
    )
    if not candidate:
        raise ValueError("The selected IR candidate does not exist in this package.")
    extension = str(candidate.get("file_extension") or Path(urlparse(candidate["source_url"]).path).suffix).lower()
    if extension != ".pdf":
        raise ValueError("Approve And Download is available only for a direct PDF file.")
    validation = validate_public_http_url(candidate["source_url"])
    if not validation.is_valid:
        raise ValueError("The selected IR candidate is not a safe public URL.")
    relevant, reason = investor_relevance(candidate.get("title") or "", candidate["source_url"])
    if not relevant or candidate.get("download_status") in {"NON_INVESTOR_MATERIAL", "MIME_MISMATCH", "BLOCKED_DOMAIN"}:
        raise ValueError(reason or "The selected file is not investor-relevant.")
    source_domain = _domain(candidate["source_url"])
    company_domain = str(package.get("official_website_domain") or package.get("official_ir_domain") or candidate.get("official_domain") or "")
    domain_related = bool(company_domain) and (
        _root_domain(company_domain) == _root_domain(source_domain)
        or _q4_endpoint_allowed(company_domain, candidate["source_url"])
    )
    if not domain_related:
        database.update_ir_material_candidate(
            candidate_id, {"download_status": "BLOCKED_DOMAIN", "rejection_reason": "The source domain is not related to the verified official domain."}, db_path=db_path,
        )
        raise ValueError("The source domain is not related to the verified official domain.")
    approved_at = database.utc_now_iso()
    database.update_ir_material_candidate(
        candidate_id,
        {
            "analyst_approved": 1, "approval_timestamp": approved_at,
            "original_confidence": candidate.get("original_confidence") or candidate.get("confidence"),
        },
        db_path=db_path,
    )
    approved = dict(candidate)
    approved.update({"cutoff_eligibility": "ELIGIBLE", "selected": 1, "analyst_approved": 1})
    summary = download_official_ir_materials(package, [approved], session=session, db_path=db_path)
    result = (
        "DOWNLOADED_NOW" if summary["downloaded_now"] else "ALREADY_COLLECTED" if summary["already_collected"]
        else "DUPLICATE" if summary["duplicate"] else "FAILED"
    )
    database.update_ir_material_candidate(
        candidate_id, {"final_download_result": result, "download_status": result}, db_path=db_path,
    )
    return {"candidate_id": candidate_id, "approval_timestamp": approved_at, "final_download_result": result, **summary}


def _set_ir_candidate_status(
    item: dict[str, Any], status: str, *, reason: str | None = None, db_path: Path | str
) -> None:
    candidate_id = item.get("candidate_id")
    if not candidate_id:
        return
    updates: dict[str, Any] = {"download_status": status}
    if reason:
        updates["rejection_reason"] = reason[:500]
    database.update_ir_material_candidate(str(candidate_id), updates, db_path=db_path)


def resolve_and_collect_official_ir_materials(
    package: dict[str, Any],
    *,
    selected_workspace_categories: list[str] | tuple[str, ...] | None = None,
    analyst_ir_url: str | None = None,
    search_provider: SearchProvider | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> OfficialIrCollectionResult:
    """Resolve, discover, classify, and download official IR materials in one auditable flow."""
    selected_categories = {
        category
        for workspace_label in selected_workspace_categories or ()
        for category in WORKSPACE_CATEGORY_MAP.get(workspace_label, frozenset())
    }
    active_provider = search_provider
    brave_key = config.brave_search_api_key()
    if active_provider is None and config.SEARCH_PROVIDER == "brave" and brave_key:
        active_provider = BraveSearchProvider(brave_key, session=session)

    resolved, candidates = resolve_official_company_website(
        package,
        analyst_url=str(analyst_ir_url or "").strip() or None,
        search_provider=active_provider,
        session=session,
        db_path=db_path,
    )
    if not resolved:
        reason = "No official company website could be verified from SEC metadata, package metadata, SEC filings, the optional override, or the configured search provider."
        _record_ir_resolution_failure(package, reason, db_path=db_path)
        return OfficialIrCollectionResult(
            None, None, "NOT_FOUND", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            (reason, *tuple(reason for candidate in candidates for reason in candidate.rejection_reasons[:1])),
        )

    refreshed = database.get_package_by_package_id(package["package_id"], db_path=db_path) or package
    discovery = discover_official_ir_materials(
        refreshed,
        resolved.url,
        analyst_confirmed_ir_url=str(analyst_ir_url or "").strip() or None,
        session=session,
        db_path=db_path,
    )
    run_id = discovery["run_id"]
    inventory = [
        row for row in database.list_ir_material_candidates(package["package_id"], db_path=db_path)
        if row.get("discovery_run_id") == run_id
    ]
    downloadable: list[dict[str, Any]] = []
    status_counts = {
        "not_selected": 0,
        "outside_selected_window": 0,
        "date_review_required": 0,
        "needs_manual_review": 0,
    }
    for item in inventory:
        status = str(item.get("download_status") or "")
        reason = str(item.get("rejection_reason") or "")
        selected = False
        if status in {"NON_INVESTOR_MATERIAL", "MIME_MISMATCH", "BLOCKED_DOMAIN", "DUPLICATE"}:
            pass
        elif status in {"NEEDS_MANUAL_REVIEW", "NEEDS_JS_MANUAL_REVIEW"}:
            status = "NEEDS_MANUAL_REVIEW"
            reason = reason or "The material requires analyst review before download."
            status_counts["needs_manual_review"] += 1
        elif item.get("cutoff_eligibility") in {"DATE_REVIEW_REQUIRED", "NEEDS_DATE_REVIEW"} or not item.get("publication_date"):
            status = "DATE_REVIEW_REQUIRED" if item.get("cutoff_eligibility") == "DATE_REVIEW_REQUIRED" else "NEEDS_DATE_REVIEW"
            reason = reason or "Publication date could not be verified automatically."
            status_counts["date_review_required"] += 1
        elif item.get("cutoff_eligibility") != "ELIGIBLE":
            status = config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW
            reason = reason or "Publication date is outside the selected research time window."
            status_counts["outside_selected_window"] += 1
        elif item.get("category") not in selected_categories:
            status = "NOT_SELECTED"
            reason = "The material category was not selected in the workspace."
            status_counts["not_selected"] += 1
        else:
            status = "DISCOVERED" if item.get("download_status") == "DISCOVERED" else "INVESTOR_RELEVANT"
            selected = True
            downloadable.append(item)
        database.update_ir_material_candidate(
            item["candidate_id"],
            {"selected": int(selected), "download_status": status, "rejection_reason": reason or None},
            db_path=db_path,
        )

    downloads = download_official_ir_materials(refreshed, downloadable, session=session, db_path=db_path)
    official_ir_url = discovery.get("ir_url") or refreshed.get("official_ir_url")
    resolution_status = (
        "NEEDS_MANUAL_REVIEW"
        if discovery.get("status") == "NEEDS_MANUAL_REVIEW" or status_counts["needs_manual_review"]
        else "COMPLETED_WITH_WARNINGS"
        if discovery.get("warnings") or discovery.get("errors")
        else "COMPLETED"
    )
    database.update_ir_discovery_run(
        run_id,
        {
            "materials_downloaded": downloads["downloaded_now"],
            "materials_needing_review": status_counts["needs_manual_review"] + status_counts["date_review_required"],
        },
        db_path=db_path,
    )
    return OfficialIrCollectionResult(
        resolved.url,
        official_ir_url,
        resolution_status,
        int(discovery.get("pages_crawled") or 0),
        len(inventory),
        downloads["downloaded_now"],
        downloads["already_collected"],
        downloads["duplicate"],
        status_counts["not_selected"],
        status_counts["outside_selected_window"],
        status_counts["date_review_required"],
        status_counts["needs_manual_review"],
        downloads["failed"],
        tuple(discovery.get("warnings") or ()) + tuple(discovery.get("errors") or ()),
    )


def _record_ir_resolution_failure(package: dict[str, Any], reason: str, *, db_path: Path | str) -> None:
    now = database.utc_now_iso()
    database.create_ir_discovery_run(
        {
            "discovery_run_id": f"IRDISC-{secrets.token_hex(8).upper()}",
            "package_id": package["package_id"],
            "official_url": None,
            "official_domain": None,
            "ir_url": None,
            "ir_domain": None,
            "status": "NOT_FOUND",
            "started_at": now,
            "completed_at": now,
            "duration_seconds": 0.0,
            "pages_crawled": 0,
            "materials_discovered": 0,
            "materials_downloaded": 0,
            "materials_needing_review": 0,
            "warnings_json": json.dumps([reason]),
            "errors_json": "[]",
        },
        db_path=db_path,
    )
