"""Realised profit and loss from broker-confirmed fills. Pure and deterministic.

Nothing here opens a connection, reads a clock or writes a file. Every input is
an argument and every output is a value, which is what makes a stored result
reproducible from the stored fills long after the configuration, the market and
the model have all moved on.

.. code-block:: text

    confirmed BUY fills   ->  what the account actually paid
    confirmed SELL fills  ->  what the account actually received
          |
    per-leg matching          min(opened, closed) units, average cost
          |
    structure total           one number for one trade
          |
    RealizedPnL               COMPLETE / PARTIAL / NOT_AVAILABLE

Five rules govern it:

* **Only a fill counts.** The authorisation's price, the limit price, the
  reference quote and the last thing the market printed are all irrelevant
  here. If no confirmed fill establishes a side, that side has no figure.
* **Nothing is assumed.** A missing multiplier is not 100. A missing commission
  is not zero. A cross-currency pair is not converted. Each is a named reason
  code and, where it matters, a ``NOT_AVAILABLE`` result.
* **A structure is one trade.** The total is over the whole structure; the legs
  are reported because they explain it, never as results of their own.
* **Matching is explicit.** Only ``min(opened, closed)`` units per leg are
  matched, and where they differ the entry cost is prorated — exactly, from
  the average price the account actually paid, quantised once to the currency's
  precision and flagged as prorated. A structure that closed four of ten units
  has a real result over four units and no opinion about the other six.
* **Gross and net are separate.** The fill prices support a gross figure
  immediately; commissions frequently arrive later. Reporting a net figure
  built from a zero-filled commission would understate the cost of every trade
  the feed was slow about.

**Units.** ``price`` on a fill is the broker's *quoted* terms (6.05). Money is
``price x quantity x multiplier`` (605.00 for one contract). The multiplication
happens here, once per fill, against the multiplier the broker itself reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from trading_system.domain.enums import (
    CommissionStatus,
    OptionRight,
    OrderSide,
    PnLReasonCode,
    PnLStatus,
    StrategyType,
)
from trading_system.domain.models import SystemVersions
from trading_system.pnl.models import (
    RealizedPnL,
    RealizedPnLLeg,
    realized_pnl_identifier,
)

__all__ = [
    "FillLike",
    "PnLCalculator",
    "PnLInputs",
    "session_date_of",
]


class FillLike(Protocol):
    """The shape a confirmed broker fill has to have to be counted.

    Duck-typed rather than importing
    :class:`~trading_system.positions.models.ObservedFill`, for the same reason
    the Milestone 2 translation modules are duck-typed: it keeps this module a
    function of plain values, testable without constructing a position ledger.

    Declared as read-only properties rather than plain attributes, deliberately.
    The concrete fill is an immutable model whose fields are read-only, and a
    protocol with *mutable* attributes would refuse it — the right refusal
    expressed the wrong way round. Nothing here writes to a fill, and the
    protocol says so.
    """

    @property
    def fill_id(self) -> str: ...

    @property
    def key(self) -> str: ...

    @property
    def side(self) -> OrderSide: ...

    @property
    def quantity(self) -> Decimal: ...

    @property
    def price(self) -> Decimal: ...

    @property
    def commission(self) -> Decimal | None: ...

    @property
    def multiplier(self) -> int | None: ...

    @property
    def currency(self) -> str | None: ...

    @property
    def executed_at(self) -> datetime: ...

    @property
    def contract_id(self) -> int | None: ...

    @property
    def right(self) -> OptionRight | None: ...

    @property
    def strike(self) -> Decimal | None: ...

    @property
    def expiration(self) -> date | None: ...

    @property
    def execution_id(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PnLInputs:
    """Everything one realised result is computed from.

    Every field is *captured state*. There is no repository, no clock and no
    broker here, which is what makes the result a pure function of what the
    account actually did.
    """

    position_id: str
    campaign_id: str
    underlying: str
    strategy: StrategyType
    #: Confirmed fills belonging to the entry execution.
    entry_fills: Sequence[FillLike]
    #: Confirmed fills belonging to every exit execution for this position.
    exit_fills: Sequence[FillLike]
    computed_at: datetime
    day_boundary_timezone: str = "America/New_York"
    currency_precision: int = 2
    require_commission_for_net: bool = True
    #: Whether an execution against this position is unresolved. A result over
    #: fills that may yet be joined by more is recorded as such.
    execution_unknown: bool = False
    #: When Milestone 10's lifecycle says the position closed. Used for the
    #: *session attribution* only, and only when the exit fills do not supply
    #: one — which is exactly the ``NOT_AVAILABLE`` case, where there are no
    #: exit fills at all.
    #:
    #: Without it, a position that closed today and produced no usable figure
    #: would belong to no trading day, and the day's roll-up would report
    #: NOT_TRACKED — "nothing happened" — for a day on which a position closed
    #: and the result could not be computed. That is the single most misleading
    #: thing the daily loss limit could be told.
    position_closed_at: datetime | None = None

    entry_execution_id: str | None = None
    exit_execution_ids: Sequence[str] = ()
    allocation_id: str | None = None
    opportunity_id: str | None = None
    reservation_id: str | None = None
    research_report_id: str | None = None
    contract_selection_id: str | None = None
    account_reference: str | None = None
    broker_source: str = "UNKNOWN"
    versions: SystemVersions | None = None


@dataclass(slots=True)
class _LegAccumulator:
    """One contract's confirmed fills, on both sides, before matching."""

    key: str
    leg_index: int
    contract_id: int | None = None
    right: OptionRight | None = None
    strike: Decimal | None = None
    expiration: date | None = None
    multiplier: int | None = None
    currency: str | None = None

    opened_quantity: Decimal = Decimal("0")
    closed_quantity: Decimal = Decimal("0")
    entry_notional: Decimal = Decimal("0")
    exit_notional: Decimal = Decimal("0")
    entry_commission: Decimal = Decimal("0")
    exit_commission: Decimal = Decimal("0")
    entry_commission_known: bool = True
    exit_commission_known: bool = True
    multiplier_known: bool = True
    entry_fill_ids: list[str] = field(default_factory=list)
    exit_fill_ids: list[str] = field(default_factory=list)
    currencies: set[str] = field(default_factory=set)


