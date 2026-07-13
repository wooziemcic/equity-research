from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

import requests

from app import config

logger = logging.getLogger(__name__)


class HttpClientError(RuntimeError):
    """Raised when a bounded HTTP request fails."""


@dataclass(frozen=True)
class UrlValidation:
    is_valid: bool
    error: str = ""


def validate_public_http_url(url: str) -> UrlValidation:
    """Validate analyst-entered URLs and block private-network targets."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return UrlValidation(False, "Only http and https URLs are allowed.")
    if not parsed.netloc:
        return UrlValidation(False, "Enter a complete URL with a host.")
    host = parsed.hostname or ""
    if host.lower() in {"localhost"} or host.lower().endswith(".localhost"):
        return UrlValidation(False, "Localhost URLs are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return UrlValidation(False, "Private-network URLs are not allowed.")
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return UrlValidation(False, "Private-network hosts are not allowed.")
        except OSError:
            return UrlValidation(False, "The URL host could not be resolved.")
    return UrlValidation(True)


def request_with_retries(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = config.HTTP_TIMEOUT_SECONDS,
    max_retries: int = config.HTTP_MAX_RETRIES,
    delay_seconds: float = 0.0,
    session: requests.Session | None = None,
) -> requests.Response:
    """Make a bounded GET request with exponential backoff for temporary failures."""
    client = session or requests.Session()
    last_error: Exception | None = None
    for attempt in range(max_retries):
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            response = client.get(url, headers=dict(headers or {}), timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = HttpClientError(f"Temporary HTTP {response.status_code}")
                time.sleep(min(2 ** attempt, 8))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 8))
    raise HttpClientError("Request failed after bounded retries.") from last_error


def response_bytes_with_limit(response: requests.Response, *, max_bytes: int) -> bytes:
    """Read response content only if it stays below the configured size limit."""
    content = response.content
    if len(content) > max_bytes:
        raise HttpClientError("Downloaded file exceeds the configured size limit.")
    return content
