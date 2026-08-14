"""The deterministic exit policies, one function each.

Every function here is pure: it takes facts and configuration and returns an
:class:`~trading_system.exit.models.ExitPolicyOutcome`. No clock, no broker, no
repository, no model. That is what makes a stored evaluation reproducible — and
what makes each policy testable in isolation, which matters because the whole
milestone is the claim that these decisions are deterministic.

Expiration, trailing and thesis live in their own modules because each carries
real machinery. What is left is here:

``position_consistency``
    Does this position exist, do our records and the broker's agree about it,
    and is the structure whole?
``broker_observation``
    Was the broker actually read? A comparison against an absence of data is
    not a comparison.
``execution_state``
    Is an exit already working, or unresolved? The idempotency gate.
``contract_validity``
    Do we hold the contract terms an exit order would need?
``data_quality``
    Is the price this evaluation rests on usable at all?
``max_loss``
    Has the position lost more of its **declared** maximum than policy permits?
``take_profit``
    Has it made enough?

The two money policies deserve their reasons stated. ``max_loss`` reuses
Milestone 7's :class:`~trading_system.domain.enums.MaxLossBasis` and computes
nothing that Milestone 7 did not already define: a basis of ``NET_DEBIT_PAID``
means the most that can be lost is what was paid, so the loss is exactly
``entry cost - current value`` and the percentage is of the entry cost. A basis
of ``NOT_DEFINED`` is ``RISK_BASIS_UNAVAILABLE`` — a block, never an estimate,
because an unquantified loss is not a small one. And ``take_profit`` is
measured on the same basis so the two are directly comparable; a take-profit
computed one way and a maximum loss another would make "how far is this from
each" unanswerable.
"""

from __future__ import annotations

from decimal import Decimal

from trading_system.domain.enums import (
    BrokerReadStatus,
    ExitDecisionType,
    ExitPolicyKind,
    ExitQuoteField,
    ExitReasonCode,
    MaxLossBasis,
    PositionLifecycleState,
    StructureStatus,
)
from trading_system.exit.models import ExitPolicyOutcome, PositionValuation
from trading_system.infrastructure.settings import (
    ExitDataQualityConfig,
    ExitMaxLossConfig,
    ExitTakeProfitConfig,
    UnusableQuotePolicy,
)

__all__ = [
    "broker_observation",
    "contract_validity",
    "data_quality",
    "evaluate_max_loss",
    "evaluate_take_profit",
    "execution_state",
    "position_consistency",
]


