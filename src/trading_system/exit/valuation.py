"""What the position is worth right now, and where that number came from.

Two halves, deliberately separated:

* :class:`ExitQuoteReader` reads stored, point-in-time option quotes through
  :class:`~trading_system.data.repository.DataRepository`. No provider, no
  broker, no path, no network — the data layer collects, this reads, and there
  is no code path from an exit evaluation to a live market request. That is not
  merely tidy: Milestone 2 measured that only the first uncached round trip on
  a TWS connection is reliably answered, and a monitor that fetched a quote per
  position per tick is exactly the shape that constraint forbids.
* :func:`value_position` is a pure function over the quotes that were read. Same
  quotes, same legs, same field, same answer — which is what makes a stored
  valuation reproducible.

Deliberately narrower than :class:`~trading_system.strategies.chain.ChainReader`,
which assembles a whole chain because Milestone 6 has to *choose* from it. Here
the contract is already known: the position holds specific broker contract ids,
and the only question is what those are quoted at. Reading a chain to answer it
would drag the strike policy, the reference price and the trading-class merge
into a stage that needs none of them.

**Nothing is filled in.** The configured field is read and no other field
stands in for it. A missing bid does not become the ask, the last print, the
midpoint or the price we paid; it becomes ``None``, the leg is unpriced, and
the structure is unpriced with it. Half a straddle is a directional bet, so a
structure valued from the legs that happened to be quoted would be a valuation
of a different position.

**Point in time.** Quotes are read through ``get_as_of`` and every surviving
record is re-checked with ``assert_no_look_ahead``. A quote retrieved after the
evaluation instant is invisible to it, however recent the price it describes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.data.models import OptionQuote
from trading_system.data.point_in_time import LookAheadError, assert_no_look_ahead
from trading_system.data.repository import DataRepository, records_of
from trading_system.domain.enums import DataType, ExitQuoteField
from trading_system.exit.models import ExitLegValuation, PositionValuation

__all__ = [
    "ExitQuoteReader",
    "HeldLeg",
    "QuoteLookup",
    "quote_field_value",
    "value_position",
]


def quote_field_value(quote: OptionQuote, field: ExitQuoteField) -> Decimal | None:
    """The configured field's value, or ``None``.

    ``MID`` is the only computed member and it needs *both* sides: half a quote
    is not a midpoint, and deriving one from a single side would invent a price
    the other half of the market never showed.
    """
    match field:
        case ExitQuoteField.BID:
            return quote.bid
        case ExitQuoteField.ASK:
            return quote.ask
        case ExitQuoteField.LAST:
            return quote.last
        case ExitQuoteField.MID:
            return quote.mid
    return None  # pragma: no cover - the match is exhaustive over the enum


@dataclass(frozen=True, slots=True)
class HeldLeg:
    """One leg of the structure, as the position ledger reports it.

    Deliberately duck-typed input rather than a model: the caller assembles it
    from an execution record's legs and a broker snapshot, and this module has
    no business knowing which of those supplied which field.
    """

    leg_index: int
    key: str
    contract_id: int | None
    underlying: str
    right: object | None = None
    strike: Decimal | None = None
    expiration: object | None = None
    ratio: int = 1
    multiplier: int | None = None
    observed_quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class QuoteLookup:
    """Every quote visible for one underlying at one instant, keyed for lookup.

    Keyed by broker contract id where there is one, and by the human-readable
    terms only where there is not — the same identity rule the position ledger
    uses, and for the same reason: adjusted option contracts share symbol,
    strike, expiry and right, so a lookup keyed on those alone would eventually
    price a position from a different instrument's quote.
    """

    as_of: datetime
    snapshot_id: str | None = None
    by_contract_id: dict[int, OptionQuote] | None = None
    by_terms: dict[tuple[str, str, str], OptionQuote] | None = None

    def find(self, leg: HeldLeg) -> OptionQuote | None:
        if leg.contract_id is not None and self.by_contract_id:
            found = self.by_contract_id.get(leg.contract_id)
            if found is not None:
                return found
        if not self.by_terms:
            return None
        expiration = getattr(leg.expiration, "isoformat", lambda: "")()
        right = getattr(leg.right, "value", "") or ""
        strike = str(leg.strike) if leg.strike is not None else ""
        return self.by_terms.get((expiration, strike, right))


class ExitQuoteReader:
    """Reads the stored option quotes an exit evaluation may see."""

    def __init__(self, repository: DataRepository) -> None:
        self._repository = repository

    def read(self, symbol: str, as_of: datetime) -> QuoteLookup:
        """Every option quote for ``symbol`` visible at ``as_of``.

        Returns an empty lookup rather than raising when nothing is stored:
        "no quote was collected" is an ordinary fact about our data, and the
        data-quality policy decides what it means for a decision.

        Raises :class:`~trading_system.data.point_in_time.LookAheadError` when a
        stored record could not have been known at ``as_of``. That is a
        correctness bug in storage rather than a market outcome, and the caller
        turns it into ``POINT_IN_TIME_ERROR`` — never into a decision.
        """
        key = symbol.strip().upper()
        snapshot = self._repository.get_as_of(DataType.OPTION_QUOTE, key, as_of)
        if snapshot is None:
            return QuoteLookup(as_of=as_of)
        quotes = records_of(snapshot, OptionQuote)
        assert_no_look_ahead(quotes, as_of)

        by_id: dict[int, OptionQuote] = {}
        by_terms: dict[tuple[str, str, str], OptionQuote] = {}
        for quote in quotes:
            contract = quote.contract
            if contract.contract_id is not None:
                existing = by_id.get(contract.contract_id)
                if existing is None or _newer(quote, existing):
                    by_id[contract.contract_id] = quote
            terms = (
                contract.expiration.isoformat() if contract.expiration else "",
                str(contract.strike) if contract.strike is not None else "",
                contract.right.value if contract.right else "",
            )
            existing_terms = by_terms.get(terms)
            if existing_terms is None or _newer(quote, existing_terms):
                by_terms[terms] = quote
        return QuoteLookup(
            as_of=as_of,
            snapshot_id=snapshot.snapshot_id if quotes else None,
            by_contract_id=by_id,
            by_terms=by_terms,
        )


def _newer(candidate: OptionQuote, existing: OptionQuote) -> bool:
    """Deterministic preference between two quotes for one contract."""
    return (candidate.as_of, candidate.source.retrieved_at) > (
        existing.as_of,
        existing.source.retrieved_at,
    )


def value_position(
    legs: Sequence[HeldLeg],
    *,
    lookup: QuoteLookup,
    as_of: datetime,
    quote_field: ExitQuoteField,
    open_quantity: int,
    entry_quote: Decimal | None,
    multiplier: int | None,
    currency: str | None = None,
    require_research_usable: bool = True,
) -> PositionValuation:
    """Price the whole structure at the configured field. Pure.

    The structure's exit quote is ``sum(leg price x leg ratio)`` — the net a
    seller would receive for one unit, in the broker's quoted terms, which is
    the same basis the entry's combo fill price was reported in. Multiplying by
    the shared multiplier gives money for one unit, and by the open quantity
    gives money for the holding. The three are named apart on the record so no
    caller has to remember which is which.

    Every derived figure is ``None`` when any leg is unpriced, and the unpriced
    legs are named. ``require_research_usable`` treats the data layer's own
    verdict as disqualifying for the *price* rather than re-grading it here:
    quality is Milestone 3's judgement and this stage consumes it.
    """
    valued: list[ExitLegValuation] = []
    unpriced: list[int] = []
    ages: list[float] = []

    for leg in legs:
        quote = lookup.find(leg)
        price: Decimal | None = None
        detail: str | None = None
        usable: bool | None = None
        age: float | None = None

        if quote is None:
            detail = (
                f"no stored {quote_field.value} quote for contract {leg.contract_id} was "
                f"visible at {as_of.isoformat()}"
            )
        else:
            usable = quote.quality.research_usable
            # The provenance's own age, which prefers the venue's clock over
            # ours and falls back to retrieval only where the provider stamped
            # nothing. Freshness is a claim about the market, not about when we
            # happened to save a file.
            age = quote.source.age_seconds(as_of)
            ages.append(age)
            raw = quote_field_value(quote, quote_field)
            if raw is None:
                detail = (
                    f"the quote carries no {quote_field.value}. No other field is substituted "
                    f"for it: a position valued at a price no seller could get is worse than "
                    f"an unpriced one"
                )
            elif raw <= 0:
                detail = (
                    f"{quote_field.value} is {raw}, which is not a price this structure could "
                    f"be sold at"
                )
            elif require_research_usable and not usable:
                detail = (
                    "the data layer judged this quote unusable for research; its verdict is "
                    "consumed rather than re-graded here"
                )
            else:
                price = raw

        if price is None:
            unpriced.append(leg.leg_index)

        valued.append(
            ExitLegValuation(
                leg_index=leg.leg_index,
                contract_id=leg.contract_id,
                key=leg.key,
                right=leg.right,
                strike=leg.strike,
                expiration=leg.expiration,
                ratio=leg.ratio,
                multiplier=leg.multiplier,
                observed_quantity=leg.observed_quantity,
                quote_field=quote_field,
                price=price,
                bid=quote.bid if quote else None,
                ask=quote.ask if quote else None,
                last=quote.last if quote else None,
                quote_snapshot_id=lookup.snapshot_id if quote else None,
                quote_as_of=quote.as_of if quote else None,
                quote_retrieved_at=quote.source.retrieved_at if quote else None,
                provider=quote.source.provider if quote else None,
                data_quality=quote.quality.classification if quote else None,
                research_usable=usable,
                quote_age_seconds=age,
                detail=detail,
            )
        )

    exit_quote: Decimal | None = None
    if valued and not unpriced:
        exit_quote = sum(
            (
                (leg.price or Decimal("0")) * Decimal(leg.ratio)
                for leg in valued
                if leg.price is not None
            ),
            Decimal("0"),
        )

    exit_value = (
        exit_quote * Decimal(multiplier)
        if exit_quote is not None and multiplier is not None
        else None
    )
    entry_cost = (
        entry_quote * Decimal(multiplier)
        if entry_quote is not None and multiplier is not None
        else None
    )

    return PositionValuation(
        as_of=as_of,
        quote_field=quote_field,
        multiplier=multiplier,
        open_quantity=open_quantity,
        currency=currency,
        legs=valued,
        entry_quote=entry_quote,
        entry_cost=entry_cost,
        exit_quote=exit_quote,
        exit_value=exit_value,
        max_quote_age_seconds=max(ages) if ages else None,
        unpriced_legs=unpriced,
        detail=(
            None
            if not unpriced
            else (
                f"legs {unpriced} carry no usable {quote_field.value}; the structure is "
                f"unpriced rather than priced from the legs that do"
            )
        ),
    )


def read_and_value(
    reader: ExitQuoteReader,
    legs: Sequence[HeldLeg],
    *,
    symbol: str,
    as_of: datetime,
    quote_field: ExitQuoteField,
    open_quantity: int,
    entry_quote: Decimal | None,
    multiplier: int | None,
    currency: str | None = None,
    require_research_usable: bool = True,
) -> tuple[PositionValuation | None, str | None]:
    """Read and price in one call, turning a look-ahead leak into a message.

    Returns ``(None, detail)`` when a stored record was not knowable at
    ``as_of``. The caller records ``POINT_IN_TIME_ERROR`` and blocks: a leak is
    a correctness bug in storage, and a decision made from leaked data would
    look exactly like a good one.
    """
    try:
        lookup = reader.read(symbol, as_of)
    except LookAheadError as exc:
        return None, (
            f"a stored option quote for {symbol} was not knowable at {as_of.isoformat()}: "
            f"{exc}. No exit decision is made from it"
        )
    return (
        value_position(
            legs,
            lookup=lookup,
            as_of=as_of,
            quote_field=quote_field,
            open_quantity=open_quantity,
            entry_quote=entry_quote,
            multiplier=multiplier,
            currency=currency,
            require_research_usable=require_research_usable,
        ),
        None,
    )
