"""Data quality engine.

The engine's contract is narrow and absolute: **it describes records, it never
changes them**. Every check produces a finding attached to a
:class:`~trading_system.data.models.DataQualityReport`; the record's values —
including the implausible ones — are passed through byte for byte.

That rule exists because of a real observation. IBKR paper validation returned
an SPY volume that cannot be a real session volume. The tempting fixes are all
wrong:

* silently correcting it invents data;
* dropping the record destroys the evidence that the feed misbehaved;
* passing it through unflagged lets it reach a liquidity calculation.

So the value is preserved, ``plausibility_valid`` goes false,
``SUSPICIOUS_VOLUME`` is recorded, and ``research_usable`` goes false. A future
consumer filters on ``research_usable``; an auditor reads the raw value.

Quality has eight independent dimensions (Milestone 3 brief section 2.4)
because they fail independently: a record can be perfectly well-formed and
economically impossible at the same time, and collapsing that into one boolean
throws away the only information that distinguishes "broken pipeline" from
"broken feed".

Thresholds are configuration (``config/data.yaml``), never constants here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypeVar

from trading_system.data.hashing import stable_hash
from trading_system.data.models import (
    LIVE_ORIGINS,
    CorporateEvent,
    DataQualityReport,
    DataRecord,
    FundamentalSnapshot,
    MarketBar,
    MarketQuote,
    NewsArticle,
    OptionChain,
    OptionContract,
    OptionQuote,
    OptionSnapshot,
    RegulatoryEvent,
)
from trading_system.domain.enums import DataQualityIssue, SecurityType
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import DataConfig

__all__ = ["QualityContext", "QualityEngine", "content_fingerprint"]

RecordT = TypeVar("RecordT", bound=DataRecord)

#: Clock skew tolerated before a timestamp counts as being from the future.
#: Provider clocks are not our clock, and a two-second disagreement is not a
#: data-integrity problem.
_FUTURE_TOLERANCE = timedelta(seconds=5)


def content_fingerprint(record: DataRecord) -> str:
    """Stable fingerprint of a record's data content.

    Excludes retrieval metadata and the quality verdict, so re-fetching an
    unchanged record produces the same fingerprint — that is what makes
    duplicate detection meaningful rather than a timestamp comparison.
    """
    return stable_hash(record.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class QualityContext:
    """Everything the engine needs beyond the record itself."""

    now: datetime
    #: The provider the collector actually asked. A record claiming a different
    #: provider means the plumbing crossed wires somewhere.
    expected_provider: str | None = None
    #: Fingerprints already seen, for duplicate detection.
    known_fingerprints: frozenset[str] = frozenset()


@dataclass
class _Findings:
    """Accumulator for one record's evaluation."""

    transport_valid: bool = True
    schema_valid: bool = True
    source_valid: bool = True
    timestamp_valid: bool = True
    freshness_valid: bool = True
    completeness_valid: bool = True
    plausibility_valid: bool = True
    consistency_valid: bool = True
    issues: list[DataQualityIssue] = field(default_factory=list)
    #: The subset of ``issues`` raised by :meth:`fail_plausibility`. ``issues``
    #: is flat, so without this a finding cannot be traced to the dimension it
    #: failed — and the tolerated-issue allow-list has to know exactly that.
    plausibility_issues: list[DataQualityIssue] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def add(self, issue: DataQualityIssue, detail: str) -> None:
        """Record a finding. Informational unless a dimension is also cleared."""
        if issue not in self.issues:
            self.issues.append(issue)
        self.details.append(detail)

    def fail_source(self, issue: DataQualityIssue, detail: str) -> None:
        self.source_valid = False
        self.add(issue, detail)

    def fail_timestamp(self, issue: DataQualityIssue, detail: str) -> None:
        self.timestamp_valid = False
        self.add(issue, detail)

    def fail_completeness(self, issue: DataQualityIssue, detail: str) -> None:
        self.completeness_valid = False
        self.add(issue, detail)

    def fail_plausibility(self, issue: DataQualityIssue, detail: str) -> None:
        self.plausibility_valid = False
        if issue not in self.plausibility_issues:
            self.plausibility_issues.append(issue)
        self.add(issue, detail)

    def fail_consistency(self, issue: DataQualityIssue, detail: str) -> None:
        self.consistency_valid = False
        self.add(issue, detail)

    def merge(self, other: DataQualityReport, prefix: str) -> None:
        """Roll a nested record's verdict up into this one."""
        self.transport_valid &= other.transport_valid
        self.schema_valid &= other.schema_valid
        self.source_valid &= other.source_valid
        self.timestamp_valid &= other.timestamp_valid
        self.freshness_valid &= other.freshness_valid
        self.completeness_valid &= other.completeness_valid
        self.plausibility_valid &= other.plausibility_valid
        self.consistency_valid &= other.consistency_valid
        for issue in other.issues:
            if issue not in self.issues:
                self.issues.append(issue)
        for issue in other.plausibility_issues:
            if issue not in self.plausibility_issues:
                self.plausibility_issues.append(issue)
        self.details.extend(f"{prefix}: {detail}" for detail in other.details)