def session_date_of(instant: datetime, timezone: str) -> date:
    """The exchange-local trading day one instant belongs to.

    A closure at 21:30 UTC belongs to the New York session that has just
    ended. Bounding the day in UTC would file it under tomorrow, and a losing
    afternoon would look like two quiet days to the daily loss limit.
    """
    return instant.astimezone(ZoneInfo(timezone)).date()


class PnLCalculator:
    """Computes one structure's realised result from confirmed fills.

    A class rather than a function only so the currency precision and the day
    boundary travel with it; there is no state, no clock and no I/O, and two
    calculators built from the same configuration produce byte-identical
    records for identical fills.
    """

    def __init__(self, *, currency_precision: int = 2, day_boundary_timezone: str = "UTC") -> None:
        self._precision = currency_precision
        self._timezone = day_boundary_timezone

    # --- the calculation ---------------------------------------------------
    def compute(self, inputs: PnLInputs) -> RealizedPnL:
        """The realised result for one structure, or an honest refusal."""
        precision = inputs.currency_precision or self._precision
        timezone = inputs.day_boundary_timezone or self._timezone

        legs = _accumulate(inputs.entry_fills, inputs.exit_fills)
        reasons: list[PnLReasonCode] = []
        currencies = {currency for leg in legs.values() for currency in leg.currencies}

        base: dict[str, Any] = dict(
            position_id=inputs.position_id,
            campaign_id=inputs.campaign_id,
            underlying=inputs.underlying,
            strategy=inputs.strategy,
            computed_at=inputs.computed_at,
            entry_execution_id=inputs.entry_execution_id,
            exit_execution_ids=sorted(inputs.exit_execution_ids),
            allocation_id=inputs.allocation_id,
            opportunity_id=inputs.opportunity_id,
            reservation_id=inputs.reservation_id,
            research_report_id=inputs.research_report_id,
            contract_selection_id=inputs.contract_selection_id,
            account_reference=inputs.account_reference,
            broker_source=inputs.broker_source,
            versions=inputs.versions,
            source_fill_ids=sorted(
                {fill.fill_id for fill in (*inputs.entry_fills, *inputs.exit_fills)}
            ),
            opened_at=_earliest(inputs.entry_fills),
            closed_at=_latest(inputs.exit_fills),
        )
        closed_at = base["closed_at"] or inputs.position_closed_at
        if isinstance(closed_at, datetime):
            base["session_date"] = session_date_of(closed_at, timezone)

        # --- the refusals, in the order they matter ------------------------
        if not inputs.entry_fills:
            return self._unavailable(
                base,
                [PnLReasonCode.ENTRY_FILLS_UNAVAILABLE],
                "no broker-confirmed fill establishes the entry, so what this position cost is "
                "not a known quantity. An authorisation, a submitted order and an "
                "acknowledgement all establish nothing about what was actually paid",
            )
        if not inputs.exit_fills:
            return self._unavailable(
                base,
                [PnLReasonCode.EXIT_FILLS_UNAVAILABLE],
                "no broker-confirmed fill establishes the exit. A position that is gone from "
                "the broker's report but has no closing fill behind it has a result nobody can "
                "compute, and inventing one from the last quote would be a marked-to-market "
                "estimate wearing a realised result's clothes",
            )
        if inputs.execution_unknown:
            return self._unavailable(
                base,
                [PnLReasonCode.EXECUTION_UNKNOWN],
                "an execution against this position is UNKNOWN: an order may be working at the "
                "broker right now, so more fills may yet arrive. What traded is not settled "
                "fact, and a result computed over part of it would be wrong in a direction "
                "nobody could predict",
            )
        if len(currencies) > 1:
            return self._unavailable(
                base,
                [PnLReasonCode.CURRENCY_MISMATCH],
                f"fills settled in {', '.join(sorted(currencies))} and no deterministic FX rate "
                f"source exists. A result converted at a guessed rate is a made-up number in "
                f"the one place this system has to be exact",
            )
        missing_multiplier = sorted(key for key, leg in legs.items() if not leg.multiplier_known)
        if missing_multiplier:
            return self._unavailable(
                base,
                [PnLReasonCode.MULTIPLIER_UNAVAILABLE],
                f"the broker reported no contract multiplier for {', '.join(missing_multiplier)}. "
                f"A standard US equity option is 100 and assuming so would misprice the first "
                f"one that is not — silently, in the figure the daily loss limit reads",
            )
        unmatched = sorted(
            key for key, leg in legs.items() if leg.opened_quantity == 0 or leg.closed_quantity == 0
        )
        if unmatched:
            return self._unavailable(
                base,
                [PnLReasonCode.UNMATCHED_LEG],
                f"{', '.join(unmatched)} has fills on only one side. A leg that was sold and "
                f"never bought, or bought and never sold, is a ledger fault rather than a "
                f"market outcome",
            )

        # --- the arithmetic ------------------------------------------------
        currency = next(iter(currencies), None)
        computed = [
            _match_leg(leg, precision=precision, currency=currency)
            for _, leg in sorted(legs.items(), key=lambda item: item[1].leg_index)
        ]
        prorated = any(record.prorated for record in computed)
        if prorated:
            reasons.append(PnLReasonCode.ENTRY_COST_PRORATED)

        leg_models = [record.leg for record in computed]
        entry_cost = sum((leg.entry_cost or Decimal("0") for leg in leg_models), Decimal("0"))
        exit_proceeds = sum((leg.exit_proceeds or Decimal("0") for leg in leg_models), Decimal("0"))
        gross = exit_proceeds - entry_cost

        commission_status = _commission_status(legs.values())
        total_commission: Decimal | None = None
        net: Decimal | None = None
        if commission_status is CommissionStatus.KNOWN:
            total_commission = sum(
                (leg.entry_commission + leg.exit_commission for leg in legs.values()),
                Decimal("0"),
            )
            net = gross - total_commission
        elif not inputs.require_commission_for_net:  # pragma: no cover - refused in config
            total_commission = None
            net = None
        if commission_status is not CommissionStatus.KNOWN:
            reasons.append(PnLReasonCode.COMMISSION_UNAVAILABLE)

        opened_units = _structure_units(leg.opened_quantity for leg in legs.values())
        closed_units = _structure_units(leg.closed_quantity for leg in legs.values())
        matched_units = _structure_units(leg.matched_quantity for leg in leg_models)

        status = PnLStatus.COMPLETE
        if opened_units and matched_units < opened_units:
            status = PnLStatus.PARTIAL
            reasons.append(PnLReasonCode.PARTIALLY_CLOSED)
        if not reasons:
            reasons.append(PnLReasonCode.OK)

        return RealizedPnL(
            pnl_id=realized_pnl_identifier(
                position_id=inputs.position_id,
                entry_execution_id=inputs.entry_execution_id or "",
                exit_execution_ids=sorted(inputs.exit_execution_ids),
                matched_quantity=str(matched_units),
            ),
            status=status,
            reason_codes=reasons,
            opened_quantity=opened_units,
            closed_quantity=closed_units,
            matched_quantity=matched_units,
            legs=leg_models,
            entry_cost=entry_cost,
            exit_proceeds=exit_proceeds,
            realized_gross_pnl=gross,
            realized_net_pnl=net,
            total_commission=total_commission,
            commission_status=commission_status,
            return_pct=_return_pct(gross if net is None else net, entry_cost),
            currency=currency,
            detail=_detail(status, prorated=prorated, commission_status=commission_status),
            **base,
        )

    # --- internals ---------------------------------------------------------
    def _unavailable(
        self, base: dict[str, Any], reasons: list[PnLReasonCode], detail: str
    ) -> RealizedPnL:
        """A refusal that carries no figure at all, and says why."""
        matched = Decimal("0")
        return RealizedPnL(
            pnl_id=realized_pnl_identifier(
                position_id=str(base["position_id"]),
                entry_execution_id=str(base.get("entry_execution_id") or ""),
                exit_execution_ids=list(base.get("exit_execution_ids") or []),
                matched_quantity=f"UNAVAILABLE:{reasons[0].value}",
            ),
            status=PnLStatus.NOT_AVAILABLE,
            reason_codes=reasons,
            matched_quantity=matched,
            commission_status=CommissionStatus.NOT_AVAILABLE,
            detail=detail,
            **base,
        )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _MatchedLeg:
    leg: RealizedPnLLeg
    prorated: bool


