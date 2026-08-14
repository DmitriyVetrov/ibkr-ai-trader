"""The exit service: the composition root for Milestone 10.

.. code-block:: text

    Milestone 9 position reality      <- CONSUMED, never re-derived
          |
    open strategy positions           <- from confirmed fills only
          |
    stored point-in-time quotes       <- repository only; no live request
          |
    ExitPolicyEngine                  <- pure, deterministic, no model
          |
    WAIT / EXIT / BLOCK               <- immutable evaluation + decision
          |
    ExitRequest -> Milestone 8        <- the ONLY path to an exit order
          |
    Milestone 9 reconciliation        <- what actually happened

Properties this service holds regardless of which path it takes:

* **It holds no broker.** There is no connection, no writable factory and no
  import anywhere in this package that reaches one; a test walks the transitive
  import graph and asserts it. The only way an exit order exists is
  :meth:`~trading_system.execution.service.ExecutionService.submit_exit`, and
  this service reaches that through an injected execution service.
* **It consults no model.** No LLM client, no prompt, no agent import. Whether
  a position should close is arithmetic over stored artifacts, and identical
  inputs produce an identical decision.
* **It re-decides nothing upstream.** No universe selection, no research, no
  strategy selection, no contract selection, no allocation, no sizing. The
  quantity an exit closes is what the broker says is held.
* **It never trades while evaluating.** ``evaluate`` and ``monitor`` build no
  order and construct no broker. Handing an exit to Milestone 8 needs
  ``execution.enabled``, an explicit authorisation, and a decision that
  actually triggered.
* **It fails closed, per position.** A position whose inputs cannot be
  assembled ends as a named ``BLOCK`` and the run continues; one unreadable
  position must not stop a monitoring cycle.
* **It is safe to run repeatedly.** Nothing lives in process memory: the
  trailing level, the lifecycle state and every past judgement are read from
  the store each time, and a re-run over unchanged state re-observes rather
  than deciding again.

**Operation names for future telemetry.** The five boundaries a Milestone 11
tracer would wrap are named as methods and logged with stable event names:
``position.monitor``, ``exit.evaluate``, ``exit.decision``, ``exit.execute``
and ``exit.confirm``. Nothing here imports a telemetry vendor, and no
observability decision may change a trading decision.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system import __version__ as application_version
from trading_system.data.market_calendar import MarketCalendar
from trading_system.domain.enums import (
    EXIT_SUBMISSION_BLOCKED_STATES,
    BrokerReadStatus,
    ExecutionState,
    ExitDecisionType,
    ExitReasonCode,
    ExitRunStatus,
    LegAction,
    MaxLossBasis,
    PositionLifecycleEventType,
    PositionLifecycleState,
    StrategyType,
    TrailingStopState,
)
from trading_system.domain.models import SystemVersions
from trading_system.exit.engine import ExitInputs, ExitPolicyEngine
from trading_system.exit.expiration import expiration_view
from trading_system.exit.models import (
    EXIT_SCHEMA_VERSION,
    ExitDecisionRecord,
    ExitEvaluation,
    ExitRequest,
    ExitRunCounts,
    ExitRunResult,
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    PositionValuation,
    TrailingStopRecord,
    exit_request_identifier,
    exit_run_identifier,
    lifecycle_event_identifier,
    lifecycle_snapshot_identifier,
)
from trading_system.exit.store import ExitRepository, FilesystemExitRepository
from trading_system.exit.thesis import ThesisView, check_conditions
from trading_system.exit.trailing import new_trailing_record, observe
from trading_system.exit.validation import effective_policy, strategy_config_for
from trading_system.exit.valuation import ExitQuoteReader, HeldLeg, read_and_value
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    TRADING_EXIT_ID,
    TRADING_POSITION_ID,
    TRADING_REASON_CODE,
    TRADING_STATUS,
)
from trading_system.observability.instrument import traced

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_system.execution.models import ExecutionRecord
    from trading_system.execution.service import ExecutionService, ExitSubmission
    from trading_system.positions.models import BrokerPositionSnapshot, StrategyPosition
    from trading_system.positions.service import PositionService

__all__ = ["ExitEvaluationOutcome", "ExitRun", "ExitService", "OpenPosition"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """One already-open strategy position, assembled from Milestone 9.

    Everything here is *read*. The structure and its legs come from the entry
    execution; the quantity actually held comes from the broker snapshot; the
    lifecycle state comes from this milestone's own store. Nothing is computed
    that Milestone 9 already answers.
    """

    position_id: str
    underlying: str
    strategy: StrategyType
    structure: StrategyPosition
    entry: ExecutionRecord
    lifecycle: PositionLifecycleSnapshot
    legs: tuple[HeldLeg, ...]
    #: Units of the structure the broker reports, from the weakest leg.
    #: ``None`` when broker data was unusable — never zero, which would mean
    #: the broker said it holds none.
    observed_units: int | None
    expected_units: int
    multiplier: int | None
    entry_quote: Decimal | None
    currency: str | None
    broker_read_status: BrokerReadStatus

    @property
    def held(self) -> bool:
        return bool(self.observed_units)


@dataclass(frozen=True, slots=True)
class ExitEvaluationOutcome:
    """One position's judgement, and everything the caller needs to report it."""

    position: OpenPosition
    evaluation: ExitEvaluation
    decision: ExitDecisionRecord
    trailing: TrailingStopRecord | None = None
    stored: bool = False
    #: False when this exact judgement was already on file — a re-observation.
    is_new: bool = False
    submission: ExitSubmission | None = None

    @property
    def orders_submitted(self) -> int:
        return self.submission.orders_submitted if self.submission else 0


