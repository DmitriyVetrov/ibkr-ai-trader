"""The market research service.

One pipeline, run once per underlying, in this order:

.. code-block:: text

    universe run                 <- consumed, never re-selected
          |
    point-in-time retrieval      (DataRepository only; no provider, no broker)
          |
    research input contract      <- bounded, deduplicated, provenance-carrying
          |
    Market Researcher agent      <- one isolated context per underlying
          |
    contract validation          <- schema and shape
          |
    deterministic semantic validation
          |
    immutable research report    <- appended, never overwritten

Properties the pipeline holds regardless of which path it takes:

* **It cannot trade.** No broker is constructed and no order path is reachable
  from here. A research run is a pure consumer of history already collected.
* **It fails closed, per symbol.** An unreachable model, an unusable answer, a
  look-ahead leak or thin evidence ends *that underlying* with a status naming
  what happened and **no hypothesis**. There is deliberately no deterministic
  fallback: a universe can be ordered without a model, but an outlook
  synthesised in place of one would be a fabricated view wearing a report's
  clothes. One symbol failing never stops the others.
* **It is reproducible.** The run identifier is derived from the instant, the
  configuration and the universe consumed; report identifiers are derived from
  content. Identical inputs and an identical model answer produce byte-identical
  records, so a re-run is idempotent rather than a second record of one event.
* **It never re-selects.** The universe is an input. Research does not add a
  symbol the universe did not choose, and cannot: the symbol list comes from
  the stored run.
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
from trading_system.agents.market_researcher import (
    PROMPT_NAME,
    MarketResearcherAgent,
    ResearchOutcome,
)
from trading_system.agents.prompts import prompt_fingerprint
from trading_system.data.hashing import stable_hash
from trading_system.data.point_in_time import LookAheadError
from trading_system.data.repository import DataRepository, FilesystemDataRepository
from trading_system.domain.enums import DataQuality, ResearchStatus, UniverseSelectionStatus
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.observability import metrics as _metrics
from trading_system.observability.attributes import TRADING_RESEARCH_ID, TRADING_STATUS
from trading_system.observability.instrument import traced
from trading_system.research.context import ResearchInputBuilder
from trading_system.research.models import (
    RESEARCH_SCHEMA_VERSION,
    MarketResearchReport,
    ReportedEvent,
    ReportedEvidence,
    ResearchAgentMetadata,
    ResearchDataQualitySummary,
    ResearchHorizon,
    ResearchInput,
    ResearchLimitsSnapshot,
    ResearchRunCounts,
    ResearchRunResult,
    ResearchWindowSnapshot,
    report_identifier,
)
from trading_system.research.sources import SourceTrustPolicy
from trading_system.research.store import (
    FilesystemResearchRepository,
    ResearchHistoryEntry,
    ResearchRepository,
    SymbolHistoryEntry,
)
from trading_system.research.validation import ResearchOutputInvalidError
from trading_system.universe.models import UniverseSelectionResult
from trading_system.universe.store import (
    FilesystemUniverseRepository,
    UniverseRepository,
)

__all__ = ["ResearchRun", "ResearchService", "build_run_id"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResearchRun:
    """One run's outcome, plus what the caller needs to report it."""

    result: ResearchRunResult
    #: The input each symbol was actually given, for ``--dry-run`` inspection.
    #: Not persisted inside the run record: the report names the snapshots it
    #: rests on, and storing a second copy of the evidence would create two
    #: versions of the truth that could drift.
    inputs: dict[str, ResearchInput]
    stored: bool
    dry_run: bool
    #: Wall-clock time the run took. Deliberately not part of the stored
    #: record: a measured elapsed time is a fact about the machine, not the
    #: decision.
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status is ResearchStatus.SUCCESS


