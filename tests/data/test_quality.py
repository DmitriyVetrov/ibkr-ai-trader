"""Data quality: what it flags, and what it must never change.

The engine's whole value rests on one property — it describes records, it never
edits them. Several tests below assert exactly that: after a suspicious value
has been flagged, the value is still bit-for-bit what the provider sent.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_system.data.models import (
    DataQualityReport,
    FundamentalSnapshot,
    MarketBar,
    NewsArticle,
    OptionContract,
    OptionSnapshot,
    RegulatoryEvent,
)
from trading_system.data.quality import QualityContext, QualityEngine, content_fingerprint
from trading_system.domain.enums import (
    BarInterval,
    DataQuality,
    DataQualityIssue,
    MarketDataOrigin,
    RegulatoryFormType,
    SecurityType,
    SourceTier,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. A valid quote
# ---------------------------------------------------------------------------
def test_a_clean_quote_passes_every_dimension(quality_engine, make_quote, data_now) -> None:
    report = quality_engine.evaluate(make_quote(), context=QualityContext(now=data_now))

    assert report.issues == []
    assert report.research_usable
    assert report.classification is DataQuality.OK
    for dimension in (
        report.transport_valid,
        report.schema_valid,
        report.source_valid,
        report.timestamp_valid,
        report.freshness_valid,
        report.completeness_valid,
        report.plausibility_valid,
        report.consistency_valid,
    ):
        assert dimension


# ---------------------------------------------------------------------------
# 2. bid > ask
# ---------------------------------------------------------------------------
def test_crossed_quote_is_flagged_and_never_swapped(quality_engine, make_quote, data_now) -> None:
    """A crossed market is a broken feed. Swapping the sides would hide it."""
    quote = make_quote(bid=Decimal("500.90"), ask=Decimal("500.10"))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.CROSSED_BID_ASK)
    assert not report.consistency_valid
    assert not report.research_usable
    # The values are exactly what came in.
    assert quote.bid == Decimal("500.90")
    assert quote.ask == Decimal("500.10")


# ---------------------------------------------------------------------------
# 3. Negative and zero prices
# ---------------------------------------------------------------------------
def test_negative_price_is_flagged_and_preserved(quality_engine, make_quote, data_now) -> None:
    quote = make_quote(bid=Decimal("-1.50"))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.NEGATIVE_PRICE)
    assert not report.plausibility_valid
    assert quote.bid == Decimal("-1.50")


def test_zero_price_is_flagged_rather_than_nulled(quality_engine, make_quote, data_now) -> None:
    """Zero is IBKR's "no data" marker, not a price — and it stays visible."""
    quote = make_quote(bid=Decimal("0"))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.ZERO_PRICE)
    assert quote.bid == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Timestamps
# ---------------------------------------------------------------------------
def test_future_timestamp_is_invalid(quality_engine, make_quote, data_now) -> None:
    quote = make_quote(as_of=data_now + timedelta(hours=1))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.FUTURE_TIMESTAMP)
    assert not report.timestamp_valid
    assert not report.research_usable


def test_publication_after_retrieval_is_contradictory(quality_engine, make_quote, data_now) -> None:
    quote = make_quote(published_at=data_now + timedelta(days=1), retrieved_at=data_now)
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.CONTRADICTORY_FIELDS)
    assert not report.consistency_valid


# ---------------------------------------------------------------------------
# 5. Staleness
# ---------------------------------------------------------------------------
def test_stale_quote_is_flagged_but_stays_research_usable(
    quality_engine, make_quote, data_now
) -> None:
    """Freshness is contextual, so the consumer decides — see config/data.yaml.

    The record still carries ``freshness_valid=False`` so a consumer that does
    care can filter on it.
    """
    old = data_now - timedelta(hours=6)
    quote = make_quote(as_of=old, retrieved_at=old, source_timestamp=old)
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.STALE_DATA)
    assert not report.freshness_valid
    assert report.research_usable
    assert report.classification is DataQuality.STALE


