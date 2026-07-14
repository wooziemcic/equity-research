from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app import config
from app.services.company_resolver import sec_headers
from app.services.collection_profile import (
    conditional_rule,
    is_profile_eligible,
    normalize_sec_form,
)
from app.services.research_window import selected_date_bounds, window_from_package
from app.services.http_client import HttpClientError, request_with_retries, response_bytes_with_limit
from app.services.workspace_service import atomic_write_bytes, safe_document_path, sanitize_filename, write_metadata_json
from app.utils import database


@dataclass(frozen=True)
class FilingCandidate:
    accession_number: str
    form_type: str
    filing_date: str
    report_period: str
    primary_document: str
    primary_document_url: str
    filing_index_url: str
    title: str
    normalized_form_family: str | None = None
    inventory_status: str = "ELIGIBLE"
    selected: bool = True
    reporting_person: str = ""
    security: str = ""
    shares: float | None = None
    aggregate_market_value: float | None = None
    issuer: str = ""
    filing_items: str = ""
    selection_reason: str = "Included by profile"


@dataclass(frozen=True)
class SecExhibitCandidate:
    parent_accession_number: str
    filename: str
    description: str
    source_url: str
    filing_date: str
    title: str


def accession_directory(accession_number: str) -> str:
    """Return accession number without dashes for SEC archive paths."""
    return accession_number.replace("-", "")


def build_sec_document_url(cik: str, accession_number: str, primary_document: str) -> str:
    cik_int = str(int(cik))
    accession = accession_directory(accession_number)
    return (
        f"{config.SEC_ARCHIVES_BASE_URL}/{cik_int}/{accession}/"
        f"{quote(primary_document)}"
    )


def build_sec_index_url(cik: str, accession_number: str) -> str:
    cik_int = str(int(cik))
    accession = accession_directory(accession_number)
    return f"{config.SEC_ARCHIVES_BASE_URL}/{cik_int}/{accession}/{accession_number}-index.html"


def standardized_sec_filename(ticker: str, filing: FilingCandidate) -> str:
    extension = Path(filing.primary_document).suffix or ".html"
    form = re.sub(r"[^A-Za-z0-9-]+", "-", filing.form_type)
    return sanitize_filename(
        f"{ticker}_{form}_{filing.filing_date}_{filing.accession_number}{extension}"
    )


def form_144_preselected(filing: FilingCandidate) -> bool:
    """Apply only explicitly configured Form 144 criteria; blank criteria select nothing."""
    if not config.FORM_144_AUTO_SELECT_ENABLED:
        return False
    checks: list[bool] = []
    if config.FORM_144_MIN_SHARES is not None:
        checks.append(filing.shares is not None and filing.shares >= config.FORM_144_MIN_SHARES)
    if config.FORM_144_MIN_MARKET_VALUE is not None:
        checks.append(
            filing.aggregate_market_value is not None
            and filing.aggregate_market_value >= config.FORM_144_MIN_MARKET_VALUE
        )
    return bool(checks) and all(checks)


def _eight_k_items_from_index(
    filing_index_url: str,
    *,
    session: requests.Session | None,
) -> str:
    try:
        response = request_with_retries(
            filing_index_url,
            headers=sec_headers(),
            delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
            session=session,
        )
        if response.status_code != 200:
            return ""
        found = sorted(set(re.findall(r"Item\s+(\d+\.\d+)", response.text, flags=re.I)))
        return ",".join(found)
    except Exception:
        return ""


def _eight_k_selection(items: str) -> tuple[str, bool, str]:
    mode = config.SEC_8K_COLLECTION_MODE
    normalized_items = {item.strip().upper().removeprefix("ITEM ") for item in items.split(",") if item.strip()}
    if mode == "ALL_8K":
        return "ELIGIBLE", True, "Selected because SEC_8K_COLLECTION_MODE=ALL_8K."
    if mode == "ANALYST_SELECTION":
        return "AWAITING_8K_SELECTION", False, "Awaiting analyst selection under ANALYST_SELECTION mode."
    approved = set(config.SEC_8K_APPROVED_ITEMS)
    if not approved:
        return "EXCLUDED_8K_MODE", False, "No investment-team-approved 8-K item list is configured."
    matched = sorted(normalized_items & approved)
    if matched:
        return "ELIGIBLE", True, f"Matched approved 8-K item(s): {', '.join(matched)}."
    return "EXCLUDED_8K_MODE", False, "Filing items did not match the configured approved-item list."


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def allowed_start_date(package: dict[str, Any]) -> date:
    return selected_date_bounds(window_from_package(package))[0]


