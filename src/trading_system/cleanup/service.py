"""The cleanup service: the composition root for orphan-position cleanup.

One operation, and it orchestrates three services that already made their own
decisions rather than making any of its own:

.. code-block:: text

    ReconciliationService.run()      what the broker holds, and which of it
          |                          is an ORPHAN_BROKER_POSITION
    select_targets()                 PURE: by broker contract id only
          |
    evaluate_run_gates()             PURE: mode, guards, config, freshness
    evaluate_target_gates()          PURE: long, priced, no working order
          |
    ExecutionService.submit_cleanup()    the ONLY path to an order
          |
    PositionService.capture()        broker reality AFTER
          |
    ReconciliationService.run()      the normal, unmodified comparison
          |
    immutable OrphanCleanupRun

Properties this service holds regardless of which path it takes:

* **It holds no broker.** It has no writable factory and no import that reaches
  one; a boundary test walks the transitive graph. An order exists only because
  :meth:`~trading_system.execution.service.ExecutionService.submit_cleanup`
  made one, under Milestone 8's own switches and its own idempotency.
* **It adopts nothing.** No allocation, purchase card, risk decision,
  opportunity, strategy, research report, expected position or strategy
  position is created for a holding this system never opened, and the execution
  record refuses to carry one.
* **It moves no campaign money.** No reservation, no budget, no realised
  profit or loss. ``ExecutionIntent.CLEANUP`` is excluded from the reservation
  ledger, from the expected-position ledger and from the profit-and-loss
  ledger, in each case by the ledger itself rather than by anything here.
* **It never retries.** A rejected order is reported, an ``UNKNOWN`` one blocks
  everything about that holding, and neither produces a second submission.
* **It reports; it never repairs.** Nothing here edits a reconciliation, a
  finding, an execution or a position into agreement.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system import __version__ as application_version
from trading_system.cleanup.gates import GateVerdict, evaluate_run_gates, evaluate_target_gates
from trading_system.cleanup.models import (
    CLEANUP_SCHEMA_VERSION,
    CleanupOutcome,
    CleanupOutcomeStatus,
    CleanupRunStatus,
    CleanupTarget,
    OrphanCleanupRequest,
    OrphanCleanupRun,
    cleanup_request_identifier,
    cleanup_run_identifier,
)
from trading_system.cleanup.store import CleanupRepository, FilesystemCleanupRepository
from trading_system.cleanup.targets import TargetSelection, select_targets
from trading_system.domain.enums import ExecutionState
from trading_system.domain.models import SystemVersions
from trading_system.execution.service import CleanupSubmission, ExecutionService
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import TRADING_RECONCILIATION_ID, TRADING_STATUS
from trading_system.observability.instrument import traced
from trading_system.observability.tracing import current_trace_context
from trading_system.positions.models import BrokerPositionSnapshot
from trading_system.reconciliation.models import ReconciliationResult
from trading_system.reconciliation.service import ReconciliationRun, ReconciliationService

__all__ = ["CleanupPlan", "CleanupRunOutcome", "CleanupService"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """Everything decided before anything is sent.

    This is what a review shows, and it is deliberately a complete answer to
    "what exactly would be submitted": every target with its identity and
    quantity, every gate with its verdict, and the order that would go out for
    each one. A review that showed less would be a weaker promise than the one
    this operation makes.
    """

    selection: TargetSelection
    request: OrphanCleanupRequest | None
    run_gates: tuple[GateVerdict, ...] = ()
    target_gates: dict[str, tuple[GateVerdict, ...]] = field(default_factory=dict)
    submissions: tuple[CleanupSubmission, ...] = ()
    detail: str | None = None

    @property
    def gates_passed(self) -> bool:
        return all(verdict.passed for verdict in self.run_gates) and all(
            verdict.passed for verdicts in self.target_gates.values() for verdict in verdicts
        )

    @property
    def blocking(self) -> tuple[GateVerdict, ...]:
        return tuple(verdict for verdict in self.run_gates if not verdict.passed) + tuple(
            verdict
            for verdicts in self.target_gates.values()
            for verdict in verdicts
            if not verdict.passed
        )


@dataclass(frozen=True, slots=True)
class CleanupRunOutcome:
    """One invocation's outcome, plus what the caller needs to report it."""

    run: OrphanCleanupRun
    plan: CleanupPlan
    before: ReconciliationRun | None = None
    after: ReconciliationRun | None = None
    stored: bool = False
    is_new: bool = False
    duration_seconds: float = 0.0

    @property
    def orders_submitted(self) -> int:
        return self.run.orders_submitted


