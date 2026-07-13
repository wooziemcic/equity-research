from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from app import config
from app.services.company_resolver import sec_headers
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


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def allowed_start_date(package: dict[str, Any]) -> date:
    cutoff = _parse_date(package["research_cutoff_date"])
    return cutoff.replace(year=cutoff.year - int(package["filing_history_years"]))


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
    cutoff = _parse_date(package["research_cutoff_date"])
    start = allowed_start_date(package)
    candidates: list[FilingCandidate] = []
    for index, form in enumerate(recent.get("form", [])):
        filing_date = recent.get("filingDate", [""])[index]
        primary = recent.get("primaryDocument", [""])[index]
        accession = recent.get("accessionNumber", [""])[index]
        if not filing_date or not primary or not accession or form not in form_types:
            continue
        parsed = _parse_date(filing_date)
        if parsed > cutoff or parsed < start:
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
            )
        )
    write_metadata_json(
        package["package_id"],
        "sec_filing_inventory.json",
        {"filings": [candidate.__dict__ for candidate in candidates]},
    )
    return candidates


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
    for filing in filings:
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
            database.create_document_record(
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
