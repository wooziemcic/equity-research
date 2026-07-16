from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import requests

from app import config


_SAFE_RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit",
    "x-ratelimit-policy",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


@dataclass(frozen=True)
class BraveSearchRequest:
    query: str
    count: int = 10
    offset: int = 0
    country: str = config.BRAVE_SEARCH_COUNTRY
    search_language: str = config.BRAVE_SEARCH_LANGUAGE
    ui_language: str = config.BRAVE_SEARCH_UI_LANGUAGE
    safesearch: str = config.BRAVE_SEARCH_SAFESEARCH
    freshness: str | None = None
    extra_snippets: bool = config.BRAVE_SEARCH_EXTRA_SNIPPETS
    package_id: str | None = None
    slot_instance_id: str | None = None
    search_run_id: str | None = None
    query_purpose: str = "SLOT_DISCOVERY"


@dataclass(frozen=True)
class BraveSearchResult:
    rank: int
    title: str
    url: str
    description: str = ""
    extra_snippets: tuple[str, ...] = ()
    page_age: str = ""
    language: str = ""
    content_type_hint: str = ""
    profile: str = ""
    source_provider: str = "brave"


@dataclass(frozen=True)
class BraveSearchResponse:
    query: str
    results: tuple[BraveSearchResult, ...] = ()
    more_results_available: bool = False
    request_duration_ms: int = 0
    result_count: int = 0
    response_status: str = "SUCCESS"
    safe_rate_limit_metadata: dict[str, str] = field(default_factory=dict)
    provider_request_id: str = ""
    cache_status: str = "MISS"
    error_category: str = ""
    safe_error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BraveSearchResponse":
        return cls(
            **{
                **payload,
                "results": tuple(BraveSearchResult(**row) for row in payload.get("results", [])),
            }
        )