def preview_filings(
    package: dict[str, Any],
    form_types: list[str],
    *,
    session: requests.Session | None = None,
) -> list[FilingCandidate]:
    """Return SEC filing candidates within package date and form constraints."""
    cik = package.get("cik")
    if not cik:
        return []
    response = request_with_retries(
        config.SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik),
        headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
        session=session,
    )
    if response.status_code != 200:
        return []
    recent = response.json().get("filings", {}).get("recent", {})
    window = window_from_package(package)
    candidates: list[FilingCandidate] = []
    for index, form in enumerate(recent.get("form", [])):
        filing_date = recent.get("filingDate", [""])[index]
        primary = recent.get("primaryDocument", [""])[index]
        accession = recent.get("accessionNumber", [""])[index]
        if not filing_date or not primary or not accession or form not in form_types:
            continue
        parsed = _parse_date(filing_date)
        if not window.contains(parsed):
            continue
        report_periods = recent.get("reportDate", [""])
        report_period = report_periods[index] if index < len(report_periods) else ""
        candidates.append(
            FilingCandidate(
                accession_number=accession,
                form_type=form,
                filing_date=filing_date,
                report_period=report_period,
                primary_document=primary,
                primary_document_url=build_sec_document_url(cik, accession, primary),
                filing_index_url=build_sec_index_url(cik, accession),
                title=f"{package['ticker']} {form} filed {filing_date}",
                normalized_form_family=normalize_sec_form(form),
            )
        )
    write_metadata_json(
        package["package_id"],
        "sec_filing_inventory.json",
        {"filings": [candidate.__dict__ for candidate in candidates]},
    )
    return candidates


