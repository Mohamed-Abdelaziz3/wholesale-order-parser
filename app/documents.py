"""Rendering data for the picking / delivery note.

Scope and legal position
------------------------
This module produces an **internal operations document** — an ``إذن صرف`` /
``أمر تحضير`` used to pick an order in the store and hand it to a driver. It is
not, and must never be presented as, a document with tax standing.

Egypt's Tax Authority mandates electronic invoicing through its own integrated
system. This application has no e-signature, submits nothing to the authority,
and receives no UUID back from it. Every document therefore carries
:data:`LEGAL_FOOTER` verbatim, and the ``tax_id`` a merchant may type in
settings is reproduced as display text only. It asserts nothing.

:data:`LEGAL_FOOTER` is the single place in the codebase permitted to name the
banned wording, because it is the sentence that *denies* the claim. Everywhere
else that wording is forbidden outright, and
``tests/test_document_and_settings.py`` enforces exactly that by stripping this
one constant before scanning every source file for it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .money import ZERO, from_storage, money_sum
from .money import line_total as calculated_line_total

# Deliberately one unbroken literal: the wording-ban test strips this exact
# constant out of each source file before scanning it, and an implicitly
# concatenated literal would not match the text on disk, so the disclaimer would
# read as a violation of the very rule it exists to satisfy.
LEGAL_FOOTER = "مستند داخلي لتحضير وتسليم الطلب — ليس فاتورة ضريبية ولم يُرسل لمنظومة الفاتورة الإلكترونية."

CURRENCY = "ج.م"


def money(value: Any) -> str:
    """Format an amount with thousands separators and exactly two decimals."""
    try:
        amount = from_storage(value if value not in (None, "") else "0.00")
    except ValueError:
        amount = ZERO
    return f"{amount:,.2f}"


def quantity_text(value: Any) -> str:
    """Render a quantity without a pointless trailing ``.0``."""
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    if abs(amount - round(amount)) < 1e-9:
        return str(int(round(amount)))
    return f"{amount:g}"


def document_date(value: Optional[str]) -> str:
    """Render an ISO timestamp as ``YYYY/MM/DD``.

    The approval timestamp is used rather than "now": re-downloading an old
    order must produce the same document, and the date the order was approved
    is the date the warehouse cares about.
    """
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)[:10].replace("-", "/")
    return parsed.strftime("%Y/%m/%d")


def _amount(value: Any, fallback: Decimal = ZERO) -> Decimal:
    """Return a display-safe monetary value without raising for old rows."""
    try:
        return from_storage(value)
    except ValueError:
        return fallback


def _optional_amount(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return from_storage(value)
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _is_cancelled(item: Dict[str, Any]) -> bool:
    return (
        item.get("status") == "cancelled_by_human"
        or item.get("human_decision") == "CANCELLED"
        or _truthy(item.get("is_cancelled"))
    )


def _price_override(item: Dict[str, Any]) -> Optional[Decimal]:
    for key in ("price_override", "unit_price_override", "override_price"):
        value = _optional_amount(item.get(key))
        if value is not None:
            return value
    return None


def _catalog_price(item: Dict[str, Any], fallback: Decimal) -> Decimal:
    for key in ("catalog_price", "original_catalog_price"):
        value = _optional_amount(item.get(key))
        if value is not None:
            return value
    return fallback


def _is_price_overridden(item: Dict[str, Any], override: Optional[Decimal]) -> bool:
    return (
        _truthy(item.get("price_overridden"))
        or _truthy(item.get("is_price_overridden"))
        or override is not None
    )


def build_document_context(
    document: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Shape one approved order plus the shop identity for the template.

    Every monetary figure is computed here, server-side, from the approved
    snapshot. The template performs no arithmetic and the downloaded file needs
    no JavaScript to show a correct total.
    """
    lines: List[Dict[str, Any]] = []
    frozen_line_totals: list[Decimal] = []

    for item in document.get("snapshot", []):
        # A well-formed approved snapshot never contains a cancelled row.  Keep
        # this read-side guard as well so a corrupt or pre-release row cannot
        # reach a picking document.
        if _is_cancelled(item):
            continue
        index = len(lines) + 1
        override = _price_override(item)
        price = _amount(item.get("price"), override if override is not None else ZERO)
        catalog_price = _catalog_price(item, price)
        overridden = _is_price_overridden(item, override)
        qty = item.get("quantity", 0)
        frozen_total = item.get("line_total")
        line_total = (
            from_storage(frozen_total, "Snapshot line total")
            if frozen_total not in (None, "")
            else calculated_line_total(price, qty)
        )
        frozen_line_totals.append(line_total)
        lines.append(
            {
                "index": index,
                "product_id": item.get("product_id") or "",
                "product_name": item.get("product_name") or "",
                "unit": item.get("unit") or "",
                "quantity": quantity_text(qty),
                "price": money(price),
                "line_total": money(line_total),
                "catalog_price": money(catalog_price) if overridden else "",
                "price_overridden": overridden,
            }
        )

    calculated_subtotal = money_sum(frozen_line_totals)
    # New approvals freeze all three values together.  The calculated fallbacks
    # preserve legacy snapshots made before order-level financials existed.
    subtotal = _optional_amount(document.get("subtotal"))
    if subtotal is None:
        subtotal = calculated_subtotal
    discount = _amount(document.get("discount"))
    grand_total = _optional_amount(document.get("grand_total"))
    if grand_total is None:
        grand_total = max(ZERO, subtotal - discount)

    return {
        "shop": {
            "name": settings.get("shop_name") or "",
            "address": settings.get("shop_address") or "",
            "phone": settings.get("shop_phone") or "",
            "tax_id": settings.get("tax_id") or "",
            "footer_note": settings.get("footer_note") or "",
            "logo_data_url": settings.get("logo_data_url") or "",
        },
        "document_title": settings.get("document_title") or "إذن صرف",
        "order_id": document.get("order_id"),
        "date": document_date(document.get("approved_at")),
        "customer": {
            "name": document.get("customer_name") or "",
            "phone": document.get("customer_phone") or "",
            "address": document.get("customer_address") or "",
        },
        "lines": lines,
        "subtotal": money(subtotal),
        "discount": money(discount),
        "grand_total": money(grand_total),
        "currency": CURRENCY,
        "legal_footer": LEGAL_FOOTER,
    }
