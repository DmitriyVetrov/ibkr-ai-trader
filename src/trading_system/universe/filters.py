"""The deterministic pre-filter.

This module decides eligibility, and nothing above it may reverse that decision.
The AI agent runs *after* this and can only reorder what survives — if the
filter says ``REJECTED``, no ranking, no prompt and no model output can restore
the asset. That is the milestone's central rule, and it is enforced structurally:
:class:`~trading_system.universe.models.UniverseSelectionInput` refuses to carry
a rejected asset, so an excluded symbol is not merely unlikely to be ranked, it
is not expressible in the agent's input at all.

Three properties hold for every decision here:

* **Every rejection is machine-readable.** A code from
  :class:`~trading_system.domain.enums.UniverseRejectionReason`, plus a
  human-readable detail. "Why was AAPL not researched on 10 August" is answerable
  from the stored run.
* **Unavailable is not a failure of the asset.** ``PRICE_UNAVAILABLE`` and
  ``PRICE_BELOW_MINIMUM`` are different verdicts, because "we have no price" and
  "the price is too low" are different facts. Nothing is imputed to fill a gap.
* **Order is fixed.** Checks run in a defined sequence and stop at the first
  failure, so the same asset and the same data always produce the same reason —
  not whichever of several true reasons happened to be evaluated first.

Thresholds are configuration (``config/universe.yaml``), never constants here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    Optionability,
    UniverseEligibility,
    UniverseRejectionReason,
)
from trading_system.infrastructure.settings import OptionabilityPolicy, UniverseFilterConfig
from trading_system.universe.features import AssetEvidence, reference_price
from trading_system.universe.models import (
    CandidateAsset,
    CandidateProvenance,
    DataQualitySummary,
    FilterConfigSnapshot,
    RejectedAsset,
)

__all__ = [
    "FilterOutcome",
    "PreFilter",
    "collect_snapshot_ids",
    "filter_config_snapshot",
]


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    """One asset's verdict: exactly one of ``candidate`` or ``rejection``."""

    symbol: str
    candidate: CandidateAsset | None = None
    rejection: RejectedAsset | None = None

    @property
    def eligible(self) -> bool:
        return self.candidate is not None


def filter_config_snapshot(config: UniverseFilterConfig) -> FilterConfigSnapshot:
    """Echo the applied thresholds into a form the stored run can carry."""
    return FilterConfigSnapshot(
        allowed_security_types=[t.value for t in config.allowed_security_types],
        allowed_currencies=list(config.allowed_currencies),
        allowed_exchanges=list(config.allowed_exchanges),
        min_price=config.min_price,
        min_average_daily_volume=config.min_average_daily_volume,
        max_data_age_seconds=config.max_data_age_seconds,
        require_research_usable=config.require_research_usable,
        optionability_policy=config.optionability_policy.value,
        exclusions=list(config.exclusions),
        max_candidates=config.max_candidates,
        max_selected_assets=config.max_selected_assets,
    )


