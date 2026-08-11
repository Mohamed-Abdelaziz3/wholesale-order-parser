"""Minimal fail-closed unit comparison for the assisted pilot.

The pilot has one catalog unit per SKU and deliberately has no conversion
metadata.  This module therefore recognises only deterministic spellings of a
single unit family (pieces).  Every other spelling is comparable only to the
same normalised text; it is never converted to another unit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

UNIT_CHECK_PENDING = "PENDING_CATALOG_SELECTION"
UNIT_CHECK_EQUIVALENT = "EQUIVALENT"
UNIT_CHECK_MISMATCH = "MISMATCH_UNRESOLVED"
UNIT_CHECK_MISSING_REQUESTED = "MISSING_REQUESTED_UNIT"
UNIT_CHECK_MISSING_CATALOG = "MISSING_CATALOG_UNIT"
UNIT_CHECK_HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
UNIT_CHECK_MANUAL_CATALOG = "MANUAL_CATALOG_UNIT"
UNIT_CHECK_LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
UNIT_CHECK_LEGACY_APPROVED = "LEGACY_APPROVED"

_SPACE = re.compile(r"\s+")

# This is intentionally *not* the broader normalizer unit map.  In particular,
# bottle, carton, pack, dozen and box must never collapse into one another.
_PIECE_ALIASES = frozenset(
    {
        "قطعة",
        "قطعه",
        "قطع",
        "pc",
        "pcs",
        "piece",
        "pieces",
    }
)


def normalise_unit_text(value: Any) -> str:
    """Return a safe comparison key without inferring a conversion factor."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = _SPACE.sub(" ", text).casefold()
    if text in _PIECE_ALIASES:
        return "piece"
    return text


def units_equivalent(requested_unit: Any, catalog_unit: Any) -> bool:
    """True only for a non-empty deterministic unit equivalence."""
    requested = normalise_unit_text(requested_unit)
    catalog = normalise_unit_text(catalog_unit)
    return bool(requested and catalog and requested == catalog)


def classify_unit_check(requested_unit: Any, catalog_unit: Any) -> str:
    """Classify a requested/catalog unit pair without performing conversion."""
    requested = normalise_unit_text(requested_unit)
    catalog = normalise_unit_text(catalog_unit)
    if not requested:
        return UNIT_CHECK_MISSING_REQUESTED
    if not catalog:
        return UNIT_CHECK_MISSING_CATALOG
    if requested == catalog:
        return UNIT_CHECK_EQUIVALENT
    return UNIT_CHECK_MISMATCH


def unit_check_is_approval_ready(status: Any) -> bool:
    """Whether a commercial SELECT line has adequate unit evidence."""
    return str(status or "") in {
        UNIT_CHECK_EQUIVALENT,
        UNIT_CHECK_HUMAN_OVERRIDE,
        UNIT_CHECK_MANUAL_CATALOG,
    }


def unit_check_requires_resolution(status: Any) -> bool:
    """Whether the reviewer must record an explicit override before SELECT."""
    return str(status or "") in {
        UNIT_CHECK_MISMATCH,
        UNIT_CHECK_MISSING_REQUESTED,
        UNIT_CHECK_MISSING_CATALOG,
        UNIT_CHECK_LEGACY_UNVERIFIED,
    }