def preview_cutler_profile(
    package: dict[str, Any],
    *,
    enabled_families: set[str] | None = None,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[FilingCandidate]:
    """Retrieve one SEC inventory, normalize it, and mark profile eligibility."""
    cik = package.get("cik")
    if not cik:
        return []
    response = request_with_retries(
        config.SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik),
        headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
        session=session,
    )
    if response.status_code != 200:
        return []
    recent = response.json().get("filings", {}).get("recent", {})
    window = window_from_package(package)
    enabled = enabled_families or set(config.SEC_SUPPORTED_FORMS)
    inventory: list[FilingCandidate] = []
    seen: set[tuple[str, str]] = set()
    forms = recent.get("form", [])
    for index, form in enumerate(forms):
        filing_date = _at(recent, "filingDate", index)
        primary = _at(recent, "primaryDocument", index)
        accession = _at(recent, "accessionNumber", index)
        if not filing_date or not primary or not accession:
            continue
        try:
            parsed = _parse_date(filing_date)
        except ValueError:
            continue
        outside_window = not window.contains(parsed)
        identity = (accession, primary.lower())
        if identity in seen:
            continue
        seen.add(identity)
        family = normalize_sec_form(form)
        eligible = is_profile_eligible(form, include_form_144=True) and family in enabled
        existing = database.get_document_by_accession(package["package_id"], accession, db_path=db_path)
        status = "ALREADY_COLLECTED" if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED else "ELIGIBLE"
        filing_items = _at(recent, "items", index)
        selection_reason = "Included by collection profile."
        selected = eligible and family != "144" and status != "ALREADY_COLLECTED"
        if outside_window:
            status = config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW
            selected = False
            selection_reason = "Filing date is outside the selected research time window."
        if family == "8-K" and eligible and status not in {"ALREADY_COLLECTED", config.DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW}:
            if config.SEC_8K_COLLECTION_MODE == "MATERIAL_8K_ONLY" and not filing_items:
                filing_items = _eight_k_items_from_index(build_sec_index_url(cik, accession), session=session)
            status, selected, selection_reason = _eight_k_selection(filing_items)
        if family == "144" and not outside_window:
            status = "AWAITING_SELECTION" if status != "ALREADY_COLLECTED" else status
            selection_reason = "Form 144 requires analyst selection."
        elif not eligible and not outside_window:
            status = "EXCLUDED_BY_PROFILE"
            selected = False
            selection_reason = "Form is outside the active collection profile."
        candidate = FilingCandidate(
            accession_number=accession,
            form_type=form,
            filing_date=filing_date,
            report_period=_at(recent, "reportDate", index),
            primary_document=primary,
            primary_document_url=build_sec_document_url(cik, accession, primary),
            filing_index_url=build_sec_index_url(cik, accession),
            title=f"{package['ticker']} {form} filed {filing_date}",
            normalized_form_family=family,
            inventory_status=status,
            selected=selected,
            issuer=package.get("company_name") or package.get("ticker") or "",
            filing_items=filing_items,
            selection_reason=selection_reason,
        )
        if family == "144" and not outside_window:
            candidate = FilingCandidate(**{**candidate.__dict__, "selected": form_144_preselected(candidate)})
        inventory.append(candidate)
    database.replace_sec_filing_inventory(
        package["package_id"],
        [
            {
                "accession_number": item.accession_number,
                "primary_document": item.primary_document,
                "original_form_type": item.form_type,
                "normalized_form_family": item.normalized_form_family,
                "filing_date": item.filing_date,
                "source_url": item.primary_document_url,
                "inventory_status": item.inventory_status,
                "conditional_rule": conditional_rule(item.normalized_form_family),
                "selected": item.selected,
                "filing_items": item.filing_items,
                "selection_reason": item.selection_reason,
                "metadata_json": json.dumps(
                    {
                        "reporting_person": item.reporting_person,
                        "security": item.security,
                        "shares": item.shares,
                        "aggregate_market_value": item.aggregate_market_value,
                        "issuer": item.issuer,
                    },
                    sort_keys=True,
                ),
            }
            for item in inventory
        ],
        db_path=db_path,
    )
    write_metadata_json(package["package_id"], "sec_filing_inventory.json", {"filings": [item.__dict__ for item in inventory]})
    return inventory


def _at(mapping: dict[str, Any], key: str, index: int) -> str:
    values = mapping.get(key, [])
    return str(values[index]) if index < len(values) and values[index] is not None else ""