# ---------------------------------------------------------------------------
# 1. Does this position exist, and do we and the broker agree about it?
# ---------------------------------------------------------------------------
def position_consistency(
    *,
    lifecycle_state: PositionLifecycleState,
    structure_status: StructureStatus,
    expected_quantity: int,
    observed_quantity: int | None,
    has_reconciliation_findings: bool = False,
    block_on_reconciliation_findings: bool = True,
) -> ExitPolicyOutcome:
    """The first policy, and the one that makes every later one meaningful.

    A position whose broker reality is disputed is one whose price, profit,
    trailing level and remaining time are all computed from a quantity that may
    be wrong. Judging it would be arithmetic on a guess, so this runs first and
    blocks.

    ``CLOSED`` is answered here too, and answered as a ``WAIT``: a position the
    broker no longer holds needs no decision, and calling that an exit would
    record a trade nobody made.
    """
    if structure_status is StructureStatus.UNKNOWN or observed_quantity is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.POSITION_STATE_UNKNOWN,
            measured=None,
            threshold=str(expected_quantity),
            summary="the broker's view of this position could not be established",
            detail=(
                "'we could not look' is not 'there is nothing there'. No exit decision is made "
                "against an unreadable position: every figure downstream would be computed "
                "from a quantity nobody confirmed"
            ),
            evaluated=False,
        )

    if structure_status is StructureStatus.PARTIAL:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.PARTIAL_STRUCTURE,
            measured=str(observed_quantity),
            threshold=str(expected_quantity),
            summary="the broker holds only part of this structure",
            detail=(
                "a straddle with one leg held is a naked long option: the risk of what is "
                "actually held is not the risk that was authorised, and no exit policy in this "
                "milestone was written for it. Reported so a person can decide"
            ),
        )

    # Checked *after* the two structural refusals above, deliberately. A
    # half-held straddle has zero complete units, and answering "closed" for it
    # would file a naked long option as a finished trade — the single most
    # misleading thing this policy could say.
    if lifecycle_state is PositionLifecycleState.CLOSED or observed_quantity == 0:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.POSITION_CLOSED,
            measured="0",
            threshold=str(expected_quantity),
            summary="the broker holds none of this structure; the position is closed",
            detail=(
                "terminal, and a no-op. Nothing here reopens a closed position: if the broker "
                "later reports contracts under these ids, that is a new position with its own "
                "history or a reconciliation finding, and either is better than a record that "
                "silently reopened"
            ),
        )

    if observed_quantity != expected_quantity:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.POSITION_QUANTITY_MISMATCH,
            measured=str(observed_quantity),
            threshold=str(expected_quantity),
            summary=(
                f"the broker holds {observed_quantity} unit(s) where confirmed fills imply "
                f"{expected_quantity}"
            ),
            detail=(
                "the broker is authoritative for what is held, and this stage does not adjust "
                "the internal ledger to agree. Reconcile first: an exit order sized against "
                "the wrong quantity either leaves a remainder or sells something else"
            ),
        )

    if has_reconciliation_findings and block_on_reconciliation_findings:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.RECONCILIATION_REQUIRED,
            measured=str(observed_quantity),
            threshold=str(expected_quantity),
            summary="an unresolved reconciliation finding concerns this position",
            detail=(
                "acting on a position whose broker state is disputed is acting on a guess. "
                "Resolve the finding — nothing here repairs one"
            ),
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.POSITION_CONSISTENCY,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.POLICY_SATISFIED,
        measured=str(observed_quantity),
        threshold=str(expected_quantity),
        summary=(
            f"the broker holds {observed_quantity} unit(s), matching what confirmed fills imply"
        ),
    )


# ---------------------------------------------------------------------------
# 2. Was the broker actually read?
# ---------------------------------------------------------------------------
def broker_observation(
    *, read_status: BrokerReadStatus, require_broker_confirmation: bool = True
) -> ExitPolicyOutcome:
    """Whether the broker answered at all.

    Carries Milestone 9's central distinction into this stage unchanged: an
    empty list means the account holds nothing and is a valid answer about it;
    an exception means we could not look, and is a fact about the connection.
    Only the first may be reconciled against, and only the first may be exited
    from.
    """
    if read_status.usable:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.BROKER_OBSERVATION,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.POLICY_SATISFIED,
            measured=read_status.value,
            summary=f"broker position state was read ({read_status.value})",
        )
    if not require_broker_confirmation:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.BROKER_OBSERVATION,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            measured=read_status.value,
            summary=f"broker position state is {read_status.value}; confirmation not required",
            evaluated=False,
        )
    return ExitPolicyOutcome(
        policy=ExitPolicyKind.BROKER_OBSERVATION,
        decision=ExitDecisionType.BLOCK,
        reason_code=ExitReasonCode.BROKER_DATA_UNAVAILABLE,
        measured=read_status.value,
        summary=f"broker position state could not be read ({read_status.value})",
        detail=(
            "this is NOT an empty account. Nothing here says the broker holds no positions, "
            "only that we were unable to look, and a position that cannot be seen cannot be "
            "sold"
        ),
        evaluated=False,
    )


