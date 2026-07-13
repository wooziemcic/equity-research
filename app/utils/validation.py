from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


MAX_TICKER_LENGTH = 12
MAX_ANALYST_NOTES_LENGTH = 2000
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,11}$")


@dataclass(frozen=True)
class ValidationResult:
    """Structured result for user-input validation."""

    is_valid: bool
    value: str = ""
    error: str = ""


def validate_ticker(raw_ticker: str | None) -> ValidationResult:
    """Normalize and validate a public-company ticker-like symbol."""
    ticker = (raw_ticker or "").strip().upper()
    if not ticker:
        return ValidationResult(False, error="Enter a ticker before creating a package.")
    if len(ticker) > MAX_TICKER_LENGTH:
        return ValidationResult(
            False,
            error=f"Ticker must be {MAX_TICKER_LENGTH} characters or fewer.",
        )
    if ticker.startswith(("HTTP://", "HTTPS://", "WWW.")) or "/" in ticker:
        return ValidationResult(False, error="Enter a ticker symbol, not a URL.")
    if any(character.isspace() for character in ticker):
        return ValidationResult(False, error="Ticker cannot contain embedded spaces.")
    if not TICKER_PATTERN.fullmatch(ticker):
        return ValidationResult(
            False,
            error="Ticker can only use letters, numbers, dots, and hyphens.",
        )
    if ticker.endswith((".", "-")):
        return ValidationResult(False, error="Ticker cannot end with a dot or hyphen.")
    return ValidationResult(True, value=ticker)


def validate_cutoff_date(cutoff_date: date) -> ValidationResult:
    """Validate the research cutoff date for Phase 1 package setup."""
    if cutoff_date > date.today():
        return ValidationResult(
            False,
            error="Research cutoff date cannot be in the future during Phase 1.",
        )
    return ValidationResult(True, value=cutoff_date.isoformat())


def sanitize_analyst_notes(notes: str | None) -> ValidationResult:
    """Trim analyst notes while preserving user-entered text content."""
    cleaned = (notes or "").strip()
    if len(cleaned) > MAX_ANALYST_NOTES_LENGTH:
        return ValidationResult(
            False,
            error=f"Analyst notes must be {MAX_ANALYST_NOTES_LENGTH} characters or fewer.",
        )
    return ValidationResult(True, value=cleaned)
