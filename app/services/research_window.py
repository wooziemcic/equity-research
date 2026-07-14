from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable


ALL_MONTHS = tuple(range(1, 13))


@dataclass(frozen=True)
class ResearchWindow:
    years: tuple[int, ...]
    months: tuple[int, ...]
    cutoff: date

    @property
    def uses_all_months(self) -> bool:
        return len(self.years) > 1 or self.months == ALL_MONTHS

    def contains(self, value: str | date | None) -> bool:
        parsed = parse_document_date(value)
        if parsed is None:
            return True
        if parsed > self.cutoff or parsed.year not in self.years:
            return False
        return len(self.years) > 1 or parsed.month in self.months

    def snapshot(self) -> dict[str, Any]:
        return {
            "selected_years": list(self.years),
            "selected_months": list(self.months),
            "all_months": self.uses_all_months,
            "research_cutoff_date": self.cutoff.isoformat(),
        }

    def fingerprint(self) -> str:
        payload = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_document_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalize_window(
    *,
    selected_years: Iterable[int],
    selected_months: Iterable[int] | None,
    cutoff: str | date,
) -> ResearchWindow:
    parsed_cutoff = parse_document_date(cutoff)
    if parsed_cutoff is None:
        raise ValueError("Research cutoff date is invalid.")
    years = tuple(sorted({int(year) for year in selected_years if 1900 <= int(year) <= parsed_cutoff.year}))
    if not years:
        raise ValueError("Select at least one calendar year.")
    if len(years) > 1:
        months = ALL_MONTHS
    else:
        allowed_last_month = parsed_cutoff.month if years[0] == parsed_cutoff.year else 12
        raw_months = selected_months if selected_months is not None else range(1, allowed_last_month + 1)
        months = tuple(sorted({int(month) for month in raw_months if 1 <= int(month) <= allowed_last_month}))
        if not months:
            raise ValueError("Select at least one month.")
        if months == tuple(range(1, 13)):
            months = ALL_MONTHS
    return ResearchWindow(years=years, months=months, cutoff=parsed_cutoff)


def window_from_package(package: dict[str, Any]) -> ResearchWindow:
    cutoff = parse_document_date(package.get("research_cutoff_date"))
    if cutoff is None:
        raise ValueError("Research cutoff date is invalid.")
    years = _json_ints(package.get("selected_years_json"))
    months = _json_ints(package.get("selected_months_json"))
    if years:
        return normalize_window(selected_years=years, selected_months=months or None, cutoff=cutoff)

    history = max(1, int(package.get("filing_history_years") or 1))
    legacy_years = range(max(1900, cutoff.year - history + 1), cutoff.year + 1)
    return normalize_window(selected_years=legacy_years, selected_months=ALL_MONTHS, cutoff=cutoff)


def document_window_status(package: dict[str, Any], value: str | date | None) -> str:
    return "ELIGIBLE" if window_from_package(package).contains(value) else "OUTSIDE_SELECTED_WINDOW"


def selected_date_bounds(window: ResearchWindow) -> tuple[date, date]:
    first_year = min(window.years)
    first_month = min(window.months) if len(window.years) == 1 else 1
    last_year = max(window.years)
    last_month = max(window.months) if len(window.years) == 1 else 12
    start = date(first_year, first_month, 1)
    end = date(last_year, last_month, monthrange(last_year, last_month)[1])
    return start, min(end, window.cutoff)


def _json_ints(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    return tuple(int(item) for item in parsed) if isinstance(parsed, list) else ()