# ---------------------------------------------------------------------------
# 3. Is an exit already in flight?
# ---------------------------------------------------------------------------
def execution_state(
    *,
    lifecycle_state: PositionLifecycleState,
    exit_execution_id: str | None = None,
    exit_execution_state: str | None = None,
) -> ExitPolicyOutcome:
    """The idempotency gate, and the most important policy in the module.

    Two situations mean *an exit order may be live right now*, and they are
    treated differently only in what they tell an operator:

    ``EXIT_SUBMITTED``
        A ``WAIT``, and a decisive one — the engine stops there. The order is
        working, waiting for it is the correct behaviour, and there is nothing
        wrong to resolve. Continuing to judge the position would produce an
        ``EXIT`` verdict for a position that is already being exited.
    ``EXIT_UNKNOWN``
        A ``BLOCK``. We sent something and never learned the outcome. No
        elapsed time turns that into a failure, and the only way out is to
        observe the broker.

    The ``UNKNOWN`` check reads the **execution ledger's** own state as well as
    the lifecycle, and that redundancy is load bearing. The lifecycle is a fold
    of this milestone's events and could in principle move on; the execution
    record is Milestone 8's permanent account of what was sent, and while it
    says ``UNKNOWN`` no exit decision may be acted on however the lifecycle
    reads. Checking only the lifecycle would let an unrelated later block move
    the position out of ``EXIT_UNKNOWN`` and quietly restore the ability to
    send a second order.
    """
    ledger_unknown = (exit_execution_state or "").upper() == "UNKNOWN"
    if lifecycle_state is PositionLifecycleState.EXIT_UNKNOWN or ledger_unknown:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXECUTION_STATE,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.EXIT_OUTCOME_UNKNOWN,
            measured=exit_execution_state or "UNKNOWN",
            summary=(
                f"exit execution {exit_execution_id or '(unnamed)'} was sent and its outcome "
                f"was never learned"
            ),
            detail=(
                "the order may be live at the broker right now. There is no path from here to "
                "a second submission: an UNKNOWN exit is resolved by observing the broker "
                "('execution explain --resolve' or 'reconciliation run'), never by sending "
                "again, and no amount of elapsed time is evidence"
            ),
        )

    if lifecycle_state is PositionLifecycleState.EXIT_SUBMITTED:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.EXECUTION_STATE,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.EXIT_ALREADY_SUBMITTED,
            measured=exit_execution_state or "SUBMITTED",
            summary=f"exit execution {exit_execution_id or '(unnamed)'} is working at the broker",
            detail=(
                "waiting for it is the whole correct behaviour. A second exit order would "
                "close this position twice — once at a price that was decided on and once at "
                "whatever the market does next"
            ),
        )

    # A ``BLOCKED`` lifecycle is deliberately *not* a block here. A block is
    # re-derived from current conditions on every evaluation rather than
    # remembered, so a position blocked last run because a research file could
    # not be read is judged afresh this run — and can still be force-exited at
    # its expiration deadline. Treating the state itself as blocking would let
    # a stale, unrelated condition suppress the most important policy in the
    # milestone, which is the one failure mode this whole ordering exists to
    # prevent. What must never be retried is a submission whose outcome is
    # unknown, and that is the branch above.
    return ExitPolicyOutcome(
        policy=ExitPolicyKind.EXECUTION_STATE,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.POLICY_SATISFIED,
        measured=lifecycle_state.value,
        summary="no exit order is outstanding for this position",
    )


