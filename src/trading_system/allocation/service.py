"""The allocation service: the composition root for Milestone 7.

One stage, one boundary at each end:

.. code-block:: text

    contract-selection run (M6)   <- consumed, never re-selected
          |
    candidates                    <- legs + the cost of ONE unit, plus provenance
          |
    campaign snapshot             <- replayed from this ledger, point-in-time
    account snapshot              <- captured once, read back by id
          |
    RiskEngine                    <- permitted? deterministic, no model, no broker
          |
    AllocationEngine              <- how many units? floor, never rounds up
          |
    immutable allocation run      <- appended, never overwritten

Properties this service holds regardless of which path it takes:

* **It cannot trade.** No broker is constructed here and no order path is
  reachable from it. Milestone 7 ends at an authorisation, which is not an
  order and cannot be sent to anything.
* **It consults no model.** There is no LLM client parameter, no prompt and no
  agent import. The composition root itself is the evidence: there is nowhere
  a model could be called from.
* **It fails closed, per candidate.** A missing price, a stale quote, an
  unusable currency or a look-ahead leak ends *that candidate* with a named
  reason and no authorisation. One bad candidate never stops the rest, and a
  missing account snapshot stops the whole run rather than letting it guess.
* **It is reproducible.** Run identifiers are derived from the instant, the
  configuration, the contract run and the account snapshot; allocation ids are
  derived from content. A re-run over unchanged inputs recognises its own
  earlier authorisations instead of committing the capital twice.
* **It never re-selects upstream.** Contract selection is an input. This stage
  cannot add a symbol the selector did not choose, and a ``--symbol`` it did
  not cover is refused rather than allocated.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system import __version__ as application_version
from trading_system.allocation.budget_allocator import AllocationEngine, CandidateAllocation
from trading_system.allocation.campaign import build_campaign_snapshot
from trading_system.allocation.candidates import CandidateBuildError, build_candidate
from trading_system.allocation.models import (
    AllocationRunCounts,
    AllocationRunResult,
    CampaignAllocation,
    allocation_identifier,
    allocation_run_identifier,
    campaign_fingerprint,
)
from trading_system.allocation.store import (
    AllocationHistoryEntry,
    AllocationRepository,
    FilesystemAllocationRepository,
    SymbolAllocationEntry,
)
from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationRunStatus,
    DataQuality,
    ExpectedMagnitude,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import TRADING_ALLOCATION_ID, TRADING_STATUS
from trading_system.observability.instrument import traced
from trading_system.pnl.campaign_state import CampaignPnLState
from trading_system.research.store import FilesystemResearchRepository, ResearchRepository
from trading_system.risk.engine import RiskEngine
from trading_system.risk.exposure import would_add
from trading_system.risk.limits import resolve_campaign_budget, resolve_limits
from trading_system.risk.models import (
    ALLOCATION_SCHEMA_VERSION,
    AccountSnapshot,
    AllocationCandidate,
    CampaignSnapshot,
    RiskLimits,
)
from trading_system.risk.store import (
    AccountSnapshotRepository,
    FilesystemAccountSnapshotRepository,
)
from trading_system.strategies.models import ContractSelectionRunResult, StrategyRunResult
from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.store import (
    ContractSelectionRepository,
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
    StrategyRepository,
)

__all__ = ["AllocationRun", "AllocationService"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AllocationRun:
    """One allocation run's outcome, plus what the caller needs to report it."""

    result: AllocationRunResult
    #: The candidates each decision was made about, for ``--dry-run``
    #: inspection. Not persisted inside the run record: an allocation names the
    #: contract selection it rests on, and a second stored copy could drift.
    candidates: dict[str, AllocationCandidate]
    stored: bool
    dry_run: bool
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status is AllocationRunStatus.SUCCESS