class PreFilter:
    """Applies the configured eligibility rules to gathered evidence."""

    def __init__(self, config: UniverseFilterConfig) -> None:
        self._config = config

    @property
    def config(self) -> UniverseFilterConfig:
        return self._config

    # --- whole-pool passes -------------------------------------------------
    def apply(
        self,
        symbols: Sequence[str],
        evidence: dict[str, AssetEvidence],
        as_of: datetime,
    ) -> list[FilterOutcome]:
        """Evaluate every symbol, in order, applying pool-level rules too.

        Duplicates and the candidate cap are pool-level: they cannot be decided
        by looking at one asset. Both produce a recorded rejection rather than a
        silently shorter list — a universe that quietly dropped candidates would
        misreport what was considered.
        """
        outcomes: list[FilterOutcome] = []
        seen: set[str] = set()

        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if symbol in seen:
                outcomes.append(
                    _reject(
                        symbol,
                        UniverseRejectionReason.DUPLICATE_SYMBOL,
                        "listed more than once by the universe source; "
                        "the first occurrence is the canonical candidate",
                        evidence.get(symbol),
                    )
                )
                continue
            seen.add(symbol)
            outcomes.append(self.evaluate(symbol, evidence.get(symbol), as_of))

        return self._apply_candidate_cap(outcomes)

    def _apply_candidate_cap(self, outcomes: list[FilterOutcome]) -> list[FilterOutcome]:
        """Enforce ``max_candidates`` deterministically and visibly.

        Truncation is by the source's own order, which is stable across runs.
        Ranking the overflow by liquidity would be a selection judgement, and
        selection is the agent's job — this only bounds how much it is shown.
        """
        cap = self._config.max_candidates
        eligible = [o for o in outcomes if o.eligible]
        if len(eligible) <= cap:
            return outcomes

        keep = {id(o) for o in eligible[:cap]}
        capped: list[FilterOutcome] = []
        for outcome in outcomes:
            if outcome.eligible and id(outcome) not in keep:
                assert outcome.candidate is not None  # narrowing; eligible implies present
                capped.append(
                    _reject(
                        outcome.symbol,
                        UniverseRejectionReason.CANDIDATE_LIMIT_EXCEEDED,
                        f"more than max_candidates ({cap}) assets passed the filters; "
                        f"dropped in the universe source's own order",
                        None,
                        optionability=outcome.candidate.optionability,
                        source=outcome.candidate.source,
                    )
                )
            else:
                capped.append(outcome)
        return capped

    # --- per-asset ---------------------------------------------------------
    def evaluate(
        self, symbol: str, evidence: AssetEvidence | None, as_of: datetime
    ) -> FilterOutcome:
        """Apply every rule to one asset, stopping at the first failure."""
        config = self._config

        if config.excludes(symbol):
            return _reject(
                symbol,
                UniverseRejectionReason.EXCLUDED_BY_CONFIGURATION,
                "listed in the configured exclusions; excluded before any data is consulted",
                evidence,
            )

        if evidence is None or evidence.quote is None:
            detail = (
                evidence.missing_reason
                if evidence is not None and evidence.missing_reason
                else "no market data has been collected for this symbol"
            )
            return _reject(symbol, UniverseRejectionReason.DATA_UNAVAILABLE, detail, evidence)

        quote = evidence.quote
        quality = evidence.quality

        if quote.security_type not in config.allowed_security_types:
            return _reject(
                symbol,
                UniverseRejectionReason.SECURITY_TYPE_NOT_ALLOWED,
                f"security type {quote.security_type.value} is not in "
                f"{[t.value for t in config.allowed_security_types]}",
                evidence,
            )

        currency = (quote.currency or "").upper()
        if currency not in {c.upper() for c in config.allowed_currencies}:
            return _reject(
                symbol,
                UniverseRejectionReason.CURRENCY_NOT_ALLOWED,
                f"currency {currency or 'unknown'} is not in {config.allowed_currencies}",
                evidence,
            )

        if config.allowed_exchanges:
            exchange = (quote.exchange or "").upper()
            if exchange not in {e.upper() for e in config.allowed_exchanges}:
                return _reject(
                    symbol,
                    UniverseRejectionReason.EXCHANGE_NOT_ALLOWED,
                    f"exchange {exchange or 'unknown'} is not in {config.allowed_exchanges}",
                    evidence,
                )

        if config.require_research_usable and not quality.research_usable:
            issues = ", ".join(i.value for i in quality.issues) or "no issue recorded"
            return _reject(
                symbol,
                UniverseRejectionReason.DATA_NOT_RESEARCH_USABLE,
                f"the data layer marked this record unusable for research ({issues}); "
                f"the value is preserved and flagged, never corrected",
                evidence,
            )

        age = evidence.age_seconds(as_of)
        if age is not None and age > config.max_data_age_seconds:
            return _reject(
                symbol,
                UniverseRejectionReason.DATA_STALE,
                f"market data is {age:.0f}s old, beyond the configured "
                f"{config.max_data_age_seconds}s",
                evidence,
            )

        price, price_field = reference_price(quote)
        if price is None:
            return _reject(
                symbol,
                UniverseRejectionReason.PRICE_UNAVAILABLE,
                "the visible quote carries no last, close, midpoint, bid or ask; "
                "no price is unavailable, not zero",
                evidence,
            )
        if price < config.min_price:
            return _reject(
                symbol,
                UniverseRejectionReason.PRICE_BELOW_MINIMUM,
                f"{price_field} price {price} is below the configured minimum {config.min_price}",
                evidence,
            )

        volume = quote.volume
        if config.min_average_daily_volume > 0:
            if volume is None:
                return _reject(
                    symbol,
                    UniverseRejectionReason.VOLUME_UNAVAILABLE,
                    "no underlying volume was reported; an unknown volume cannot be "
                    "read as meeting a liquidity floor",
                    evidence,
                )
            if volume < Decimal(config.min_average_daily_volume):
                return _reject(
                    symbol,
                    UniverseRejectionReason.VOLUME_BELOW_MINIMUM,
                    f"underlying volume {volume} is below the configured "
                    f"{config.min_average_daily_volume} (underlying liquidity, "
                    f"which does not establish option liquidity)",
                    evidence,
                )

        optionability = evidence.optionability
        rejection = self._check_optionability(symbol, optionability, evidence)
        if rejection is not None:
            return rejection

        return FilterOutcome(
            symbol=symbol,
            candidate=CandidateAsset(
                symbol=symbol,
                security_type=quote.security_type,
                currency=currency,
                exchange=quote.exchange,
                deterministic_eligibility=UniverseEligibility.ELIGIBLE,
                reference_price=price,
                reference_price_field=price_field,
                underlying_volume=volume,
                market_data_as_of=quote.as_of,
                market_data_age_seconds=age,
                market_data_origin=quote.source.origin,
                optionability=optionability,
                option_expiration_count=(
                    len(evidence.chain.expirations) if evidence.chain is not None else None
                ),
                option_strike_count=(
                    len(evidence.chain.strikes) if evidence.chain is not None else None
                ),
                data_quality=DataQualitySummary.of(quality),
                research_usable=quality.research_usable,
                source=_provenance(evidence),
            ),
        )

    def _check_optionability(
        self, symbol: str, optionability: Optionability, evidence: AssetEvidence
    ) -> FilterOutcome | None:
        """Apply the configured optionability policy.

        ``UNKNOWN`` is never rewritten into ``TRUE`` or ``FALSE``; the policy
        only decides whether an unresolved candidate may proceed, and the
        unresolved state travels with it either way.
        """
        policy = self._config.optionability_policy
        if policy is OptionabilityPolicy.IGNORED:
            return None
        if optionability is Optionability.FALSE:
            return _reject(
                symbol,
                UniverseRejectionReason.OPTIONABILITY_FALSE,
                "the collected chain lists no expirations, so no option trade is possible",
                evidence,
            )
        if optionability is Optionability.UNKNOWN and policy is OptionabilityPolicy.REQUIRED:
            return _reject(
                symbol,
                UniverseRejectionReason.OPTIONABILITY_UNKNOWN,
                "optionability has not been established and the policy requires it; "
                "UNKNOWN is not read as FALSE — collect an option chain for this symbol",
                evidence,
            )
        return None