def test_freshness_window_differs_by_data_type(data_config) -> None:
    """One global threshold cannot be right for a quote and a filing at once."""
    quote_window = data_config.freshness.window_seconds("MARKET_QUOTE")
    filing_window = data_config.freshness.window_seconds("REGULATORY_EVENT")
    bar_window = data_config.freshness.window_seconds("MARKET_BAR")

    assert quote_window < bar_window < filing_window


def test_realtime_origin_tightens_the_window(data_config) -> None:
    realtime = data_config.freshness.window_seconds("MARKET_QUOTE", "BROKER_REALTIME")
    delayed = data_config.freshness.window_seconds("MARKET_QUOTE", "BROKER_DELAYED")

    assert realtime < delayed


# ---------------------------------------------------------------------------
# 6. Suspicious volume — the IBKR paper finding
# ---------------------------------------------------------------------------
def test_suspicious_volume_is_flagged_and_the_raw_value_survives(
    quality_engine, make_quote, data_now, data_config
) -> None:
    """The Milestone 2 finding, encoded as a regression.

    Real IBKR paper validation returned an SPY volume that cannot be a real
    session volume. The required behaviour is: preserve the value and flag it
    — never correct it and never drop the record.

    Whether such a record may still be *researched* is a separate, configured
    question, and it is the only thing the tolerated-issue allow-list moves;
    see the tolerance section at the end of this module.
    """
    absurd = Decimal(data_config.plausibility.max_equity_daily_volume) * 10
    quote = make_quote(volume=absurd)
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert not report.plausibility_valid
    assert DataQualityIssue.SUSPICIOUS_VOLUME in report.plausibility_issues
    # Technically valid and economically impossible at the same time.
    assert report.transport_valid and report.schema_valid and report.source_valid
    assert report.technically_valid
    assert quote.volume == absurd


def test_negative_volume_is_flagged(quality_engine, make_quote, data_now) -> None:
    report = quality_engine.evaluate(
        make_quote(volume=Decimal("-5")), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.NEGATIVE_VOLUME)


def test_ordinary_volume_is_not_flagged(quality_engine, make_quote, data_now) -> None:
    """The bound catches feed corruption, not a busy day."""
    report = quality_engine.evaluate(
        make_quote(volume=Decimal("180000000")), context=QualityContext(now=data_now)
    )
    assert not report.has(DataQualityIssue.SUSPICIOUS_VOLUME)


def test_the_corrupt_delayed_volume_is_flagged_and_preserved(
    quality_engine, make_quote, data_config, data_now
) -> None:
    """The exact SPY tick-74 value captured off the wire on 2026-08-15.

    IBKR sent ``31367915626456`` for a session whose real volume was some 31.4
    million shares. It is not the ``DBL_MAX`` unset sentinel, so nothing
    upstream drops it; it is preserved verbatim here and flagged, because the
    inflation factor is not constant across symbols and any fixed correction
    would therefore be a fabrication on some of them.
    """
    corrupt = Decimal("31367915626456")
    assert corrupt > data_config.plausibility.max_equity_daily_volume

    quote = make_quote(volume=corrupt, average_daily_volume=Decimal("52014430"))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert quote.volume == corrupt, "the raw value must survive the quality pass"
    assert quote.volume != corrupt / Decimal(1_000_000), "nothing rescales it"
    assert quote.average_daily_volume == Decimal("52014430")


def test_a_sound_average_volume_is_not_flagged_by_a_corrupt_session_volume(
    quality_engine, make_quote, data_now
) -> None:
    """The finding names the field that failed, and only that field."""
    quote = make_quote(volume=Decimal("31367915626456"), average_daily_volume=Decimal("52014430"))
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    details = " ".join(report.details)
    assert "average_daily_volume" not in details
    assert "volume 31367915626456" in details


