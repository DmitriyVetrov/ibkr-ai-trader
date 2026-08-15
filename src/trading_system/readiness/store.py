"""Persistence for readiness runs and live-readiness sign-offs (Milestone 12).

The same discipline as every other store in this system, and for the same
reasons:

* **Immutable base records.** A readiness run is written once. Writing
  different content under an existing id raises rather than overwriting — an
  assessment that changed silently would be a rewritten audit conclusion, which
  is precisely what an acceptance gate exists to make impossible.
* **Append-only history.** ``runs.jsonl`` and ``signoffs.jsonl`` gain a line
  per artifact and are never rewritten.
* **Atomic.** Written through a temporary file and ``os.replace``, because a
  half-written record is indistinguishable from a truncated one.
* **Content-addressed.** Ids derive from the evidence *and the conclusion*, so
  re-evaluating unchanged evidence records a re-observation rather than a
  second, differently-named copy of one fact.

Sign-offs live in their own directory and their own index, deliberately. A
sign-off is a *human decision about* a run, not a property of it; folding it
into the run record would mean rewriting an immutable artifact to record that
somebody read it.
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

from trading_system.readiness.models import LiveReadinessSignoff, ReadinessRun

__all__ = [
    "FilesystemReadinessRepository",
    "ReadinessHistoryEntry",
    "ReadinessRepository",
    "ReadinessStoreError",
    "SignoffHistoryEntry",
]

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class ReadinessStoreError(RuntimeError):
    """A readiness artifact could not be stored or read back."""


@dataclass(frozen=True, slots=True)
class ReadinessHistoryEntry:
    """One line of the append-only run index.

    Carries the level and the revision so ``readiness history`` — and the
    operational-history collector, which counts distinct days — can be answered
    by scanning one file rather than by opening every stored run.
    """

    readiness_run_id: str
    status: str
    level: str
    trading_mode: str
    git_revision: str | None
    working_tree_clean: bool | None
    evaluated_at: datetime
    assessment_id: str | None = None
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "readiness_run_id": self.readiness_run_id,
            "status": self.status,
            "level": self.level,
            "trading_mode": self.trading_mode,
            "git_revision": self.git_revision,
            "working_tree_clean": self.working_tree_clean,
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "assessment_id": self.assessment_id,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ReadinessHistoryEntry:
        return cls(
            readiness_run_id=str(payload["readiness_run_id"]),
            status=str(payload["status"]),
            level=str(payload["level"]),
            trading_mode=str(payload["trading_mode"]),
            git_revision=(
                None if payload.get("git_revision") is None else str(payload["git_revision"])
            ),
            working_tree_clean=(
                None
                if payload.get("working_tree_clean") is None
                else bool(payload["working_tree_clean"])
            ),
            evaluated_at=datetime.fromisoformat(str(payload["evaluated_at"])),
            assessment_id=(
                None if payload.get("assessment_id") is None else str(payload["assessment_id"])
            ),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(cls, run: ReadinessRun, *, reobserved: bool = False) -> ReadinessHistoryEntry:
        return cls(
            readiness_run_id=run.readiness_run_id,
            status=run.status.value,
            level=run.level.value,
            trading_mode=run.trading_mode.value,
            git_revision=run.git_revision,
            working_tree_clean=run.working_tree_clean,
            evaluated_at=run.evaluated_at,
            assessment_id=(run.assessment.assessment_id if run.assessment else None),
            reobserved=reobserved,
        )


@dataclass(frozen=True, slots=True)
class SignoffHistoryEntry:
    """One line of the append-only sign-off index."""

    signoff_id: str
    readiness_run_id: str
    status: str
    readiness_level: str
    signed_by: str
    signed_at: datetime
    git_revision: str | None
    reobserved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "signoff_id": self.signoff_id,
            "readiness_run_id": self.readiness_run_id,
            "status": self.status,
            "readiness_level": self.readiness_level,
            "signed_by": self.signed_by,
            "signed_at": self.signed_at.astimezone(UTC).isoformat(),
            "git_revision": self.git_revision,
            "reobserved": self.reobserved,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SignoffHistoryEntry:
        return cls(
            signoff_id=str(payload["signoff_id"]),
            readiness_run_id=str(payload["readiness_run_id"]),
            status=str(payload["status"]),
            readiness_level=str(payload["readiness_level"]),
            signed_by=str(payload["signed_by"]),
            signed_at=datetime.fromisoformat(str(payload["signed_at"])),
            git_revision=(
                None if payload.get("git_revision") is None else str(payload["git_revision"])
            ),
            reobserved=bool(payload.get("reobserved", False)),
        )

    @classmethod
    def of(cls, signoff: LiveReadinessSignoff, *, reobserved: bool = False) -> SignoffHistoryEntry:
        return cls(
            signoff_id=signoff.signoff_id,
            readiness_run_id=signoff.readiness_run_id,
            status=signoff.status.value,
            readiness_level=signoff.readiness_level.value,
            signed_by=signoff.signed_by,
            signed_at=signoff.signed_at,
            git_revision=signoff.git_revision,
            reobserved=reobserved,
        )


class ReadinessRepository(ABC):
    """What the readiness service needs from storage."""

    @abstractmethod
    def save_run(self, run: ReadinessRun) -> tuple[str, bool]: ...

    @abstractmethod
    def get_run(self, readiness_run_id: str) -> ReadinessRun | None: ...

    @abstractmethod
    def latest_run(self) -> ReadinessRun | None: ...

    @abstractmethod
    def history(self, limit: int | None = None) -> list[ReadinessHistoryEntry]: ...

    @abstractmethod
    def save_signoff(self, signoff: LiveReadinessSignoff) -> tuple[str, bool]: ...

    @abstractmethod
    def latest_signoff(self) -> LiveReadinessSignoff | None: ...

    @abstractmethod
    def signoff_history(self, limit: int | None = None) -> list[SignoffHistoryEntry]: ...


class FilesystemReadinessRepository(ReadinessRepository):
    """Immutable readiness records under a data root.

    .. code-block:: text

        <root>/
          runs/<day>/<readiness_run_id>.json
          signoffs/<day>/<signoff_id>.json
          runs.jsonl
          signoffs.jsonl
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def runs_dir(self) -> Path:
        return self._root / "runs"

    @property
    def signoffs_dir(self) -> Path:
        return self._root / "signoffs"

    @property
    def runs_history_path(self) -> Path:
        return self._root / "runs.jsonl"

    @property
    def signoffs_history_path(self) -> Path:
        return self._root / "signoffs.jsonl"

    # --- runs ---------------------------------------------------------------
    def save_run(self, run: ReadinessRun) -> tuple[str, bool]:
        path = (
            self.runs_dir
            / run.evaluated_at.astimezone(UTC).date().isoformat()
            / f"{_safe(run.readiness_run_id)}.json"
        )
        payload = run.model_dump(mode="json")
        is_new = _write_once(path, payload, what=f"readiness run {run.readiness_run_id}")
        _append_line(
            self.runs_history_path, ReadinessHistoryEntry.of(run, reobserved=not is_new).to_json()
        )
        return run.readiness_run_id, is_new

    def get_run(self, readiness_run_id: str) -> ReadinessRun | None:
        for entry in self.history():
            if entry.readiness_run_id == readiness_run_id:
                return self._load_run(entry)
        return None

    def latest_run(self) -> ReadinessRun | None:
        for entry in self.history():
            record = self._load_run(entry)
            if record is not None:
                return record
        return None

    def all_runs(self, limit: int | None = None) -> list[ReadinessRun]:
        seen: set[str] = set()
        records: list[ReadinessRun] = []
        for entry in self.history():
            if entry.readiness_run_id in seen:
                continue
            seen.add(entry.readiness_run_id)
            record = self._load_run(entry)
            if record is not None:
                records.append(record)
            if limit is not None and len(records) >= limit:
                break
        return records

    def history(self, limit: int | None = None) -> list[ReadinessHistoryEntry]:
        entries = [
            ReadinessHistoryEntry.from_json(payload)
            for payload in _read_lines(self.runs_history_path, "readiness history")
        ]
        entries.sort(key=lambda e: (e.evaluated_at, e.readiness_run_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- sign-offs ------------------------------------------------------------
    def save_signoff(self, signoff: LiveReadinessSignoff) -> tuple[str, bool]:
        path = (
            self.signoffs_dir
            / signoff.signed_at.astimezone(UTC).date().isoformat()
            / f"{_safe(signoff.signoff_id)}.json"
        )
        payload = signoff.model_dump(mode="json")
        is_new = _write_once(path, payload, what=f"live-readiness sign-off {signoff.signoff_id}")
        _append_line(
            self.signoffs_history_path,
            SignoffHistoryEntry.of(signoff, reobserved=not is_new).to_json(),
        )
        return signoff.signoff_id, is_new

    def latest_signoff(self) -> LiveReadinessSignoff | None:
        for entry in self.signoff_history():
            record = self._load_signoff(entry)
            if record is not None:
                return record
        return None

    def signoffs_for(self, readiness_run_id: str) -> list[LiveReadinessSignoff]:
        records: list[LiveReadinessSignoff] = []
        for entry in sorted(self.signoff_history(), key=lambda e: (e.signed_at, e.signoff_id)):
            if entry.readiness_run_id != readiness_run_id:
                continue
            record = self._load_signoff(entry)
            if record is not None:
                records.append(record)
        return records

    def signoff_history(self, limit: int | None = None) -> list[SignoffHistoryEntry]:
        entries = [
            SignoffHistoryEntry.from_json(payload)
            for payload in _read_lines(self.signoffs_history_path, "sign-off history")
        ]
        entries.sort(key=lambda e: (e.signed_at, e.signoff_id), reverse=True)
        return entries[:limit] if limit is not None else entries

    # --- internals -------------------------------------------------------------
    def _load_run(self, entry: ReadinessHistoryEntry) -> ReadinessRun | None:
        path = (
            self.runs_dir
            / entry.evaluated_at.astimezone(UTC).date().isoformat()
            / f"{_safe(entry.readiness_run_id)}.json"
        )
        if not path.is_file():
            return None
        return ReadinessRun.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _load_signoff(self, entry: SignoffHistoryEntry) -> LiveReadinessSignoff | None:
        path = (
            self.signoffs_dir
            / entry.signed_at.astimezone(UTC).date().isoformat()
            / f"{_safe(entry.signoff_id)}.json"
        )
        if not path.is_file():
            return None
        return LiveReadinessSignoff.model_validate(json.loads(path.read_text(encoding="utf-8")))


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


def _write_once(path: Path, payload: dict[str, Any], *, what: str) -> bool:
    """Write a record that must never change. Returns whether it was new.

    Identical content under an existing id is a *re-observation* and is
    accepted silently; different content is a contradiction and raises. A
    readiness conclusion that changed under the same id could not be audited,
    because there would be no record of what it used to say — and this is the
    artifact somebody will eventually point at to justify going live.
    """
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == payload:
            return False
        raise ReadinessStoreError(
            f"{what} already exists with different content at {path}. Readiness results are "
            f"immutable: a verdict that changed under the same id is exactly the artifact an "
            f"acceptance gate exists to make impossible."
        )
    _atomic_write(path, payload)
    return True


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
            raise ReadinessStoreError(f"{what} line {number} in {path} is not valid JSON") from exc
    return payloads
