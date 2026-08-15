"""Fixtures for the Milestone 12 readiness suites.

Four rules hold across every test here:

* **Offline.** No gateway, no Docker, no collector, no model, no socket. The
  evaluator is a pure function and the tests exercise it as one; where a
  collector is involved it is handed a constructed evidence record rather than
  being allowed to go and look.
* **Nothing writes into the repository's own ``data/``.** Every store is
  rooted at ``tmp_path``, and a test asserts that a readiness run leaves no
  stray file behind.
* **Time is a fixture.** Evidence ages against a fixed ``as_of``, so a
  freshness test asserts a rule rather than racing a clock.
* **Zero orders, structurally.** Nothing in this package can construct a
  writable broker; an autouse fixture makes any attempt to reach one an
  outright test failure rather than a wrong status line.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_system.domain.enums import (
    ReadinessCriterionId,
    ReadinessDomain,
    ReadinessEvidenceKind,
    ReadinessLevel,
    ReadinessReasonCode,
    ReadinessStatus,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import (
    ReadinessConfig,
    Settings,
    SystemConfig,
    load_config,
)
from trading_system.readiness.evidence import EvidenceBundle, EvidenceRecord
from trading_system.readiness.models import ReadinessCriterion
from trading_system.readiness.policy import ReadinessPolicy

#: The instant every assessment in this suite is made as of.
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

#: The revision the evidence describes. Twelve hex characters is enough to be
#: recognisable in a failure message and short enough to read.
REVISION = "abc123def456"
OTHER_REVISION = "999888777666"


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def readiness_clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def readiness_settings() -> Settings:
    """Settings pinned to PAPER, ignoring any developer ``.env``."""
    return Settings(_env_file=None, trading_mode="PAPER")


#: The repository's own ``config/``, resolved at import time.
#:
#: Captured before the autouse ``PROJECT_ROOT`` override below points
#: ``project_root()`` at ``tmp_path``. The override is what stops a test writing
#: into the repository's data tree; without this constant it would also stop the
#: tests reading the shipped configuration they are about.
REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def system_config() -> SystemConfig:
    """The shipped configuration, read from the repository rather than tmp_path."""
    return load_config(REPO_CONFIG_DIR)


@pytest.fixture
def readiness_config(system_config: SystemConfig) -> ReadinessConfig:
    return system_config.readiness


@pytest.fixture
def policy(readiness_config: ReadinessConfig) -> ReadinessPolicy:
    return ReadinessPolicy.of(readiness_config)


@pytest.fixture
def readiness_root(tmp_path: Path) -> Path:
    """A project root under ``tmp_path``, never the repository's own."""
    return tmp_path / "project"


@pytest.fixture
def no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything in a readiness test reaches for a broker.

    An assertion rather than a stub returning a refusal: a stub would let a
    test pass while proving something weaker than it claims, which is exactly
    the mistake ``tests/execution/test_execution_safety.py`` records about
    asserting ``BROKER_UNAVAILABLE`` instead of ``never_called``.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "a readiness test asked for a broker. The readiness core has no broker and no "
            "order path; if this fires, a collector has been wired into the evaluator."
        )

    monkeypatch.setattr("trading_system.broker.factory.build_broker", refuse)
    monkeypatch.setattr("trading_system.broker.factory.build_execution_broker", refuse)


@pytest.fixture(autouse=True)
def _no_stray_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ``project_root()`` at ``tmp_path`` for anything that falls back to it."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------
@pytest.fixture
def make_record() -> Callable[..., EvidenceRecord]:
    """Build one evidence record with sensible, overridable defaults."""

    def build(
        *,
        kind: ReadinessEvidenceKind = ReadinessEvidenceKind.COMMAND,
        source: str = "test",
        observed_at: datetime | None = None,
        collected: bool = True,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
        git_revision: str | None = REVISION,
        artifact_ids: tuple[str, ...] = (),
        age: timedelta | None = None,
    ) -> EvidenceRecord:
        instant = observed_at or (NOW - age if age else NOW)
        return EvidenceRecord.of(
            kind=kind,
            source=source,
            observed_at=instant,
            collected=collected,
            error=error,
            detail=detail or {},
            git_revision=git_revision,
            artifact_ids=artifact_ids,
        )

    return build


