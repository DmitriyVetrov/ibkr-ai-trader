"""The universe selection service.

One pipeline, run in this order:

.. code-block:: text

    configured source
          |
    point-in-time evidence      (DataRepository only; no provider, no broker)
          |
    deterministic pre-filter    <- decides eligibility; nothing above reverses it
          |
    agent input contract        <- structurally cannot carry a rejected asset
          |
    AI ranking                  <- reorders; cannot admit, cannot exceed the cap
          |
    deterministic validation    <- rejects the whole response on any violation
          |
    immutable run record        <- appended, never overwritten

Properties the pipeline holds regardless of which path it takes:

* **It cannot trade.** No broker is constructed and no order path is reachable
  from here. A universe run is a pure consumer of history already collected.
* **It fails closed.** An unreachable or unusable model ends the run with a
  status naming what happened and *no* selected assets. A deterministic
  ordering is substituted only when configuration explicitly permits it, and
  such runs are stamped ``DETERMINISTIC_ONLY`` so the record never implies a
  model was involved.
* **It is reproducible.** The run identifier is derived from the instant, the
  configuration and the data snapshots consumed — so the same inputs and the
  same ranking produce byte-identical output, and a re-run is idempotent rather
  than a second record of the same event.
* **Every asset is explainable.** Selected assets carry reasons and provenance;
  rejected assets carry a machine-readable reason. There is no third category.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system import __version__ as application_version
from trading_system.agents.base import (
    AgentError,
    AgentInvalidOutputError,
    AgentUnavailableError,
    LLMClient,
)
from trading_system.agents.prompts import prompt_fingerprint
from trading_system.agents.universe_selector import (
    PROMPT_NAME,
    RankingOutcome,
    UniverseSelectorAgent,
)
from trading_system.data.repository import DataRepository, FilesystemDataRepository
from trading_system.domain.enums import (
    ConfidenceLevel,
    Optionability,
    SelectionMethod,
    UniverseEligibility,
    UniverseRejectionReason,
    UniverseSelectionReason,
    UniverseSelectionStatus,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig, project_root
from trading_system.universe.features import EvidenceGatherer
from trading_system.universe.filters import (
    FilterOutcome,
    PreFilter,
    collect_snapshot_ids,
    filter_config_snapshot,
)
from trading_system.universe.models import (
    UNIVERSE_SCHEMA_VERSION,
    AgentMetadata,
    CandidateAsset,
    RejectedAsset,
    SelectedAsset,
    UniverseAgentRanking,
    UniverseRunCounts,
    UniverseSelectionInput,
    UniverseSelectionResult,
    universe_snapshot_id,
)
from trading_system.universe.source import UniverseSourceError, load_symbols, source_reference
from trading_system.universe.store import (
    FilesystemUniverseRepository,
    UniverseHistoryEntry,
    UniverseRepository,
)
from trading_system.universe.validation import AgentOutputInvalidError

__all__ = ["UniverseRun", "UniverseSelectionService", "build_run_id"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UniverseRun:
    """One run's outcome, plus what the caller needs to report it."""

    result: UniverseSelectionResult
    agent_input: UniverseSelectionInput | None
    stored: bool
    dry_run: bool
    #: Wall-clock time the run took. Deliberately not part of the stored record:
    #: a measured elapsed time is a fact about the machine, not the decision.
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.result.status is UniverseSelectionStatus.SUCCESS


def build_run_id(
    *,
    as_of: datetime,
    config_version: str,
    source_version: str,
    filter_fingerprint: str,
    snapshot_ids: list[str],
) -> str:
    """Derive a run identifier from everything that determines the run.

    Derived rather than random so a re-run over unchanged inputs is idempotent —
    the store recognises it as the same run instead of accumulating duplicate
    records of one event. Data snapshots are folded in, so a run over *new* data
    at the same instant is correctly a different run rather than a collision.
    """
    from trading_system.data.hashing import stable_hash

    digest = stable_hash(
        [
            config_version,
            source_version,
            filter_fingerprint,
            as_of.isoformat(),
            sorted(snapshot_ids),
        ]
    )
    return f"universe-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"


