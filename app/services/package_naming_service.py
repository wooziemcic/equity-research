from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


SLOT_LABELS = {
    "most_recent_10_q_and_10_k": {"10-K": "10K", "10-Q": "10Q"},
    "latest_earnings_release": "Earnings Release",
    "available_supplemental_or_earnings_presentation": "Earnings Presentation",
    "latest_earnings_call_audio": "Earnings Call Audio",
    "latest_earnings_call_transcript": "Earnings Commentary",
    "investor_presentations": "Investor Presentation",
    "material_company_press_releases_since_last_earnings_release": "Company Announcement",
    "liquidity_and_capital_resources": "Liquidity & Capital Resources",
    "description_of_business_and_risk": "Business & Risk Factors",
    "executive_compensation_information": "Executive Compensation Info",
    "sell_side_reports": "Sell-Side Report",
    "initiated_coverage_report": "Initiated Coverage",
    "credit_reports": "Credit Report",
    "industry_report": "Industry Report",
    "morningstar_report_and_most_recent_model": "Morningstar",
    "bbg_des": "DES",
    "bbg_dvd": "DVD",
    "bbg_hds": "HDS",
    "bbg_anr": "ANR",
    "drsk_default_risk": "DRSK",
    "bbg_fa": "FA",
    "bbg_fa_credit_ratios": "Credit Ratios",
    "ccm_historical_multiples_valuation": "EV EBITDA Analysis",
}

UPLOAD_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("bbg_fa_credit_ratios", ("credit ratios",), "Bloomberg"),
    ("ccm_historical_multiples_valuation", ("ev ebitda",), "Cutler"),
    ("ccm_historical_multiples_valuation", ("valuation",), "Cutler"),
    ("initiated_coverage_report", ("initiation",), "Sell-Side"),
    ("industry_report", ("industry",), "Third-Party Research"),
    ("morningstar_report_and_most_recent_model", ("morningstar",), "Morningstar"),
    ("credit_reports", ("moody",), "Moody's"),
    ("credit_reports", ("s&p",), "S&P Global"),
    ("credit_reports", ("bloomberg intelligence",), "Bloomberg Intelligence"),
    ("credit_reports", (" bi ", "credit"), "Bloomberg Intelligence"),
    ("bbg_des", (" des ",), "Bloomberg"),
    ("bbg_dvd", (" dvd ",), "Bloomberg"),
    ("bbg_hds", (" hds ",), "Bloomberg"),
    ("bbg_anr", (" anr ",), "Bloomberg"),
    ("drsk_default_risk", (" drsk ",), "Bloomberg"),
    ("bbg_fa", (" fa ",), "Bloomberg"),
    ("sell_side_reports", (" gs ",), "GS"),
    ("sell_side_reports", (" jpm ",), "JPM"),
    ("sell_side_reports", ("jefferies",), "Jefferies"),
    ("sell_side_reports", ("evercore",), "Evercore"),
    ("most_recent_10_q_and_10_k", ("10-k",), "SEC"),
    ("most_recent_10_q_and_10_k", ("10k",), "SEC"),
    ("most_recent_10_q_and_10_k", ("10-q",), "SEC"),
    ("most_recent_10_q_and_10_k", ("10q",), "SEC"),
    ("available_supplemental_or_earnings_presentation", ("earnings presentation",), "Company"),
    ("latest_earnings_release", ("earnings release",), "Company"),
    ("latest_earnings_call_transcript", ("earnings commentary",), "Company"),
    ("latest_earnings_call_transcript", ("transcript",), "Company"),
    ("material_company_press_releases_since_last_earnings_release", ("press release",), "Company"),
    ("liquidity_and_capital_resources", ("liquidity",), "SEC"),
    ("description_of_business_and_risk", ("risk factors",), "SEC"),
    ("executive_compensation_information", ("executive compensation",), "SEC"),
)


@dataclass(frozen=True)
class UploadClassification:
    normalized_slot_type: str | None
    source: str
    document_date: str | None
    confidence: str
    matched_tokens: tuple[str, ...]
    explanation: str


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _safe_component(value: str) -> str:
    value = _ascii(value).replace("&", "and")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def parse_filename_date(filename: str) -> str | None:
    stem = Path(filename).stem
    iso = re.search(r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)", stem)
    if iso:
        year, month, day = map(int, iso.groups())
    else:
        short = re.search(r"(?<!\d)(\d{1,2})[.-](\d{1,2})[.-](\d{2,4})(?!\d)", stem)
        if not short:
            return None
        month, day, year = map(int, short.groups())
        year += 2000 if year < 100 else 0
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def readable_date(value: str | None) -> str:
    if not value:
        return "Undated"
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return "Undated"
    return f"{parsed.month}.{parsed.day}.{str(parsed.year)[-2:]}"


def classify_upload_filename(filename: str) -> UploadClassification:
    normalized = f" {_ascii(Path(filename).stem).casefold().replace('_', ' ').replace('-', ' ')} "
    normalized = re.sub(r"\s+", " ", normalized)
    extension = Path(filename).suffix.casefold()
    for slot_type, tokens, source in UPLOAD_RULES:
        if all(token in normalized for token in tokens):
            if slot_type == "morningstar_report_and_most_recent_model" and extension in {".xls", ".xlsx", ".xlsm"}:
                source = "Morningstar Model"
            return UploadClassification(
                slot_type,
                source,
                parse_filename_date(filename),
                "HIGH",
                tuple(token.strip() for token in tokens),
                f"Matched {', '.join(token.strip() for token in tokens)} in the filename.",
            )
    return UploadClassification(None, "Unknown", parse_filename_date(filename), "LOW", (), "No reliable Cutler filename rule matched.")


def generate_package_display_filename(
    *,
    ticker: str,
    slot_type: str,
    document: dict[str, Any],
    anchor: dict[str, Any] | None = None,
    existing_names: Iterable[str] = (),
) -> str:
    ticker = _safe_component(ticker.upper()) or "TICKER"
    form = str(document.get("form_type") or document.get("normalized_form_family") or "").upper()
    label_value = SLOT_LABELS.get(slot_type, _safe_component(document.get("title") or "Document"))
    label = label_value.get(form, form.replace("-", "")) if isinstance(label_value, dict) else label_value
    source = str(document.get("source_institution") or document.get("source_name") or "")
    if slot_type in {"sell_side_reports", "credit_reports", "industry_report"} and source:
        label = _safe_component(source)
    period = ""
    if form in {"10-K", "10-Q"}:
        fiscal_year = (anchor or {}).get("fiscal_year")
        quarter = (anchor or {}).get("fiscal_quarter") if form == "10-Q" else None
        period = " ".join(part for part in (quarter, f"FY{str(fiscal_year)[-2:]}" if fiscal_year else None) if part)
    document_date = document.get("document_date") or document.get("publication_date") or document.get("filing_date")
    extension = Path(str(document.get("original_filename") or document.get("local_filename") or "")).suffix.lower()
    if not extension:
        extension = {"application/pdf": ".pdf", "text/html": ".html"}.get(str(document.get("mime_type") or ""), ".bin")
    base = _safe_component(" ".join(part for part in (ticker, str(label), period, readable_date(document_date)) if part))
    base = base[: max(24, 180 - len(extension))].rstrip(" .")
    used = {name.casefold() for name in existing_names}
    candidate = f"{base}{extension}"
    sequence = 2
    while candidate.casefold() in used:
        candidate = f"{base} {sequence}{extension}"
        sequence += 1
    return candidate
