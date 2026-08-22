"""The execution service: the composition root for Milestone 8.

One stage, and the narrowest boundary in the system at its far end:

.. code-block:: text

    allocation run (M7)          <- consumed, never re-decided
          |
    approved authorisation       <- APPROVED only; every other outcome refused
          |
    ExecutionValidator           <- deterministic, no broker, injected clock
          |
    PurchaseCardFactory          <- the M1 card and risk decision, minted
          |
    OrderBuilder                 <- the only place a price becomes an order
          |
    idempotency check            <- has this exact trade already been sent?
          |
    ExecutionEngine + Broker     <- one submission, one short-lived connection
          |
    immutable execution record   <- written BEFORE the send, appended after

Properties this service holds regardless of which path it takes:

* **It re-decides nothing.** No research, no strategy selection, no contract
  selection, no risk evaluation, no sizing. The quantity, the capital and the
  maximum loss are read from the authorisation and copied; a validator on the
  record refuses figures that disagree with it.
* **It consults no model.** No LLM client parameter, no prompt, no agent
  import. Execution translates an approved deterministic artifact into an
  order, and there is nothing here for a model to decide.
* **It cannot submit by accident.** Two independent switches must both be on:
  ``execution.enabled`` in configuration, and an explicit
  :class:`~trading_system.execution.models.ExecutionRequest` carrying
  ``execution_authorized=True``. A dry run builds everything and reaches no
  broker at all — it never even constructs one.
* **It fails closed, per authorisation.** A refused precondition ends *that*
  execution with a named reason and no order. One refusal never stops the rest,
  and an ambiguous submission stops that authorisation permanently rather than
  being retried.
* **It never retries.** There is no code path from a timeout to a second
  submission. Resolution is by observation, in
  :meth:`ExecutionService.resolve`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system import __version__ as application_version
from trading_system.allocation.models import AllocationRunResult, CampaignAllocation
from trading_system.allocation.store import (
    AllocationRepository,
    FilesystemAllocationRepository,
)
from trading_system.data.market_calendar import MarketCalendar
from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionRunStatus,
    ExecutionState,
    LegAction,
    OrderType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import (
    OptionLeg,
    OrderIntent,
    PurchaseCard,
    ResearchReport,
    RiskDecision,
    StrategyDecision,
    SystemVersions,
)
from trading_system.execution.execution_engine import ExecutionEngine, SubmissionOutcome
from trading_system.execution.models import (
    EXECUTION_SCHEMA_VERSION,
    ExecutionLeg,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionRunCounts,
    ExecutionRunResult,
    cleanup_execution_request_identifier,
    execution_identifier,
    execution_request_identifier,
    execution_run_identifier,
)
from trading_system.execution.order_builder import (
    OrderBuildError,
    build_cleanup_order_intent,
    build_exit_order_intent,
    build_order_intent,
    reference_quote_of,
)
from trading_system.execution.purchase_card import (
    PurchaseCardError,
    build_execution_legs,
    build_purchase_card,
    build_risk_decision,
)
from trading_system.execution.store import (
    ExecutionHistoryEntry,
    ExecutionRepository,
    ExecutionRunHistoryEntry,
    FilesystemExecutionRepository,
    has_live_attempt,
)
from trading_system.execution.validation import ExecutionValidation, ExecutionValidator
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    TRADING_CONTRACT_ID,
    TRADING_EXECUTION_ID,
    TRADING_POSITION_ID,
    TRADING_STATUS,
)
from trading_system.observability.instrument import traced
from trading_system.research.store import FilesystemResearchRepository, ResearchRepository
from trading_system.strategies.store import FilesystemStrategyRepository, StrategyRepository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from trading_system.broker.base import Broker
    from trading_system.cleanup.models import CleanupTarget
    from trading_system.exit.models import ExitRequest

__all__ = [
    "CleanupSubmission",
    "ExecutionPlan",
    "ExecutionRun",
    "ExecutionService",
    "ExitSubmission",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Everything built for one authorisation, before anything is sent.

    This is what ``--dry-run`` shows. It is deliberately a complete answer to
    "what exactly would be submitted": the card, the risk decision, the order
    intent with its derived limit price, and the validation verdict. A dry run
    that showed less would be a weaker promise than the one this milestone
    makes.
    """

    allocation: CampaignAllocation
    request: ExecutionRequest
    validation: ExecutionValidation
    card: PurchaseCard | None = None
    risk_decision: RiskDecision | None = None
    intent: OrderIntent | None = None
    reason_codes: tuple[ExecutionReasonCode, ...] = ()
    detail: str | None = None

    @property
    def submittable(self) -> bool:
        return self.validation.ok and self.intent is not None and not self.reason_codes


@dataclass(frozen=True, slots=True)
class ExitSubmission:
    """What one exit submission attempt produced.

    ``record`` is ``None`` only when nothing was built at all — the request was
    unauthorised, execution is disabled, the market is closed, or an existing
    attempt may already be live. In every other case there is a record, and it
    says what happened whether or not an order left the process.
    """

    request: ExitRequest
    record: ExecutionRecord | None = None
    intent: OrderIntent | None = None
    reason_codes: tuple[ExecutionReasonCode, ...] = ()
    detail: str | None = None
    #: An attempt already on file that blocked this one. Named so the caller
    #: can report *which* order may be live rather than only that one may be.
    existing: ExecutionRecord | None = None
    orders_submitted: int = 0
    dry_run: bool = False

    @property
    def submitted(self) -> bool:
        """Whether an order may exist at the broker because of this attempt."""
        return self.record is not None and self.record.submitted and not self.dry_run

    @property
    def refused(self) -> bool:
        return self.record is None or bool(self.reason_codes)


@dataclass(frozen=True, slots=True)
class CleanupSubmission:
    """What one orphan-cleanup submission attempt produced.

    The same shape as :class:`ExitSubmission`, deliberately: the two are
    different *authorisations* for the same act, and a caller that can report
    one can report the other.
    """

    target_key: str
    record: ExecutionRecord | None = None
    intent: OrderIntent | None = None
    reason_codes: tuple[ExecutionReasonCode, ...] = ()
    detail: str | None = None
    #: An attempt already on file that blocked this one.
    existing: ExecutionRecord | None = None
    orders_submitted: int = 0
    dry_run: bool = False

    @property
    def submitted(self) -> bool:
        return self.record is not None and self.record.submitted and not self.dry_run

    @property
    def refused(self) -> bool:
        return self.record is None or bool(self.reason_codes)


