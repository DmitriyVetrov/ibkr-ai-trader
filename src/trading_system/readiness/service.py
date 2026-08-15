"""The readiness composition root (Milestone 12).

The single place collectors, policy, the evaluator and the store are wired
together. The CLI calls this and so would a scheduled job; a command and a
cadence cannot get differently configured readiness runs, for the same reason
``data/service.py`` and ``universe/service.py`` exist.

.. code-block:: text

    ReadinessService.check(scope)
          |
      collect ......... git, toolchain, config, stores, broker, HTTP probes
          |             each one optional, each one recorded either way
          v
      EvidenceBundle .. frozen
          |
      evaluate() ...... pure
          v
      ReadinessRun .... immutable, content-addressed, stored once

**This service has no order path.** It constructs no writable broker, imports
no execution service, and the ``ReadinessRun`` model refuses a non-zero
``orders_submitted`` outright. The paper-order gate is a *different* module
(:mod:`trading_system.readiness.paper_gate`) behind its own switches, and it is
not imported here.

Scopes exist because the collectors have wildly different costs. The offline
scope runs no subprocess and touches nothing outside the filesystem; the full
scope runs the whole test suite and can take minutes. A gate nobody runs
because it is slow is a gate that reports nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from trading_system.domain.enums import ReadinessRunStatus
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import (
    Settings,
    SystemConfig,
    load_config,
    project_root,
)
from trading_system.readiness import collectors
from trading_system.readiness.evaluator import evaluate
from trading_system.readiness.evidence import EvidenceBundle
from trading_system.readiness.models import ReadinessRun, run_identifier
from trading_system.readiness.policy import ReadinessPolicy
from trading_system.readiness.store import (
    FilesystemReadinessRepository,
    ReadinessRepository,
)

__all__ = ["CheckScope", "ReadinessService"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CheckScope:
    """Which collectors to run.

    Every flag defaults to *off* in :meth:`offline`, and a collector that is
    not run leaves its slot empty — which the evaluator reports as
    ``NOT_TESTED`` rather than as a pass. That asymmetry is the whole design:
    the cheap default cannot accidentally certify anything.
    """

    #: Subprocess-backed: pytest, ruff, mypy. Minutes.
    toolchain: bool = False
    #: The targeted safety suites. Minutes.
    safety_suites: bool = False
    #: One short-lived read-only broker connection.
    broker: bool = False
    #: Runs reconciliation against the broker.
    reconciliation: bool = False
    #: HTTP probes against a running observability stack, and a real emission.
    observability: bool = False
    #: Use the simulator instead of a real gateway, where a broker is involved.
    simulated: bool = False

    @classmethod
    def offline(cls) -> CheckScope:
        """Filesystem and configuration only. Seconds, and no side effects."""
        return cls()

    @classmethod
    def full(cls, *, simulated: bool = False) -> CheckScope:
        """Everything. This is what an acceptance run uses."""
        return cls(
            toolchain=True,
            safety_suites=True,
            broker=True,
            reconciliation=True,
            observability=True,
            simulated=simulated,
        )

    @property
    def skipped_slots(self) -> dict[str, str]:
        """Slots this scope deliberately does not fill, and why.

        Recorded on the run so "the observability stack was not started" shows
        up as an operator's *choice* rather than only as a scatter of
        ``NOT_TESTED`` criteria a reader has to piece together.
        """
        reasons: dict[str, str] = {}
        if not self.toolchain:
            for slot in ("test_suite", "lint", "format", "typecheck"):
                reasons[slot] = "toolchain collectors were not requested for this run"
        if not self.safety_suites:
            for slot in (
                "execution_safety",
                "position_lifecycle",
                "exit_management",
                "pnl",
                "agents",
                "agent_boundaries",
                "data",
                "privacy",
            ):
                reasons[slot] = "safety suites were not requested for this run"
        if not self.broker:
            reasons["broker"] = "the broker probe was not requested for this run"
        if not self.reconciliation:
            reasons["reconciliation"] = "reconciliation was not requested for this run"
        if not self.observability:
            for slot in (
                "observability_stack",
                "collector",
                "tempo",
                "prometheus",
                "loki",
                "grafana",
                "correlation",
            ):
                reasons[slot] = "the observability stack was not probed for this run"
        return reasons


@dataclass
class ReadinessCheck:
    """What one call to :meth:`ReadinessService.check` produced."""

    run: ReadinessRun
    bundle: EvidenceBundle
    stored: bool = False
    is_new: bool = False
    warnings: list[str] = field(default_factory=list)


class ReadinessService:
    """Assemble evidence, evaluate it, and store the result."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: SystemConfig | None = None,
        config_error: str | None = None,
        clock: Clock | None = None,
        repository: ReadinessRepository | None = None,
        repo_root: Path | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._config_error = config_error
        self._clock = clock or SystemClock()
        self._repo_root = repo_root or project_root()

        #: The *project* root — the directory holding ``config/`` and the data
        #: tree — kept separately from the resolved data root below.
        #:
        #: Every other service in this system takes the project root as ``root``
        #: and resolves ``data.storage.root`` beneath it itself. Handing one of
        #: them the already-resolved data root nests the tree a second time
        #: (``data/data/reconciliation/``) and it fails silently: the run
        #: succeeds, writes its artifacts somewhere nobody looks, and the next
        #: reader sees an empty store. Found by running the gate in a container
        #: and finding the reconciliation results missing.
        self._root = root or project_root()

        if config is not None:
            data_root = Path(config.data.storage.root)
            if not data_root.is_absolute():
                data_root = self._root / data_root
        else:
            data_root = self._root / "data"
        self._data_root = data_root
        self._repository = repository or FilesystemReadinessRepository(data_root / "readiness")

    # --- accessors ----------------------------------------------------------
    @property
    def repository(self) -> ReadinessRepository:
        return self._repository

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def config(self) -> SystemConfig | None:
        return self._config

    @property
    def settings(self) -> Settings:
        """The settings this assessment describes.

        Exposed so the paper gate is checked against the *same* settings and
        configuration the readiness run was assessed against. Building a second
        pair inside the command would let a gate refuse — or worse, permit —
        against a configuration nobody had assessed.
        """
        return self._settings

    @property
    def policy(self) -> ReadinessPolicy | None:
        return None if self._config is None else ReadinessPolicy.of(self._config.readiness)

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        root: Path | None = None,
    ) -> ReadinessService:
        """Build from the environment, tolerating a configuration that will not load.

        A readiness assessor that could not start because the configuration is
        broken would be unable to report the one thing it most needs to: that
        the configuration is broken. So the failure is captured and becomes
        evidence.
        """
        resolved = settings or Settings()
        config: SystemConfig | None = None
        error: str | None = None
        try:
            config = load_config(resolved.config_dir)
        except Exception as exc:
            # Deliberately broad. ConfigError is the expected failure, but a
            # malformed YAML file can surface as almost anything, and an
            # assessor that crashed on the way to reporting "the configuration
            # is broken" would withhold the finding that mattered most.
            error = str(exc)
        return cls(
            settings=resolved,
            config=config,
            config_error=error,
            clock=clock,
            root=root,
        )

    # --- collection -----------------------------------------------------------
    def collect(self, scope: CheckScope, *, as_of: datetime | None = None) -> EvidenceBundle:
        """Gather every piece of evidence this scope asks for.

        No collector is allowed to stop the others. A run in which the gateway
        is down and the toolchain is green must report both, and one that
        aborted at the first failure would report neither.
        """
        instant = as_of or self._clock.now()
        git = collectors.collect_git(repo_root=self._repo_root, observed_at=instant)
        revision = git.detail.get("git_revision")
        revision = str(revision) if revision else None

        bundle = EvidenceBundle(
            as_of=instant,
            git_revision=revision,
            working_tree_clean=git.detail.get("working_tree_clean"),
        )
        bundle = bundle.with_record("git", git)

        for slot, reason in scope.skipped_slots.items():
            bundle = bundle.with_skip(slot, reason)

        # --- always cheap ---------------------------------------------------
        bundle = bundle.with_record(
            "configuration",
            collectors.collect_configuration(
                settings=self._settings,
                config=self._config,
                config_error=self._config_error,
                observed_at=instant,
            ),
        )
        bundle = bundle.with_record(
            "test_isolation",
            collectors.collect_test_isolation(
                repo_root=self._repo_root, observed_at=instant, git_revision=revision
            ),
        )
        bundle = bundle.with_record(
            "secrets", collectors.collect_secrets(repo_root=self._repo_root, observed_at=instant)
        )
        bundle = bundle.with_record(
            "masking",
            collectors.collect_masking(
                data_root=self._data_root,
                observed_at=instant,
                account=self._settings.ibkr_account_id,
            ),
        )
        bundle = bundle.with_record(
            "scheduler",
            collectors.collect_scheduler(data_root=self._data_root, observed_at=instant),
        )
        bundle = bundle.with_record(
            "daily_loss",
            collectors.collect_daily_loss(data_root=self._data_root, observed_at=instant),
        )
        bundle = bundle.with_record(
            "operational_history",
            collectors.collect_operational_history(
                data_root=self._data_root,
                observed_at=instant,
                minimums=self._history_minimums(),
            ),
        )

        # --- toolchain --------------------------------------------------------
        if scope.toolchain:
            root = self._repo_root
            python = collectors.resolve_tool(root, "python")
            ruff = collectors.resolve_tool(root, "ruff")
            mypy = collectors.resolve_tool(root, "mypy")
            bundle = bundle.with_record(
                "test_suite",
                collectors.collect_test_suite(
                    repo_root=root, observed_at=instant, git_revision=revision, python=python
                ),
            )
            bundle = bundle.with_record(
                "lint",
                collectors.collect_lint(
                    repo_root=root, observed_at=instant, git_revision=revision, ruff=ruff
                ),
            )
            bundle = bundle.with_record(
                "format",
                collectors.collect_format(
                    repo_root=root, observed_at=instant, git_revision=revision, ruff=ruff
                ),
            )
            bundle = bundle.with_record(
                "typecheck",
                collectors.collect_typecheck(
                    repo_root=root, observed_at=instant, git_revision=revision, mypy=mypy
                ),
            )

        # --- targeted safety suites -------------------------------------------
        if scope.safety_suites:
            root = self._repo_root
            python = collectors.resolve_tool(root, "python")
            # These two carry an extra claim each — zero orders submitted, and
            # settlement idempotency — so they have their own collectors rather
            # than being rows in the table.
            bundle = bundle.with_record(
                "execution_safety",
                collectors.collect_execution_safety(
                    repo_root=root, observed_at=instant, git_revision=revision, python=python
                ),
            )
            bundle = bundle.with_record(
                "pnl",
                collectors.collect_pnl(
                    repo_root=root, observed_at=instant, git_revision=revision, python=python
                ),
            )
            for slot in collectors.SAFETY_SUITES:
                bundle = bundle.with_record(
                    slot,
                    collectors.collect_safety_suite(
                        slot,
                        repo_root=root,
                        observed_at=instant,
                        git_revision=revision,
                        python=python,
                    ),
                )

        # --- broker ------------------------------------------------------------
        if scope.broker and self._config is not None:
            bundle = bundle.with_record(
                "broker",
                collectors.collect_broker(
                    settings=self._settings,
                    config=self._config,
                    project_root=self._root,
                    observed_at=instant,
                    simulated=scope.simulated,
                ),
            )

        if scope.reconciliation and self._config is not None:
            bundle = bundle.with_record(
                "reconciliation",
                collectors.collect_reconciliation(
                    settings=self._settings,
                    config=self._config,
                    project_root=self._root,
                    observed_at=instant,
                    simulated=scope.simulated,
                ),
            )

        # --- observability ------------------------------------------------------
        if scope.observability and self._config is not None:
            bundle = self._collect_observability(bundle, instant)
        else:
            # The cardinality guard is still readable from configuration alone,
            # and reading it there is honest: the record says the live
            # exposition was not checked.
            bundle = bundle.with_record(
                "cardinality",
                collectors.collect_cardinality(
                    config=self._config, exposition=None, observed_at=instant
                ),
            )

        return bundle

    def _collect_observability(self, bundle: EvidenceBundle, instant: datetime) -> EvidenceBundle:
        """Probe the stack, emit real telemetry, and ask each backend for it.

        Imported here rather than at module scope on purpose. The probe module
        imports ``urllib``; keeping it out of this module's import graph until
        somebody actually asks for an observability run is what lets the
        boundary test assert that a readiness *evaluation* cannot reach a
        socket.
        """
        from trading_system.readiness import telemetry_emission
        from trading_system.readiness.observability_probe import (
            ObservabilityProbe,
            probe_collector,
            probe_correlation,
            probe_grafana,
            probe_loki,
            probe_prometheus,
            probe_services,
            probe_tempo,
        )

        assert self._config is not None  # guarded by the caller
        acceptance = self._config.readiness.observability_acceptance
        probe = ObservabilityProbe(config=acceptance)

        services = probe_services(probe, observed_at=instant)
        bundle = bundle.with_record("observability_stack", services)

        # Emit a real trace, metric and log through the ordinary application
        # telemetry path — not a bespoke OTLP client. The point of the gate is
        # that *this application's* pipeline works.
        emission = telemetry_emission.emit_acceptance_signals(
            settings=self._settings, config=self._config
        )

        collector = probe_collector(probe, observed_at=instant)
        bundle = bundle.with_record("collector", collector)

        tempo = probe_tempo(probe, trace_id=emission.trace_id, observed_at=instant)
        bundle = bundle.with_record("tempo", tempo)

        prometheus = probe_prometheus(probe, metric=emission.metric, observed_at=instant)
        bundle = bundle.with_record("prometheus", prometheus)

        loki = probe_loki(
            probe,
            query='{service_name="trading-system"}',
            observed_at=instant,
            expect_substring=emission.trace_id,
        )
        bundle = bundle.with_record("loki", loki)

        bundle = bundle.with_record(
            "grafana",
            probe_grafana(
                probe,
                observed_at=instant,
                required_datasources=acceptance.required_datasources,
                required_dashboards=acceptance.required_dashboards,
            ),
        )
        bundle = bundle.with_record(
            "correlation",
            probe_correlation(
                trace_id=emission.trace_id, tempo=tempo, loki=loki, observed_at=instant
            ),
        )
        bundle = bundle.with_record(
            "cardinality",
            collectors.collect_cardinality(
                config=self._config,
                exposition=emission.exposition,
                observed_at=instant,
            ),
        )
        return bundle

    def _history_minimums(self) -> dict[str, int]:
        if self._config is None:
            return {}
        history = self._config.readiness.operational_history
        return {
            "min_readiness_runs": history.min_readiness_runs,
            "min_distinct_days": history.min_distinct_days,
            "min_reconciliation_runs": history.min_reconciliation_runs,
            "min_scheduler_ticks": history.min_scheduler_ticks,
            "min_history_days": history.min_history_days,
        }

    # --- evaluation ------------------------------------------------------------
    def check(
        self,
        scope: CheckScope | None = None,
        *,
        as_of: datetime | None = None,
        store: bool = True,
        bundle: EvidenceBundle | None = None,
    ) -> ReadinessCheck:
        """Collect, evaluate and (optionally) store one readiness run."""
        resolved_scope = scope or CheckScope.offline()
        instant = as_of or self._clock.now()

        if self._config is None:
            run = ReadinessRun(
                readiness_run_id=run_identifier(
                    assessment_id="none",
                    as_of=instant,
                    status=ReadinessRunStatus.CONFIGURATION_ERROR.value,
                ),
                status=ReadinessRunStatus.CONFIGURATION_ERROR,
                evaluated_at=instant,
                as_of=instant,
                trading_mode=self._settings.trading_mode,
                error=self._config_error or "configuration could not be loaded",
            )
            return ReadinessCheck(run=run, bundle=EvidenceBundle(as_of=instant))

        evidence = bundle if bundle is not None else self.collect(resolved_scope, as_of=instant)
        policy = ReadinessPolicy.of(self._config.readiness)

        warnings = [
            f"config/readiness.yaml names {criterion.value}, which no criterion defines. "
            f"It can never be satisfied, so the level it blocks can never open."
            for criterion in policy.unknown_criteria()
        ]

        assessment = evaluate(
            evidence,
            policy,
            trading_mode=self._settings.trading_mode,
            system_version=self._system_version(),
            config_version=self._config.application.config_version,
        )
        status = (
            ReadinessRunStatus.COMPLETE
            if not evidence.not_collected
            else ReadinessRunStatus.PARTIAL
        )
        if not store:
            status = ReadinessRunStatus.DRY_RUN

        run = ReadinessRun(
            readiness_run_id=run_identifier(
                assessment_id=assessment.assessment_id, as_of=instant, status=status.value
            ),
            status=status,
            evaluated_at=instant,
            as_of=evidence.as_of,
            trading_mode=self._settings.trading_mode,
            git_revision=evidence.git_revision,
            working_tree_clean=evidence.working_tree_clean,
            system_version=self._system_version(),
            config_version=self._config.application.config_version,
            readiness_config_version=self._config.readiness.config_version,
            assessment=assessment,
            not_collected=dict(evidence.not_collected),
        )

        stored = False
        is_new = False
        if store:
            _, is_new = self._repository.save_run(run)
            stored = True
            _logger.info(
                "readiness.assessed",
                readiness_run_id=run.readiness_run_id,
                level=run.level.value,
                git_revision=run.git_revision,
                reobserved=not is_new,
            )

        return ReadinessCheck(
            run=run, bundle=evidence, stored=stored, is_new=is_new, warnings=warnings
        )

    def _system_version(self) -> str | None:
        try:
            from trading_system import __version__

            return __version__
        except Exception:  # pragma: no cover - defensive
            return None

    # --- reading ---------------------------------------------------------------
    def latest(self) -> ReadinessRun | None:
        return self._repository.latest_run()

    def get(self, readiness_run_id: str) -> ReadinessRun | None:
        return self._repository.get_run(readiness_run_id)

    def history(self, limit: int | None = None):  # type: ignore[no-untyped-def]
        return self._repository.history(limit)

    def now(self) -> datetime:
        return self._clock.now().astimezone(UTC)
