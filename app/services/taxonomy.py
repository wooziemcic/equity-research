from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentCategory:
    code: str
    display_name: str
    group: str
    public_default: bool
    allowed_extensions: tuple[str, ...]
    requirement_level: str
    security_types: tuple[str, ...]


ALL_SECURITY_TYPES = ("Common Equity", "Convertible Security", "Credit / Debt", "Other")
COMMON_AND_CONVERT = ("Common Equity", "Convertible Security")
CREDIT_TYPES = ("Credit / Debt", "Convertible Security")


def _category(
    code: str,
    display: str,
    group: str,
    public: bool,
    level: str = "optional",
    security_types: tuple[str, ...] = ALL_SECURITY_TYPES,
    exts: tuple[str, ...] = (),
) -> DocumentCategory:
    return DocumentCategory(code, display, group, public, exts, level, security_types)


CATEGORIES: dict[str, DocumentCategory] = {
    item.code: item
    for item in (
        _category("annual_filing", "Annual Filing", "Company and filings", True, "required"),
        _category("quarterly_filing", "Quarterly Filing", "Company and filings", True, "required"),
        _category("current_report", "Current Report", "Company and filings", True, "required"),
        _category("proxy_statement", "Proxy Statement", "Company and filings", True, "recommended"),
        _category("registration_s3", "S-3 Registration Statement", "Company and filings", True, "recommended"),
        _category("registration_s4", "S-4 Registration Statement", "Company and filings", True, "recommended"),
        _category("form_144", "Selected Form 144", "Company and filings", True, "optional"),
        _category("dividend_announcement", "Dividend Announcement", "Company and filings", True, "optional"),
        _category("y15_regulatory_report", "Y-15 Regulatory Report", "Company and filings", True, "optional"),
        _category("analyst_data_sheet", "Analyst Data Sheet", "Models and analyst materials", False, "optional"),
        _category("internal_research", "Internal Research", "Models and analyst materials", False, "optional"),
        _category("other_research", "Other Research", "External research", False, "optional"),
        _category("earnings_release", "Earnings Release", "Earnings and presentations", True, "required"),
        _category("earnings_presentation", "Earnings Presentation", "Earnings and presentations", True, "required"),
        _category("earnings_transcript", "Earnings Transcript", "Earnings and presentations", False, "recommended"),
        _category("investor_presentation", "Investor Presentation", "Earnings and presentations", True, "required"),
        _category("investor_day", "Investor Day Material", "Earnings and presentations", True, "optional"),
        _category("company_press_release", "Company Press Release", "Company and filings", True, "optional"),
        _category("executive_compensation", "Executive Compensation", "Company and filings", True, "optional"),
        _category("esg_sustainability", "ESG / Sustainability", "Company and filings", True, "optional"),
        _category("bloomberg_des", "Bloomberg DES", "Terminal and market data", False, "recommended"),
        _category("bloomberg_fa", "Bloomberg FA", "Terminal and market data", False, "recommended"),
        _category("bloomberg_anr", "Bloomberg ANR", "Terminal and market data", False, "recommended"),
        _category("bloomberg_drsk", "Bloomberg DRSK", "Terminal and market data", False, "recommended"),
        _category("bloomberg_credit", "Bloomberg Credit", "Terminal and market data", False, "recommended", CREDIT_TYPES),
        _category("bloomberg_other", "Bloomberg Other", "Terminal and market data", False, "optional"),
        _category("factset_export", "FactSet Export", "Terminal and market data", False, "recommended"),
        _category("morningstar_report", "Morningstar Report", "Terminal and market data", False, "recommended"),
        _category("morningstar_model", "Morningstar Model", "Terminal and market data", False, "optional"),
        _category("historical_valuation", "Historical Valuation Analysis", "Terminal and market data", False, "recommended"),
        _category("sell_side_research", "Sell-Side Research", "External research", False, "recommended"),
        _category("sell_side_initiation", "Sell-Side Initiation Report", "External research", False, "recommended"),
        _category("industry_research", "Industry Research", "External research", False, "recommended"),
        _category("credit_research", "Credit Research", "Credit and risk", False, "recommended", CREDIT_TYPES),
        _category("rating_agency", "Rating Agency Research", "Credit and risk", False, "optional", CREDIT_TYPES),
        _category("activist_research", "Activist Research", "External research", False, "recommended", COMMON_AND_CONVERT),
        _category("short_seller_research", "Short-Seller Research", "External research", False, "recommended", COMMON_AND_CONVERT),
        _category("legal_regulatory", "Legal / Regulatory Research", "External research", False, "optional"),
        _category("financial_model", "Financial Model", "Models and analyst materials", False, "recommended"),
        _category("convertible_analysis", "Convertible Analysis", "Models and analyst materials", False, "required", ("Convertible Security",)),
        _category("debt_analysis", "Debt Analysis", "Credit and risk", False, "required", CREDIT_TYPES),
        _category("internal_notes", "Internal Analyst Notes", "Models and analyst materials", False, "optional"),
        _category("other", "Other", "Models and analyst materials", False, "optional"),
    )
}


