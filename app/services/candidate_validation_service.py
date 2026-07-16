from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse

import requests

from app import config
from app.services.http_client import validate_public_http_url
from app.services.official_ir_service import canonicalize_url, investor_relevance


@dataclass(frozen=True)
class CandidateValidation:
    eligible: bool
    status: str
    reason_code: str = ""
    reason: str = ""
    canonical_url: str = ""
    final_url: str = ""
    mime_type: str = ""
    file_extension: str = ""
    file_signature: str = ""
    content_length: int = 0
    sha256_hash: str = ""
    content: bytes = b""


def normalized_candidate_url(url: str) -> str:
    parsed = urlparse(urldefrag((url or "").strip())[0])
    if parsed.username or parsed.password:
        return ""
    return canonicalize_url(parsed.geturl()) if parsed.scheme in {"http", "https"} else ""


def validate_candidate_metadata(
    *,
    title: str,
    url: str,
    slot_type: str,
    company_name: str,
    ticker: str,
    official_domains: set[str] | None = None,
    description: str = "",
    publication_date: str | None = None,
    research_cutoff: str | None = None,
) -> CandidateValidation:
    canonical = normalized_candidate_url(url)
    if not canonical:
        return CandidateValidation(False, "FAILED", "UNSAFE_URL", "The candidate URL is unsafe or malformed.")
    parsed = urlparse(canonical)
    domain = (parsed.hostname or "").lower().removeprefix("www.")
    exclusion_text = f"{title} {description} {parsed.path}".lower().replace("_", " ").replace("-", " ")
    hard_exclusions = (
        "new account", "account application", "credit application", "customer form", "vendor form",
        "supplier form", "w 9", "banking instructions", "employment application", "careers",
        "privacy policy", "terms and conditions", "login", "registration", "product catalogue",
        "product catalog", "sales brochure", "marketing brochure", "order form",
    )
    if any(term in exclusion_text for term in hard_exclusions):
        return CandidateValidation(
            False, "NON_INVESTOR_MATERIAL", "NON_INVESTOR_MATERIAL",
            "Hard exclusion signal indicates non-investor corporate material.", canonical_url=canonical,
        )
    relevant, reason = investor_relevance(title, canonical, context=f"{description} {slot_type}")
    if not relevant:
        return CandidateValidation(False, "NON_INVESTOR_MATERIAL", "NON_INVESTOR_MATERIAL", reason, canonical_url=canonical)
    identity = f"{title} {description} {canonical}".lower()
    company_tokens = [token.lower() for token in company_name.replace("&", " ").split() if len(token) > 2]
    identity_ok = ticker.lower() in identity or any(token in identity for token in company_tokens)
    official = domain in (official_domains or set()) or domain == "sec.gov" or domain.endswith(".sec.gov")
    if not identity_ok and not official:
        return CandidateValidation(False, "COMPANY_MISMATCH", "COMPANY_MISMATCH", "Company identity could not be verified.", canonical_url=canonical)
    if publication_date and research_cutoff:
        try:
            if date.fromisoformat(publication_date[:10]) > date.fromisoformat(research_cutoff[:10]):
                return CandidateValidation(False, "OUTSIDE_WINDOW", "OUTSIDE_WINDOW", "Publication date is after the research cutoff.", canonical_url=canonical)
        except ValueError:
            pass
    return CandidateValidation(True, "METADATA_VALID", canonical_url=canonical)


def validate_candidate_response(
    url: str,
    response: requests.Response,
    *,
    max_bytes: int = config.MAX_DOWNLOAD_BYTES,
) -> CandidateValidation:
    canonical = normalized_candidate_url(url)
    chain = [*list(getattr(response, "history", []) or []), response]
    if len(chain) - 1 > config.IR_MAX_REDIRECTS:
        return CandidateValidation(False, "FAILED", "REDIRECT_LIMIT", "The redirect limit was exceeded.", canonical_url=canonical)
    for hop in chain:
        validation = validate_public_http_url(str(getattr(hop, "url", "") or url))
        if not validation.is_valid:
            return CandidateValidation(False, "FAILED", "UNSAFE_REDIRECT", validation.error, canonical_url=canonical)
    if response.status_code != 200:
        return CandidateValidation(False, "FAILED", "HTTP_ERROR", f"The source returned HTTP {response.status_code}.", canonical_url=canonical)
    content = bytes(response.content or b"")
    if len(content) > max_bytes:
        return CandidateValidation(False, "FAILED", "MAXIMUM_SIZE", "The source exceeds the configured size limit.", canonical_url=canonical)
    final_url = str(getattr(response, "url", "") or url)
    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    extension = Path(urlparse(final_url).path).suffix.lower()
    starts_pdf = content.startswith(b"%PDF")
    looks_html = content[:1024].lstrip().lower().startswith((b"<html", b"<!doctype html")) or b"<html" in content[:1024].lower()
    if extension == ".pdf" or content_type == "application/pdf":
        if not starts_pdf or looks_html:
            return CandidateValidation(False, "MIME_MISMATCH", "MIME_MISMATCH", "A PDF source did not return a valid PDF signature.", canonical_url=canonical, final_url=final_url)
        mime_type, signature = "application/pdf", "%PDF"
    elif content_type in {"text/html", "application/xhtml+xml"} or looks_html:
        if any(marker in content[:4096].lower() for marker in (b"access denied", b"page not found", b"error 404")):
            return CandidateValidation(False, "FAILED", "HTML_ERROR_PAGE", "The source returned an HTML error page.", canonical_url=canonical, final_url=final_url)
        mime_type, signature = "text/html", "HTML"
        extension = extension if extension in {".htm", ".html"} else ".html"
    elif extension in {".mp3", ".m4a", ".mp4"} or content_type.startswith(("audio/", "video/")):
        mime_type, signature = content_type or mimetypes.guess_type(final_url)[0] or "application/octet-stream", "MEDIA"
    else:
        return CandidateValidation(False, "UNSUPPORTED_FORMAT", "UNSUPPORTED_FORMAT", "The source format is not supported for this slot.", canonical_url=canonical, final_url=final_url)
    return CandidateValidation(
        True,
        "CONTENT_VALID",
        canonical_url=canonical,
        final_url=final_url,
        mime_type=mime_type,
        file_extension=extension,
        file_signature=signature,
        content_length=len(content),
        sha256_hash=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def fetch_and_validate_candidate(
    url: str,
    *,
    session: requests.Session | None = None,
    max_bytes: int = config.MAX_DOWNLOAD_BYTES,
) -> CandidateValidation:
    initial = validate_public_http_url(url)
    if not initial.is_valid:
        return CandidateValidation(False, "FAILED", "UNSAFE_URL", initial.error)
    client = session or requests.Session()
    try:
        response = client.get(url, timeout=config.HTTP_TIMEOUT_SECONDS, allow_redirects=True, stream=False)
    except requests.Timeout:
        return CandidateValidation(False, "FAILED", "TIMEOUT", "The source request timed out.")
    except requests.RequestException:
        return CandidateValidation(False, "FAILED", "REQUEST_FAILED", "The source could not be reached.")
    return validate_candidate_response(url, response, max_bytes=max_bytes)
