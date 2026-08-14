"""Persistence for job runs, scheduler ticks, health reports and alerts.

The same discipline as every other store in this system, with one deliberate
difference worth reading.

**A job run is written twice, and that is not a mutation.** Everything else in
this system is immutable-once-written; a job run is written as ``RUNNING``
before the work starts and completed when it ends, under the same id. The
alternative — writing only on completion — is precisely the failure mode the
brief calls out: a process that dies mid-job would leave *silence*, and the
next start would have no way to tell "never ran" from "ran and we never found
out". The first write is what turns that into a question the scheduler can
answer, and the completion is recorded as an appended history line as well, so
the transition itself survives.

Everything else holds: atomic writes through a temporary file, append-only
indexes that are never rewritten, and content-derived ids so a replayed firing
is recognised rather than duplicated.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_system.domain.enums import JobStatus
from trading_system.operations.models import (
    Alert,
    JobRun,
    OperationalHealth,
    SchedulerRun,
)

__all__ = [
    "FilesystemOperationsRepository",
    "JobRunEntry",
    "OperationsRepository",
    "OperationsStoreError",
]

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class OperationsStoreError(RuntimeError):
    """An operational artifact could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class JobRunEntry:
    """One line of the append-only job index.

    Carries the status so ``ops jobs`` can answer "what failed today" with a
    scan rather than by opening every record — and so a stale ``RUNNING`` line
    is visible without loading anything.
    """

    job_run_id: str
    job: str
    scheduled_for: datetime
    started_at: datetime
    finished_at: datetime | None
    status: str
    skip_reason: str | None
    error_type: str | None
    orders_submitted: int

    def to_json(self) -> dict[str, Any]:
        return {
            "job_run_id": self.job_run_id,
            "job": self.job,
            "scheduled_for": self.scheduled_for.astimezone(UTC).isoformat(),
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "finished_at": (
                None if self.finished_at is None else self.finished_at.astimezone(UTC).isoformat()
            ),
            "status": self.status,
            "skip_reason": self.skip_reason,
            "error_type": self.error_type,
            "orders_submitted": self.orders_submitted,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> JobRunEntry:
        finished = payload.get("finished_at")
        return cls(
            job_run_id=str(payload["job_run_id"]),
            job=str(payload["job"]),
            scheduled_for=datetime.fromisoformat(str(payload["scheduled_for"])),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            finished_at=None if finished is None else datetime.fromisoformat(str(finished)),
            status=str(payload["status"]),
            skip_reason=(
                None if payload.get("skip_reason") is None else str(payload["skip_reason"])
            ),
            error_type=None if payload.get("error_type") is None else str(payload["error_type"]),
            orders_submitted=int(payload.get("orders_submitted", 0)),
        )

    @classmethod
    def of(cls, run: JobRun) -> JobRunEntry:
        return cls(
            job_run_id=run.job_run_id,
            job=run.job,
            scheduled_for=run.scheduled_for,
            started_at=run.started_at,
            finished_at=run.finished_at,
            status=run.status.value,
            skip_reason=None if run.skip_reason is None else run.skip_reason.value,
            error_type=run.error_type,
            orders_submitted=run.orders_submitted,
        )


class OperationsRepository(ABC):
    """What an operational store has to be able to do."""

    @abstractmethod
    def save_job_run(self, run: JobRun) -> str:
        """Store or complete one job run. Safe to call twice with the same id."""

    @abstractmethod
    def job_run(self, job_run_id: str) -> JobRun | None: ...

    @abstractmethod
    def job_runs(self, limit: int | None = None, *, job: str | None = None) -> list[JobRun]: ...

    @abstractmethod
    def job_history(self, limit: int | None = None) -> list[JobRunEntry]: ...

    @abstractmethod
    def unfinished_job_runs(self) -> list[JobRun]:
        """Runs still recorded as ``RUNNING``. The restart-safety question."""

    @abstractmethod
    def save_scheduler_run(self, run: SchedulerRun) -> str: ...

    @abstractmethod
    def scheduler_runs(self, limit: int | None = None) -> list[SchedulerRun]: ...

    @abstractmethod
    def latest_scheduler_run(self) -> SchedulerRun | None: ...

    @abstractmethod
    def save_health(self, report: OperationalHealth) -> str: ...

    @abstractmethod
    def latest_health(self) -> OperationalHealth | None: ...

    @abstractmethod
    def save_alert(self, alert: Alert) -> tuple[str, bool]:
        """Store one alert. Returns its id and whether it was new."""

    @abstractmethod
    def alerts(self, limit: int | None = None) -> list[Alert]: ...

    @abstractmethod
    def alert(self, alert_id: str) -> Alert | None: ...


class FilesystemOperationsRepository(OperationsRepository):
    """JSON-on-disk store, rooted at ``<data>/operations/``.

    ::

        operations/
          jobs/<day>/<job_run_id>.json
          ticks/<day>/<scheduler_run_id>.json
          health/<day>/<health_id>.json
          alerts/<day>/<alert_id>.json
          jobs.jsonl
          ticks.jsonl
          health.jsonl
          alerts.jsonl
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def jobs_dir(self) -> Path:
        return self._root / "jobs"

    @property
    def ticks_dir(self) -> Path:
        return self._root / "ticks"

    @property
    def health_dir(self) -> Path:
        return self._root / "health"

    @property
    def alerts_dir(self) -> Path:
        return self._root / "alerts"

    # --- job runs ----------------------------------------------------------
    def save_job_run(self, run: JobRun) -> str:
        """Write or complete one job run.

        Overwriting is permitted *only* to complete a run that is currently
        ``RUNNING``. Rewriting a finished run is refused: a job that recorded
        FAILED and later reads SUCCESS would erase the evidence somebody needs.
        """
        path = self._job_path(run)
        if path.is_file():
            existing = JobRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if existing.status is not JobStatus.RUNNING and run.status is not existing.status:
                raise OperationsStoreError(
                    f"job run {run.job_run_id} is already recorded as {existing.status.value} "
                    f"and cannot be rewritten as {run.status.value}. A completed run is "
                    f"evidence; the next firing gets its own record."
                )
        _atomic_write(path, run.model_dump(mode="json"))
        _append_line(self._root / "jobs.jsonl", JobRunEntry.of(run).to_json())
        return run.job_run_id

    def job_run(self, job_run_id: str) -> JobRun | None:
        for entry in self.job_history():
            if entry.job_run_id == job_run_id:
                return self._load_job(entry)
        return None

    def job_runs(self, limit: int | None = None, *, job: str | None = None) -> list[JobRun]:
        seen: set[str] = set()
        runs: list[JobRun] = []
        for entry in self.job_history():
            if job is not None and entry.job != job:
                continue
            if entry.job_run_id in seen:
                continue
            seen.add(entry.job_run_id)
            record = self._load_job(entry)
            if record is not None:
                runs.append(record)
            if limit is not None and len(runs) >= limit:
                break
        return runs

    def job_history(self, limit: int | None = None) -> list[JobRunEntry]:
        entries = [
            JobRunEntry.from_json(payload)
            for payload in _read_lines(self._root / "jobs.jsonl", "job history")
        ]
        entries.sort(key=lambda e: (e.started_at, e.job_run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    def unfinished_job_runs(self) -> list[JobRun]:
        """Every run still on disk as ``RUNNING``.

        The question a restarting scheduler asks first. A run here is not a
        failure and not a success — it is a job whose completion was never
        recorded, and the scheduler reclassifies it as ``UNKNOWN`` rather than
        assuming either.
        """
        return [run for run in self.job_runs() if run.status is JobStatus.RUNNING]

    # --- ticks -------------------------------------------------------------
    def save_scheduler_run(self, run: SchedulerRun) -> str:
        path = (
            self.ticks_dir
            / run.scheduled_for.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(run.scheduler_run_id)}.json"
        )
        _atomic_write(path, run.model_dump(mode="json"))
        _append_line(
            self._root / "ticks.jsonl",
            {
                "scheduler_run_id": run.scheduler_run_id,
                "scheduled_for": run.scheduled_for.astimezone(UTC).isoformat(),
                "started_at": run.started_at.astimezone(UTC).isoformat(),
                "status": run.status.value,
                "jobs": len(run.runs),
                "orders_submitted": run.orders_submitted,
            },
        )
        return run.scheduler_run_id

    def scheduler_runs(self, limit: int | None = None) -> list[SchedulerRun]:
        payloads = _read_lines(self._root / "ticks.jsonl", "scheduler history")
        payloads.sort(key=lambda payload: str(payload.get("started_at", "")), reverse=True)
        seen: set[str] = set()
        runs: list[SchedulerRun] = []
        for payload in payloads:
            identifier = str(payload["scheduler_run_id"])
            if identifier in seen:
                continue
            seen.add(identifier)
            path = (
                self.ticks_dir
                / datetime.fromisoformat(str(payload["scheduled_for"]))
                .astimezone(UTC)
                .strftime("%Y-%m-%d")
                / f"{_safe(identifier)}.json"
            )
            if path.is_file():
                runs.append(
                    SchedulerRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
                )
            if limit is not None and len(runs) >= limit:
                break
        return runs

    def latest_scheduler_run(self) -> SchedulerRun | None:
        runs = self.scheduler_runs(limit=1)
        return runs[0] if runs else None

    # --- health ------------------------------------------------------------
    def save_health(self, report: OperationalHealth) -> str:
        path = (
            self.health_dir
            / report.as_of.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(report.health_id)}.json"
        )
        _atomic_write(path, report.model_dump(mode="json"))
        _append_line(
            self._root / "health.jsonl",
            {
                "health_id": report.health_id,
                "as_of": report.as_of.astimezone(UTC).isoformat(),
                "trading_status": report.trading_status.value,
                "observability_status": report.observability_status.value,
            },
        )
        return report.health_id

    def latest_health(self) -> OperationalHealth | None:
        payloads = _read_lines(self._root / "health.jsonl", "health history")
        if not payloads:
            return None
        newest = max(payloads, key=lambda payload: str(payload.get("as_of", "")))
        as_of = datetime.fromisoformat(str(newest["as_of"]))
        path = (
            self.health_dir
            / as_of.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(str(newest['health_id']))}.json"
        )
        if not path.is_file():
            return None
        return OperationalHealth.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # --- alerts ------------------------------------------------------------
    def save_alert(self, alert: Alert) -> tuple[str, bool]:
        path = (
            self.alerts_dir
            / alert.raised_at.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(alert.alert_id)}.json"
        )
        if path.is_file():
            # A condition that is still true on the next tick derives the same
            # id and is the same alert, not a new one. Re-firing an unresolved
            # condition every five minutes is how a channel becomes noise.
            return alert.alert_id, False
        _atomic_write(path, alert.model_dump(mode="json"))
        _append_line(
            self._root / "alerts.jsonl",
            {
                "alert_id": alert.alert_id,
                "code": alert.code.value,
                "category": alert.category.value,
                "severity": alert.severity.value,
                "subject": alert.subject,
                "raised_at": alert.raised_at.astimezone(UTC).isoformat(),
                "occurrences": alert.occurrences,
                "notified_channels": alert.notified_channels,
            },
        )
        return alert.alert_id, True

    def alerts(self, limit: int | None = None) -> list[Alert]:
        payloads = _read_lines(self._root / "alerts.jsonl", "alert history")
        payloads.sort(key=lambda payload: str(payload.get("raised_at", "")), reverse=True)
        seen: set[str] = set()
        records: list[Alert] = []
        for payload in payloads:
            identifier = str(payload["alert_id"])
            if identifier in seen:
                continue
            seen.add(identifier)
            record = self._load_alert(datetime.fromisoformat(str(payload["raised_at"])), identifier)
            if record is not None:
                records.append(record)
            if limit is not None and len(records) >= limit:
                break
        return records

    def alert(self, alert_id: str) -> Alert | None:
        for record in self.alerts():
            if record.alert_id == alert_id:
                return record
        return None

    # --- internals ---------------------------------------------------------
    def _job_path(self, run: JobRun) -> Path:
        bucket = run.scheduled_for.astimezone(UTC).strftime("%Y-%m-%d")
        return self.jobs_dir / bucket / f"{_safe(run.job_run_id)}.json"

    def _load_job(self, entry: JobRunEntry) -> JobRun | None:
        bucket = entry.scheduled_for.astimezone(UTC).strftime("%Y-%m-%d")
        path = self.jobs_dir / bucket / f"{_safe(entry.job_run_id)}.json"
        if not path.is_file():
            return None
        return JobRun.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _load_alert(self, raised_at: datetime, alert_id: str) -> Alert | None:
        path = (
            self.alerts_dir
            / raised_at.astimezone(UTC).strftime("%Y-%m-%d")
            / f"{_safe(alert_id)}.json"
        )
        if not path.is_file():
            return None
        return Alert.model_validate(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Filesystem primitives
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    return _SAFE.sub("_", name)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write through a temporary file, then rename. Never a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:  # pragma: no cover - defensive
        Path(temporary).unlink(missing_ok=True)
        raise


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


def _read_lines(path: Path, what: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payloads: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise OperationsStoreError(f"{what} line {number} in {path} is not valid JSON") from exc
    return payloads