@dataclass(frozen=True, slots=True)
class ExitRun:
    """One monitoring or evaluation run, and what it did."""

    result: ExitRunResult
    outcomes: tuple[ExitEvaluationOutcome, ...] = ()
    stored: bool = False
    dry_run: bool = False
    duration_seconds: float = 0.0

    @property
    def orders_submitted(self) -> int:
        """Read off the broker through Milestone 8, never accumulated here."""
        return self.result.orders_submitted

    @property
    def exits(self) -> tuple[ExitEvaluationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.decision.decision is ExitDecisionType.EXIT)

    @property
    def blocks(self) -> tuple[ExitEvaluationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.decision.decision is ExitDecisionType.BLOCK)


class ExitService:
    """Evaluates open positions and, when authorised, hands exits to Milestone 8."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        exit_repository: ExitRepository | None = None,
        position_service: PositionService | None = None,
        execution_service: ExecutionService | None = None,
        quote_reader: ExitQuoteReader | None = None,
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

        self._repository = exit_repository or FilesystemExitRepository(data_root / "exit")
        self._position_service = position_service
        self._execution_service = execution_service
        self._quote_reader = quote_reader
        #: Passed through to the Milestone 9 position service, which builds a
        #: *read-only* connection whatever the settings say. This package has
        #: no writable constructor and no import that reaches one.
        self._broker_factory = broker_factory
        self._calendar = MarketCalendar(config.data.market_calendar)
        self._engine = ExitPolicyEngine(config.exit)

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ExitRepository:
        return self._repository

    @property
    def engine(self) -> ExitPolicyEngine:
        return self._engine

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def enabled(self) -> bool:
        """Whether exit *evaluation* runs at all. Not permission to trade."""
        return self._config.exit.enabled

    def versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            strategy_spec_version=EXIT_SCHEMA_VERSION,
        )

    def history(self, limit: int | None = None, *, position_id: str | None = None):  # type: ignore[no-untyped-def]
        return self._repository.history(limit=limit, position_id=position_id)

    def latest_run(self) -> ExitRunResult | None:
        return self._repository.latest_run()

    def lifecycle(self, position_id: str) -> PositionLifecycleSnapshot | None:
        return self._repository.lifecycle(position_id)

    def positions(self) -> PositionService:
        if self._position_service is None:
            from trading_system.positions.service import PositionService as _PositionService

            self._position_service = _PositionService(
                settings=self._settings,
                config=self._config,
                clock=self._clock,
                broker_factory=self._broker_factory,
                root=self._data_root.parent,
            )
        return self._position_service

    def executions(self) -> ExecutionService:
        if self._execution_service is None:
            from trading_system.execution.service import ExecutionService as _ExecutionService

            self._execution_service = _ExecutionService(
                settings=self._settings,
                config=self._config,
                clock=self._clock,
                root=self._data_root.parent,
            )
        return self._execution_service

    def quotes(self) -> ExitQuoteReader:
        if self._quote_reader is None:
            from trading_system.data.repository import FilesystemDataRepository

            self._quote_reader = ExitQuoteReader(
                FilesystemDataRepository(self._data_root, clock=self._clock)
            )
        return self._quote_reader

    # --- operation: position.monitor ---------------------------------------
    def open_positions(
        self,
        *,
        as_of: datetime | None = None,
        snapshot: BrokerPositionSnapshot | None = None,
        position_ids: Sequence[str] | None = None,
    ) -> list[OpenPosition]:
        """Every position exit management should look at.

        Derived from Milestone 9 and from nothing else: an ``OPEN`` execution
        with confirmed fills establishes a logical structure, and the broker
        snapshot says how much of it is actually there. A closed position stays
        in this list — with ``observed_units == 0`` — because the lifecycle
        still has to *record* that it closed, and a position that vanished from
        the list the moment it closed would never reach ``CLOSED``.
        """
        positions = self.positions()
        stored = snapshot if snapshot is not None else positions.latest_usable_snapshot()
        read_status = stored.read_status if stored is not None else BrokerReadStatus.UNAVAILABLE
        projection = positions.expected(as_of=as_of, snapshot=stored)

        wanted = set(position_ids) if position_ids else None
        by_execution = {record.execution_id: record for record in positions.execution_records()}

        found: list[OpenPosition] = []
        for structure in projection.strategies:
            position_id = structure.strategy_position_id
            if wanted is not None and position_id not in wanted:
                continue
            entry = by_execution.get(structure.execution_id or "")
            if entry is None:
                # A structure with no readable entry execution cannot be
                # exited: the legs, the contract ids and the entry price all
                # come from it. Skipped here and reported by the caller rather
                # than invented.
                continue
            found.append(self._assemble(structure, entry, read_status=read_status))
        return sorted(found, key=lambda p: (p.underlying, p.position_id))

    def _assemble(
        self,
        structure: StrategyPosition,
        entry: ExecutionRecord,
        *,
        read_status: BrokerReadStatus,
    ) -> OpenPosition:
        legs = tuple(
            HeldLeg(
                leg_index=leg.leg_index,
                key=leg.key,
                contract_id=leg.contract_id,
                underlying=leg.underlying,
                right=leg.right,
                strike=leg.strike,
                expiration=leg.expiration,
                ratio=next(
                    (
                        execution_leg.ratio
                        for execution_leg in entry.legs
                        if execution_leg.leg_index == leg.leg_index
                    ),
                    1,
                ),
                multiplier=leg.multiplier,
                observed_quantity=leg.observed_quantity,
            )
            for leg in structure.legs
        )
        stored_lifecycle = self._repository.lifecycle(structure.strategy_position_id)
        lifecycle = stored_lifecycle or self._new_lifecycle(structure, entry)
        return OpenPosition(
            position_id=structure.strategy_position_id,
            underlying=structure.underlying,
            strategy=structure.strategy,
            structure=structure,
            entry=entry,
            lifecycle=lifecycle,
            legs=legs,
            observed_units=_observed_units(legs),
            expected_units=int(structure.filled_quantity),
            multiplier=entry.multiplier or None,
            entry_quote=entry.average_fill_price,
            currency=entry.currency,
            broker_read_status=read_status,
        )

    def _new_lifecycle(
        self, structure: StrategyPosition, entry: ExecutionRecord
    ) -> PositionLifecycleSnapshot:
        """The record a position starts with: OPEN, with nothing decided yet."""
        return PositionLifecycleSnapshot(
            lifecycle_id=lifecycle_snapshot_identifier(
                position_id=structure.strategy_position_id,
                as_of=structure.as_of,
                content_digest=entry.execution_id,
            ),
            position_id=structure.strategy_position_id,
            as_of=structure.as_of,
            updated_at=structure.as_of,
            state=PositionLifecycleState.OPEN,
            underlying=structure.underlying,
            strategy=structure.strategy,
            open_quantity=int(structure.filled_quantity),
            entry_execution_id=entry.execution_id,
            opportunity_id=structure.opportunity_id,
            allocation_id=structure.allocation_id,
            campaign_id=structure.campaign_id,
            research_report_id=structure.research_report_id,
            strategy_decision_id=structure.strategy_decision_id,
            detail=(
                "established by confirmed fills and not yet evaluated. Only a confirmed broker "
                "fill makes a position, and only broker reality closes one"
            ),
        )

    # --- operation: exit.evaluate ------------------------------------------
    @traced(
        "exit.evaluate",
        attributes=lambda self, position, **kwargs: {
            TRADING_POSITION_ID: position.position_id,
        },
        count=_metrics.EXIT_EVALUATIONS_TOTAL,
        duration=_metrics.EXIT_EVALUATION_DURATION,
        result_attributes=lambda outcome: {
            TRADING_EXIT_ID: outcome.decision.decision_id,
            TRADING_STATUS: outcome.decision.decision.value,
            TRADING_REASON_CODE: outcome.decision.primary_reason.value,
        },
        labels=lambda outcome: {
            "decision": outcome.decision.decision.value,
            "strategy": outcome.position.strategy.value,
        },
    )
    def evaluate(
        self,
        position: OpenPosition,
        *,
        as_of: datetime | None = None,
        store: bool = True,
    ) -> ExitEvaluationOutcome:
        """Judge one position. Builds no order and constructs no broker.

        The whole method is a sequence of *reads* followed by one pure call:
        the price from the repository, the thesis from the research store, the
        trailing state from this store, then
        :class:`~trading_system.exit.engine.ExitPolicyEngine`. Nothing between
        those reads can send anything, which is why "evaluation never trades"
        is structural rather than a flag anyone has to check correctly.
        """
        now = as_of or self._clock.now()
        observed_at = self._clock.now()
        strategy_config = strategy_config_for(self._config, position.strategy)
        policy = effective_policy(
            strategy=position.strategy,
            strategy_config=strategy_config,
            exit_config=self._config.exit,
        )

        valuation, point_in_time_detail = read_and_value(
            self.quotes(),
            position.legs,
            symbol=position.underlying,
            as_of=now,
            quote_field=policy.quote_field,
            open_quantity=max(position.observed_units or 0, 0),
            entry_quote=position.entry_quote,
            multiplier=position.multiplier,
            currency=position.currency,
            require_research_usable=policy.require_research_usable,
        )
        if valuation is None:
            valuation = _unpriced(position, policy.quote_field, as_of=now)

        expiration = expiration_view(
            [leg.expiration for leg in position.legs],  # type: ignore[misc]
            as_of=now,
            calendar=self._calendar,
        )
        trailing = self._trailing_for(position, policy=policy, valuation=valuation, at=observed_at)
        thesis = self._thesis_for(position)
        checks = check_conditions(thesis, at=now, return_pct=valuation.return_pct)
        basis, max_loss_total = self._max_loss_for(position, valuation, strategy_config)
        exit_execution = self._exit_execution_for(position)

        fatal_reason: ExitReasonCode | None = None
        if point_in_time_detail is not None:
            fatal_reason = ExitReasonCode.POINT_IN_TIME_ERROR
        elif strategy_config is None:
            fatal_reason = ExitReasonCode.STRATEGY_METADATA_UNAVAILABLE
            point_in_time_detail = (
                f"no configuration file defines {position.strategy.value}, so the exit policy "
                f"this position was opened under cannot be resolved. Managing it under the "
                f"global defaults would apply a policy nobody chose for it"
            )

        inputs = ExitInputs(
            position_id=position.position_id,
            underlying=position.underlying,
            strategy=position.strategy,
            as_of=now,
            evaluated_at=observed_at,
            lifecycle_state=position.lifecycle.state,
            structure_status=position.structure.status,
            expected_quantity=position.expected_units,
            observed_quantity=position.observed_units,
            broker_read_status=position.broker_read_status,
            valuation=valuation,
            expiration=expiration,
            policy=policy,
            versions=self.versions(),
            trading_mode=self._settings.trading_mode,
            trailing=trailing,
            thesis=thesis,
            thesis_checks=tuple(checks),
            max_loss_basis=basis,
            max_loss_total=max_loss_total,
            exit_execution_id=exit_execution.execution_id if exit_execution else None,
            exit_execution_state=exit_execution.state.value if exit_execution else None,
            entry_execution_id=position.entry.execution_id,
            allocation_id=position.entry.allocation_id,
            opportunity_id=position.entry.opportunity_id,
            campaign_id=position.entry.campaign_id,
            research_report_id=position.entry.research_report_id,
            strategy_decision_id=position.entry.strategy_decision_id,
            contract_selection_id=position.entry.contract_selection_id,
            fatal_reason=fatal_reason,
            fatal_detail=point_in_time_detail,
        )

        evaluation = self._engine.evaluate(inputs)
        decision = self._engine.decide(evaluation, trading_mode=self._settings.trading_mode)

        is_new = False
        if store:
            _, is_new = self._repository.save_evaluation(evaluation, decision)
            if trailing is not None:
                self._repository.save_trailing(trailing)
            self._advance_lifecycle(position, decision, trailing=trailing, at=observed_at)

        _logger.info(
            "exit.evaluate",
            position_id=position.position_id,
            symbol=position.underlying,
            decision=decision.decision.value,
            reason=decision.primary_reason.value,
            dte=evaluation.days_to_expiration,
            quote=str(valuation.exit_quote) if valuation.exit_quote is not None else None,
        )
        return ExitEvaluationOutcome(
            position=position,
            evaluation=evaluation,
            decision=decision,
            trailing=trailing,
            stored=store,
            is_new=is_new,
        )

    # --- operation: position.monitor ---------------------------------------
    @traced(
        "position.monitor",
        duration=_metrics.POSITION_MONITOR_DURATION,
        result_attributes=lambda run: {TRADING_STATUS: run.result.status.value},
        labels=lambda run: {"status": run.result.status.value},
    )
    def monitor(
        self,
        *,
        as_of: datetime | None = None,
        position_ids: Sequence[str] | None = None,
        snapshot: BrokerPositionSnapshot | None = None,
        capture: bool = False,
        authorized: bool = False,
        dry_run: bool = False,
        store: bool = True,
    ) -> ExitRun:
        """Evaluate every eligible position, and act only if authorised.

        Safe to run repeatedly and safe to run from a scheduler: nothing is
        held in process memory, the store is idempotent, and a re-run over
        unchanged state re-observes rather than deciding again.

        ``authorized`` is the deliberate exit-execution request. Without it —
        which is the default, and what ``positions monitor`` and
        ``exit evaluate`` use — nothing is built and nothing is sent, however
        many positions triggered.
        """
        started = time.perf_counter()
        now = as_of or self._clock.now()

        if not self._config.exit.enabled:
            return self._empty(
                ExitRunStatus.CONFIGURATION_ERROR,
                detail=(
                    "exit.enabled is false in config/exit.yaml, so no position was evaluated. "
                    "Note this switch governs evaluation only; sending an exit order "
                    "additionally needs execution.enabled and an explicit confirmation"
                ),
                as_of=now,
                started=started,
                dry_run=dry_run,
                authorized=authorized,
            )

        stored_snapshot = snapshot
        if stored_snapshot is None and capture:
            stored_snapshot = self.positions().capture(store=store).snapshot
        if stored_snapshot is None:
            stored_snapshot = self.positions().latest_usable_snapshot()

        if stored_snapshot is None or not stored_snapshot.usable:
            return self._empty(
                ExitRunStatus.BROKER_DATA_UNAVAILABLE,
                detail=(
                    "no usable broker position snapshot is available, so nothing was "
                    "evaluated. This is NOT an empty account: run 'positions snapshot' first. "
                    "No exit decision is made against an absence of data"
                ),
                as_of=now,
                started=started,
                dry_run=dry_run,
                authorized=authorized,
            )

        positions = self.open_positions(
            as_of=now, snapshot=stored_snapshot, position_ids=position_ids
        )
        if not positions:
            return self._empty(
                ExitRunStatus.NO_POSITIONS,
                detail=(
                    "there is no open position to manage. A campaign holding nothing is the "
                    "ordinary answer, not a failure"
                ),
                as_of=now,
                started=started,
                dry_run=dry_run,
                authorized=authorized,
                snapshot_id=stored_snapshot.snapshot_id,
            )

        outcomes = [self.evaluate(p, as_of=now, store=store and not dry_run) for p in positions]

        if authorized and not dry_run:
            outcomes = [self._act(outcome, at=now) for outcome in outcomes]

        submitted = sum(outcome.orders_submitted for outcome in outcomes)
        result = ExitRunResult(
            run_id=exit_run_identifier(
                as_of=now,
                position_ids=[p.position_id for p in positions],
                lifecycle_state=[f"{p.position_id}:{p.lifecycle.state.value}" for p in positions],
                policy_version=self._config.exit.policy_version,
                config_version=self._config.application.config_version,
                trading_mode=self._settings.trading_mode,
                dry_run=dry_run,
            ),
            campaign_id=self._config.campaign.campaign_id,
            as_of=now,
            generated_at=self._clock.now(),
            status=_status_of(outcomes),
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            policy_version=self._config.exit.policy_version,
            execution_authorized=authorized and not dry_run,
            evaluations=[o.evaluation for o in outcomes],
            decisions=[o.decision for o in outcomes],
            counts=_counts(outcomes),
            exit_execution_ids=[
                o.submission.record.execution_id
                for o in outcomes
                if o.submission is not None and o.submission.record is not None
            ],
            position_snapshot_id=stored_snapshot.snapshot_id,
            orders_submitted=submitted,
            versions=self.versions(),
        )
        stored = False
        if store and not dry_run:
            self._repository.save_run(result)
            stored = True

        _logger.info(
            "position.monitor",
            positions=len(positions),
            waiting=result.counts.waiting,
            exiting=result.counts.exiting,
            blocked=result.counts.blocked,
            orders_submitted=submitted,
        )
        return ExitRun(
            result=result,
            outcomes=tuple(outcomes),
            stored=stored,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- operation: exit.execute -------------------------------------------
    def build_request(
        self, outcome: ExitEvaluationOutcome, *, at: datetime, dry_run: bool = False
    ) -> ExitRequest | None:
        """The Milestone 8 boundary artifact, or ``None`` if none is warranted.

        Returns ``None`` — never a request that will be politely declined
        later — whenever an exit must not be built: the decision was not an
        ``EXIT``, the lifecycle says an order may already be live, there is
        nothing held to sell, or there is no reference price. A request object
        that can exist unauthorised is one a caller can forget to check, and
        the same reasoning applies to one that can exist unwarranted.
        """
        decision = outcome.decision
        position = outcome.position
        if decision.decision is not ExitDecisionType.EXIT:
            return None
        if position.lifecycle.state in EXIT_SUBMISSION_BLOCKED_STATES:
            return None
        if not position.observed_units:
            return None
        reference = outcome.evaluation.valuation.exit_quote
        if reference is None or reference <= 0:
            return None

        order = self._config.exit.order
        return ExitRequest(
            exit_request_id=exit_request_identifier(
                position_id=position.position_id,
                entry_execution_id=position.entry.execution_id,
                trading_mode=self._settings.trading_mode,
                order_type=order.order_type,
                time_in_force=order.time_in_force,
                policy_version=self._config.exit.policy_version,
            ),
            position_id=position.position_id,
            decision_id=decision.decision_id,
            evaluation_id=outcome.evaluation.evaluation_id,
            created_at=at,
            exit_authorized=True,
            underlying=position.underlying,
            strategy=position.strategy,
            quantity=position.observed_units,
            close_whole_strategy=True,
            exit_reason=decision.primary_reason,
            triggering_policy=decision.triggering_policy or outcome.evaluation.outcomes[0].policy,
            reference_quote=reference,
            quote_field=outcome.evaluation.valuation.quote_field,
            quote_as_of=outcome.evaluation.valuation.as_of,
            currency=position.currency,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            entry_execution_id=position.entry.execution_id,
            allocation_id=position.entry.allocation_id,
            campaign_id=position.entry.campaign_id,
            opportunity_id=position.entry.opportunity_id,
            purchase_card_id=position.entry.purchase_card_id,
            risk_decision_id=position.entry.risk_decision_id,
            policy_version=self._config.exit.policy_version,
            versions=self.versions(),
        )

    @traced(
        "exit.execute",
        attributes=lambda self, outcome, **kwargs: {
            TRADING_POSITION_ID: outcome.position.position_id,
            TRADING_EXIT_ID: outcome.decision.decision_id,
        },
        count=_metrics.EXIT_TRIGGERED_TOTAL,
        labels=lambda outcome: {"strategy": outcome.position.strategy.value},
    )
    def _act(self, outcome: ExitEvaluationOutcome, *, at: datetime) -> ExitEvaluationOutcome:
        """Hand one triggered exit to Milestone 8, and record what came back."""
        request = self.build_request(outcome, at=at)
        if request is None:
            return outcome

        submission = self.executions().submit_exit(
            request, entry=outcome.position.entry, authorized=True, at=at
        )
        if submission.record is not None:
            self._record_submission(outcome, submission, at=at)
        _logger.info(
            "exit.execute",
            position_id=outcome.position.position_id,
            exit_request_id=request.exit_request_id,
            execution_id=(
                submission.record.execution_id if submission.record is not None else None
            ),
            state=submission.record.state.value if submission.record is not None else None,
            reason=[code.value for code in submission.reason_codes],
            orders_submitted=submission.orders_submitted,
        )
        return ExitEvaluationOutcome(
            position=outcome.position,
            evaluation=outcome.evaluation,
            decision=outcome.decision,
            trailing=outcome.trailing,
            stored=outcome.stored,
            is_new=outcome.is_new,
            submission=submission,
        )

    def _record_submission(
        self, outcome: ExitEvaluationOutcome, submission: ExitSubmission, *, at: datetime
    ) -> None:
        """Move the lifecycle to match what Milestone 8 reports.

        ``UNKNOWN`` and ``SUBMITTED`` are recorded as different states, because
        they permit different things next: waiting, and asking the broker. A
        submission that provably failed leaves the lifecycle where it was — the
        position is still open, still required to exit, and a later run may try
        again because nothing was sent.
        """
        record = submission.record
        assert record is not None
        if record.state is ExecutionState.UNKNOWN:
            state = PositionLifecycleState.EXIT_UNKNOWN
            event_type = PositionLifecycleEventType.EXIT_STATE_UNKNOWN
            reason: ExitReasonCode | None = None
        elif record.submitted:
            state = PositionLifecycleState.EXIT_SUBMITTED
            event_type = PositionLifecycleEventType.EXIT_SUBMITTED
            reason = None
        else:
            return

        self._append_lifecycle(
            outcome.position,
            state=state,
            event_type=event_type,
            at=at,
            reason_code=reason,
            decision=outcome.decision,
            exit_execution_id=record.execution_id,
            exit_request_id=submission.request.exit_request_id,
            detail=(
                f"exit execution {record.execution_id} is {record.state.value}"
                + (
                    ". The order may be live at the broker; it is resolved by observation, "
                    "never by sending again"
                    if record.state is ExecutionState.UNKNOWN
                    else ""
                )
            ),
        )

    # --- operation: exit.confirm -------------------------------------------
    @traced(
        "exit.confirm",
        count=_metrics.POSITIONS_CLOSED_TOTAL,
        labels=lambda confirmed: {"confirmed": str(len(confirmed))},
    )
    def confirm(
        self, *, as_of: datetime | None = None, snapshot: BrokerPositionSnapshot | None = None
    ) -> list[PositionLifecycleSnapshot]:
        """Close out the lifecycle of positions broker reality says are gone.

        **Milestone 9 decides, not this service.** A position becomes
        ``CLOSED`` when the broker reports it holds none of the structure — not
        when an exit order was submitted, not when Milestone 8 reported a fill,
        and not when this service decided to exit. Between the submission and
        the confirmation the position stays ``EXIT_SUBMITTED``, which is the
        honest state.

        An ``EXIT_UNKNOWN`` position that the broker turns out to hold is moved
        to ``BLOCKED`` rather than back to monitoring: the exit may still be
        working, and a position that quietly resumed monitoring could be sold a
        second time by the next run.
        """
        now = as_of or self._clock.now()
        confirmed: list[PositionLifecycleSnapshot] = []
        for position in self.open_positions(as_of=now, snapshot=snapshot):
            lifecycle = position.lifecycle
            if lifecycle.terminal:
                continue
            if position.observed_units == 0:
                confirmed.append(
                    self._append_lifecycle(
                        position,
                        state=PositionLifecycleState.CLOSED,
                        event_type=PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED,
                        at=now,
                        open_quantity=0,
                        detail=(
                            "the broker reports none of this structure. Closure is broker "
                            "reality, never an inference from a submitted order"
                        ),
                    )
                )
                continue
            if lifecycle.state is PositionLifecycleState.EXIT_UNKNOWN:
                confirmed.append(
                    self._append_lifecycle(
                        position,
                        state=PositionLifecycleState.BLOCKED,
                        event_type=PositionLifecycleEventType.LIFECYCLE_BLOCKED,
                        at=now,
                        reason_code=ExitReasonCode.EXIT_OUTCOME_UNKNOWN,
                        detail=(
                            "an exit was sent, its outcome was never learned, and the broker "
                            "still holds the structure. The order may be working right now, so "
                            "this position is blocked rather than returned to monitoring"
                        ),
                    )
                )
        return confirmed

    # --- internals ---------------------------------------------------------
    def _trailing_for(
        self,
        position: OpenPosition,
        *,
        policy: object,
        valuation: PositionValuation,
        at: datetime,
    ) -> TrailingStopRecord | None:
        """Load the trail, apply this observation, and return the new state.

        Loaded from the store on every evaluation rather than kept in memory:
        that is what makes the restart guarantee real. A process that dies
        between two monitoring cycles loses nothing, and reloading the record
        and replaying the same observation produces the same state.
        """
        from trading_system.exit.models import ExitPolicySnapshot

        assert isinstance(policy, ExitPolicySnapshot)
        if not policy.trailing_enabled:
            return None

        record = self._repository.trailing(position.position_id)
        if record is None:
            record = new_trailing_record(
                position_id=position.position_id,
                entry_quote=position.entry_quote,
                config=self._config.exit.trailing,
                quote_field=policy.quote_field,
                distance_pct=policy.trailing_distance_pct,
                activation_return_pct=policy.trailing_activation_return_pct,
                created_at=at,
            )
        if valuation.exit_quote is None:
            # No usable price: the trail is neither advanced nor reset. A stop
            # that moved on an absent observation would be following a price
            # nobody saw.
            return record
        return observe(record, observed_quote=valuation.exit_quote, at=at).record

    def _thesis_for(self, position: OpenPosition) -> ThesisView:
        """Read the Milestone 5 report this position rests on.

        Consumed, never re-derived: the exit engine reads the conditions
        research already wrote down and asks no model to reconstruct them.
        """
        report_id = position.entry.research_report_id
        if not report_id:
            return ThesisView(
                unavailable=True,
                detail=(
                    "the entry execution names no research report, so there is no stated "
                    "thesis to check this position against"
                ),
            )
        try:
            from trading_system.research.store import FilesystemResearchRepository

            repository = FilesystemResearchRepository(self._data_root / "research")
            for entry in repository.history():
                run = repository.get(entry.run_id)
                for report in run.reports if run else []:
                    if report.report_id == report_id:
                        return _thesis_view_of(report)
        except Exception as exc:  # a store fault, not a market event
            return ThesisView(
                unavailable=True, detail=f"the research store could not be read: {exc!r}"
            )
        return ThesisView(
            unavailable=True,
            detail=f"research report {report_id} is not in the store",
        )

    def _max_loss_for(
        self,
        position: OpenPosition,
        valuation: PositionValuation,
        strategy_config: object | None,
    ) -> tuple[MaxLossBasis | None, Decimal | None]:
        """The strategy's declared maximum-loss basis, and the money it bounds.

        **Milestone 7's basis, read rather than re-derived.** The basis is
        declared on the strategy's own
        :class:`~trading_system.strategies.base.StrategyStructure`, in code,
        because it is a property of the payoff. ``NET_DEBIT_PAID`` means the
        most that can be lost is what was paid, so the money at risk is the
        entry cost times what is actually held — Milestone 7's own arithmetic
        applied to the quantity that filled. Any other basis returns ``None``,
        and the policy blocks rather than estimating.
        """
        from trading_system.strategies.registry import STRUCTURES

        structure = STRUCTURES.get(position.strategy)
        basis = structure.max_loss_basis if structure else None
        if basis is not MaxLossBasis.NET_DEBIT_PAID:
            return basis, None
        entry_total = valuation.entry_total
        return basis, entry_total

    def _exit_execution_for(self, position: OpenPosition) -> ExecutionRecord | None:
        """The exit attempt this position is waiting on, if any."""
        execution_id = position.lifecycle.exit_execution_id
        if not execution_id:
            return None
        return self.executions().get(execution_id)

    def _advance_lifecycle(
        self,
        position: OpenPosition,
        decision: ExitDecisionRecord,
        *,
        trailing: TrailingStopRecord | None,
        at: datetime,
    ) -> PositionLifecycleSnapshot:
        """Move the lifecycle to whatever this evaluation established.

        The mapping is deliberately narrow: an evaluation can start monitoring,
        arm a trail, require an exit or block. It can never mark a position
        closed — only :meth:`confirm` does that, from broker reality — and it
        can never move a position out of ``EXIT_SUBMITTED`` or
        ``EXIT_UNKNOWN``, because doing so would permit a second order.
        """
        current = position.lifecycle.state
        if (
            decision.primary_reason is ExitReasonCode.POSITION_CLOSED
            and position.observed_units == 0
        ):
            # Broker reality, not an inference. The broker reports none of this
            # structure, which is the only thing that closes a position — not a
            # submitted order, not a reported fill, not a decision to exit.
            target = PositionLifecycleState.CLOSED
            event_type = PositionLifecycleEventType.EXIT_CONFIRMED_CLOSED
        elif decision.decision is ExitDecisionType.BLOCK:
            target = PositionLifecycleState.BLOCKED
            event_type = PositionLifecycleEventType.LIFECYCLE_BLOCKED
        elif decision.decision is ExitDecisionType.EXIT:
            target = PositionLifecycleState.EXIT_REQUIRED
            event_type = PositionLifecycleEventType.EXIT_REQUIRED
        elif trailing is not None and trailing.state in (
            TrailingStopState.ARMED,
            TrailingStopState.ACTIVE,
        ):
            target = PositionLifecycleState.TRAILING_ACTIVE
            event_type = PositionLifecycleEventType.TRAILING_ACTIVATED
        else:
            target = PositionLifecycleState.MONITORING
            event_type = PositionLifecycleEventType.LIFECYCLE_MONITORED

        if (
            current in EXIT_SUBMISSION_BLOCKED_STATES
            and target is not PositionLifecycleState.CLOSED
        ):
            # An evaluation may never move a position out of a state where an
            # exit order may be live. Only :meth:`confirm` does that, from
            # broker reality. The graph alone is not enough here: ``EXIT_UNKNOWN
            # -> BLOCKED`` is a *legal* edge (that is how a confirmation records
            # an exit the broker did not take), so without this an ordinary
            # block — a stale quote, say — would move the position out of
            # EXIT_UNKNOWN and quietly restore the ability to send a second
            # order for a position that may already have been sold.
            target, event_type = current, PositionLifecycleEventType.LIFECYCLE_OBSERVED
        elif target is current:
            event_type = PositionLifecycleEventType.LIFECYCLE_OBSERVED
        elif not _reachable(current, target):
            # The lifecycle graph refuses this move. Record the observation
            # without the transition: the evaluation happened and is worth
            # keeping, and the state stays exactly where it is.
            target, event_type = current, PositionLifecycleEventType.LIFECYCLE_OBSERVED

        return self._append_lifecycle(
            position,
            state=target,
            event_type=event_type,
            at=at,
            reason_code=(
                decision.primary_reason if target is PositionLifecycleState.BLOCKED else None
            ),
            decision=decision,
            trailing=trailing,
            detail=decision.summary,
        )

    def _append_lifecycle(
        self,
        position: OpenPosition,
        *,
        state: PositionLifecycleState,
        event_type: PositionLifecycleEventType,
        at: datetime,
        reason_code: ExitReasonCode | None = None,
        decision: ExitDecisionRecord | None = None,
        trailing: TrailingStopRecord | None = None,
        exit_execution_id: str | None = None,
        exit_request_id: str | None = None,
        open_quantity: int | None = None,
        detail: str | None = None,
    ) -> PositionLifecycleSnapshot:
        """Persist one lifecycle observation and return the folded record.

        The base record is written once, on the first observation, and every
        later movement is an appended event folded onto it. That is why the
        base is only saved when the store has none: an edited anchor would
        silently change what every stored event means.
        """
        if self._repository.lifecycle(position.position_id) is None:
            self._repository.save_lifecycle(position.lifecycle)
        sequence = len(self._repository.lifecycle_events(position.position_id))
        event = PositionLifecycleEvent(
            event_id=lifecycle_event_identifier(
                position_id=position.position_id,
                sequence=sequence,
                event_type=event_type.value,
            ),
            position_id=position.position_id,
            sequence=sequence,
            event_type=event_type,
            state=state,
            occurred_at=at,
            observed_at=at,
            source="exit",
            decision=decision.decision if decision else None,
            reason_code=reason_code,
            detail=detail,
            last_evaluation_id=decision.evaluation_id if decision else None,
            exit_decision_id=decision.decision_id if decision else None,
            exit_request_id=exit_request_id,
            exit_execution_id=exit_execution_id,
            open_quantity=(
                open_quantity
                if open_quantity is not None
                else (position.observed_units if position.observed_units is not None else None)
            ),
            peak_quote=trailing.peak_quote if trailing else None,
            stop_quote=trailing.stop_quote if trailing else None,
            observed_quote=trailing.trigger_quote if trailing else None,
        )
        self._repository.append_lifecycle_event(event)
        return self._repository.lifecycle(position.position_id) or position.lifecycle

    def _empty(
        self,
        status: ExitRunStatus,
        *,
        detail: str,
        as_of: datetime,
        started: float,
        dry_run: bool,
        authorized: bool,
        snapshot_id: str | None = None,
    ) -> ExitRun:
        result = ExitRunResult(
            run_id=exit_run_identifier(
                as_of=as_of,
                position_ids=[],
                lifecycle_state=None,
                policy_version=self._config.exit.policy_version,
                config_version=self._config.application.config_version,
                trading_mode=self._settings.trading_mode,
                dry_run=dry_run,
            ),
            campaign_id=self._config.campaign.campaign_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=status,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            policy_version=self._config.exit.policy_version,
            execution_authorized=authorized and not dry_run,
            position_snapshot_id=snapshot_id,
            orders_submitted=0,
            versions=self.versions(),
            status_detail=detail,
        )
        return ExitRun(
            result=result,
            outcomes=(),
            stored=False,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _observed_units(legs: Sequence[HeldLeg]) -> int | None:
    """Units of the structure the broker reports, from the weakest leg.

    ``None`` when any leg's quantity is unknown — never zero, which is the
    broker saying it holds none. A structure is present in as many units as its
    least-held leg supports: two calls and one put is one straddle and a spare
    call, and treating it as two would authorise an exit for contracts that do
    not exist.
    """
    if not legs:
        return None
    units: list[int] = []
    for leg in legs:
        if leg.observed_quantity is None:
            return None
        ratio = max(leg.ratio, 1)
        units.append(int(abs(leg.observed_quantity) // ratio))
    return min(units)


def _unpriced(position: OpenPosition, quote_field: object, *, as_of: datetime) -> PositionValuation:
    """A valuation with no price at all, for a position that could not be read."""
    from trading_system.domain.enums import ExitQuoteField
    from trading_system.exit.models import ExitLegValuation

    field = quote_field if isinstance(quote_field, ExitQuoteField) else ExitQuoteField.BID
    return PositionValuation(
        as_of=as_of,
        quote_field=field,
        multiplier=position.multiplier,
        open_quantity=max(position.observed_units or 0, 0),
        currency=position.currency,
        legs=[
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
                quote_field=field,
                price=None,
                detail="no quote could be read for this leg",
            )
            for leg in position.legs
        ],
        entry_quote=position.entry_quote,
        entry_cost=(
            position.entry_quote * Decimal(position.multiplier)
            if position.entry_quote is not None and position.multiplier
            else None
        ),
        unpriced_legs=[leg.leg_index for leg in position.legs],
        detail="no usable quote was available for any leg of this structure",
    )


def _thesis_view_of(report: object) -> ThesisView:
    """Project a Milestone 5 report onto the facts a deterministic check can use.

    Deliberately narrow. Evidence, sources, confidence, the thesis prose and
    the agent's rationale are all left behind: the exit engine has no business
    reading them, and a shape that cannot carry them cannot be tempted to
    interpret them.

    The dated catalysts come from ``key_events`` rather than from the
    ``bullish_catalysts``/``bearish_catalysts`` lists, and the difference
    matters. A :class:`~trading_system.research.models.Catalyst` is a *summary*
    with no date on it, so nothing about it is deterministically checkable; a
    :class:`~trading_system.research.models.ReportedEvent` carries
    ``expected_event_time``, which is a structured fact about a calendar. Only
    the second can settle an invalidation condition.
    """
    conditions = tuple(
        (condition.condition, condition.observable)
        for condition in getattr(report, "invalidation_conditions", None) or []
    )
    catalysts = tuple(
        (event.summary, event.expected_event_time)
        for event in getattr(report, "key_events", None) or []
    )
    direction = getattr(report, "direction", None)
    return ThesisView(
        conditions=conditions,
        as_of=getattr(report, "as_of", None),
        horizon_days=getattr(report, "horizon_days", None),
        catalysts=catalysts,
        direction=getattr(direction, "value", None) if direction is not None else None,
        unavailable=False,
    )


def _reachable(current: PositionLifecycleState, target: PositionLifecycleState) -> bool:
    from trading_system.exit.lifecycle import can_transition

    return can_transition(current, target)


def _counts(outcomes: Sequence[ExitEvaluationOutcome]) -> ExitRunCounts:
    return ExitRunCounts(
        evaluated=len(outcomes),
        waiting=sum(1 for o in outcomes if o.decision.decision is ExitDecisionType.WAIT),
        exiting=sum(1 for o in outcomes if o.decision.decision is ExitDecisionType.EXIT),
        blocked=sum(1 for o in outcomes if o.decision.decision is ExitDecisionType.BLOCK),
        closed=sum(
            1 for o in outcomes if o.decision.primary_reason is ExitReasonCode.POSITION_CLOSED
        ),
        exits_submitted=sum(
            1 for o in outcomes if o.submission is not None and o.submission.submitted
        ),
        exits_refused=sum(1 for o in outcomes if o.submission is not None and o.submission.refused),
    )


def _status_of(outcomes: Sequence[ExitEvaluationOutcome]) -> ExitRunStatus:
    """A run's status, derived from its own decisions.

    A run with any block is ``PARTIAL``, never ``SUCCESS``: a position nobody
    could judge is not a success, and calling it one is how it stops being
    looked at.
    """
    if not outcomes:
        return ExitRunStatus.NO_POSITIONS
    if any(o.decision.decision is ExitDecisionType.BLOCK for o in outcomes):
        return ExitRunStatus.PARTIAL
    return ExitRunStatus.SUCCESS


#: The structural leg direction an exit reverses. Exported so a test can assert
#: the exit path never *opens* anything: every leg this milestone sends is the
#: opposite of one that was bought.
CLOSING_ACTION = LegAction.SELL