def _accumulate(
    entry_fills: Sequence[FillLike], exit_fills: Sequence[FillLike]
) -> dict[str, _LegAccumulator]:
    """Group confirmed fills by contract, keeping both sides apart.

    Identity is the fill's own contract key — the broker's contract id wherever
    there is one. Two legs of a straddle are two keys; the same key appearing
    on both sides is what makes a match.
    """
    legs: dict[str, _LegAccumulator] = {}
    ordered_keys: list[str] = []

    def accumulator(fill: FillLike) -> _LegAccumulator:
        if fill.key not in legs:
            ordered_keys.append(fill.key)
            legs[fill.key] = _LegAccumulator(
                key=fill.key,
                leg_index=len(ordered_keys) - 1,
                contract_id=fill.contract_id,
                right=fill.right,
                strike=fill.strike,
                expiration=fill.expiration,
                multiplier=fill.multiplier,
            )
        return legs[fill.key]

    for fill in sorted(entry_fills, key=_fill_order):
        leg = accumulator(fill)
        _apply(leg, fill, entry=True)
    for fill in sorted(exit_fills, key=_fill_order):
        leg = accumulator(fill)
        _apply(leg, fill, entry=False)
    return legs


def _fill_order(fill: FillLike) -> tuple[datetime, str]:
    """Deterministic ordering. Ties break on the broker's own fill id."""
    return (fill.executed_at, fill.fill_id)