def _provenance(evidence: AssetEvidence) -> CandidateProvenance:
    quote = evidence.quote
    assert quote is not None  # only called for assets that have a quote
    return CandidateProvenance(
        provider=quote.source.provider,
        retrieved_at=quote.source.retrieved_at,
        requested_provider=quote.source.requested_provider,
        source_tier=quote.source.source_tier,
        snapshot_ids=list(evidence.snapshot_ids),
    )


def _reject(
    symbol: str,
    reason: UniverseRejectionReason,
    detail: str,
    evidence: AssetEvidence | None,
    *,
    optionability: Optionability | None = None,
    source: CandidateProvenance | None = None,
) -> FilterOutcome:
    """Build a rejection that explains itself."""
    resolved_optionability = optionability
    if resolved_optionability is None:
        resolved_optionability = (
            evidence.optionability if evidence is not None else Optionability.UNKNOWN
        )
    resolved_source = source
    if resolved_source is None and evidence is not None and evidence.quote is not None:
        resolved_source = _provenance(evidence)

    return FilterOutcome(
        symbol=symbol,
        rejection=RejectedAsset(
            symbol=symbol,
            deterministic_eligibility=UniverseEligibility.REJECTED,
            reason=reason,
            detail=detail,
            optionability=resolved_optionability,
            source=resolved_source,
        ),
    )


def collect_snapshot_ids(outcomes: Iterable[FilterOutcome]) -> list[str]:
    """Every data snapshot the run's decisions rest on, deduplicated and sorted.

    Rejections contribute too: "AAPL was excluded because *this* snapshot said
    its data was unusable" is as much a part of the audit trail as a selection.
    """
    ids: set[str] = set()
    for outcome in outcomes:
        if outcome.candidate is not None:
            ids.update(outcome.candidate.source.snapshot_ids)
        elif outcome.rejection is not None and outcome.rejection.source is not None:
            ids.update(outcome.rejection.source.snapshot_ids)
    return sorted(ids)