def _reached_the_broker(records: Sequence[ExecutionRecord]) -> ExecutionRecord | None:
    """Any earlier attempt under this identity that may have reached a broker.

    Stricter than :func:`~trading_system.execution.store.has_live_attempt`, and
    the extra strictness is the whole idempotency guarantee for a cleanup.
    ``has_live_attempt`` asks *is an order in flight* — so a ``FILLED`` record
    does not block, which is right for an entry (the position exists; nothing
    would re-submit it) and wrong here. A cleanup whose fill has not yet
    propagated to the position list would otherwise be sent a second time, and
    a second sale of a holding of one contract is a short position of one.

    What does *not* block: an attempt that provably never left the process.
    ``FAILED`` and ``REJECTED`` with nothing at the broker are exactly that,
    and blocking on them would make a transient refusal permanent.
    """
    for record in sorted(records, key=lambda item: item.created_at):
        if record.dry_run:
            continue
        if record.submitted or record.broker_order_id or record.filled_quantity:
            return record
    return None


def _closing_execution_leg(leg: ExecutionLeg) -> ExecutionLeg:
    """The same contract, in the opposite direction.

    Only ``action`` changes; the contract id, strike, expiration, ratio and
    multiplier are the ones the entry order actually used. The mirror of
    ``order_builder._closing_leg``, applied to the stored record rather than to
    the intent, so the ledger and the order agree about what was sent.
    """
    closing = LegAction.SELL if leg.action is LegAction.BUY else LegAction.BUY
    return leg.model_copy(update={"action": closing})


def _option_leg_of(leg: ExecutionLeg) -> OptionLeg:
    """Translate a stored execution leg back into the Milestone 1 leg shape.

    Nothing is normalised, derived or defaulted: the contract id, strike,
    expiration, ratio and multiplier are the ones the entry order actually
    used, because a closing order for anything else would open a new position
    rather than end this one.
    """
    return OptionLeg(
        underlying=leg.underlying,
        right=leg.right,
        strike=leg.strike,
        expiration=leg.expiration,
        action=leg.action,
        ratio=leg.ratio,
        multiplier=leg.multiplier,
        occ_symbol=leg.local_symbol,
        broker_contract_id=leg.contract_id,
    )


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    """One run's outcome, plus what the caller needs to report it."""

    result: ExecutionRunResult
    plans: tuple[ExecutionPlan, ...]
    stored: bool
    dry_run: bool
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status in (ExecutionRunStatus.SUCCESS, ExecutionRunStatus.DRY_RUN)

    @property
    def orders_submitted(self) -> int:
        return self.result.orders_submitted