def _apply(leg: _LegAccumulator, fill: FillLike, *, entry: bool) -> None:
    """Fold one confirmed fill into its leg, in money."""
    if fill.multiplier is None or fill.multiplier <= 0:
        leg.multiplier_known = False
    elif leg.multiplier is None:
        leg.multiplier = fill.multiplier
    if fill.currency:
        leg.currencies.add(fill.currency)
    if leg.contract_id is None:
        leg.contract_id = fill.contract_id
    if leg.right is None:
        leg.right = fill.right
    if leg.strike is None:
        leg.strike = fill.strike
    if leg.expiration is None:
        leg.expiration = fill.expiration

    multiplier = Decimal(fill.multiplier) if fill.multiplier else Decimal("0")
    notional = fill.price * fill.quantity * multiplier

    if entry:
        leg.opened_quantity += fill.quantity
        leg.entry_notional += notional
        leg.entry_fill_ids.append(fill.fill_id)
        if fill.commission is None:
            leg.entry_commission_known = False
        else:
            leg.entry_commission += fill.commission
    else:
        leg.closed_quantity += fill.quantity
        leg.exit_notional += notional
        leg.exit_fill_ids.append(fill.fill_id)
        if fill.commission is None:
            leg.exit_commission_known = False
        else:
            leg.exit_commission += fill.commission


