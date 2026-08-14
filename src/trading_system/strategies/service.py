"""The strategy-selection and contract-selection services.

Two stages, two composition roots, one hard boundary between them:

.. code-block:: text

    research run                 <- consumed, never re-researched
          |
    eligibility (deterministic)  <- registry + configured gates; no model yet
          |
    strategy input contract      <- a research conclusion and a list of strategies
          |
    Strategy Selector agent      <- one isolated context per underlying
          |
    deterministic validation     <- rejects a violating decision in full
          |
    immutable strategy decision  <- appended, never overwritten
          |
    deterministic contract selector   <- no model, at all, ever
          |
    immutable contract selection <- appended, never overwritten

Properties both services hold regardless of which path they take:

* **They cannot trade.** No broker is constructed and no order path is
  reachable from either. Milestone 6 ends at a purchase *candidate*.
* **They fail closed, per symbol.** An unreachable model, an unusable answer, a
  missing chain or a look-ahead leak ends *that underlying* with a status
  naming what happened and no proposal. One symbol failing never stops the
  others.
* **They are reproducible.** Run identifiers are derived from the instant, the
  configuration and the run consumed; decision and selection identifiers are
  derived from content. A re-run over unchanged inputs is idempotent rather
  than a second record of one event.
* **They never re-select upstream.** Research is an input. The strategy stage
  cannot add a symbol research did not cover, and the contract stage cannot
  choose a strategy the decision did not name.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading_system import __version__ as application_version
from trading_system.agents.base import (
    AgentError,
    AgentInvalidOutputError,
    AgentUnavailableError,
    LLMClient,
)
from trading_system.agents.prompts import prompt_fingerprint
from trading_system.agents.strategy_selector import (
    PROMPT_NAME,
    StrategyOutcome,
    StrategySelectorAgent,
)
from trading_system.data.hashing import stable_hash
from trading_system.data.market_calendar import MarketCalendar
from trading_system.data.point_in_time import LookAheadError
from trading_system.data.repository import DataRepository, FilesystemDataRepository
from trading_system.domain.enums import (
    ContractSelectionStatus,
    DecisionMethod,
    RelevanceLevel,
    StrategyAction,
    StrategySelectionReason,
    StrategySelectionStatus,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import (
    TRADING_CONTRACT_ID,
    TRADING_STATUS,
    TRADING_STRATEGY_ID,
)
from trading_system.observability.instrument import traced
from trading_system.research.models import MarketResearchReport, ResearchRunResult
from trading_system.research.store import FilesystemResearchRepository, ResearchRepository
from trading_system.strategies.context import DataReadinessProbe, build_selection_input
from trading_system.strategies.contract_selector import ContractSelector, SelectionContext
from trading_system.strategies.models import (
    STRATEGY_SCHEMA_VERSION,
    ContractRunCounts,
    ContractSelectionResult,
    ContractSelectionRunResult,
    DataReadiness,
    StrategyAgentMetadata,
    StrategyDecisionRecord,
    StrategyOption,
    StrategyRunCounts,
    StrategyRunResult,
    StrategySelectionInput,
    confidence_rank,
    decision_identifier,
    selection_identifier,
)
from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.store import (
    ContractHistoryEntry,
    ContractSelectionRepository,
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
    StrategyHistoryEntry,
    StrategyRepository,
    SymbolDecisionEntry,
    SymbolSelectionEntry,
)
from trading_system.strategies.validation import StrategyOutputInvalidError

__all__ = [
    "ContractRun",
    "ContractSelectionService",
    "StrategyRun",
    "StrategyService",
    "build_contract_run_id",
    "build_strategy_run_id",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StrategyRun:
    """One strategy run's outcome, plus what the caller needs to report it."""

    result: StrategyRunResult
    #: The input each symbol was actually given, for ``--dry-run`` inspection.
    #: Not persisted inside the run record: the decision names the research it
    #: rests on, and a second stored copy could drift from the first.
    inputs: dict[str, StrategySelectionInput]
    stored: bool
    dry_run: bool
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status is StrategySelectionStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class ContractRun:
    """One contract-selection run's outcome."""

    result: ContractSelectionRunResult
    stored: bool
    dry_run: bool
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status is ContractSelectionStatus.SUCCESS


def build_strategy_run_id(
    *,
    as_of: datetime,
    config_version: str,
    prompt_version: str,
    research_run_id: str | None,
    symbols: Sequence[str],
) -> str:
    """Derive a run identifier from everything that determines the run."""
    digest = stable_hash(
        [
            "STRATEGY_RUN",
            config_version,
            prompt_version,
            research_run_id or "",
            as_of.isoformat(),
            [s.upper() for s in symbols],
        ]
    )
    return f"strategy-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"


