"""Point-in-time visibility and look-ahead protection.

One question has to be answerable for every stored record: *was this available
to the system at time T?* Everything historical depends on it. A backtest that
reads tomorrow's news, or an evaluation that scores a decision against a price
that had not printed yet, produces results that look excellent and mean
nothing.

The rule implemented here is deliberately strict and has two parts:

1. **Retrieval binds.** Information we had not fetched was not information we
   had. A filing published in 2019 that we first downloaded today is invisible
   to any reconstruction of last week.
2. **Every clock must have passed.** ``as_of``, ``source_timestamp``,
   ``published_at`` and ``effective_at`` must all be at or before T, not just
   whichever one is convenient.

Future-dated *content* is fine and expected — an earnings date next month is
exactly what a calendar is for. What is forbidden is the record being visible
before it was known. :class:`~trading_system.data.models.CorporateEvent`
therefore gates on ``announced_at``, not on ``event_time``.

Nothing here filters silently by default: :func:`assert_no_look_ahead` exists
so a caller can demand a hard failure rather than a quietly shortened list.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_system.data.models import DataRecord, DataSnapshot

__all__ = [
    "LookAheadError",
    "PointInTime",
    "assert_no_look_ahead",
    "latest_as_of",
    "visible_at",
]


class LookAheadError(RuntimeError):
    """A record that was not yet knowable was about to be used as if it were.

    Raised rather than silently dropped: a look-ahead leak is a correctness
    bug, and the only safe default is to stop.
    """


@runtime_checkable
class PointInTime(Protocol):
    """Anything that can say whether it was known at a given instant."""

    def known_at(self, instant: datetime) -> bool: ...


def visible_at[ItemT: PointInTime](items: Iterable[ItemT], as_of: datetime) -> list[ItemT]:
    """Those items that were already known at ``as_of``.

    Order is preserved. Anything retrieved, published or effective after
    ``as_of`` is excluded — including records that describe an earlier instant
    but reached us later.
    """
    _require_aware(as_of)
    return [item for item in items if item.known_at(as_of)]


def assert_no_look_ahead[ItemT: PointInTime](items: Iterable[ItemT], as_of: datetime) -> None:
    """Raise if any item would not have been known at ``as_of``.

    Use where a caller believes it has already filtered correctly and wants
    that belief checked, rather than having a leak papered over.
    """
    _require_aware(as_of)
    offenders = [item for item in items if not item.known_at(as_of)]
    if offenders:
        raise LookAheadError(
            f"{len(offenders)} record(s) were not knowable at {as_of.isoformat()}: "
            f"{[_describe(item) for item in offenders[:5]]}"
        )


def latest_as_of[ItemT: PointInTime](items: Sequence[ItemT], as_of: datetime) -> ItemT | None:
    """The most recent item that was already known at ``as_of``.

    "Most recent" is judged by the instant the item describes, not by when we
    fetched it: given two snapshots visible at T, the one describing the later
    market state is the better answer.
    """
    candidates = visible_at(items, as_of)
    if not candidates:
        return None
    return max(candidates, key=_ordering_key)


def _ordering_key(item: PointInTime) -> tuple[datetime, datetime]:
    """Sort by described instant, breaking ties by retrieval time."""
    as_of = getattr(item, "as_of", None)
    source = getattr(item, "source", None)
    retrieved = getattr(item, "retrieved_at", None) or getattr(source, "retrieved_at", None)
    if not isinstance(as_of, datetime):  # pragma: no cover - defensive
        raise TypeError(f"{type(item).__name__} has no comparable as_of")
    return (as_of, retrieved if isinstance(retrieved, datetime) else as_of)


def _describe(item: PointInTime) -> str:
    if isinstance(item, DataSnapshot):
        return f"{item.data_type.value}/{item.key}@{item.as_of.isoformat()}"
    if isinstance(item, DataRecord):
        return f"{item.data_type.value}@{item.as_of.isoformat()}"
    return repr(item)


def _require_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise LookAheadError(
            "point-in-time queries require a timezone-aware instant; a naive "
            "datetime has no defined position on the timeline"
        )
