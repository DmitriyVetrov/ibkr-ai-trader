"""SEC EDGAR filing metadata.

Exercised against a recorded EDGAR response, so the real parsing runs with no
network access. What is checked: the filings become canonical records with
correct point-in-time timestamps, unknown forms degrade to ``OTHER`` without
losing the exact form string, and every failure mode is reported rather than
raised into the collector.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.data.providers.http import StaticHttpFetcher
from trading_system.data.providers.regulatory import (
    SEC_TICKER_URL,
    SecEdgarRegulatoryProvider,
    SecTickerResolver,
    parse_form_type,
)
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    RegulatoryFormType,
    SourceTier,
)

pytestmark = pytest.mark.unit

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"


@pytest.fixture
def edgar_fetcher(load_data_fixture) -> StaticHttpFetcher:
    return StaticHttpFetcher(
        {
            SUBMISSIONS_URL: load_data_fixture("sec_submissions_aapl.json"),
            SEC_TICKER_URL: load_data_fixture("sec_company_tickers.json"),
        },
        headers={SUBMISSIONS_URL: {"last-modified": "Sat, 01 Aug 2026 18:10:00 GMT"}},
    )


@pytest.fixture
def edgar(edgar_fetcher: StaticHttpFetcher, data_clock) -> SecEdgarRegulatoryProvider:
    return SecEdgarRegulatoryProvider(edgar_fetcher, clock=data_clock)


# ---------------------------------------------------------------------------
# Valid response
# ---------------------------------------------------------------------------
def test_filings_become_canonical_records(edgar) -> None:
    result = edgar.fetch_filings("AAPL")

    assert result.outcome is CollectionOutcome.SUCCESS
    assert result.record_count == 3
    filing = result.records[0]
    assert filing.company_name == "Apple Inc."
    assert filing.cik == "0000320193"
    assert filing.accession_number == "0000320193-26-000081"
    assert filing.url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")


def test_the_provider_is_tier_one_and_free(edgar) -> None:
    description = edgar.describe()

    assert description.tier is SourceTier.TIER_1
    assert description.cost.value == "FREE"
    assert description.requires_network


def test_acceptance_time_is_the_publication_time(edgar) -> None:
    """A filing becomes public when EDGAR accepts it, not on its filing date."""
    filing = edgar.fetch_filings("AAPL").records[0]

    assert filing.accepted_at == datetime(2026, 8, 1, 18, 4, 12, tzinfo=UTC)
    assert filing.source.published_at == filing.accepted_at
    assert filing.filed_at == filing.accepted_at


def test_the_period_of_report_is_kept_separate_from_the_filing_date(edgar) -> None:
    filing = edgar.fetch_filings("AAPL").records[0]

    assert filing.period_of_report is not None
    assert filing.period_of_report < filing.filed_at.date()


def test_form_items_are_parsed(edgar) -> None:
    eight_k = next(f for f in edgar.fetch_filings("AAPL").records if f.raw_form == "8-K")
    assert eight_k.items == ["2.02", "9.01"]


def test_the_raw_response_is_preserved(edgar) -> None:
    result = edgar.fetch_filings("AAPL")

    assert result.raw is not None
    assert result.raw.data_type is DataType.REGULATORY_EVENT
    assert result.raw.source_identifier == SUBMISSIONS_URL
    assert result.raw.payload["name"] == "Apple Inc."


def test_the_sources_own_last_modified_is_recorded(edgar) -> None:
    result = edgar.fetch_filings("AAPL")
    assert result.raw is not None
    assert result.raw.source_timestamp == datetime(2026, 8, 1, 18, 10, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Form mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10-K", RegulatoryFormType.FORM_10K),
        ("10-Q", RegulatoryFormType.FORM_10Q),
        ("8-K", RegulatoryFormType.FORM_8K),
        ("10-K/A", RegulatoryFormType.FORM_10K),
        ("NT 10-Q", RegulatoryFormType.OTHER),
        ("", RegulatoryFormType.OTHER),
    ],
)
def test_form_types_map_or_fall_back_to_other(raw: str, expected: RegulatoryFormType) -> None:
    assert parse_form_type(raw) is expected


def test_an_amendment_keeps_its_exact_form_string(edgar) -> None:
    """``10-K/A`` maps to 10-K, but the record still says it was an amendment."""
    amendment = next(f for f in edgar.fetch_filings("AAPL").records if "/" in f.raw_form)

    assert amendment.raw_form == "10-K/A"
    assert amendment.form_type is RegulatoryFormType.FORM_10K


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def test_since_filters_by_filing_time(edgar) -> None:
    cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    result = edgar.fetch_filings("AAPL", since=cutoff)

    assert result.record_count == 2
    assert all(f.filed_at >= cutoff for f in result.records)


def test_limit_is_respected(edgar) -> None:
    assert edgar.fetch_filings("AAPL", limit=1).record_count == 1


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_an_unknown_ticker_is_reported_not_guessed(edgar) -> None:
    """Guessing a CIK would attribute a filing to the wrong company."""
    result = edgar.fetch_filings("NOTATICKER")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert "no SEC CIK" in (result.error or "")
    assert result.records == ()


def test_an_unreachable_endpoint_is_reported(data_clock, load_data_fixture) -> None:
    fetcher = StaticHttpFetcher({SEC_TICKER_URL: load_data_fixture("sec_company_tickers.json")})
    provider = SecEdgarRegulatoryProvider(fetcher, clock=data_clock)

    result = provider.fetch_filings("AAPL")
    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()


def test_a_malformed_payload_is_reported_as_invalid_data(data_clock) -> None:
    fetcher = StaticHttpFetcher(
        {
            SUBMISSIONS_URL: '{"name": "Apple Inc.", "filings": {"recent": "not-an-object"}}',
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    provider = SecEdgarRegulatoryProvider(fetcher, clock=data_clock)

    result = provider.fetch_filings("AAPL")
    assert result.outcome is CollectionOutcome.INVALID_DATA
    assert result.records == ()
    # The raw evidence is kept even though parsing failed.
    assert result.raw is not None


def test_non_json_is_reported(data_clock) -> None:
    fetcher = StaticHttpFetcher(
        {
            SUBMISSIONS_URL: "<html>service unavailable</html>",
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    result = SecEdgarRegulatoryProvider(fetcher, clock=data_clock).fetch_filings("AAPL")

    assert result.outcome is CollectionOutcome.INVALID_DATA


def test_a_filing_with_no_usable_date_is_skipped(data_clock) -> None:
    """Without a timestamp the filing cannot be placed on a timeline."""
    fetcher = StaticHttpFetcher(
        {
            SUBMISSIONS_URL: (
                '{"name":"X","filings":{"recent":{"accessionNumber":["a","b"],'
                '"form":["8-K","8-K"],"filingDate":["","2026-08-01"],'
                '"reportDate":["",""],"acceptanceDateTime":["",""],'
                '"primaryDocument":["x.htm","y.htm"],"items":["",""]}}}'
            ),
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    result = SecEdgarRegulatoryProvider(fetcher, clock=data_clock).fetch_filings("AAPL")

    assert result.record_count == 1
    assert result.records[0].accession_number == "b"


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------
def test_a_preloaded_cik_map_avoids_the_remote_lookup(data_clock) -> None:
    fetcher = StaticHttpFetcher({})
    resolver = SecTickerResolver(fetcher, preloaded={"AAPL": "320193"})

    assert resolver.resolve("aapl") == "0000320193"
    assert fetcher.requested_urls == []


def test_ciks_are_zero_padded(edgar_fetcher) -> None:
    resolver = SecTickerResolver(edgar_fetcher)
    assert resolver.resolve("NVDA") == "0001045810"


# ---------------------------------------------------------------------------
# The provider does not interpret
# ---------------------------------------------------------------------------
def test_the_provider_returns_metadata_and_no_interpretation(edgar) -> None:
    filing = edgar.fetch_filings("AAPL").records[0]
    fields = set(filing.model_dump())

    for forbidden in ("sentiment", "summary_analysis", "signal", "hypothesis"):
        assert forbidden not in fields