def test_an_implausible_average_volume_is_flagged_on_its_own_terms(
    quality_engine, make_quote, data_config, data_now
) -> None:
    """Tick 21 is trusted, not exempt. It gets the same bound and its own label."""
    absurd = Decimal(data_config.plausibility.max_equity_daily_volume) * 10
    quote = make_quote(volume=Decimal("180000000"), average_daily_volume=absurd)

    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert "average_daily_volume" in " ".join(report.details)
    assert quote.average_daily_volume == absurd


def test_a_missing_average_volume_is_not_an_issue(quality_engine, make_quote, data_now) -> None:
    """Absence is honest, and honestly not a defect: it is simply unavailable."""
    report = quality_engine.evaluate(
        make_quote(volume=Decimal("180000000"), average_daily_volume=None),
        context=QualityContext(now=data_now),
    )

    assert not report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert not report.has(DataQualityIssue.NEGATIVE_VOLUME)


# ---------------------------------------------------------------------------
# 7-8. Missing IV and open interest
# ---------------------------------------------------------------------------
def test_missing_implied_volatility_is_noted_but_not_fatal(
    quality_engine, make_option_quote, data_now
) -> None:
    """Missing Greeks are routine. ``None`` means unknown, and never zero."""
    quote = make_option_quote(implied_volatility=None)
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.MISSING_IMPLIED_VOLATILITY)
    assert quote.implied_volatility is None
    assert report.research_usable


def test_missing_open_interest_is_noted(quality_engine, make_option_quote, data_now) -> None:
    quote = make_option_quote(open_interest=None)
    report = quality_engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.MISSING_OPEN_INTEREST)
    assert quote.open_interest is None


def test_implausible_implied_volatility_is_flagged(
    quality_engine, make_option_quote, data_now
) -> None:
    report = quality_engine.evaluate(
        make_option_quote(implied_volatility=Decimal("50")),
        context=QualityContext(now=data_now),
    )
    assert report.has(DataQualityIssue.IMPLAUSIBLE_IMPLIED_VOLATILITY)
    assert not report.plausibility_valid


def test_impossible_delta_is_flagged(quality_engine, make_option_quote, data_now) -> None:
    report = quality_engine.evaluate(
        make_option_quote(delta=Decimal("3.2")), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.IMPLAUSIBLE_GREEK)


# ---------------------------------------------------------------------------
# 9-10. Contract validity
# ---------------------------------------------------------------------------
def test_expired_contract_is_flagged(
    quality_engine, make_option_quote, spy_option_contract, data_now
) -> None:
    expired = spy_option_contract.model_copy(update={"expiration": date(2020, 1, 17)})
    report = quality_engine.evaluate(
        make_option_quote(contract=expired), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.EXPIRED_CONTRACT)
    assert not report.consistency_valid


def test_missing_expiration_is_flagged_and_the_raw_broker_string_is_kept(
    quality_engine, make_option_quote, spy_option_contract, data_now
) -> None:
    """``YYYYMM`` with no day cannot become a date, and no day is invented."""
    undated = spy_option_contract.model_copy(
        update={"expiration": None, "raw_last_trade_date": "202609"}
    )
    report = quality_engine.evaluate(
        make_option_quote(contract=undated), context=QualityContext(now=data_now)
    )

    assert report.has(DataQualityIssue.INVALID_EXPIRATION)
    assert undated.expiration is None
    assert undated.raw_last_trade_date == "202609"
    assert any("202609" in detail for detail in report.details)


def test_invalid_multiplier_is_flagged(
    quality_engine, make_option_quote, spy_option_contract, data_now
) -> None:
    odd = spy_option_contract.model_copy(update={"multiplier": 7})
    report = quality_engine.evaluate(
        make_option_quote(contract=odd), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.INVALID_MULTIPLIER)


def test_missing_option_right_is_flagged(quality_engine, make_option_quote, data_now) -> None:
    rightless = OptionContract(
        underlying="SPY",
        symbol="SPY",
        security_type=SecurityType.OPTION,
        expiration=date(2026, 9, 18),
        strike=Decimal("500"),
        right=None,
        multiplier=100,
    )
    report = quality_engine.evaluate(
        make_option_quote(contract=rightless), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.INVALID_OPTION_RIGHT)
    assert not report.completeness_valid