class UniverseSelectionService:
    """Builds and drives universe selection from configuration.

    Like the data layer's service, this is the single composition root: the CLI
    and the future scheduler both go through it, so a command and a scheduled
    job cannot end up with differently configured pipelines.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig,
        clock: Clock | None = None,
        data_repository: DataRepository | None = None,
        universe_repository: UniverseRepository | None = None,
        llm_client: LLMClient | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._clock = clock or SystemClock()
        self._root = root
        self._universe_config = config.universe

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
        self._gatherer = EvidenceGatherer(self._data_repository)
        self._pre_filter = PreFilter(self._universe_config.filters)
        self._llm_client = llm_client

    # --- exposed pieces ----------------------------------------------------
    @property
    def repository(self) -> UniverseRepository:
        return self._universe_repository

    @property
    def data_repository(self) -> DataRepository:
        return self._data_repository

    @property
    def pre_filter(self) -> PreFilter:
        return self._pre_filter

    @property
    def data_root(self) -> Path:
        return self._data_root

    def configured_symbols(self) -> list[str]:
        """The candidate pool, as configured. Raises on an unusable source."""
        return load_symbols(self._universe_config.source, root=self._root)

    def history(self, limit: int | None = None) -> list[UniverseHistoryEntry]:
        return self._universe_repository.history(limit=limit)

    def get(self, run_id: str) -> UniverseSelectionResult | None:
        return self._universe_repository.get(run_id)

    def latest(self) -> UniverseSelectionResult | None:
        return self._universe_repository.latest()

    # --- the run -----------------------------------------------------------
    def run(self, *, as_of: datetime | None = None, dry_run: bool = False) -> UniverseRun:
        """Select a universe for ``as_of`` (default: now).

        ``dry_run`` performs the whole pipeline, including the agent call if
        configured, and simply does not persist the result. It cannot reach a
        broker either way — there is no order path from this method.
        """
        started = time.perf_counter()
        instant = as_of or self._clock.now()

        try:
            symbols = self.configured_symbols()
        except UniverseSourceError as exc:
            return self._finish(
                self._failed_result(
                    instant,
                    UniverseSelectionStatus.CONFIGURATION_ERROR,
                    str(exc),
                ),
                agent_input=None,
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        evidence = self._gatherer.gather_all(symbols, instant)
        outcomes = self._pre_filter.apply(symbols, evidence, instant)
        candidates = [o.candidate for o in outcomes if o.candidate is not None]
        rejections = [o.rejection for o in outcomes if o.rejection is not None]
        snapshot_ids = collect_snapshot_ids(outcomes)

        run_id = build_run_id(
            as_of=instant,
            config_version=self._universe_config.config_version,
            source_version=self._universe_config.source.version,
            filter_fingerprint=self._filter_fingerprint(),
            snapshot_ids=snapshot_ids,
        )

        if not candidates:
            status = self._empty_status(outcomes)
            return self._finish(
                self._build_result(
                    run_id=run_id,
                    as_of=instant,
                    status=status,
                    status_detail=_empty_detail(status, len(symbols)),
                    selection_method=SelectionMethod.DETERMINISTIC_ONLY,
                    selected=[],
                    rejected=rejections,
                    agent_metadata=None,
                    snapshot_ids=snapshot_ids,
                    counts=UniverseRunCounts(
                        candidates=len(symbols),
                        deterministic_pass=0,
                        deterministic_reject=len(rejections),
                    ),
                ),
                agent_input=None,
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        agent_input = self._build_agent_input(run_id, instant, candidates, snapshot_ids)
        max_selected = self._universe_config.filters.max_selected_assets

        ranking: UniverseAgentRanking | None = None
        agent_metadata: AgentMetadata | None = None
        method = SelectionMethod.DETERMINISTIC_ONLY
        failure: tuple[UniverseSelectionStatus, str] | None = None

        if self._universe_config.ai_ranking.enabled:
            try:
                outcome = self._rank(agent_input, max_selected=max_selected)
            except AgentUnavailableError as exc:
                failure = (UniverseSelectionStatus.AI_UNAVAILABLE, str(exc))
            except (AgentInvalidOutputError, AgentOutputInvalidError) as exc:
                failure = (UniverseSelectionStatus.AI_INVALID_OUTPUT, str(exc))
            except AgentError as exc:  # pragma: no cover - defensive
                failure = (UniverseSelectionStatus.AI_UNAVAILABLE, str(exc))
            else:
                ranking = outcome.ranking
                agent_metadata = _agent_metadata(outcome)
                method = SelectionMethod.AI_RANKED

        fallback_allowed = self._universe_config.ai_ranking.allow_deterministic_fallback
        if failure is not None and not fallback_allowed:
            status, detail = failure
            result = self._build_result(
                run_id=run_id,
                as_of=instant,
                status=status,
                status_detail=detail,
                selection_method=SelectionMethod.DETERMINISTIC_ONLY,
                selected=[],
                rejected=rejections
                + [
                    _not_ranked(candidate, status)
                    for candidate in sorted(candidates, key=lambda c: c.symbol)
                ],
                agent_metadata=None,
                snapshot_ids=snapshot_ids,
                counts=UniverseRunCounts(
                    candidates=len(symbols),
                    deterministic_pass=len(candidates),
                    deterministic_reject=len(rejections),
                    ai_input=len(candidates),
                ),
            )
            return self._finish(
                result,
                agent_input=agent_input,
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - started,
            )

        if ranking is None:
            # Either AI ranking is disabled, or it failed and configuration
            # explicitly permits a deterministic ordering. Either way the run is
            # stamped DETERMINISTIC_ONLY, so the record never implies otherwise.
            ranking = _deterministic_ranking(agent_input, max_selected=max_selected)

        selected, not_selected = self._apply_ranking(ranking, agent_input)
        counts = UniverseRunCounts(
            candidates=len(symbols),
            deterministic_pass=len(candidates),
            deterministic_reject=len(rejections),
            ai_input=len(candidates),
            ai_selected=len(selected),
            final=len(selected),
        )
        result = self._build_result(
            run_id=run_id,
            as_of=instant,
            status=UniverseSelectionStatus.SUCCESS,
            status_detail=(
                None
                if failure is None
                else f"deterministic fallback after {failure[0].value}: {failure[1]}"
            ),
            selection_method=method,
            selected=selected,
            rejected=rejections + not_selected,
            agent_metadata=agent_metadata,
            snapshot_ids=snapshot_ids,
            counts=counts,
        )
        return self._finish(
            result,
            agent_input=agent_input,
            dry_run=dry_run,
            duration_seconds=time.perf_counter() - started,
        )

    # --- steps -------------------------------------------------------------
    def _rank(self, agent_input: UniverseSelectionInput, *, max_selected: int) -> RankingOutcome:
        policy = self._universe_config.ai_ranking
        agent = UniverseSelectorAgent(
            self._llm_client or self._build_client(),
            max_output_tokens=policy.max_output_tokens,
            timeout_seconds=policy.timeout_seconds,
            effort=policy.effort,
        )
        return agent.rank(agent_input, max_selected=max_selected)

    def _build_client(self) -> LLMClient:
        """Construct the configured provider's client, or fail honestly."""
        policy = self._universe_config.ai_ranking
        if policy.model_provider.upper() != "ANTHROPIC":
            raise AgentUnavailableError(
                f"AI ranking provider {policy.model_provider!r} is not implemented; "
                f"configure ANTHROPIC or disable AI ranking"
            )
        from trading_system.agents.anthropic_client import AnthropicLLMClient

        key = self._settings.anthropic_api_key
        return AnthropicLLMClient(
            api_key=key.get_secret_value() if key is not None else "",
            model_name=policy.model_name,
            prompt_version=policy.prompt_version,
            prompt_fingerprint=prompt_fingerprint(PROMPT_NAME),
        )

    def _build_agent_input(
        self,
        run_id: str,
        as_of: datetime,
        candidates: list[CandidateAsset],
        snapshot_ids: list[str],
    ) -> UniverseSelectionInput:
        return UniverseSelectionInput(
            run_id=run_id,
            as_of=as_of,
            universe_source=source_reference(
                self._universe_config.source, [c.symbol for c in candidates]
            ),
            deterministic_filter_config=filter_config_snapshot(self._universe_config.filters),
            candidate_assets=candidates,
            data_snapshot_ids=snapshot_ids,
            max_selected_assets=self._universe_config.filters.max_selected_assets,
        )

    def _apply_ranking(
        self, ranking: UniverseAgentRanking, agent_input: UniverseSelectionInput
    ) -> tuple[list[SelectedAsset], list[RejectedAsset]]:
        """Turn a validated ranking into stored assets.

        Any eligible candidate the ranking did not select becomes an explicit
        ``NOT_SELECTED_BY_RANKING`` rejection — including one the agent simply
        omitted. Silence is not a verdict, and an asset that vanished without a
        reason would break the guarantee that every candidate is explainable.
        """
        by_symbol = {c.symbol: c for c in agent_input.candidate_assets}
        selected: list[SelectedAsset] = []
        rejected: list[RejectedAsset] = []
        ranked: set[str] = set()

        for entry in ranking.selected:
            candidate = by_symbol[entry.symbol]
            ranked.add(entry.symbol)
            selected.append(
                SelectedAsset(
                    symbol=entry.symbol,
                    rank=entry.rank if entry.rank is not None else len(selected) + 1,
                    deterministic_eligibility=candidate.deterministic_eligibility,
                    reasons=list(entry.reasons),
                    data_quality=candidate.data_quality,
                    confidence=entry.confidence,
                    selection_score=entry.selection_score,
                    rationale=entry.rationale,
                    optionability=candidate.optionability,
                    reference_price=candidate.reference_price,
                    underlying_volume=candidate.underlying_volume,
                    source=candidate.source,
                )
            )

        for entry in ranking.not_selected:
            candidate = by_symbol[entry.symbol]
            ranked.add(entry.symbol)
            rejected.append(
                RejectedAsset(
                    symbol=entry.symbol,
                    deterministic_eligibility=candidate.deterministic_eligibility,
                    reason=UniverseRejectionReason.NOT_SELECTED_BY_RANKING,
                    detail=entry.rationale
                    or f"ranked but not selected ({', '.join(r.value for r in entry.reasons)})",
                    optionability=candidate.optionability,
                    source=candidate.source,
                )
            )

        for symbol, candidate in sorted(by_symbol.items()):
            if symbol in ranked:
                continue
            rejected.append(
                RejectedAsset(
                    symbol=symbol,
                    deterministic_eligibility=candidate.deterministic_eligibility,
                    reason=UniverseRejectionReason.NOT_SELECTED_BY_RANKING,
                    detail="the ranking returned no verdict for this candidate",
                    optionability=candidate.optionability,
                    source=candidate.source,
                )
            )

        return selected, rejected

    # --- assembly ----------------------------------------------------------
    def _build_result(
        self,
        *,
        run_id: str,
        as_of: datetime,
        status: UniverseSelectionStatus,
        status_detail: str | None,
        selection_method: SelectionMethod,
        selected: list[SelectedAsset],
        rejected: list[RejectedAsset],
        agent_metadata: AgentMetadata | None,
        snapshot_ids: list[str],
        counts: UniverseRunCounts,
    ) -> UniverseSelectionResult:
        source = source_reference(
            self._universe_config.source, [a.symbol for a in selected] if selected else None
        )
        filter_config = filter_config_snapshot(self._universe_config.filters)
        snapshot = universe_snapshot_id(
            run_id=run_id,
            as_of=as_of,
            source=source,
            filter_config=filter_config,
            selected=selected,
        )
        return UniverseSelectionResult(
            snapshot_id=snapshot,
            run_id=run_id,
            as_of=as_of,
            generated_at=self._clock.now(),
            status=status,
            status_detail=status_detail,
            selection_method=selection_method,
            universe_source=source,
            deterministic_filter_config=filter_config,
            selected_assets=selected,
            rejected_assets=sorted(rejected, key=lambda a: a.symbol),
            agent_metadata=agent_metadata,
            input_snapshot_ids=snapshot_ids,
            counts=counts,
            schema_version=UNIVERSE_SCHEMA_VERSION,
            versions=SystemVersions(
                application_version=application_version,
                config_version=self._config.application.config_version,
                agent_version=(
                    f"{agent_metadata.prompt_version}+{agent_metadata.agent_version}"
                    if agent_metadata and agent_metadata.agent_version
                    else None
                ),
                model_id=agent_metadata.model_name if agent_metadata else None,
                data_source_versions={"universe": self._universe_config.config_version},
            ),
        )

    def _failed_result(
        self,
        as_of: datetime,
        status: UniverseSelectionStatus,
        detail: str,
    ) -> UniverseSelectionResult:
        """A run that could not even build a candidate pool."""
        return self._build_result(
            run_id=build_run_id(
                as_of=as_of,
                config_version=self._universe_config.config_version,
                source_version=self._universe_config.source.version,
                filter_fingerprint=self._filter_fingerprint(),
                snapshot_ids=[],
            ),
            as_of=as_of,
            status=status,
            status_detail=detail,
            selection_method=SelectionMethod.DETERMINISTIC_ONLY,
            selected=[],
            rejected=[],
            agent_metadata=None,
            snapshot_ids=[],
            counts=UniverseRunCounts(),
        )

    def _finish(
        self,
        result: UniverseSelectionResult,
        *,
        agent_input: UniverseSelectionInput | None,
        dry_run: bool,
        duration_seconds: float,
    ) -> UniverseRun:
        """Persist unless this is a dry run, then log the run."""
        stored = False
        if not dry_run:
            self._universe_repository.save(result)
            stored = True
        self._log(result, dry_run=dry_run, duration_seconds=duration_seconds)
        return UniverseRun(
            result=result,
            agent_input=agent_input,
            stored=stored,
            dry_run=dry_run,
            duration_seconds=duration_seconds,
        )

    def _filter_fingerprint(self) -> str:
        from trading_system.data.hashing import stable_hash

        snapshot = filter_config_snapshot(self._universe_config.filters)
        return stable_hash(snapshot.model_dump(mode="json"))

    def _empty_status(self, outcomes: list[FilterOutcome]) -> UniverseSelectionStatus:
        """Distinguish "nothing qualified" from "we have no data".

        Both produce an empty universe, and they demand different responses: one
        is a fact about the market and the policy, the other is a fact about our
        own plumbing. Only the first should be read as evidence.
        """
        rejections = [o.rejection for o in outcomes if o.rejection is not None]
        if not rejections:
            return UniverseSelectionStatus.NO_CANDIDATES
        if all(r.reason is UniverseRejectionReason.DATA_UNAVAILABLE for r in rejections):
            return UniverseSelectionStatus.DATA_UNAVAILABLE
        return UniverseSelectionStatus.NO_CANDIDATES

    @staticmethod
    def _log(result: UniverseSelectionResult, *, dry_run: bool, duration_seconds: float) -> None:
        """Structured record of the run. No secret ever reaches here."""
        _logger.info(
            "universe.run",
            universe_run_id=result.run_id,
            snapshot_id=result.snapshot_id,
            as_of=result.as_of.isoformat(),
            status=result.status.value,
            selection_method=result.selection_method.value,
            candidate_count=result.counts.candidates,
            deterministic_pass_count=result.counts.deterministic_pass,
            deterministic_reject_count=result.counts.deterministic_reject,
            ai_input_count=result.counts.ai_input,
            ai_selected_count=result.counts.ai_selected,
            final_count=result.counts.final,
            duration_seconds=round(duration_seconds, 4),
            failure_state=(
                None if result.status is UniverseSelectionStatus.SUCCESS else result.status.value
            ),
            model_name=result.agent_metadata.model_name if result.agent_metadata else None,
            prompt_version=(
                result.agent_metadata.prompt_version if result.agent_metadata else None
            ),
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------
def _deterministic_ranking(
    agent_input: UniverseSelectionInput, *, max_selected: int
) -> UniverseAgentRanking:
    """Order candidates without a model, for explicitly configured runs.

    Ordered by underlying volume descending, ties broken by symbol, candidates
    with no reported volume last. Deliberately simple and deliberately not a
    hidden scoring model: this is a documented fallback that has to be
    switched on, and a run using it is stamped ``DETERMINISTIC_ONLY`` so it can
    never be mistaken for a model's judgement.
    """
    ordered = sorted(
        agent_input.candidate_assets,
        key=lambda c: (
            0 if c.underlying_volume is not None else 1,
            -(c.underlying_volume or Decimal(0)),
            c.symbol,
        ),
    )

    from trading_system.universe.models import UniverseAssetRanking

    rankings: list[UniverseAssetRanking] = []
    for index, candidate in enumerate(ordered):
        selected = index < max_selected
        rankings.append(
            UniverseAssetRanking(
                symbol=candidate.symbol,
                selection="SELECTED" if selected else "NOT_SELECTED",
                rank=index + 1 if selected else None,
                reasons=_deterministic_reasons(candidate, selected),
                confidence=ConfidenceLevel.LOW,
                rationale=("deterministic ordering by underlying volume; no model was consulted"),
            )
        )
    return UniverseAgentRanking(run_id=agent_input.run_id, rankings=rankings)


def _deterministic_reasons(
    candidate: CandidateAsset, selected: bool
) -> list[UniverseSelectionReason]:
    """Reason codes the candidate's own evidence actually supports."""
    reasons: list[UniverseSelectionReason] = []
    if candidate.underlying_volume is not None:
        reasons.append(
            UniverseSelectionReason.HIGH_UNDERLYING_LIQUIDITY
            if selected
            else UniverseSelectionReason.LOWER_UNDERLYING_LIQUIDITY
        )
    if candidate.optionability is Optionability.TRUE:
        reasons.append(UniverseSelectionReason.OPTIONS_AVAILABLE)
    elif candidate.optionability is Optionability.UNKNOWN:
        reasons.append(UniverseSelectionReason.OPTIONABILITY_NOT_ESTABLISHED)
    if candidate.data_quality.research_usable:
        reasons.append(UniverseSelectionReason.SUFFICIENT_DATA_QUALITY)
    if not selected:
        reasons.append(UniverseSelectionReason.UNIVERSE_SIZE_LIMIT)
    return reasons or [UniverseSelectionReason.SUFFICIENT_DATA_QUALITY]


def _agent_metadata(outcome: RankingOutcome) -> AgentMetadata:
    """Build the audit record from a completed ranking call."""
    response = outcome.response
    identity = response.identity
    return AgentMetadata(
        model_provider=identity.provider,
        model_name=identity.model_name,
        model_version=identity.model_version,
        prompt_version=identity.prompt_version,
        agent_version=identity.agent_version,
        generated_at=response.generated_at,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        raw_response=response.text,
    )


def _not_ranked(candidate: CandidateAsset, status: UniverseSelectionStatus) -> RejectedAsset:
    """An eligible candidate that never got ranked because the run failed."""
    return RejectedAsset(
        symbol=candidate.symbol,
        deterministic_eligibility=UniverseEligibility.ELIGIBLE,
        reason=UniverseRejectionReason.NOT_SELECTED_BY_RANKING,
        detail=(
            f"deterministically eligible but never ranked: the run ended as "
            f"{status.value}. Eligibility is preserved here so the failure cannot be "
            f"mistaken for the asset being unsuitable."
        ),
        optionability=candidate.optionability,
        source=candidate.source,
    )


def _empty_detail(status: UniverseSelectionStatus, considered: int) -> str:
    if status is UniverseSelectionStatus.DATA_UNAVAILABLE:
        return (
            f"none of the {considered} configured symbols had market data visible at this "
            f"instant; collect data first (data collect --symbol ...). This is a fact "
            f"about our plumbing, not about the market."
        )
    return (
        f"all {considered} configured symbols were considered and none passed the "
        f"deterministic filters. An empty universe is a valid outcome; filters are "
        f"never relaxed automatically to produce candidates."
    )
