"""Gathering the evidence a universe decision rests on.

This module is the only place the universe layer touches stored data, and it
reaches it through :class:`~trading_system.data.repository.DataRepository` —
never a provider, never a broker, never a path. The agent sits two layers above
this and sees only what :mod:`trading_system.universe.filters` hands it.

Everything is read **as of** an instant, through
:meth:`~trading_system.data.repository.DataRepository.get_as_of`, which decides
visibility from the append-only ledger's timestamps. A snapshot retrieved after
that instant is never loaded, let alone read: retrieval binds, so a chain
downloaded on 15 August cannot make an underlying look optionable on the 10th.
:func:`~trading_system.data.point_in_time.assert_no_look_ahead` re-checks the
records themselves rather than trusting that filtering worked.

No value is derived that the data layer did not supply. There is no computed
volatility, no synthetic liquidity score, no imputed price — an absent fact
stays absent, and the filters treat "unavailable" as its own outcome rather
than as a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trading_system.data.models import (
    DataQualityReport,
    DataSnapshot,
    MarketQuote,
    OptionChain,
)
from trading_system.data.point_in_time import assert_no_look_ahead
from trading_system.data.repository import DataRepository, records_of
from trading_system.domain.enums import DataType, Optionability
from trading_system.domain.models import Money
from trading_system.universe.models import PriceField

__all__ = ["AssetEvidence", "EvidenceGatherer", "reference_price"]


@dataclass(frozen=True, slots=True)
class AssetEvidence:
    """Everything known about one underlying at one instant.

    A deliberately flat record of *facts*, with no verdict attached. The
    verdict is :mod:`trading_system.universe.filters`' job, and keeping the two
    apart is what lets the filters be tested without a repository and the
    gathering be tested without a policy.
    """

    symbol: str
    quote: MarketQuote | None = None
    chain: OptionChain | None = None
    snapshot_ids: tuple[str, ...] = ()
    #: Set when nothing was visible at the requested instant.
    missing_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_quote(self) -> bool:
        return self.quote is not None

    @property
    def optionability(self) -> Optionability:
        """Whether the underlying has listed options, as far as we can tell.

        Three states, and the third is never collapsed into the second:

        * a chain snapshot visible at the instant, with expirations — ``TRUE``;
        * a chain snapshot visible at the instant, with none — ``FALSE``, which
          is a provider answering that nothing is listed;
        * no chain snapshot visible at all — ``UNKNOWN``. We have not looked, or
          we looked and got no answer. That is not evidence of absence, and
          reading it as ``FALSE`` would quietly delete every underlying whose
          chain has simply never been collected.
        """
        if self.chain is None:
            return Optionability.UNKNOWN
        return Optionability.TRUE if self.chain.expirations else Optionability.FALSE

    @property
    def quality(self) -> DataQualityReport:
        """The quality verdict on the quote, or an empty report when absent."""
        return self.quote.quality if self.quote is not None else DataQualityReport()

    def age_seconds(self, instant: datetime) -> float | None:
        if self.quote is None:
            return None
        return self.quote.source.age_seconds(instant)


def reference_price(quote: MarketQuote) -> tuple[Money | None, str | None]:
    """Pick the price a threshold is compared against, and say which one it is.

    Preference order is last, close, midpoint, bid, ask: a traded price beats a
    derived one, and a midpoint beats a single side. The field name travels with
    the value because "below the minimum price" is not auditable if nobody can
    tell afterwards which number was compared. Returns ``(None, None)`` when the
    quote carries no price at all — a real and common outcome outside market
    hours, and not the same as a price of zero.
    """
    if quote.last is not None:
        return quote.last, PriceField.LAST
    if quote.close is not None:
        return quote.close, PriceField.CLOSE
    mid = quote.mid
    if mid is not None:
        return mid, PriceField.MID
    if quote.bid is not None:
        return quote.bid, PriceField.BID
    if quote.ask is not None:
        return quote.ask, PriceField.ASK
    return None, None


class EvidenceGatherer:
    """Reads point-in-time evidence for a list of symbols.

    Holds a repository and nothing else. It cannot collect, cannot connect and
    cannot reach a provider: a universe run is a pure consumer of history that
    was already accumulated, so it can be replayed for a past instant and can
    never place an order.
    """

    def __init__(self, repository: DataRepository) -> None:
        self._repository = repository

    def gather(self, symbol: str, as_of: datetime) -> AssetEvidence:
        """Everything visible about ``symbol`` at ``as_of``."""
        key = symbol.upper()
        snapshot_ids: list[str] = []
        notes: list[str] = []

        quote_snapshot = self._repository.get_as_of(DataType.MARKET_QUOTE, key, as_of)
        quote = self._newest_quote(quote_snapshot, key, as_of)
        if quote_snapshot is not None:
            snapshot_ids.append(quote_snapshot.snapshot_id)

        chain_snapshot = self._repository.get_as_of(DataType.OPTION_CHAIN, key, as_of)
        chain = self._chain(chain_snapshot, as_of)
        if chain_snapshot is not None:
            snapshot_ids.append(chain_snapshot.snapshot_id)
        else:
            notes.append("no option chain snapshot was visible at this instant")

        missing = None
        if quote is None:
            missing = (
                "no market quote snapshot was visible at this instant"
                if quote_snapshot is None
                else "the visible market quote snapshot contained no usable record"
            )

        return AssetEvidence(
            symbol=key,
            quote=quote,
            chain=chain,
            snapshot_ids=tuple(snapshot_ids),
            missing_reason=missing,
            notes=tuple(notes),
        )

    def gather_all(self, symbols: list[str], as_of: datetime) -> dict[str, AssetEvidence]:
        return {symbol.upper(): self.gather(symbol, as_of) for symbol in symbols}

    # --- internals ---------------------------------------------------------
    @staticmethod
    def _newest_quote(
        snapshot: DataSnapshot | None, key: str, as_of: datetime
    ) -> MarketQuote | None:
        """The quote for ``key`` in a snapshot, re-checked for look-ahead.

        The ledger has already decided the snapshot was visible; this verifies
        the records inside it. A leak here is a correctness bug in storage, so
        it raises rather than being quietly filtered — a shortened list would
        look like an ordinary "no data" outcome.
        """
        if snapshot is None:
            return None
        records = [q for q in records_of(snapshot, MarketQuote) if q.symbol.upper() == key]
        if not records:
            return None
        assert_no_look_ahead(records, as_of)
        return max(records, key=lambda q: (q.as_of, q.source.retrieved_at))

    @staticmethod
    def _chain(snapshot: DataSnapshot | None, as_of: datetime) -> OptionChain | None:
        if snapshot is None:
            return None
        records = records_of(snapshot, OptionChain)
        if not records:
            return None
        assert_no_look_ahead(records, as_of)
        return max(records, key=lambda c: (c.as_of, c.source.retrieved_at))