def _match_leg(leg: _LegAccumulator, *, precision: int, currency: str | None) -> _MatchedLeg:
    """Match one leg's two sides and attribute cost to the matched units.

    Where both sides agree the arithmetic is exact: the whole entry cost is
    matched against the whole exit proceeds. Where they do not — a structure
    that closed four of its ten units — the entry cost is *prorated* from the
    average price the account actually paid and quantised once, and the result
    records that it was. Attributing the full entry cost to a partial close
    would report a loss the position has not taken.
    """
    matched = min(leg.opened_quantity, leg.closed_quantity)
    prorated = False

    if matched == leg.opened_quantity:
        entry_cost = leg.entry_notional
    else:
        prorated = True
        entry_cost = _quantize(
            leg.entry_notional * matched / leg.opened_quantity, precision=precision
        )

    if matched == leg.closed_quantity:
        exit_proceeds = leg.exit_notional
    else:
        prorated = True
        exit_proceeds = _quantize(
            leg.exit_notional * matched / leg.closed_quantity, precision=precision
        )

    multiplier = Decimal(leg.multiplier) if leg.multiplier else Decimal("1")
    return _MatchedLeg(
        leg=RealizedPnLLeg(
            leg_index=leg.leg_index,
            key=leg.key,
            contract_id=leg.contract_id,
            right=leg.right,
            strike=leg.strike,
            expiration=leg.expiration,
            multiplier=leg.multiplier,
            opened_quantity=leg.opened_quantity,
            closed_quantity=leg.closed_quantity,
            matched_quantity=matched,
            entry_cost=entry_cost,
            exit_proceeds=exit_proceeds,
            average_entry_quote=_average_quote(leg.entry_notional, leg.opened_quantity, multiplier),
            average_exit_quote=_average_quote(leg.exit_notional, leg.closed_quantity, multiplier),
            entry_commission=leg.entry_commission if leg.entry_commission_known else None,
            exit_commission=leg.exit_commission if leg.exit_commission_known else None,
            entry_fill_ids=sorted(leg.entry_fill_ids),
            exit_fill_ids=sorted(leg.exit_fill_ids),
            currency=currency,
        ),
        prorated=prorated,
    )


def _average_quote(notional: Decimal, quantity: Decimal, multiplier: Decimal) -> Decimal | None:
    """Money back to the broker's quoted terms. ``None`` rather than a division by zero."""
    if quantity <= 0 or multiplier <= 0:
        return None
    quote = notional / quantity / multiplier
    return quote if quote > 0 else None


def _quantize(value: Decimal, *, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def _structure_units(quantities: Iterable[Decimal]) -> Decimal:
    """Units of the *structure*, taken from the weakest leg.

    The same rule Milestone 9 uses to say how much of a straddle is actually
    held: a structure exists in as many complete units as its scarcest leg
    supports, and counting anything more would report a position that is not
    there.
    """
    values = list(quantities)
    return min(values) if values else Decimal("0")


def _commission_status(legs: Iterable[_LegAccumulator]) -> CommissionStatus:
    """Whether trading costs are actually known, on both sides of every leg."""
    flags = [
        known for leg in legs for known in (leg.entry_commission_known, leg.exit_commission_known)
    ]
    if not flags:
        return CommissionStatus.NOT_AVAILABLE
    if all(flags):
        return CommissionStatus.KNOWN
    if any(flags):
        return CommissionStatus.PARTIAL
    return CommissionStatus.NOT_AVAILABLE


def _return_pct(result: Decimal | None, entry_cost: Decimal) -> float | None:
    """Result over cost, as a percentage. ``None`` rather than a division by zero."""
    if result is None or entry_cost <= 0:
        return None
    return float(result / entry_cost * Decimal("100"))


def _earliest(fills: Sequence[FillLike]) -> datetime | None:
    return min((fill.executed_at for fill in fills), default=None)


def _latest(fills: Sequence[FillLike]) -> datetime | None:
    return max((fill.executed_at for fill in fills), default=None)


def _detail(status: PnLStatus, *, prorated: bool, commission_status: CommissionStatus) -> str:
    parts = [
        {
            PnLStatus.COMPLETE: "the whole structure closed and both sides are confirmed fills",
            PnLStatus.PARTIAL: (
                "part of the structure closed; this result covers the matched units only and "
                "makes no claim about the rest"
            ),
            PnLStatus.NOT_AVAILABLE: "no result could be computed",
        }[status]
    ]
    if prorated:
        parts.append(
            "the entry cost was prorated from the average price actually paid, quantised once "
            "to the currency's precision"
        )
    if commission_status is not CommissionStatus.KNOWN:
        parts.append(
            f"commissions are {commission_status.value}: the gross figure stands and the net "
            f"one is not available, because a cost that is partly reported is not a cost"
        )
    return ". ".join(parts)