# ---------------------------------------------------------------------------
# 4. Do we hold what an exit order needs?
# ---------------------------------------------------------------------------
def contract_validity(valuation: PositionValuation) -> ExitPolicyOutcome:
    """Whether an exit order could actually be built for this structure.

    Checked *before* any price policy, because a structure whose contract ids
    or multiplier are missing cannot be sold however attractive the exit looks.
    Every requirement here is one Milestone 8's order builder enforces on the
    way in, checked early so the answer is a named block rather than a build
    failure after a decision was recorded.
    """
    if not valuation.legs:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.CONTRACT_VALIDITY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.CONTRACT_METADATA_UNAVAILABLE,
            summary="this position reports no legs",
            evaluated=False,
        )

    missing_ids = [leg.leg_index for leg in valuation.legs if not leg.contract_id]
    if missing_ids:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.CONTRACT_VALIDITY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.CONTRACT_METADATA_UNAVAILABLE,
            measured=str(missing_ids),
            summary=f"legs {missing_ids} carry no broker contract id",
            detail=(
                "rebuilding one from symbol, strike and expiration would place an order for a "
                "contract nobody selected — the same refusal the entry side makes"
            ),
            evaluated=False,
        )

    if valuation.multiplier is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.CONTRACT_VALIDITY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.MULTIPLIER_UNAVAILABLE,
            summary="no contract multiplier is recorded for this structure",
            detail=(
                "never assumed to be 100. Without it, money and quoted terms cannot be "
                "converted, and a limit price off by a factor of a hundred is a hundredfold "
                "overpayment every downstream number would faithfully reproduce"
            ),
            evaluated=False,
        )

    multipliers = {leg.multiplier for leg in valuation.legs if leg.multiplier is not None}
    if len(multipliers) > 1:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.CONTRACT_VALIDITY,
            decision=ExitDecisionType.BLOCK,
            reason_code=ExitReasonCode.CONTRACT_METADATA_UNAVAILABLE,
            measured=str(sorted(m for m in multipliers if m is not None)),
            summary="the legs of this structure carry different multipliers",
            detail=(
                "the net price of a combo is only defined when every leg shares one "
                "multiplier, so no single exit order can express this structure"
            ),
            evaluated=False,
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.CONTRACT_VALIDITY,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.POLICY_SATISFIED,
        measured=str(len(valuation.legs)),
        summary=(
            f"{len(valuation.legs)} leg(s), each with a broker contract id and a shared "
            f"multiplier of {valuation.multiplier}"
        ),
    )


# ---------------------------------------------------------------------------
# 5. Is the price usable?
# ---------------------------------------------------------------------------
def data_quality(
    valuation: PositionValuation, *, config: ExitDataQualityConfig
) -> ExitPolicyOutcome:
    """Whether the price every later policy rests on may be used.

    Runs before maximum loss, take profit and the trailing stop because all
    three are functions of this one number. A stale or unusable price silently
    accepted here would produce a confident, wrong verdict from each of them.
    """
    if valuation.unpriced_legs or valuation.exit_quote is None:
        reason = (
            ExitReasonCode.QUOTE_FIELD_UNAVAILABLE
            if _any_quote_present(valuation)
            else ExitReasonCode.MARKET_DATA_UNAVAILABLE
        )
        quality_failure = any(
            leg.research_usable is False
            for leg in valuation.legs
            if leg.leg_index in valuation.unpriced_legs
        )
        if quality_failure:
            reason = ExitReasonCode.MARKET_DATA_QUALITY_FAILED
        return _unusable(
            config.on_unavailable,
            reason=reason,
            summary=(
                f"legs {valuation.unpriced_legs} carry no usable {valuation.quote_field.value}"
            ),
            detail=(
                f"{valuation.detail or ''} No other field is substituted: valuing a long "
                f"option at the ask, the last print or the price we paid because the bid is "
                f"missing invents a price no seller could get, and every trailing level, "
                f"take-profit target and maximum-loss figure derived from it would inherit "
                f"the invention"
            ).strip(),
        )

    age = valuation.max_quote_age_seconds
    if age is not None and age > config.max_quote_age_seconds:
        return _unusable(
            config.on_stale,
            reason=ExitReasonCode.MARKET_DATA_STALE,
            measured=f"{age:.0f}",
            threshold=str(config.max_quote_age_seconds),
            summary=(
                f"the stalest contributing quote is {age:.0f}s old, beyond the "
                f"{config.max_quote_age_seconds}s window"
            ),
            detail=(
                "a structure is only as fresh as its stalest leg. Measured against the "
                "evaluation's own as_of rather than wall clock, so a historical replay is not "
                "penalised for being run today"
            ),
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.DATA_QUALITY,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.POLICY_SATISFIED,
        measured=f"{age:.0f}" if age is not None else None,
        threshold=str(config.max_quote_age_seconds),
        summary=(
            f"{valuation.quote_field.value} available on every leg; structure quoted at "
            f"{valuation.exit_quote}"
        ),
    )