def test_implausible_strike_is_flagged(
    quality_engine, make_option_quote, spy_option_contract, data_now, data_config
) -> None:
    absurd = spy_option_contract.model_copy(
        update={"strike": data_config.plausibility.max_strike * 10}
    )
    report = quality_engine.evaluate(
        make_option_quote(contract=absurd), context=QualityContext(now=data_now)
    )
    assert report.has(DataQualityIssue.IMPLAUSIBLE_STRIKE)


# ---------------------------------------------------------------------------
# 11. Duplicates
# ---------------------------------------------------------------------------
def test_a_repeated_record_is_flagged_as_duplicate(quality_engine, make_quote, data_now) -> None:
    quote = make_quote()
    fingerprint = content_fingerprint(quote)

    report = quality_engine.evaluate(
        quote,
        context=QualityContext(now=data_now, known_fingerprints=frozenset({fingerprint})),
    )
    assert report.has(DataQualityIssue.DUPLICATE_RECORD)


def test_duplicates_within_a_batch_are_detected(quality_engine, make_quote, data_now) -> None:
    quote = make_quote()
    assessed = quality_engine.attach_all([quote, quote], context=QualityContext(now=data_now))

    assert not assessed[0].quality.has(DataQualityIssue.DUPLICATE_RECORD)
    assert assessed[1].quality.has(DataQualityIssue.DUPLICATE_RECORD)


def test_a_chain_with_duplicate_contract_ids_is_inconsistent(
    quality_engine, make_chain, spy_option_contract, data_now
) -> None:
    chain = make_chain(with_contracts=True)
    duplicated = chain.model_copy(update={"contracts": [spy_option_contract, spy_option_contract]})
    report = quality_engine.evaluate(duplicated, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.DUPLICATE_RECORD)
    assert not report.consistency_valid


# ---------------------------------------------------------------------------
# 12. Source metadata
# ---------------------------------------------------------------------------
def test_provider_mismatch_is_flagged(quality_engine, make_quote, data_now) -> None:
    """A record claiming a provider we did not ask is crossed plumbing."""
    report = quality_engine.evaluate(
        make_quote(provider="SOMEONE_ELSE"),
        context=QualityContext(now=data_now, expected_provider="IBKR"),
    )
    assert report.has(DataQualityIssue.SOURCE_MISMATCH)
    assert not report.source_valid
    assert not report.research_usable


def test_a_live_origin_without_a_source_timestamp_is_misrepresented(
    quality_engine, make_quote, data_now
) -> None:
    """ "Realtime" has to be substantiated by the source, not asserted by us."""
    report = quality_engine.evaluate(
        make_quote(origin=MarketDataOrigin.BROKER_REALTIME, source_timestamp=None),
        context=QualityContext(now=data_now),
    )
    assert report.has(DataQualityIssue.ORIGIN_MISREPRESENTED)
    assert not report.source_valid


def test_cached_origin_needs_no_source_timestamp(quality_engine, make_quote, data_now) -> None:
    """Cached data is not claiming to be live, so it is held to a lower bar."""
    report = quality_engine.evaluate(
        make_quote(origin=MarketDataOrigin.CACHED, source_timestamp=None),
        context=QualityContext(now=data_now),
    )
    assert not report.has(DataQualityIssue.ORIGIN_MISREPRESENTED)


# ---------------------------------------------------------------------------
# Other record types
# ---------------------------------------------------------------------------
def test_news_without_a_url_cannot_be_cited(quality_engine, make_source, data_now) -> None:
    article = NewsArticle(
        as_of=data_now,
        source=make_source(
            provider="FIXTURE_NEWS",
            tier=SourceTier.TIER_2,
            origin=MarketDataOrigin.HISTORICAL,
            published_at=data_now,
            source_identifier=None,
        ),
        article_id="a-1",
        headline="A headline",
        symbols=["NVDA"],
    )
    report = quality_engine.evaluate(article, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.MISSING_PROVENANCE)
    assert not report.source_valid


