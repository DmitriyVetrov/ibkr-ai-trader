"""Human-readable rendering of committed capital.

Rendered on demand from the immutable records: a report is a view, and a stored
view is a second copy of the truth that can drift from the first.

Two words are load-bearing here and are never used loosely:

* **committed** — capital the campaign cannot spend again, whether it is in a
  position or held for an order that has not resolved;
* **available** — capital the campaign may actually commit to something new.

An ``UNKNOWN`` reservation is printed with its reason spelled out, every time.
It is the state most likely to be misread as "stuck" and cleared by hand, and
clearing it by hand is how the same trade gets funded twice.
"""

from __future__ import annotations

from trading_system.domain.enums import ReservationState
from trading_system.reservations.models import CampaignCapital, Reservation
from trading_system.reservations.service import ReservationUpdate

__all__ = [
    "render_capital",
    "render_reservation",
    "render_reservations",
    "render_update",
]


def _or_unknown(value: object) -> str:
    """A figure, or a phrase that cannot be mistaken for one.

    ``None`` here means the envelope is not known in the currency the campaign
    trades, which is a different fact from zero and from any number. Printing
    a bare ``None`` invites a reader to treat it as an outage; naming it says
    what to do about it.
    """
    return "unknown" if value is None else str(value)


def render_capital(capital: CampaignCapital) -> str:
    """The campaign's capital position, with uncertainty called out separately."""
    lines = [
        "CAMPAIGN CAPITAL",
        "",
        f"Campaign   : {capital.campaign_id}",
        f"As of      : {capital.as_of.isoformat()}",
        f"Currency   : {capital.currency}   (what this campaign trades)",
        f"Declared   : {capital.declared_budget} {capital.declared_currency}"
        f"   (what the operator holds)",
        "",
        f"Budget           : {_or_unknown(capital.budget)}",
        f"Policy reserve   : {_or_unknown(capital.reserve)}",
        f"Allocatable      : {_or_unknown(capital.allocatable)}",
        "",
        f"Authorised total : {capital.authorized_total}",
        f"Consumed         : {capital.consumed_total}   (in positions)",
        f"Released         : {capital.released_total}   (returned to the campaign)",
        f"Committed        : {capital.committed_total}   (cannot be spent again)",
        f"Available        : {_or_unknown(capital.available)}",
        "",
        f"Reservations     : {capital.reservation_count}",
        f"Unresolved       : {capital.unknown_count}",
        f"Locked by UNKNOWN: {capital.locked_by_unknown}",
    ]
    if capital.budget is None:
        lines.extend(
            [
                "",
                f"The envelope is unknown in {capital.currency}. It is declared in "
                f"{capital.declared_currency}, and",
                "turning one into the other needs a rate this command holds no broker to",
                "fetch. The converted figure is read from the last allocation run, which",
                "recorded it with the rate that produced it — so run 'risk capture-account'",
                "and then 'allocation run'. Nothing is subtracted across currencies in the",
                "meantime.",
            ]
        )
    if capital.constrained_by_uncertainty:
        lines.extend(
            [
                "",
                f"{capital.locked_by_unknown} is held because {capital.unknown_count} "
                f"execution(s) are UNKNOWN.",
                "That capital is NOT available: an order may be live at the broker. Resolve",
                "with 'reconciliation run' or 'execution explain --resolve'; do not release it",
                "by hand, and never re-authorise it.",
            ]
        )
    return "\n".join(lines)


def render_reservation(reservation: Reservation) -> str:
    """One reservation, in full: what was authorised and what became of it."""
    lines = [
        f"Reservation : {reservation.reservation_id}",
        f"Allocation  : {reservation.allocation_id}",
        f"Opportunity : {reservation.opportunity_id}",
        f"Symbol      : {reservation.symbol}  ({reservation.strategy.value})",
        f"State       : {reservation.state.value}",
        f"Currency    : {reservation.currency}",
        "",
        f"Authorised  : {reservation.authorized_amount} "
        f"({reservation.authorized_quantity} unit(s))",
        f"Consumed    : {reservation.consumed_amount} "
        f"({'actual fills' if reservation.consumed_from_actual_fills else 'authorised unit cost'})",
        f"Released    : {reservation.released_amount}",
        f"Remaining   : {reservation.remaining_amount}",
        f"Committed   : {reservation.committed_amount}",
        "",
        f"Execution   : {reservation.execution_id or '-'}",
        f"Broker order: {reservation.broker_order_id or '-'}",
        f"Reasons     : {', '.join(code.value for code in reservation.reason_codes) or '-'}",
    ]
    if reservation.detail:
        lines.extend(["", reservation.detail])
    if reservation.state is ReservationState.UNKNOWN:
        lines.extend(
            [
                "",
                "This capital is committed because an execution's outcome is UNKNOWN, not",
                "because a position exists. It stays committed until the broker settles what",
                "happened — elapsed time is not evidence.",
            ]
        )
    return "\n".join(lines)


def render_reservations(reservations: list[Reservation]) -> str:
    """One line per reservation, newest last."""
    if not reservations:
        return "No reservations. Run 'reservations show' after an allocation run."
    lines = [
        f"{'reservation':<34} {'symbol':<8} {'state':<20} "
        f"{'authorised':>12} {'consumed':>12} {'released':>12} {'remaining':>12}"
    ]
    lines.extend(
        f"{r.reservation_id:<34} {r.symbol:<8} {r.state.value:<20} "
        f"{r.authorized_amount:>12} {r.consumed_amount:>12} "
        f"{r.released_amount:>12} {r.remaining_amount:>12}"
        for r in reservations
    )
    return "\n".join(lines)


def render_update(update: ReservationUpdate) -> str:
    """What one evaluation concluded, and whether anything moved."""
    outcome = update.outcome
    lines = [
        f"Reservation : {update.reservation.reservation_id}",
        f"Conclusion  : {outcome.state.value}  ({outcome.reason_code.value})",
        f"Consumed Δ  : {outcome.consumed_delta}",
        f"Released Δ  : {outcome.released_delta}",
        f"Recorded    : {'yes' if update.applied else 'no change'}",
    ]
    if outcome.detail:
        lines.extend(["", outcome.detail])
    return "\n".join(lines)
