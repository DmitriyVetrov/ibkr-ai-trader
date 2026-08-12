"""Reading a point-in-time option chain out of the repository.

The contract selector needs *candidates*: concrete contracts, each with
whatever quote data the store actually holds for it. Assembling them is
separated from choosing between them so that the choosing stays a pure function
of its inputs — which is what makes the determinism claim testable rather than
asserted.

Four rules govern what comes out of here:

* **The repository only.** No provider, no broker, no path, no network. The
  data layer collects; this reads. Milestone 2's one-reliable-round-trip
  constraint is therefore not merely respected, it is unreachable: there is no
  code path from a contract selection to a live request.
* **Point in time.** Chain and quotes are read through ``get_as_of``, and every
  record that survives is re-checked with ``assert_no_look_ahead``. A quote
  retrieved after ``as_of`` is invisible; an *expiration* after ``as_of`` is
  entirely normal and is not look-ahead.
* **A candidate belongs to the chain.** A quote whose contract is not in the
  chain snapshot's expirations and strikes, or whose trading class differs
  from the chain's, is not a candidate for this selection. Milestone 2 found
  ``SMART/SPY`` and ``SMART/2SPY`` coexisting; a contract assembled across two
  chains is a contract that may not exist.
* **Nothing is filled in.** A missing bid stays missing. A candidate with no
  quote is a candidate with no quote, and the selector decides what that means
  under the configured policy — it never becomes a zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trading_system.data.models import MarketQuote, OptionChain, OptionContract, OptionQuote
from trading_system.data.point_in_time import assert_no_look_ahead
from trading_system.data.repository import DataRepository, records_of
from trading_system.domain.enums import DataType, OptionRight
from trading_system.infrastructure.settings import ContractSelectionConfig, ReferencePriceField

__all__ = [
    "ChainReader",
    "ChainView",
    "ContractCandidate",
    "ReferencePrice",
]


@dataclass(frozen=True, slots=True)
class ContractCandidate:
    """One contract the store knows about, with whatever data it carries.

    ``quote`` is ``None`` when the chain enumerated the contract but no quote
    was ever collected for it — the ordinary situation today, since Milestone 3
    deliberately deferred per-contract quote collection. That is a fact about
    our data, and the selector reports it as one.
    """

    contract: OptionContract
    chain_snapshot_id: str
    quote: OptionQuote | None = None
    quote_snapshot_id: str | None = None

    # --- identity ----------------------------------------------------------
    @property
    def expiration(self) -> date | None:
        return self.contract.expiration

    @property
    def strike(self) -> Decimal | None:
        return self.contract.strike

    @property
    def right(self) -> OptionRight | None:
        return self.contract.right

    @property
    def contract_id(self) -> int | None:
        return self.contract.contract_id

    @property
    def trading_class(self) -> str | None:
        return self.contract.trading_class

    @property
    def multiplier(self) -> int | None:
        return self.contract.multiplier

    # --- market data -------------------------------------------------------
    @property
    def has_quote(self) -> bool:
        return self.quote is not None and self.quote.has_price

    @property
    def quote_as_of(self) -> datetime | None:
        return self.quote.as_of if self.quote is not None else None

    @property
    def research_usable(self) -> bool:
        """The data layer's verdict, carried through and never re-judged."""
        return self.quote is None or self.quote.quality.research_usable

    @property
    def bid(self) -> Decimal | None:
        return self.quote.bid if self.quote else None

    @property
    def ask(self) -> Decimal | None:
        return self.quote.ask if self.quote else None

    @property
    def last(self) -> Decimal | None:
        return self.quote.last if self.quote else None

    @property
    def implied_volatility(self) -> Decimal | None:
        return self.quote.implied_volatility if self.quote else None

    @property
    def delta(self) -> Decimal | None:
        return self.quote.delta if self.quote else None

    @property
    def volume(self) -> Decimal | None:
        return self.quote.volume if self.quote else None

    @property
    def open_interest(self) -> Decimal | None:
        return self.quote.open_interest if self.quote else None

    @property
    def mid(self) -> Decimal | None:
        """Midpoint, or ``None`` when either side is missing. Never derived
        from one side: half a quote is not a midpoint."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * Decimal(100)

    def sort_key(self) -> tuple[str, Decimal, str, int]:
        """A total, stable order over candidates.

        Every tie-break the selector performs ends here, which is what makes
        two runs over the same data choose the same contract rather than
        whichever the filesystem happened to yield first.
        """
        return (
            self.expiration.isoformat() if self.expiration else "",
            self.strike if self.strike is not None else Decimal(0),
            self.right.value if self.right else "",
            self.contract_id or 0,
        )


@dataclass(frozen=True, slots=True)
class ReferencePrice:
    """The underlying price a strike policy is measured against, and its source.

    Recorded rather than assumed: a strike chosen against "the price" cannot be
    audited when three fields disagree about what the price was.
    """

    value: Decimal
    field: str
    snapshot_id: str | None = None
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChainView:
    """Everything the selector may see about one underlying at one instant."""

    symbol: str
    as_of: datetime
    chain: OptionChain
    chain_snapshot_id: str
    candidates: tuple[ContractCandidate, ...]
    reference: ReferencePrice | None
    snapshot_ids: tuple[str, ...]

    @property
    def expirations(self) -> tuple[date, ...]:
        return tuple(self.chain.expirations)

    @property
    def trading_class(self) -> str | None:
        return self.chain.trading_class

    @property
    def multiplier(self) -> int | None:
        return self.chain.multiplier

    @property
    def quoted_candidates(self) -> tuple[ContractCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.has_quote)

    def for_expiration(self, expiration: date) -> tuple[ContractCandidate, ...]:
        return tuple(c for c in self.candidates if c.expiration == expiration)


class ChainReader:
    """Assembles a :class:`ChainView` from stored, point-in-time data."""

    def __init__(self, repository: DataRepository, *, config: ContractSelectionConfig) -> None:
        self._repository = repository
        self._config = config

    def read(self, symbol: str, as_of: datetime) -> ChainView | None:
        """The chain and its candidates as of ``as_of``, or ``None``.

        ``None`` means no option chain was visible at that instant — not that
        the underlying has no options. The caller reports the difference.
        """
        key = symbol.strip().upper()
        chain_snapshot = self._repository.get_as_of(DataType.OPTION_CHAIN, key, as_of)
        if chain_snapshot is None:
            return None
        chains = records_of(chain_snapshot, OptionChain)
        assert_no_look_ahead(chains, as_of)
        if not chains:
            return None
        chain = max(chains, key=lambda c: (c.as_of, c.source.retrieved_at))

        quotes: list[OptionQuote] = []
        quote_snapshot_id: str | None = None
        quote_snapshot = self._repository.get_as_of(DataType.OPTION_QUOTE, key, as_of)
        if quote_snapshot is not None:
            quotes = records_of(quote_snapshot, OptionQuote)
            assert_no_look_ahead(quotes, as_of)
            quote_snapshot_id = quote_snapshot.snapshot_id if quotes else None

        candidates = _merge(
            chain=chain,
            chain_snapshot_id=chain_snapshot.snapshot_id,
            quotes=quotes,
            quote_snapshot_id=quote_snapshot_id,
        )
        reference = self._reference_price(key, as_of, candidates)

        snapshot_ids = {chain_snapshot.snapshot_id}
        if quote_snapshot_id:
            snapshot_ids.add(quote_snapshot_id)
        if reference is not None and reference.snapshot_id:
            snapshot_ids.add(reference.snapshot_id)

        return ChainView(
            symbol=key,
            as_of=as_of,
            chain=chain,
            chain_snapshot_id=chain_snapshot.snapshot_id,
            candidates=tuple(sorted(candidates, key=lambda c: c.sort_key())),
            reference=reference,
            snapshot_ids=tuple(sorted(snapshot_ids)),
        )

    # --- reference price ---------------------------------------------------
    def _reference_price(
        self, symbol: str, as_of: datetime, candidates: Iterable[ContractCandidate]
    ) -> ReferencePrice | None:
        """The underlying price, from the configured fields in order.

        Falls back to an option quote's own ``underlying_price`` only when
        configuration allows it, and records that it did. Never invented: with
        no price from either source the strike policies cannot be applied, and
        the selection fails rather than guessing at the money.
        """
        snapshot = self._repository.get_as_of(DataType.MARKET_QUOTE, symbol, as_of)
        if snapshot is not None:
            quotes = records_of(snapshot, MarketQuote)
            assert_no_look_ahead(quotes, as_of)
            if quotes:
                quote = max(quotes, key=lambda q: (q.as_of, q.source.retrieved_at))
                for field in self._config.strike.reference_price_fields:
                    value = _quote_field(quote, field)
                    if value is not None and value > 0:
                        return ReferencePrice(
                            value=value,
                            field=field.value,
                            snapshot_id=snapshot.snapshot_id,
                            as_of=quote.as_of,
                        )

        if not self._config.strike.allow_option_underlying_price:
            return None
        for candidate in sorted(candidates, key=lambda c: c.sort_key()):
            option_quote = candidate.quote
            if option_quote is None:
                continue
            underlying_price = option_quote.underlying_price
            if underlying_price is not None and underlying_price > 0:
                return ReferencePrice(
                    value=underlying_price,
                    field="OPTION_UNDERLYING_PRICE",
                    snapshot_id=candidate.quote_snapshot_id,
                    as_of=option_quote.as_of,
                )
        return None


def _quote_field(quote: MarketQuote, field: ReferencePriceField) -> Decimal | None:
    match field:
        case ReferencePriceField.LAST:
            return quote.last
        case ReferencePriceField.CLOSE:
            return quote.close
        case ReferencePriceField.MID:
            return quote.mid
        case ReferencePriceField.BID:
            return quote.bid
        case ReferencePriceField.ASK:
            return quote.ask
    return None


def _merge(
    *,
    chain: OptionChain,
    chain_snapshot_id: str,
    quotes: list[OptionQuote],
    quote_snapshot_id: str | None,
) -> list[ContractCandidate]:
    """Union the chain's contracts with the quotes, keyed by identity.

    A contract known from both sources keeps the quote's own contract record:
    it is the one the quote was actually taken against, and pairing a quote with
    a different provider's idea of the same contract is how a contract id and a
    price come to disagree.

    Anything the chain snapshot does not contain is dropped here rather than
    rejected later, because it is not a candidate *for this chain* at all — a
    quote from a different trading class describes a different instrument.
    """
    expirations = set(chain.expirations)
    strikes = set(chain.strikes)
    trading_class = (chain.trading_class or "").strip().upper()

    def belongs(contract: OptionContract) -> bool:
        if contract.underlying.upper() != chain.underlying.upper():
            return False
        if contract.expiration is None or contract.strike is None or contract.right is None:
            # Incompletely identified contracts are kept: the selector rejects
            # them by name (MISSING_STRIKE, MISSING_EXPIRATION), which is more
            # informative than their silent absence.
            return True
        if expirations and contract.expiration not in expirations:
            return False
        if strikes and contract.strike not in strikes:
            return False
        candidate_class = (contract.trading_class or "").strip().upper()
        return not (trading_class and candidate_class and candidate_class != trading_class)

    merged: dict[tuple[str, str, str], ContractCandidate] = {}
    for contract in chain.contracts:
        if not belongs(contract):
            continue
        merged[_identity(contract)] = ContractCandidate(
            contract=contract, chain_snapshot_id=chain_snapshot_id
        )
    for quote in quotes:
        if not belongs(quote.contract):
            continue
        merged[_identity(quote.contract)] = ContractCandidate(
            contract=quote.contract,
            chain_snapshot_id=chain_snapshot_id,
            quote=quote,
            quote_snapshot_id=quote_snapshot_id,
        )
    return list(merged.values())


def _identity(contract: OptionContract) -> tuple[str, str, str]:
    return (
        contract.expiration.isoformat() if contract.expiration else "",
        str(contract.strike) if contract.strike is not None else "",
        contract.right.value if contract.right else "",
    )
