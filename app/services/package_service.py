from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
import json

from app import config
from app.services.collection_profile import default_profile_for_security_type
from app.services.research_window import normalize_window
from app.utils import database
from app.utils.validation import (
    ValidationResult,
    sanitize_analyst_notes,
    validate_cutoff_date,
    validate_ticker,
)


@dataclass(frozen=True)
class PackageInput:
    """Validated package creation inputs."""

    ticker: str
    security_type: str
    research_cutoff_date: date
    filing_history_years: int
    analyst_notes: str = ""
    selected_years: tuple[int, ...] | None = None
    selected_months: tuple[int, ...] | None = None


def generate_package_id(ticker: str) -> str:
    """Create a human-readable package identifier."""
    return f"CRAI-{ticker}-{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}"


def validate_package_input(package_input: PackageInput) -> list[str]:
    """Return validation errors for a package request."""
    errors: list[str] = []
    ticker_result = validate_ticker(package_input.ticker)
    cutoff_result = validate_cutoff_date(package_input.research_cutoff_date)
    notes_result = sanitize_analyst_notes(package_input.analyst_notes)

    if not ticker_result.is_valid:
        errors.append(ticker_result.error)
    if package_input.security_type not in config.SUPPORTED_SECURITY_TYPES:
        errors.append("Select a supported security type.")
    if package_input.selected_years is None and package_input.filing_history_years not in config.FILING_HISTORY_OPTIONS.values():
        errors.append("Select a supported filing history period.")
    if not cutoff_result.is_valid:
        errors.append(cutoff_result.error)
    if not notes_result.is_valid:
        errors.append(notes_result.error)
    if cutoff_result.is_valid and package_input.selected_years is not None:
        try:
            normalize_window(
                selected_years=package_input.selected_years,
                selected_months=package_input.selected_months,
                cutoff=cutoff_result.value,
            )
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def create_package(
    package_input: PackageInput,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Validate, create, and return a package record."""
    errors = validate_package_input(package_input)
    if errors:
        raise ValueError(" ".join(errors))

    ticker = validate_ticker(package_input.ticker).value
    notes = sanitize_analyst_notes(package_input.analyst_notes).value
    cutoff = validate_cutoff_date(package_input.research_cutoff_date).value
    cutoff_date = date.fromisoformat(cutoff)

    database.initialize_database(db_path)
    package_id = generate_package_id(ticker)
    while database.get_package_by_package_id(package_id, db_path=db_path) is not None:
        package_id = generate_package_id(ticker)

    profile = default_profile_for_security_type(package_input.security_type)
    selected_years = package_input.selected_years or tuple(
        range(cutoff_date.year - package_input.filing_history_years + 1, cutoff_date.year + 1)
    )
    window = normalize_window(
        selected_years=selected_years,
        selected_months=package_input.selected_months,
        cutoff=cutoff,
    )

    return database.create_package_record(
        package_id=package_id,
        ticker=ticker,
        company_name="Company resolution pending",
        security_type=package_input.security_type,
        status=config.STATUS_DRAFT,
        research_cutoff_date=cutoff,
        filing_history_years=len(window.years),
        analyst_notes=notes,
        collection_profile_name=profile.name if profile else None,
        collection_profile_snapshot_json=json.dumps(profile.snapshot(), sort_keys=True) if profile else None,
        selected_years_json=json.dumps(list(window.years)),
        selected_months_json=json.dumps(list(window.months)),
        research_window_fingerprint=window.fingerprint(),
        db_path=db_path,
    )


def get_dashboard_metrics(
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, int]:
    """Return dashboard counters derived from the database."""
    database.initialize_database(db_path)
    status_counts = database.count_packages_by_status(db_path=db_path)
    draft_count = status_counts.get(config.STATUS_DRAFT, 0) + status_counts.get(
        config.STATUS_SETUP, 0
    )
    return {
        "total": database.count_all_packages(db_path=db_path),
        "draft": draft_count,
        "completed": status_counts.get(config.STATUS_COMPLETE, 0),
        "awaiting_review": status_counts.get(config.STATUS_AWAITING_REVIEW, 0),
    }


def list_recent_packages(
    *,
    limit: int = 10,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Return recent package records for dashboard display."""
    database.initialize_database(db_path)
    return database.list_packages(limit=limit, db_path=db_path)


def find_existing_ticker_packages(
    ticker_result: ValidationResult,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Return existing packages for a valid ticker validation result."""
    if not ticker_result.is_valid:
        return []
    database.initialize_database(db_path)
    return database.list_packages_by_ticker(ticker_result.value, db_path=db_path)