class AllocationService:
    """Builds and drives deterministic risk evaluation and capital allocation.

    Constructs no broker and no LLM client, and holds neither. Account state
    reaches it only as a stored snapshot, which is both an architectural
    boundary and a safety one: Milestone 2 established that a second uncached
    round trip on one IBKR connection can go unanswered indefinitely, so a
    service that fetched account state mid-allocation could hang.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        research_repository: ResearchRepository | None = None,
        strategy_repository: StrategyRepository | None = None,
        contract_repository: ContractSelectionRepository | None = None,
        allocation_repository: AllocationRepository | None = None,
        account_repository: AccountSnapshotRepository | None = None,
        campaign_state: CampaignPnLState | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()
        #: Supplied by a caller that has already read the profit-and-loss
        #: ledger, or in a test that wants an exact daily figure. ``None``
        #: reads it from the store on demand.
        self._campaign_state = campaign_state

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._research_repository = research_repository or FilesystemResearchRepository(
            data_root / "research"
        )
        self._strategy_repository = strategy_repository or FilesystemStrategyRepository(
            data_root / "strategy"
        )
        self._contract_repository = contract_repository or FilesystemContractSelectionRepository(
            data_root / "contracts"
        )
        self._allocation_repository = allocation_repository or FilesystemAllocationRepository(
            data_root / "allocation"
        )
        self._account_repository = account_repository or FilesystemAccountSnapshotRepository(
            data_root / "accounts"
        )
        self._registry = StrategyRegistry.from_config(config)

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> AllocationRepository:
        return self._allocation_repository

    @property
    def account_repository(self) -> AccountSnapshotRepository:
        return self._account_repository

    @property
    def config(self) -> SystemConfig:
        return self._config

    @property
    def contract_repository(self) -> ContractSelectionRepository:
        return self._contract_repository

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def data_root(self) -> Path:
        return self._data_root

    def limits(
        self, *, account: AccountSnapshot | None = None, as_of: datetime | None = None
    ) -> RiskLimits:
        """The limits in force, resolved across every layer and converted once.

        ``account`` supplies the exchange rates. They come from the account
        snapshot rather than from anywhere else because that is where they were
        captured — in the same broker read as the balance they convert — and
        because this package may not reach a broker to fetch its own.

        Called without one, as ``risk validate`` does, the limits come back in
        the currency they were *declared* in and marked not convertible. That
        is the honest answer to "what are the limits" when nobody has looked up
        a rate, and :meth:`RiskLimits.usable_against` is what stops those
        figures being compared with a price.
        """
        return resolve_limits(
            self._config,
            budget_override=self._settings.campaign_budget,
            fx_rates=account.fx_rates if account is not None else None,
            as_of=as_of if as_of is not None else (account.as_of if account else None),
        )

    def history(self, limit: int | None = None) -> list[AllocationHistoryEntry]:
        return self._allocation_repository.history(limit=limit)

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolAllocationEntry]:
        return self._allocation_repository.symbol_history(symbol, limit=limit)

    def get(self, run_id: str) -> AllocationRunResult | None:
        return self._allocation_repository.get(run_id)

    def latest(self) -> AllocationRunResult | None:
        return self._allocation_repository.latest()

    def contract_run(self, run_id: str | None = None) -> ContractSelectionRunResult | None:
        if run_id is not None:
            return self._contract_repository.get(run_id)
        return self._contract_repository.latest()

    def strategy_run(self, run_id: str | None = None) -> StrategyRunResult | None:
        if run_id is not None:
            return self._strategy_repository.get(run_id)
        return self._strategy_repository.latest()

    def campaign_snapshot(
        self, as_of: datetime, *, account: AccountSnapshot | None = None
    ) -> CampaignSnapshot:
        """The campaign as it stood at ``as_of``, replayed from the ledger.

        Milestone 11 adds two facts to the replay, and both come from stores
        rather than from a service — this package may not reach a broker, a
        provider or a data repository, and a boundary test walks its whole
        import graph to keep it that way:

        * **settled opportunities** stop consuming the envelope. Milestone 7
          treated every authorisation as permanently spent because it could not
          know whether the order filled; a settlement is proof that the position
          is confirmed closed and the capital has come back.
        * **the day's realised result**, with its reliability alongside it. An
          unknown day is passed through as unknown, never as zero.
        """
        declared_budget, declared_reserve, source = resolve_campaign_budget(
            self._config.campaign, override=self._settings.campaign_budget
        )
        # The envelope this campaign may actually spend is the declared one
        # carried into the currency it trades. The declared figure is kept
        # beside it: the operator holds euro, and a record that only showed
        # dollars could not answer how much of their own money is committed.
        limits = self.limits(account=account, as_of=as_of)
        if limits.convertible:
            budget = limits.campaign_budget
            reserve = limits.campaign_reserve
        else:
            budget, reserve = declared_budget, declared_reserve
        state = self._campaign_pnl_state(as_of)
        return build_campaign_snapshot(
            self._allocation_repository.all_runs(),
            campaign_id=self._config.campaign.campaign_id,
            currency=limits.limit_currency,
            budget=budget,
            reserve=reserve,
            declared_budget=declared_budget,
            declared_currency=self._config.campaign.budget_currency,
            fx=limits.fx,
            as_of=as_of,
            budget_source=source,
            realized_pnl_today=state.realized_pnl_today,
            daily_pnl_status=state.daily_pnl_status,
            unavailable_pnl_position_ids=state.unavailable_position_ids,
            settled_opportunity_ids=state.settled_opportunity_ids,
        )

    def _campaign_pnl_state(self, as_of: datetime) -> CampaignPnLState:
        """Read the two facts the profit-and-loss ledger contributes.

        Imported inside the method, deliberately. The rule this package lives
        under is that nothing in ``allocation/`` may reach a broker, a provider
        or a data repository, and while this particular reader touches none of
        them, keeping the import local means the constraint is enforced at the
        one place a future change would break it.

        A store that cannot be read at all yields ``untracked`` rather than an
        exception: an unavailable ledger must not stop an allocation run, and
        ``NOT_TRACKED`` is exactly the honest thing to record — the daily-loss
        limit is then unevaluated rather than passed.
        """
        if self._campaign_state is not None:
            return self._campaign_state
        from trading_system.pnl.campaign_state import CampaignPnLState as _State
        from trading_system.pnl.campaign_state import read_campaign_state

        try:
            return read_campaign_state(
                self._data_root,
                campaign_id=self._config.campaign.campaign_id,
                as_of=as_of,
                day_boundary_timezone=self._config.pnl.day_boundary_timezone,
                enabled=self._config.pnl.enabled,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "allocation.pnl_state_unavailable",
                error=str(exc),
                detail=(
                    "the profit-and-loss ledger could not be read, so the daily loss limit is "
                    "recorded as unevaluated rather than passed and no capital is treated as "
                    "settled"
                ),
            )
            return _State.untracked()

    def account_snapshot(self, as_of: datetime) -> AccountSnapshot | None:
        """The newest account snapshot that was knowable at ``as_of``."""
        return self._account_repository.latest_as_of(as_of)

    # --- the run -----------------------------------------------------------
    @traced(
        "allocation.calculate",
        count=_metrics.ALLOCATIONS_TOTAL,
        duration=_metrics.ALLOCATION_DURATION,
        result_attributes=lambda run: {
            TRADING_ALLOCATION_ID: run.result.run_id,
            TRADING_STATUS: run.result.status.value,
        },
        labels=lambda run: {"status": run.result.status.value},
    )
    def run(
        self,
        *,
        dry_run: bool = False,
        contract_run_id: str | None = None,
        symbols: Sequence[str] | None = None,
        account_snapshot_id: str | None = None,
    ) -> AllocationRun:
        """Allocate campaign capital across one contract-selection run.

        There is deliberately no ``as_of`` parameter, for the same reason
        contract selection has none: the instant comes from the run being
        allocated against, so an authorisation reconstructs exactly the prices
        that were visible when the contract was chosen. Allocating a two-day-old
        selection against today's quotes would size a position nobody proposed.
        """
        started = time.perf_counter()
        selection_run = self.contract_run(contract_run_id)
        if selection_run is None:
            return self._empty(
                AllocationRunStatus.NO_CONTRACT_RUN,
                detail=(
                    f"no contract run with id {contract_run_id!r} was found"
                    if contract_run_id
                    else "no contract selection exists yet; run 'contract select' first"
                ),
                dry_run=dry_run,
                started=started,
            )

        as_of = selection_run.as_of
        wanted = {s.strip().upper() for s in symbols} if symbols else None
        if wanted:
            covered = {selection.symbol for selection in selection_run.selections}
            missing = sorted(wanted - covered)
            if missing:
                return self._empty(
                    AllocationRunStatus.CONFIGURATION_ERROR,
                    detail=(
                        f"contract run {selection_run.run_id} did not cover {', '.join(missing)}; "
                        f"this stage allocates against a selection and never makes one"
                    ),
                    dry_run=dry_run,
                    started=started,
                    as_of=as_of,
                    contract_run_id=selection_run.run_id,
                )

        candidates, build_failures = self._candidates(selection_run, wanted)

        # The account is read *before* the campaign is replayed, because the
        # exchange rates that carry the envelope into the traded currency are
        # captured on it. Reversing the order would build a campaign snapshot
        # that could not be converted and then convert it afterwards, which is
        # how a figure ends up recorded in one currency and compared in another.
        account = (
            self._account_repository.get(account_snapshot_id)
            if account_snapshot_id
            else self.account_snapshot(as_of)
        )
        campaign = self.campaign_snapshot(as_of, account=account)

        if not candidates:
            return self._empty(
                AllocationRunStatus.NO_CANDIDATES,
                detail=(
                    "; ".join(build_failures)
                    if build_failures
                    else (
                        "the contract run selected no contract to allocate against. This is a "
                        "considered outcome, not a failure"
                    )
                ),
                dry_run=dry_run,
                started=started,
                as_of=as_of,
                campaign=campaign,
                contract_run_id=selection_run.run_id,
            )

        if account is None and self._config.campaign.account.require_account_snapshot:
            return self._empty(
                AllocationRunStatus.ACCOUNT_SNAPSHOT_UNAVAILABLE,
                detail=(
                    f"no account snapshot was knowable at {as_of.isoformat()}. Capture one "
                    f"with 'risk capture-account'; the campaign envelope alone is not "
                    f"permission to spend"
                ),
                dry_run=dry_run,
                started=started,
                as_of=as_of,
                campaign=campaign,
                contract_run_id=selection_run.run_id,
            )

        limits = self.limits(account=account, as_of=as_of)
        engine = AllocationEngine(limits, RiskEngine(limits))
        decisions = engine.allocate(
            candidates,
            campaign,
            as_of=as_of,
            account=account,
            trading_mode=self._settings.trading_mode,
            live_guards_satisfied=(
                self._settings.live_trading_confirmed
                and self._settings.live_readiness_checklist_signed_off
            ),
        )

        run_id = allocation_run_identifier(
            as_of=as_of,
            config_version=self._config.application.config_version,
            policy_version=self._config.campaign.allocation.policy_version,
            contract_run_id=selection_run.run_id,
            account_snapshot_id=account.snapshot_id if account else None,
            campaign_id=campaign.campaign_id,
            campaign_state=campaign_fingerprint(campaign),
            opportunity_ids=[candidate.opportunity_id for candidate in candidates],
        )

        allocations = [
            self._record(decision, campaign, account, run_id, dry_run=dry_run)
            for decision in decisions
        ]
        committed = sum((a.capital_committed for a in allocations if a.approved), Decimal("0"))
        risk_authorized = sum((a.total_max_loss for a in allocations if a.approved), Decimal("0"))

        result = AllocationRunResult(
            run_id=run_id,
            campaign_id=campaign.campaign_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=_run_status(allocations),
            schema_version=ALLOCATION_SCHEMA_VERSION,
            policy=self._config.campaign.allocation.policy,
            policy_version=self._config.campaign.allocation.policy_version,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            campaign_before=campaign,
            account_snapshot_id=account.snapshot_id if account else None,
            currency=campaign.currency,
            budget=campaign.budget,
            reserve=campaign.reserve,
            declared_budget=campaign.declared_budget,
            declared_currency=campaign.declared_currency,
            fx=campaign.fx,
            allocated_before=campaign.allocated,
            allocated_this_run=committed,
            available_after=campaign.available - committed,
            risk_authorized_this_run=risk_authorized,
            allocations=sorted(allocations, key=lambda a: (a.rank, a.symbol)),
            counts=_counts(allocations),
            contract_run_id=selection_run.run_id,
            strategy_run_id=selection_run.strategy_run_id,
            research_run_id=selection_run.research_run_id,
            versions=self._versions(),
            status_detail="; ".join(build_failures) or None,
        )
        return self._finish(
            result,
            candidates={c.symbol: c for c in candidates},
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- candidates --------------------------------------------------------
    def _candidates(
        self,
        selection_run: ContractSelectionRunResult,
        wanted: set[str] | None,
    ) -> tuple[list[AllocationCandidate], list[str]]:
        """Carry every successful selection across the milestone boundary.

        A selection this stage cannot build a candidate from is recorded as a
        named failure rather than dropped silently — "nothing to allocate" and
        "three things could not be read" are different facts about a run.
        """
        strategy_run = (
            self._strategy_repository.get(selection_run.strategy_run_id)
            if selection_run.strategy_run_id
            else None
        )
        candidates: list[AllocationCandidate] = []
        failures: list[str] = []

        for selection in sorted(selection_run.selections, key=lambda s: s.symbol):
            if wanted is not None and selection.symbol not in wanted:
                continue
            if not selection.succeeded:
                continue
            if selection.strategy is None:
                failures.append(f"{selection.symbol}: a successful selection named no strategy")
                continue
            specification = self._registry.get(selection.strategy)
            if specification is None:
                failures.append(
                    f"{selection.symbol}: {selection.strategy.value} has no enabled "
                    f"configuration, so no strategy-level risk model applies to it"
                )
                continue

            decision = strategy_run.decision(selection.symbol) if strategy_run else None
            magnitude, usable, quality = self._research_facts(selection.symbol, decision)
            try:
                candidates.append(
                    build_candidate(
                        selection,
                        specification,
                        self._config.campaign.ranking,
                        decision=decision,
                        expected_magnitude=magnitude,
                        research_usable=usable,
                        data_quality=quality,
                        price_source=self._config.campaign.allocation.price_source,
                    )
                )
            except CandidateBuildError as exc:
                failures.append(str(exc))
        return candidates, failures

    def _research_facts(
        self, symbol: str, decision: object
    ) -> tuple[ExpectedMagnitude | None, bool, DataQuality]:
        """The research report's magnitude and data-quality verdict, if stored.

        Read from the Milestone 5 report rather than re-derived. Absent, the
        magnitude stays ``None`` — the scorer treats an unknown band as the
        least favourable rather than as the benefit of the doubt — and the
        quality verdict defaults to usable, because the contract selector
        already refused to select against quotes the data layer rejected and a
        weaker second guess here would only add noise.
        """
        run_id = getattr(decision, "research_run_id", None)
        if not run_id:
            return None, True, DataQuality.OK
        research = self._research_repository.get(str(run_id))
        if research is None:
            return None, True, DataQuality.OK
        report = research.report(symbol)
        if report is None:
            return None, True, DataQuality.OK
        return (
            report.expected_magnitude,
            report.data_quality.research_usable,
            report.data_quality.classification,
        )

    # --- records -----------------------------------------------------------
    def _record(
        self,
        decision: CandidateAllocation,
        campaign: CampaignSnapshot,
        account: AccountSnapshot | None,
        run_id: str,
        *,
        dry_run: bool,
    ) -> CampaignAllocation:
        candidate = decision.candidate
        calculation = decision.calculation
        approved = decision.outcome is AllocationOutcome.APPROVED

        exposure_after = would_add(
            campaign,
            candidate,
            quantity=decision.quantity if approved else 0,
            unit_cost=calculation.unit_cost if calculation else Decimal("0"),
            unit_max_loss=calculation.unit_max_loss if calculation else Decimal("0"),
        )

        return CampaignAllocation(
            allocation_id=allocation_identifier(
                run_id=run_id,
                opportunity_id=candidate.opportunity_id,
                outcome=decision.outcome,
                quantity=decision.quantity if approved else 0,
                capital_committed=decision.capital_committed if approved else Decimal("0"),
            ),
            run_id=run_id,
            opportunity_id=candidate.opportunity_id,
            campaign_id=campaign.campaign_id,
            symbol=candidate.symbol,
            as_of=candidate.as_of,
            decided_at=self._clock.now(),
            outcome=decision.outcome,
            strategy=candidate.strategy,
            strategy_version=candidate.risk_profile.strategy_version,
            direction=candidate.risk_profile.directional_view,
            legs=list(candidate.legs) if approved else [],
            expiration=candidate.expiration,
            dte=candidate.dte,
            quantity=decision.quantity if approved else 0,
            unit_cost=candidate.price.unit_cost if approved else None,
            capital_committed=decision.capital_committed if approved else Decimal("0"),
            unit_max_loss=decision.evaluation.unit_max_loss if approved else None,
            total_max_loss=decision.total_max_loss if approved else Decimal("0"),
            risk_basis=candidate.risk_profile.max_loss_basis if approved else None,
            price_source=candidate.price.source,
            currency=candidate.price.currency,
            calculation=calculation if approved else None,
            risk_outcome=decision.evaluation.outcome,
            reason_codes=list(decision.evaluation.reason_codes),
            allocation_reasons=list(decision.reasons),
            opportunity_score=candidate.score,
            rank=decision.rank,
            risk_evaluation=decision.evaluation,
            exposure_after=exposure_after,
            contract_selection_id=candidate.contract_selection_id,
            contract_run_id=candidate.contract_run_id,
            strategy_decision_id=candidate.strategy_decision_id,
            strategy_run_id=candidate.strategy_run_id,
            research_report_id=candidate.research_report_id,
            research_run_id=candidate.research_run_id,
            universe_run_id=candidate.universe_run_id,
            account_snapshot_id=account.snapshot_id if account else None,
            campaign_snapshot_as_of=campaign.as_of,
            input_snapshot_ids=list(candidate.input_snapshot_ids),
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            versions=self._versions(),
            detail=decision.detail,
        )

    def _empty(
        self,
        status: AllocationRunStatus,
        *,
        detail: str,
        dry_run: bool,
        started: float,
        as_of: datetime | None = None,
        campaign: CampaignSnapshot | None = None,
        contract_run_id: str | None = None,
    ) -> AllocationRun:
        """A run that authorised nothing, and an honest record of why."""
        instant = as_of or self._clock.now()
        state = campaign or self.campaign_snapshot(instant)
        result = AllocationRunResult(
            run_id=allocation_run_identifier(
                as_of=instant,
                config_version=self._config.application.config_version,
                policy_version=self._config.campaign.allocation.policy_version,
                contract_run_id=contract_run_id,
                account_snapshot_id=None,
                campaign_id=state.campaign_id,
                campaign_state=campaign_fingerprint(state),
                opportunity_ids=[],
            ),
            campaign_id=state.campaign_id,
            as_of=instant,
            generated_at=self._clock.now(),
            status=status,
            policy=self._config.campaign.allocation.policy,
            policy_version=self._config.campaign.allocation.policy_version,
            trading_mode=self._settings.trading_mode,
            dry_run=dry_run,
            campaign_before=state,
            currency=state.currency,
            budget=state.budget,
            reserve=state.reserve,
            declared_budget=state.declared_budget,
            declared_currency=state.declared_currency,
            fx=state.fx,
            allocated_before=state.allocated,
            available_after=state.available,
            contract_run_id=contract_run_id,
            versions=self._versions(),
            status_detail=detail,
        )
        return self._finish(
            result,
            candidates={},
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    def _finish(
        self,
        result: AllocationRunResult,
        *,
        candidates: dict[str, AllocationCandidate],
        dry_run: bool,
        duration_seconds: float,
    ) -> AllocationRun:
        stored = False
        if not dry_run:
            self._allocation_repository.save(result)
            stored = True
        _logger.info(
            "allocation.run",
            allocation_run_id=result.run_id,
            campaign_id=result.campaign_id,
            as_of=result.as_of.isoformat(),
            status=result.status.value,
            contract_run_id=result.contract_run_id,
            considered=result.counts.candidates_considered,
            approved=result.counts.approved,
            rejected=result.counts.rejected,
            no_trade=result.counts.no_trade,
            already_allocated=result.counts.already_allocated,
            allocated=str(result.allocated_this_run),
            available_after=str(result.available_after),
            orders_submitted=0,
            duration_seconds=round(duration_seconds, 4),
            dry_run=dry_run,
        )
        return AllocationRun(
            result=result,
            candidates=candidates,
            stored=stored,
            dry_run=dry_run,
            duration_seconds=duration_seconds,
        )

    def _versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            data_source_versions={
                "risk": self._config.risk.config_version,
                "allocation_policy": self._config.campaign.allocation.policy_version,
                "contract_selection": self._config.contract_selection.config_version,
            },
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _counts(allocations: list[CampaignAllocation]) -> AllocationRunCounts:
    return AllocationRunCounts(
        candidates_considered=len(allocations),
        approved=sum(1 for a in allocations if a.outcome is AllocationOutcome.APPROVED),
        rejected=sum(1 for a in allocations if a.outcome is AllocationOutcome.REJECTED),
        no_trade=sum(1 for a in allocations if a.outcome is AllocationOutcome.NO_TRADE),
        already_allocated=sum(
            1 for a in allocations if a.outcome is AllocationOutcome.ALREADY_ALLOCATED
        ),
    )


def _run_status(allocations: list[CampaignAllocation]) -> AllocationRunStatus:
    """The run's own outcome, distinct from any one candidate's.

    A run that authorised nothing is ``NO_ALLOCATION`` rather than a failure.
    That is the ordinary answer when the campaign is committed, and calling it
    an error would train an operator to ignore errors.
    """
    if not allocations:
        return AllocationRunStatus.NO_CANDIDATES
    if any(a.approved for a in allocations):
        return AllocationRunStatus.SUCCESS
    return AllocationRunStatus.NO_ALLOCATION
