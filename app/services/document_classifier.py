from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class ClassificationSuggestion:
    category_code: str
    category_display: str
    confidence: str
    method: str
    rules_matched: list[str]


RULES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("bloomberg_credit_ratios", ("credit", "ratios"), "Bloomberg Credit Ratios", "High"),
    ("historical_valuation", ("ev", "ebitda"), "Historical Multiples or Valuation Analysis", "High"),
    ("morningstar_model", ("morningstar",), "Morningstar Model", "High"),
    ("bloomberg_des", ("des",), "Bloomberg DES", "High"),
    ("bloomberg_dvd", ("dvd",), "Bloomberg DVD", "High"),
    ("bloomberg_hds", ("hds",), "Bloomberg HDS", "High"),
    ("bloomberg_anr", ("anr",), "Bloomberg ANR", "High"),
    ("bloomberg_drsk", ("drsk",), "Bloomberg DRSK", "High"),
    ("bloomberg_fa", ("fa",), "Bloomberg FA", "High"),
    ("bloomberg_des", ("bloomberg", "des"), "Bloomberg DES", "High"),
    ("bloomberg_fa", ("bloomberg", "fa"), "Bloomberg FA", "High"),
    ("annual_filing", ("10-k",), "Annual Filing", "High"),
    ("quarterly_filing", ("10-q",), "Quarterly Filing", "High"),
    ("current_report", ("8-k",), "Current Report", "High"),
    ("earnings_transcript", ("transcript",), "Earnings Transcript", "High"),
    ("sell_side_initiation", ("initiation",), "Sell-Side Initiation Report", "High"),
    ("rating_agency", ("moody",), "Rating Agency Research", "High"),
    ("rating_agency", ("s&p",), "Rating Agency Research", "High"),
    ("rating_agency", ("fitch",), "Rating Agency Research", "High"),
    ("credit_research", ("credit",), "Credit Research", "Medium"),
    ("short_seller_research", ("short report",), "Short-Seller Research", "High"),
    ("short_seller_research", ("short-seller",), "Short-Seller Research", "High"),
    ("financial_model", ("model",), "Financial Model", "Medium"),
    ("earnings_release", ("earnings", "release"), "Earnings Release", "Medium"),
    ("earnings_presentation", ("earnings", "presentation"), "Earnings Presentation", "Medium"),
    ("investor_presentation", ("investor", "presentation"), "Investor Presentation", "Medium"),
    ("industry_research", ("industry",), "Industry Research", "Medium"),
    ("factset_export", ("factset",), "FactSet Export", "High"),
    ("morningstar_report", ("morningstar",), "Morningstar Report", "High"),
)


def classify_document(filename: str, *, source_type: str = "") -> ClassificationSuggestion:
    """Suggest a document category from safe metadata only."""
    stem = Path(filename).stem.lower().replace("_", " ").replace(".", " ")
    ext = Path(filename).suffix.lower()
    haystack = f"{stem} {source_type.lower()}".replace("_", " ")
    for category_code, keywords, display, confidence in RULES:
        matched = all(
            bool(re.search(rf"\b{re.escape(keyword)}\b", haystack)) if len(keyword) <= 4 else keyword in haystack
            for keyword in keywords
        )
        if matched:
            if category_code == "morningstar_model" and ext not in {".xlsx", ".xlsm", ".xls", ".csv"}:
                continue
            if category_code == "financial_model" and ext not in {".xlsx", ".xlsm", ".csv"}:
                continue
            return ClassificationSuggestion(
                category_code=category_code,
                category_display=display,
                confidence=confidence,
                method="filename_source_rules",
                rules_matched=["+".join(keywords)],
            )
    if source_type == "bloomberg":
        return ClassificationSuggestion("bloomberg_other", "Bloomberg Other", "Low", "source_default", ["source:bloomberg"])
    if source_type == "factset":
        return ClassificationSuggestion("factset_export", "FactSet Export", "Medium", "source_default", ["source:factset"])
    if source_type == "morningstar":
        return ClassificationSuggestion("morningstar_report", "Morningstar Report", "Medium", "source_default", ["source:morningstar"])
    return ClassificationSuggestion("other", "Other", "Low", "fallback", [])
