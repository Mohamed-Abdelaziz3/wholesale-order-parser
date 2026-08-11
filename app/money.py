"""Exact commercial-money primitives.

The application stores every *new* monetary amount as canonical two-place
decimal text (for example ``"19.99"``).  SQLite's dynamic typing makes this
more reliable than ``REAL``: no binary floating-point value becomes part of a
commercial decision or an immutable approval snapshot.

Quantities deliberately remain quantities, not money.  They are converted via
``Decimal(str(value))`` only at the one boundary where a line total is derived.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

MONEY_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def _decimal(value: Any, name: str) -> Decimal:
    """Parse without ever passing through binary float arithmetic."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a valid monetary amount")
    else:
        try:
            # ``str`` is intentional for legacy sqlite REAL / JSON-number input:
            # it preserves the human-visible decimal representation rather than
            # importing the binary expansion into the commercial calculation.
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name} must be a valid monetary amount") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def parse_money(value: Any, name: str = "Amount", *, minimum: Decimal = ZERO) -> Decimal:
    """Validate a user/catalog monetary input with no silent rounding.

    Derived values may be rounded by :func:`line_total`; entered prices and
    discounts may not.  This deliberately rejects more than two fractional
    places instead of changing what an operator entered.
    """
    amount = _decimal(value, name)
    if amount < minimum:
        raise ValueError(f"{name} must be at least {to_storage(minimum)}")
    if amount.as_tuple().exponent < -2:
        raise ValueError(f"{name} must have at most two decimal places")
    return amount.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def legacy_money(value: Any, name: str = "Legacy amount") -> Decimal:
    """Read an existing persisted amount without altering its evidence.

    Legacy REAL rows can contain more than two places.  They remain readable as
    the exact decimal spelling exposed by SQLite/Python; only new commercial
    writes are constrained to the two-place invariant.
    """
    return _decimal(value, name)


def legacy_to_storage(value: Any, name: str = "Legacy amount") -> str:
    """Persist a legacy amount without silently reducing its recorded scale."""
    return format(legacy_money(value, name), "f")


def to_storage(value: Decimal) -> str:
    """Return the canonical SQLite/API representation for a new money amount."""
    rounded = value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    # Decimal keeps a sign bit on zero. Commercial storage has one canonical
    # representation for zero, so an accepted ``-0`` cannot create a second
    # audit/action payload spelling.
    if rounded == ZERO:
        return "0.00"
    return rounded.to_eng_string()


def from_storage(value: Any, name: str = "Amount") -> Decimal:
    """Read canonical or legacy text/REAL without using float arithmetic."""
    return legacy_money(value, name)


def quantity_decimal(value: Any, name: str = "Quantity") -> Decimal:
    """Validate a finite positive/non-negative quantity for multiplication."""
    amount = _decimal(value, name)
    return amount


def line_total(unit_price: Any, quantity: Any) -> Decimal:
    """Apply the pilot rounding rule: unit price × quantity, HALF_UP to EGP cents."""
    price = from_storage(unit_price, "Unit price")
    qty = quantity_decimal(quantity)
    if price < ZERO:
        raise ValueError("Unit price must not be negative")
    if qty < Decimal("0"):
        raise ValueError("Quantity must not be negative")
    return (price * qty).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def money_sum(values: Iterable[Any]) -> Decimal:
    """Sum already-rounded monetary amounts exactly."""
    total = ZERO
    for value in values:
        total += from_storage(value)
    return total.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def api_money(value: Any, name: str = "Amount") -> float:
    """Stable numeric API presentation boundary, never a calculation boundary.

    Frontend consumers historically receive JSON numbers.  Business logic and
    persistence remain Decimal/Text; this conversion happens only after a
    value is final so the existing UI contract stays usable during the pilot.
    """
    return float(from_storage(value, name))