def _any_quote_present(valuation: PositionValuation) -> bool:
    """Whether a quote existed at all, as opposed to the field being absent.

    The distinction an operator needs: ``MARKET_DATA_UNAVAILABLE`` means
    collect some data, ``QUOTE_FIELD_UNAVAILABLE`` means the data arrived
    without the side this system trades out on.
    """
    return any(
        leg.bid is not None or leg.ask is not None or leg.last is not None for leg in valuation.legs
    )


def _unusable(
    policy: UnusableQuotePolicy,
    *,
    reason: ExitReasonCode,
    summary: str,
    detail: str | None = None,
    measured: str | None = None,
    threshold: str | None = None,
) -> ExitPolicyOutcome:
    """Apply the configured response to an unusable price.

    Two answers only. ``BLOCK`` refuses to judge; ``WAIT`` keeps the position
    and records that the evaluation had no price. Both are honest; substituting
    a different field is not, which is why there is no third branch.
    """
    if policy is UnusableQuotePolicy.BLOCK:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.DATA_QUALITY,
            decision=ExitDecisionType.BLOCK,
            reason_code=reason,
            measured=measured,
            threshold=threshold,
            summary=summary,
            detail=detail,
            evaluated=False,
        )
    return ExitPolicyOutcome(
        policy=ExitPolicyKind.DATA_QUALITY,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.NOT_EVALUATED,
        measured=measured,
        threshold=threshold,
        summary=f"{summary}; policy is WAIT, so the position is kept and not judged on price",
        detail=detail,
        evaluated=False,
    )


# ---------------------------------------------------------------------------
# 6. Has it lost too much?
# ---------------------------------------------------------------------------
def evaluate_max_loss(
    valuation: PositionValuation,
    *,
    basis: MaxLossBasis | None,
    max_loss_total: Decimal | None,
    effective_loss_pct: float,
    config: ExitMaxLossConfig,
) -> ExitPolicyOutcome:
    """Whether the position has given up more than policy permits.

    **Milestone 7's basis, reused rather than re-derived.** ``NET_DEBIT_PAID``
    means the most that can be lost is what was paid, so the loss is
    ``entry cost - current value`` and the percentage is of the entry cost —
    which is exactly Milestone 7's own arithmetic applied to what actually
    filled. Any other basis is a number this engine cannot compute, and
    computing one anyway is how a credit spread gets sized as though it could
    only lose its premium.

    ``effective_loss_pct`` is the strategy's where it narrowed the global
    ceiling. Configuration loading already refused any strategy that widened it.
    """
    if not config.enabled:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.MAX_LOSS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="the maximum-loss policy is switched off in configuration",
            evaluated=False,
        )

    if basis is None or basis is MaxLossBasis.NOT_DEFINED or max_loss_total is None:
        if config.block_on_unavailable_basis:
            return ExitPolicyOutcome(
                policy=ExitPolicyKind.MAX_LOSS,
                decision=ExitDecisionType.BLOCK,
                reason_code=ExitReasonCode.RISK_BASIS_UNAVAILABLE,
                measured=basis.value if basis else None,
                threshold=str(effective_loss_pct),
                summary=(
                    f"the maximum loss of this structure is "
                    f"{basis.value if basis else 'unknown'}, which this engine cannot compute"
                ),
                detail=(
                    "Milestone 7 declares each strategy's maximum-loss basis on its structure, "
                    "and a basis with no computable bound is a refusal there too. An "
                    "unquantified loss is not a small one, and nothing here estimates it"
                ),
                evaluated=False,
            )
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.MAX_LOSS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary="no computable maximum-loss basis; the policy was not evaluated",
            evaluated=False,
        )

    loss = _loss_pct(valuation)
    if loss is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.MAX_LOSS,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            threshold=str(effective_loss_pct),
            summary="no usable exit price, so the maximum-loss policy was not evaluated",
            evaluated=False,
        )

    if loss >= Decimal(str(effective_loss_pct)):
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.MAX_LOSS,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.MAX_LOSS_REACHED,
            measured=f"{loss:.4f}",
            threshold=str(effective_loss_pct),
            summary=(
                f"the position has lost {loss:.2f}% of its declared maximum "
                f"({max_loss_total}), at or beyond the {effective_loss_pct}% limit"
            ),
            detail=f"maximum-loss basis {basis.value}, as declared by the strategy structure",
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.MAX_LOSS,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.MAX_LOSS_NOT_REACHED,
        measured=f"{loss:.4f}",
        threshold=str(effective_loss_pct),
        summary=(
            f"the position has lost {loss:.2f}% of its declared maximum, below the "
            f"{effective_loss_pct}% limit"
        ),
    )