def build_contract_run_id(
    *,
    as_of: datetime,
    policy_version: str,
    strategy_run_id: str | None,
    symbols: Sequence[str],
) -> str:
    """Derive a contract-run identifier. No model version: none is involved."""
    digest = stable_hash(
        [
            "CONTRACT_RUN",
            policy_version,
            strategy_run_id or "",
            as_of.isoformat(),
            [s.upper() for s in symbols],
        ]
    )
    return f"contract-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------
class StrategyService:
    """Builds and drives strategy selection from configuration."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        data_repository: DataRepository | None = None,
        research_repository: ResearchRepository | None = None,
        strategy_repository: StrategyRepository | None = None,
        llm_client: LLMClient | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._stage = config.strategy
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._data_repository = data_repository or FilesystemDataRepository(
            data_root, clock=self._clock
        )
        self._research_repository = research_repository or FilesystemResearchRepository(
            data_root / "research"
        )
        self._strategy_repository = strategy_repository or FilesystemStrategyRepository(
            data_root / "strategy"
        )
        self._registry = StrategyRegistry.from_config(config)
        self._readiness = DataReadinessProbe(self._data_repository)
        self._llm_client = llm_client

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> StrategyRepository:
        return self._strategy_repository

    @property
    def research_repository(self) -> ResearchRepository:
        return self._research_repository

    @property
    def data_repository(self) -> DataRepository:
        return self._data_repository

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def data_root(self) -> Path:
        return self._data_root

    def history(self, limit: int | None = None) -> list[StrategyHistoryEntry]:
        return self._strategy_repository.history(limit=limit)

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolDecisionEntry]:
        return self._strategy_repository.symbol_history(symbol, limit=limit)

    def get(self, run_id: str) -> StrategyRunResult | None:
        return self._strategy_repository.get(run_id)

    def latest(self) -> StrategyRunResult | None:
        return self._strategy_repository.latest()

    def research(self, run_id: str | None = None) -> ResearchRunResult | None:
        """The research run the strategy stage would consume."""
        if run_id is not None:
            return self._research_repository.get(run_id)
        return self._research_repository.latest()

    # --- the run -----------------------------------------------------------
    @traced(
        "strategy.selection",
        count=_metrics.STRATEGY_DECISIONS_TOTAL,
        duration=_metrics.STRATEGY_DURATION,
        result_attributes=lambda run: {
            TRADING_STRATEGY_ID: run.result.run_id,
            TRADING_STATUS: run.result.status.value,
        },
        labels=lambda run: {"status": run.result.status.value},
    )
    def run(
        self,
        *,
        as_of: datetime | None = None,
        dry_run: bool = False,
        symbols: Sequence[str] | None = None,
        research_run_id: str | None = None,
    ) -> StrategyRun:
        """Choose a strategy for every researched underlying, or none.

        ``dry_run`` performs the whole pipeline, including the agent call, and
        simply does not persist. It cannot reach a broker either way — there is
        no order path from this method.
        """
        started = time.perf_counter()
        instant = as_of or self._clock.now()
        research = self.research(research_run_id)

        reports, failure = self._resolve_reports(research, symbols, research_run_id)
        if failure is not None:
            status, detail = failure
            run_id = build_strategy_run_id(
                as_of=instant,
                config_version=self._stage.config_version,
                prompt_version=self._stage.agent.prompt_version,
                research_run_id=research.run_id if research else None,
                symbols=[],
            )
            return self._finish(
                StrategyRunResult(
                    run_id=run_id,
                    as_of=instant,
                    generated_at=self._clock.now(),
                    status=status,
                    status_detail=detail,
                    research_run_id=research.run_id if research else None,
                    universe_run_id=research.universe_run_id if research else None,
                    versions=self._versions(None),
                ),
                inputs={},
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        limit = self._stage.limits.max_symbols_per_run
        considered = reports[:limit]
        skipped = reports[limit:]

        run_id = build_strategy_run_id(
            as_of=instant,
            config_version=self._stage.config_version,
            prompt_version=self._stage.agent.prompt_version,
            research_run_id=research.run_id if research else None,
            symbols=[report.symbol for report in considered],
        )

        decisions: list[StrategyDecisionRecord] = []
        inputs: dict[str, StrategySelectionInput] = {}
        for report in considered:
            decision, selection_input = self._decide(report, run_id=run_id)
            decisions.append(decision)
            if selection_input is not None:
                inputs[report.symbol] = selection_input

        decisions.extend(self._skipped(report, run_id=run_id, limit=limit) for report in skipped)

        result = StrategyRunResult(
            run_id=run_id,
            as_of=instant,
            generated_at=self._clock.now(),
            status=_run_status(decisions),
            schema_version=STRATEGY_SCHEMA_VERSION,
            research_run_id=research.run_id if research else None,
            universe_run_id=research.universe_run_id if research else None,
            decisions=sorted(decisions, key=lambda d: d.symbol),
            counts=_strategy_counts(decisions, len(reports)),
            versions=self._versions(
                next((d.agent_metadata for d in decisions if d.agent_metadata), None)
            ),
        )
        return self._finish(
            result,
            inputs=inputs,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- one underlying ----------------------------------------------------
    def _decide(
        self, report: MarketResearchReport, *, run_id: str
    ) -> tuple[StrategyDecisionRecord, StrategySelectionInput | None]:
        """Decide for one underlying, in its own isolated context.

        Every failure is caught and turned into a record with a named status and
        no strategy. A symbol that cannot be decided must not stop the others,
        and must never be represented as anything other than undecided.
        """
        assert report.hypothesis is not None  # a SUCCESS report always states one
        eligible = self._eligible(report)

        try:
            readiness = self._readiness.probe(report.symbol, report.as_of)
        except LookAheadError as exc:
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.REQUIRED_DATA_UNAVAILABLE,
                    eligible=eligible,
                    readiness=DataReadiness(detail=str(exc)),
                    detail=(
                        f"a stored option record was not knowable at "
                        f"{report.as_of.isoformat()}: {exc}. This is a correctness bug in "
                        f"storage, not a market outcome."
                    ),
                ),
                None,
            )

        gate = self._gate(report, eligible, readiness)
        if gate is not None:
            status, reasons, rationale = gate
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=status,
                    eligible=eligible,
                    readiness=readiness,
                    reasons=reasons,
                    rationale=rationale,
                    detail=rationale,
                ),
                None,
            )

        selection_input = build_selection_input(
            run_id=run_id,
            report=report,
            eligible=[option for option in eligible],
        )

        if not self._stage.agent.enabled:
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.AI_UNAVAILABLE,
                    eligible=eligible,
                    readiness=readiness,
                    detail=(
                        "the strategy agent is disabled in config/strategy.yaml. There is no "
                        "deterministic substitute for a strategy choice, so none is made."
                    ),
                ),
                selection_input,
            )

        try:
            outcome = self._ask(selection_input)
        except AgentUnavailableError as exc:
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.AI_UNAVAILABLE,
                    eligible=eligible,
                    readiness=readiness,
                    detail=str(exc),
                ),
                selection_input,
            )
        except AgentInvalidOutputError as exc:
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.AI_INVALID_OUTPUT,
                    eligible=eligible,
                    readiness=readiness,
                    detail=str(exc),
                ),
                selection_input,
            )
        except StrategyOutputInvalidError as exc:
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.SEMANTIC_VALIDATION_FAILED,
                    eligible=eligible,
                    readiness=readiness,
                    detail=(
                        f"{exc}. The decision is rejected in full rather than repaired: a "
                        f"corrected choice would be recorded as the model's own."
                    ),
                ),
                selection_input,
            )
        except AgentError as exc:  # pragma: no cover - defensive
            return (
                self._record(
                    report,
                    run_id=run_id,
                    status=StrategySelectionStatus.AI_UNAVAILABLE,
                    eligible=eligible,
                    readiness=readiness,
                    detail=str(exc),
                ),
                selection_input,
            )

        return (
            self._accepted(report, outcome, run_id=run_id, eligible=eligible, readiness=readiness),
            selection_input,
        )

    def _eligible(self, report: MarketResearchReport) -> list[StrategyOption]:
        """Strategies the registry admits for this report's hypothesis.

        Narrowed further by the underlying's security type where the store knows
        it, and by horizon overlap where configuration requires it. Every
        narrowing here is deterministic and happens before any model sees the
        list.
        """
        assert report.hypothesis is not None
        specifications = self._registry.for_hypothesis(report.hypothesis)
        if self._stage.eligibility.require_horizon_overlap and report.horizon_days is not None:
            specifications = [
                specification
                for specification in specifications
                if specification.covers_horizon(report.horizon_days)
            ]
        return [specification.to_option() for specification in specifications]

    def _gate(
        self,
        report: MarketResearchReport,
        eligible: list[StrategyOption],
        readiness: DataReadiness,
    ) -> tuple[StrategySelectionStatus, list[StrategySelectionReason], str] | None:
        """Deterministic reasons to answer before a model is consulted.

        Each returns a status, the reason codes and the rationale. Spending a
        request to be told "there is not enough to go on" is waste, and an agent
        handed an inadequate outlook tends to fill the gap rather than decline.
        """
        policy = self._stage.eligibility
        quality = report.data_quality
        if not eligible:
            return (
                StrategySelectionStatus.SUCCESS,
                [StrategySelectionReason.NO_ELIGIBLE_STRATEGY],
                (
                    f"no configured strategy answers hypothesis "
                    f"{report.hypothesis.value if report.hypothesis else '?'} within a window "
                    f"that can express a {report.horizon_days}-day outlook. NO_TRADE is a "
                    f"correct outcome, not a failure."
                ),
            )
        if policy.require_research_usable and not quality.research_usable:
            return (
                StrategySelectionStatus.SUCCESS,
                [StrategySelectionReason.DATA_QUALITY_INSUFFICIENT],
                (
                    "the data behind this outlook was not research-usable, so no strategy is "
                    "chosen from it."
                ),
            )
        if len(report.evidence) < policy.min_evidence_items:
            return (
                StrategySelectionStatus.SUCCESS,
                [StrategySelectionReason.EVIDENCE_INSUFFICIENT],
                (
                    f"the outlook rests on {len(report.evidence)} evidence item(s), below the "
                    f"configured minimum of {policy.min_evidence_items}."
                ),
            )
        if report.confidence is not None and confidence_rank(report.confidence) < confidence_rank(
            policy.min_confidence
        ):
            return (
                StrategySelectionStatus.SUCCESS,
                [StrategySelectionReason.CONFIDENCE_INSUFFICIENT],
                (
                    f"research confidence {report.confidence.value} is below the configured "
                    f"minimum of {policy.min_confidence.value}."
                ),
            )
        if policy.require_option_chain and not readiness.option_chain_available:
            return (
                StrategySelectionStatus.REQUIRED_DATA_UNAVAILABLE,
                [],
                readiness.detail
                or (
                    f"no option chain for {report.symbol} was visible at {report.as_of.isoformat()}"
                ),
            )
        return None

    def _ask(self, selection_input: StrategySelectionInput) -> StrategyOutcome:
        policy = self._stage.agent
        agent = StrategySelectorAgent(
            self._llm_client or self._build_client(),
            config=self._stage,
            max_output_tokens=policy.max_output_tokens,
            timeout_seconds=policy.timeout_seconds,
            effort=policy.effort,
        )
        return agent.select(selection_input)

    def _build_client(self) -> LLMClient:
        """Construct the configured provider's client, or fail honestly."""
        policy = self._stage.agent
        if policy.model_provider.upper() != "ANTHROPIC":
            raise AgentUnavailableError(
                f"strategy provider {policy.model_provider!r} is not implemented; configure "
                f"ANTHROPIC or disable the strategy agent"
            )
        from trading_system.agents.anthropic_client import AnthropicLLMClient

        key = self._settings.anthropic_api_key
        return AnthropicLLMClient(
            api_key=key.get_secret_value() if key is not None else "",
            model_name=policy.model_name,
            prompt_version=policy.prompt_version,
            prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
        )

    # --- record assembly ----------------------------------------------------
    def _accepted(
        self,
        report: MarketResearchReport,
        outcome: StrategyOutcome,
        *,
        run_id: str,
        eligible: list[StrategyOption],
        readiness: DataReadiness,
    ) -> StrategyDecisionRecord:
        output = outcome.output
        specification = (
            self._registry.get(output.selected_strategy)
            if output.selected_strategy is not None
            else None
        )
        return self._record(
            report,
            run_id=run_id,
            status=StrategySelectionStatus.SUCCESS,
            eligible=eligible,
            readiness=readiness,
            action=output.action,
            strategy=output.selected_strategy,
            strategy_version=specification.version if specification else None,
            confidence=output.confidence,
            reasons=list(output.reasons),
            rationale=output.rationale,
            method=DecisionMethod.AI_SELECTED,
            metadata=_agent_metadata(outcome),
        )

    def _record(
        self,
        report: MarketResearchReport,
        *,
        run_id: str,
        status: StrategySelectionStatus,
        eligible: list[StrategyOption],
        readiness: DataReadiness,
        action: StrategyAction = StrategyAction.NO_TRADE,
        strategy: object = None,
        strategy_version: str | None = None,
        confidence: object = None,
        reasons: list[StrategySelectionReason] | None = None,
        rationale: str | None = None,
        method: DecisionMethod = DecisionMethod.DETERMINISTIC_ONLY,
        metadata: StrategyAgentMetadata | None = None,
        detail: str | None = None,
    ) -> StrategyDecisionRecord:
        from trading_system.domain.enums import ConfidenceLevel, StrategyType

        selected = strategy if isinstance(strategy, StrategyType) else None
        band = confidence if isinstance(confidence, ConfidenceLevel) else None
        return StrategyDecisionRecord(
            decision_id=decision_identifier(
                run_id=run_id,
                symbol=report.symbol,
                as_of=report.as_of,
                status=status,
                action=action,
                strategy=selected,
            ),
            run_id=run_id,
            symbol=report.symbol,
            as_of=report.as_of,
            generated_at=self._clock.now(),
            status=status,
            schema_version=STRATEGY_SCHEMA_VERSION,
            action=action,
            selected_strategy=selected,
            strategy_version=strategy_version,
            decision_method=method,
            confidence=band,
            reasons=reasons or [],
            rationale=rationale,
            hypothesis=report.hypothesis,
            research_confidence=report.confidence,
            research_horizon_days=report.horizon_days,
            research_report_id=report.report_id,
            research_run_id=report.run_id,
            universe_run_id=report.universe_run_id,
            eligible_strategies=[option.strategy_id for option in eligible],
            data_readiness=readiness,
            input_snapshot_ids=readiness.snapshot_ids,
            agent_metadata=metadata,
            versions=self._versions(metadata),
            status_detail=detail,
        )

    def _skipped(
        self, report: MarketResearchReport, *, run_id: str, limit: int
    ) -> StrategyDecisionRecord:
        return self._record(
            report,
            run_id=run_id,
            status=StrategySelectionStatus.SKIPPED_COST_LIMIT,
            eligible=[],
            readiness=DataReadiness(),
            detail=(
                f"researched but not put to a strategy: the configured ceiling of {limit} "
                f"underlyings per run was already reached. Recorded rather than omitted, "
                f"because 'we did not look' is not 'we found nothing'."
            ),
        )

    # --- helpers -----------------------------------------------------------
    def _resolve_reports(
        self,
        research: ResearchRunResult | None,
        requested: Sequence[str] | None,
        research_run_id: str | None,
    ) -> tuple[list[MarketResearchReport], tuple[StrategySelectionStatus, str] | None]:
        """Which outlooks to act on, or why none can be.

        The strategy stage consumes research; it never re-selects one. A
        requested symbol research did not cover is refused rather than decided,
        because a stage that could pick its own subjects would make the stage
        above it decorative.
        """
        if research is None:
            return [], (
                StrategySelectionStatus.NO_RESEARCH,
                (
                    f"no research run with id {research_run_id!r} was found"
                    if research_run_id
                    else "no research has been run yet; run 'research run' first. The "
                    "strategy stage consumes an outlook, it does not form one."
                ),
            )

        successful = [report for report in research.reports if report.succeeded]
        if not successful:
            return [], (
                StrategySelectionStatus.NO_RESEARCH,
                (
                    f"research run {research.run_id} produced no outlook "
                    f"({research.status.value}); no strategy may be chosen from it"
                ),
            )

        if requested is None:
            return successful, None

        wanted = {s.strip().upper() for s in requested if s.strip()}
        available = {report.symbol for report in successful}
        unknown = sorted(wanted - available)
        if unknown:
            return [], (
                StrategySelectionStatus.CONFIGURATION_ERROR,
                (
                    f"{', '.join(unknown)} has no successful research report in run "
                    f"{research.run_id} ({', '.join(sorted(available)) or 'none'}). The "
                    f"strategy stage consumes research and cannot extend it."
                ),
            )
        return [report for report in successful if report.symbol in wanted], None

    def _finish(
        self,
        result: StrategyRunResult,
        *,
        inputs: dict[str, StrategySelectionInput],
        dry_run: bool,
        duration_seconds: float,
    ) -> StrategyRun:
        stored = False
        if not dry_run:
            self._strategy_repository.save(result)
            stored = True
        _logger.info(
            "strategy.run",
            strategy_run_id=result.run_id,
            as_of=result.as_of.isoformat(),
            status=result.status.value,
            research_run_id=result.research_run_id,
            considered=result.counts.considered,
            proposed=result.counts.proposed,
            no_trade=result.counts.no_trade,
            failed=result.counts.failed,
            skipped=result.counts.skipped,
            duration_seconds=round(duration_seconds, 4),
            dry_run=dry_run,
        )
        return StrategyRun(
            result=result,
            inputs=inputs,
            stored=stored,
            dry_run=dry_run,
            duration_seconds=duration_seconds,
        )

    def _versions(self, metadata: StrategyAgentMetadata | None) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            agent_version=metadata.agent_version if metadata else None,
            model_id=metadata.model_name if metadata else None,
            data_source_versions={
                "strategy": self._stage.config_version,
                "risk": self._config.risk.config_version,
            },
        )