def build_run_id(
    *,
    as_of: datetime,
    config_version: str,
    prompt_version: str,
    universe_snapshot_id: str | None,
    symbols: Sequence[str],
) -> str:
    """Derive a run identifier from everything that determines the run.

    Derived rather than random so a re-run over unchanged inputs is idempotent —
    the store recognises it as the same run instead of accumulating duplicate
    records of one event. The universe snapshot is folded in, so research over a
    *new* universe at the same instant is correctly a different run rather than
    a collision.
    """
    digest = stable_hash(
        [
            "RESEARCH_RUN",
            config_version,
            prompt_version,
            universe_snapshot_id or "",
            as_of.isoformat(),
            [s.upper() for s in symbols],
        ]
    )
    return f"research-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"


class ResearchService:
    """Builds and drives market research from configuration.

    Like the data and universe layers, this is the single composition root: the
    CLI and the future scheduler both go through it, so a command and a
    scheduled job cannot end up with differently configured pipelines.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        data_repository: DataRepository | None = None,
        universe_repository: UniverseRepository | None = None,
        research_repository: ResearchRepository | None = None,
        llm_client: LLMClient | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._research_config = config.research
        self._clock = clock or SystemClock()
        self._root = root

        data_root = Path(config.data.storage.root)
        if not data_root.is_absolute():
            data_root = (root or project_root()) / data_root
        self._data_root = data_root

        self._data_repository = data_repository or FilesystemDataRepository(
            data_root, clock=self._clock
        )
        self._universe_repository = universe_repository or FilesystemUniverseRepository(
            data_root / "universe"
        )
        self._research_repository = research_repository or FilesystemResearchRepository(
            data_root / "research"
        )
        self._sources = SourceTrustPolicy(config.sources)
        self._builder = ResearchInputBuilder(
            self._data_repository,
            config=self._research_config,
            source_policy=self._sources,
        )
        self._llm_client = llm_client

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> ResearchRepository:
        return self._research_repository

    @property
    def data_repository(self) -> DataRepository:
        return self._data_repository

    @property
    def universe_repository(self) -> UniverseRepository:
        return self._universe_repository

    @property
    def source_policy(self) -> SourceTrustPolicy:
        return self._sources

    @property
    def input_builder(self) -> ResearchInputBuilder:
        return self._builder

    @property
    def data_root(self) -> Path:
        return self._data_root

    def history(self, limit: int | None = None) -> list[ResearchHistoryEntry]:
        return self._research_repository.history(limit=limit)

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[SymbolHistoryEntry]:
        return self._research_repository.symbol_history(symbol, limit=limit)

    def get(self, run_id: str) -> ResearchRunResult | None:
        return self._research_repository.get(run_id)

    def latest(self) -> ResearchRunResult | None:
        return self._research_repository.latest()

    def universe(self, run_id: str | None = None) -> UniverseSelectionResult | None:
        """The universe run research would consume."""
        if run_id is not None:
            return self._universe_repository.get(run_id)
        return self._universe_repository.latest()

    # --- the run -----------------------------------------------------------
    @traced(
        "research.run",
        count=_metrics.RESEARCH_RUNS_TOTAL,
        duration=_metrics.RESEARCH_DURATION,
        result_attributes=lambda run: {
            TRADING_RESEARCH_ID: run.result.run_id,
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
        universe_run_id: str | None = None,
    ) -> ResearchRun:
        """Research the selected universe as of ``as_of`` (default: now).

        ``dry_run`` performs the whole pipeline, including the agent call if
        configured, and simply does not persist. It cannot reach a broker
        either way — there is no order path from this method.

        ``symbols`` narrows the run to a subset of the universe. It cannot
        *widen* it: a symbol the universe did not select is refused, because
        research selecting its own subjects would make the universe stage
        decorative.
        """
        started = time.perf_counter()
        instant = as_of or self._clock.now()

        universe = self.universe(universe_run_id)
        selection, failure = self._resolve_symbols(universe, symbols, universe_run_id)
        if failure is not None:
            status, detail = failure
            return self._finish(
                self._build_result(
                    run_id=build_run_id(
                        as_of=instant,
                        config_version=self._research_config.config_version,
                        prompt_version=self._research_config.agent.prompt_version,
                        universe_snapshot_id=universe.snapshot_id if universe else None,
                        symbols=[],
                    ),
                    as_of=instant,
                    status=status,
                    status_detail=detail,
                    universe=universe,
                    reports=[],
                    universe_asset_count=len(universe.selected_assets) if universe else 0,
                ),
                inputs={},
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        limit = self._research_config.limits.max_assets_per_run
        researched = selection[:limit]
        skipped = selection[limit:]

        run_id = build_run_id(
            as_of=instant,
            config_version=self._research_config.config_version,
            prompt_version=self._research_config.agent.prompt_version,
            universe_snapshot_id=universe.snapshot_id if universe else None,
            symbols=researched,
        )

        reports: list[MarketResearchReport] = []
        inputs: dict[str, ResearchInput] = {}
        for symbol in researched:
            report, research_input = self._research_symbol(
                symbol, instant, run_id=run_id, universe=universe
            )
            reports.append(report)
            if research_input is not None:
                inputs[symbol] = research_input

        reports.extend(
            self._skipped_report(symbol, instant, run_id=run_id, universe=universe, limit=limit)
            for symbol in skipped
        )

        result = self._build_result(
            run_id=run_id,
            as_of=instant,
            status=_run_status(reports),
            status_detail=None,
            universe=universe,
            reports=reports,
            universe_asset_count=len(selection),
        )
        return self._finish(
            result,
            inputs=inputs,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- one underlying ----------------------------------------------------
    def _research_symbol(
        self,
        symbol: str,
        as_of: datetime,
        *,
        run_id: str,
        universe: UniverseSelectionResult | None,
    ) -> tuple[MarketResearchReport, ResearchInput | None]:
        """Research one underlying in its own isolated context.

        Every failure is caught and turned into a report with a named status
        and no hypothesis. A symbol that cannot be researched must not stop the
        others, and must never be represented as anything other than
        unresearched.
        """
        try:
            research_input = self._builder.build(
                symbol,
                as_of,
                run_id=run_id,
                universe_run_id=universe.run_id if universe else None,
                universe_snapshot_id=universe.snapshot_id if universe else None,
            )
        except LookAheadError as exc:
            return (
                self._failed_report(
                    symbol,
                    as_of,
                    run_id=run_id,
                    universe=universe,
                    status=ResearchStatus.POINT_IN_TIME_ERROR,
                    detail=(
                        f"a stored record was not knowable at {as_of.isoformat()}: {exc}. "
                        f"This is a correctness bug in storage or gathering, not a market "
                        f"outcome, and no outlook is produced from it."
                    ),
                ),
                None,
            )

        policy = self._research_config.evidence
        usable = research_input.usable_evidence_count
        if research_input.market_snapshot is None and usable == 0:
            return (
                self._no_view_report(
                    research_input,
                    ResearchStatus.NO_DATA,
                    f"nothing about {symbol} was visible at {as_of.isoformat()}; "
                    f"collect data first (data collect --symbol {symbol}).",
                    universe=universe,
                ),
                research_input,
            )
        if usable < policy.min_evidence_items:
            return (
                self._no_view_report(
                    research_input,
                    ResearchStatus.INSUFFICIENT_EVIDENCE,
                    f"{usable} research-usable fact(s) were visible, below the configured "
                    f"minimum of {policy.min_evidence_items}. Insufficient evidence is a "
                    f"valid outcome and is not a directional signal.",
                    universe=universe,
                ),
                research_input,
            )

        if not self._research_config.agent.enabled:
            return (
                self._no_view_report(
                    research_input,
                    ResearchStatus.AI_UNAVAILABLE,
                    "the research agent is disabled in config/research.yaml. There is no "
                    "deterministic substitute for a market outlook, so none is produced.",
                    universe=universe,
                ),
                research_input,
            )

        try:
            outcome = self._ask(research_input)
        except AgentUnavailableError as exc:
            return (
                self._no_view_report(
                    research_input, ResearchStatus.AI_UNAVAILABLE, str(exc), universe=universe
                ),
                research_input,
            )
        except AgentInvalidOutputError as exc:
            return (
                self._no_view_report(
                    research_input, ResearchStatus.AI_INVALID_OUTPUT, str(exc), universe=universe
                ),
                research_input,
            )
        except ResearchOutputInvalidError as exc:
            return (
                self._no_view_report(
                    research_input,
                    ResearchStatus.SEMANTIC_VALIDATION_FAILED,
                    f"{exc}. The response is rejected in full rather than repaired: a "
                    f"corrected outlook would be recorded as the model's own.",
                    universe=universe,
                ),
                research_input,
            )
        except AgentError as exc:  # pragma: no cover - defensive
            return (
                self._no_view_report(
                    research_input, ResearchStatus.AI_UNAVAILABLE, str(exc), universe=universe
                ),
                research_input,
            )

        return (
            self._successful_report(research_input, outcome, universe=universe),
            research_input,
        )

    def _ask(self, research_input: ResearchInput) -> ResearchOutcome:
        policy = self._research_config.agent
        agent = MarketResearcherAgent(
            self._llm_client or self._build_client(),
            config=self._research_config,
            max_output_tokens=policy.max_output_tokens,
            timeout_seconds=policy.timeout_seconds,
            effort=policy.effort,
        )
        return agent.research(research_input)

    def _build_client(self) -> LLMClient:
        """Construct the configured provider's client, or fail honestly."""
        policy = self._research_config.agent
        if policy.model_provider.upper() != "ANTHROPIC":
            raise AgentUnavailableError(
                f"research provider {policy.model_provider!r} is not implemented; "
                f"configure ANTHROPIC or disable the research agent"
            )
        from trading_system.agents.anthropic_client import AnthropicLLMClient

        key = self._settings.anthropic_api_key
        return AnthropicLLMClient(
            api_key=key.get_secret_value() if key is not None else "",
            model_name=policy.model_name,
            prompt_version=policy.prompt_version,
            prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
        )

    # --- report assembly ----------------------------------------------------
    def _successful_report(
        self,
        research_input: ResearchInput,
        outcome: ResearchOutcome,
        *,
        universe: UniverseSelectionResult | None,
    ) -> MarketResearchReport:
        """Merge the agent's reading with the input's facts.

        The direction of the merge matters: every provenance field — source,
        tier, publication time, retrieval time, snapshot id — is copied from the
        *input*, never from the response. The agent contributes an
        interpretation of a fact it was given; it does not get to describe the
        fact.
        """
        output = outcome.output
        evidence = [
            ReportedEvidence(
                evidence_id=item.evidence_id,
                kind=found.kind,
                claim=item.claim,
                fact=found.summary,
                direction=item.direction,
                stance=item.stance,
                relevance=item.relevance,
                confidence=item.confidence,
                source=found.source,
                occurred_at=found.occurred_at,
                duplicate_count=found.duplicate_count,
                duplicate_source_names=list(found.duplicate_source_names),
            )
            for item in output.evidence
            if (found := research_input.evidence(item.evidence_id)) is not None
        ]
        key_events = [
            ReportedEvent(
                event_id=assessment.event_id,
                event_type=event.event_type,
                summary=event.summary,
                expected_event_time=event.expected_event_time,
                announced_at=event.announced_at,
                confirmed=event.confirmed,
                days_until=event.days_until,
                within_horizon=event.within_horizon,
                expected_relevance=assessment.expected_relevance,
                directional_uncertainty=assessment.directional_uncertainty,
                rationale=assessment.rationale,
                source=event.source,
            )
            for assessment in output.key_events
            if (event := research_input.event(assessment.event_id)) is not None
        ]

        metadata = _agent_metadata(outcome)
        return MarketResearchReport(
            report_id=report_identifier(
                run_id=research_input.run_id,
                symbol=research_input.symbol,
                as_of=research_input.as_of,
                status=ResearchStatus.SUCCESS,
                hypothesis=output.hypothesis,
                confidence=output.confidence,
                evidence_ids=[e.evidence_id for e in evidence],
            ),
            run_id=research_input.run_id,
            symbol=research_input.symbol,
            as_of=research_input.as_of,
            generated_at=self._clock.now(),
            horizon=research_input.horizon,
            status=ResearchStatus.SUCCESS,
            hypothesis=output.hypothesis,
            confidence=output.confidence,
            direction=output.direction,
            expected_magnitude=output.expected_magnitude,
            horizon_days=output.horizon_days,
            thesis=output.thesis,
            expected_behavior=output.expected_behavior,
            explanation=output.explanation,
            evidence=evidence,
            key_events=key_events,
            bullish_catalysts=list(output.bullish_catalysts),
            bearish_catalysts=list(output.bearish_catalysts),
            risks=list(output.risks),
            invalidation_conditions=list(output.invalidation_conditions),
            contradiction_resolution=output.contradiction_resolution,
            missing_information=list(output.missing_information),
            data_quality=research_input.data_quality_summary,
            window=research_input.window,
            limits=research_input.limits,
            source_policy=research_input.source_policy,
            universe_run_id=universe.run_id if universe else None,
            universe_snapshot_id=universe.snapshot_id if universe else None,
            input_snapshot_ids=list(research_input.data_snapshot_ids),
            agent_metadata=metadata,
            versions=self._versions(metadata),
        )

    def _no_view_report(
        self,
        research_input: ResearchInput,
        status: ResearchStatus,
        detail: str,
        *,
        universe: UniverseSelectionResult | None,
    ) -> MarketResearchReport:
        """A report that states no outlook, and says exactly why.

        The evidence and the data-quality verdict are preserved even though no
        hypothesis was reached — "we looked at these facts and could not form a
        view" is a materially different record from "we did not look", and both
        are worth keeping.
        """
        return MarketResearchReport(
            report_id=report_identifier(
                run_id=research_input.run_id,
                symbol=research_input.symbol,
                as_of=research_input.as_of,
                status=status,
                hypothesis=None,
                confidence=None,
                evidence_ids=sorted(research_input.evidence_ids),
            ),
            run_id=research_input.run_id,
            symbol=research_input.symbol,
            as_of=research_input.as_of,
            generated_at=self._clock.now(),
            horizon=research_input.horizon,
            status=status,
            status_detail=detail,
            data_quality=research_input.data_quality_summary,
            window=research_input.window,
            limits=research_input.limits,
            source_policy=research_input.source_policy,
            universe_run_id=universe.run_id if universe else None,
            universe_snapshot_id=universe.snapshot_id if universe else None,
            input_snapshot_ids=list(research_input.data_snapshot_ids),
            versions=self._versions(None),
        )

    def _failed_report(
        self,
        symbol: str,
        as_of: datetime,
        *,
        run_id: str,
        universe: UniverseSelectionResult | None,
        status: ResearchStatus,
        detail: str,
    ) -> MarketResearchReport:
        """A report for a symbol whose input could not even be assembled."""
        return MarketResearchReport(
            report_id=report_identifier(
                run_id=run_id,
                symbol=symbol,
                as_of=as_of,
                status=status,
                hypothesis=None,
                confidence=None,
                evidence_ids=[],
            ),
            run_id=run_id,
            symbol=symbol.upper(),
            as_of=as_of,
            generated_at=self._clock.now(),
            horizon=self._horizon(),
            status=status,
            status_detail=detail,
            data_quality=_empty_quality(),
            window=self._window_snapshot(),
            limits=self._limits_snapshot(),
            source_policy=self._sources.snapshot(),
            universe_run_id=universe.run_id if universe else None,
            universe_snapshot_id=universe.snapshot_id if universe else None,
            versions=self._versions(None),
        )

    def _skipped_report(
        self,
        symbol: str,
        as_of: datetime,
        *,
        run_id: str,
        universe: UniverseSelectionResult | None,
        limit: int,
    ) -> MarketResearchReport:
        return self._failed_report(
            symbol,
            as_of,
            run_id=run_id,
            universe=universe,
            status=ResearchStatus.SKIPPED_COST_LIMIT,
            detail=(
                f"selected by the universe but not researched: the configured ceiling of "
                f"{limit} underlyings per run was already reached. Recorded rather than "
                f"omitted, because 'we did not look' is not 'we found nothing'."
            ),
        )

    # --- assembly helpers ---------------------------------------------------
    def _resolve_symbols(
        self,
        universe: UniverseSelectionResult | None,
        requested: Sequence[str] | None,
        universe_run_id: str | None,
    ) -> tuple[list[str], tuple[ResearchStatus, str] | None]:
        """Which underlyings to research, or why none can be.

        Research consumes the universe; it never re-selects one. A requested
        symbol the universe did not choose is refused rather than researched,
        because a stage that could pick its own subjects would make the
        deterministic pre-filter upstream of it decorative.
        """
        if universe is None:
            return [], (
                ResearchStatus.NO_DATA,
                (
                    f"no universe run with id {universe_run_id!r} was found"
                    if universe_run_id
                    else "no universe has been selected yet; run 'universe run' first. "
                    "Research consumes a universe, it does not select one."
                ),
            )

        if universe.status is not UniverseSelectionStatus.SUCCESS:
            return [], (
                ResearchStatus.NO_DATA,
                (
                    f"universe run {universe.run_id} ended as {universe.status.value} and "
                    f"produced no universe; no downstream stage may consume it"
                ),
            )

        available = universe.symbols
        if requested is None:
            if not available:
                return [], (
                    ResearchStatus.NO_DATA,
                    f"universe run {universe.run_id} selected no underlyings",
                )
            return available, None

        wanted = [s.strip().upper() for s in requested if s.strip()]
        unknown = [s for s in wanted if s not in available]
        if unknown:
            return [], (
                ResearchStatus.CONFIGURATION_ERROR,
                (
                    f"{', '.join(unknown)} were not selected by universe run "
                    f"{universe.run_id} ({', '.join(available) or 'none'}). Research "
                    f"consumes the universe and cannot extend it."
                ),
            )
        return [s for s in available if s in set(wanted)], None

    def _build_result(
        self,
        *,
        run_id: str,
        as_of: datetime,
        status: ResearchStatus,
        status_detail: str | None,
        universe: UniverseSelectionResult | None,
        reports: list[MarketResearchReport],
        universe_asset_count: int,
    ) -> ResearchRunResult:
        return ResearchRunResult(
            run_id=run_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=status,
            status_detail=status_detail,
            horizon=self._horizon(),
            schema_version=RESEARCH_SCHEMA_VERSION,
            universe_run_id=universe.run_id if universe else None,
            universe_snapshot_id=universe.snapshot_id if universe else None,
            reports=sorted(reports, key=lambda r: r.symbol),
            counts=_counts(reports, universe_asset_count),
            versions=self._versions(
                next((r.agent_metadata for r in reports if r.agent_metadata), None)
            ),
        )

    def _finish(
        self,
        result: ResearchRunResult,
        *,
        inputs: dict[str, ResearchInput],
        dry_run: bool,
        duration_seconds: float,
    ) -> ResearchRun:
        """Persist unless this is a dry run, then log the run."""
        stored = False
        if not dry_run:
            self._research_repository.save(result)
            stored = True
        self._log(result, dry_run=dry_run, duration_seconds=duration_seconds)
        return ResearchRun(
            result=result,
            inputs=inputs,
            stored=stored,
            dry_run=dry_run,
            duration_seconds=duration_seconds,
        )

    def _horizon(self) -> ResearchHorizon:
        return ResearchHorizon(
            min_days=self._research_config.horizon.min_days,
            max_days=self._research_config.horizon.max_days,
        )

    def _window_snapshot(self) -> ResearchWindowSnapshot:
        window = self._research_config.window
        return ResearchWindowSnapshot(
            news_lookback_days=window.news_lookback_days,
            event_lookahead_days=window.event_lookahead_days,
            event_lookback_days=window.event_lookback_days,
            historical_lookback_days=window.historical_lookback_days,
            fundamentals_lookback_days=window.fundamentals_lookback_days,
            regulatory_lookback_days=window.regulatory_lookback_days,
            volatility_annualization_days=window.volatility_annualization_days,
        )

    def _limits_snapshot(self) -> ResearchLimitsSnapshot:
        limits = self._research_config.limits
        return ResearchLimitsSnapshot(
            max_evidence_items=limits.max_evidence_items,
            max_news_items=limits.max_news_items,
            max_events=limits.max_events,
            max_regulatory_items=limits.max_regulatory_items,
            max_fundamental_periods=limits.max_fundamental_periods,
        )

    def _versions(self, metadata: ResearchAgentMetadata | None) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            agent_version=metadata.agent_version if metadata else None,
            model_id=metadata.model_name if metadata else None,
            data_source_versions={
                "research": self._research_config.config_version,
                "sources": self._config.sources.config_version,
            },
        )

    @staticmethod
    def _log(result: ResearchRunResult, *, dry_run: bool, duration_seconds: float) -> None:
        """Structured record of the run. No secret ever reaches here."""
        _logger.info(
            "research.run",
            research_run_id=result.run_id,
            as_of=result.as_of.isoformat(),
            status=result.status.value,
            universe_run_id=result.universe_run_id,
            universe_assets=result.counts.universe_assets,
            researched=result.counts.researched,
            succeeded=result.counts.succeeded,
            insufficient_evidence=result.counts.insufficient_evidence,
            no_data=result.counts.no_data,
            failed=result.counts.failed,
            skipped=result.counts.skipped,
            duration_seconds=round(duration_seconds, 4),
            failure_state=(
                None if result.status is ResearchStatus.SUCCESS else result.status.value
            ),
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _agent_metadata(outcome: ResearchOutcome) -> ResearchAgentMetadata:
    """Build the audit record from a completed research call."""
    response = outcome.response
    identity = response.identity
    return ResearchAgentMetadata(
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


def _counts(reports: list[MarketResearchReport], universe_assets: int) -> ResearchRunCounts:
    def count(*statuses: ResearchStatus) -> int:
        return sum(1 for report in reports if report.status in statuses)

    return ResearchRunCounts(
        universe_assets=universe_assets,
        researched=len(reports) - count(ResearchStatus.SKIPPED_COST_LIMIT),
        succeeded=count(ResearchStatus.SUCCESS),
        insufficient_evidence=count(ResearchStatus.INSUFFICIENT_EVIDENCE),
        no_data=count(ResearchStatus.NO_DATA),
        failed=count(
            ResearchStatus.AI_UNAVAILABLE,
            ResearchStatus.AI_INVALID_OUTPUT,
            ResearchStatus.SEMANTIC_VALIDATION_FAILED,
            ResearchStatus.POINT_IN_TIME_ERROR,
            ResearchStatus.CONFIGURATION_ERROR,
        ),
        skipped=count(ResearchStatus.SKIPPED_COST_LIMIT),
    )


def _run_status(reports: list[MarketResearchReport]) -> ResearchStatus:
    """The run's own outcome, distinct from any one symbol's.

    A run succeeds when it produced at least one outlook. When it produced
    none, the run reports the reason the *majority* of its symbols failed for,
    so an operator sees "the model was unreachable" rather than a generic
    failure — but any single symbol's own status is always preserved on its own
    report.
    """
    if not reports:
        return ResearchStatus.NO_DATA
    if any(report.succeeded for report in reports):
        return ResearchStatus.SUCCESS

    considered = [r.status for r in reports if r.status is not ResearchStatus.SKIPPED_COST_LIMIT]
    if not considered:
        return ResearchStatus.SKIPPED_COST_LIMIT
    return max(set(considered), key=lambda status: (considered.count(status), status.value))


def _empty_quality() -> ResearchDataQualitySummary:
    """The quality verdict for a symbol whose input could not be assembled."""
    return ResearchDataQualitySummary(
        research_usable=False,
        classification=DataQuality.UNUSABLE,
        freshness_valid=False,
        completeness_valid=False,
        plausibility_valid=False,
        consistency_valid=False,
    )