class QualityEngine:
    """Evaluates canonical records against configured quality rules."""

    def __init__(self, config: DataConfig, clock: Clock | None = None) -> None:
        self._config = config
        self._clock = clock or SystemClock()

    # --- public API --------------------------------------------------------
    def evaluate(
        self,
        record: DataRecord,
        *,
        context: QualityContext | None = None,
    ) -> DataQualityReport:
        """Assess one record. Returns a report; the record is untouched."""
        ctx = context or QualityContext(now=self._clock.now())
        findings = _Findings()

        self._check_provenance(record, ctx, findings)
        self._check_timestamps(record, ctx, findings)
        self._check_freshness(record, ctx, findings)
        self._check_duplicate(record, ctx, findings)
        self._check_payload(record, ctx, findings)

        return self._report(findings, ctx.now)

    def attach(
        self,
        record: RecordT,
        *,
        context: QualityContext | None = None,
    ) -> RecordT:
        """Return a copy of ``record`` carrying its quality report."""
        return record.with_quality(self.evaluate(record, context=context))

    def attach_all(
        self,
        records: Sequence[RecordT],
        *,
        context: QualityContext | None = None,
    ) -> list[RecordT]:
        """Evaluate a batch, detecting duplicates *within* the batch as well.

        Two identical records in one response is a provider bug worth surfacing,
        and it is invisible to per-record evaluation.
        """
        ctx = context or QualityContext(now=self._clock.now())
        seen = set(ctx.known_fingerprints)
        assessed: list[RecordT] = []
        for record in records:
            fingerprint = content_fingerprint(record)
            batch_context = QualityContext(
                now=ctx.now,
                expected_provider=ctx.expected_provider,
                known_fingerprints=frozenset(seen),
            )
            assessed.append(record.with_quality(self.evaluate(record, context=batch_context)))
            seen.add(fingerprint)
        return assessed

    # --- shared checks -----------------------------------------------------
    def _check_provenance(
        self, record: DataRecord, ctx: QualityContext, findings: _Findings
    ) -> None:
        source = record.source
        if not source.provider.strip():
            findings.fail_source(DataQualityIssue.MISSING_PROVENANCE, "provider is empty")

        if ctx.expected_provider is not None and source.provider != ctx.expected_provider:
            findings.fail_source(
                DataQualityIssue.SOURCE_MISMATCH,
                f"record claims provider {source.provider!r} but was collected from "
                f"{ctx.expected_provider!r}",
            )

        # A record cannot claim to be a live observation without any evidence
        # of when the source observed it. Data read back from storage that kept
        # a live origin would otherwise look realtime forever.
        if (
            source.origin in LIVE_ORIGINS
            and source.source_timestamp is None
            and source.observed_at is None
        ):
            findings.fail_source(
                DataQualityIssue.ORIGIN_MISREPRESENTED,
                f"origin {source.origin.value} claims a live observation but the source "
                f"supplied neither source_timestamp nor observed_at",
            )

    def _check_timestamps(
        self, record: DataRecord, ctx: QualityContext, findings: _Findings
    ) -> None:
        source = record.source
        horizon = ctx.now + _FUTURE_TOLERANCE

        if source.retrieved_at > horizon:
            findings.fail_timestamp(
                DataQualityIssue.FUTURE_TIMESTAMP,
                f"retrieved_at {source.retrieved_at.isoformat()} is in the future",
            )
        if record.as_of > horizon:
            findings.fail_timestamp(
                DataQualityIssue.FUTURE_TIMESTAMP,
                f"as_of {record.as_of.isoformat()} is in the future",
            )
        if source.source_timestamp is not None and source.source_timestamp > horizon:
            findings.fail_timestamp(
                DataQualityIssue.FUTURE_TIMESTAMP,
                f"source_timestamp {source.source_timestamp.isoformat()} is in the future",
            )
        if source.published_at is not None and source.published_at > source.retrieved_at:
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                "published_at is after retrieved_at: the source cannot have published "
                "an item after we fetched it",
            )

    def _check_freshness(
        self, record: DataRecord, ctx: QualityContext, findings: _Findings
    ) -> None:
        window = self._config.freshness.window_seconds(
            record.data_type.value, record.source.origin.value
        )
        age = record.source.age_seconds(ctx.now)
        if age > window:
            findings.freshness_valid = False
            findings.add(
                DataQualityIssue.STALE_DATA,
                f"age {age:.0f}s exceeds the {window}s window for "
                f"{record.data_type.value}/{record.source.origin.value}",
            )

    def _check_duplicate(
        self, record: DataRecord, ctx: QualityContext, findings: _Findings
    ) -> None:
        if not ctx.known_fingerprints:
            return
        if content_fingerprint(record) in ctx.known_fingerprints:
            findings.add(
                DataQualityIssue.DUPLICATE_RECORD,
                "an identical record has already been collected",
            )

    # --- per-type checks ---------------------------------------------------
    def _check_payload(self, record: DataRecord, ctx: QualityContext, findings: _Findings) -> None:
        if isinstance(record, MarketQuote):
            self._check_market_quote(record, findings)
        elif isinstance(record, MarketBar):
            self._check_market_bar(record, findings)
        elif isinstance(record, OptionQuote):
            self._check_option_quote(record, findings)
        elif isinstance(record, OptionChain):
            self._check_option_chain(record, findings)
        elif isinstance(record, OptionSnapshot):
            self._check_option_snapshot(record, ctx, findings)
        elif isinstance(record, NewsArticle):
            self._check_news(record, findings)
        elif isinstance(record, CorporateEvent):
            self._check_corporate_event(record, findings)
        elif isinstance(record, FundamentalSnapshot):
            self._check_fundamentals(record, findings)
        elif isinstance(record, RegulatoryEvent):
            self._check_regulatory(record, findings)

    def _check_prices(
        self,
        findings: _Findings,
        prices: Sequence[tuple[str, Decimal | None]],
    ) -> None:
        bounds = self._config.plausibility
        for name, value in prices:
            if value is None:
                continue
            if value < 0:
                findings.fail_plausibility(
                    DataQualityIssue.NEGATIVE_PRICE, f"{name} is negative: {value}"
                )
            elif value == 0:
                # Zero is a real IBKR "no data" marker, not a price. Flagged
                # rather than nulled: the raw value stays visible.
                findings.fail_plausibility(DataQualityIssue.ZERO_PRICE, f"{name} is zero")
            elif value > bounds.max_price:
                findings.fail_plausibility(
                    DataQualityIssue.IMPLAUSIBLE_PRICE,
                    f"{name} {value} exceeds max_price {bounds.max_price}",
                )
            elif value < bounds.min_price:
                findings.fail_plausibility(
                    DataQualityIssue.IMPLAUSIBLE_PRICE,
                    f"{name} {value} is below min_price {bounds.min_price}",
                )

    def _check_two_sided(
        self,
        findings: _Findings,
        bid: Decimal | None,
        ask: Decimal | None,
    ) -> None:
        """Bid/ask relationship. A crossed quote is flagged, never repaired.

        Swapping the two would turn a broken feed into a plausible-looking
        quote and destroy the evidence that anything was wrong.
        """
        if bid is None or ask is None:
            return
        if bid > ask:
            findings.fail_consistency(
                DataQualityIssue.CROSSED_BID_ASK,
                f"bid {bid} exceeds ask {ask}; values preserved as received, not swapped",
            )
            return
        if ask > 0:
            spread_pct = float((ask - bid) / ask * 100)
            if spread_pct > self._config.plausibility.max_spread_pct:
                findings.fail_plausibility(
                    DataQualityIssue.WIDE_SPREAD,
                    f"spread {spread_pct:.1f}% exceeds {self._config.plausibility.max_spread_pct}%",
                )

    def _check_volume(
        self,
        findings: _Findings,
        volume: Decimal | None,
        limit: int,
        label: str = "volume",
    ) -> None:
        if volume is None:
            return
        if volume < 0:
            findings.fail_plausibility(
                DataQualityIssue.NEGATIVE_VOLUME, f"{label} is negative: {volume}"
            )
        elif volume > limit:
            findings.fail_plausibility(
                DataQualityIssue.SUSPICIOUS_VOLUME,
                f"{label} {volume} exceeds the plausible maximum {limit}; raw value preserved",
            )

    def _check_market_quote(self, quote: MarketQuote, findings: _Findings) -> None:
        if not quote.has_price:
            findings.fail_completeness(
                DataQualityIssue.MISSING_REQUIRED_FIELD,
                "quote carries no bid, ask, last or close",
            )
        self._check_prices(
            findings,
            [
                ("bid", quote.bid),
                ("ask", quote.ask),
                ("last", quote.last),
                ("close", quote.close),
                ("open", quote.open),
                ("high", quote.high),
                ("low", quote.low),
            ],
        )
        self._check_two_sided(findings, quote.bid, quote.ask)
        self._check_volume(
            findings, quote.volume, self._config.plausibility.max_equity_daily_volume
        )
        # Checked on its own terms against the same bound, and labelled so the
        # finding names which field failed. The two are independent
        # observations: a corrupt session volume says nothing about tick 21,
        # and neither is ever substituted for the other.
        self._check_volume(
            findings,
            quote.average_daily_volume,
            self._config.plausibility.max_equity_daily_volume,
            label="average_daily_volume",
        )

        if quote.high is not None and quote.low is not None and quote.high < quote.low:
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                f"high {quote.high} is below low {quote.low}",
            )
        if quote.security_type is SecurityType.OTHER:
            findings.add(
                DataQualityIssue.UNEXPECTED_NULL,
                "security_type could not be mapped and fell back to OTHER",
            )

    def _check_market_bar(self, bar: MarketBar, findings: _Findings) -> None:
        if bar.close is None:
            findings.fail_completeness(DataQualityIssue.MISSING_REQUIRED_FIELD, "bar has no close")
        self._check_prices(
            findings,
            [("open", bar.open), ("high", bar.high), ("low", bar.low), ("close", bar.close)],
        )
        self._check_volume(findings, bar.volume, self._config.plausibility.max_equity_daily_volume)

        prices = [p for p in (bar.open, bar.high, bar.low, bar.close) if p is not None]
        if bar.high is not None and prices and bar.high < max(prices):
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                f"high {bar.high} is below another price in the same bar",
            )
        if bar.low is not None and prices and bar.low > min(prices):
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                f"low {bar.low} is above another price in the same bar",
            )

    def _check_contract(
        self, contract: OptionContract, reference: datetime, findings: _Findings
    ) -> None:
        bounds = self._config.plausibility

        if contract.right is None:
            findings.fail_completeness(
                DataQualityIssue.INVALID_OPTION_RIGHT, "option right is missing"
            )
        if contract.strike is None:
            findings.fail_completeness(DataQualityIssue.IMPLAUSIBLE_STRIKE, "strike is missing")
        elif contract.strike > bounds.max_strike:
            findings.fail_plausibility(
                DataQualityIssue.IMPLAUSIBLE_STRIKE,
                f"strike {contract.strike} exceeds max_strike {bounds.max_strike}",
            )

        if contract.multiplier is None:
            findings.add(DataQualityIssue.INVALID_MULTIPLIER, "multiplier is missing")
        elif contract.multiplier <= 0:
            findings.fail_plausibility(
                DataQualityIssue.INVALID_MULTIPLIER,
                f"multiplier {contract.multiplier} is not positive",
            )
        elif bounds.allowed_multipliers and contract.multiplier not in bounds.allowed_multipliers:
            findings.fail_plausibility(
                DataQualityIssue.INVALID_MULTIPLIER,
                f"multiplier {contract.multiplier} is not among the configured "
                f"{bounds.allowed_multipliers}",
            )

        if contract.expiration is None:
            findings.fail_completeness(
                DataQualityIssue.INVALID_EXPIRATION,
                "expiration is missing"
                + (
                    f"; broker sent {contract.raw_last_trade_date!r}"
                    if contract.raw_last_trade_date
                    else ""
                ),
            )
        else:
            reference_date = reference.date()
            if contract.expiration < reference_date:
                findings.fail_consistency(
                    DataQualityIssue.EXPIRED_CONTRACT,
                    f"expiration {contract.expiration.isoformat()} precedes the "
                    f"observation date {reference_date.isoformat()}",
                )
            elif (contract.expiration - reference_date).days > bounds.max_expiration_horizon_days:
                findings.fail_plausibility(
                    DataQualityIssue.INVALID_EXPIRATION,
                    f"expiration {contract.expiration.isoformat()} is more than "
                    f"{bounds.max_expiration_horizon_days} days out",
                )

    def _check_option_quote(self, quote: OptionQuote, findings: _Findings) -> None:
        bounds = self._config.plausibility
        self._check_contract(quote.contract, quote.as_of, findings)

        if not quote.has_price:
            findings.fail_completeness(
                DataQualityIssue.MISSING_REQUIRED_FIELD,
                "option quote carries no bid, ask, last or close",
            )
        self._check_prices(
            findings,
            [
                ("bid", quote.bid),
                ("ask", quote.ask),
                ("last", quote.last),
                ("close", quote.close),
            ],
        )
        self._check_two_sided(findings, quote.bid, quote.ask)
        self._check_volume(findings, quote.volume, bounds.max_option_contract_volume)
        self._check_volume(
            findings, quote.open_interest, bounds.max_open_interest, label="open_interest"
        )

        # Missing Greeks and open interest are routine, not defects: many
        # feeds simply do not carry them. Recorded as findings so a consumer
        # can filter, but they do not make the record incomplete.
        if quote.implied_volatility is None:
            findings.add(
                DataQualityIssue.MISSING_IMPLIED_VOLATILITY,
                "implied volatility unavailable (None means unknown, not zero)",
            )
        else:
            iv = float(quote.implied_volatility)
            if iv < bounds.min_implied_volatility or iv > bounds.max_implied_volatility:
                findings.fail_plausibility(
                    DataQualityIssue.IMPLAUSIBLE_IMPLIED_VOLATILITY,
                    f"implied volatility {iv} outside the plausible range "
                    f"[{bounds.min_implied_volatility}, {bounds.max_implied_volatility}]",
                )
        if quote.open_interest is None:
            findings.add(
                DataQualityIssue.MISSING_OPEN_INTEREST,
                "open interest unavailable (None means unknown, not zero)",
            )
        if quote.delta is not None and abs(float(quote.delta)) > bounds.max_abs_delta:
            findings.fail_plausibility(
                DataQualityIssue.IMPLAUSIBLE_GREEK,
                f"|delta| {quote.delta} exceeds {bounds.max_abs_delta}",
            )

    def _check_option_chain(self, chain: OptionChain, findings: _Findings) -> None:
        if not chain.expirations and not chain.contracts:
            findings.fail_completeness(
                DataQualityIssue.EMPTY_PAYLOAD, "chain lists neither expirations nor contracts"
            )
        if not chain.strikes and not chain.contracts:
            findings.fail_completeness(DataQualityIssue.EMPTY_PAYLOAD, "chain lists no strikes")

        contract_ids = [c.contract_id for c in chain.contracts if c.contract_id is not None]
        if len(set(contract_ids)) != len(contract_ids):
            findings.fail_consistency(
                DataQualityIssue.DUPLICATE_RECORD,
                "the chain contains more than one contract with the same broker id",
            )
        for contract in chain.contracts:
            self._check_contract(contract, chain.as_of, findings)
        if chain.multiplier is not None and chain.multiplier <= 0:
            findings.fail_plausibility(
                DataQualityIssue.INVALID_MULTIPLIER,
                f"chain multiplier {chain.multiplier} is not positive",
            )

    def _check_option_snapshot(
        self, snapshot: OptionSnapshot, ctx: QualityContext, findings: _Findings
    ) -> None:
        findings.merge(self.evaluate(snapshot.chain, context=ctx), "chain")
        for quote in snapshot.quotes:
            findings.merge(
                self.evaluate(quote, context=ctx),
                quote.contract.local_symbol or quote.contract.symbol,
            )
        if snapshot.underlying_quote is not None:
            findings.merge(self.evaluate(snapshot.underlying_quote, context=ctx), "underlying")

    def _check_news(self, article: NewsArticle, findings: _Findings) -> None:
        if not article.headline.strip():
            findings.fail_completeness(DataQualityIssue.MISSING_REQUIRED_FIELD, "headline is empty")
        if article.source.published_at is None:
            findings.fail_timestamp(
                DataQualityIssue.MISSING_REQUIRED_FIELD,
                "published_at is missing; a news item without a publication time cannot "
                "be placed on a timeline",
            )
        if not article.source.source_identifier:
            findings.fail_source(
                DataQualityIssue.MISSING_PROVENANCE,
                "no URL or source identifier: the article cannot be cited",
            )
        if not article.symbols and not article.entities:
            findings.add(
                DataQualityIssue.UNEXPECTED_NULL, "article names neither symbols nor entities"
            )

    def _check_corporate_event(self, event: CorporateEvent, findings: _Findings) -> None:
        # A future event_time is expected and correct: a calendar is about
        # things that have not happened yet.
        if event.announced_at is not None and event.announced_at > event.source.retrieved_at:
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                "announced_at is after retrieved_at",
            )
        if not event.confirmed:
            findings.add(
                DataQualityIssue.UNEXPECTED_NULL,
                "event date is unconfirmed and may move",
            )

    def _check_fundamentals(self, snapshot: FundamentalSnapshot, findings: _Findings) -> None:
        if (
            snapshot.period_end is not None
            and snapshot.period_start is not None
            and snapshot.period_end < snapshot.period_start
        ):
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                "period_end precedes period_start",
            )
        if snapshot.shares_outstanding is not None and snapshot.shares_outstanding <= 0:
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                f"shares_outstanding {snapshot.shares_outstanding} is not positive",
            )
        reported = [
            snapshot.revenue,
            snapshot.net_income,
            snapshot.eps_basic,
            snapshot.eps_diluted,
            snapshot.shares_outstanding,
            snapshot.market_capitalization,
        ]
        if all(value is None for value in reported):
            findings.fail_completeness(
                DataQualityIssue.EMPTY_PAYLOAD, "no fundamental figure was reported"
            )
        if not snapshot.filing_accession_number:
            findings.add(
                DataQualityIssue.MISSING_PROVENANCE,
                "no filing reference: the figures cannot be traced to a filing",
            )

    def _check_regulatory(self, event: RegulatoryEvent, findings: _Findings) -> None:
        if not event.raw_form.strip():
            findings.fail_completeness(
                DataQualityIssue.MISSING_REQUIRED_FIELD, "filing form is empty"
            )
        if not event.accession_number and not event.url:
            findings.fail_source(
                DataQualityIssue.MISSING_PROVENANCE,
                "neither an accession number nor a URL: the filing cannot be cited",
            )
        if event.period_of_report is not None and event.period_of_report > event.filed_at.date():
            findings.fail_consistency(
                DataQualityIssue.CONTRADICTORY_FIELDS,
                "period_of_report is after the filing date",
            )

    # --- verdict -----------------------------------------------------------
    def _plausibility_permits_research(self, findings: _Findings) -> bool:
        """Whether the plausibility dimension leaves the record usable.

        A clean record passes. A failing one passes only when *every*
        plausibility finding it carries appears in
        ``research_usability.tolerated_plausibility_issues`` — one untolerated
        finding fails the record even when a tolerated one sits beside it.

        The empty-findings case fails closed. A dimension marked invalid with
        nothing named cannot be reasoned about, and "no findings" would satisfy
        a subset test vacuously; that arises when an older stored report, from
        before findings were attributed to dimensions, is merged in.

        Nothing here changes ``plausibility_valid``, the findings or the raw
        values. Only the derived ``research_usable`` moves.
        """
        if findings.plausibility_valid:
            return True
        tolerated = set(self._config.research_usability.tolerated_plausibility_issues)
        if not tolerated or not findings.plausibility_issues:
            return False
        return all(issue in tolerated for issue in findings.plausibility_issues)

    def _report(self, findings: _Findings, now: datetime) -> DataQualityReport:
        policy = self._config.research_usability
        usable = all(
            [
                findings.transport_valid or not policy.require_transport,
                findings.schema_valid or not policy.require_schema,
                findings.source_valid or not policy.require_source,
                findings.timestamp_valid or not policy.require_timestamp,
                findings.completeness_valid or not policy.require_completeness,
                self._plausibility_permits_research(findings) or not policy.require_plausibility,
                findings.consistency_valid or not policy.require_consistency,
                findings.freshness_valid or not policy.require_freshness,
            ]
        )
        return DataQualityReport(
            transport_valid=findings.transport_valid,
            schema_valid=findings.schema_valid,
            source_valid=findings.source_valid,
            timestamp_valid=findings.timestamp_valid,
            freshness_valid=findings.freshness_valid,
            completeness_valid=findings.completeness_valid,
            plausibility_valid=findings.plausibility_valid,
            consistency_valid=findings.consistency_valid,
            research_usable=usable,
            issues=list(findings.issues),
            plausibility_issues=list(findings.plausibility_issues),
            details=list(findings.details),
            evaluated_at=now.astimezone(UTC),
        )
