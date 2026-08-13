"""Human-readable rendering of position state.

Rendered on demand from the immutable records rather than stored alongside
them: a report is a view, and a stored view is a second copy of the truth that
can drift from the first.

One rule governs every line here, and it is the milestone's whole point: an
**internal expected** position and a **broker observed** position are labelled
as such, every time, without exception. A reader who cannot tell which they are
looking at cannot tell whether a number is a belief or a fact — and the entire
value of this stage is that the two are kept apart.

The second rule is the Milestone 3 one: an unavailable value prints as ``-``
and never as ``0``. "The broker reported no market value" and "the market value
is zero" must not look the same on a screen either.
"""

from __future__ import annotations

from decimal import Decimal

from trading_system.positions.expected import ExpectedProjection
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ExpectedPosition,
    ObservedFill,
    ObservedPosition,
    StrategyPosition,
)
from trading_system.positions.service import PositionCapture

__all__ = [
    "render_capture",
    "render_expected",
    "render_fill",
    "render_observed",
    "render_snapshot",
    "render_strategy_position",
]


def _or_dash(value: object | None) -> str:
    """An unavailable value is a dash. Never a zero."""
    return "-" if value is None else str(value)


def render_snapshot(snapshot: BrokerPositionSnapshot) -> str:
    """One broker snapshot: what the account holds, or why we do not know."""
    lines = [
        "BROKER OBSERVED POSITIONS",
        "",
        f"Snapshot   : {snapshot.snapshot_id}",
        f"Broker     : {snapshot.broker}",
        f"Account    : {snapshot.account_reference}",
        f"Mode       : {snapshot.trading_mode.value}",
        f"As of      : {snapshot.as_of.isoformat()}",
        f"Observed   : {snapshot.observed_at.isoformat()}",
        f"Read status: {snapshot.read_status.value}",
        f"Content    : {snapshot.content_hash}",
        f"Orders submitted: {snapshot.orders_submitted}",
    ]
    if not snapshot.usable:
        lines.extend(
            [
                "",
                "Broker state could NOT be read. This is not an empty account: nothing here",
                "says the broker holds no positions, only that we were unable to look.",
                f"Detail: {snapshot.detail}",
            ]
        )
        return "\n".join(lines)

    if not snapshot.positions:
        lines.extend(
            [
                "",
                "The broker answered and reported no positions. That is a fact about the",
                "account, not a failed read.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["", f"Positions  : {len(snapshot.positions)}", ""])
    lines.extend(f"  {render_observed(position)}" for position in snapshot.positions)
    return "\n".join(lines)


def render_observed(position: ObservedPosition) -> str:
    """One line for one broker-reported holding."""
    identity = "contract id" if position.identified_by_contract_id else "symbol (weak key)"
    return (
        f"{position.describe():<34} qty {position.quantity:>8}  "
        f"avg cost {_or_dash(position.average_cost):>10}  "
        f"mkt value {_or_dash(position.market_value):>12}  "
        f"unreal P&L {_or_dash(position.unrealized_pnl):>10}  "
        f"[{identity}; provenance {position.provenance.value}]"
    )


def render_expected(position: ExpectedPosition) -> str:
    """One line for one internally expected holding."""
    return (
        f"{position.symbol:<8} {position.key:<24} qty {position.quantity:>8}  "
        f"avg price {_or_dash(position.average_price):>8}  "
        f"avg cost {_or_dash(position.average_cost):>10}  "
        f"fills {len(position.fill_ids):>3}  "
        f"commission {_or_dash(position.commission)}"
        f"{'' if position.commission_complete else ' (incomplete)'}"
    )


def render_fill(fill: ObservedFill) -> str:
    """One recorded fill, in the broker's own quoted terms."""
    return (
        f"{fill.executed_at.isoformat()}  {fill.side.value:<4} {fill.quantity:>6} "
        f"{fill.underlying:<6} @ {fill.price:>8}  "
        f"commission {_or_dash(fill.commission):>8}  "
        f"exec {fill.broker_execution_id or '(derived id)'}  "
        f"order {fill.broker_order_id or '-'}"
    )


def render_strategy_position(position: StrategyPosition) -> str:
    """The logical structure, and whether the broker actually holds it."""
    lines = [
        f"Structure  : {position.underlying} {position.strategy.value}",
        f"Status     : {position.status.value}",
        f"Authorised : {position.authorized_quantity} unit(s)",
        f"Filled     : {position.filled_quantity} unit(s)",
        f"Opportunity: {position.opportunity_id}",
        f"Execution  : {position.execution_id or '-'}",
    ]
    for leg in position.legs:
        expected = leg.expected_quantity
        observed = "unknown" if leg.observed_quantity is None else str(leg.observed_quantity)
        lines.append(
            f"  leg {leg.leg_index}  {leg.right.value if leg.right else '-':<4} "
            f"{_or_dash(leg.strike):>8} {_or_dash(leg.expiration)}  "
            f"expected {expected:>6}  broker {observed:>8}"
        )
    if position.detail:
        lines.extend(["", position.detail])
    return "\n".join(lines)


def render_capture(capture: PositionCapture) -> str:
    """The outcome of one ``positions snapshot``, including what was refused."""
    lines = [render_snapshot(capture.snapshot), ""]
    lines.append(f"Stored             : {'yes' if capture.stored else 'no'}")
    lines.append(f"Fills recorded     : {len(capture.recorded_fills)}")
    lines.append(f"Fills re-observed  : {len(capture.reobserved_fills)}")
    if capture.refused_fills:
        lines.append("")
        lines.append("Fills that could NOT be recorded (reported, never guessed at):")
        lines.extend(f"  - {reason}" for reason in capture.refused_fills)
    if capture.warnings:
        lines.append("")
        lines.append("Broker reads that failed:")
        lines.extend(f"  - {warning}" for warning in capture.warnings)
    lines.append("")
    lines.append(f"Orders submitted   : {capture.orders_submitted}")
    return "\n".join(lines)


def render_projection(projection: ExpectedProjection) -> str:
    """The internal ledger, labelled as internal on every line."""
    lines = [
        "INTERNAL EXPECTED POSITIONS",
        "",
        "These are what this system believes should exist, derived from confirmed broker",
        "fills. They are NOT broker reality; compare them with 'reconciliation run'.",
        "",
        f"Instruments        : {len(projection.positions)}",
        f"Open               : {len(projection.open_positions)}",
        f"From recorded fills: {len(projection.covered_by_fills)} execution(s)",
        f"From execution ledger: {len(projection.covered_by_execution_record)} execution(s)",
        "",
    ]
    if not projection.positions:
        lines.append("  (none)")
        return "\n".join(lines)
    lines.extend(f"  {render_expected(position)}" for position in projection.positions)
    if projection.strategies:
        lines.extend(["", "Logical structures:", ""])
        for structure in projection.strategies:
            lines.append(render_strategy_position(structure))
            lines.append("")
    return "\n".join(lines)


def total_expected_quantity(projection: ExpectedProjection) -> Decimal:
    """Sum of every expected contract. Derived, never stored."""
    return sum((position.quantity for position in projection.positions), Decimal("0"))
