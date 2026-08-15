"""Project confirmed fills onto the positions this system believes it holds.

The internal ledger, and the only thing that may feed it is a **confirmed
broker fill**. Written out, because every one of these is a way a trading
system convinces itself it holds something it does not:

.. code-block:: text

    ALLOCATION          is not a position
    SUBMITTED ORDER     is not a position
    ACKNOWLEDGED ORDER  is not a position
    UNKNOWN ORDER       is not a position
    CONFIRMED FILL      is a position

The arithmetic is explicit rather than assumed. A buy adds quantity and a sell
subtracts it, read from the fill's own ``side``. Nothing infers direction from
a strategy name: the four shipped strategies are long-premium structures today,
and code that hard-coded that would be wrong the first time this system closes
one.

Two sources contribute, and which one covered an execution is recorded:

* **Recorded fills**, when this system captured the broker's execution reports
  for an execution. Exact, per contract.
* **The execution ledger's own confirmed fill counts**, otherwise. IBKR's
  execution list is *session-scoped* — it reports today's fills, not the
  account's history — so an execution filled last week has no fills to read,
  while its record still holds the count the broker reported at the time.

The fallback is deliberately conservative about price. For a single-leg order,
the broker's average fill price *is* the leg's price. For a combo it is the
price of the **structure**, and attributing it to each leg would invent two
numbers that never traded, so the leg price is left unknown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    ExecutionIntent,
    LegAction,
    OrderSide,
    SecurityType,
    StructureStatus,
)
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedFill,
    StrategyLegPosition,
    StrategyPosition,
    contract_key,
    position_identifier,
    strategy_position_identifier,
)

__all__ = [
    "ExpectedProjection",
    "expected_from_execution",
    "expected_from_fills",
    "leg_key",
    "project_expected_positions",
    "strategy_position_for",
]


def leg_key(leg: object, *, symbol: str, currency: str | None = None) -> str:
    """The contract key for one execution leg. Duck-typed, like the fill translation."""
    return contract_key(
        contract_id=getattr(leg, "contract_id", None),
        symbol=symbol,
        security_type=SecurityType.OPTION,
        expiration=getattr(leg, "expiration", None),
        strike=getattr(leg, "strike", None),
        right=getattr(leg, "right", None),
        currency=currency or getattr(leg, "currency", None),
    )


@dataclass(frozen=True, slots=True)
class ExpectedProjection:
    """What the internal ledger believes, and where each part of it came from.

    ``covered_by_fills`` and ``covered_by_execution_record`` are not decoration:
    a position derived from recorded fills is exact per contract, one derived
    from an execution's own counts is exact in quantity and silent about
    per-leg price. Reporting which is which keeps the second from being read as
    the first.
    """

    positions: tuple[ExpectedPosition, ...] = ()
    strategies: tuple[StrategyPosition, ...] = ()
    covered_by_fills: tuple[str, ...] = ()
    covered_by_execution_record: tuple[str, ...] = ()
    #: Executions that established nothing: refused, failed, or never filled.
    contributed_nothing: tuple[str, ...] = ()

    def by_key(self, key: str) -> ExpectedPosition | None:
        return next((position for position in self.positions if position.key == key), None)

    @property
    def open_positions(self) -> tuple[ExpectedPosition, ...]:
        return tuple(position for position in self.positions if position.is_open)


# ---------------------------------------------------------------------------
# From recorded fills
# ---------------------------------------------------------------------------
def expected_from_fills(
    fills: Sequence[ObservedFill],
    *,
    as_of: datetime,
    account_reference: str,
) -> list[ExpectedPosition]:
    """Net every confirmed fill into one expected position per instrument.

    Deterministic: fills are grouped by contract key and applied in
    ``executed_at`` order, so the same history always produces the same ledger.
    A contract that was opened and closed produces a position of zero, which is
    a different statement from having no record of it at all.
    """
    grouped: dict[str, list[ObservedFill]] = {}
    for fill in fills:
        grouped.setdefault(fill.key, []).append(fill)

    positions: list[ExpectedPosition] = []
    for key in sorted(grouped):
        ordered = sorted(grouped[key], key=lambda fill: (fill.executed_at, fill.fill_id))
        positions.append(_position_from(ordered, key=key, as_of=as_of, account=account_reference))
    return positions


def _position_from(
    fills: list[ObservedFill], *, key: str, as_of: datetime, account: str
) -> ExpectedPosition:
    first = fills[0]
    bought = sum((f.quantity for f in fills if f.side is OrderSide.BUY), Decimal("0"))
    sold = sum((f.quantity for f in fills if f.side is OrderSide.SELL), Decimal("0"))
    net = bought - sold

    # The average price of the side that established what is currently held. A
    # closed position has no entry price to report here: what it cost and what
    # it returned is a profit-and-loss question, and this model is not one.
    establishing = OrderSide.BUY if net > 0 else OrderSide.SELL
    contributing = [f for f in fills if f.side is establishing] if net != 0 else []
    volume = sum((f.quantity for f in contributing), Decimal("0"))
    average_price: Decimal | None = None
    if volume > 0:
        average_price = sum((f.price * f.quantity for f in contributing), Decimal("0")) / volume

    multiplier = next((f.multiplier for f in fills if f.multiplier is not None), None)
    average_cost = (
        average_price * Decimal(multiplier)
        if average_price is not None and multiplier is not None
        else None
    )
    total_cost = average_cost * abs(net) if average_cost is not None and net != 0 else None

    known = [f.commission for f in fills if f.commission is not None]
    complete = len(known) == len(fills) and bool(fills)
    commission = sum(known, Decimal("0")) if known else None

    return ExpectedPosition(
        position_id=position_identifier(account_reference=account, key=key),
        account_reference=account,
        key=key,
        as_of=as_of,
        underlying=first.underlying,
        asset_class=first.asset_class,
        symbol=first.symbol,
        contract_id=first.contract_id,
        expiration=first.expiration,
        strike=first.strike,
        right=first.right,
        multiplier=multiplier,
        currency=first.currency,
        quantity=net,
        bought_quantity=bought,
        sold_quantity=sold,
        average_price=average_price,
        average_cost=average_cost,
        total_cost=total_cost,
        commission=commission,
        commission_complete=complete,
        fill_ids=[f.fill_id for f in fills],
        execution_ids=sorted({f.execution_id for f in fills if f.execution_id}),
        allocation_ids=sorted({f.allocation_id for f in fills if f.allocation_id}),
        opportunity_ids=sorted({f.opportunity_id for f in fills if f.opportunity_id}),
        first_fill_at=fills[0].executed_at,
        last_fill_at=fills[-1].executed_at,
        provenance=(
            AcquisitionProvenance.SYSTEM_EXECUTION
            if any(f.execution_id for f in fills)
            else AcquisitionProvenance.UNKNOWN
        ),
    )


# ---------------------------------------------------------------------------
# From an execution record's own confirmed counts
# ---------------------------------------------------------------------------
def expected_from_execution(
    record: object,
    *,
    as_of: datetime,
    account_reference: str,
) -> list[ExpectedPosition]:
    """One expected position per leg, from an execution's confirmed fill count.

    Used where the broker's execution list cannot supply the fills — which is
    the ordinary case for anything older than the current session. The count
    comes from the record's ``filled_quantity``, which only a broker fill
    report ever moves.

    Per-leg price is reported only for a single-leg order. A combo's average
    fill price is the price of the structure; splitting it across legs would
    produce two prices that never traded.
    """
    filled = int(getattr(record, "filled_quantity", 0) or 0)
    legs = list(getattr(record, "legs", []) or [])
    if filled <= 0 or not legs:
        return []

    symbol = str(getattr(record, "underlying", "")).strip().upper()
    single_leg = len(legs) == 1
    average = getattr(record, "average_fill_price", None) if single_leg else None
    execution_id = getattr(record, "execution_id", None)
    allocation_id = getattr(record, "allocation_id", None)
    opportunity_id = getattr(record, "opportunity_id", None)
    at = getattr(record, "filled_at", None) or getattr(record, "updated_at", None)

    positions: list[ExpectedPosition] = []
    for leg in legs:
        ratio = int(getattr(leg, "ratio", 1) or 1)
        action = getattr(leg, "action", LegAction.BUY)
        contracts = Decimal(filled * ratio)
        signed = contracts if action is LegAction.BUY else -contracts
        key = leg_key(leg, symbol=symbol)
        multiplier = getattr(leg, "multiplier", None)
        cost = (
            average * Decimal(multiplier)
            if average is not None and multiplier is not None
            else None
        )
        positions.append(
            ExpectedPosition(
                position_id=position_identifier(account_reference=account_reference, key=key),
                account_reference=account_reference,
                key=key,
                as_of=as_of,
                underlying=symbol,
                asset_class=SecurityType.OPTION,
                symbol=symbol,
                contract_id=getattr(leg, "contract_id", None),
                expiration=getattr(leg, "expiration", None),
                strike=getattr(leg, "strike", None),
                right=getattr(leg, "right", None),
                multiplier=multiplier,
                currency=getattr(leg, "currency", None),
                quantity=signed,
                bought_quantity=contracts if action is LegAction.BUY else Decimal("0"),
                sold_quantity=Decimal("0") if action is LegAction.BUY else contracts,
                average_price=average,
                average_cost=cost,
                total_cost=cost * abs(signed) if cost is not None else None,
                commission=None,
                commission_complete=False,
                # The execution record *is* the evidence here, so it stands in
                # for a fill id. Naming it keeps "this position rests on
                # something" true without inventing a broker execution id.
                fill_ids=[f"execution:{execution_id}"] if execution_id else [],
                execution_ids=[execution_id] if execution_id else [],
                allocation_ids=[allocation_id] if allocation_id else [],
                opportunity_ids=[opportunity_id] if opportunity_id else [],
                first_fill_at=at,
                last_fill_at=at,
                provenance=AcquisitionProvenance.SYSTEM_EXECUTION,
            )
        )
    return positions


# ---------------------------------------------------------------------------
# The whole internal ledger
# ---------------------------------------------------------------------------
def project_expected_positions(
    *,
    fills: Sequence[ObservedFill],
    executions: Sequence[object],
    as_of: datetime,
    account_reference: str,
    snapshot: BrokerPositionSnapshot | None = None,
) -> ExpectedProjection:
    """Build the internal position ledger from everything confirmed.

    Executions covered by recorded fills use those; the rest fall back to their
    own confirmed counts, and no execution contributes twice. An execution that
    filled nothing contributes nothing — including one in ``UNKNOWN``, which is
    precisely a submission whose fills are not known.
    """
    fills_by_execution: dict[str, list[ObservedFill]] = {}
    for fill in fills:
        if fill.execution_id:
            fills_by_execution.setdefault(fill.execution_id, []).append(fill)

    # A fill with no execution of ours behind it is deliberately excluded. It
    # is real — the broker reported it — but this ledger records what *this
    # system* believes it did, and a holding acquired by something else is not
    # that. Including it would make an orphan broker position agree with an
    # internal expectation that was itself derived from the broker, so the
    # comparison would confirm itself and report nothing. Such fills surface as
    # ORPHAN_BROKER_FILL, and the positions they explain as
    # ORPHAN_BROKER_POSITION, which is what a person actually needs to see.
    covered_by_fills: list[str] = []
    covered_by_record: list[str] = []
    nothing: list[str] = []
    contributing: list[ObservedFill] = []
    from_records: list[ExpectedPosition] = []

    for record in executions:
        execution_id = str(getattr(record, "execution_id", ""))
        if not getattr(record, "intent", ExecutionIntent.OPEN).adjusts_expected_positions:
            # An orphan cleanup. Its fills are real, they are recorded, and
            # they are deliberately kept out of *this* ledger — which records
            # what this system believes it did, not everything it has ever
            # sent. Netting a cleanup sale in would produce an expected
            # position of minus one for a contract this system never expected
            # to hold, and the very next reconciliation would report that
            # invention as a discrepancy against a broker holding of zero.
            #
            # Filed under ``contributed_nothing``, which is the truth: it
            # established nothing and it ended nothing of ours.
            nothing.append(execution_id)
            continue
        recorded = fills_by_execution.get(execution_id)
        if recorded:
            contributing.extend(recorded)
            covered_by_fills.append(execution_id)
            continue
        projected = expected_from_execution(
            record, as_of=as_of, account_reference=account_reference
        )
        if projected:
            from_records.extend(projected)
            covered_by_record.append(execution_id)
        else:
            nothing.append(execution_id)

    merged = _merge(
        expected_from_fills(contributing, as_of=as_of, account_reference=account_reference),
        from_records,
    )
    # Only an *opening* execution establishes a logical structure. A closing
    # one ends the structure its entry established and authorises no new one,
    # so deriving a second StrategyPosition from it would describe a straddle
    # with two negative legs — and a partially closed structure would surface
    # as PARTIAL_STRUCTURE, which is a finding about an authorised position
    # that is only half held, not about one that is half sold. The exit's
    # *fills* still net into the expected positions above, which is precisely
    # how the structure becomes MISSING and the position becomes closed.
    strategies = tuple(
        position
        for position in (
            strategy_position_for(record, snapshot=snapshot, as_of=as_of, account=account_reference)
            for record in executions
            if getattr(record, "intent", ExecutionIntent.OPEN).establishes_position
        )
        if position is not None
    )
    return ExpectedProjection(
        positions=tuple(sorted(merged, key=lambda p: (p.underlying, p.key))),
        strategies=strategies,
        covered_by_fills=tuple(sorted(covered_by_fills)),
        covered_by_execution_record=tuple(sorted(covered_by_record)),
        contributed_nothing=tuple(sorted(nothing)),
    )


def _merge(
    from_fills: Sequence[ExpectedPosition], from_records: Sequence[ExpectedPosition]
) -> list[ExpectedPosition]:
    """Combine two sources into one position per instrument.

    Quantities add; a price known on one side and unknown on the other is not
    blended, because averaging a known price with a missing one would produce a
    number no fill ever traded at. The merged record keeps the price only when
    exactly one side supplied one.
    """
    merged: dict[str, ExpectedPosition] = {position.key: position for position in from_fills}
    for position in from_records:
        existing = merged.get(position.key)
        if existing is None:
            merged[position.key] = position
            continue
        bought = existing.bought_quantity + position.bought_quantity
        sold = existing.sold_quantity + position.sold_quantity
        prices = [
            price for price in (existing.average_price, position.average_price) if price is not None
        ]
        average = prices[0] if len(prices) == 1 else None
        multiplier = existing.multiplier or position.multiplier
        cost = average * Decimal(multiplier) if average is not None and multiplier else None
        net = bought - sold
        merged[position.key] = existing.model_copy(
            update={
                "quantity": net,
                "bought_quantity": bought,
                "sold_quantity": sold,
                "average_price": average,
                "average_cost": cost,
                "total_cost": cost * abs(net) if cost is not None and net != 0 else None,
                "commission_complete": False,
                "fill_ids": [*existing.fill_ids, *position.fill_ids],
                "execution_ids": sorted({*existing.execution_ids, *position.execution_ids}),
                "allocation_ids": sorted({*existing.allocation_ids, *position.allocation_ids}),
                "opportunity_ids": sorted({*existing.opportunity_ids, *position.opportunity_ids}),
            }
        )
    return list(merged.values())


# ---------------------------------------------------------------------------
# The logical view
# ---------------------------------------------------------------------------
def strategy_position_for(
    record: object,
    *,
    snapshot: BrokerPositionSnapshot | None,
    as_of: datetime,
    account: str,
) -> StrategyPosition | None:
    """The logical structure one execution established, and whether it is there.

    ``None`` for an execution that filled nothing: there is no structure to
    describe, and describing one as ``MISSING`` would imply the system expected
    a position it never had.

    Where broker data is unusable the status is ``UNKNOWN`` and every observed
    quantity is ``None``. That is not the same as zero: "we could not look" and
    "the broker holds none" would otherwise be the same finding, and one of
    them is a reason to stop trading while the other is a reason to investigate
    a specific contract.
    """
    filled = int(getattr(record, "filled_quantity", 0) or 0)
    legs = list(getattr(record, "legs", []) or [])
    if filled <= 0 or not legs:
        return None

    symbol = str(getattr(record, "underlying", "")).strip().upper()
    usable = snapshot is not None and snapshot.usable
    leg_positions: list[StrategyLegPosition] = []
    for index, leg in enumerate(legs):
        key = leg_key(leg, symbol=symbol)
        ratio = int(getattr(leg, "ratio", 1) or 1)
        action = getattr(leg, "action", LegAction.BUY)
        contracts = Decimal(filled * ratio)
        expected = contracts if action is LegAction.BUY else -contracts
        observed: Decimal | None = None
        if usable and snapshot is not None:
            held = snapshot.by_key(key)
            observed = held.quantity if held is not None else Decimal("0")
        leg_positions.append(
            StrategyLegPosition(
                leg_index=index,
                key=key,
                underlying=symbol,
                right=getattr(leg, "right", None),
                strike=getattr(leg, "strike", None),
                expiration=getattr(leg, "expiration", None),
                contract_id=getattr(leg, "contract_id", None),
                multiplier=getattr(leg, "multiplier", None),
                expected_quantity=expected,
                observed_quantity=observed,
            )
        )

    status = structure_status(leg_positions, usable=usable)
    return StrategyPosition(
        strategy_position_id=strategy_position_identifier(
            opportunity_id=str(getattr(record, "opportunity_id", "")),
            execution_id=getattr(record, "execution_id", None),
        ),
        account_reference=account,
        as_of=as_of,
        underlying=symbol,
        strategy=getattr(record, "strategy", None),
        status=status,
        authorized_quantity=int(getattr(record, "quantity", 0) or 0),
        filled_quantity=Decimal(filled),
        legs=leg_positions,
        opportunity_id=str(getattr(record, "opportunity_id", "")),
        allocation_id=getattr(record, "allocation_id", None),
        execution_id=getattr(record, "execution_id", None),
        campaign_id=getattr(record, "campaign_id", None),
        purchase_card_id=getattr(record, "purchase_card_id", None),
        contract_selection_id=getattr(record, "contract_selection_id", None),
        strategy_decision_id=getattr(record, "strategy_decision_id", None),
        research_report_id=getattr(record, "research_report_id", None),
        broker_source=snapshot.broker if snapshot is not None else "UNKNOWN",
        detail=(
            None
            if usable
            else (
                f"broker position data is {_read_status(snapshot).value}, so no claim is made "
                f"about whether this structure is held"
            )
        ),
    )


def _read_status(snapshot: BrokerPositionSnapshot | None) -> BrokerReadStatus:
    """What we know about the broker read behind a structure's observed side."""
    return snapshot.read_status if snapshot is not None else BrokerReadStatus.NOT_REQUESTED


def structure_status(legs: Sequence[StrategyLegPosition], *, usable: bool) -> StructureStatus:
    """Derive a structure's status from its legs, never from its allocation.

    ``PARTIAL`` is the answer this exists for. A straddle with its call held
    and its put absent is not a straddle and is not an absence of one — it is a
    naked long call against limits nobody checked for one, and only a status
    that can say so will ever cause anyone to look.
    """
    if not usable or any(leg.observed_quantity is None for leg in legs):
        return StructureStatus.UNKNOWN
    if all(leg.present for leg in legs):
        return StructureStatus.COMPLETE
    if all(leg.absent for leg in legs):
        return StructureStatus.MISSING
    return StructureStatus.PARTIAL
