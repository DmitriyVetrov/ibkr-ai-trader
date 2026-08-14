"""Fixtures for the Milestone 11 operations suites.

Three rules hold across every test here:

* **No network, no broker, no model.** The scheduler is constructed with a
  store, a configuration and an injected clock. Jobs are replaced with fakes
  that record what they were asked to do; the ones that are not replaced are
  never reached, because nothing in these tests is due.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``.
* **Time is a fixture.** Everything is anchored at a Monday inside the New York
  session, in a year the shipped market calendar has actually verified — so
  "the market is open" is a fact rather than a hope, and a test about market
  hours moves the clock rather than skipping on a weekend.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.operations.jobs import JobContext, JobOutcome
from trading_system.operations.scheduler import Scheduler
from trading_system.operations.store import FilesystemOperationsRepository

#: A Monday at 14:35 UTC — 10:35 in New York, inside the session, on a minute
#: that a ``*/5`` cadence fires on.
NOW = datetime(2026, 8, 10, 14, 35, tzinfo=UTC)
#: The same Monday at 02:00 UTC: outside the session, so a market-hours job
#: skips and a round-the-clock one does not.
BEFORE_OPEN = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
#: A Saturday.
WEEKEND = datetime(2026, 8, 15, 14, 35, tzinfo=UTC)


@pytest.fixture
def ops_now() -> datetime:
    return NOW


@pytest.fixture
def ops_clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def ops_settings() -> Settings:
    return Settings(_env_file=None, trading_mode="PAPER")


@pytest.fixture
def ops_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def ops_repository(ops_root: Path) -> FilesystemOperationsRepository:
    return FilesystemOperationsRepository(ops_root / "operations")


@pytest.fixture
def recording_job() -> Callable[..., object]:
    """A job that records its calls instead of doing anything.

    Returned as a factory so a test can decide what the job does — succeed,
    raise, skip, hang — without three near-identical fixtures.
    """

    class _Recorder:
        def __init__(
            self,
            *,
            outcome: JobOutcome | None = None,
            raises: BaseException | None = None,
            blocks: float | None = None,
        ) -> None:
            self.calls: list[JobContext] = []
            self._outcome = outcome or JobOutcome(summary="did the thing")
            self._raises = raises
            self._blocks = blocks

        def __call__(self, context: JobContext) -> JobOutcome:
            self.calls.append(context)
            if self._blocks is not None:
                import time

                time.sleep(self._blocks)
            if self._raises is not None:
                raise self._raises
            return self._outcome

        @property
        def call_count(self) -> int:
            return len(self.calls)

    return _Recorder


@pytest.fixture
def build_scheduler(
    ops_settings: Settings,
    ops_clock: FixedClock,
    ops_repository: FilesystemOperationsRepository,
    ops_root: Path,
    system_config: SystemConfig,
) -> Callable[..., Scheduler]:
    """A scheduler wired at ``tmp_path``, with the shipped cadences.

    ``jobs`` replaces the registered implementations after construction, which
    is what lets a test assert *that* the scheduler invoked something without
    standing up a broker, a research pipeline or a position ledger. The
    scheduler's own logic — cron, calendar, isolation, persistence — is the
    thing under test and is never replaced.
    """

    def build(
        *,
        jobs: dict[str, object] | None = None,
        config: SystemConfig | None = None,
        clock: FixedClock | None = None,
        repository: FilesystemOperationsRepository | None = None,
    ) -> Scheduler:
        from dataclasses import replace

        scheduler = Scheduler(
            settings=ops_settings,
            config=config or system_config,
            clock=clock or ops_clock,
            repository=repository or ops_repository,
            root=ops_root.parent,
        )
        for name, implementation in (jobs or {}).items():
            definition = scheduler.registry[name]
            scheduler.registry[name] = replace(definition, run=implementation)  # type: ignore[arg-type]
        return scheduler

    return build


@pytest.fixture
def enabled_config(system_config: SystemConfig) -> SystemConfig:
    """The shipped configuration with every job enabled and no market gate.

    Tests about *cadence* should not also be tests about which jobs happen to
    ship enabled; the ones that care about the shipped defaults assert them
    explicitly against ``system_config``.
    """
    jobs = {
        name: job.model_copy(update={"enabled": True, "market_hours_only": False})
        for name, job in system_config.schedules.jobs.items()
    }
    return system_config.model_copy(
        update={"schedules": system_config.schedules.model_copy(update={"jobs": jobs})}
    )


@pytest.fixture(autouse=True)
def _no_broker_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything in these tests reaches for a broker connection.

    A test that reaches the real gateway is a bug in the test (and, here,
    usually a bug in the fixture that forgot to replace a job). Without this
    guard the failure is silent and slow: the connection attempt times out,
    the job records a failure, and the assertion still passes for the wrong
    reason.
    """

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "an operations test tried to construct a broker. Replace the job with a "
            "recording fake, or supply the simulator explicitly — the scheduler holds no "
            "broker and its tests must not open one either."
        )

    monkeypatch.setattr("trading_system.broker.factory.build_broker", refuse)