CHECKLIST_PROFILES: dict[str, list[dict[str, str]]] = {
    "Common Equity": [
        {"id": "latest_annual", "category_code": "annual_filing", "display_name": "Latest annual filing", "requirement_level": "required", "group": "Company and filings"},
        {"id": "latest_quarterly", "category_code": "quarterly_filing", "display_name": "Latest quarterly filing", "requirement_level": "required", "group": "Company and filings"},
        {"id": "recent_8k", "category_code": "current_report", "display_name": "Recent material current reports", "requirement_level": "required", "group": "Company and filings"},
        {"id": "registration_s3", "category_code": "registration_s3", "display_name": "S-3 when filed in period", "requirement_level": "recommended", "group": "Company and filings"},
        {"id": "registration_s4", "category_code": "registration_s4", "display_name": "S-4 when filed in period", "requirement_level": "recommended", "group": "Company and filings"},
        {"id": "latest_proxy", "category_code": "proxy_statement", "display_name": "Latest DEF 14A", "requirement_level": "required", "group": "Company and filings"},
        {"id": "selected_form_144", "category_code": "form_144", "display_name": "Selected Form 144", "requirement_level": "optional", "group": "Company and filings"},
        {"id": "dividend_announcement", "category_code": "dividend_announcement", "display_name": "Dividend announcement", "requirement_level": "optional", "group": "Company and filings"},
        {"id": "y15_report", "category_code": "y15_regulatory_report", "display_name": "Y-15 regulatory report", "requirement_level": "optional", "group": "Company and filings"},
        {"id": "bloomberg_des", "category_code": "bloomberg_des", "display_name": "Bloomberg DES", "requirement_level": "recommended", "group": "Terminal and market data"},
        {"id": "bloomberg_fa", "category_code": "bloomberg_fa", "display_name": "Bloomberg FA", "requirement_level": "recommended", "group": "Terminal and market data"},
        {"id": "bloomberg_anr", "category_code": "bloomberg_anr", "display_name": "Bloomberg ANR", "requirement_level": "recommended", "group": "Terminal and market data"},
        {"id": "bloomberg_drsk", "category_code": "bloomberg_drsk", "display_name": "Bloomberg DRSK", "requirement_level": "recommended", "group": "Terminal and market data"},
        {"id": "sell_side_research", "category_code": "sell_side_research", "display_name": "Sell-side research", "requirement_level": "recommended", "group": "External research"},
        {"id": "industry_research", "category_code": "industry_research", "display_name": "Industry research", "requirement_level": "recommended", "group": "External research"},
        {"id": "historical_valuation", "category_code": "historical_valuation", "display_name": "Historical valuation analysis", "requirement_level": "recommended", "group": "Terminal and market data"},
        {"id": "credit_research_when_relevant", "category_code": "credit_research", "display_name": "Credit research, when relevant", "requirement_level": "recommended", "group": "Credit and risk"},
        {"id": "activist_bear", "category_code": "short_seller_research", "display_name": "Activist or bearish research, when available", "requirement_level": "recommended", "group": "External research"},
        {"id": "esg_report", "category_code": "esg_sustainability", "display_name": "ESG report", "requirement_level": "optional", "group": "Company and filings"},
        {"id": "executive_comp", "category_code": "executive_compensation", "display_name": "Executive compensation", "requirement_level": "optional", "group": "Company and filings"},
        {"id": "investor_day", "category_code": "investor_day", "display_name": "Investor day materials", "requirement_level": "optional", "group": "Earnings and presentations"},
        {"id": "rating_agency", "category_code": "rating_agency", "display_name": "Rating agency report", "requirement_level": "optional", "group": "Credit and risk"},
        {"id": "internal_notes", "category_code": "internal_notes", "display_name": "Internal analyst notes", "requirement_level": "optional", "group": "Models and analyst materials"},
    ],
    "Convertible Security": [
        {"id": "convertible_terms", "category_code": "convertible_analysis", "display_name": "Convertible terms", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "conversion_price", "category_code": "convertible_analysis", "display_name": "Conversion price", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "conversion_premium", "category_code": "convertible_analysis", "display_name": "Conversion premium", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "coupon", "category_code": "convertible_analysis", "display_name": "Coupon", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "maturity", "category_code": "convertible_analysis", "display_name": "Maturity", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "call_provisions", "category_code": "convertible_analysis", "display_name": "Call provisions", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "bond_floor", "category_code": "debt_analysis", "display_name": "Bond floor analysis", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "equity_sensitivity", "category_code": "convertible_analysis", "display_name": "Equity sensitivity", "requirement_level": "required", "group": "Models and analyst materials"},
        {"id": "credit_analysis", "category_code": "credit_research", "display_name": "Credit analysis", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "up_down_analysis", "category_code": "financial_model", "display_name": "Up/down analysis", "requirement_level": "required", "group": "Models and analyst materials"},
    ],
    "Credit / Debt": [
        {"id": "debt_terms", "category_code": "debt_analysis", "display_name": "Debt terms", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "maturity_schedule", "category_code": "debt_analysis", "display_name": "Maturity schedule", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "covenants", "category_code": "debt_analysis", "display_name": "Covenant information", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "credit_ratings", "category_code": "rating_agency", "display_name": "Credit ratings", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "liquidity", "category_code": "credit_research", "display_name": "Liquidity analysis", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "interest_coverage", "category_code": "debt_analysis", "display_name": "Interest coverage", "requirement_level": "required", "group": "Credit and risk"},
        {"id": "recovery_downside", "category_code": "credit_research", "display_name": "Recovery or downside analysis", "requirement_level": "required", "group": "Credit and risk"},
    ],
    "Other": [
        {"id": "company_overview", "category_code": "investor_presentation", "display_name": "Company overview", "requirement_level": "required", "group": "Earnings and presentations"},
        {"id": "supporting_research", "category_code": "other", "display_name": "Supporting research", "requirement_level": "recommended", "group": "Models and analyst materials"},
    ],
}

CHECKLIST_PROFILES["Convertible Security"] = CHECKLIST_PROFILES["Common Equity"] + CHECKLIST_PROFILES["Convertible Security"]
CHECKLIST_PROFILES["Credit / Debt"] = CHECKLIST_PROFILES["Credit / Debt"] + [
    item for item in CHECKLIST_PROFILES["Common Equity"] if item["requirement_level"] != "required"
]


def category_options() -> list[tuple[str, str]]:
    """Return category options sorted by group then display name."""
    return [
        (category.code, category.display_name)
        for category in sorted(CATEGORIES.values(), key=lambda item: (item.group, item.display_name))
    ]


def category_display(code: str | None) -> str:
    if not code:
        return ""
    return CATEGORIES.get(code, CATEGORIES["other"]).display_name
