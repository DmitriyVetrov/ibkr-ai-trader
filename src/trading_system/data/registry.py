"""Provider registry and explicit fallback.

Consumers ask the registry for "something that can supply option chains", never
for "the IBKR provider". That indirection is what lets a paid provider be added
later — or a broken one be removed — without touching a collector.

Fallback is supported and is deliberately noisy about itself. When provider A
is unavailable and provider B answers, the resulting records say ``provider:
B`` and ``requested_provider: A``. There is no code path that lets B's data be
returned under A's name, because the record that would say otherwise is built
by B (:meth:`~trading_system.data.providers.base.DataProvider.metadata`) and
the registry only annotates it afterwards.

Selection is deterministic: registration order within a data type, filtered by
availability. Given the same registry and the same environment, the same
provider is chosen every time.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import TypeVar

from trading_system.data.models import DataRecord
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderDescription,
    ProviderError,
    ProviderResult,
    ProviderUnavailableError,
)
from trading_system.domain.enums import CollectionOutcome, DataType

__all__ = ["FallbackAttempt", "ProviderRegistry", "ProviderSelection"]

ProviderT = TypeVar("ProviderT", bound=DataProvider)
RecordT = TypeVar("RecordT", bound=DataRecord)


@dataclass(frozen=True, slots=True)
class FallbackAttempt:
    """One provider that was asked and did not answer."""

    provider_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Which provider was chosen for a data type, and who was skipped."""

    provider: DataProvider
    skipped: tuple[FallbackAttempt, ...] = ()


class ProviderRegistry:
    """A registry of providers, indexed by the data types they supply."""

    def __init__(self) -> None:
        # builtins.list because this class deliberately exposes a method named
        # `list()` (the registry API the specification asks for), which shadows
        # the builtin inside the class body.
        self._providers: builtins.list[DataProvider] = []

    def register(self, provider: DataProvider) -> DataProvider:
        """Add a provider. Returns it, so registration can be inlined.

        A duplicate ``provider_id`` for the same data type is refused: two
        different objects answering to one name would make stored provenance
        ambiguous, and provenance is the whole point.
        """
        if not provider.provider_id:
            raise ValueError("a provider must declare a provider_id")
        for existing in self._providers:
            if existing.provider_id == provider.provider_id and (
                existing.data_types & provider.data_types
            ):
                raise ValueError(
                    f"provider id {provider.provider_id!r} is already registered for "
                    f"{sorted(t.value for t in existing.data_types & provider.data_types)}"
                )
        self._providers.append(provider)
        return provider

    def get(self, provider_id: str) -> DataProvider:
        for provider in self._providers:
            if provider.provider_id == provider_id:
                return provider
        raise KeyError(f"no provider registered with id {provider_id!r}")

    def list(self) -> Sequence[DataProvider]:
        """Every registered provider, in registration order."""
        return tuple(self._providers)

    def describe(self) -> Sequence[ProviderDescription]:
        return [provider.describe() for provider in self._providers]

    def for_data_type(self, data_type: DataType) -> Sequence[DataProvider]:
        """Providers that can supply ``data_type``, in registration order."""
        return [p for p in self._providers if data_type in p.data_types]

    def select(
        self,
        data_type: DataType,
        *,
        preferred: str | None = None,
        require_available: bool = True,
    ) -> ProviderSelection:
        """Choose a provider for ``data_type``.

        Raises:
            ProviderUnavailableError: nothing registered can supply the type,
                or everything that can is unavailable. Deliberately an error
                rather than a ``None``: a caller that ignores the difference
                between "no data" and "no provider" will eventually store one
                as the other.
        """
        candidates = list(self.for_data_type(data_type))
        if preferred is not None:
            candidates = [p for p in candidates if p.provider_id == preferred] or candidates
        if not candidates:
            raise ProviderUnavailableError(f"no provider is registered for {data_type.value}")

        skipped: builtins.list[FallbackAttempt] = []
        for provider in candidates:
            availability = provider.availability()
            if not require_available or availability is ProviderAvailability.AVAILABLE:
                return ProviderSelection(provider=provider, skipped=tuple(skipped))
            skipped.append(
                FallbackAttempt(provider_id=provider.provider_id, reason=availability.value)
            )

        raise ProviderUnavailableError(
            f"every provider for {data_type.value} is unavailable: "
            + ", ".join(f"{a.provider_id}={a.reason}" for a in skipped)
        )

    def fetch_with_fallback(
        self,
        data_type: DataType,
        operation: Callable[[DataProvider], ProviderResult[RecordT]],
        *,
        key: str,
        preferred: str | None = None,
    ) -> ProviderResult[RecordT]:
        """Try each capable provider in turn until one returns data.

        The returned records name whoever actually produced them. When a
        fallback was used, ``requested_provider`` on every record records who
        was asked first — so the audit trail shows both the intent and the
        reality.
        """
        candidates = list(self.for_data_type(data_type))
        if preferred is not None:
            candidates = [p for p in candidates if p.provider_id == preferred] + [
                p for p in candidates if p.provider_id != preferred
            ]
        if not candidates:
            raise ProviderUnavailableError(f"no provider is registered for {data_type.value}")

        first_asked = candidates[0].provider_id
        attempts: builtins.list[FallbackAttempt] = []
        last: ProviderResult[RecordT] | None = None

        for provider in candidates:
            if provider.availability() is not ProviderAvailability.AVAILABLE:
                attempts.append(
                    FallbackAttempt(provider.provider_id, provider.availability().value)
                )
                continue
            try:
                result = operation(provider)
            except ProviderError as exc:
                attempts.append(FallbackAttempt(provider.provider_id, str(exc)))
                continue
            if result.succeeded:
                return _annotate_fallback(result, first_asked=first_asked, attempts=attempts)
            attempts.append(
                FallbackAttempt(provider.provider_id, result.error or result.outcome.value)
            )
            last = result

        if last is not None:
            return _annotate_fallback(last, first_asked=first_asked, attempts=attempts)
        return ProviderResult(
            provider_id=first_asked,
            data_type=data_type,
            key=key,
            outcome=CollectionOutcome.PROVIDER_UNAVAILABLE,
            error="; ".join(f"{a.provider_id}: {a.reason}" for a in attempts)
            or "no provider was available",
            notes=tuple(f"{a.provider_id}: {a.reason}" for a in attempts),
        )

    def __iter__(self) -> Iterator[DataProvider]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)


def _annotate_fallback[RecordT: DataRecord](
    result: ProviderResult[RecordT],
    *,
    first_asked: str,
    attempts: Sequence[FallbackAttempt],
) -> ProviderResult[RecordT]:
    """Record that a fallback happened, without rewriting who produced the data."""
    if result.provider_id == first_asked or not result.records:
        return (
            result
            if not attempts
            else replace(result, notes=tuple(f"{a.provider_id}: {a.reason}" for a in attempts))
        )

    annotated = tuple(
        record.model_copy(
            update={"source": record.source.model_copy(update={"requested_provider": first_asked})}
        )
        for record in result.records
    )
    return replace(
        result,
        records=annotated,
        notes=tuple(f"{a.provider_id}: {a.reason}" for a in attempts),
    )