class CleanupService:
    """Close pre-existing broker holdings an operator explicitly named."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        reconciliation_service: ReconciliationService | None = None,
        execution_service: ExecutionService | None = None,
        repository: CleanupRepository | None = None,
        root: Path | str | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (Path(root) if root is not None else project_root()) / data_root
        self._data_root = data_root

        project = Path(root) if root is not None else None
        self._reconciliation = reconciliation_service or ReconciliationService(
            settings=settings, config=config, clock=self._clock, root=project
        )
        self._executions = execution_service or ExecutionService(
            settings=settings, config=config, clock=self._clock, root=project
        )
        self._repository = repository or FilesystemCleanupRepository(data_root / "cleanup")

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> CleanupRepository:
        return self._repository

    @property
    def reconciliation(self) -> ReconciliationService:
        return self._reconciliation

    @property
    def executions(self) -> ExecutionService:
        return self._executions

    @property
    def enabled(self) -> bool:
        return self._config.cleanup.enabled

    def versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            strategy_spec_version=CLEANUP_SCHEMA_VERSION,
        )

    # --- the run -----------------------------------------------------------
    @traced(
        "orphan.cleanup",
        count=_metrics.RECONCILIATION_RUNS_TOTAL,
        duration=_metrics.RECONCILIATION_DURATION,
        result_attributes=lambda outcome: {
            TRADING_RECONCILIATION_ID: outcome.run.source_reconciliation_id,
            TRADING_STATUS: outcome.run.status.value,
        },
        labels=lambda outcome: {"status": outcome.run.status.value},
    )
    def run(
        self,
        *,
        authorized: bool = False,
        contract_ids: Sequence[int] | None = None,
        reconciliation_id: str | None = None,
        as_of: datetime | None = None,
    ) -> CleanupRunOutcome:
        """Observe, decide, close what was authorised, observe again.

        ``authorized=False`` — the default, and what an operator gets by simply
        running the command — evaluates everything, builds every order, opens
        **no writable broker at all** and submits nothing. That is structural
        rather than a flag anyone has to check correctly: the dry-run path never
        reaches the one method that can construct one.

        The broker is read before and after through the ordinary
        reconciliation workflow, which is unmodified. This operation cannot
        hide an orphan finding, because it does not compute one.
        """
        started = time.perf_counter()
        dry_run = not authorized
        now = as_of or self._clock.now()

        # 1. Broker observation BEFORE. A stored reconciliation may be named
        #    explicitly, which is what lets an operator authorise exactly the
        #    report they reviewed; otherwise a fresh one is run.
        before: ReconciliationRun | None = None
        if reconciliation_id is not None:
            result = self._reconciliation.get(reconciliation_id)
            if result is None:
                return self._nothing(
                    now,
                    started,
                    detail=f"no stored reconciliation {reconciliation_id}",
                    status=CleanupRunStatus.NO_TARGETS,
                )
            snapshot = self._snapshot_for(result)
        else:
            before = self._reconciliation.run(as_of=now, dry_run=dry_run)
            result = before.result
            snapshot = before.capture.snapshot

        if snapshot is None or not snapshot.read_status.usable:
            return self._nothing(
                now,
                started,
                detail=(
                    f"reconciliation {result.reconciliation_id} has no usable position "
                    f"snapshot, so there is nothing a target could be compared against. "
                    f"'We could not look' is not 'the account holds nothing', and only the "
                    f"second could authorise anything"
                ),
                status=CleanupRunStatus.NO_TARGETS,
                source_reconciliation_id=result.reconciliation_id,
                before=before,
            )

        # 2. Selection. Pure, and narrow: only a reported orphan, only by the
        #    broker's own contract id, only at the quantity that was reported.
        selection = select_targets(
            result=result, snapshot=snapshot, wanted_contract_ids=contract_ids
        )
        targets = selection.targets

        request = (
            self._request_for(selection, result=result, at=now, dry_run=dry_run)
            if targets
            else None
        )

        # 3. Gates. All of them, before any broker is constructed.
        working = self._working_order_contract_ids(before)
        run_gates = evaluate_run_gates(
            settings=self._settings,
            cleanup=self._config.cleanup,
            execution=self._config.execution,
            authorized=authorized,
            dry_run=dry_run,
            result=result,
            target_count=len(targets),
            at=now,
            # The account the BROKER reported on this run's own observation.
            # Reading it from settings would compare the configuration against
            # itself and prove nothing at all.
            broker_account_id=before.capture.state.account_id if before is not None else None,
        )
        target_gates = {
            target.key: evaluate_target_gates(
                target=target,
                cleanup=self._config.cleanup,
                working_order_contract_ids=working,
            )
            for target in targets
        }

        plan = CleanupPlan(
            selection=selection, request=request, run_gates=run_gates, target_gates=target_gates
        )

        if request is None:
            return self._nothing(
                now,
                started,
                detail=(
                    f"reconciliation {result.reconciliation_id} reports "
                    f"{selection.orphan_count} orphan finding(s) and none of them is currently "
                    f"targetable"
                ),
                status=CleanupRunStatus.NO_TARGETS,
                source_reconciliation_id=result.reconciliation_id,
                plan=plan,
                before=before,
            )

        # 4. Submission — or, for every path that is not a fully-gated,
        #    explicitly authorised run, no submission at all.
        run_blocked = [verdict for verdict in run_gates if not verdict.passed]
        submissions: list[CleanupSubmission] = []
        outcomes: list[CleanupOutcome] = []

        for target in targets:
            blocked = list(run_blocked) + [
                verdict for verdict in target_gates[target.key] if not verdict.passed
            ]
            if blocked and not dry_run:
                outcomes.append(self._refused(target, blocked))
                continue
            submission = self._executions.submit_cleanup(
                target,
                cleanup_request_id=request.cleanup_request_id,
                campaign_id=self._config.campaign.campaign_id,
                authorized=authorized,
                dry_run=dry_run,
            )
            submissions.append(submission)
            outcomes.append(self._outcome(target, submission, blocked=blocked))

        plan = CleanupPlan(
            selection=selection,
            request=request,
            run_gates=run_gates,
            target_gates=target_gates,
            submissions=tuple(submissions),
        )

        # 5. Broker observation AFTER, then the ordinary reconciliation. Only
        #    a position read can close a target: a reported fill is a claim
        #    about an order, not about the account.
        after: ReconciliationRun | None = None
        if not dry_run and any(outcome.orders_submitted for outcome in outcomes):
            after = self._reconciliation.run(dry_run=False)
            outcomes = [self._observe(outcome, after) for outcome in outcomes]

        submitted = sum(outcome.orders_submitted for outcome in outcomes)
        run = OrphanCleanupRun(
            run_id=cleanup_run_identifier(
                request_id=request.cleanup_request_id,
                as_of=now,
                outcomes=[f"{outcome.key}:{outcome.status.value}" for outcome in outcomes],
                dry_run=dry_run,
            ),
            cleanup_request_id=request.cleanup_request_id,
            source_reconciliation_id=result.reconciliation_id,
            result_reconciliation_id=after.result.reconciliation_id if after else None,
            account_reference=result.account_reference,
            campaign_id=self._config.campaign.campaign_id,
            as_of=now,
            generated_at=self._clock.now(),
            status=_status_of(outcomes, dry_run=dry_run),
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            broker=result.broker,
            outcomes=outcomes,
            gates=[verdict.render() for verdict in run_gates]
            + [
                f"{key}  {verdict.render()}"
                for key, verdicts in sorted(target_gates.items())
                for verdict in verdicts
            ],
            orders_submitted=submitted,
            trace_id=current_trace_context()[0],
            policy_version=self._config.application.config_version,
            versions=self.versions(),
        )

        stored = False
        is_new = False
        if not dry_run:
            self._repository.save_request(request)
            _, is_new = self._repository.save_run(run)
            stored = True

        _logger.info(
            "cleanup.run",
            run_id=run.run_id,
            status=run.status.value,
            targets=len(outcomes),
            closed=run.closed,
            orders_submitted=run.orders_submitted,
            dry_run=dry_run,
        )
        return CleanupRunOutcome(
            run=run,
            plan=plan,
            before=before,
            after=after,
            stored=stored,
            is_new=is_new,
            duration_seconds=time.perf_counter() - started,
        )

    # --- history -----------------------------------------------------------
    def latest(self) -> OrphanCleanupRun | None:
        return self._repository.latest_run()

    def get(self, run_id: str) -> OrphanCleanupRun | None:
        return self._repository.get_run(run_id)

    def history(self, limit: int | None = None) -> list[object]:
        return list(self._repository.history(limit=limit))

    # --- internals ---------------------------------------------------------
    def _snapshot_for(self, result: ReconciliationResult) -> BrokerPositionSnapshot | None:
        """The snapshot a stored reconciliation compared against.

        Read back by id rather than re-captured: the targets must be the ones
        the operator reviewed, and a fresh capture could differ. The gates then
        decide whether that snapshot is recent enough to act on.
        """
        if result.position_snapshot_id is None:
            return None
        return self._reconciliation.positions.repository.get_snapshot(result.position_snapshot_id)

    def _working_order_contract_ids(self, before: ReconciliationRun | None) -> frozenset[int]:
        """Contracts with an order working at the broker right now.

        Read from the observation this run made. When none was made — a stored
        reconciliation was named instead — the set is empty and the gate cannot
        fire; that is why naming a stored reconciliation still requires it to
        be fresh, and why a stale one is refused.
        """
        if before is None:
            return frozenset()
        return frozenset(
            order.contract_id
            for order in before.capture.state.orders
            if order.contract_id is not None
        )

    def _request_for(
        self,
        selection: TargetSelection,
        *,
        result: ReconciliationResult,
        at: datetime,
        dry_run: bool,
    ) -> OrphanCleanupRequest:
        targets = selection.targets
        return OrphanCleanupRequest(
            cleanup_request_id=cleanup_request_identifier(
                account_reference=result.account_reference,
                reconciliation_id=result.reconciliation_id,
                contract_keys=[target.key for target in targets],
                trading_mode=self._settings.trading_mode,
                policy_version=self._config.application.config_version,
            ),
            source_reconciliation_id=result.reconciliation_id,
            account_reference=result.account_reference,
            campaign_id=self._config.campaign.campaign_id,
            requested_at=at,
            targets=list(targets),
            cleanup_authorized=True,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            policy_version=self._config.application.config_version,
            versions=self.versions(),
        )

    def _refused(self, target: CleanupTarget, blocked: list[GateVerdict]) -> CleanupOutcome:
        return CleanupOutcome(
            key=target.key,
            contract_id=target.contract_id,
            symbol=target.symbol,
            describe=target.describe(),
            status=CleanupOutcomeStatus.REFUSED,
            observed_quantity_before=target.quantity,
            requested_quantity=int(target.quantity) if target.quantity > 0 else 0,
            gate_failures=[verdict.render() for verdict in blocked],
            detail="; ".join(verdict.detail for verdict in blocked),
        )

    def _outcome(
        self,
        target: CleanupTarget,
        submission: CleanupSubmission,
        *,
        blocked: list[GateVerdict],
    ) -> CleanupOutcome:
        record = submission.record
        status = _outcome_status(submission)
        return CleanupOutcome(
            key=target.key,
            contract_id=target.contract_id,
            symbol=target.symbol,
            describe=target.describe(),
            status=status,
            observed_quantity_before=target.quantity,
            requested_quantity=record.quantity if record else 0,
            execution_id=record.execution_id if record else None,
            execution_request_id=record.execution_request_id if record else None,
            order_intent_id=submission.intent.intent_id if submission.intent else None,
            broker_order_id=record.broker_order_id if record else None,
            execution_state=record.state if record else None,
            limit_price=record.submitted_price if record else None,
            reference_quote=target.market_price,
            filled_quantity=record.filled_quantity if record else 0,
            average_fill_price=record.average_fill_price if record else None,
            orders_submitted=submission.orders_submitted,
            reason_codes=list(submission.reason_codes)
            + [code for code in (record.reason_codes if record else []) if code],
            gate_failures=[verdict.render() for verdict in blocked],
            detail=submission.detail or (record.failure_reason if record else None),
        )

    def _observe(self, outcome: CleanupOutcome, after: ReconciliationRun) -> CleanupOutcome:
        """Fold the post-cleanup broker read onto one outcome.

        The only place a target becomes ``CLOSED``, and it takes a *position*
        read to do it. A snapshot that could not be read leaves the outcome
        exactly as the submission left it: not knowing is not the same as
        knowing the holding is gone.
        """
        snapshot = after.capture.snapshot
        if snapshot is None or not snapshot.read_status.usable:
            return outcome
        observed = next(
            (position for position in snapshot.positions if position.key == outcome.key), None
        )
        remaining = observed.quantity if observed is not None else Decimal("0")
        status = outcome.status
        if outcome.status in (
            CleanupOutcomeStatus.CLOSED,
            CleanupOutcomeStatus.PARTIALLY_CLOSED,
            CleanupOutcomeStatus.WORKING,
        ):
            # The POSITION READ governs, not the fill report. A broker that
            # acknowledged a fill and still reports the whole holding has not
            # moved it yet, and calling that PARTIALLY_CLOSED on the strength
            # of the fill alone would report a reduction the account does not
            # show.
            if remaining == 0:
                status = CleanupOutcomeStatus.CLOSED
            elif remaining < outcome.observed_quantity_before:
                status = CleanupOutcomeStatus.PARTIALLY_CLOSED
            else:
                status = CleanupOutcomeStatus.WORKING
        return outcome.model_copy(
            update={
                "status": status,
                "observed_quantity_after": remaining,
                "observed_after_at": snapshot.observed_at,
            }
        )

    def _nothing(
        self,
        now: datetime,
        started: float,
        *,
        detail: str,
        status: CleanupRunStatus,
        source_reconciliation_id: str = "none",
        plan: CleanupPlan | None = None,
        before: ReconciliationRun | None = None,
    ) -> CleanupRunOutcome:
        """A run that targeted nothing. Recorded in memory, never stored.

        Nothing happened, so there is nothing immutable to keep: writing a
        record every time an operator checks an already-clean account would
        turn the cleanup history into a log of looks rather than of acts.
        """
        run = OrphanCleanupRun(
            run_id=cleanup_run_identifier(request_id="none", as_of=now, outcomes=[], dry_run=True),
            cleanup_request_id="none",
            source_reconciliation_id=source_reconciliation_id,
            account_reference=(before.result.account_reference if before else "(broker not read)"),
            campaign_id=self._config.campaign.campaign_id,
            as_of=now,
            generated_at=self._clock.now(),
            status=status,
            trading_mode=self._settings.trading_mode,
            dry_run=True,
            broker=before.result.broker if before else "NONE",
            policy_version=self._config.application.config_version,
            versions=self.versions(),
            detail=detail,
        )
        empty = TargetSelection(
            reconciliation_id=source_reconciliation_id, account_reference=run.account_reference
        )
        return CleanupRunOutcome(
            run=run,
            plan=plan or CleanupPlan(selection=empty, request=None, detail=detail),
            before=before,
            duration_seconds=time.perf_counter() - started,
        )


def _outcome_status(submission: CleanupSubmission) -> CleanupOutcomeStatus:
    """What one submission means, before the broker is re-read.

    ``UNCERTAIN`` is the member that matters: something was sent and the
    outcome was never learned, so an order may be live right now. It is
    deliberately not ``REFUSED`` — a refusal means nothing reached the broker —
    and deliberately not a reason to send anything else.
    """
    record = submission.record
    if record is None:
        return CleanupOutcomeStatus.REFUSED
    if submission.dry_run:
        return CleanupOutcomeStatus.WORKING
    if record.state is ExecutionState.UNKNOWN:
        return CleanupOutcomeStatus.UNCERTAIN
    if record.state is ExecutionState.REJECTED:
        # The broker received it and turned it down. Not REFUSED: the attempt
        # reached the broker and its own counter records one, which is a
        # different fact from a gate that stopped the order here.
        return CleanupOutcomeStatus.REJECTED
    if record.state is ExecutionState.FAILED:
        # FAILED means the attempt provably never left the process — a
        # read-only broker, a disconnected one, an order our own translation
        # refused to build.
        return CleanupOutcomeStatus.REFUSED
    if record.state is ExecutionState.FILLED:
        return CleanupOutcomeStatus.WORKING
    if record.state is ExecutionState.PARTIALLY_FILLED:
        return CleanupOutcomeStatus.PARTIALLY_CLOSED
    return CleanupOutcomeStatus.WORKING


def _status_of(outcomes: Sequence[CleanupOutcome], *, dry_run: bool) -> CleanupRunStatus:
    """The run as a whole, with ``UNCERTAIN`` outranking everything else.

    An operator reading this needs to know first whether an order may be live,
    before they know how many holdings are gone.
    """
    if any(outcome.status is CleanupOutcomeStatus.UNCERTAIN for outcome in outcomes):
        return CleanupRunStatus.UNCERTAIN
    if dry_run:
        return CleanupRunStatus.DRY_RUN
    if not outcomes:
        return CleanupRunStatus.NO_TARGETS
    if all(
        outcome.status in (CleanupOutcomeStatus.CLOSED, CleanupOutcomeStatus.ALREADY_CLOSED)
        for outcome in outcomes
    ):
        return CleanupRunStatus.COMPLETE
    if all(
        outcome.orders_submitted == 0 and outcome.status is not CleanupOutcomeStatus.REJECTED
        for outcome in outcomes
    ):
        return CleanupRunStatus.NOTHING_SUBMITTED
    return CleanupRunStatus.PARTIAL
