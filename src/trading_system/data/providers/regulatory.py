"""Regulatory filing providers.

SEC EDGAR is a Tier 1 source: it is the filing itself, not a report about the
filing. It is free, needs no key, and asks only for a descriptive User-Agent.

This provider returns filing *metadata* — form type, acceptance time, period,
accession number, URL. It does not read the filing. Deciding what a 10-K means
is the research agent's job in Milestone 5, and a data provider that starts
interpreting is a research agent nobody reviewed.

Point-in-time matters here more than anywhere else. A filing's
``published_at`` is its acceptance timestamp, which is the instant it became
public. A filing accepted after time T is invisible to a reconstruction of T
even though we hold it today.

Network access is required. ``pytest`` never touches the network: every
ordinary test injects
:class:`~trading_system.data.providers.http.StaticHttpFetcher` with a recorded
response, which exercises exactly the same parsing code.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from trading_system.data.hashing import payload_hash
from trading_system.data.models import RawRecord, RegulatoryEvent
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
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    RegulatoryFormType,
    SourceTier,
)
from trading_system.infrastructure.clock import Clock, SystemClock

__all__ = [
    "SEC_TICKER_URL",
    "RegulatoryProvider",
    "SecEdgarRegulatoryProvider",
    "SecTickerResolver",
    "parse_form_type",
]

#: The SEC's ticker-to-CIK map. On www.sec.gov rather than data.sec.gov.
SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"

_FORMS: dict[str, RegulatoryFormType] = {
    "10-K": RegulatoryFormType.FORM_10K,
    "10-Q": RegulatoryFormType.FORM_10Q,
    "8-K": RegulatoryFormType.FORM_8K,
    "S-1": RegulatoryFormType.FORM_S1,
    "4": RegulatoryFormType.FORM_4,
    "13F-HR": RegulatoryFormType.FORM_13F,
    "DEF 14A": RegulatoryFormType.FORM_DEF14A,
}


def parse_form_type(raw: str) -> RegulatoryFormType:
    """Map a form string, falling back to ``OTHER`` rather than guessing.

    Amendments (``10-K/A``) map to their base form: the same document type,
    filed again. The exact string is preserved on the record either way.
    """
    cleaned = raw.strip().upper()
    if cleaned in _FORMS:
        return _FORMS[cleaned]
    base = cleaned.split("/", 1)[0].strip()
    return _FORMS.get(base, RegulatoryFormType.OTHER)


class RegulatoryProvider(DataProvider):
    """Interface for regulatory filing retrieval."""

    @property
    def data_types(self) -> frozenset[DataType]:
        return frozenset({DataType.REGULATORY_EVENT})

    @abstractmethod
    def fetch_filings(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> ProviderResult[RegulatoryEvent]:
        """Retrieve filing metadata for one issuer. The filings are not read."""


class SecTickerResolver:
    """Resolves a ticker to a 10-digit zero-padded CIK.

    Accepts a preloaded mapping so that tests, and deployments that would
    rather not depend on one more remote file, can skip the lookup entirely.
    """

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        timeout_seconds: float = 15.0,
        preloaded: Mapping[str, str] | None = None,
        url: str = SEC_TICKER_URL,
    ) -> None:
        self._fetcher = fetcher
        self._timeout = timeout_seconds
        self._url = url
        self._map: dict[str, str] = {
            key.upper(): _pad_cik(value) for key, value in (preloaded or {}).items()
        }
        self._loaded = bool(self._map)

    def resolve(self, symbol: str) -> str:
        """Return the padded CIK for ``symbol``.

        Raises:
            ProviderUnavailableError: the mapping could not be loaded, or the
                ticker is not in it. Guessing a CIK would attribute a filing to
                the wrong company.
        """
        key = symbol.upper()
        if key in self._map:
            return self._map[key]
        if not self._loaded:
            self._load()
        if key not in self._map:
            raise ProviderUnavailableError(f"no SEC CIK is known for ticker {key}")
        return self._map[key]

    def _load(self) -> None:
        response = self._fetcher.get(self._url, timeout_seconds=self._timeout)
        payload = response.json()
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str", row.get("cik"))
            if ticker and cik is not None:
                self._map.setdefault(ticker, _pad_cik(cik))
        self._loaded = True


class SecEdgarRegulatoryProvider(RegulatoryProvider):
    """Filing metadata from the SEC EDGAR submissions API."""

    provider_id = "SEC_EDGAR"
    display_name = "SEC EDGAR"
    tier = SourceTier.TIER_1
    cost = ProviderCost.FREE
    origin = MarketDataOrigin.PROVIDER_REALTIME
    requires_network = True
    notes = "Free, no key. Requires a descriptive User-Agent and outbound HTTPS."

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
        # Deliberately not a live probe: availability is consulted before every
        # collection, and an HTTP request per check would be its own rate-limit
        # problem. A network failure surfaces as a PROVIDER_UNAVAILABLE result.
        return ProviderAvailability.AVAILABLE

    def submissions_url(self, cik: str) -> str:
        return f"{self._base_url}/submissions/CIK{cik}.json"

    def fetch_filings(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> ProviderResult[RegulatoryEvent]:
        key = symbol.upper()
        try:
            cik = self._resolver.resolve(key)
            url = self.submissions_url(cik)
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
            data_type=DataType.REGULATORY_EVENT,
            key=key,
            retrieved_at=retrieved_at,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_identifier=url,
            source_timestamp=_header_timestamp(response.headers),
            request={"symbol": key, "cik": cik},
        )

        try:
            events = self._parse(payload, key, cik, url, retrieved_at, since=since, limit=limit)
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(
                key, CollectionOutcome.INVALID_DATA, f"unexpected EDGAR payload: {exc}", raw=raw
            )

        return successful_result(
            provider_id=self.provider_id,
            data_type=DataType.REGULATORY_EVENT,
            key=key,
            records=events,
            raw=raw,
        )

    # --- parsing -----------------------------------------------------------
    def _parse(
        self,
        payload: object,
        symbol: str,
        cik: str,
        url: str,
        retrieved_at: datetime,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[RegulatoryEvent]:
        if not isinstance(payload, dict):
            raise TypeError("EDGAR submissions payload is not an object")
        company = str(payload.get("name") or "") or None
        recent = ((payload.get("filings") or {}) or {}).get("recent") or {}
        if not isinstance(recent, dict):
            raise TypeError("EDGAR filings.recent is not an object")

        accessions = _column(recent, "accessionNumber")
        forms = _column(recent, "form")
        filing_dates = _column(recent, "filingDate")
        report_dates = _column(recent, "reportDate")
        acceptances = _column(recent, "acceptanceDateTime")
        documents = _column(recent, "primaryDocument")
        items = _column(recent, "items")

        events: list[RegulatoryEvent] = []
        for index, accession in enumerate(accessions):
            if len(events) >= limit:
                break
            raw_form = _at(forms, index) or ""
            accepted_at = _parse_timestamp(_at(acceptances, index))
            filed_at = accepted_at or _parse_date_as_utc(_at(filing_dates, index))
            if filed_at is None:
                # No usable filing time means the record cannot be placed on a
                # timeline, which makes it useless for point-in-time work.
                continue
            if since is not None and filed_at < since:
                continue

            events.append(
                RegulatoryEvent(
                    as_of=filed_at,
                    source=self.metadata(
                        retrieved_at=retrieved_at,
                        source_identifier=_filing_url(cik, accession, _at(documents, index)),
                        published_at=accepted_at or filed_at,
                        effective_at=filed_at,
                        source_timestamp=accepted_at,
                    ),
                    event_id=f"sec:{accession}",
                    symbol=symbol,
                    form_type=parse_form_type(raw_form),
                    raw_form=raw_form,
                    filed_at=filed_at,
                    accepted_at=accepted_at,
                    period_of_report=_parse_date(_at(report_dates, index)),
                    company_name=company,
                    cik=cik,
                    accession_number=accession,
                    url=_filing_url(cik, accession, _at(documents, index)),
                    items=[
                        part.strip()
                        for part in (_at(items, index) or "").split(",")
                        if part.strip()
                    ],
                )
            )
        return events

    def _failure(
        self,
        key: str,
        outcome: CollectionOutcome,
        error: str,
        *,
        raw: RawRecord | None = None,
    ) -> ProviderResult[RegulatoryEvent]:
        return failed_result(
            provider_id=self.provider_id,
            data_type=DataType.REGULATORY_EVENT,
            key=key,
            outcome=outcome,
            error=error,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _pad_cik(value: object) -> str:
    return str(value).strip().lstrip("CIK").lstrip("cik").strip().zfill(10)


def _column(recent: Mapping[str, object], name: str) -> Sequence[str]:
    value = recent.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"EDGAR column {name} is not a list")
    return [str(item) if item is not None else "" for item in value]


def _at(column: Sequence[str], index: int) -> str | None:
    if index >= len(column):
        return None
    return column[index] or None


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an EDGAR acceptance timestamp, which ends in ``Z``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_date_as_utc(value: str | None) -> datetime | None:
    """A filing date with no time. Midnight UTC, and the record says so.

    Inventing a plausible business-hours time would be a fabricated timestamp;
    midnight is unambiguous and conservative for visibility.
    """
    parsed = _parse_date(value)
    return None if parsed is None else datetime.combine(parsed, datetime.min.time(), tzinfo=UTC)


def _filing_url(cik: str, accession: str, document: str | None) -> str:
    plain = accession.replace("-", "")
    stem = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{plain}"
    return f"{stem}/{document}" if document else f"{stem}/{accession}-index.htm"


def _header_timestamp(headers: Mapping[str, str]) -> datetime | None:
    """The source's own ``Last-Modified``, when it supplies one."""
    raw = headers.get("last-modified")
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
