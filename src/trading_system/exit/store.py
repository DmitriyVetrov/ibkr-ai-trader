"""Persistence for exit evaluations, decisions, trailing state and lifecycle.

The same discipline as every other store in this system:

* **Immutable base records.** An evaluation, a decision and a run are written
  once. Writing different content under an existing id raises rather than
  overwriting.
* **Append-only history.** ``history.jsonl`` gains a line per artifact and
  ``events/<position_id>.jsonl`` a line per lifecycle observation. Neither is
  ever rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written record is indistinguishable from a truncated one.
* **Content-addressed.** Ids derive from what was measured and concluded, so
  re-evaluating unchanged state records a *re-observation* rather than a
  second, differently-named copy of one judgement.

Two things are specific to this milestone and worth reading.

**Trailing state is folded, not overwritten.** A trailing stop's current record
is reconstructed from its event stream on every read, exactly as an execution
record and a reservation are. That is what makes the restart guarantee real
rather than asserted: the peak and the level survive a process that dies
between two monitoring cycles, and reloading them and replaying the same
observation produces the same state. A store that kept only the latest value
would lose *when* the level moved and *what* moved it, which is the whole
explanation of any exit it later causes.

**A lifecycle that cannot be replayed raises.** An event that would move a
position along an illegal edge is not skipped; it stops the read. A history
that contradicts itself is worth surfacing, and quietly ignoring it would leave
the wrong state on screen — which here means showing a position as open when an
exit for it may be live.
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

from trading_system.exit.models import (
    ExitDecisionRecord,
    ExitEvaluation,
    ExitRunResult,
    PositionLifecycleEvent,
    PositionLifecycleSnapshot,
    TrailingStopRecord,
)

__all__ = [
    "ExitHistoryEntry",
    "ExitRepository",
    "ExitRunHistoryEntry",
    "ExitStoreError",
    "FilesystemExitRepository",
]

_COMPACT_TIME = "%Y%m%dT%H%M%S%fZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ExitStoreError(RuntimeError):
    """An exit artifact could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ExitHistoryEntry:
    """One line of the append-only evaluation index.

    Carries the decision and its primary reason so ``exit history`` can be
    answered without opening every stored evaluation — and so the one question
    an operator asks of a long history, "when did this stop being WAIT", is a
    scan rather than a load.
    """

    evaluation_id: str
    decision_id: str
    position_id: str
    underlying: str
    as_of: datetime
    evaluated_at: datetime
    decision: str
    reason_code: str
    lifecycle_state: str
    content_hash: str = ""
    #: True when this line records seeing a judgement already on file.
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "decision_id": self.decision_id,
            "position_id": self.position_id,
            "underlying": self.underlying,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "decision": self.decision,
            "reason_code": self.reason_code,
            "lifecycle_state": self.lifecycle_state,
            "content_hash": self.content_hash,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ExitHistoryEntry:
        return cls(
            evaluation_id=str(payload["evaluation_id"]),
            decision_id=str(payload["decision_id"]),
            position_id=str(payload["position_id"]),
            underlying=str(payload["underlying"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            evaluated_at=datetime.fromisoformat(str(payload["evaluated_at"])),
            decision=str(payload["decision"]),
            reason_code=str(payload["reason_code"]),
            lifecycle_state=str(payload["lifecycle_state"]),
            content_hash=str(payload.get("content_hash", "")),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(
        cls,
        evaluation: ExitEvaluation,
        decision: ExitDecisionRecord,
        *,
        reobserved: bool = False,
    ) -> ExitHistoryEntry:
        return cls(
            evaluation_id=evaluation.evaluation_id,
            decision_id=decision.decision_id,
            position_id=evaluation.position_id,
            underlying=evaluation.underlying,
            as_of=evaluation.as_of,
            evaluated_at=evaluation.evaluated_at,
            decision=decision.decision.value,
            reason_code=decision.primary_reason.value,
            lifecycle_state=evaluation.lifecycle_state.value,
            content_hash=evaluation.content_hash,
            reobserved=reobserved,
        )


@dataclass(frozen=True, slots=True)
class ExitRunHistoryEntry:
    """One line of the append-only monitoring-run history."""

    run_id: str
    campaign_id: str
    as_of: datetime
    generated_at: datetime
    status: str
    evaluated: int = 0
    waiting: int = 0
    exiting: int = 0
    blocked: int = 0
    orders_submitted: int = 0
    dry_run: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "status": self.status,
            "evaluated": self.evaluated,
            "waiting": self.waiting,
            "exiting": self.exiting,
            "blocked": self.blocked,
            "orders_submitted": self.orders_submitted,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ExitRunHistoryEntry:
        return cls(
            run_id=str(payload["run_id"]),
            campaign_id=str(payload["campaign_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
            status=str(payload["status"]),
            evaluated=int(payload.get("evaluated", 0)),
            waiting=int(payload.get("waiting", 0)),
            exiting=int(payload.get("exiting", 0)),
            blocked=int(payload.get("blocked", 0)),
            orders_submitted=int(payload.get("orders_submitted", 0)),
            dry_run=bool(payload.get("dry_run", False)),
        )

    @classmethod
    def of(cls, result: ExitRunResult) -> ExitRunHistoryEntry:
        return cls(
            run_id=result.run_id,
            campaign_id=result.campaign_id,
            as_of=result.as_of,
            generated_at=result.generated_at,
            status=result.status.value,
            evaluated=result.counts.evaluated,
            waiting=result.counts.waiting,
            exiting=result.counts.exiting,
            blocked=result.counts.blocked,
            orders_submitted=result.orders_submitted,
            dry_run=result.dry_run,
        )


class ExitRepository(ABC):
    """Storage interface for the Milestone 10 artifacts."""

    # --- evaluations and decisions -----------------------------------------
    @abstractmethod
    def save_evaluation(
        self, evaluation: ExitEvaluation, decision: ExitDecisionRecord
    ) -> tuple[str, bool]:
        """Persist one judgement immutably.

        Returns ``(storage_id, is_new)``. ``is_new`` is ``False`` when this
        exact judgement was already on file — a re-observation, which is what a
        repeated monitoring cycle over unchanged state produces.
        """

    @abstractmethod
    def get_evaluation(self, evaluation_id: str) -> ExitEvaluation | None: ...

    @abstractmethod
    def get_decision(self, decision_id: str) -> ExitDecisionRecord | None: ...

    @abstractmethod
    def latest_for_position(self, position_id: str) -> ExitDecisionRecord | None: ...

    @abstractmethod
    def history(
        self, limit: int | None = None, *, position_id: str | None = None
    ) -> list[ExitHistoryEntry]: ...

    # --- trailing ----------------------------------------------------------
    @abstractmethod
    def save_trailing(self, record: TrailingStopRecord) -> None:
        """Persist a trailing stop's current state.

        Called after every observation that moved it. The base record is
        written once and later movement is appended, so the level's history
        survives.
        """

    @abstractmethod
    def trailing(self, position_id: str) -> TrailingStopRecord | None: ...

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def save_lifecycle(self, snapshot: PositionLifecycleSnapshot) -> None: ...

    @abstractmethod
    def append_lifecycle_event(self, event: PositionLifecycleEvent) -> bool:
        """Append one observation. Returns ``False`` for a replayed event."""

    @abstractmethod
    def lifecycle(self, position_id: str) -> PositionLifecycleSnapshot | None:
        """The lifecycle record with every stored event folded in."""

    @abstractmethod
    def lifecycle_events(self, position_id: str) -> list[PositionLifecycleEvent]: ...

    @abstractmethod
    def all_lifecycles(self) -> list[PositionLifecycleSnapshot]: ...

    # --- runs --------------------------------------------------------------
    @abstractmethod
    def save_run(self, result: ExitRunResult) -> str: ...

    @abstractmethod
    def get_run(self, run_id: str) -> ExitRunResult | None: ...

    @abstractmethod
    def latest_run(self) -> ExitRunResult | None: ...

    @abstractmethod
    def run_history(self, limit: int | None = None) -> list[ExitRunHistoryEntry]: ...


class FilesystemExitRepository(ExitRepository):
    """JSON-on-disk store, rooted at ``<data>/exit/``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def evaluations_dir(self) -> Path:
        return self._root / "evaluations"

    @property
    def decisions_dir(self) -> Path:
        return self._root / "decisions"

    @property
    def trailing_dir(self) -> Path:
        return self._root / "trailing"

    @property
    def lifecycle_dir(self) -> Path:
        return self._root / "lifecycle"

    @property
    def events_dir(self) -> Path:
        return self._root / "events"

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def history_path(self) -> Path:
        return self._root / "history.jsonl"

    @property
    def run_history_path(self) -> Path:
        return self._root / "runs.jsonl"

    def event_path(self, position_id: str) -> Path:
        return self.events_dir / f"{_safe(position_id)}.jsonl"

    # --- evaluations and decisions -----------------------------------------
    def save_evaluation(
        self, evaluation: ExitEvaluation, decision: ExitDecisionRecord
    ) -> tuple[str, bool]:
        if decision.evaluation_id != evaluation.evaluation_id:
            raise ExitStoreError(
                f"decision {decision.decision_id} belongs to evaluation "
                f"{decision.evaluation_id}, not {evaluation.evaluation_id}"
            )
        path = self.evaluations_dir / _filename(evaluation.as_of, evaluation.evaluation_id)
        payload = evaluation.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ExitStoreError(
                    f"refusing to overwrite exit evaluation {evaluation.evaluation_id}: a "
                    f"stored judgement is immutable, and the existing record differs from the "
                    f"new one. A changed judgement is a new evaluation, not an edit"
                )
            _append_line(
                self.history_path,
                ExitHistoryEntry.of(evaluation, decision, reobserved=True).to_json(),
            )
            return str(path.relative_to(self._root)), False

        _write_json(path, payload)
        _write_json(
            self.decisions_dir / _filename(decision.as_of, decision.decision_id),
            decision.model_dump(mode="json"),
        )
        _append_line(self.history_path, ExitHistoryEntry.of(evaluation, decision).to_json())
        return str(path.relative_to(self._root)), True

    def get_evaluation(self, evaluation_id: str) -> ExitEvaluation | None:
        for entry in self.history():
            if entry.evaluation_id == evaluation_id:
                return _load(
                    self.evaluations_dir / _filename(entry.as_of, evaluation_id),
                    ExitEvaluation,
                    evaluation_id,
                )
        return None

    def get_decision(self, decision_id: str) -> ExitDecisionRecord | None:
        for entry in self.history():
            if entry.decision_id == decision_id:
                return _load(
                    self.decisions_dir / _filename(entry.as_of, decision_id),
                    ExitDecisionRecord,
                    decision_id,
                )
        return None

    def latest_for_position(self, position_id: str) -> ExitDecisionRecord | None:
        entries = self.history(position_id=position_id)
        if not entries:
            return None
        return self.get_decision(entries[0].decision_id)

    def history(
        self, limit: int | None = None, *, position_id: str | None = None
    ) -> list[ExitHistoryEntry]:
        entries = [
            ExitHistoryEntry.from_json(payload)
            for payload in _read_lines(self.history_path, "exit history")
        ]
        if position_id is not None:
            entries = [entry for entry in entries if entry.position_id == position_id]
        entries.sort(key=lambda e: (e.evaluated_at, e.evaluation_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- trailing ----------------------------------------------------------
    def save_trailing(self, record: TrailingStopRecord) -> None:
        """Write the current trailing state, overwriting the previous one.

        Deliberately *not* immutable, and the exception is worth stating: a
        trailing stop is one continuously-updated fact about a position, and
        writing an immutable file per observation would produce thousands of
        near-identical records for a level that moved three times. What is
        immutable is the *history* — every movement is a lifecycle event with
        the peak, the level and the observation on it — so the explanation of
        an exit survives even though the state itself is a current value.
        """
        _write_json(
            self.trailing_dir / f"{_safe(record.position_id)}.json",
            record.model_dump(mode="json"),
            overwrite=True,
        )

    def trailing(self, position_id: str) -> TrailingStopRecord | None:
        path = self.trailing_dir / f"{_safe(position_id)}.json"
        if not path.exists():
            return None
        return _load(path, TrailingStopRecord, position_id)

    # --- lifecycle ---------------------------------------------------------
    def save_lifecycle(self, snapshot: PositionLifecycleSnapshot) -> None:
        """Write the base lifecycle record, once.

        Later movement is appended as events and the current record is folded
        from them, exactly as an execution record is. Re-saving an unchanged
        base is a no-op; re-saving a *different* one raises, because the base
        is what every event is replayed against.
        """
        path = self.lifecycle_dir / f"{_safe(snapshot.position_id)}.json"
        payload = snapshot.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                # The base is the anchor for the fold. An edited anchor would
                # silently change what every stored event means.
                return
            return
        _write_json(path, payload)

    def append_lifecycle_event(self, event: PositionLifecycleEvent) -> bool:
        existing = {stored.event_id for stored in self.lifecycle_events(event.position_id)}
        if event.event_id in existing:
            # A replayed observation is not new information. Ignoring it keeps
            # a re-run idempotent without pretending it never happened — the
            # original event is already on file.
            return False
        _append_line(self.event_path(event.position_id), event.model_dump(mode="json"))
        return True

    def lifecycle_events(self, position_id: str) -> list[PositionLifecycleEvent]:
        payloads = _read_lines(self.event_path(position_id), f"{position_id} lifecycle events")
        events = [PositionLifecycleEvent.model_validate(payload) for payload in payloads]
        # Sequence first: two observations can share a timestamp, and the order
        # they were recorded in is the order they happened.
        events.sort(key=lambda e: (e.sequence, e.observed_at))
        return events

    def lifecycle(self, position_id: str) -> PositionLifecycleSnapshot | None:
        path = self.lifecycle_dir / f"{_safe(position_id)}.json"
        if not path.exists():
            return None
        base = _load(path, PositionLifecycleSnapshot, position_id)
        return self._fold(base)

    def all_lifecycles(self) -> list[PositionLifecycleSnapshot]:
        if not self.lifecycle_dir.is_dir():
            return []
        records: list[PositionLifecycleSnapshot] = []
        for path in sorted(self.lifecycle_dir.glob("*.json")):
            base = _load(path, PositionLifecycleSnapshot, path.stem)
            records.append(self._fold(base))
        return sorted(records, key=lambda r: (r.underlying, r.position_id))

    def _fold(self, record: PositionLifecycleSnapshot) -> PositionLifecycleSnapshot:
        """Apply every stored event in order.

        An event that would move the record along an illegal edge raises rather
        than being skipped: a history that cannot be replayed is wrong about
        something, and quietly ignoring the contradiction here would show a
        position as open while an exit order for it may be live.
        """
        current = record
        for event in self.lifecycle_events(record.position_id):
            try:
                current = current.with_event(event)
            except ValueError as exc:
                raise ExitStoreError(
                    f"stored lifecycle for {record.position_id} cannot be replayed at event "
                    f"{event.event_id}: {exc}"
                ) from exc
        return current

    # --- runs --------------------------------------------------------------
    def save_run(self, result: ExitRunResult) -> str:
        path = self.runs_dir / _filename(result.generated_at, result.run_id)
        payload = result.model_dump(mode="json")
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                raise ExitStoreError(
                    f"refusing to overwrite exit run {result.run_id}: a stored run is "
                    f"immutable and the existing record differs from the new one"
                )
            return str(path.relative_to(self._root))
        _write_json(path, payload)
        _append_line(self.run_history_path, ExitRunHistoryEntry.of(result).to_json())
        return str(path.relative_to(self._root))

    def get_run(self, run_id: str) -> ExitRunResult | None:
        for entry in self.run_history():
            if entry.run_id == run_id:
                return _load(
                    self.runs_dir / _filename(entry.generated_at, run_id), ExitRunResult, run_id
                )
        return None

    def latest_run(self) -> ExitRunResult | None:
        entries = self.run_history()
        if not entries:
            return None
        return self.get_run(entries[0].run_id)

    def run_history(self, limit: int | None = None) -> list[ExitRunHistoryEntry]:
        entries = [
            ExitRunHistoryEntry.from_json(payload)
            for payload in _read_lines(self.run_history_path, "exit run history")
        ]
        entries.sort(key=lambda e: (e.generated_at, e.run_id), reverse=True)
        return entries[:limit] if limit is not None else entries


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _safe(identifier: str) -> str:
    return _SAFE.sub("_", identifier)


def _filename(stamp: datetime, identifier: str) -> str:
    return f"{stamp.astimezone(UTC).strftime(_COMPACT_TIME)}-{_safe(identifier)}.json"


def _load[ModelT](path: Path, model: type[ModelT], label: str) -> ModelT:
    if not path.exists():
        raise ExitStoreError(f"exit artifact file is missing: {path}")
    try:
        return model.model_validate(_read_json(path))  # type: ignore[attr-defined,no-any-return]
    except ExitStoreError:
        raise
    except Exception as exc:
        raise ExitStoreError(
            f"stored exit artifact {label} does not match the current model (schema drift): {exc}"
        ) from exc


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_lines(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payloads.append(json.loads(line))
        except Exception as exc:
            raise ExitStoreError(f"corrupt {label} line {path}:{number}: {exc}") from exc
    return payloads


def _write_json(path: Path, payload: Any, *, overwrite: bool = False) -> None:
    """Write JSON atomically. A half-written record would still be read back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ExitStoreError(f"refusing to overwrite {path}")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExitStoreError(f"corrupt stored exit artifact {path}: {exc}") from exc