class BraveSearchClient:
    """Bounded Brave Web Search client that never places credentials in a URL."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = config.BRAVE_SEARCH_ENDPOINT,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self.endpoint = endpoint
        self.session = session or requests.Session()
        self.successful_request_count = 0
        self.total_result_count = 0

    def is_configured(self) -> bool:
        return bool(self._api_key and self.endpoint.startswith("https://"))

    @staticmethod
    def retry_policy() -> dict[str, float | int]:
        return {
            "max_retries": config.BRAVE_REQUEST_MAX_RETRIES,
            "backoff_seconds": config.BRAVE_REQUEST_BACKOFF_SECONDS,
            "timeout_seconds": config.BRAVE_REQUEST_TIMEOUT_SECONDS,
        }

    @staticmethod
    def safe_rate_limit_metadata(headers: Any) -> dict[str, str]:
        return {
            str(key).lower(): str(value)
            for key, value in dict(headers or {}).items()
            if str(key).lower() in _SAFE_RATE_LIMIT_HEADERS
        }

    def _params(self, request: BraveSearchRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": request.query.strip(),
            "count": max(1, min(config.BRAVE_MAX_RESULTS_PER_QUERY, int(request.count))),
            "offset": max(0, min(config.BRAVE_MAX_PAGES_PER_QUERY - 1, int(request.offset))),
            "country": request.country,
            "search_lang": request.search_language,
            "ui_lang": request.ui_language,
            "safesearch": request.safesearch,
            "extra_snippets": str(bool(request.extra_snippets)).lower(),
        }
        if request.freshness:
            params["freshness"] = request.freshness
        return params

    def search(self, request: BraveSearchRequest) -> BraveSearchResponse:
        if not self.is_configured():
            return BraveSearchResponse(
                query=request.query,
                response_status="NOT_CONFIGURED",
                error_category="CONFIGURATION_REQUIRED",
                safe_error_message="Brave Search is not configured.",
            )
        if not request.query.strip():
            return BraveSearchResponse(
                query=request.query,
                response_status="FAILED",
                error_category="INVALID_REQUEST",
                safe_error_message="A nonempty search query is required.",
            )
        started = time.perf_counter()
        response: requests.Response | None = None
        last_category = "REQUEST_FAILED"
        last_message = "Brave Search request failed."
        for attempt in range(config.BRAVE_REQUEST_MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    self.endpoint,
                    params=self._params(request),
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "X-Subscription-Token": self._api_key,
                    },
                    timeout=config.BRAVE_REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code == 200:
                    break
                last_category = "RATE_LIMITED" if response.status_code == 429 else "PROVIDER_ERROR"
                last_message = f"Brave Search returned HTTP {response.status_code}."
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
            except requests.Timeout:
                last_category = "TIMEOUT"
                last_message = "Brave Search timed out."
            except requests.RequestException:
                last_category = "REQUEST_FAILED"
                last_message = "Brave Search could not be reached."
            if attempt < config.BRAVE_REQUEST_MAX_RETRIES and config.BRAVE_REQUEST_BACKOFF_SECONDS:
                time.sleep(config.BRAVE_REQUEST_BACKOFF_SECONDS * (2**attempt))
        duration_ms = round((time.perf_counter() - started) * 1000)
        if response is None or response.status_code != 200:
            return BraveSearchResponse(
                query=request.query,
                request_duration_ms=duration_ms,
                response_status="FAILED",
                safe_rate_limit_metadata=self.safe_rate_limit_metadata(getattr(response, "headers", {})),
                provider_request_id=str(getattr(response, "headers", {}).get("x-request-id", "")) if response is not None else "",
                error_category=last_category,
                safe_error_message=last_message,
            )
        try:
            parsed = self.parse_response(response.json(), request=request, response=response, duration_ms=duration_ms)
        except (TypeError, ValueError, requests.JSONDecodeError):
            return BraveSearchResponse(
                query=request.query,
                request_duration_ms=duration_ms,
                response_status="FAILED",
                safe_rate_limit_metadata=self.safe_rate_limit_metadata(response.headers),
                error_category="INVALID_RESPONSE",
                safe_error_message="Brave Search returned an invalid response.",
            )
        self.successful_request_count += 1
        self.total_result_count += parsed.result_count
        return parsed

    def parse_response(
        self,
        payload: dict[str, Any],
        *,
        request: BraveSearchRequest,
        response: requests.Response,
        duration_ms: int,
    ) -> BraveSearchResponse:
        web = payload.get("web") or {}
        rows = web.get("results") or []
        results = tuple(
            BraveSearchResult(
                rank=index,
                title=str(row.get("title") or "Untitled result"),
                url=str(row.get("url") or ""),
                description=str(row.get("description") or ""),
                extra_snippets=tuple(str(item) for item in (row.get("extra_snippets") or [])),
                page_age=str(row.get("page_age") or row.get("age") or ""),
                language=str(row.get("language") or ""),
                content_type_hint=str(row.get("type") or row.get("subtype") or ""),
                profile=str(row.get("profile") or ""),
            )
            for index, row in enumerate(rows, start=1)
            if row.get("url")
        )
        more = bool((payload.get("query") or {}).get("more_results_available", False))
        return BraveSearchResponse(
            query=request.query,
            results=results,
            more_results_available=more,
            request_duration_ms=duration_ms,
            result_count=len(results),
            response_status="SUCCESS",
            safe_rate_limit_metadata=self.safe_rate_limit_metadata(response.headers),
            provider_request_id=str(response.headers.get("x-request-id", "")),
        )

    def test_connection(self) -> BraveSearchResponse:
        return self.search(BraveSearchRequest(query="public web search connection test", count=1, query_purpose="CONNECTION_TEST"))

    def usage_record(self) -> dict[str, int | float | None]:
        estimated = None
        if config.BRAVE_COST_PER_1000_REQUESTS is not None:
            estimated = self.successful_request_count * config.BRAVE_COST_PER_1000_REQUESTS / 1000
        return {
            "request_count": self.successful_request_count,
            "result_count": self.total_result_count,
            "estimated_cost": estimated,
        }
