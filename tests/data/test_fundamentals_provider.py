"""SEC XBRL fundamentals.

Exercised against a recorded ``companyfacts`` response. The properties that
matter: figures arrive as exact decimals with the filing date attached, the
most recent period wins without an older restatement displacing it, and the two
fields that would require cross-provider arithmetic are left ``None`` rather
than computed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.data.providers.fundamentals import SecFundamentalsProvider
from trading_system.data.providers.http import StaticHttpFetcher
from trading_system.data.providers.regulatory import SEC_TICKER_URL, SecTickerResolver
from trading_system.domain.enums import CollectionOutcome, DataType, SourceTier

pytestmark = pytest.mark.unit

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


@pytest.fixture
def facts_fetcher(load_data_fixture) -> StaticHttpFetcher:
    return StaticHttpFetcher(
        {
            FACTS_URL: load_data_fixture("sec_companyfacts_aapl.json"),
            SEC_TICKER_URL: load_data_fixture("sec_company_tickers.json"),
        }
    )


@pytest.fixture
def fundamentals(facts_fetcher: StaticHttpFetcher, data_clock) -> SecFundamentalsProvider:
    return SecFundamentalsProvider(facts_fetcher, clock=data_clock)


# ---------------------------------------------------------------------------
# Valid response
# ---------------------------------------------------------------------------
def test_reported_figures_become_a_canonical_snapshot(fundamentals) -> None:
    result = fundamentals.fetch_fundamentals("AAPL")

    assert result.outcome is CollectionOutcome.SUCCESS
    snapshot = result.records[0]
    assert snapshot.symbol == "AAPL"
    assert snapshot.revenue == Decimal("94036000000")
    assert snapshot.net_income == Decimal("23434000000")
    assert snapshot.eps_basic == Decimal("1.58")
    assert snapshot.eps_diluted == Decimal("1.57")
    assert snapshot.shares_outstanding == Decimal("14840000000")


def test_figures_are_exact_decimals(fundamentals) -> None:
    """An EPS that arrived as a float would compare unreliably ever after."""
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert isinstance(snapshot.eps_basic, Decimal)
    assert str(snapshot.eps_basic) == "1.58"


def test_the_period_is_recorded(fundamentals) -> None:
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.period_end == date(2026, 6, 27)
    assert snapshot.period_start == date(2026, 3, 29)
    assert snapshot.fiscal_period == "Q3"


def test_the_filing_reference_is_kept(fundamentals) -> None:
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.filing_accession_number == "0000320193-26-000081"
    assert snapshot.filing_form == "10-Q"
    assert snapshot.filing_filed_at == datetime(2026, 8, 1, tzinfo=UTC)


def test_the_latest_period_wins_over_an_older_one(fundamentals) -> None:
    """The fixture holds a 2025 revenue alongside the 2026 one."""
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.revenue == Decimal("94036000000")
    assert snapshot.revenue != Decimal("85777000000")


# ---------------------------------------------------------------------------
# Point-in-time timestamps
# ---------------------------------------------------------------------------
def test_publication_and_effective_times_are_distinct(fundamentals) -> None:
    """When it was published and what period it describes are different facts."""
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.source.published_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert snapshot.source.effective_at == datetime(2026, 6, 27, tzinfo=UTC)
    assert snapshot.source.effective_at < snapshot.source.published_at


def test_the_snapshot_is_invisible_before_it_was_filed(fundamentals) -> None:
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert not snapshot.known_at(datetime(2026, 7, 15, tzinfo=UTC))
    assert snapshot.known_at(datetime(2026, 8, 10, 14, 30, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Field-level provenance
# ---------------------------------------------------------------------------
def test_each_figure_records_the_concept_it_came_from(fundamentals) -> None:
    """Figures can come from different filings; the record says which."""
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]
    provenance = snapshot.source.field_provenance

    assert provenance["revenue"].startswith("us-gaap:RevenueFromContractWithCustomer")
    assert provenance["shares_outstanding"].startswith("dei:EntityCommonStockSharesOutstanding")
    assert "0000320193-26-000081" in provenance["revenue"]


def test_nothing_is_computed_across_providers(fundamentals) -> None:
    """Market cap would need a SEC share count and a broker price. Not merged."""
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.market_capitalization is None
    assert snapshot.next_earnings_date is None


def test_the_provider_is_tier_one_and_free(fundamentals) -> None:
    description = fundamentals.describe()

    assert description.tier is SourceTier.TIER_1
    assert description.cost.value == "FREE"
    assert DataType.FUNDAMENTAL_SNAPSHOT in description.data_types


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_an_unknown_ticker_is_reported(fundamentals) -> None:
    result = fundamentals.fetch_fundamentals("NOTATICKER")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()


def test_an_empty_facts_payload_is_no_data(data_clock) -> None:
    fetcher = StaticHttpFetcher(
        {
            FACTS_URL: '{"cik": 320193, "entityName": "Apple Inc.", "facts": {}}',
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    result = SecFundamentalsProvider(fetcher, clock=data_clock).fetch_fundamentals("AAPL")

    assert result.outcome is CollectionOutcome.NO_DATA
    assert result.records == ()
    assert result.raw is not None


def test_a_malformed_payload_is_reported_as_invalid_data(data_clock) -> None:
    fetcher = StaticHttpFetcher(
        {
            FACTS_URL: '{"cik": 320193, "facts": "not-an-object"}',
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    result = SecFundamentalsProvider(fetcher, clock=data_clock).fetch_fundamentals("AAPL")

    assert result.outcome is CollectionOutcome.INVALID_DATA
    assert result.records == ()


def test_facts_missing_a_filing_date_are_ignored(data_clock) -> None:
    """A figure with no filing date cannot be placed on a timeline."""
    fetcher = StaticHttpFetcher(
        {
            FACTS_URL: (
                '{"cik":320193,"facts":{"us-gaap":{"NetIncomeLoss":{"units":{"USD":['
                '{"end":"2026-06-27","val":100}]}}}}}'
            ),
            SEC_TICKER_URL: '{"0": {"cik_str": 320193, "ticker": "AAPL"}}',
        }
    )
    result = SecFundamentalsProvider(fetcher, clock=data_clock).fetch_fundamentals("AAPL")

    assert result.outcome is CollectionOutcome.NO_DATA


def test_the_raw_payload_is_preserved(fundamentals) -> None:
    result = fundamentals.fetch_fundamentals("AAPL")

    assert result.raw is not None
    assert result.raw.source_identifier == FACTS_URL
    assert result.raw.payload["entityName"] == "Apple Inc."


def test_a_preloaded_resolver_is_used(data_clock, load_data_fixture) -> None:
    fetcher = StaticHttpFetcher({FACTS_URL: load_data_fixture("sec_companyfacts_aapl.json")})
    provider = SecFundamentalsProvider(
        fetcher,
        resolver=SecTickerResolver(fetcher, preloaded={"AAPL": "320193"}),
        clock=data_clock,
    )

    assert provider.fetch_fundamentals("AAPL").succeeded
    assert SEC_TICKER_URL not in fetcher.requested_urls


def test_the_discrete_period_wins_over_the_cumulative_one(fundamentals) -> None:
    """An issuer files both the quarter and the year-to-date, ending the same day.

    Both series live under one XBRL concept with the same ``end``. Taking the
    longer window silently turns "quarterly revenue" into a figure several times
    larger, and nothing in the record would say so — except that it does: the
    period bounds always state which window was chosen.
    """
    snapshot = fundamentals.fetch_fundamentals("AAPL").records[0]

    assert snapshot.period_start == date(2026, 3, 29)
    assert snapshot.period_end == date(2026, 6, 27)
    assert snapshot.revenue == Decimal("94036000000")
    assert snapshot.revenue != Decimal("294000000000")
