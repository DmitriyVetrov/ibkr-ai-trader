"""Deterministic hashing of data payloads.

Three jobs (Milestone 3 brief section 38):

* detect that a provider returned a response we already have;
* detect that a snapshot's content is unchanged since the last one;
* verify that a stored snapshot has not been altered on disk.

The hash is **not** the identifier. Snapshot ids are built from the hash *plus*
the data type, key, provider, schema version and ``as_of``, so two genuinely
different datasets that happened to serialise identically could never collide
into one stored record.

Volatile fields are excluded before hashing. Whether we fetched a response at
09:00 or at 09:05 is a fact about us, not about the data, and letting it change
the hash would make idempotent collection impossible: every re-run would look
like new history. What the *provider* stamped (``as_of``, ``source_timestamp``,
``published_at``) is part of the data and is hashed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
    "VOLATILE_KEYS",
    "canonical_json",
    "payload_hash",
    "snapshot_identifier",
    "stable_hash",
]

#: Keys stripped before hashing, at any nesting depth.
#:
#: The hash answers one question: *is this the same information?*
#:
#: Observation clocks are excluded, because *when we looked* is a fact about us
#: rather than about the market: ``as_of``, ``retrieved_at``,
#: ``source_timestamp`` and ``observed_at``. So are our own derived values —
#: ``quality``, ``evaluated_at``, ``payload_hash``, ``snapshot_id``.
#:
#: This is the load-bearing choice in the module. Keeping the observation
#: clocks would make every re-collection of an unchanged option chain look like
#: new history, since a chain's timestamps come from our own clock — exactly
#: the uncontrolled duplication that section 31 forbids. The cost is that two
#: observations with byte-identical values collapse into one snapshot plus a
#: re-observation entry in the ledger, which is the intended behaviour: the
#: second observation added no information, and the ledger still records that
#: we looked and when.
#:
#: Timestamps that *are* information stay hashed: a bar's ``period_end``, a
#: filing's ``filed_at``, an article's ``published_at``, a restatement's
#: ``effective_at``. Two articles published a day apart are two articles.
VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "as_of",
        "retrieved_at",
        "source_timestamp",
        "observed_at",
        "quality",
        "evaluated_at",
        "payload_hash",
        "snapshot_id",
        "record_count",
        "requested_provider",
    }
)

_HASH_PREFIX_LENGTH = 32


def _normalise(value: Any) -> Any:
    """Reduce a value to a JSON-safe, order-stable form.

    Decimals become strings rather than floats: ``5.10`` and ``5.1`` are the
    same number but different exact decimals, and a hash that cannot tell them
    apart would silently deduplicate a real change in quoted precision.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return f"D:{value}"
    if isinstance(value, datetime):
        return f"T:{value.isoformat()}"
    if isinstance(value, date):
        return f"D8:{value.isoformat()}"
    if isinstance(value, float):
        # Should not occur — the models reject binary floats — but a provider
        # payload stored raw may contain one, and it must still hash stably.
        return f"F:{value!r}"
    if isinstance(value, bytes | bytearray):
        return f"B:{value.hex()}"
    if isinstance(value, Mapping):
        return {
            str(k): _normalise(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k) not in VOLATILE_KEYS
        }
    if isinstance(value, Sequence):
        return [_normalise(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(_normalise(v) for v in value)
    return f"R:{value!r}"


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to a canonical JSON string.

    Keys sorted, no incidental whitespace, volatile fields removed. Two
    payloads that mean the same thing produce the same string.
    """
    return json.dumps(
        _normalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def stable_hash(value: Any) -> str:
    """SHA-256 of the canonical serialisation, truncated to 32 hex characters.

    Truncated because these are stored in filenames and read by humans; 128
    bits is far beyond what an accidental collision needs, and the hash is
    never the sole identifier anyway.
    """
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return digest[:_HASH_PREFIX_LENGTH]


def payload_hash(records: Any) -> str:
    """Hash the content of a payload — a raw response or a list of records."""
    return stable_hash(records)


def snapshot_identifier(
    *,
    data_type: str,
    key: str,
    provider: str,
    schema_version: str,
    as_of: datetime,
    content_hash: str,
) -> str:
    """Build a snapshot's deterministic identifier.

    A snapshot is uniquely identified by symbol, timestamp, data type, provider
    and schema version (Milestone 3 brief section 16); the content hash is
    folded in as well so that a corrected re-collection for the same instant
    does not silently overwrite the earlier one.
    """
    return stable_hash(
        [
            data_type,
            key,
            provider,
            schema_version,
            as_of.isoformat(),
            content_hash,
        ]
    )
