"""Preconditions that must hold before any limit is worth checking.

A limit check answers *may we commit this much?* A guard answers the prior
question: *is this input fit to be reasoned about at all?* Keeping them apart
matters, because the two fail for different reasons and want different fixes —
"the campaign is full" is a market outcome, "the price is missing" is a data
problem, and reporting the second as the first would send someone to widen a
limit that was never the constraint.

Every guard here fails **closed**. Unknown broker state, an unusable price, a
stale quote, a currency nobody configured a rate for, a record that was not
knowable at the decision instant: each ends the candidate with a named reason
and no authorisation. None of them repairs its input.

Three rules are enforced here that the rest of the system depends on:

* **Missing is not zero.** A price that was never reported is
  ``PRICE_UNAVAILABLE``, never ``0`` and never a midpoint reconstructed from
  one side of the market.
* **Upstream quality verdicts are read, never re-graded.** Milestone 3 owns the
  judgement of whether a record is usable. If it says no, this says no.
* **Retrieval binds.** A quote captured after the decision instant was not
  available at that instant, however recent the market it describes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import (
    MaxLossBasis,
    RiskCheckOutcome,
    RiskLimitScope,
    RiskReasonCode,
    TradingMode,
)
from trading_system.risk.models import (
    AccountSnapshot,
    AllocationCandidate,
    CampaignSnapshot,
    RiskCheck,
    RiskLimits,
)

__all__ = [
    "check_account_snapshot",
    "check_currency",
    "check_data_quality",
    "check_point_in_time",
    "check_price",
    "check_trading_mode",
    "unit_max_loss",
]


def _passed(
    name: str,
    scope: RiskLimitScope,
    *,
    actual: str | None = None,
    limit: str | None = None,
    detail: str | None = None,
) -> RiskCheck:
    return RiskCheck(
        name=name,
        scope=scope,
        outcome=RiskCheckOutcome.PASS,
        actual=actual,
        limit=limit,
        detail=detail,
    )


def _failed(
    name: str,
    scope: RiskLimitScope,
    reason: RiskReasonCode,
    detail: str,
    *,
    actual: str | None = None,
    limit: str | None = None,
) -> RiskCheck:
    return RiskCheck(
        name=name,
        scope=scope,
        outcome=RiskCheckOutcome.FAIL,
        reason_code=reason,
        detail=detail,
        actual=actual,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Mode and broker state
# ---------------------------------------------------------------------------
def check_trading_mode(mode: TradingMode, *, live_guards_satisfied: bool) -> list[RiskCheck]:
    """Whether this mode may authorise capital at all.

    ``LIVE`` without its explicit guards is refused here as well as at settings
    construction. Belt and braces on purpose: this is the check that appears on
    the stored authorisation, so a past decision records that the guard was in
    force rather than leaving a reader to infer it from a version number.
    """
    checks = [
        _passed(
            "trading_mode", RiskLimitScope.GLOBAL, actual=mode.value, limit="PAPER|DRY_RUN|LIVE"
        )
    ]
    if mode is TradingMode.LIVE and not live_guards_satisfied:
        checks.append(
            _failed(
                "live_mode_guards",
                RiskLimitScope.GLOBAL,
                RiskReasonCode.LIVE_MODE_GUARD_NOT_SATISFIED,
                "LIVE requires an explicitly confirmed, signed-off readiness checklist",
                actual="not satisfied",
                limit="satisfied",
            )
        )
    return checks


def check_account_snapshot(
    snapshot: AccountSnapshot | None,
    limits: RiskLimits,
    *,
    as_of: datetime,
) -> list[RiskCheck]:
    """Whether the account state is present, current and internally coherent.

    An absent snapshot is not "assume the money is there". The campaign
    envelope says what *policy* permits; only the account says what actually
    exists, and the more restrictive of the two is what may be committed.
    """
    if snapshot is None:
        if not limits.require_account_snapshot:
            return [
                RiskCheck(
                    name="account_snapshot",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.NOT_EVALUATED,
                    detail=(
                        "no account snapshot, and configuration does not require one; the "
                        "broker's own view of available capital was not considered"
                    ),
                )
            ]
        return [
            _failed(
                "account_snapshot",
                RiskLimitScope.CAMPAIGN,
                RiskReasonCode.ACCOUNT_SNAPSHOT_UNAVAILABLE,
                "no account snapshot is available; capture one with 'risk capture-account'",
            )
        ]

    checks: list[RiskCheck] = []
    age = snapshot.age_seconds(as_of)
    if age > limits.max_account_snapshot_age_seconds:
        checks.append(
            _failed(
                "account_snapshot_age",
                RiskLimitScope.CAMPAIGN,
                RiskReasonCode.ACCOUNT_SNAPSHOT_STALE,
                "the newest account snapshot predates the decision by more than policy allows",
                actual=f"{age:.0f}s",
                limit=f"{limits.max_account_snapshot_age_seconds}s",
            )
        )
    else:
        checks.append(
            _passed(
                "account_snapshot_age",
                RiskLimitScope.CAMPAIGN,
                actual=f"{age:.0f}s",
                limit=f"{limits.max_account_snapshot_age_seconds}s",
            )
        )

    if snapshot.spendable is None:
        checks.append(
            _failed(
                "account_spendable_reported",
                RiskLimitScope.CAMPAIGN,
                RiskReasonCode.INVALID_ACCOUNT_SNAPSHOT,
                (
                    "the broker reported no cash, buying power or available funds. An "
                    "unreported balance is unknown, not large"
                ),
            )
        )
    else:
        checks.append(
            _passed(
                "account_spendable_reported",
                RiskLimitScope.CAMPAIGN,
                actual=str(snapshot.spendable),
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Point in time
# ---------------------------------------------------------------------------
def check_point_in_time(
    candidate: AllocationCandidate,
    campaign: CampaignSnapshot,
    account: AccountSnapshot | None,
    *,
    as_of: datetime,
) -> list[RiskCheck]:
    """Whether every input was actually knowable at the decision instant.

    A look-ahead leak is a correctness bug, never a market outcome, so it is
    reported as its own reason code rather than folded into a data-quality
    failure. Nothing is filtered or repaired: the candidate fails, and the
    offending record stays exactly as it was stored.
    """
    offenders: list[str] = []
    if not candidate.known_at(as_of):
        offenders.append(f"candidate {candidate.opportunity_id}")
    if campaign.as_of > as_of:
        offenders.append(f"campaign snapshot ({campaign.as_of.isoformat()})")
    if account is not None and not account.known_at(as_of):
        offenders.append(f"account snapshot {account.snapshot_id}")
    for position in campaign.open_positions:
        if not position.known_at(as_of):
            offenders.append(f"reservation {position.opportunity_id}")

    if offenders:
        return [
            _failed(
                "point_in_time",
                RiskLimitScope.GLOBAL,
                RiskReasonCode.POINT_IN_TIME_ERROR,
                f"not knowable at {as_of.isoformat()}: {', '.join(offenders)}",
                actual=f"{len(offenders)} record(s) from the future",
                limit="0",
            )
        ]
    return [_passed("point_in_time", RiskLimitScope.GLOBAL, actual="0", limit="0")]


# ---------------------------------------------------------------------------
# Price and data quality
# ---------------------------------------------------------------------------
def check_price(
    candidate: AllocationCandidate, limits: RiskLimits, *, as_of: datetime
) -> list[RiskCheck]:
    """Whether the unit cost is present, positive, attributable and current.

    The rule that matters: a missing price is never replaced. Not with zero,
    not with the last thing we saw, not with a midpoint derived from one side
    of the market. A structure whose cost is unknown cannot be sized, and
    pretending otherwise is how a system buys something at a price nobody
    quoted.
    """
    price = candidate.price
    checks: list[RiskCheck] = []

    if not price.available or price.unit_cost is None:
        return [
            _failed(
                "unit_cost_available",
                RiskLimitScope.POSITION,
                RiskReasonCode.PRICE_UNAVAILABLE,
                price.unavailable_reason or "no unit cost was established for this structure",
            )
        ]

    if price.unit_cost <= 0:
        return [
            _failed(
                "unit_cost_positive",
                RiskLimitScope.STRATEGY,
                RiskReasonCode.INVALID_PRICE,
                (
                    "a structure that costs nothing or less has not been priced; a zero or "
                    "negative debit is a data fault, not a free option"
                ),
                actual=str(price.unit_cost),
                limit="> 0",
            )
        ]
    checks.append(
        _passed("unit_cost_positive", RiskLimitScope.STRATEGY, actual=str(price.unit_cost))
    )

    multipliers = {leg.multiplier for leg in candidate.legs}
    if len(multipliers) != 1 or min(multipliers) < 1:
        return [
            *checks,
            _failed(
                "contract_multiplier",
                RiskLimitScope.STRATEGY,
                RiskReasonCode.INVALID_MULTIPLIER,
                "the legs do not share one valid contract multiplier",
                actual=", ".join(str(m) for m in sorted(multipliers)),
                limit="one value >= 1",
            ),
        ]
    checks.append(
        _passed("contract_multiplier", RiskLimitScope.STRATEGY, actual=str(candidate.multiplier))
    )

    unpriced = [leg.leg_index for leg in candidate.legs if leg.quote_as_of is None]
    if unpriced:
        checks.append(
            _failed(
                "price_attributable_to_contract",
                RiskLimitScope.STRATEGY,
                RiskReasonCode.INVALID_PRICE,
                (
                    f"leg(s) {unpriced} carry no quote of their own, so the unit cost cannot "
                    f"be attributed to the selected contracts"
                ),
                actual=f"{len(unpriced)} unquoted leg(s)",
                limit="0",
            )
        )
    else:
        checks.append(_passed("price_attributable_to_contract", RiskLimitScope.STRATEGY))

    if price.quote_as_of is None:
        checks.append(
            _failed(
                "quote_freshness",
                RiskLimitScope.GLOBAL,
                RiskReasonCode.STALE_MARKET_DATA,
                "the unit cost carries no quote timestamp, so its age cannot be established",
            )
        )
    else:
        age = (as_of - price.quote_as_of).total_seconds()
        if age > limits.max_market_data_age_seconds:
            checks.append(
                _failed(
                    "quote_freshness",
                    RiskLimitScope.GLOBAL,
                    RiskReasonCode.STALE_MARKET_DATA,
                    (
                        "the price behind this candidate was already stale at the decision "
                        "instant; stale data means no trade"
                    ),
                    actual=f"{age:.0f}s",
                    limit=f"{limits.max_market_data_age_seconds}s",
                )
            )
        else:
            checks.append(
                _passed(
                    "quote_freshness",
                    RiskLimitScope.GLOBAL,
                    actual=f"{age:.0f}s",
                    limit=f"{limits.max_market_data_age_seconds}s",
                )
            )

    spread = price.max_leg_spread_pct
    ceiling = candidate.risk_profile.max_bid_ask_spread_pct
    if spread is None:
        checks.append(
            RiskCheck(
                name="bid_ask_spread",
                scope=RiskLimitScope.STRATEGY,
                outcome=RiskCheckOutcome.NOT_EVALUATED,
                limit=f"{ceiling}%",
                detail=(
                    "no two-sided quote on at least one leg, so the spread could not be "
                    "measured. An unmeasured spread is not a narrow one"
                ),
            )
        )
    elif spread > ceiling:
        checks.append(
            _failed(
                "bid_ask_spread",
                RiskLimitScope.STRATEGY,
                RiskReasonCode.SPREAD_TOO_WIDE,
                "the widest leg's spread exceeds what this strategy permits",
                actual=f"{spread:.2f}%",
                limit=f"{ceiling}%",
            )
        )
    else:
        checks.append(
            _passed(
                "bid_ask_spread",
                RiskLimitScope.STRATEGY,
                actual=f"{spread:.2f}%",
                limit=f"{ceiling}%",
            )
        )

    return checks


def check_data_quality(candidate: AllocationCandidate) -> list[RiskCheck]:
    """Whether the data layer judged these inputs usable.

    Read, never re-derived. Milestone 3 owns data quality and has eight
    independent dimensions to say so with; a risk engine that re-graded a
    record here would be a second opinion nobody asked for and would let a
    record be unusable for research and usable for spending money.
    """
    if not candidate.research_usable:
        return [
            _failed(
                "upstream_data_quality",
                RiskLimitScope.GLOBAL,
                RiskReasonCode.DATA_QUALITY_FAILED,
                (
                    f"the data behind this candidate was judged not research-usable "
                    f"(classification {candidate.data_quality.value}); that verdict is not "
                    f"re-graded here"
                ),
                actual=candidate.data_quality.value,
                limit="research_usable",
            )
        ]
    return [
        _passed(
            "upstream_data_quality",
            RiskLimitScope.GLOBAL,
            actual=candidate.data_quality.value,
            limit="research_usable",
        )
    ]


def check_currency(candidate: AllocationCandidate, limits: RiskLimits) -> list[RiskCheck]:
    """Whether every figure is in a currency the campaign can spend.

    No arbitrary rate is ever applied. Either a deterministic conversion is
    configured, or a mismatch is a rejection — because a position sized through
    an invented exchange rate is sized wrongly by an amount nobody recorded.
    """
    accepted = set(limits.accepted_currencies)
    currencies = candidate.currencies
    price_currency = candidate.price.currency
    if price_currency:
        currencies = currencies | {price_currency}

    if len(currencies) > 1:
        return [
            _failed(
                "currency",
                RiskLimitScope.CAMPAIGN,
                RiskReasonCode.CURRENCY_MISMATCH,
                "the legs of one structure are quoted in more than one currency",
                actual=", ".join(sorted(currencies)),
                limit="one currency",
            )
        ]

    foreign = sorted(currencies - accepted)
    if not foreign:
        return [
            _passed(
                "currency",
                RiskLimitScope.CAMPAIGN,
                actual=", ".join(sorted(currencies)) or "unstated",
                limit=", ".join(sorted(accepted)),
            )
        ]

    if limits.allow_currency_conversion:
        return [
            _failed(
                "currency",
                RiskLimitScope.CAMPAIGN,
                RiskReasonCode.FX_CONVERSION_UNAVAILABLE,
                (
                    "conversion is permitted by policy but no deterministic rate source is "
                    "configured; converting at an arbitrary rate would invent a price"
                ),
                actual=", ".join(foreign),
                limit=", ".join(sorted(accepted)),
            )
        ]
    return [
        _failed(
            "currency",
            RiskLimitScope.CAMPAIGN,
            RiskReasonCode.CURRENCY_MISMATCH,
            (
                f"priced in {', '.join(foreign)}, which the campaign does not accept as its "
                f"own currency, and conversion is disabled"
            ),
            actual=", ".join(foreign),
            limit=", ".join(sorted(accepted)),
        )
    ]


# ---------------------------------------------------------------------------
# Maximum loss
# ---------------------------------------------------------------------------
def unit_max_loss(candidate: AllocationCandidate) -> tuple[Decimal | None, RiskCheck]:
    """The most one unit of this structure can lose, and the check that says so.

    The number comes from the strategy's declared
    :class:`~trading_system.domain.enums.MaxLossBasis`, not from a formula this
    module chose. For ``NET_DEBIT_PAID`` the answer is the debit: a bought
    option cannot lose more than it cost, whatever it was bought for. For
    anything else the answer is ``None`` and the candidate is refused — an
    unquantified loss is not a small one, and sizing a credit structure as
    though the premium bounded the loss would be exactly backwards.
    """
    basis = candidate.risk_profile.max_loss_basis
    price = candidate.price

    if basis is MaxLossBasis.NET_DEBIT_PAID:
        if price.unit_cost is None:
            return None, _failed(
                "max_loss_model",
                RiskLimitScope.STRATEGY,
                RiskReasonCode.PRICE_UNAVAILABLE,
                "maximum loss is the debit paid, and no debit was established",
            )
        return price.unit_cost, _passed(
            "max_loss_model",
            RiskLimitScope.STRATEGY,
            actual=str(price.unit_cost),
            detail=f"{basis.value}: the most a bought structure can lose is what it cost",
        )

    return None, _failed(
        "max_loss_model",
        RiskLimitScope.STRATEGY,
        RiskReasonCode.MAX_LOSS_UNDEFINED,
        (
            f"{candidate.strategy.value} declares its maximum loss as {basis.value}, which "
            f"this engine cannot compute from the candidate alone. Fail closed: an "
            f"unquantified loss is not a bounded one"
        ),
        actual=basis.value,
        limit=MaxLossBasis.NET_DEBIT_PAID.value,
    )