def _loss_pct(valuation: PositionValuation) -> Decimal | None:
    """How much of what was paid has been given up, as a percentage.

    Zero when the position is in profit rather than a negative number: "lost
    -30%" is a sentence nobody reads correctly, and the comparison against a
    positive limit would work either way.
    """
    entry, current = valuation.entry_cost, valuation.exit_value
    if entry is None or current is None or entry <= 0:
        return None
    given_up = entry - current
    if given_up <= 0:
        return Decimal("0")
    return given_up / entry * Decimal(100)


# ---------------------------------------------------------------------------
# 7. Has it made enough?
# ---------------------------------------------------------------------------
def evaluate_take_profit(
    valuation: PositionValuation,
    *,
    effective_return_pct: float | None,
    config: ExitTakeProfitConfig,
) -> ExitPolicyOutcome:
    """Whether the position has reached its profit target.

    Measured on the same basis as the maximum-loss policy — return over what
    was paid for one unit of the structure — so the two are directly
    comparable. A take-profit computed one way and a maximum loss another would
    make "how far is this position from each of its bounds" unanswerable, which
    is the question an operator actually asks.

    ``effective_return_pct`` of ``None`` means this strategy takes no profit
    target, which is permitted: take profit is not a safety limit, and a
    position that never takes one is still bounded by the trailing stop, the
    maximum loss and the expiration policy.
    """
    if not config.enabled or effective_return_pct is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TAKE_PROFIT,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            summary=(
                "take profit is switched off in configuration"
                if not config.enabled
                else "this strategy states no take-profit target"
            ),
            evaluated=False,
        )

    achieved = valuation.return_pct
    if achieved is None:
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TAKE_PROFIT,
            decision=ExitDecisionType.WAIT,
            reason_code=ExitReasonCode.NOT_EVALUATED,
            threshold=str(effective_return_pct),
            summary="no usable exit price, so the take-profit policy was not evaluated",
            evaluated=False,
        )

    if achieved >= Decimal(str(effective_return_pct)):
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.TAKE_PROFIT,
            decision=ExitDecisionType.EXIT,
            reason_code=ExitReasonCode.TAKE_PROFIT_REACHED,
            measured=f"{achieved:.4f}",
            threshold=str(effective_return_pct),
            summary=(
                f"the position is up {achieved:.2f}% on what was paid, at or beyond the "
                f"{effective_return_pct}% target"
            ),
        )

    return ExitPolicyOutcome(
        policy=ExitPolicyKind.TAKE_PROFIT,
        decision=ExitDecisionType.WAIT,
        reason_code=ExitReasonCode.TAKE_PROFIT_NOT_REACHED,
        measured=f"{achieved:.4f}",
        threshold=str(effective_return_pct),
        summary=(f"the position is at {achieved:.2f}% against a {effective_return_pct}% target"),
    )


#: Quote fields that describe a price a *seller* can actually get.
#:
#: Exported so ``exit validate`` can say plainly which configured field is the
#: honest one for a long position and which is an estimate, without the CLI
#: re-stating the reasoning.
SELLABLE_QUOTE_FIELDS: frozenset[ExitQuoteField] = frozenset({ExitQuoteField.BID})
