"""Choosing which pre-existing holdings a cleanup may touch.

A pure function of a stored reconciliation result and a broker position
snapshot. No broker, no repository, no clock, no configuration side effects —
so the target list can be reviewed, stored, and derived again to check that it
was right.

The selection rule is deliberately the narrow one:

.. code-block:: text

    a holding is targetable  IFF

        a reconciliation result reported it as ORPHAN_BROKER_POSITION
        AND the finding is addressed by a broker CONTRACT ID
        AND the current broker snapshot still reports that contract
        AND the current quantity equals the quantity that was reported
        AND the quantity is positive and whole

Note what is **not** the rule. "Every position the internal ledger does not
recognise" is a different and much more dangerous test, because when the ledger
cannot be read *nothing* is recognised — so that rule liquidates the account at
precisely the moment the system is least able to say what it owns.
``cleanup.require_orphan_finding: false`` fails to load for the same reason.

Every rejection is kept, with a reason. A holding that was an orphan yesterday
and has changed quantity today is a fact an operator needs to see, and silently
dropping it from the list would present a shorter target set as though the
account had simply grown tidier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_system.cleanup.models import CleanupTarget
from trading_system.domain.enums import ReconciliationFindingType
from trading_system.positions.models import BrokerPositionSnapshot, ObservedPosition
from trading_system.reconciliation.models import ReconciliationFinding, ReconciliationResult

__all__ = [
    "CleanupCandidate",
    "TargetSelection",
    "select_targets",
]


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    """One orphan finding and what became of it during selection."""

    key: str
    finding_id: str
    summary: str
    accepted: bool
    reason: str
    target: CleanupTarget | None = None
    observed: ObservedPosition | None = None


@dataclass(frozen=True, slots=True)
class TargetSelection:
    """The whole selection: what was accepted, what was not, and why."""

    reconciliation_id: str
    account_reference: str
    candidates: tuple[CleanupCandidate, ...] = ()

    @property
    def targets(self) -> tuple[CleanupTarget, ...]:
        return tuple(
            candidate.target
            for candidate in self.candidates
            if candidate.accepted and candidate.target is not None
        )

    @property
    def rejected(self) -> tuple[CleanupCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if not candidate.accepted)

    @property
    def orphan_count(self) -> int:
        return len(self.candidates)


def orphan_findings(result: ReconciliationResult) -> tuple[ReconciliationFinding, ...]:
    """Every ``ORPHAN_BROKER_POSITION`` the result reported, ordered by key."""
    return tuple(
        sorted(
            (
                finding
                for finding in result.findings
                if finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_POSITION
            ),
            key=lambda finding: finding.identifier,
        )
    )


def select_targets(
    *,
    result: ReconciliationResult,
    snapshot: BrokerPositionSnapshot,
    wanted_contract_ids: Sequence[int] | None = None,
) -> TargetSelection:
    """Derive the cleanup target set from a reconciliation and a fresh snapshot.

    ``wanted_contract_ids`` narrows the set and can never widen it: a contract
    id the reconciliation did not report as an orphan is not selectable by
    naming it, which is what makes the option safe to expose on the CLI.
    """
    by_key = {position.key: position for position in snapshot.positions}
    wanted = {int(value) for value in wanted_contract_ids} if wanted_contract_ids else None

    candidates: list[CleanupCandidate] = []
    for finding in orphan_findings(result):
        key = finding.identifier
        summary = finding.summary

        if not key.startswith("cid:") or finding.contract_id is None:
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    reason=(
                        "identified without a broker contract id. A symbol, strike, expiry and "
                        "right are shared by adjusted contracts, so an order addressed that way "
                        "could sell a different instrument than the one reported"
                    ),
                )
            )
            continue

        if wanted is not None and finding.contract_id not in wanted:
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    reason="not among the contract ids this run was asked to close",
                )
            )
            continue

        observed = by_key.get(key)
        if observed is None:
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    reason=(
                        "the broker no longer reports this holding. Nothing to close, and "
                        "nothing is sent — this is the ordinary answer once a cleanup has "
                        "already run"
                    ),
                )
            )
            continue

        reported = _reported_quantity(finding)
        if reported is not None and observed.quantity != reported:
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    observed=observed,
                    reason=(
                        f"the broker now holds {observed.quantity} where the reconciliation "
                        f"reported {reported}. The account changed after the report an operator "
                        f"reviewed; re-run reconciliation and review the new one"
                    ),
                )
            )
            continue

        if observed.quantity <= 0:
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    observed=observed,
                    reason=(
                        f"the holding is {observed.quantity}. Closing a short is a purchase "
                        f"whose cost is unbounded above and nothing here is authorised to "
                        f"decide it; a zero is not a holding at all"
                    ),
                )
                if observed.quantity < 0
                else CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    observed=observed,
                    reason="the broker reports a quantity of zero; there is nothing to close",
                )
            )
            continue

        if observed.quantity != observed.quantity.to_integral_value():
            candidates.append(
                CleanupCandidate(
                    key=key,
                    finding_id=finding.finding_id,
                    summary=summary,
                    accepted=False,
                    observed=observed,
                    reason=(
                        f"the broker reports a fractional quantity of {observed.quantity}; an "
                        f"option position is whole contracts and this describes something this "
                        f"operation does not model"
                    ),
                )
            )
            continue

        candidates.append(
            CleanupCandidate(
                key=key,
                finding_id=finding.finding_id,
                summary=summary,
                accepted=True,
                observed=observed,
                reason="reported as an orphan, still held, quantity unchanged",
                target=_target_from(
                    observed,
                    finding_id=finding.finding_id,
                    reconciliation_id=result.reconciliation_id,
                ),
            )
        )

    return TargetSelection(
        reconciliation_id=result.reconciliation_id,
        account_reference=result.account_reference,
        candidates=tuple(candidates),
    )


def _reported_quantity(finding: ReconciliationFinding) -> Decimal | None:
    """The quantity the finding recorded, or ``None`` if it recorded none.

    Parsed rather than assumed. A finding whose observed value is not a number
    yields ``None``, and the quantity check is then skipped — the snapshot's own
    figure is still what the order is built from, so nothing is invented either
    way.
    """
    if finding.observed_value is None:
        return None
    try:
        return Decimal(str(finding.observed_value))
    except (ArithmeticError, ValueError):
        return None


def _target_from(
    observed: ObservedPosition, *, finding_id: str, reconciliation_id: str
) -> CleanupTarget:
    """Copy a broker-observed holding into a target. Nothing is completed."""
    assert observed.contract_id is not None  # guarded by the caller's key check
    return CleanupTarget(
        key=observed.key,
        contract_id=observed.contract_id,
        position_id=observed.position_id,
        account_reference=observed.account_reference,
        underlying=observed.underlying,
        symbol=observed.symbol,
        asset_class=observed.asset_class,
        expiration=observed.expiration,
        strike=observed.strike,
        right=observed.right,
        multiplier=observed.multiplier,
        trading_class=observed.trading_class,
        exchange=observed.exchange,
        local_symbol=observed.local_symbol,
        currency=observed.currency,
        quantity=observed.quantity,
        average_cost=observed.average_cost,
        market_price=observed.market_price,
        market_value=observed.market_value,
        finding_id=finding_id,
        reconciliation_id=reconciliation_id,
        broker_source=observed.broker_source,
        observed_at=observed.observed_at,
        broker_timestamp=observed.broker_timestamp,
        simulated=observed.simulated,
    )