@pytest.fixture
def make_bundle(make_record: Callable[..., EvidenceRecord]) -> Callable[..., EvidenceBundle]:
    """Build an evidence bundle from a mapping of slot to detail payload."""

    def build(
        records: dict[str, dict[str, Any]] | None = None,
        *,
        as_of: datetime | None = None,
        git_revision: str | None = REVISION,
        working_tree_clean: bool | None = True,
        raw: dict[str, EvidenceRecord] | None = None,
    ) -> EvidenceBundle:
        bundle = EvidenceBundle(
            as_of=as_of or NOW,
            git_revision=git_revision,
            working_tree_clean=working_tree_clean,
        )
        for slot, detail in (records or {}).items():
            bundle = bundle.with_record(
                slot, make_record(source=slot, detail=detail, git_revision=git_revision)
            )
        for slot, record in (raw or {}).items():
            bundle = bundle.with_record(slot, record)
        return bundle

    return build


@pytest.fixture
def passing_evidence() -> dict[str, dict[str, Any]]:
    """A detail payload per slot that satisfies every criterion.

    The starting point for "everything is fine, now break exactly one thing"
    tests — which is the only way to assert that a specific criterion is what
    holds a level shut.
    """
    ok = {"exit_code": 0, "passed": 1, "failed": 0}
    return {
        "test_suite": dict(ok),
        "lint": dict(ok),
        "format": dict(ok),
        "typecheck": dict(ok),
        "execution_safety": {**ok, "orders_submitted": 0},
        "position_lifecycle": dict(ok),
        "exit_management": dict(ok),
        "pnl": {**ok, "settlement_idempotent": True},
        "agents": dict(ok),
        "agent_boundaries": dict(ok),
        "data": dict(ok),
        "privacy": dict(ok),
        "configuration": {
            "config_loaded": True,
            "trading_mode": "PAPER",
            "live_trading_confirmed": False,
            "live_readiness_checklist_signed_off": False,
            "execution_enabled": False,
            "execution_allow_live": False,
            "require_explicit_authorization": True,
            "ibkr_read_only": True,
        },
        "test_isolation": {
            "suite_is_hermetic": True,
            "live_suites_gated": True,
            "paper_execution_double_gated": True,
        },
        "broker": {
            "connected": True,
            "broker": "IBKR",
            "trading_mode": "PAPER",
            "account_status": "OK",
            "positions_status": "OK",
            "orders_status": "EMPTY",
            "executions_status": "EMPTY",
        },
        "reconciliation": {
            "status": "MATCH",
            "critical_findings": 0,
            "unknown_executions": 0,
        },
        "daily_loss": {"daily_pnl_status": "TRACKED"},
        "scheduler": {
            "scheduler_ran": True,
            "failed_jobs": 0,
            "unknown_jobs": 0,
            "skipped_jobs": 2,
        },
        "observability_stack": {
            "services": {
                "otel-collector": True,
                "tempo": True,
                "prometheus": True,
                "loki": True,
                "grafana": True,
            }
        },
        "collector": {"spans_accepted": 4},
        "tempo": {"trace_found": True},
        "prometheus": {"metric_found": True},
        "loki": {"log_found": True},
        "grafana": {
            "grafana_healthy": True,
            "missing_datasources": [],
            "datasources": ["tempo", "loki", "prometheus"],
            "missing_dashboards": [],
            "dashboards": ["trading-system-overview"],
        },
        "correlation": {"trace_id": "abc", "trace_found": True, "log_found": True},
        "cardinality": {"forbidden_labels_found": [], "guarded_labels": 16},
        "secrets": {"tracked_secret_files": [], "dotenv_ignored": True},
        "masking": {"account_identifiers_masked": True, "files_scanned": 3},
        "operational_history": {"shortfalls": [], "readiness_runs": 9, "days": 5},
        "git": {"git_revision": REVISION, "working_tree_clean": True, "changed_files": 0},
    }


@pytest.fixture
def make_criterion() -> Callable[..., ReadinessCriterion]:
    """Build one assessed criterion directly, for model-level tests."""

    def build(
        *,
        criterion_id: ReadinessCriterionId = ReadinessCriterionId.TEST_SUITE_PASSES,
        status: ReadinessStatus = ReadinessStatus.PASS,
        reason_code: ReadinessReasonCode | None = None,
        blocking_for: tuple[ReadinessLevel, ...] = (ReadinessLevel.READY_FOR_PAPER,),
        evidence_id: str | None = "evidence-test",
        domain: ReadinessDomain = ReadinessDomain.SOFTWARE_QUALITY,
        detail: str = "built by a test",
    ) -> ReadinessCriterion:
        resolved_reason = reason_code or (
            ReadinessReasonCode.SATISFIED
            if status is ReadinessStatus.PASS
            else ReadinessReasonCode.NO_EVIDENCE
        )
        return ReadinessCriterion(
            criterion_id=criterion_id,
            domain=domain,
            title="a test criterion",
            status=status,
            reason_code=resolved_reason,
            detail=detail,
            blocking_for=blocking_for,
            evidence_id=(None if status is ReadinessStatus.NOT_TESTED else evidence_id),
        )

    return build
