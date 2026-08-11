"""Primitive conversions from IBKR representations to domain types.

Kept in one place because every one of these is a trap:

* IBKR reports "no value" as ``NaN`` or as a sentinel near ``DBL_MAX``, not as
  ``None``. Passing either through arithmetic silently poisons a calculation,
  so both become ``None`` here.
* IBKR prices arrive as binary floats. ``Decimal(str(x))`` recovers the
  shortest exact decimal (``5.95``), whereas ``Decimal(x)`` would preserve the
  float's representation error.
* Expiry dates arrive as ``YYYYMMDD`` strings, and for some instruments as
  ``YYYYMM`` with no day at all.

These functions are pure and import nothing from ``ib_async``, so they are
unit-testable without the library or a connection.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_system.domain.enums import OptionRight, OrderSide, OrderStatus, SecurityType

__all__ = [
    "to_date",
    "to_decimal",
    "to_option_right",
    "to_order_side",
    "to_order_status",
    "to_security_type",
    "to_utc",
]

#: IBKR's "unset" sentinel for double fields.
_UNSET_DOUBLE = 1.7976931348623157e308
#: IBKR's "unset" sentinel for integer fields.
_UNSET_INTEGER = 2147483647

_SECURITY_TYPES = {
    "STK": SecurityType.STOCK,
    "OPT": SecurityType.OPTION,
    "FUT": SecurityType.FUTURE,
    "FOP": SecurityType.FUTURE_OPTION,
    "CASH": SecurityType.CASH,
    "IND": SecurityType.INDEX,
}

_RIGHTS = {
    "C": OptionRight.CALL,
    "CALL": OptionRight.CALL,
    "P": OptionRight.PUT,
    "PUT": OptionRight.PUT,
}

_SIDES = {
    "BUY": OrderSide.BUY,
    "BOT": OrderSide.BUY,
    "SELL": OrderSide.SELL,
    "SLD": OrderSide.SELL,
    "SSHORT": OrderSide.SELL,
}

#: IBKR order status -> our status.
#:
#: ``PendingCancel`` maps to SUBMITTED, not CANCELLED: the cancel has been
#: requested but not confirmed, and the order can still fill. Treating it as
#: cancelled would understate live exposure.
#:
#: ``Inactive`` maps to REJECTED: IBKR uses it for orders that are not working,
#: which is the fail-safe reading — never assume an inactive order is live.
_ORDER_STATUSES = {
    "PendingSubmit": OrderStatus.PENDING_SUBMIT,
    "ApiPending": OrderStatus.PENDING_SUBMIT,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "PendingCancel": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


def to_decimal(value: Any) -> Decimal | None:
    """Convert an IBKR numeric field to an exact Decimal, or ``None``.

    Returns ``None`` for missing values in every form IBKR uses: ``None``,
    ``NaN``, infinity, the unset sentinels, and the empty string.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return None if value.is_nan() else value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None if value == _UNSET_INTEGER else Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if abs(value) >= _UNSET_DOUBLE:
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return None if parsed.is_nan() else parsed
    return None


def to_utc(value: Any) -> datetime | None:
    """Normalise an IBKR timestamp to timezone-aware UTC.

    A naive timestamp is interpreted as UTC — that is what the IBKR API
    documents it to be — rather than as local time, which would silently shift
    every recorded execution by the developer's offset.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_date(value: Any) -> date | None:
    """Parse an IBKR contract date.

    ``YYYYMMDD`` yields a date. ``YYYYMM`` (used for some futures) yields
    ``None``: the day is genuinely unknown and inventing one — the first or
    last of the month — would be a fabricated expiry.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def to_security_type(value: Any) -> SecurityType:
    """Map an IBKR ``secType``. Unrecognised values become ``OTHER``, never a guess."""
    if not isinstance(value, str):
        return SecurityType.OTHER
    return _SECURITY_TYPES.get(value.strip().upper(), SecurityType.OTHER)


def to_option_right(value: Any) -> OptionRight | None:
    if not isinstance(value, str):
        return None
    return _RIGHTS.get(value.strip().upper())


def to_order_side(value: Any) -> OrderSide | None:
    if not isinstance(value, str):
        return None
    return _SIDES.get(value.strip().upper())


def to_order_status(value: Any, filled: Decimal | None = None) -> OrderStatus:
    """Map an IBKR order status, deriving PARTIALLY_FILLED from the fill count.

    IBKR reports a partly filled working order as ``Submitted`` with a non-zero
    filled quantity; there is no distinct status for it. Collapsing that into
    plain SUBMITTED would hide real, already-owned exposure.
    """
    if not isinstance(value, str):
        return OrderStatus.PENDING_SUBMIT
    mapped = _ORDER_STATUSES.get(value.strip())
    if mapped is None:
        return OrderStatus.PENDING_SUBMIT
    if mapped is OrderStatus.SUBMITTED and filled is not None and filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    return mapped
