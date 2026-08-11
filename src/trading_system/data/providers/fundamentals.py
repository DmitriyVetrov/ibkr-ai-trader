"""Fundamentals providers.

The free, official source for US issuers is the SEC's XBRL ``companyfacts``
API: the numbers as filed, with the filing date attached to every single fact.
That last detail is what makes it usable for point-in-time work — a restated
figure does not overwrite the original, it arrives as a new fact with a later
``filed`` date, and both remain visible.

Scope is deliberately narrow. This provider *retrieves* reported figures. It
computes no ratios, no growth rates and no valuation: analysis is a later
milestone, and a provider that quietly starts analysing is an unreviewed model
in the data path.

Two fields are left ``None`` on purpose rather than being filled in:

``market_capitalization``
    Would require multiplying a share count from the SEC by a price from the
    broker. Cross-provider arithmetic without field-level provenance is exactly
    what section 29 forbids, and the two sources are not synchronised.
``next_earnings_date``
    EDGAR records filings that have happened, not calendars of what will. No
    free, reliable source for scheduled earnings dates is wired up.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from trading_system.data.hashing import payload_hash
from trading_system.data.models import FundamentalSnapshot, RawRecord
from trading_system.data.providers.base import (
    DataProvider,
    ProviderAvailability,
    ProviderCost,
    ProviderError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    failed_result,
    successful_result,
)
from trading_system.data.providers.http import HttpFetcher
from trading_system.data.providers.regulatory import SecTickerResolver
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    SourceTier,
)
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "FundamentalsProvider",
    "SecFundamentalsProvider",
]


class _Fact(NamedTuple):
    """One XBRL observation, with the provenance that makes it usable."""

    concept: str
    value: Decimal
    unit: str
    period_start: date | None
    period_end: date
    filed: date
    accession: str | None
    form: str | None
    fiscal_period: str | None


#: Concepts to try, in order, for each canonical field. Issuers use different
#: revenue tags depending on their accounting policy, so the first tag that has
#: data wins and the record says which one it was.
_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
    ),
    "net_income": (("us-gaap", "NetIncomeLoss"),),
    "eps_basic": (("us-gaap", "EarningsPerShareBasic"),),
    "eps_diluted": (("us-gaap", "EarningsPerShareDiluted"),),
    "shares_outstanding": (("dei", "EntityCommonStockSharesOutstanding"),),
}


class FundamentalsProvider(DataProvider):
    """Interface for reported-fundamentals retrieval."""

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.FUNDAMENTAL_SNAPSHOT})

    @abstractmethod
    def fetch_fundamentals(self, symbol: str) -> ProviderResult[FundamentalSnapshot]:
        """Retrieve the most recently reported figures for one issuer."""


class SecFundamentalsProvider(FundamentalsProvider):
    """Reported fundamentals from the SEC XBRL ``companyfacts`` API."""

    provider_id = "SEC_XBRL"
    display_name = "SEC XBRL company facts"
    tier = SourceTier.TIER_1
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.PROVIDER_REALTIME
    requires_network = True
    notes = "Free, no key. As-filed figures with per-fact filing dates."

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        resolver: SecTickerResolver | None = None,
        base_url: str = "https://data.sec.gov",
        clock: Clock | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._fetcher = fetcher
        self._resolver = resolver or SecTickerResolver(fetcher, timeout_seconds=timeout_seconds)
        self._base_url = base_url.rstrip("/")
        self._clock = clock or SystemClock()

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability.AVAILABLE

    def companyfacts_url(self, cik: str) -> str:
        return f"{self._base_url}/api/xbrl/companyfacts/CIK{cik}.json"

    def fetch_fundamentals(self, symbol: str) -> ProviderResult[FundamentalSnapshot]:
        key = symbol.upper()
        try:
            cik = self._resolver.resolve(key)
            url = self.companyfacts_url(cik)
            response = self._fetcher.get(url, timeout_seconds=self._timeout_seconds)
            payload = response.json()
        except ProviderTimeoutError as exc:
            return self._failure(key, CollectionOutcome.TIMEOUT, str(exc))
        except ProviderUnavailableError as exc:
            return self._failure(key, CollectionOutcome.PROVIDER_UNAVAILABLE, str(exc))
        except ProviderError as exc:
            return self._failure(key, CollectionOutcome.INVALID_DATA, str(exc))

        retrieved_at = self._clock.now()
        raw = RawRecord(
            provider=self.provider_id,
            data_type=DataType.FUNDAMENTAL_SNAPSHOT,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_identifier=url,
            request={"symbol": key, "cik": cik},
        )

        try:
            snapshot = self._build(payload, key, url, retrieved_at)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            return self._failure(
                key,
                CollectionOutcome.INVALID_DATA,
                f"unexpected companyfacts payload: {exc}",
                raw=raw,
            )

        if snapshot is None:
            return failed_result(
                provider_id=self.provider_id,
                data_type=DataType.FUNDAMENTAL_SNAPSHOT,
                key=key,
                outcome=CollectionOutcome.NO_DATA,
                error=f"no usable XBRL facts for {key}",
                raw=raw,
            )

        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.FUNDAMENTAL_SNAPSHOT,
            key=key,
            records=[snapshot],
            raw=raw,
        )

    # --- parsing -----------------------------------------------------------
    def _build(
        self, payload: Any, symbol: str, url: str, retrieved_at: datetime
    ) -> FundamentalSnapshot | None:
        if not isinstance(payload, dict):
            raise TypeError("companyfacts payload is not an object")
        facts = payload.get("facts")
        if not isinstance(facts, dict):
            raise TypeError("companyfacts payload has no facts object")

        selected: dict[str, _Fact] = {}
        for field_name, candidates in _CONCEPTS.items():
            for namespace, concept in candidates:
                fact = _latest_fact(facts, namespace, concept)
                if fact is not None:
                    selected[field_name] = fact
                    break

        if not selected:
            return None

        # Facts can come from different filings — a share count from a cover
        # page, revenue from the statements. Recording which concept and which
        # filing produced each figure is what keeps the mix honest.
        provenance = {
            name: f"{fact.concept}@{fact.accession or 'unknown-accession'}"
            for name, fact in selected.items()
        }
        published = max(fact.filed for fact in selected.values())

        # The reporting period comes from whichever statement fact was actually
        # selected, not from a range spanning several of them. The ``dei`` share
        # count in particular is stamped with the cover-page date, days after
        # the quarter it accompanies — letting it set ``period_end`` would label
        # a Q3 statement with a date that falls in Q4.
        statement_facts = [
            fact for fact in selected.values() if fact.concept.startswith("us-gaap:")
        ] or list(selected.values())
        primary = selected.get("revenue") or statement_facts[0]
        period_end = primary.period_end
        starts = [primary.period_start] if primary.period_start else []
        currency = next(
            (fact.unit for fact in selected.values() if fact.unit.upper() in {"USD", "EUR"}),
            None,
        )

        published_at = datetime.combine(published, datetime.min.time(), tzinfo=UTC)
        effective_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)

        return FundamentalSnapshot(
            as_of=published_at,
            source=self.metadata(
                retrieved_at=retrieved_at,
                source_identifier=url,
                published_at=published_at,
                # The period the figures describe, which is not when they were
                # published. Keeping both is what makes a restatement legible.
                effective_at=effective_at,
                field_provenance=provenance,
            ),
            symbol=symbol,
            currency=currency,
            fiscal_period=primary.fiscal_period,
            period_start=min(starts) if starts else None,
            period_end=period_end,
            revenue=_value(selected, "revenue"),
            net_income=_value(selected, "net_income"),
            eps_basic=_value(selected, "eps_basic"),
            eps_diluted=_value(selected, "eps_diluted"),
            shares_outstanding=_value(selected, "shares_outstanding"),
            market_capitalization=None,
            next_earnings_date=None,
            filing_accession_number=primary.accession,
            filing_form=primary.form,
            filing_filed_at=datetime.combine(primary.filed, datetime.min.time(), tzinfo=UTC),
        )

    def _failure(
        self,
        key: str,
        outcome: CollectionOutcome,
        error: str,
        *,
        raw: RawRecord | None = None,
    ) -> ProviderResult[FundamentalSnapshot]:
        return failed_result(
            provider_id=self.provider_id,
            data_type=DataType.FUNDAMENTAL_SNAPSHOT,
            key=key,
            outcome=outcome,
            error=error,
            raw=raw,
        )


def _value(selected: Mapping[str, _Fact], field_name: str) -> Decimal | None:
    fact = selected.get(field_name)
    return None if fact is None else fact.value


def _latest_fact(facts: Mapping[str, Any], namespace: str, concept: str) -> _Fact | None:
    """The most recently reported observation of one concept.

    Ordered by period end first, then filing date, then *shortest* period.

    The last one matters more than it looks. An issuer reports the same concept
    over several windows ending on the same day — Apple's Q3 filing carries
    both the three-month revenue and the nine-month cumulative, both ending
    2026-06-27. Preferring the longer window silently turns "quarterly revenue"
    into a year-to-date figure roughly four times larger. The discrete period
    is what a reader means by a quarter, so the shorter window wins, and the
    record's ``period_start``/``period_end`` always state which was chosen.
    """
    namespace_facts = facts.get(namespace)
    if not isinstance(namespace_facts, dict):
        return None
    entry = namespace_facts.get(concept)
    if not isinstance(entry, dict):
        return None
    units = entry.get("units")
    if not isinstance(units, dict):
        return None

    best: _Fact | None = None
    for unit, observations in units.items():
        if not isinstance(observations, Sequence):
            continue
        for observation in observations:
            candidate = _to_fact(observation, f"{namespace}:{concept}", str(unit))
            if candidate is None:
                continue
            if best is None or _ordering(candidate) > _ordering(best):
                best = candidate
    return best


def _ordering(fact: _Fact) -> tuple[date, date, int]:
    """Rank one observation: newest period, newest filing, shortest window."""
    duration = (fact.period_end - fact.period_start).days if fact.period_start else 0
    return (fact.period_end, fact.filed, -duration)


def _to_fact(observation: Any, concept: str, unit: str) -> _Fact | None:
    if not isinstance(observation, dict):
        return None
    end = _as_date(observation.get("end"))
    filed = _as_date(observation.get("filed"))
    raw_value = observation.get("val")
    if end is None or filed is None or raw_value is None:
        return None
    try:
        # str() first: XBRL values arrive as JSON numbers, and Decimal(float)
        # would preserve the float's representation error rather than the
        # figure the issuer reported.
        value = Decimal(str(raw_value))
    except InvalidOperation:
        return None
    return _Fact(
        concept=concept,
        value=value,
        unit=unit,
        period_start=_as_date(observation.get("start")),
        period_end=end,
        filed=filed,
        accession=_as_str(observation.get("accn")),
        form=_as_str(observation.get("form")),
        fiscal_period=_as_str(observation.get("fp")),
    )


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