def test_a_bar_whose_high_is_below_its_close_is_contradictory(
    quality_engine, make_source, data_now
) -> None:
    bar = MarketBar(
        as_of=data_now,
        source=make_source(),
        symbol="SPY",
        security_type=SecurityType.STOCK,
        interval=BarInterval.DAY_1,
        period_start=data_now - timedelta(days=1),
        period_end=data_now,
        open=Decimal("500"),
        high=Decimal("501"),
        low=Decimal("499"),
        close=Decimal("505"),
    )
    report = quality_engine.evaluate(bar, context=QualityContext(now=data_now))
    assert report.has(DataQualityIssue.CONTRADICTORY_FIELDS)


def test_fundamentals_with_no_figures_are_incomplete(quality_engine, make_source, data_now) -> None:
    snapshot = FundamentalSnapshot(
        as_of=data_now,
        source=make_source(provider="SEC_XBRL"),
        symbol="AAPL",
    )
    report = quality_engine.evaluate(snapshot, context=QualityContext(now=data_now))
    assert report.has(DataQualityIssue.EMPTY_PAYLOAD)
    assert not report.completeness_valid


def test_a_filing_without_an_accession_or_url_cannot_be_cited(
    quality_engine, make_source, data_now
) -> None:
    event = RegulatoryEvent(
        as_of=data_now,
        source=make_source(provider="SEC_EDGAR"),
        event_id="x",
        form_type=RegulatoryFormType.FORM_8K,
        raw_form="8-K",
        filed_at=data_now,
    )
    report = quality_engine.evaluate(event, context=QualityContext(now=data_now))
    assert report.has(DataQualityIssue.MISSING_PROVENANCE)


def test_option_snapshot_rolls_up_the_worst_of_its_parts(
    quality_engine, make_chain, make_option_quote, data_now
) -> None:
    """One implausible contract makes the whole snapshot implausible."""
    snapshot = OptionSnapshot(
        as_of=data_now,
        source=make_chain().source,
        underlying="SPY",
        chain=make_chain(),
        quotes=[make_option_quote(), make_option_quote(delta=Decimal("9"))],
    )
    report = quality_engine.evaluate(snapshot, context=QualityContext(now=data_now))

    assert report.has(DataQualityIssue.IMPLAUSIBLE_GREEK)
    assert not report.plausibility_valid


# ---------------------------------------------------------------------------
# Research usability is a separate question from technical validity
# ---------------------------------------------------------------------------
def test_research_usability_is_configurable_and_distinct(data_config, make_quote, data_now) -> None:
    """Same record, two policies, two verdicts — and identical raw values.

    The finding used here is deliberately one the shipped allow-list does not
    tolerate, so the two verdicts come from ``require_plausibility`` alone.
    """
    impossible = make_quote(ask=Decimal(data_config.plausibility.max_price) * 10)

    strict = QualityEngine(data_config).evaluate(impossible, context=QualityContext(now=data_now))
    relaxed_config = data_config.model_copy(
        update={
            "research_usability": data_config.research_usability.model_copy(
                update={"require_plausibility": False}
            )
        }
    )
    relaxed = QualityEngine(relaxed_config).evaluate(
        impossible, context=QualityContext(now=data_now)
    )

    assert not strict.research_usable
    assert relaxed.research_usable
    # In both cases the finding is recorded and the value untouched.
    assert strict.has(DataQualityIssue.IMPLAUSIBLE_PRICE)
    assert relaxed.has(DataQualityIssue.IMPLAUSIBLE_PRICE)


def test_the_report_is_machine_readable(quality_engine, make_quote, data_now) -> None:
    """A consumer filters on fields, not on prose."""
    report = quality_engine.evaluate(
        make_quote(bid=Decimal("600"), ask=Decimal("500")), context=QualityContext(now=data_now)
    )
    payload = report.model_dump(mode="json")

    assert set(payload) >= {
        "transport_valid",
        "schema_valid",
        "source_valid",
        "timestamp_valid",
        "freshness_valid",
        "completeness_valid",
        "plausibility_valid",
        "consistency_valid",
        "research_usable",
        "issues",
    }
    assert payload["issues"] == [DataQualityIssue.CROSSED_BID_ASK.value]