def _valid_sec_content(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith((b"<html", b"<!doctype html", b"<sec-document", b"<?xml")) or b"<html" in head


def _document_record(
    package: dict[str, Any],
    filing: FilingCandidate,
    *,
    status: str,
    document_id: str,
    local_path: Path | None = None,
    content: bytes | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    sha = hashlib.sha256(content).hexdigest() if content else None
    filename = local_path.name if local_path else standardized_sec_filename(package["ticker"], filing)
    return {
        "document_id": document_id,
        "package_id": package["package_id"],
        "ticker": package["ticker"],
        "category": "SEC Filing",
        "document_type": filing.form_type,
        "title": filing.title,
        "source_name": "SEC EDGAR",
        "source_url": filing.primary_document_url,
        "source_domain": "sec.gov",
        "accession_number": filing.accession_number,
        "form_type": filing.form_type,
        "normalized_form_family": filing.normalized_form_family or normalize_sec_form(filing.form_type),
        "publication_date": filing.filing_date,
        "report_period": filing.report_period,
        "local_filename": filename,
        "local_path": str(local_path) if local_path else None,
        "mime_type": mimetypes.guess_type(filename)[0] or "text/html",
        "file_size_bytes": len(content) if content else None,
        "sha256_hash": sha,
        "collection_method": "SEC",
        "collection_status": status,
        "is_public": True,
        "error_message": error_message,
    }


def download_selected_filings(
    package: dict[str, Any],
    filings: list[FilingCandidate],
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Download selected SEC filings and record every result."""
    run_id = f"RUN-SEC-{secrets.token_hex(8).upper()}"
    database.create_collection_run(
        run_id=run_id,
        package_id=package["package_id"],
        source_type="SEC",
        status=config.COLLECTION_STATUS_RUNNING,
        db_path=db_path,
    )
    summary = {
        "discovered": len(filings),
        "downloaded": 0,
        "downloaded_now": 0,
        "already_collected": 0,
        "duplicate": 0,
        "skipped": 0,
        "failed": 0,
        "not_found": 0,
    }
    window = window_from_package(package)
    for filing in filings:
        if not window.contains(filing.filing_date):
            summary["skipped"] += 1
            continue
        existing = database.get_document_by_accession(
            package["package_id"],
            filing.accession_number,
            db_path=db_path,
        ) or database.get_document_by_url(package["package_id"], filing.primary_document_url, db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["already_collected"] += 1
            summary["skipped"] += 1
            continue
        try:
            response = request_with_retries(
                filing.primary_document_url,
                headers=sec_headers(),
                delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
                session=session,
            )
            if response.status_code != 200:
                raise HttpClientError(f"SEC returned HTTP {response.status_code}.")
            content = response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES)
            if not _valid_sec_content(content):
                raise HttpClientError("SEC response did not look like an HTML, XML, or text filing.")
            sha = hashlib.sha256(content).hexdigest()
            existing_hash = database.get_document_by_hash(package["package_id"], sha, db_path=db_path)
            if existing_hash and existing_hash.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
                summary["duplicate"] += 1
                summary["skipped"] += 1
                continue
            path = safe_document_path(package["package_id"], "sec", standardized_sec_filename(package["ticker"], filing))
            atomic_write_bytes(path, content)
            created = database.create_document_record(
                _document_record(
                    package,
                    filing,
                    status=config.DOCUMENT_STATUS_DOWNLOADED,
                    document_id=database.generate_document_id("DOC-SEC"),
                    local_path=path,
                    content=content,
                ),
                db_path=db_path,
            )
            database.update_document_metadata(
                created["document_id"],
                {
                    "normalized_form_family": filing.normalized_form_family or normalize_sec_form(filing.form_type),
                    "selected_window_status": "ELIGIBLE",
                },
                db_path=db_path,
            )
            summary["downloaded"] += 1
            summary["downloaded_now"] += 1
        except Exception as exc:
            database.create_document_record(
                _document_record(
                    package,
                    filing,
                    status=config.DOCUMENT_STATUS_FAILED,
                    document_id=database.generate_document_id("DOC-SEC"),
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
    package_status = (
        config.STATUS_PUBLIC_COLLECTION_PARTIAL
        if summary["failed"] or summary["skipped"]
        else config.STATUS_PUBLIC_COLLECTION
    )
    database.update_package_collection_state(package["package_id"], package_status, db_path=db_path)
    return summary


def download_profile_inventory(
    package: dict[str, Any],
    inventory: list[FilingCandidate],
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Download only profile-eligible selected rows and retain excluded rows as inventory."""
    selected = [
        item
        for item in inventory
        if item.selected and item.inventory_status in {"ELIGIBLE", "AWAITING_SELECTION", "AWAITING_8K_SELECTION"}
    ]
    result = download_selected_filings(package, selected, session=session, db_path=db_path)
    result["discovered"] = len(inventory)
    result["eligible"] = sum(item.inventory_status in {"ELIGIBLE", "ALREADY_COLLECTED"} for item in inventory)
    result["excluded_by_profile"] = sum(item.inventory_status == "EXCLUDED_BY_PROFILE" for item in inventory)
    result["awaiting_form_144_selection"] = sum(
        item.inventory_status == "AWAITING_SELECTION" and not item.selected for item in inventory
    )
    runs = database.list_recent_collection_runs(package["package_id"], limit=1, db_path=db_path)
    if runs:
        database.update_collection_run(
            runs[0]["run_id"],
            status=runs[0]["status"],
            documents_discovered=result["discovered"],
            documents_eligible=result["eligible"],
            documents_downloaded=result["downloaded_now"],
            documents_skipped=result["skipped"],
            documents_already_collected=result["already_collected"],
            documents_duplicated=result["duplicate"],
            documents_not_found=result["not_found"],
            documents_failed=result["failed"],
            documents_excluded_profile=result["excluded_by_profile"],
            documents_awaiting_selection=result["awaiting_form_144_selection"],
            db_path=db_path,
        )
    return result


DIVIDEND_TERMS = (
    "dividend declaration",
    "quarterly dividend",
    "cash dividend",
    "dividend increase",
    "dividend reduction",
    "dividend suspension",
    "distribution",
    "dividend",
)


def discover_dividend_exhibits(
    package: dict[str, Any],
    filing: FilingCandidate,
    *,
    session: requests.Session | None = None,
) -> list[SecExhibitCandidate]:
    """Inspect an 8-K archive index and return only explicitly dividend-related exhibits."""
    if (filing.normalized_form_family or normalize_sec_form(filing.form_type)) != "8-K":
        return []
    cik = str(int(package["cik"]))
    accession = accession_directory(filing.accession_number)
    index_url = f"{config.SEC_ARCHIVES_BASE_URL}/{cik}/{accession}/index.json"
    response = request_with_retries(
        index_url,
        headers=sec_headers(),
        delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
        session=session,
    )
    if response.status_code != 200:
        return []
    items = response.json().get("directory", {}).get("item", [])
    exhibits: list[SecExhibitCandidate] = []
    for item in items:
        name = str(item.get("name") or "")
        description = str(item.get("description") or item.get("title") or "")
        haystack = f"{name} {description}".lower()
        if not name or name == filing.primary_document or not any(term in haystack for term in DIVIDEND_TERMS):
            continue
        exhibits.append(
            SecExhibitCandidate(
                parent_accession_number=filing.accession_number,
                filename=name,
                description=description,
                source_url=f"{config.SEC_ARCHIVES_BASE_URL}/{cik}/{accession}/{quote(name)}",
                filing_date=filing.filing_date,
                title=f"{package['ticker']} Dividend Announcement {filing.filing_date}",
            )
        )
    if not exhibits and items:
        index_page = request_with_retries(
            filing.filing_index_url,
            headers=sec_headers(),
            delay_seconds=config.SEC_REQUEST_DELAY_SECONDS,
            session=session,
        )
        if index_page.status_code == 200:
            index_text = index_page.content.decode("utf-8", errors="replace").lower()
            for item in items:
                name = str(item.get("name") or "")
                if not name or name == filing.primary_document:
                    continue
                position = index_text.find(name.lower())
                context = index_text[max(0, position - 400):position + len(name) + 400] if position >= 0 else ""
                if not any(term in context for term in DIVIDEND_TERMS):
                    continue
                exhibits.append(
                    SecExhibitCandidate(
                        parent_accession_number=filing.accession_number,
                        filename=name,
                        description="Dividend-related SEC exhibit",
                        source_url=f"{config.SEC_ARCHIVES_BASE_URL}/{cik}/{accession}/{quote(name)}",
                        filing_date=filing.filing_date,
                        title=f"{package['ticker']} Dividend Announcement {filing.filing_date}",
                    )
                )
    return exhibits


def download_dividend_exhibits(
    package: dict[str, Any],
    exhibits: list[SecExhibitCandidate],
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    summary = {"downloaded_now": 0, "already_collected": 0, "duplicate": 0, "failed": 0, "not_found": 0}
    for exhibit in exhibits:
        existing = database.get_document_by_url(package["package_id"], exhibit.source_url, db_path=db_path)
        if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
            summary["already_collected"] += 1
            continue
        try:
            response = request_with_retries(exhibit.source_url, headers=sec_headers(), delay_seconds=config.SEC_REQUEST_DELAY_SECONDS, session=session)
            if response.status_code != 200:
                raise HttpClientError(f"SEC returned HTTP {response.status_code}.")
            content = response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES)
            sha = hashlib.sha256(content).hexdigest()
            if database.get_document_by_hash(package["package_id"], sha, db_path=db_path):
                summary["duplicate"] += 1
                continue
            suffix = Path(exhibit.filename).suffix or ".html"
            filename = sanitize_filename(f"{package['ticker']}_Dividend_Announcement_{exhibit.filing_date}_{exhibit.parent_accession_number}{suffix}")
            path = safe_document_path(package["package_id"], "sec", filename)
            atomic_write_bytes(path, content)
            created = database.create_document_record(
                {
                    "document_id": database.generate_document_id("DOC-SEC-EXHIBIT"),
                    "package_id": package["package_id"],
                    "ticker": package["ticker"],
                    "category": "Dividend Announcement",
                    "document_type": "Dividend Announcement",
                    "title": exhibit.title,
                    "source_name": "SEC EDGAR",
                    "source_url": exhibit.source_url,
                    "source_domain": "sec.gov",
                    "accession_number": f"{exhibit.parent_accession_number}:{exhibit.filename}",
                    "form_type": "8-K EXHIBIT",
                    "publication_date": exhibit.filing_date,
                    "local_filename": path.name,
                    "local_path": str(path),
                    "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "file_size_bytes": len(content),
                    "sha256_hash": sha,
                    "collection_method": "SEC_EXHIBIT",
                    "collection_status": config.DOCUMENT_STATUS_DOWNLOADED,
                    "is_public": True,
                    "final_category_code": "dividend_announcement",
                },
                db_path=db_path,
            )
            database.update_document_metadata(
                created["document_id"],
                {"parent_accession_number": exhibit.parent_accession_number, "normalized_form_family": "DIVIDEND_ANNOUNCEMENT", "final_category_code": "dividend_announcement"},
                db_path=db_path,
            )
            summary["downloaded_now"] += 1
        except Exception as exc:
            summary["not_found" if "HTTP 404" in str(exc) else "failed"] += 1
    return summary


def store_official_y15(
    package: dict[str, Any],
    source_url: str,
    *,
    session: requests.Session | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Store a directly supplied Y-15 URL from an approved official public domain."""
    host = (urlparse(source_url).hostname or "").lower()
    if not any(host == domain or host.endswith(f".{domain}") for domain in ("federalreserve.gov", "ffiec.gov", "sec.gov")):
        raise ValueError("Y-15 links must use an approved official public source.")
    existing = database.get_document_by_url(package["package_id"], source_url, db_path=db_path)
    if existing and existing.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED:
        return existing
    response = request_with_retries(source_url, headers=sec_headers(), delay_seconds=config.SEC_REQUEST_DELAY_SECONDS, session=session)
    if response.status_code != 200:
        raise HttpClientError(f"Official source returned HTTP {response.status_code}.")
    content = response_bytes_with_limit(response, max_bytes=config.MAX_DOWNLOAD_BYTES)
    suffix = Path(urlparse(source_url).path).suffix or ".pdf"
    filename = sanitize_filename(f"{package['ticker']}_Official_{package['research_cutoff_date']}_Y-15{suffix}")
    path = safe_document_path(package["package_id"], "sec", filename)
    atomic_write_bytes(path, content)
    created = database.create_document_record(
        {
            "document_id": database.generate_document_id("DOC-Y15"), "package_id": package["package_id"], "ticker": package["ticker"],
            "category": "Y-15 Regulatory Report", "document_type": "Y-15 Regulatory Report", "title": f"{package['ticker']} Y-15 Regulatory Report",
            "source_name": host, "source_url": source_url, "source_domain": host, "local_filename": path.name, "local_path": str(path),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "file_size_bytes": len(content),
            "sha256_hash": hashlib.sha256(content).hexdigest(), "collection_method": "OFFICIAL_DIRECT_LINK",
            "collection_status": config.DOCUMENT_STATUS_DOWNLOADED, "is_public": True, "final_category_code": "y15_regulatory_report",
        }, db_path=db_path,
    )
    return database.update_document_metadata(
        created["document_id"],
        {"normalized_form_family": "Y-15", "final_category_code": "y15_regulatory_report"},
        db_path=db_path,
    ) or created
