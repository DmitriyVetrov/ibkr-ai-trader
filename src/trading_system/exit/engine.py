"""The exit policy engine: precedence, and nothing else.

A **pure function of captured state**. No clock, no broker, no repository, no
market data request, no model — every fact it needs arrives on
:class:`ExitInputs`, which is what makes a stored evaluation reproducible from
the artifacts long after the configuration, the data and the position have all
moved on. The same discipline
:mod:`trading_system.reconciliation.engine` and
:mod:`trading_system.risk.engine` hold, and for the same reason.

What it does is run every policy in
:data:`~trading_system.domain.enums.EXIT_POLICY_PRECEDENCE` and combine their
outcomes under **one** rule:

.. code-block:: text

    the FIRST policy in precedence order that does not say WAIT decides.

That is the whole design, and it is worth reading twice, because two tempting
alternatives are both wrong.

**Precedence decides — a later block does not veto an earlier exit.** The
policies are ordered safety-first, so a position that is one day from expiry
*and* whose research report cannot be read exits on the expiration deadline.
The thesis is a secondary signal that the expiration policy does not depend on,
and letting a missing file suppress a force-exit would mean the most important
policy in the milestone could be disabled by deleting something unrelated to
it. Precedence is the answer to which policy governs, and it is answered once.

**An earlier block still beats a later exit, and that is the same rule.**
A position at its take-profit whose quantity the broker disputes blocks rather
than sells, because ``POSITION_CONSISTENCY`` sits at position 1 and
``TAKE_PROFIT`` at position 9 — the profit figure was computed from a quantity
nobody confirmed. Nothing special is needed to get this: it falls out of the
ordering.

**Every policy still runs.** Even after the decisive outcome is found, the
remaining policies are evaluated and their outcomes stored. It costs nothing —
these are pure functions over data already assembled — and it means an operator
resolving a block can see what the rest of the machinery thought, rather than
resolving it only to discover the next problem one run later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    EXIT_POLICY_PRECEDENCE,
    BrokerReadStatus,
    DataQuality,
    ExitDecisionType,
    ExitPolicyKind,
    ExitReasonCode,
    MaxLossBasis,
    PositionLifecycleState,
    StrategyType,
    StructureStatus,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.exit import policies
from trading_system.exit.expiration import ExpirationView, evaluate_expiration
from trading_system.exit.models import (
    ExitDecisionRecord,
    ExitEvaluation,
    ExitPolicyOutcome,
    ExitPolicySnapshot,
    PositionValuation,
    ThesisConditionCheck,
    TrailingStopRecord,
    exit_decision_identifier,
    exit_evaluation_identifier,
)
from trading_system.exit.thesis import ThesisView, evaluate_thesis
from trading_system.exit.trailing import evaluate_trailing
from trading_system.infrastructure.settings import ExitConfig

__all__ = ["ExitInputs", "ExitPolicyEngine", "evaluation_content_digest"]

#: Outcomes that settle an evaluation on their own, before the later policies
#: are consulted at all.
#:
#: Both are ``WAIT`` reasons, and both mean *there is nothing here to decide*:
#: the position is gone, or an exit for it is already working. Judging further
#: would compute a return, a trailing level and a profit target for a position
#: that no longer exists or is already being sold, and record a verdict about
#: it that never actually governed anything.
_SETTLED: frozenset[ExitReasonCode] = frozenset(
    {ExitReasonCode.POSITION_CLOSED, ExitReasonCode.EXIT_ALREADY_SUBMITTED}
)


@dataclass(frozen=True, slots=True)
class ExitInputs:
    """Everything one evaluation needs, captured before it starts.

    A dataclass rather than a model because it is never persisted: what is
    persisted is the :class:`~trading_system.exit.models.ExitEvaluation` this
    produces, which carries the same facts in a validated, versioned shape.
    Assembling it is the service's job; deciding from it is this module's, and
    the split is what keeps the deciding pure.
    """

    position_id: str
    underlying: str
    strategy: StrategyType
    as_of: datetime
    evaluated_at: datetime

    lifecycle_state: PositionLifecycleState
    structure_status: StructureStatus
    expected_quantity: int
    observed_quantity: int | None
    broker_read_status: BrokerReadStatus

    valuation: PositionValuation
    expiration: ExpirationView
    policy: ExitPolicySnapshot
    versions: SystemVersions
    trading_mode: TradingMode = TradingMode.PAPER

    trailing: TrailingStopRecord | None = None
    thesis: ThesisView = field(default_factory=ThesisView)
    thesis_checks: tuple[ThesisConditionCheck, ...] = ()

    max_loss_basis: MaxLossBasis | None = None
    max_loss_total: Decimal | None = None

    exit_execution_id: str | None = None
    exit_execution_state: str | None = None
    has_reconciliation_findings: bool = False

    # --- provenance: ids, never copies -------------------------------------
    entry_execution_id: str | None = None
    allocation_id: str | None = None
    opportunity_id: str | None = None
    campaign_id: str | None = None
    research_report_id: str | None = None
    strategy_decision_id: str | None = None
    contract_selection_id: str | None = None
    position_snapshot_id: str | None = None
    reconciliation_id: str | None = None

    #: Set when assembling the inputs itself hit a correctness problem — a
    #: look-ahead leak, an unreadable ledger. Carried rather than raised so the
    #: run records a named block for this position and keeps evaluating the
    #: others; one bad position must not stop a monitoring cycle.
    fatal_reason: ExitReasonCode | None = None
    fatal_detail: str | None = None

    @property
    def expiration_date(self) -> date | None:
        return self.expiration.expiration


def evaluation_content_digest(
    *,
    position_id: str,
    lifecycle_state: PositionLifecycleState,
    observed_quantity: int | None,
    exit_quote: Decimal | None,
    entry_quote: Decimal | None,
    days_to_expiration: int | None,
    trailing_state: str | None,
    trailing_stop_quote: Decimal | None,
    outcomes: Sequence[ExitPolicyOutcome],
    policy_version: str,
) -> str:
    """Hash what the decision actually rested on.

    Deliberately excludes every observation clock — ``evaluated_at``, quote
    retrieval times, the trailing record's ``updated_at`` — exactly as the data
    layer's snapshot identity and Milestone 9's reconciliation identity do. It
    answers one question: *is this the same judgement about the same state?*

    Re-evaluating unchanged state therefore lands on the same digest, and a
    second artifact is recognised as a re-observation rather than filed as a
    new decision that happens to agree.
    """
    return stable_hash(
        [
            "EXIT_EVALUATION_CONTENT",
            position_id,
            lifecycle_state.value,
            str(observed_quantity) if observed_quantity is not None else "",
            str(exit_quote) if exit_quote is not None else "",
            str(entry_quote) if entry_quote is not None else "",
            str(days_to_expiration) if days_to_expiration is not None else "",
            trailing_state or "",
            str(trailing_stop_quote) if trailing_stop_quote is not None else "",
            [
                f"{o.policy.value}:{o.decision.value}:{o.reason_code.value}:"
                f"{o.measured or ''}:{o.threshold or ''}"
                for o in outcomes
            ],
            policy_version,
        ]
    )


class ExitPolicyEngine:
    """Runs the deterministic exit policies and combines their verdicts.

    Constructed with configuration and nothing else. It holds no repository, no
    broker, no clock and no client, and a boundary test walks the import graph
    to prove it cannot reach one.
    """

    def __init__(self, config: ExitConfig) -> None:
        self._config = config

    @property
    def config(self) -> ExitConfig:
        return self._config

    @property
    def precedence(self) -> tuple[ExitPolicyKind, ...]:
        return EXIT_POLICY_PRECEDENCE

    # --- evaluation --------------------------------------------------------
    def evaluate(self, inputs: ExitInputs) -> ExitEvaluation:
        """Run every policy, in precedence order, and record what each said."""
        outcomes = (
            [self._fatal_outcome(inputs)]
            if inputs.fatal_reason is not None
            else self._run_policies(inputs)
        )

        digest = evaluation_content_digest(
            position_id=inputs.position_id,
            lifecycle_state=inputs.lifecycle_state,
            observed_quantity=inputs.observed_quantity,
            exit_quote=inputs.valuation.exit_quote,
            entry_quote=inputs.valuation.entry_quote,
            days_to_expiration=inputs.expiration.dte,
            trailing_state=inputs.trailing.state.value if inputs.trailing else None,
            trailing_stop_quote=inputs.trailing.stop_quote if inputs.trailing else None,
            outcomes=outcomes,
            policy_version=inputs.policy.policy_version,
        )
        return ExitEvaluation(
            evaluation_id=exit_evaluation_identifier(
                position_id=inputs.position_id,
                as_of=inputs.as_of,
                content_digest=digest,
                policy_version=inputs.policy.policy_version,
            ),
            position_id=inputs.position_id,
            as_of=inputs.as_of,
            evaluated_at=inputs.evaluated_at,
            underlying=inputs.underlying,
            strategy=inputs.strategy,
            lifecycle_state=inputs.lifecycle_state,
            structure_status=inputs.structure_status,
            open_quantity=max(inputs.observed_quantity or 0, 0),
            days_to_expiration=inputs.expiration.dte,
            expiration=inputs.expiration.expiration,
            max_loss_basis=inputs.max_loss_basis,
            max_loss_total=inputs.max_loss_total,
            valuation=inputs.valuation,
            trailing=inputs.trailing,
            thesis_checks=list(inputs.thesis_checks),
            policy=inputs.policy,
            outcomes=outcomes,
            entry_execution_id=inputs.entry_execution_id,
            allocation_id=inputs.allocation_id,
            opportunity_id=inputs.opportunity_id,
            campaign_id=inputs.campaign_id,
            research_report_id=inputs.research_report_id,
            strategy_decision_id=inputs.strategy_decision_id,
            contract_selection_id=inputs.contract_selection_id,
            position_snapshot_id=inputs.position_snapshot_id,
            reconciliation_id=inputs.reconciliation_id,
            content_hash=digest,
            versions=inputs.versions,
            orders_submitted=0,
        )

    def _run_policies(self, inputs: ExitInputs) -> list[ExitPolicyOutcome]:
        """Every policy, in the one order that is ever used.

        The order is read from :data:`EXIT_POLICY_PRECEDENCE` rather than being
        the order these calls happen to appear in, so the reviewable list and
        the executed order cannot drift apart. A test asserts the outcomes come
        back in exactly that order.
        """
        policy = inputs.policy
        consistency = policies.position_consistency(
            lifecycle_state=inputs.lifecycle_state,
            structure_status=inputs.structure_status,
            expected_quantity=inputs.expected_quantity,
            observed_quantity=inputs.observed_quantity,
            has_reconciliation_findings=inputs.has_reconciliation_findings,
            block_on_reconciliation_findings=self._config.block_on_reconciliation_findings,
        )
        if consistency.reason_code in _SETTLED:
            # A short circuit, and not an optimisation. A position the broker no
            # longer holds is not a thing to judge: its return, its remaining
            # time and its maximum loss are all arithmetic over a holding of
            # zero, and the money policies would report an unavailable risk
            # basis and block a position that is simply gone.
            return [consistency]

        execution = policies.execution_state(
            lifecycle_state=inputs.lifecycle_state,
            exit_execution_id=inputs.exit_execution_id,
            exit_execution_state=inputs.exit_execution_state,
        )
        if execution.reason_code in _SETTLED:
            # The other short circuit, and this one is a safety property. An
            # exit order is already working: continuing would evaluate the
            # take-profit and the trailing stop against a position that is
            # being sold, and record an ``EXIT`` verdict for it. Nothing would
            # send a second order — the lifecycle refuses that — but the stored
            # decision would say the system decided to exit a position it had
            # already decided to exit, which is not what happened.
            return [consistency, execution]

        by_kind: dict[ExitPolicyKind, ExitPolicyOutcome] = {
            ExitPolicyKind.POSITION_CONSISTENCY: consistency,
            ExitPolicyKind.BROKER_OBSERVATION: policies.broker_observation(
                read_status=inputs.broker_read_status,
                require_broker_confirmation=self._config.require_broker_confirmation,
            ),
            ExitPolicyKind.EXECUTION_STATE: execution,
            ExitPolicyKind.CONTRACT_VALIDITY: policies.contract_validity(inputs.valuation),
            ExitPolicyKind.EXPIRATION: evaluate_expiration(
                inputs.expiration,
                config=self._config.expiration,
                force_exit_dte=policy.expiration_force_exit_dte,
            ),
            ExitPolicyKind.DATA_QUALITY: policies.data_quality(
                inputs.valuation, config=self._config.data_quality
            ),
            ExitPolicyKind.MAX_LOSS: policies.evaluate_max_loss(
                inputs.valuation,
                basis=inputs.max_loss_basis,
                max_loss_total=inputs.max_loss_total,
                effective_loss_pct=policy.max_loss_pct,
                config=self._config.max_loss,
            ),
            ExitPolicyKind.THESIS: evaluate_thesis(
                inputs.thesis, inputs.thesis_checks, config=self._config.thesis
            ),
            ExitPolicyKind.TAKE_PROFIT: policies.evaluate_take_profit(
                inputs.valuation,
                effective_return_pct=policy.take_profit_return_pct,
                config=self._config.take_profit,
            ),
            ExitPolicyKind.TRAILING_STOP: (
                evaluate_trailing(
                    inputs.trailing,
                    observed_quote=inputs.valuation.exit_quote,
                    enabled=policy.trailing_enabled,
                )
                if inputs.trailing is not None
                else ExitPolicyOutcome(
                    policy=ExitPolicyKind.TRAILING_STOP,
                    decision=ExitDecisionType.WAIT,
                    reason_code=ExitReasonCode.NOT_EVALUATED,
                    summary="no trailing state exists for this position yet",
                    evaluated=False,
                )
            ),
        }
        return [by_kind[kind] for kind in EXIT_POLICY_PRECEDENCE]

    def _fatal_outcome(self, inputs: ExitInputs) -> ExitPolicyOutcome:
        """One block, for a position whose inputs could not be assembled.

        Recorded as a ``POSITION_CONSISTENCY`` block because that is the policy
        that owns "can this position be judged at all", and running the rest
        against inputs known to be wrong would produce nine confident verdicts
        computed from bad data.
        """
        assert inputs.fatal_reason is not None
        return ExitPolicyOutcome(
            policy=ExitPolicyKind.POSITION_CONSISTENCY,
            decision=ExitDecisionType.BLOCK,
            reason_code=inputs.fatal_reason,
            summary=f"this position could not be evaluated: {inputs.fatal_reason.value}",
            detail=inputs.fatal_detail,
            evaluated=False,
        )

    # --- the decision ------------------------------------------------------
    def decide(
        self, evaluation: ExitEvaluation, *, trading_mode: TradingMode
    ) -> ExitDecisionRecord:
        """Combine the outcomes into one verdict. Pure, and total.

        Every branch is reachable from stored data alone, so replaying an
        evaluation reproduces its decision without the configuration that
        produced it.
        """
        valuation = evaluation.valuation
        # The outcomes arrive in precedence order, and the first one that does
        # not say WAIT decides. One rule, applied once.
        decisive = next(
            (o for o in evaluation.outcomes if o.decision is not ExitDecisionType.WAIT), None
        )

        if decisive is None:
            decision = ExitDecisionType.WAIT
            chosen = _most_informative_wait(evaluation.outcomes)
            reasons = [chosen.reason_code]
            triggering = chosen.policy
            summary = f"WAIT: {chosen.summary}"
            action = None
        elif decisive.decision is ExitDecisionType.BLOCK:
            decision = ExitDecisionType.BLOCK
            # Every block is reported, not only the decisive one: an operator
            # resolving this should not have to rediscover the next problem on
            # the following run.
            reasons = [
                outcome.reason_code
                for outcome in evaluation.outcomes
                if outcome.decision is ExitDecisionType.BLOCK
            ]
            triggering = decisive.policy
            summary = f"BLOCKED: {decisive.summary}"
            action = (
                "ACTION REQUIRED: resolve the condition above before this position is judged "
                "again. Nothing here retries around it, and no exit order was built"
            )
        else:
            decision = ExitDecisionType.EXIT
            reasons = [decisive.reason_code]
            triggering = decisive.policy
            summary = f"EXIT ({decisive.policy.value}): {decisive.summary}"
            action = None

        return ExitDecisionRecord(
            decision_id=exit_decision_identifier(evaluation_id=evaluation.evaluation_id),
            evaluation_id=evaluation.evaluation_id,
            position_id=evaluation.position_id,
            as_of=evaluation.as_of,
            decided_at=evaluation.evaluated_at,
            decision=decision,
            reason_codes=reasons,
            triggering_policy=triggering,
            underlying=evaluation.underlying,
            strategy=evaluation.strategy,
            lifecycle_state=evaluation.lifecycle_state,
            quantity=evaluation.open_quantity,
            close_whole_strategy=True,
            exit_quote=valuation.exit_quote,
            exit_value=valuation.exit_value,
            entry_cost=valuation.entry_cost,
            unrealized_pnl=valuation.unrealized_pnl,
            return_pct=(
                f"{valuation.return_pct:.4f}" if valuation.return_pct is not None else None
            ),
            days_to_expiration=evaluation.days_to_expiration,
            quote_field=valuation.quote_field,
            data_quality=_worst_quality(valuation),
            currency=valuation.currency,
            summary=summary,
            detail=_detail_of(evaluation, decision),
            recommended_action=action,
            policy_version=evaluation.policy.policy_version,
            entry_execution_id=evaluation.entry_execution_id,
            opportunity_id=evaluation.opportunity_id,
            campaign_id=evaluation.campaign_id,
            trading_mode=trading_mode,
            versions=evaluation.versions,
        )


def _most_informative_wait(outcomes: Sequence[ExitPolicyOutcome]) -> ExitPolicyOutcome:
    """Which ``WAIT`` to report as the headline.

    A run where nothing triggered still has something specific to say, and
    ``POLICY_SATISFIED`` from the first policy is the least of it. Preference
    goes to the outcomes that carry real information about *how close* the
    position is: an armed trailing stop, an expiration warning, a profit target
    being approached. Deterministic and total — ties break on precedence order,
    and the last resort is the first outcome, which always exists.
    """
    ranked = (
        ExitReasonCode.EXPIRATION_WARNING,
        ExitReasonCode.TRAILING_ABOVE_STOP,
        ExitReasonCode.EXIT_ALREADY_SUBMITTED,
        ExitReasonCode.POSITION_CLOSED,
        ExitReasonCode.TRAILING_NOT_ACTIVE,
        ExitReasonCode.TAKE_PROFIT_NOT_REACHED,
        ExitReasonCode.MAX_LOSS_NOT_REACHED,
        ExitReasonCode.THESIS_INTACT,
        ExitReasonCode.EXPIRATION_NOT_REACHED,
    )
    for reason in ranked:
        for outcome in outcomes:
            if outcome.reason_code is reason:
                return outcome
    return outcomes[0]


def _worst_quality(valuation: PositionValuation) -> DataQuality | None:
    """The least good quality classification across the contributing legs.

    A structure is only as trustworthy as its worst leg, exactly as it is only
    as fresh as its stalest one.
    """
    order = [DataQuality.OK, DataQuality.DEGRADED, DataQuality.STALE, DataQuality.UNUSABLE]
    seen = [leg.data_quality for leg in valuation.legs if leg.data_quality is not None]
    if not seen:
        return None
    return max(seen, key=order.index)


def _detail_of(evaluation: ExitEvaluation, decision: ExitDecisionType) -> str:
    """A one-paragraph account of every policy that had something to say.

    Verbose on purpose. This is read by a person deciding whether to intervene
    in a trading account, and every line here is one they would otherwise have
    to reconstruct from four ledgers.
    """
    lines = [
        f"{outcome.policy.value}: {outcome.decision.value} ({outcome.reason_code.value})"
        + (f" measured={outcome.measured}" if outcome.measured else "")
        + (f" threshold={outcome.threshold}" if outcome.threshold else "")
        for outcome in evaluation.outcomes
    ]
    header = {
        ExitDecisionType.BLOCK: (
            "no exit decision could be made: the first policy in precedence order that did not "
            "say WAIT was a block. Every policy below was still evaluated, so resolving this "
            "does not mean discovering the next problem one run later"
        ),
        ExitDecisionType.EXIT: (
            "precedence decides, and it is ordered safety before profit-taking: the first "
            "policy that did not say WAIT was a trigger, and it is the one recorded. A block "
            "from a LATER policy does not veto it — the exit does not depend on that policy"
        ),
        ExitDecisionType.WAIT: "every applicable policy was evaluated and none triggered",
    }[decision]
    return header + ". " + "; ".join(lines)
