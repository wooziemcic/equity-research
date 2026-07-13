from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceType:
    code: str
    display_name: str


EVIDENCE_TYPES: dict[str, EvidenceType] = {
    item.code: item
    for item in (
        EvidenceType("COMPANY_DESCRIPTION", "Company Description"),
        EvidenceType("BUSINESS_SEGMENT", "Business Segment"),
        EvidenceType("REPORTED_REVENUE", "Reported Revenue"),
        EvidenceType("REPORTED_GROWTH", "Reported Growth"),
        EvidenceType("REPORTED_MARGIN", "Reported Margin"),
        EvidenceType("REPORTED_EPS", "Reported EPS"),
        EvidenceType("REPORTED_CASH_FLOW", "Reported Cash Flow"),
        EvidenceType("REPORTED_DEBT", "Reported Debt"),
        EvidenceType("REPORTED_LIQUIDITY", "Reported Liquidity"),
        EvidenceType("MANAGEMENT_GUIDANCE", "Management Guidance"),
        EvidenceType("ANALYST_ESTIMATE", "Analyst Estimate"),
        EvidenceType("ANALYST_RATING", "Analyst Rating"),
        EvidenceType("PRICE_TARGET", "Price Target"),
        EvidenceType("CREDIT_RATING", "Credit Rating"),
        EvidenceType("COVENANT", "Covenant"),
        EvidenceType("CAPITAL_ALLOCATION", "Capital Allocation"),
        EvidenceType("ACQUISITION", "Acquisition"),
        EvidenceType("DIVESTITURE", "Divestiture"),
        EvidenceType("CATALYST", "Catalyst"),
        EvidenceType("RISK", "Risk"),
        EvidenceType("LEGAL_REGULATORY", "Legal / Regulatory"),
        EvidenceType("COMPETITIVE_POSITION", "Competitive Position"),
        EvidenceType("MANAGEMENT_CLAIM", "Management Claim"),
        EvidenceType("ACTIVIST_CLAIM", "Activist Claim"),
        EvidenceType("SHORT_SELLER_CLAIM", "Short-Seller Claim"),
        EvidenceType("VALUATION_MULTIPLE", "Valuation Multiple"),
        EvidenceType("CONVERTIBLE_TERM", "Convertible Term"),
        EvidenceType("OTHER_FACT", "Other Fact"),
    )
}


SOURCE_QUALITY_LABELS = (
    "PRIMARY_REGULATORY",
    "PRIMARY_COMPANY",
    "LICENSED_SELL_SIDE",
    "LICENSED_CREDIT",
    "TERMINAL_EXPORT",
    "ACTIVIST",
    "SHORT_SELLER",
    "INTERNAL_ANALYST",
    "OTHER",
)


def evidence_type_options() -> list[tuple[str, str]]:
    return [(item.code, item.display_name) for item in EVIDENCE_TYPES.values()]


def evidence_type_display(code: str | None) -> str:
    if not code:
        return ""
    return EVIDENCE_TYPES.get(code, EVIDENCE_TYPES["OTHER_FACT"]).display_name