def test_attach_returns_a_copy_and_leaves_the_original_alone(
    quality_engine, make_quote, data_now
) -> None:
    original = make_quote(bid=Decimal("600"), ask=Decimal("500"))
    assessed = quality_engine.attach(original, context=QualityContext(now=data_now))

    assert assessed is not original
    assert assessed.quality.has(DataQualityIssue.CROSSED_BID_ASK)
    assert original.quality == DataQualityReport()
    assert assessed.bid == original.bid and assessed.ask == original.ask


# ---------------------------------------------------------------------------
# Tolerated plausibility findings
#
# An operator may decide that one specific plausibility finding does not make
# a record unfit to reason from. The list is explicit, it defaults to empty,
# and it is all-or-nothing. It is deliberately not `require_plausibility:
# false`, which switches off every plausibility check at once — the precedent
# is `observability/privacy.py`'s ALLOWED_EXACT_NAMES: a blunt guard needs an
# exception list, not a loosened pattern.
#
# The shipped list holds exactly one entry, SUSPICIOUS_VOLUME, because every
# quote from the real IBKR feed carries it (tick 74 arrives at a scale that
# varies per value) and no decision in this system is permitted to read that
# field: the liquidity floor names average_daily_volume, which arrives clean.
# ---------------------------------------------------------------------------
def _tolerating(data_config, *issues: DataQualityIssue):
    """The shipped policy with the allow-list replaced outright."""
    return data_config.model_copy(
        update={
            "research_usability": data_config.research_usability.model_copy(
                update={"tolerated_plausibility_issues": list(issues)}
            )
        }
    )


def test_the_shipped_policy_tolerates_the_corrupt_session_volume_and_nothing_else(
    data_config,
) -> None:
    """Each entry is a separate decision. There is currently exactly one."""
    assert data_config.research_usability.tolerated_plausibility_issues == [
        DataQualityIssue.SUSPICIOUS_VOLUME
    ]


def test_the_allow_list_is_empty_by_default() -> None:
    """Tolerating a defect is an operator decision, never a default."""
    from trading_system.infrastructure.settings import ResearchUsabilityConfig

    assert ResearchUsabilityConfig().tolerated_plausibility_issues == []


def test_a_tolerated_finding_leaves_the_record_usable_and_changes_nothing_else(
    data_config, make_quote, data_now
) -> None:
    """Only the derived verdict moves. The value, the flag and the dimension stay."""
    corrupt = Decimal("31367915626456")
    quote = make_quote(volume=corrupt, average_daily_volume=Decimal("52014430"))

    engine = QualityEngine(_tolerating(data_config, DataQualityIssue.SUSPICIOUS_VOLUME))
    report = engine.evaluate(quote, context=QualityContext(now=data_now))

    assert report.research_usable
    # Everything the finding said about the record is still on the record.
    assert not report.plausibility_valid
    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert report.plausibility_issues == [DataQualityIssue.SUSPICIOUS_VOLUME]
    assert report.classification is DataQuality.DEGRADED
    # And the raw value is untouched: tolerating is not rescaling.
    assert quote.volume == corrupt
    assert quote.volume != corrupt / Decimal(1_000_000)
    assert quote.average_daily_volume == Decimal("52014430")


def test_an_empty_allow_list_leaves_the_same_record_unusable(
    data_config, make_quote, data_now
) -> None:
    """The default behaviour is unchanged: nothing is tolerated implicitly."""
    quote = make_quote(volume=Decimal("31367915626456"))

    engine = QualityEngine(_tolerating(data_config))
    report = engine.evaluate(quote, context=QualityContext(now=data_now))

    assert not report.research_usable
    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)


