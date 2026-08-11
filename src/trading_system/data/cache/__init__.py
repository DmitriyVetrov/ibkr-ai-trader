"""Regenerable cache. Never mixed with immutable snapshots.

The cache exists to stop us asking a provider the same question twice in a
minute. It is not history, not evidence, and not a fallback for a failed
request: deleting the whole directory must cost nothing but a few refetches.

Two rules keep it from quietly becoming something more:

* **Nothing is read from cache without being relabelled.** A record served
  from here comes back stamped ``CACHED``. A quote that was realtime when it
  was stored is not realtime when it is replayed, and the origin has to say so
  (:func:`as_cached`).
* **Expiry is honest.** An entry past its TTL is a miss, not a stale hit. A
  provider outage produces "no data", never yesterday's price wearing today's
  timestamp.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_system.data.models import DataRecord
from trading_system.domain.enums import MarketDataOrigin
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = ["CacheEntry", "DataCache", "as_cached"]

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def as_cached[RecordT: DataRecord](record: RecordT) -> RecordT:
    """Relabel a record as having come from the cache.

    Retrieval and source timestamps are left untouched: the data is exactly as
    old as it was, and the point of the relabelling is to make that visible
    rather than to disguise it.
    """
    source = record.source.model_copy(update={"origin": MarketDataOrigin.CACHED})
    return record.model_copy(update={"source": source})


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached payload with its expiry."""

    key: str
    stored_at: datetime
    expires_at: datetime
    payload: Any

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class DataCache:
    """A time-to-live cache on disk under ``data/cache/``.

    Deliberately dumb: no eviction policy, no size bound, no negative caching.
    Sophistication here would create exactly the kind of hidden state the data
    layer is supposed to eliminate.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        clock: Clock | None = None,
        default_ttl_seconds: int = 300,
        enabled: bool = True,
    ) -> None:
        self._root = Path(root)
        self._clock = clock or SystemClock()
        self._default_ttl = default_ttl_seconds
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def root(self) -> Path:
        return self._root

    def get(self, namespace: str, key: str) -> CacheEntry | None:
        """Return a live entry, or ``None`` for a miss or an expired entry."""
        if not self._enabled:
            return None
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = CacheEntry(
                key=str(raw["key"]),
                stored_at=datetime.fromisoformat(raw["stored_at"]),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
                payload=raw["payload"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # A corrupt cache entry is a miss. It is regenerable by definition,
            # so there is nothing to salvage and nothing to report upwards.
            path.unlink(missing_ok=True)
            return None

        if entry.is_expired(self._clock.now()):
            return None
        return entry

    def put(
        self, namespace: str, key: str, payload: Any, *, ttl_seconds: int | None = None
    ) -> CacheEntry | None:
        """Store a payload. Returns the entry, or ``None`` when disabled."""
        if not self._enabled:
            return None
        now = self._clock.now()
        entry = CacheEntry(
            key=key,
            stored_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds or self._default_ttl),
            payload=payload,
        )
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "key": entry.key,
                        "stored_at": entry.stored_at.astimezone(UTC).isoformat(),
                        "expires_at": entry.expires_at.astimezone(UTC).isoformat(),
                        "payload": entry.payload,
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return entry

    def invalidate(self, namespace: str, key: str) -> None:
        self._path(namespace, key).unlink(missing_ok=True)

    def clear(self, namespace: str | None = None) -> int:
        """Delete cached entries. Returns how many files were removed.

        Safe to call at any time — that is the defining property of a cache.
        """
        target = self._root / _safe(namespace) if namespace else self._root
        if not target.is_dir():
            return 0
        removed = 0
        for path in target.rglob("*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _path(self, namespace: str, key: str) -> Path:
        return self._root / _safe(namespace) / f"{_safe(key)}.json"


def _safe(value: str) -> str:
    return _SAFE.sub("_", value)