# ---------------------------------------------------------------------------
# Contract selection
# ---------------------------------------------------------------------------
class ContractSelectionService:
    """Builds and drives deterministic contract selection.

    Constructs no LLM client and holds none. The composition root itself is
    evidence of the boundary: there is nowhere in this class a model could be
    consulted from.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        data_repository: DataRepository | None = None,
        research_repository: ResearchRepository | None = None,
        strategy_repository: StrategyRepository | None = None,
        contract_repository: ContractSelectionRepository | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._data_repository = data_repository or FilesystemDataRepository(
            data_root, clock=self._clock
        )
        self._research_repository = research_repository or FilesystemResearchRepository(
            data_root / "research"
        )
        self._strategy_repository = strategy_repository or FilesystemStrategyRepository(
            data_root / "strategy"
        )
        self._contract_repository = contract_repository or FilesystemContractSelectionRepository(
            data_root / "contracts"
        )
        self._registry = StrategyRegistry.from_config(config)
        self._calendar = MarketCalendar(config.data.market_calendar)
        self._selector = ContractSelector(
            self._data_repository,
            config=config,
            calendar=self._calendar,
            clock=self._clock,
        )

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ContractSelectionRepository:
        return self._contract_repository

    @property
    def strategy_repository(self) -> StrategyRepository:
        return self._strategy_repository

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def selector(self) -> ContractSelector:
        return self._selector

    @property
    def data_root(self) -> Path:
        return self._data_root

    def history(self, limit: int | None = None) -> list[ContractHistoryEntry]:
        return self._contract_repository.history(limit=limit)

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolSelectionEntry]:
        return self._contract_repository.symbol_history(symbol, limit=limit)

    def get(self, run_id: str) -> ContractSelectionRunResult | None:
        return self._contract_repository.get(run_id)

    def latest(self) -> ContractSelectionRunResult | None:
        return self._contract_repository.latest()

    def strategy_run(self, run_id: str | None = None) -> StrategyRunResult | None:
        if run_id is not None:
            return self._strategy_repository.get(run_id)
        return self._strategy_repository.latest()

    # --- the run -----------------------------------------------------------
    @traced(
        "contract.selection",
        count=_metrics.CONTRACT_SELECTIONS_TOTAL,
        duration=_metrics.CONTRACT_SELECTION_DURATION,
        result_attributes=lambda run: {
            TRADING_CONTRACT_ID: run.result.run_id,
            TRADING_STATUS: run.result.status.value,
        },
        labels=lambda run: {"status": run.result.status.value},
    )
    def select(
        self,
        *,
        dry_run: bool = False,
        strategy_run_id: str | None = None,
        symbols: Sequence[str] | None = None,
    ) -> ContractRun:
        """Select contracts for every proposal in a strategy run.

        There is deliberately no ``as_of`` parameter. The instant comes from
        each decision, so contract selection reconstructs exactly the data that
        was visible when the strategy was chosen — a selection made against a
        later chain would answer a question nobody asked.
        """
        started = time.perf_counter()
        run = self.strategy_run(strategy_run_id)
        if run is None:
            instant = self._clock.now()
            return self._finish(
                ContractSelectionRunResult(
                    run_id=build_contract_run_id(
                        as_of=instant,
                        policy_version=self._config.contract_selection.selection_policy_version,
                        strategy_run_id=strategy_run_id,
                        symbols=[],
                    ),
                    as_of=instant,
                    generated_at=self._clock.now(),
                    status=ContractSelectionStatus.CONFIGURATION_ERROR,
                    selection_policy_version=(
                        self._config.contract_selection.selection_policy_version
                    ),
                    status_detail=(
                        f"no strategy run with id {strategy_run_id!r} was found"
                        if strategy_run_id
                        else "no strategy run exists yet; run 'strategy run' first"
                    ),
                    versions=self._versions(),
                ),
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        wanted = {s.strip().upper() for s in symbols} if symbols else None
        decisions = [
            decision
            for decision in run.decisions
            if decision.succeeded and (wanted is None or decision.symbol in wanted)
        ]

        run_id = build_contract_run_id(
            as_of=run.as_of,
            policy_version=self._config.contract_selection.selection_policy_version,
            strategy_run_id=run.run_id,
            symbols=[decision.symbol for decision in decisions],
        )
        selections = [self._select_one(decision, run, run_id) for decision in decisions]

        result = ContractSelectionRunResult(
            run_id=run_id,
            as_of=run.as_of,
            generated_at=self._clock.now(),
            status=_contract_run_status(selections),
            schema_version=STRATEGY_SCHEMA_VERSION,
            strategy_run_id=run.run_id,
            research_run_id=run.research_run_id,
            selection_policy_version=self._config.contract_selection.selection_policy_version,
            selections=sorted(selections, key=lambda s: s.symbol),
            counts=_contract_counts(selections),
            versions=self._versions(),
            status_detail=(
                None if selections else "the strategy run proposed no trade to select for"
            ),
        )
        return self._finish(result, dry_run=dry_run, duration_seconds=time.perf_counter() - started)

    def _select_one(
        self,
        decision: StrategyDecisionRecord,
        run: StrategyRunResult,
        run_id: str,
    ) -> ContractSelectionResult:
        """One decision: a real selection, or an honest record of why not."""
        if decision.action is not StrategyAction.BUY or decision.selected_strategy is None:
            return self._no_trade(decision, run_id)

        specification = self._registry.get(decision.selected_strategy)
        if specification is None or not specification.enabled:
            return self._not_eligible(decision, run_id)

        return self._selector.select(
            SelectionContext(
                run_id=run_id,
                decision=decision,
                specification=specification,
                as_of=decision.as_of,
                event_time=self._event_time(decision),
                research_report_id=decision.research_report_id,
                strategy_run_id=run.run_id,
            )
        )

    def _event_time(self, decision: StrategyDecisionRecord) -> datetime | None:
        """The event an event-aligned expiration would align to.

        Read from the structured research report, never from prose and never
        from a model. The most relevant event inside the horizon wins, ties
        broken by the earliest — a rule, so two runs align to the same event.
        """
        if decision.research_run_id is None:
            return None
        research = self._research_repository.get(decision.research_run_id)
        if research is None:
            return None
        report = research.report(decision.symbol)
        if report is None:
            return None
        candidates = [event for event in report.key_events if event.within_horizon]
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda event: (
                0 if event.expected_relevance is RelevanceLevel.HIGH else 1,
                event.expected_event_time,
                event.event_id,
            ),
        )
        return ordered[0].expected_event_time

    def _no_trade(self, decision: StrategyDecisionRecord, run_id: str) -> ContractSelectionResult:
        return self._bare(
            decision,
            run_id,
            ContractSelectionStatus.NO_TRADE,
            (
                "the strategy stage chose not to trade this underlying, so there is no "
                "contract to select. This is a considered outcome, not a failure."
            ),
        )

    def _not_eligible(
        self, decision: StrategyDecisionRecord, run_id: str
    ) -> ContractSelectionResult:
        strategy = decision.selected_strategy.value if decision.selected_strategy else "none"
        return self._bare(
            decision,
            run_id,
            ContractSelectionStatus.STRATEGY_NOT_ELIGIBLE,
            (
                f"{strategy} has no enabled configuration in config/strategies/; a strategy "
                f"the registry does not admit cannot be resolved to a contract, whatever "
                f"named it"
            ),
        )

    def _bare(
        self,
        decision: StrategyDecisionRecord,
        run_id: str,
        status: ContractSelectionStatus,
        detail: str,
    ) -> ContractSelectionResult:
        policy_version = self._config.contract_selection.selection_policy_version
        return ContractSelectionResult(
            selection_id=selection_identifier(
                run_id=run_id,
                symbol=decision.symbol,
                as_of=decision.as_of,
                status=status,
                strategy=decision.selected_strategy,
                contract_ids=[],
            ),
            run_id=run_id,
            symbol=decision.symbol,
            as_of=decision.as_of,
            generated_at=self._clock.now(),
            selection_status=status,
            schema_version=STRATEGY_SCHEMA_VERSION,
            strategy=decision.selected_strategy,
            strategy_version=decision.strategy_version,
            strategy_run_id=decision.run_id,
            strategy_decision_id=decision.decision_id,
            research_report_id=decision.research_report_id,
            selection_policy_version=policy_version,
            versions=self._versions(),
            status_detail=detail,
        )

    def _finish(
        self,
        result: ContractSelectionRunResult,
        *,
        dry_run: bool,
        duration_seconds: float,
    ) -> ContractRun:
        stored = False
        if not dry_run:
            self._contract_repository.save(result)
            stored = True
        _logger.info(
            "contract.select",
            contract_run_id=result.run_id,
            as_of=result.as_of.isoformat(),
            status=result.status.value,
            strategy_run_id=result.strategy_run_id,
            considered=result.counts.decisions_considered,
            selected=result.counts.selected,
            no_contract=result.counts.no_contract,
            no_trade=result.counts.no_trade,
            failed=result.counts.failed,
            duration_seconds=round(duration_seconds, 4),
            dry_run=dry_run,
        )
        return ContractRun(
            result=result,
            stored=stored,
            dry_run=dry_run,
            duration_seconds=duration_seconds,
        )

    def _versions(self) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            data_source_versions={
                "contract_selection": self._config.contract_selection.config_version,
                "risk": self._config.risk.config_version,
            },
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _agent_metadata(outcome: StrategyOutcome) -> StrategyAgentMetadata:
    """Build the audit record from a completed decision call."""
    response = outcome.response
    identity = response.identity
    return StrategyAgentMetadata(
        model_provider=identity.provider,
        model_name=identity.model_name,
        model_version=identity.model_version,
        prompt_version=identity.prompt_version,
        prompt_fingerprint=identity.prompt_fingerprint,
        agent_version=identity.agent_version,
        generated_at=response.generated_at,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        raw_response=response.text,
    )


def _strategy_counts(decisions: list[StrategyDecisionRecord], researched: int) -> StrategyRunCounts:
    skipped = sum(1 for d in decisions if d.status is StrategySelectionStatus.SKIPPED_COST_LIMIT)
    return StrategyRunCounts(
        researched_assets=researched,
        considered=len(decisions) - skipped,
        proposed=sum(1 for d in decisions if d.proposes_a_trade),
        no_trade=sum(1 for d in decisions if d.succeeded and d.action is StrategyAction.NO_TRADE),
        failed=sum(
            1
            for d in decisions
            if not d.succeeded and d.status is not StrategySelectionStatus.SKIPPED_COST_LIMIT
        ),
        skipped=skipped,
    )


def _run_status(decisions: list[StrategyDecisionRecord]) -> StrategySelectionStatus:
    """The run's own outcome, distinct from any one symbol's.

    A run succeeds when it reached at least one decision — including a
    considered ``NO_TRADE``, which is a decision. When it reached none, it
    reports the reason the majority of its symbols failed for, so an operator
    sees "the model was unreachable" rather than a generic failure.
    """
    if not decisions:
        return StrategySelectionStatus.NO_RESEARCH
    if any(decision.succeeded for decision in decisions):
        return StrategySelectionStatus.SUCCESS
    considered = [
        d.status for d in decisions if d.status is not StrategySelectionStatus.SKIPPED_COST_LIMIT
    ]
    if not considered:
        return StrategySelectionStatus.SKIPPED_COST_LIMIT
    return max(set(considered), key=lambda status: (considered.count(status), status.value))


def _contract_counts(selections: list[ContractSelectionResult]) -> ContractRunCounts:
    no_contract = {
        ContractSelectionStatus.NO_VALID_CONTRACT,
        ContractSelectionStatus.NO_VALID_EXPIRATION,
        ContractSelectionStatus.NO_VALID_STRIKE,
    }
    return ContractRunCounts(
        decisions_considered=len(selections),
        selected=sum(1 for s in selections if s.succeeded),
        no_contract=sum(1 for s in selections if s.selection_status in no_contract),
        no_trade=sum(
            1 for s in selections if s.selection_status is ContractSelectionStatus.NO_TRADE
        ),
        failed=sum(
            1
            for s in selections
            if s.selection_status
            not in no_contract | {ContractSelectionStatus.SUCCESS, ContractSelectionStatus.NO_TRADE}
        ),
    )


def _contract_run_status(
    selections: list[ContractSelectionResult],
) -> ContractSelectionStatus:
    if not selections:
        return ContractSelectionStatus.NO_TRADE
    if any(selection.succeeded for selection in selections):
        return ContractSelectionStatus.SUCCESS
    statuses = [selection.selection_status for selection in selections]
    return max(set(statuses), key=lambda status: (statuses.count(status), status.value))
