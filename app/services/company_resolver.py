from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from app import config
from app.services.http_client import request_with_retries
from app.services.workspace_service import write_metadata_json
from app.utils import database
from app.utils.validation import validate_ticker


@dataclass(frozen=True)
class ResolutionResult:
    status: str
    metadata: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None
    error: str = ""


def _cache_path() -> Path:
    return config.CACHE_DIR / "sec_company_tickers_exchange.json"


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    expires_at = datetime.fromtimestamp(path.stat().st_mtime, UTC) + timedelta(
        hours=config.SEC_CACHE_HOURS
    )
    return datetime.now(UTC) < expires_at


def sec_headers() -> dict[str, str]:
    return {
        "User-Agent": config.SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json,text/plain,*/*",
    }


def load_ticker_mapping(
    *,
    refresh: bool = False,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Load and cache the official SEC ticker mapping."""
    config.ensure_directories()
    path = _cache_path()
    if not refresh and _cache_is_fresh(path):
        return json.loads(path.read_text(encoding="utf-8"))
    if not config.sec_user_agent_is_configured():
        raise ValueError("Configure SEC_USER_AGENT before requesting SEC data.")
    response = request_with_retries(
        config.SEC_TICKER_MAPPING_URL,
        headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
        session=session,
    )
    if response.status_code != 200:
        raise ValueError("SEC ticker mapping could not be loaded.")
    payload = response.json()
    rows = payload.get("data", [])
    fields = payload.get("fields", [])
    mapping = [dict(zip(fields, row, strict=False)) for row in rows]
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return mapping


def normalize_cik(cik: str | int) -> str:
    """Normalize a CIK to the SEC's zero-padded 10 digit format."""
    return str(cik).strip().zfill(10)


def _submissions_metadata(cik: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    response = request_with_retries(
        config.SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik),
        headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
        session=session,
    )
    if response.status_code != 200:
        return {}
    return response.json()


def resolve_package_company(
    package: dict[str, Any],
    *,
    refresh: bool = False,
    selected_cik: str | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> ResolutionResult:
    """Resolve a package ticker to an SEC company identity."""
    ticker_result = validate_ticker(package.get("ticker"))
    if not ticker_result.is_valid:
        return ResolutionResult("UNRESOLVED", error=ticker_result.error)
    try:
        mapping = load_ticker_mapping(refresh=refresh, session=session)
    except ValueError as exc:
        return ResolutionResult("CONFIGURATION_REQUIRED", error=str(exc))
    matches = [
        row
        for row in mapping
        if str(row.get("ticker", "")).upper() == ticker_result.value
    ]
    if not matches:
        return ResolutionResult("UNRESOLVED", error="No exact SEC ticker match was found.")
    unique = {str(row.get("cik", row.get("cik_str", ""))) for row in matches}
    if selected_cik:
        normalized_selected = str(int(selected_cik))
        matches = [
            row
            for row in matches
            if str(row.get("cik", row.get("cik_str", ""))) == normalized_selected
        ]
        if not matches:
            return ResolutionResult("UNRESOLVED", error="Selected SEC record was not found.")
    elif len(unique) > 1:
        return ResolutionResult("MULTIPLE_MATCHES", candidates=matches)

    row = matches[0]
    cik = normalize_cik(row.get("cik", row.get("cik_str", "")))
    submissions = _submissions_metadata(cik, session=session)
    now = database.utc_now_iso()
    metadata = {
        "ticker": ticker_result.value,
        "company_name": submissions.get("name") or row.get("name") or row.get("title"),
        "cik": cik,
        "exchange": row.get("exchange"),
        "sic": str(submissions.get("sic", "") or ""),
        "industry_description": submissions.get("sicDescription"),
        "fiscal_year_end": submissions.get("fiscalYearEnd"),
        "sec_company_url": config.SEC_COMPANY_PAGE_TEMPLATE.format(cik=cik),
        "resolution_status": "RESOLVED",
        "resolution_source": config.SEC_TICKER_MAPPING_URL,
        "resolution_timestamp": now,
    }
    updated = database.update_package_company_metadata(
        package["package_id"],
        metadata,
        db_path=db_path,
    )
    if updated:
        write_metadata_json(package["package_id"], "company_resolution.json", metadata)
    return ResolutionResult("RESOLVED", metadata=updated or metadata)
