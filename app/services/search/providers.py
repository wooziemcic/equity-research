from __future__ import annotations

from dataclasses import replace
from typing import Protocol, runtime_checkable

from app import config
from app.services.search.brave_search_service import BraveSearchClient, BraveSearchRequest, BraveSearchResponse


@runtime_checkable
class SearchProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def is_configured(self) -> bool: ...
    def test_connection(self) -> BraveSearchResponse: ...
    def search(self, request: BraveSearchRequest) -> BraveSearchResponse: ...
    def supports_date_range(self) -> bool: ...
    def supports_site_filter(self) -> bool: ...
    def supports_filetype_filter(self) -> bool: ...
    def usage_summary(self) -> dict: ...


class BraveSearchProvider:
    provider_name = "brave"

    def __init__(self, client: BraveSearchClient | None = None) -> None:
        self.client = client or BraveSearchClient(config.brave_search_api_key())

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def test_connection(self) -> BraveSearchResponse:
        return self.client.test_connection()

    def search(self, request: BraveSearchRequest) -> BraveSearchResponse:
        return self.client.search(request)

    def supports_date_range(self) -> bool:
        return True

    def supports_site_filter(self) -> bool:
        return True

    def supports_filetype_filter(self) -> bool:
        return True

    def usage_summary(self) -> dict:
        return self.client.usage_record()


class DisabledSearchProvider:
    provider_name = "disabled"

    def is_configured(self) -> bool:
        return False

    def test_connection(self) -> BraveSearchResponse:
        return BraveSearchResponse(query="", response_status="DISABLED", safe_error_message="Web search is disabled.")

    def search(self, request: BraveSearchRequest) -> BraveSearchResponse:
        return BraveSearchResponse(query=request.query, response_status="DISABLED", safe_error_message="Web search is disabled.")

    def supports_date_range(self) -> bool:
        return False

    def supports_site_filter(self) -> bool:
        return False

    def supports_filetype_filter(self) -> bool:
        return False

    def usage_summary(self) -> dict:
        return {"request_count": 0, "result_count": 0, "estimated_cost": None}


class MockSearchProvider:
    provider_name = "mock"

    def __init__(self, responses: list[BraveSearchResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[BraveSearchRequest] = []

    def is_configured(self) -> bool:
        return True

    def test_connection(self) -> BraveSearchResponse:
        return BraveSearchResponse(query="mock connection test", response_status="SUCCESS")

    def search(self, request: BraveSearchRequest) -> BraveSearchResponse:
        self.requests.append(request)
        if self.responses:
            return replace(self.responses.pop(0), query=request.query)
        return BraveSearchResponse(query=request.query, response_status="SUCCESS")

    def supports_date_range(self) -> bool:
        return True

    def supports_site_filter(self) -> bool:
        return True

    def supports_filetype_filter(self) -> bool:
        return True

    def usage_summary(self) -> dict:
        return {"request_count": len(self.requests), "result_count": 0, "estimated_cost": None}


def get_search_provider() -> SearchProvider:
    if config.SEARCH_PROVIDER != "brave":
        return DisabledSearchProvider()
    return BraveSearchProvider()
