from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


CUTLER_EQUITY_INTERN_GUIDE = "CUTLER_EQUITY_INTERN_GUIDE"

FORM_FAMILY_ALIASES = {
    "10-K": "10-K",
    "10-K/A": "10-K",
    "10-Q": "10-Q",
    "10-Q/A": "10-Q",
    "8-K": "8-K",
    "8-K/A": "8-K",
    "S-3": "S-3",
    "S-3/A": "S-3",
    "S-3ASR": "S-3",
    "S-4": "S-4",
    "S-4/A": "S-4",
    "DEF 14A": "DEF 14A",
    "144": "144",
    "144/A": "144",
}

REQUIRED_FAMILIES = ("10-K", "10-Q", "8-K", "S-3", "S-4", "DEF 14A")
CONDITIONAL_FAMILIES = ("144", "DIVIDEND_ANNOUNCEMENT", "Y-15")


@dataclass(frozen=True)
class CollectionProfile:
    name: str
    security_type: str
    required_families: tuple[str, ...]
    conditional_families: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


CUTLER_COMMON_EQUITY_PROFILE = CollectionProfile(
    name=CUTLER_EQUITY_INTERN_GUIDE,
    security_type="Common Equity",
    required_families=REQUIRED_FAMILIES,
    conditional_families=CONDITIONAL_FAMILIES,
)


def default_profile_for_security_type(security_type: str) -> CollectionProfile | None:
    if security_type == "Common Equity":
        return CUTLER_COMMON_EQUITY_PROFILE
    return None


def normalize_sec_form(form_type: str | None) -> str | None:
    """Return the approved Cutler form family while preserving unknown forms as excluded."""
    return FORM_FAMILY_ALIASES.get(str(form_type or "").strip().upper())


def is_profile_eligible(form_type: str | None, *, include_form_144: bool = False) -> bool:
    family = normalize_sec_form(form_type)
    if family in REQUIRED_FAMILIES:
        return True
    return bool(include_form_144 and family == "144")


def conditional_rule(family: str | None) -> str:
    if family == "144":
        return "Analyst selection required"
    if family == "DIVIDEND_ANNOUNCEMENT":
        return "Relevant SEC 8-K exhibit only"
    if family == "Y-15":
        return "When discovered from an official source"
    return "Included by profile"