class ExecutionService:
    """Builds and drives order submission from stored authorisations.

    Constructs a broker **only** when actually submitting, and only through
    ``broker.factory.build_execution_broker`` — the one writable constructor in
    the system. A dry run never reaches that call, so "a dry run cannot place an
    order" is structural rather than a flag that has to be checked correctly.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        execution_repository: ExecutionRepository | None = None,
        allocation_repository: AllocationRepository | None = None,
        research_repository: ResearchRepository | None = None,
        strategy_repository: StrategyRepository | None = None,
        broker_factory: object | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._repository = execution_repository or FilesystemExecutionRepository(
            data_root / "execution"
        )
        self._allocation_repository = allocation_repository or FilesystemAllocationRepository(
            data_root / "allocation"
        )
        self._research_repository = research_repository or FilesystemResearchRepository(
            data_root / "research"
        )
        self._strategy_repository = strategy_repository or FilesystemStrategyRepository(
            data_root / "strategy"
        )
        #: Injected so a test can supply a controlled broker without patching a
        #: module. Defaults to the writable-broker factory, which refuses every
        #: mode except PAPER and the simulator.
        self._broker_factory = broker_factory
        self._calendar = MarketCalendar(config.data.market_calendar)
        self._validator = ExecutionValidator(
            config=config.execution, campaign=config.campaign, calendar=self._calendar
        )

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ExecutionRepository:
        return self._repository

    @property
    def validator(self) -> ExecutionValidator:
        return self._validator

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def enabled(self) -> bool:
        """Whether configuration permits a submission at all."""
        return self._config.execution.enabled

    def history(self, limit: int | None = None) -> list[ExecutionHistoryEntry]:
        return self._repository.history(limit=limit)

    def run_history(self, limit: int | None = None) -> list[ExecutionRunHistoryEntry]:
        return self._repository.run_history(limit=limit)

    def get(self, execution_id: str) -> ExecutionRecord | None:
        return self._repository.current(execution_id)

    def get_run(self, run_id: str) -> ExecutionRunResult | None:
        return self._repository.get_run(run_id)

    def latest_run(self) -> ExecutionRunResult | None:
        return self._repository.latest_run()

    def allocation_run(self, run_id: str | None = None) -> AllocationRunResult | None:
        if run_id is not None:
            return self._allocation_repository.get(run_id)
        return self._allocation_repository.latest()

    def find_allocation(self, allocation_id: str) -> CampaignAllocation | None:
        """Locate one authorisation by id, across every stored allocation run."""
        for run in reversed(self._allocation_repository.all_runs()):
            for allocation in run.allocations:
                if allocation.allocation_id == allocation_id:
                    return allocation
        return None

    def versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            strategy_spec_version=EXECUTION_SCHEMA_VERSION,
        )

    # --- planning (never submits) ------------------------------------------
    def plan(
        self,
        allocation: CampaignAllocation,
        *,
        execution_now: datetime | None = None,
        dry_run: bool = False,
        observed_unit_price: Decimal | None = None,
    ) -> ExecutionPlan:
        """Build and validate everything for one authorisation, sending nothing.

        Pure with respect to the broker: this method cannot submit, has no
        broker to submit with, and is what ``--dry-run`` calls.
        """
        now = execution_now or self._clock.now()
        policy = self._config.execution
        order_type = policy.default_order_type
        request = self._request_for(allocation, order_type=order_type, dry_run=dry_run, at=now)

        validation = self._validator.validate(
            allocation,
            execution_now=now,
            trading_mode=self._settings.trading_mode,
            order_type=order_type,
            dry_run=dry_run,
            observed_unit_price=observed_unit_price,
        )
        if not validation.ok:
            return ExecutionPlan(
                allocation=allocation,
                request=request,
                validation=validation,
                reason_codes=tuple(validation.reason_codes),
                detail=validation.detail,
            )

        try:
            research, strategy = self._provenance(allocation)
        except PurchaseCardError as exc:
            return ExecutionPlan(
                allocation=allocation,
                request=request,
                validation=validation,
                reason_codes=(exc.reason_code,),
                detail=str(exc),
            )

        try:
            card = build_purchase_card(
                allocation,
                research=research,
                strategy=strategy,
                created_at=now,
                versions=self.versions(),
                campaign_currency=self._config.campaign.target_currency,
            )
            risk_decision = build_risk_decision(
                allocation, purchase_card_id=card.card_id, versions=self.versions()
            )
        except PurchaseCardError as exc:
            return ExecutionPlan(
                allocation=allocation,
                request=request,
                validation=validation,
                reason_codes=(exc.reason_code,),
                detail=str(exc),
            )

        assert allocation.unit_cost is not None  # narrowing; the validator checked
        try:
            intent = build_order_intent(
                card,
                execution_request_id=request.execution_request_id,
                risk_decision_id=risk_decision.decision_id,
                reference_price=allocation.unit_cost,
                config=policy,
                order_type=order_type,
                time_in_force=policy.time_in_force,
                trading_mode=self._settings.trading_mode,
                created_at=now,
                versions=self.versions(),
            )
        except OrderBuildError as exc:
            return ExecutionPlan(
                allocation=allocation,
                request=request,
                validation=validation,
                card=card,
                risk_decision=risk_decision,
                reason_codes=(exc.reason_code,),
                detail=str(exc),
            )

        return ExecutionPlan(
            allocation=allocation,
            request=request,
            validation=validation,
            card=card,
            risk_decision=risk_decision,
            intent=intent,
        )

    # --- the run -----------------------------------------------------------
    @traced(
        "execution.open",
        count=_metrics.EXECUTION_SUBMISSIONS_TOTAL,
        duration=_metrics.EXECUTION_DURATION,
        failure_count=_metrics.EXECUTION_REJECTIONS_TOTAL,
        result_attributes=lambda run: {TRADING_STATUS: run.result.status.value},
        labels=lambda run: {
            "status": run.result.status.value,
            "execution_type": "OPEN",
            "trading_mode": run.result.trading_mode.value,
        },
    )
    def run(
        self,
        *,
        allocation_ids: Sequence[str] | None = None,
        allocation_run_id: str | None = None,
        symbols: Sequence[str] | None = None,
        dry_run: bool = False,
        authorized: bool = False,
        execution_now: datetime | None = None,
        broker: Broker | None = None,
    ) -> ExecutionRun:
        """Submit approved authorisations, or show what would be submitted.

        ``authorized`` is the deliberate execution request (brief section 4).
        Without it, and outside a dry run, nothing is built and nothing is
        sent — an allocation id alone never means "trade this".
        """
        started = time.perf_counter()
        now = execution_now or self._clock.now()
        policy = self._config.execution

        if not dry_run and not authorized:
            return self._empty(
                ExecutionRunStatus.NOT_AUTHORIZED,
                detail=(
                    "no execution authorisation was given. Naming an allocation is not "
                    "permission to send an order: pass --confirm to authorise, or --dry-run "
                    "to inspect what would be submitted"
                ),
                dry_run=dry_run,
                started=started,
                as_of=now,
            )
        if not dry_run and not policy.enabled:
            return self._empty(
                ExecutionRunStatus.EXECUTION_DISABLED,
                detail=(
                    "execution.enabled is false in config/execution.yaml. Submission is "
                    "switched off at the system level and no authorisation overrides that"
                ),
                dry_run=dry_run,
                started=started,
                as_of=now,
            )

        allocations, selection_detail = self._select(
            allocation_ids=allocation_ids, allocation_run_id=allocation_run_id, symbols=symbols
        )
        if selection_detail is not None:
            return self._empty(
                ExecutionRunStatus.CONFIGURATION_ERROR,
                detail=selection_detail,
                dry_run=dry_run,
                started=started,
                as_of=now,
                allocation_run_id=allocation_run_id,
            )
        if not allocations:
            return self._empty(
                ExecutionRunStatus.NOTHING_TO_EXECUTE,
                detail=(
                    "no approved authorisation was found to execute. A campaign with nothing "
                    "outstanding is the ordinary answer, not a failure"
                ),
                dry_run=dry_run,
                started=started,
                as_of=now,
                allocation_run_id=allocation_run_id,
            )

        plans = [self.plan(a, execution_now=now, dry_run=dry_run) for a in allocations]
        # Captured before anything is sent. A run over authorisations that have
        # since been submitted is a *different* decision reaching a different
        # answer, so the ledger's state is part of the run's identity — the same
        # reason an allocation run's id includes the campaign's committed state.
        # Without it, a re-run would collide with the run it is repeating and
        # the immutable store would refuse the second.
        ledger = self._ledger_fingerprint(plans)

        if dry_run:
            records = [self._diagnostic_record(plan, at=now) for plan in plans]
            return self._finish(
                records,
                plans,
                status=ExecutionRunStatus.DRY_RUN,
                dry_run=True,
                started=started,
                as_of=now,
                allocation_run_id=allocation_run_id,
                orders_submitted=0,
                broker_name="NONE",
                ledger=ledger,
            )

        with self._execution_broker(broker) as (connection, broker_failure):
            if connection is None:
                return self._empty(
                    ExecutionRunStatus.BROKER_UNAVAILABLE,
                    detail=broker_failure or "no broker could be opened for execution",
                    dry_run=dry_run,
                    started=started,
                    as_of=now,
                    allocation_run_id=allocation_run_id,
                )
            self._verify_account(connection)
            engine = ExecutionEngine(
                broker=connection, repository=self._repository, clock=self._clock
            )
            before = connection.orders_submitted
            records = [self._execute(plan, engine, at=now) for plan in plans]
            submitted = connection.orders_submitted - before
            broker_name = connection.name

        return self._finish(
            records,
            plans,
            status=_status_of(records),
            dry_run=False,
            started=started,
            as_of=now,
            allocation_run_id=allocation_run_id,
            orders_submitted=submitted,
            broker_name=broker_name,
            store=True,
            ledger=ledger,
        )

    def _ledger_fingerprint(self, plans: list[ExecutionPlan]) -> list[str]:
        """What this ledger already knows about the trades in this run.

        One entry per prior attempt, naming its execution and the state it is
        in. Two runs over the same authorisations reach the same answer only
        while this is unchanged, which is exactly when their records should
        share an id.
        """
        return sorted(
            f"{record.execution_request_id}:{record.execution_id}:{record.state.value}"
            for plan in plans
            for record in self._repository.for_request(plan.request.execution_request_id)
        )

    # --- closing an existing position (Milestone 10) ------------------------
    @traced(
        "execution.close",
        attributes=lambda self, request, **kwargs: {
            TRADING_POSITION_ID: request.position_id,
        },
        count=_metrics.EXECUTION_SUBMISSIONS_TOTAL,
        duration=_metrics.BROKER_SUBMISSION_DURATION,
        result_attributes=lambda submission: (
            {TRADING_EXECUTION_ID: submission.record.execution_id}
            if submission.record is not None
            else {}
        ),
        labels=lambda submission: {
            "execution_type": "CLOSE",
            "status": (
                submission.record.state.value if submission.record is not None else "NOT_SUBMITTED"
            ),
        },
    )
    def submit_exit(
        self,
        request: ExitRequest,
        *,
        entry: ExecutionRecord,
        authorized: bool = False,
        dry_run: bool = False,
        broker: Broker | None = None,
        at: datetime | None = None,
    ) -> ExitSubmission:
        """Submit an exit Milestone 10 decided on, or say why nothing was sent.

        The **only** way an exit order reaches a broker. Milestone 10 has no
        broker, no writable factory and no import that reaches one; it produces
        an :class:`~trading_system.exit.models.ExitRequest` and hands it here,
        and everything about *how* to send an order — validation, the order
        intent, the connection, ``UNKNOWN`` handling, the broker order id, the
        immutable record — stays Milestone 8's and is reused unchanged.

        Every safeguard the entry side has applies here in the same shape:

        * two switches, and neither implies the other. ``execution.enabled``
          *and* an explicit ``authorized=True``. A decision to exit is not
          permission to send an order any more than an allocation id is;
        * a dry run never constructs a broker at all, so "a dry run cannot
          place an order" stays structural;
        * one trade, one order. The exit request's identity is checked against
          the ledger, and an attempt in *any* state where an order may exist —
          including ``SUBMISSION_PENDING`` and ``UNKNOWN`` — refuses a second;
        * the record is written before the send, so a process that dies
          mid-submission leaves evidence that an exit may be in flight.

        The record it writes carries ``intent=CLOSE``, which is what keeps the
        reservation ledger from resolving campaign capital against it and the
        position ledger from deriving a second logical structure from it.
        """
        from trading_system.domain.enums import ExecutionIntent

        now = at or self._clock.now()
        policy = self._config.execution

        if not entry.intent.carries_an_authorisation:
            # Only an OPEN execution establishes a position, so only an OPEN
            # execution can be exited. An orphan cleanup has no authorisation
            # to inherit and no structure to close; conflating the two would
            # let Milestone 10's exit path act on a holding it never opened.
            return ExitSubmission(
                request=request,
                reason_codes=(ExecutionReasonCode.PROVENANCE_UNAVAILABLE,),
                detail=(
                    f"execution {entry.execution_id} is a {entry.intent.value}, not a position "
                    f"this system opened. An exit inherits the entry's purchase card and risk "
                    f"decision, and there are none behind a holding we never acquired"
                ),
            )
        entry_card = entry.purchase_card_id
        entry_risk = entry.risk_decision_id
        assert entry_card is not None and entry_risk is not None  # guaranteed by the validator

        if not dry_run and not authorized:
            return ExitSubmission(
                request=request,
                reason_codes=(ExecutionReasonCode.EXECUTION_NOT_AUTHORIZED,),
                detail=(
                    "no exit execution authorisation was given. Deciding that a position "
                    "should close is not permission to close it: pass --confirm to authorise, "
                    "or --dry-run to inspect what would be sent"
                ),
            )
        if not dry_run and not policy.enabled:
            return ExitSubmission(
                request=request,
                reason_codes=(ExecutionReasonCode.EXECUTION_DISABLED,),
                detail=(
                    "execution.enabled is false in config/execution.yaml. Submission is "
                    "switched off at the system level, for exits exactly as for entries, and "
                    "no exit decision overrides that"
                ),
            )

        existing = self._repository.for_request(request.exit_request_id)
        live = has_live_attempt(existing)
        if live is not None:
            return ExitSubmission(
                request=request,
                reason_codes=(ExecutionReasonCode.ALREADY_SUBMITTED,),
                detail=(
                    f"exit execution {live.execution_id} for this position is already in "
                    f"{live.state.value}. An order may exist at the broker; a second "
                    f"submission would close this position twice — once at a price that was "
                    f"decided on and once at whatever the market does next"
                ),
                existing=live,
            )

        session_refusal = self._session_refusal(now)
        if session_refusal is not None and not dry_run:
            return ExitSubmission(
                request=request,
                reason_codes=(ExecutionReasonCode.MARKET_CLOSED,),
                detail=session_refusal,
            )

        try:
            intent = build_exit_order_intent(
                position_id=request.position_id,
                exit_request_id=request.exit_request_id,
                purchase_card_id=request.purchase_card_id or entry_card,
                risk_decision_id=request.risk_decision_id or entry_risk,
                underlying=request.underlying,
                strategy_type=request.strategy,
                legs=[_option_leg_of(leg) for leg in entry.legs],
                quantity=request.quantity,
                reference_quote=request.reference_quote,
                config=self._config.exit.order,
                execution_config=policy,
                trading_mode=self._settings.trading_mode,
                created_at=now,
                versions=self.versions(),
            )
        except OrderBuildError as exc:
            return ExitSubmission(request=request, reason_codes=(exc.reason_code,), detail=str(exc))

        record = ExecutionRecord(
            execution_id=execution_identifier(
                execution_request_id=request.exit_request_id, attempt=len(existing)
            ),
            execution_request_id=request.exit_request_id,
            allocation_id=request.allocation_id,
            purchase_card_id=intent.purchase_card_id,
            risk_decision_id=intent.risk_decision_id,
            order_intent_id=intent.intent_id,
            campaign_id=request.campaign_id,
            opportunity_id=request.opportunity_id,
            created_at=now,
            updated_at=now,
            underlying=request.underlying,
            strategy=request.strategy,
            intent=ExecutionIntent.CLOSE,
            # The legs as *sent*, which for a close means every action
            # inverted. This is not cosmetic: the position ledger reads a
            # leg's action to decide whether a fill adds or subtracts, so a
            # closing record carrying the entry's BUY legs would net an exit
            # fill onto the position as though it had bought more.
            legs=[_closing_execution_leg(leg) for leg in entry.legs],
            quantity=request.quantity,
            multiplier=entry.multiplier,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            # The exit reference is already a quote; there is no structure cost
            # to convert here, and inventing one would restate the entry's
            # money as though it were this order's.
            reference_quote=request.reference_quote,
            submitted_price=intent.limit_price,
            # Zero, and validated as zero on the record: closing a position
            # commits no new capital and adds no new maximum loss.
            capital_commitment=Decimal("0"),
            maximum_loss=Decimal("0"),
            currency=request.currency,
            trading_mode=request.trading_mode,
            dry_run=dry_run,
            broker="NONE",
            state=ExecutionState.VALIDATED if dry_run else ExecutionState.SUBMISSION_PENDING,
            reason_codes=[ExecutionReasonCode.DRY_RUN] if dry_run else [],
            position_id=request.position_id,
            exit_decision_id=request.decision_id,
            exit_reason=request.exit_reason.value,
            entry_execution_id=request.entry_execution_id,
            allocation_run_id=entry.allocation_run_id,
            contract_selection_id=entry.contract_selection_id,
            strategy_decision_id=entry.strategy_decision_id,
            research_report_id=entry.research_report_id,
            policy_version=request.policy_version,
            versions=self.versions(),
        )

        if dry_run:
            return ExitSubmission(request=request, record=record, intent=intent, dry_run=True)

        with self._execution_broker(broker) as (connection, broker_failure):
            if connection is None:
                return ExitSubmission(
                    request=request,
                    reason_codes=(ExecutionReasonCode.BROKER_DISCONNECTED,),
                    detail=broker_failure or "no broker could be opened for this exit",
                )
            self._verify_account(connection)
            engine = ExecutionEngine(
                broker=connection, repository=self._repository, clock=self._clock
            )
            before = connection.orders_submitted
            outcome = engine.submit(record.model_copy(update={"broker": connection.name}), intent)
            submitted = connection.orders_submitted - before

        return ExitSubmission(
            request=request,
            record=outcome.record,
            intent=intent,
            orders_submitted=submitted,
        )

    # --- closing a pre-existing broker holding (orphan cleanup) -------------
    @traced(
        "execution.cleanup",
        attributes=lambda self, target, **kwargs: {
            TRADING_CONTRACT_ID: str(target.contract_id),
        },
        count=_metrics.EXECUTION_SUBMISSIONS_TOTAL,
        duration=_metrics.BROKER_SUBMISSION_DURATION,
        result_attributes=lambda submission: (
            {TRADING_EXECUTION_ID: submission.record.execution_id}
            if submission.record is not None
            else {}
        ),
        labels=lambda submission: {
            "execution_type": "CLEANUP",
            "status": (
                submission.record.state.value if submission.record is not None else "NOT_SUBMITTED"
            ),
        },
    )
    def submit_cleanup(
        self,
        target: CleanupTarget,
        *,
        cleanup_request_id: str,
        campaign_id: str,
        authorized: bool = False,
        dry_run: bool = False,
        broker: Broker | None = None,
        at: datetime | None = None,
    ) -> CleanupSubmission:
        """Submit a closing order for one pre-existing broker holding.

        The **only** way an orphan cleanup order reaches a broker, and a
        sibling of :meth:`submit_exit` rather than a variant of it — the two
        differ in what authorises them, not in how an order is sent.

        Every safeguard the other two paths have applies here unchanged:

        * two switches, and neither implies the other. ``execution.enabled``
          *and* an explicit ``authorized=True``. (``cleanup.enabled`` is a
          third, checked by the caller before this is reached);
        * a dry run never constructs a broker at all, so "a dry run cannot
          place an order" stays structural rather than a flag to check;
        * one holding, one order. The identity is checked against the ledger
          and an attempt that may have reached a broker — *including a filled
          one* — refuses a second;
        * the record is written before the send, so a process that dies
          mid-submission leaves evidence rather than silence;
        * an ``UNKNOWN`` outcome is never retried. It is resolved by observing
          the broker, exactly as everywhere else.

        What is different is what the record does **not** carry. There is no
        allocation, purchase card, risk decision, opportunity or strategy
        behind it, the record model refuses to let one be attached, and
        ``ExecutionIntent.CLEANUP`` keeps its fills out of the internal
        position ledger. This system did not open the holding and records
        nothing claiming it did.
        """
        from trading_system.domain.enums import ExecutionIntent

        now = at or self._clock.now()
        policy = self._config.execution
        cleanup_policy = self._config.cleanup

        if not dry_run and not authorized:
            return CleanupSubmission(
                target_key=target.key,
                reason_codes=(ExecutionReasonCode.EXECUTION_NOT_AUTHORIZED,),
                detail=(
                    "no cleanup authorisation was given. Listing the orphan holdings in an "
                    "account is not permission to sell out of them: pass --confirm to "
                    "authorise, or run without it to inspect what would be sent"
                ),
            )
        if not dry_run and not policy.enabled:
            return CleanupSubmission(
                target_key=target.key,
                reason_codes=(ExecutionReasonCode.EXECUTION_DISABLED,),
                detail=(
                    "execution.enabled is false in config/execution.yaml. Submission is "
                    "switched off at the system level, for a cleanup exactly as for a trade"
                ),
            )
        if not dry_run and self._settings.trading_mode is not TradingMode.PAPER:
            # Restated here, at the last point before a broker could be built,
            # even though the caller's gates already refused. Two checks in two
            # places is the right number for the one thing on this path that
            # cannot be undone.
            return CleanupSubmission(
                target_key=target.key,
                reason_codes=(ExecutionReasonCode.PAPER_MODE_REQUIRED,),
                detail=(
                    f"TRADING_MODE={self._settings.trading_mode.value}. An orphan cleanup "
                    f"submits orders in PAPER and in nothing else"
                ),
            )

        request_id = cleanup_execution_request_identifier(
            account_reference=target.account_reference,
            contract_key=target.key,
            trading_mode=self._settings.trading_mode,
            order_type=cleanup_policy.order.order_type,
            time_in_force=cleanup_policy.order.time_in_force,
            policy_version=self._config.application.config_version,
        )
        existing = self._repository.for_request(request_id)
        prior = _reached_the_broker(existing)
        if prior is not None:
            return CleanupSubmission(
                target_key=target.key,
                reason_codes=(ExecutionReasonCode.ALREADY_SUBMITTED,),
                detail=(
                    f"cleanup execution {prior.execution_id} for {target.key} is already in "
                    f"{prior.state.value}. An order for this holding has reached the broker "
                    f"once; a second would sell {target.quantity} contract(s) twice, and the "
                    f"account holds {target.quantity}"
                ),
                existing=prior,
            )

        session_refusal = self._session_refusal(now)
        if session_refusal is not None and not dry_run:
            return CleanupSubmission(
                target_key=target.key,
                reason_codes=(ExecutionReasonCode.MARKET_CLOSED,),
                detail=session_refusal,
            )

        try:
            intent = build_cleanup_order_intent(
                cleanup_request_id=cleanup_request_id,
                contract_key=target.key,
                underlying=target.underlying,
                contract_id=target.contract_id,
                right=target.right,
                strike=target.strike,
                expiration=target.expiration,
                multiplier=target.multiplier,
                quantity=target.quantity,
                reference_quote=target.market_price,
                local_symbol=target.local_symbol,
                config=cleanup_policy.order,
                trading_mode=self._settings.trading_mode,
                created_at=now,
                versions=self.versions(),
            )
        except OrderBuildError as exc:
            return CleanupSubmission(
                target_key=target.key, reason_codes=(exc.reason_code,), detail=str(exc)
            )

        leg = intent.legs[0]
        record = ExecutionRecord(
            execution_id=execution_identifier(
                execution_request_id=request_id, attempt=len(existing)
            ),
            execution_request_id=request_id,
            # No allocation, card, risk decision, opportunity or strategy. Not
            # omitted for tidiness: the record model *refuses* them on a
            # CLEANUP, which is what makes "this system did not open the
            # holding" a property of the artifact rather than a promise.
            order_intent_id=intent.intent_id,
            campaign_id=campaign_id,
            created_at=now,
            updated_at=now,
            underlying=target.underlying,
            intent=ExecutionIntent.CLEANUP,
            legs=[
                ExecutionLeg(
                    leg_index=0,
                    contract_id=target.contract_id,
                    action=leg.action,
                    right=leg.right,
                    underlying=target.underlying,
                    expiration=leg.expiration,
                    strike=leg.strike,
                    multiplier=leg.multiplier,
                    ratio=1,
                    # Whatever the broker reported, including nothing. It
                    # cannot be derived from the symbol and is not guessed.
                    trading_class=target.trading_class,
                    exchange=target.exchange,
                    local_symbol=target.local_symbol,
                    currency=target.currency,
                )
            ],
            quantity=intent.quantity,
            multiplier=leg.multiplier,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            # The broker's own reported price for the holding, in quoted terms.
            # There is no structure cost to convert: nothing here was purchased
            # by this system, so there is no authorised figure to restate.
            reference_quote=target.market_price,
            submitted_price=intent.limit_price,
            # Zero, and validated as zero. Selling a holding the campaign never
            # funded commits none of its capital and removes none of its risk.
            capital_commitment=Decimal("0"),
            maximum_loss=Decimal("0"),
            currency=target.currency,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            broker="NONE",
            state=ExecutionState.VALIDATED if dry_run else ExecutionState.SUBMISSION_PENDING,
            reason_codes=[ExecutionReasonCode.DRY_RUN] if dry_run else [],
            cleanup_request_id=cleanup_request_id,
            broker_position_key=target.key,
            policy_version=self._config.application.config_version,
            versions=self.versions(),
        )

        if dry_run:
            return CleanupSubmission(
                target_key=target.key, record=record, intent=intent, dry_run=True
            )

        with self._execution_broker(broker) as (connection, broker_failure):
            if connection is None:
                return CleanupSubmission(
                    target_key=target.key,
                    reason_codes=(ExecutionReasonCode.BROKER_DISCONNECTED,),
                    detail=broker_failure or "no broker could be opened for this cleanup",
                )
            self._verify_account(connection)
            engine = ExecutionEngine(
                broker=connection, repository=self._repository, clock=self._clock
            )
            before = connection.orders_submitted
            outcome = engine.submit(record.model_copy(update={"broker": connection.name}), intent)
            submitted = connection.orders_submitted - before

        return CleanupSubmission(
            target_key=target.key,
            record=outcome.record,
            intent=intent,
            orders_submitted=submitted,
        )

    def _session_refusal(self, at: datetime) -> str | None:
        """Refuse before opening a connection when the market is closed.

        Uses the same calendar and the same policy keys the entry side does, so
        an exit and an entry cannot disagree about whether the exchange is
        trading.
        """
        policy = self._config.execution
        if not policy.require_market_open:
            return None
        from trading_system.data.market_calendar import MarketCalendarError

        try:
            if self._calendar.is_open(at):
                return None
        except MarketCalendarError as exc:
            if not policy.block_on_unknown_session:
                return None
            return (
                f"the exchange calendar cannot say whether the market is open at "
                f"{at.isoformat()}: {exc}"
            )
        return (
            f"the {self._calendar.exchange} calendar says regular trading is not in progress "
            f"at {at.isoformat()}, so no exit order is sent. The session is determined from "
            f"the transcribed calendar, never invented"
        )

    # --- after the fact ----------------------------------------------------
    def resolve(self, execution_id: str, *, broker: Broker | None = None) -> ExecutionRecord | None:
        """Ask the broker what became of an execution, and record the answer.

        The supported response to an uncertain submission. It never submits.
        """
        record = self._repository.current(execution_id)
        if record is None:
            return None
        with self._execution_broker(broker) as (connection, _):
            if connection is None:
                return record
            engine = ExecutionEngine(
                broker=connection, repository=self._repository, clock=self._clock
            )
            return engine.resolve(record).record

    def cancel(self, execution_id: str, *, broker: Broker | None = None) -> ExecutionRecord | None:
        """Cancel a live order. Narrow by design; not an exit."""
        record = self._repository.current(execution_id)
        if record is None:
            return None
        with self._execution_broker(broker) as (connection, _):
            if connection is None:
                return record
            engine = ExecutionEngine(
                broker=connection, repository=self._repository, clock=self._clock
            )
            return engine.cancel(record).record

    # --- internals ---------------------------------------------------------
    def _execute(
        self, plan: ExecutionPlan, engine: ExecutionEngine, *, at: datetime
    ) -> ExecutionRecord:
        """Submit one plan, or record why it was not submitted."""
        if not plan.submittable:
            return self._refusal_record(plan, at=at, broker=engine.broker.name)

        existing = self._repository.for_request(plan.request.execution_request_id)
        live = has_live_attempt(existing)
        if live is not None:
            # The single most important branch in this milestone. A prior
            # attempt may already be an order at the broker, so this one records
            # the fact and sends nothing.
            return self._refusal_record(
                plan,
                at=at,
                broker=engine.broker.name,
                reason_codes=(ExecutionReasonCode.ALREADY_SUBMITTED,),
                detail=(
                    f"execution {live.execution_id} for this authorisation is already in "
                    f"{live.state.value}. An order may exist at the broker; a second "
                    f"submission would place this trade twice"
                ),
                state=ExecutionState.REJECTED,
                # Not persisted: no new attempt was made. The attempt this
                # refers to is already in the ledger, and writing a fresh
                # refusal on every re-run would grow the record of one event
                # without adding a fact to it.
                persist=False,
            )

        assert plan.card is not None and plan.intent is not None and plan.risk_decision is not None
        record = self._pending_record(plan, attempt=len(existing), at=at, broker=engine.broker.name)
        outcome: SubmissionOutcome = engine.submit(record, plan.intent)
        return outcome.record

    def _pending_record(
        self, plan: ExecutionPlan, *, attempt: int, at: datetime, broker: str
    ) -> ExecutionRecord:
        assert plan.card is not None and plan.intent is not None and plan.risk_decision is not None
        allocation = plan.allocation
        legs = build_execution_legs(allocation)
        multiplier = legs[0].multiplier
        assert allocation.unit_cost is not None

        return ExecutionRecord(
            execution_id=execution_identifier(
                execution_request_id=plan.request.execution_request_id, attempt=attempt
            ),
            execution_request_id=plan.request.execution_request_id,
            allocation_id=allocation.allocation_id,
            purchase_card_id=plan.card.card_id,
            risk_decision_id=plan.risk_decision.decision_id,
            order_intent_id=plan.intent.intent_id,
            campaign_id=allocation.campaign_id,
            opportunity_id=allocation.opportunity_id,
            created_at=at,
            updated_at=at,
            underlying=allocation.symbol,
            strategy=allocation.strategy,
            legs=legs,
            quantity=allocation.quantity,
            multiplier=multiplier,
            order_type=plan.intent.order_type,
            time_in_force=plan.intent.time_in_force,
            reference_price=allocation.unit_cost,
            reference_quote=reference_quote_of(allocation.unit_cost, multiplier),
            submitted_price=plan.intent.limit_price,
            capital_commitment=allocation.capital_committed,
            maximum_loss=allocation.total_max_loss,
            currency=allocation.currency,
            trading_mode=plan.request.trading_mode,
            dry_run=False,
            broker=broker,
            state=ExecutionState.SUBMISSION_PENDING,
            allocation_run_id=allocation.run_id,
            contract_selection_id=allocation.contract_selection_id,
            strategy_decision_id=allocation.strategy_decision_id,
            research_report_id=allocation.research_report_id,
            account_snapshot_id=allocation.account_snapshot_id,
            policy_version=plan.request.policy_version,
            versions=self.versions(),
        )

    def _refusal_record(
        self,
        plan: ExecutionPlan,
        *,
        at: datetime,
        broker: str,
        reason_codes: tuple[ExecutionReasonCode, ...] | None = None,
        detail: str | None = None,
        state: ExecutionState = ExecutionState.REJECTED,
        persist: bool = True,
    ) -> ExecutionRecord:
        """A refusal, recorded as fully as an order would have been.

        Stored with the same fields an attempt carries so "why did nothing
        happen" is answerable from the same place as "what did we send".

        ``persist=False`` is for the one refusal that describes an *existing*
        attempt rather than a new one — ``ALREADY_SUBMITTED``. The attempt it
        refers to is already in the ledger; writing a new record each time
        would multiply the account of one event.
        """
        codes = reason_codes or plan.reason_codes or tuple(plan.validation.reason_codes)
        record = self._skeleton(
            plan,
            at=at,
            broker=broker,
            state=state,
            reason_codes=list(codes) or [ExecutionReasonCode.EXECUTION_CONFIGURATION_ERROR],
            failure_reason=detail or plan.detail or plan.validation.detail or "refused",
            dry_run=False,
        )
        if persist:
            self._repository.save(record)
        return record

    def _diagnostic_record(self, plan: ExecutionPlan, *, at: datetime) -> ExecutionRecord:
        """A dry run's record. Never stored, and structurally unable to be live."""
        codes = list(plan.reason_codes) or list(plan.validation.reason_codes)
        return self._skeleton(
            plan,
            at=at,
            broker="NONE",
            state=ExecutionState.VALIDATED if plan.submittable else ExecutionState.REJECTED,
            reason_codes=codes or [ExecutionReasonCode.DRY_RUN],
            failure_reason=plan.detail or plan.validation.detail or None,
            dry_run=True,
        )

    def _skeleton(
        self,
        plan: ExecutionPlan,
        *,
        at: datetime,
        broker: str,
        state: ExecutionState,
        reason_codes: list[ExecutionReasonCode],
        failure_reason: str | None,
        dry_run: bool,
    ) -> ExecutionRecord:
        allocation = plan.allocation
        try:
            legs = build_execution_legs(allocation)
        except PurchaseCardError:
            # The legs themselves are why this was refused. Record the refusal
            # without them rather than failing to record it at all.
            legs = []
        multiplier = legs[0].multiplier if legs else 0
        reference = allocation.unit_cost
        quote = (
            reference_quote_of(reference, multiplier)
            if reference is not None and multiplier
            else None
        )
        return ExecutionRecord(
            execution_id=execution_identifier(
                execution_request_id=plan.request.execution_request_id,
                attempt=len(self._repository.for_request(plan.request.execution_request_id)),
            ),
            execution_request_id=plan.request.execution_request_id,
            allocation_id=allocation.allocation_id,
            purchase_card_id=plan.card.card_id if plan.card else "unavailable",
            risk_decision_id=plan.risk_decision.decision_id
            if plan.risk_decision
            else "unavailable",
            order_intent_id=plan.intent.intent_id if plan.intent else "unavailable",
            campaign_id=allocation.campaign_id,
            opportunity_id=allocation.opportunity_id,
            created_at=at,
            updated_at=at,
            underlying=allocation.symbol,
            strategy=allocation.strategy,
            legs=legs,
            quantity=allocation.quantity,
            multiplier=multiplier,
            order_type=plan.request.order_type,
            time_in_force=plan.request.time_in_force,
            reference_price=reference,
            reference_quote=quote,
            submitted_price=plan.intent.limit_price if plan.intent else None,
            capital_commitment=allocation.capital_committed,
            maximum_loss=allocation.total_max_loss,
            currency=allocation.currency,
            trading_mode=plan.request.trading_mode,
            dry_run=dry_run,
            broker=broker,
            state=state,
            reason_codes=reason_codes,
            failure_reason=failure_reason,
            allocation_run_id=allocation.run_id,
            contract_selection_id=allocation.contract_selection_id,
            strategy_decision_id=allocation.strategy_decision_id,
            research_report_id=allocation.research_report_id,
            account_snapshot_id=allocation.account_snapshot_id,
            policy_version=plan.request.policy_version,
            versions=self.versions(),
        )

    def _request_for(
        self,
        allocation: CampaignAllocation,
        *,
        order_type: OrderType,
        dry_run: bool,
        at: datetime,
    ) -> ExecutionRequest:
        policy = self._config.execution
        tif: TimeInForce = policy.time_in_force
        return ExecutionRequest(
            execution_request_id=execution_request_identifier(
                allocation_id=allocation.allocation_id,
                trading_mode=self._settings.trading_mode,
                order_type=order_type,
                time_in_force=tif,
                policy_version=self._config.application.config_version,
            ),
            allocation_id=allocation.allocation_id,
            campaign_id=allocation.campaign_id,
            opportunity_id=allocation.opportunity_id,
            requested_at=at,
            execution_authorized=True,
            order_type=order_type,
            time_in_force=tif,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            policy_version=self._config.application.config_version,
            versions=self.versions(),
        )

    def _provenance(
        self, allocation: CampaignAllocation
    ) -> tuple[ResearchReport, StrategyDecision]:
        """Load the artifacts the purchase card's *why* comes from.

        Read rather than restated: the hypothesis and the invalidation
        conditions on a card are research's words, and an execution layer that
        composed its own would be writing research. Missing provenance is
        ``PROVENANCE_UNAVAILABLE`` — "we could not look" — never a card with the
        reasoning left blank.
        """
        if allocation.research_report_id is None or allocation.strategy_decision_id is None:
            raise PurchaseCardError(
                ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
                f"allocation {allocation.allocation_id} does not name the research report and "
                f"strategy decision it descends from, so the purchase card the specification "
                f"requires before execution cannot be built",
            )

        research_run = self._research_repository.get(allocation.research_run_id or "")
        report = next(
            (
                candidate
                for candidate in (research_run.reports if research_run else [])
                if candidate.report_id == allocation.research_report_id
            ),
            None,
        )
        if report is None:
            raise PurchaseCardError(
                ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
                f"research report {allocation.research_report_id} is not in the store",
            )

        strategy_run = self._strategy_repository.get(allocation.strategy_run_id or "")
        decision = next(
            (
                candidate
                for candidate in (strategy_run.decisions if strategy_run else [])
                if candidate.decision_id == allocation.strategy_decision_id
            ),
            None,
        )
        if decision is None:
            raise PurchaseCardError(
                ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
                f"strategy decision {allocation.strategy_decision_id} is not in the store",
            )
        try:
            return report.to_research_report(), decision.to_strategy_decision()
        except ValueError as exc:
            raise PurchaseCardError(
                ExecutionReasonCode.PROVENANCE_UNAVAILABLE,
                f"the upstream artifacts cannot cross the purchase-card boundary: {exc}",
            ) from exc

    def _select(
        self,
        *,
        allocation_ids: Sequence[str] | None,
        allocation_run_id: str | None,
        symbols: Sequence[str] | None,
    ) -> tuple[list[CampaignAllocation], str | None]:
        """Choose which authorisations to act on. Never creates one."""
        if allocation_ids:
            chosen: list[CampaignAllocation] = []
            missing: list[str] = []
            for allocation_id in allocation_ids:
                found = self.find_allocation(allocation_id)
                if found is None:
                    missing.append(allocation_id)
                else:
                    chosen.append(found)
            if missing:
                return [], (
                    f"no stored authorisation with id {', '.join(missing)}. This stage executes "
                    f"an existing authorisation and never creates one"
                )
            return chosen, None

        run = self.allocation_run(allocation_run_id)
        if run is None:
            return [], (
                f"no allocation run with id {allocation_run_id!r} was found"
                if allocation_run_id
                else "no allocation run exists yet; run 'allocation run' first"
            )
        wanted = {s.strip().upper() for s in symbols} if symbols else None
        if wanted:
            covered = {a.symbol for a in run.allocations}
            absent = sorted(wanted - covered)
            if absent:
                return [], (
                    f"allocation run {run.run_id} did not cover {', '.join(absent)}; this stage "
                    f"executes an authorisation and never makes one"
                )
        return [
            allocation
            for allocation in run.allocations
            if allocation.approved and (wanted is None or allocation.symbol in wanted)
        ], None

    @contextmanager
    def _execution_broker(
        self, supplied: Broker | None
    ) -> Iterator[tuple[Broker | None, str | None]]:
        """Open a writable broker, and always close it.

        The connection is short-lived on purpose. Milestone 2 established that
        only the first uncached round trip on a TWS connection is reliably
        answered, so one submission gets one connection and the connection is
        closed after it.
        """
        if supplied is not None:
            # A caller-supplied broker is the caller's to manage. Tests use this
            # to observe a submission without the service owning the lifecycle.
            yield supplied, None
            return

        from trading_system.broker.base import BrokerError
        from trading_system.broker.factory import build_execution_broker

        factory = self._broker_factory or build_execution_broker
        try:
            connection = factory(self._settings, clock=self._clock)  # type: ignore[operator]
        except BrokerError as exc:
            yield None, str(exc)
            return

        try:
            connection.connect()
        except BrokerError as exc:
            connection.disconnect()
            yield None, str(exc)
            return

        try:
            yield connection, None
        finally:
            connection.disconnect()

    def _verify_account(self, broker: Broker) -> None:
        """Prove the session is the expected paper account before sending.

        Uses the identity resolved during the connection handshake, which is
        served from ``ib_async``'s startup cache — so this costs no round trip
        and cannot hang. The simulator has no live account to confuse and is
        skipped.
        """
        policy = self._config.execution
        if not policy.verify_paper_account:
            return
        verify = getattr(broker, "verify_paper_account", None)
        if verify is None:
            return
        verify(policy.paper_account_prefixes)

    def _finish(
        self,
        records: list[ExecutionRecord],
        plans: list[ExecutionPlan],
        *,
        status: ExecutionRunStatus,
        dry_run: bool,
        started: float,
        as_of: datetime,
        allocation_run_id: str | None,
        orders_submitted: int,
        broker_name: str,
        store: bool = False,
        ledger: list[str] | None = None,
    ) -> ExecutionRun:
        result = ExecutionRunResult(
            run_id=execution_run_identifier(
                as_of=as_of,
                allocation_run_id=allocation_run_id,
                execution_request_ids=[r.execution_request_id for r in records],
                config_version=self._config.application.config_version,
                policy_version=self._config.application.config_version,
                trading_mode=self._settings.trading_mode,
                dry_run=dry_run,
                ledger_state=ledger,
            ),
            campaign_id=self._config.campaign.campaign_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=status,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            broker=broker_name,
            policy_version=self._config.application.config_version,
            allocation_run_id=allocation_run_id,
            executions=records,
            counts=_counts(records),
            orders_submitted=orders_submitted,
            versions=self.versions(),
        )
        stored = False
        if store and not dry_run:
            self._repository.save_run(result)
            stored = True
        return ExecutionRun(
            result=result,
            plans=tuple(plans),
            stored=stored,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    def _empty(
        self,
        status: ExecutionRunStatus,
        *,
        detail: str,
        dry_run: bool,
        started: float,
        as_of: datetime,
        allocation_run_id: str | None = None,
    ) -> ExecutionRun:
        result = ExecutionRunResult(
            run_id=execution_run_identifier(
                as_of=as_of,
                allocation_run_id=allocation_run_id,
                execution_request_ids=[],
                config_version=self._config.application.config_version,
                policy_version=self._config.application.config_version,
                trading_mode=self._settings.trading_mode,
                dry_run=dry_run,
            ),
            campaign_id=self._config.campaign.campaign_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=status,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            broker="NONE",
            policy_version=self._config.application.config_version,
            allocation_run_id=allocation_run_id,
            orders_submitted=0,
            versions=self.versions(),
            status_detail=detail,
        )
        return ExecutionRun(
            result=result,
            plans=(),
            stored=False,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )


def _counts(records: list[ExecutionRecord]) -> ExecutionRunCounts:
    return ExecutionRunCounts(
        considered=len(records),
        submitted=sum(1 for r in records if r.submitted and r.state is not ExecutionState.UNKNOWN),
        refused=sum(
            1 for r in records if r.state in (ExecutionState.REJECTED, ExecutionState.FAILED)
        ),
        already_submitted=sum(
            1 for r in records if ExecutionReasonCode.ALREADY_SUBMITTED in r.reason_codes
        ),
        uncertain=sum(1 for r in records if r.state is ExecutionState.UNKNOWN),
    )


def _status_of(records: list[ExecutionRecord]) -> ExecutionRunStatus:
    """The run's status, derived from its own records.

    A run with any uncertain submission is ``PARTIAL``, never ``SUCCESS``: an
    order whose fate is unknown is not a success, and calling it one is how it
    stops being looked at.
    """
    if not records:
        return ExecutionRunStatus.NOTHING_TO_EXECUTE
    submitted = [r for r in records if r.submitted]
    if not submitted:
        return ExecutionRunStatus.NOTHING_SUBMITTED
    if len(submitted) == len(records) and not any(
        r.state is ExecutionState.UNKNOWN for r in records
    ):
        return ExecutionRunStatus.SUCCESS
    return ExecutionRunStatus.PARTIAL
