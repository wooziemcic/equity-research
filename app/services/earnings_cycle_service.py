from __future__ import annotations

import re
from datetime import date
from dataclasses import asdict, dataclass
from typing import Any, Iterable


AUTHORITATIVE_TYPES = {
    "OFFICIAL_EARNINGS_RELEASE": 5,
    "SEC_10Q": 4,
    "SEC_10K": 4,
    "SEC_8K_EXHIBIT": 3,
    "OFFICIAL_EARNINGS_PRESENTATION": 2,
    "OFFICIAL_IR_EVENT": 1,
    "OFFICIAL_IR": 2,
    "SEC": 1,
    "SEC_8K": 1,
}


@dataclass(frozen=True)
class EarningsCycle:
    fiscal_year: int | None
    fiscal_quarter: str | None
    fiscal_period_label: str | None
    reporting_period_start: str | None
    reporting_period_end: str | None
    earnings_release_date: str | None
    filing_date: str | None
    filing_form: str | None
    accession: str | None
    source_document_id: str | None
    source_candidate_id: str | None
    source_url: str | None
    anchor_source: str
    confidence: str
    validation_status: str
    evidence_summary: str
    missing_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["missing_fields"] = list(self.missing_fields)
        return value


def fiscal_quarter_from_text(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value.casefold())
    match = re.search(r"\bq(?:uarter)?\s*([1-4])\b", normalized)
    if match:
        return f"Q{match.group(1)}"
    words = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}
    return next((quarter for word, quarter in words.items() if f"{word} quarter" in normalized), None)


def fiscal_year_from_text(value: str) -> int | None:
    match = re.search(r"\b(?:fy\s*)?(20\d{2})\b", value, re.I)
    return int(match.group(1)) if match else None


def fiscal_period_from_report_date(report_period: str, fiscal_year_end: str, form: str) -> tuple[int | None, str | None]:
    """Infer fiscal focus from an SEC report date without substituting a filing date."""
    try:
        period = date.fromisoformat(report_period[:10])
        end_month = int(str(fiscal_year_end).zfill(4)[:2])
    except (TypeError, ValueError):
        return None, None
    fiscal_year = period.year if period.month <= end_month else period.year + 1
    if form.upper() == "10-K":
        return fiscal_year, "Q4"
    months_after_year_end = (period.month - end_month) % 12
    quarter = min(4, max(1, round(months_after_year_end / 3)))
    return fiscal_year, f"Q{quarter}"


def resolve_earnings_cycle(sources: Iterable[dict[str, Any]]) -> EarningsCycle:
    rows = []
    for source in sources:
        row = dict(source)
        source_type = str(row.get("source_type") or "").upper()
        if source_type == "BRAVE_SNIPPET" or source_type not in AUTHORITATIVE_TYPES:
            continue
        text = " ".join(str(row.get(key) or "") for key in ("title", "fiscal_period_label", "description", "source_url"))
        row["source_type"] = source_type
        row["fiscal_quarter"] = row.get("fiscal_quarter") or fiscal_quarter_from_text(text)
        row["fiscal_year"] = row.get("fiscal_year") or fiscal_year_from_text(text)
        rows.append(row)
    if not rows:
        return EarningsCycle(
            None, None, None, None, None, None, None, None, None, None, None, None,
            "NONE", "LOW", "NEEDS_ANALYST_REVIEW",
            "No authoritative source established the latest completed earnings cycle.",
            ("fiscal quarter", "reporting period end"),
        )

    rows.sort(
        key=lambda row: (
            str(row.get("reporting_period_end") or ""),
            str(row.get("earnings_release_date") or row.get("filing_date") or ""),
            AUTHORITATIVE_TYPES[row["source_type"]],
        ),
        reverse=True,
    )
    best = rows[0]
    period_end = str(best.get("reporting_period_end") or "") or None
    comparable = [row for row in rows if period_end and row.get("reporting_period_end") == period_end]
    if not comparable:
        comparable = [best]

    def consensus(field: str) -> Any:
        values = [row.get(field) for row in comparable if row.get(field) not in (None, "")]
        return max(set(values), key=values.count) if values else best.get(field)

    fiscal_year = consensus("fiscal_year")
    fiscal_quarter = consensus("fiscal_quarter")
    period_start = consensus("reporting_period_start")
    release_date = consensus("earnings_release_date")
    missing = []
    if not fiscal_quarter:
        missing.append("fiscal quarter")
    if not period_end:
        missing.append("reporting period end")
    used_filing_proxy = bool(best.get("reporting_period_is_filing_date_proxy"))
    if used_filing_proxy:
        missing.append("verified reporting period end")

    complete = bool(fiscal_year and fiscal_quarter and period_end and not used_filing_proxy)
    agreement_rows = [
        row for row in comparable
        if row.get("fiscal_quarter") == fiscal_quarter
        and row.get("fiscal_year") == fiscal_year
        and row.get("reporting_period_end") == period_end
    ]
    independent_types = {row["source_type"] for row in agreement_rows}
    conflict = any(
        len({row.get(field) for row in comparable if row.get(field)}) > 1
        for field in ("fiscal_year", "fiscal_quarter", "reporting_period_end")
    )
    if complete and len(independent_types) >= 2 and not conflict:
        confidence, status = "HIGH", "VALIDATED"
        summary = "At least two authoritative sources agree on the fiscal period and reporting-period end."
    elif complete and not conflict:
        confidence, status = "MEDIUM", "VALIDATED"
        summary = "One authoritative source provides complete fiscal-period evidence."
    else:
        confidence, status = "LOW", "NEEDS_ANALYST_REVIEW"
        detail = ", ".join(dict.fromkeys(missing)) or "conflicting authoritative dates"
        summary = f"Analyst confirmation is required because the anchor lacks {detail}."

    period_label = consensus("fiscal_period_label")
    if not period_label and fiscal_quarter and fiscal_year:
        period_label = f"{fiscal_quarter} FY{str(fiscal_year)[-2:]}"
    return EarningsCycle(
        int(fiscal_year) if fiscal_year else None,
        str(fiscal_quarter) if fiscal_quarter else None,
        str(period_label or "") or None,
        str(period_start or "") or None,
        period_end,
        str(release_date or "") or None,
        str(best.get("filing_date") or "") or None,
        str(best.get("filing_form") or "") or None,
        str(best.get("accession") or "") or None,
        str(best.get("source_document_id") or "") or None,
        str(best.get("source_candidate_id") or "") or None,
        str(best.get("source_url") or "") or None,
        best["source_type"],
        confidence,
        status,
        summary,
        tuple(dict.fromkeys(missing)),
    )
