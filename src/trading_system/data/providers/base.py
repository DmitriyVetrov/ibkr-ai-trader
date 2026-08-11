"""Provider abstractions.

A provider does exactly one thing: retrieve data and hand back canonical
records with honest provenance. It does not decide anything. No provider in
this package may rank a candidate, classify a hypothesis, size a position,
choose a strategy or submit an order — those belong to layers that sit above
this one, and keeping the boundary sharp is what lets a paid provider be
swapped in later without a consumer noticing.

Three properties every implementation must hold:

* **Bounded.** Every external call carries a timeout. An unbounded request is
  how a collector hangs forever on a broker round trip that will never be
  answered.
* **Honest about failure.** A provider that cannot answer raises. It never
  substitutes a stale value, a zero, or another provider's number.
* **Honest about identity.** ``provider`` on a returned record always names
  whoever actually produced the data. When a fallback was used,
  ``requested_provider`` records who was asked first — the record can never
  claim a source that was not consulted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique

from trading_system.data.models import DataRecord, DataSourceMetadata, RawRecord
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SourceTier,
)

__all__ = [
    "DataProvider",
    "ProviderAvailability",
    "ProviderCost",
    "ProviderDescription",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderResult",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "failed_result",
    "successful_result",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProviderError(RuntimeError):
    """Base class for every provider failure."""


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or is not usable right now."""


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not answer within its configured bound."""


class ProviderRateLimitError(ProviderUnavailableError):
    """The provider refused the request because we asked too often."""


class ProviderResponseError(ProviderError):
    """The provider answered, but the response could not be used."""


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
@unique
class ProviderCost(StrEnum):
    """What a provider costs.

    Recorded so ``data providers`` can prove at a glance that nothing paid is
    required. ``FREE_WITH_ACCOUNT`` covers sources that cost nothing but need
    credentials — an IBKR paper login, for instance.
    """

    FREE = "FREE"
    FREE_WITH_ACCOUNT = "FREE_WITH_ACCOUNT"
    PAID = "PAID"


@unique
class ProviderAvailability(StrEnum):
    """Whether a provider can be used in this environment right now."""

    AVAILABLE = "AVAILABLE"
    #: Implemented, but its dependency is absent — no gateway, no network.
    UNAVAILABLE = "UNAVAILABLE"
    #: Interface only; no real retrieval implemented yet, by design.
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass(frozen=True, slots=True)
class ProviderDescription:
    """Everything the CLI and the registry need to know about a provider."""

    provider_id: str
    name: str
    tier: SourceTier
    cost: ProviderCost
    origin: MarketDataOrigin
    data_types: frozenset[DataType]
    availability: ProviderAvailability
    requires_network: bool = False
    requires_broker: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProviderResult[RecordT: DataRecord]:
    """One retrieval attempt: what came back, and what happened.

    Carries the raw response alongside the normalised records so the collector
    can store both. A result is returned for failures too — a failure is
    information, and it belongs in the collection ledger rather than only in a
    stack trace.
    """

    provider_id: str
    data_type: DataType
    key: str
    outcome: CollectionOutcome
    records: tuple[RecordT, ...] = ()
    raw: RawRecord | None = None
    error: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return self.outcome in (CollectionOutcome.SUCCESS, CollectionOutcome.PARTIAL_SUCCESS)

    @property
    def record_count(self) -> int:
        return len(self.records)


def successful_result[RecordT: DataRecord](
    *,
    provider_id: str,
    data_type: DataType,
    key: str,
    records: Sequence[RecordT],
    raw: RawRecord | None = None,
    partial: bool = False,
    notes: Sequence[str] = (),
) -> ProviderResult[RecordT]:
    """Build a result for a retrieval that returned data.

    An empty record list is reported as ``NO_DATA`` rather than success: "the
    provider is fine and there is nothing" is a different fact from "we got
    what we asked for", and only the first should stop a caller retrying.
    """
    outcome = CollectionOutcome.SUCCESS
    if not records:
        outcome = CollectionOutcome.NO_DATA
    elif partial:
        outcome = CollectionOutcome.PARTIAL_SUCCESS
    return ProviderResult(
        provider_id=provider_id,
        data_type=data_type,
        key=key,
        outcome=outcome,
        records=tuple(records),
        raw=raw,
        notes=tuple(notes),
    )


def failed_result[RecordT: DataRecord](
    *,
    provider_id: str,
    data_type: DataType,
    key: str,
    outcome: CollectionOutcome,
    error: str,
    raw: RawRecord | None = None,
) -> ProviderResult[RecordT]:
    """Build a result for a retrieval that produced no usable data."""
    return ProviderResult(
        provider_id=provider_id,
        data_type=data_type,
        key=key,
        outcome=outcome,
        records=(),
        raw=raw,
        error=error,
    )


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class DataProvider(ABC):
    """Common base for every data provider."""

    #: Stable identifier recorded on every record this provider produces.
    #: Changing it orphans previously stored provenance.
    provider_id: str = ""
    display_name: str = ""
    tier: SourceTier = SourceTier.TIER_4
    cost: ProviderCost = ProviderCost.FREE
    origin: MarketDataOrigin = MarketDataOrigin.PROVIDER_DELAYED
    requires_network: bool = False
    requires_broker: bool = False
    notes: str = ""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive; unbounded requests are refused")
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        """Bound on any single external request this provider makes."""
        return self._timeout_seconds

    @property
    @abstractmethod
    def data_types(self) -> frozenset[DataType]:
        """Which data types this provider can supply."""

    @abstractmethod
    def availability(self) -> ProviderAvailability:
        """Whether the provider can be used right now.

        Must not raise and must not perform an expensive probe: it is called to
        decide whether attempting a retrieval is worthwhile.
        """

    def describe(self) -> ProviderDescription:
        return ProviderDescription(
            provider_id=self.provider_id,
            name=self.display_name or self.provider_id,
            tier=self.tier,
            cost=self.cost,
            origin=self.origin,
            data_types=self.data_types,
            availability=self.availability(),
            requires_network=self.requires_network,
            requires_broker=self.requires_broker,
            notes=self.notes,
        )

    def metadata(
        self,
        *,
        retrieved_at: datetime,
        origin: MarketDataOrigin | None = None,
        source_identifier: str | None = None,
        source_timestamp: datetime | None = None,
        published_at: datetime | None = None,
        effective_at: datetime | None = None,
        observed_at: datetime | None = None,
        requested_provider: str | None = None,
        field_provenance: Mapping[str, str] | None = None,
    ) -> DataSourceMetadata:
        """Build provenance for a record this provider produced.

        Always stamps *this* provider as the author. A fallback records who was
        asked first in ``requested_provider`` and never rewrites ``provider``.
        """
        return DataSourceMetadata(
            provider=self.provider_id,
            source_name=self.display_name or self.provider_id,
            source_tier=self.tier,
            origin=origin or self.origin,
            retrieved_at=retrieved_at,
            source_identifier=source_identifier,
            source_timestamp=source_timestamp,
            published_at=published_at,
            effective_at=effective_at,
            observed_at=observed_at,
            requested_provider=requested_provider,
            field_provenance=dict(field_provenance or {}),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider_id={self.provider_id!r}, tier={self.tier.value})"
