"""Registry-driven collection.

Binds three things that are otherwise independent: a registry that knows which
providers exist, an operation that knows what to ask a provider for, and a
:class:`~trading_system.data.collectors.base.DataCollector` that knows how to
assess and store the answer.

Keeping them separate is what makes fallback honest. The registry decides who
to ask, the provider stamps its own identity on what it returns, and the
collector stores whatever it is handed. No layer is in a position to attribute
one provider's data to another.
"""

from __future__ import annotations

from collections.abc import Callable

from trading_system.data.collectors.base import CollectionReport, DataCollector
from trading_system.data.models import DataRecord
from trading_system.data.providers.base import DataProvider, ProviderResult
from trading_system.data.registry import ProviderRegistry
from trading_system.domain.enums import DataType

__all__ = ["RegistryCollector"]


class RegistryCollector[RecordT: DataRecord]:
    """Collects one data type, choosing its provider from the registry."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        collector: DataCollector,
        operation: Callable[[DataProvider, str], ProviderResult[RecordT]],
    ) -> None:
        self._registry = registry
        self._collector = collector
        self._operation = operation

    @property
    def data_type(self) -> DataType:
        return self._collector.data_type

    def collect(self, key: str, *, preferred: str | None = None) -> CollectionReport:
        """Retrieve and store ``key``, falling back across providers if needed."""
        normalised = key.upper()
        candidates = list(self._registry.for_data_type(self._collector.data_type))
        requested = (
            preferred
            if preferred is not None
            else (candidates[0].provider_id if candidates else "NONE")
        )
        return self._collector.collect(
            normalised,
            lambda: self._registry.fetch_with_fallback(
                self._collector.data_type,
                lambda provider: self._operation(provider, normalised),
                key=normalised,
                preferred=preferred,
            ),
            requested_provider=requested,
        )
