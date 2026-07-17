from __future__ import annotations

from typing import Any


SLOT_COUNT_RULES: dict[str, tuple[int, int, int]] = {
    "most_recent_10_q_and_10_k": (2, 2, 2),
    "sell_side_reports": (1, 5, 12),
    "credit_reports": (1, 3, 5),
    "industry_report": (1, 3, 5),
    "material_company_press_releases_since_last_earnings_release": (1, 3, 12),
    "investor_presentations": (1, 2, 5),
    "morningstar_report_and_most_recent_model": (1, 2, 2),
}


def effective_document_counts(slot: dict[str, Any]) -> dict[str, int]:
    minimum, preferred, maximum = SLOT_COUNT_RULES.get(
        str(slot.get("normalized_slot_type") or ""),
        (
            int(slot.get("minimum_documents") or 1),
            int(slot.get("minimum_documents") or 1),
            int(slot.get("maximum_documents") or 1),
        ),
    )
    return {"minimum": minimum, "preferred": preferred, "maximum": maximum}