def test_tolerating_a_volume_finding_does_not_tolerate_a_bad_price(
    data_config, make_quote, data_now
) -> None:
    """The guard against constraint 2: this is an allow-list, not an off switch.

    A crossed quote fails a different dimension entirely, and an implausible
    price fails the same one. Neither is on the list, so both are still
    rejected while the volume finding beside them is tolerated.
    """
    engine = QualityEngine(_tolerating(data_config, DataQualityIssue.SUSPICIOUS_VOLUME))
    corrupt = Decimal("31367915626456")

    crossed = engine.evaluate(
        make_quote(volume=corrupt, bid=Decimal("600"), ask=Decimal("500")),
        context=QualityContext(now=data_now),
    )
    assert crossed.has(DataQualityIssue.CROSSED_BID_ASK)
    assert not crossed.consistency_valid
    assert not crossed.research_usable

    impossible = engine.evaluate(
        make_quote(volume=corrupt, ask=Decimal(data_config.plausibility.max_price) * 10),
        context=QualityContext(now=data_now),
    )
    assert impossible.has(DataQualityIssue.IMPLAUSIBLE_PRICE)
    assert not impossible.research_usable


def test_one_untolerated_finding_fails_the_record_beside_a_tolerated_one(
    data_config, make_quote, data_now
) -> None:
    """All-or-nothing: the list is a subset test over every plausibility finding."""
    engine = QualityEngine(_tolerating(data_config, DataQualityIssue.SUSPICIOUS_VOLUME))
    report = engine.evaluate(
        make_quote(volume=Decimal("31367915626456"), ask=Decimal("-1")),
        context=QualityContext(now=data_now),
    )

    assert report.has(DataQualityIssue.SUSPICIOUS_VOLUME)
    assert report.has(DataQualityIssue.NEGATIVE_PRICE)
    assert set(report.plausibility_issues) == {
        DataQualityIssue.SUSPICIOUS_VOLUME,
        DataQualityIssue.NEGATIVE_PRICE,
    }
    assert not report.research_usable


def test_an_untolerated_finding_inside_a_chain_still_fails_the_snapshot(
    data_config, make_chain, make_option_quote, data_now
) -> None:
    """A nested finding is rolled up as the plausibility finding it was.

    Without that, a merged report would show ``plausibility_valid=False`` with
    no findings attributed to the dimension, and the subset test would pass
    vacuously — tolerating an impossible delta nobody listed.
    """
    engine = QualityEngine(_tolerating(data_config, DataQualityIssue.SUSPICIOUS_VOLUME))
    snapshot = OptionSnapshot(
        as_of=data_now,
        source=make_chain().source,
        underlying="SPY",
        chain=make_chain(),
        quotes=[make_option_quote(), make_option_quote(delta=Decimal("9"))],
    )

    report = engine.evaluate(snapshot, context=QualityContext(now=data_now))

    assert DataQualityIssue.IMPLAUSIBLE_GREEK in report.plausibility_issues
    assert not report.research_usable


def test_a_failed_dimension_with_no_attributed_finding_fails_closed(
    data_config, make_quote, data_now
) -> None:
    """An empty set satisfies a subset test vacuously; that must not read as tolerated.

    The case arises when a report stored before findings were attributed to
    dimensions is merged in.
    """
    from trading_system.data.quality import _Findings

    engine = QualityEngine(_tolerating(data_config, DataQualityIssue.SUSPICIOUS_VOLUME))
    findings = _Findings()
    findings.plausibility_valid = False

    assert not engine._plausibility_permits_research(findings)


def test_an_unknown_issue_name_fails_to_load(tmp_config_dir) -> None:
    """A typo in the allow-list must fail loudly, not tolerate nothing quietly."""
    from trading_system.infrastructure.settings import ConfigError, load_config

    data_yaml = tmp_config_dir / "data.yaml"
    data_yaml.write_text(
        data_yaml.read_text(encoding="utf-8").replace(
            "    - SUSPICIOUS_VOLUME", "    - SUSPICIOUS_VOLUMES"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)
