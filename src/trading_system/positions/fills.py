"""Turn broker execution reports into recorded fills.

Pure functions over the Milestone 2 domain models. Nothing here talks to a
broker and nothing here decides anything.

Four rules, and each is a way a position ledger drifts from reality:

* **A fill is a fill report.** Nothing derives one from a submitted quantity,
  an acknowledgement or an order that "should have" filled by now.
* **Observing the same fill twice records one fill.** Reconciliation polls the
  broker repeatedly by design, so identity is what keeps that idempotent: the
  broker's own execution id where there is one, and the strongest deterministic
  combination of authoritative broker fields where there is not. The second
  observation is a re-observation, never a second trade.
* **A commission the broker did not report is ``None``.** IBKR routinely sends
  a fill before its commission report. Zero is a claim about cost.
* **An option fill that cannot be identified is refused.** A broker execution
  carries a symbol and a contract id but no strike, expiration or right, so an
  option fill with neither a contract id nor supplied contract terms would key
  on ``NVDA|OPTION`` and merge every strike into one position. That is a
  translation failure, reported as one, not a position.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trading_system.domain.enums import (
    AcquisitionProvenance,
    OptionRight,
    SecurityType,
)
from trading_system.domain.models import BrokerExecution
from trading_system.positions.models import (
    ObservedFill,
    contract_key,
    fill_identifier,
    mask_account,
)

__all__ = [
    "ContractTerms",
    "FillTranslationError",
    "aggregate_by_contract",
    "deduplicate_fills",
    "new_fills",
    "terms_from_legs",
    "to_observed_fill",
    "to_observed_fills",
]


class FillTranslationError(ValueError):
    """A broker execution could not be recorded as a fill.

    Raised rather than returning a partly-identified fill: a fill whose
    instrument is ambiguous would be aggregated into the wrong position, and a
    wrong position is worse than a missing one because nothing looks broken.
    """


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """The option terms a broker execution report does not carry.

    IBKR's execution objects name the contract by id and by underlying symbol;
    the strike, expiration, right and multiplier come from the contract that
    was submitted. Supplying them here keeps the translation honest without
    inventing anything: they are read off the execution record's own legs,
    which is where the contract was chosen.
    """

    expiration: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None
    multiplier: int | None = None
    local_symbol: str | None = None
    trading_class: str | None = None

    @property
    def complete_for_option(self) -> bool:
        return self.expiration is not None and self.strike is not None and self.right is not None


def terms_from_legs(legs: Iterable[object]) -> dict[int, ContractTerms]:
    """Index contract terms by broker contract id, from execution legs.

    Duck-typed on purpose: an :class:`~trading_system.execution.models.ExecutionLeg`
    and a Milestone 7 ``CandidateLeg`` carry the same field names, and this
    module has no business importing either package.
    """
    terms: dict[int, ContractTerms] = {}
    for leg in legs:
        contract_id = getattr(leg, "contract_id", None)
        if not isinstance(contract_id, int) or contract_id <= 0:
            continue
        terms[contract_id] = ContractTerms(
            expiration=getattr(leg, "expiration", None),
            strike=getattr(leg, "strike", None),
            right=getattr(leg, "right", None),
            multiplier=getattr(leg, "multiplier", None),
            local_symbol=getattr(leg, "local_symbol", None),
            trading_class=getattr(leg, "trading_class", None),
        )
    return terms


def to_observed_fill(
    execution: BrokerExecution,
    *,
    observed_at: datetime,
    account_reference: str | None = None,
    mask_visible: int = 4,
    terms: ContractTerms | None = None,
    execution_id: str | None = None,
    allocation_id: str | None = None,
    opportunity_id: str | None = None,
    provenance: AcquisitionProvenance = AcquisitionProvenance.UNKNOWN,
    simulated: bool = False,
) -> ObservedFill:
    """Normalise one broker execution report.

    Raises :class:`FillTranslationError` for an option fill this system cannot
    identify — see the module docstring. Everything else the broker omitted
    stays ``None``.
    """
    resolved = terms or ContractTerms()
    is_option = execution.security_type in (SecurityType.OPTION, SecurityType.FUTURE_OPTION)
    has_contract_id = execution.contract_id is not None and execution.contract_id > 0

    if is_option and not has_contract_id and not resolved.complete_for_option:
        raise FillTranslationError(
            f"option execution {execution.execution_id} carries no broker contract id and no "
            f"contract terms, so it cannot be told apart from any other {execution.symbol} "
            f"option. Recording it would merge unrelated strikes into one position"
        )

    reference = account_reference or mask_account(execution.account_id, visible=mask_visible)
    key = contract_key(
        contract_id=execution.contract_id,
        symbol=execution.symbol,
        security_type=execution.security_type,
        expiration=resolved.expiration,
        strike=resolved.strike,
        right=resolved.right,
        currency=execution.currency,
    )
    return ObservedFill(
        fill_id=fill_identifier(
            broker_execution_id=execution.execution_id,
            broker_order_id=execution.broker_order_id,
            contract=key,
            side=execution.side.value,
            quantity=str(execution.quantity),
            price=str(execution.price),
            executed_at=execution.executed_at,
        ),
        account_reference=reference,
        key=key,
        # Stripped, so a blank id is recorded as the absence it is rather than
        # as a whitespace identifier that would never match anything again.
        broker_execution_id=(execution.execution_id or "").strip() or None,
        broker_order_id=execution.broker_order_id,
        underlying=execution.symbol.strip().upper(),
        symbol=execution.symbol,
        asset_class=execution.security_type,
        contract_id=execution.contract_id,
        expiration=resolved.expiration,
        strike=resolved.strike,
        right=resolved.right,
        multiplier=resolved.multiplier,
        local_symbol=resolved.local_symbol,
        trading_class=resolved.trading_class,
        side=execution.side,
        quantity=execution.quantity,
        price=execution.price,
        commission=execution.commission,
        currency=execution.currency,
        executed_at=execution.executed_at,
        broker_timestamp=execution.as_of,
        observed_at=observed_at,
        broker_source=execution.source,
        execution_id=execution_id,
        allocation_id=allocation_id,
        opportunity_id=opportunity_id,
        provenance=provenance,
        identity_from_broker=bool((execution.execution_id or "").strip()),
        simulated=simulated,
    )


def to_observed_fills(
    executions: Sequence[BrokerExecution],
    *,
    observed_at: datetime,
    account_reference: str | None = None,
    mask_visible: int = 4,
    terms_by_contract: dict[int, ContractTerms] | None = None,
    execution_by_order: dict[str, str] | None = None,
    allocation_by_order: dict[str, str] | None = None,
    opportunity_by_order: dict[str, str] | None = None,
    simulated: bool = False,
) -> tuple[list[ObservedFill], list[str]]:
    """Normalise many, returning the fills and the executions that could not be.

    A refusal never stops the rest: one unidentifiable option fill is recorded
    as a translation failure and every other fill is still captured. Losing the
    whole batch because of one bad row is how a position ledger silently misses
    a real trade.
    """
    terms = terms_by_contract or {}
    executions_by_order = execution_by_order or {}
    allocations_by_order = allocation_by_order or {}
    opportunities_by_order = opportunity_by_order or {}

    fills: list[ObservedFill] = []
    refused: list[str] = []
    for execution in executions:
        order = execution.broker_order_id or ""
        linked = executions_by_order.get(order)
        try:
            fills.append(
                to_observed_fill(
                    execution,
                    observed_at=observed_at,
                    account_reference=account_reference,
                    mask_visible=mask_visible,
                    terms=terms.get(execution.contract_id or -1),
                    execution_id=linked,
                    allocation_id=allocations_by_order.get(order),
                    opportunity_id=opportunities_by_order.get(order),
                    provenance=(
                        AcquisitionProvenance.SYSTEM_EXECUTION
                        if linked
                        else AcquisitionProvenance.UNKNOWN
                    ),
                    simulated=simulated,
                )
            )
        except FillTranslationError as exc:
            refused.append(str(exc))
    unique, _ = deduplicate_fills(fills)
    return unique, refused


def deduplicate_fills(
    fills: Sequence[ObservedFill],
) -> tuple[list[ObservedFill], list[ObservedFill]]:
    """Split fills into first observations and repeats.

    Identity is :attr:`ObservedFill.fill_id`, which is the broker's execution
    id wherever there is one. The first observation wins: a later reading of
    the same fill adds no information, and preferring it would let a re-poll
    quietly change a recorded trade.
    """
    seen: dict[str, ObservedFill] = {}
    repeats: list[ObservedFill] = []
    for fill in fills:
        if fill.fill_id in seen:
            repeats.append(fill)
            continue
        seen[fill.fill_id] = fill
    ordered = sorted(seen.values(), key=lambda fill: (fill.executed_at, fill.fill_id))
    return ordered, repeats


def new_fills(
    observed: Sequence[ObservedFill], known: Iterable[str]
) -> tuple[list[ObservedFill], list[ObservedFill]]:
    """Partition observed fills into genuinely new ones and re-observations.

    What makes running reconciliation twice safe: the second run sees the same
    broker fills, recognises every one of them, and records nothing new.
    """
    existing = set(known)
    fresh = [fill for fill in observed if fill.fill_id not in existing]
    repeats = [fill for fill in observed if fill.fill_id in existing]
    return fresh, repeats


def aggregate_by_contract(fills: Sequence[ObservedFill]) -> dict[str, Decimal]:
    """Net signed quantity per instrument. Buys add, sells subtract.

    Explicit arithmetic rather than an assumption that everything is long: the
    shipped strategies are long-premium structures today, and a projection that
    hard-coded that would be wrong for the first exit this system performs.
    """
    totals: dict[str, Decimal] = {}
    for fill in fills:
        totals[fill.key] = totals.get(fill.key, Decimal("0")) + fill.signed_quantity
    return totals
