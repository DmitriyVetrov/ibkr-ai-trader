"""The deterministic risk engine.

Answers exactly one question — *is this proposed position permitted?* — and
deliberately not the next one. How many units the remaining budget supports is
the allocation engine's answer, and separating them is what lets a candidate be
refused before any quantity is calculated: a rejection that arrives as "we
computed a quantity of zero" tells nobody which limit bound.

Everything about this class is a consequence of one requirement — a stored
verdict must be reproducible years later, after the configuration, the data and
the broker have all moved on:

* **It is a pure function of its arguments.** No clock, no network, no broker,
  no filesystem, no repository, no random source and no model. The decision
  instant is passed in. Two calls with the same inputs return the same verdict,
  byte for byte, including the order of the checks.
* **It holds no state.** :meth:`RiskEngine.evaluate` reads its arguments and
  returns; there is nothing to reset between candidates and nothing one
  evaluation can leave behind for the next.
* **It cannot reach a broker.** Account state arrives as a stored
  :class:`~trading_system.risk.models.AccountSnapshot`. That is not only an
  architectural preference: Milestone 2 established that a second uncached
  round trip on one IBKR connection can go unanswered indefinitely, so a risk
  calculation that fetched its own account state could hang the process at
  precisely the wrong moment.
* **No agent can influence it.** There is no field on any input for a
  rationale, a confidence the model asserted about itself, or a prompt. The
  research and strategy confidences that *are* carried arrive as bands from
  validated upstream artifacts, and they affect ordering — never permission.

The verdict is a list of checks rather than a verdict plus prose. Every
rejection therefore names the limit, the actual value and the limit value, and
the human-readable explanation is generated from those. Prose is a rendering of
the decision; it is never the decision.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    RiskCheckOutcome,
    RiskLimitScope,
    RiskOutcome,
    RiskReasonCode,
    TradingMode,
)
from trading_system.risk import guards
from trading_system.risk.exposure import exposure_for
from trading_system.risk.models import (
    ALLOCATION_SCHEMA_VERSION,
    AccountSnapshot,
    AllocationCandidate,
    CampaignSnapshot,
    RiskCheck,
    RiskEvaluation,
    RiskLimits,
)

__all__ = ["RiskEngine", "evaluation_identifier"]


def evaluation_identifier(
    *,
    opportunity_id: str,
    as_of: datetime,
    outcome: RiskOutcome,
    reason_codes: list[RiskReasonCode],
    schema_version: str = ALLOCATION_SCHEMA_VERSION,
) -> str:
    """Derive an evaluation's id from what it decided.

    Excludes every runtime measurement, exactly as the Milestone 5 and 6
    identifiers do: two runs reaching an identical verdict over identical
    inputs produce an identical id, so a re-run is recognisable as the same
    event rather than recorded as a second one.
    """
    digest = stable_hash(
        [
            "RISK_EVALUATION",
            schema_version,
            opportunity_id,
            as_of.isoformat(),
            outcome.value,
            sorted(code.value for code in reason_codes),
        ]
    )
    return f"risk-{digest[:20]}"


class RiskEngine:
    """Deterministic permission. No agent may override a rejection.

    Constructed from limits alone. There is nowhere in this class a broker, a
    repository or a model could be reached from, which is what makes "the risk
    engine never calls IBKR" a structural fact rather than a rule someone has
    to remember.
    """

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    # --- the verdict -------------------------------------------------------
    def evaluate(
        self,
        candidate: AllocationCandidate,
        campaign: CampaignSnapshot,
        *,
        as_of: datetime,
        account: AccountSnapshot | None = None,
        trading_mode: TradingMode = TradingMode.PAPER,
        live_guards_satisfied: bool = False,
        new_positions_this_run: int = 0,
    ) -> RiskEvaluation:
        """Decide whether one candidate may be funded at all.

        ``APPROVED`` means *permitted*, never *funded*: it says nothing about
        how many units are affordable. A candidate can be approved here and
        still end as ``NO_TRADE`` at allocation because a whole contract no
        longer fits — which is a different fact, recorded differently.
        """
        checks: list[RiskCheck] = []
        limits = self._limits

        # Preconditions first. A limit check on an unusable input measures
        # nothing, and reporting "the campaign is full" when the real problem
        # is a missing price sends someone to the wrong file.
        checks.extend(
            guards.check_trading_mode(trading_mode, live_guards_satisfied=live_guards_satisfied)
        )
        checks.extend(guards.check_point_in_time(candidate, campaign, account, as_of=as_of))
        checks.extend(guards.check_data_quality(candidate))
        checks.extend(guards.check_currency(candidate, limits))
        checks.extend(guards.check_account_snapshot(account, limits, as_of=as_of))
        checks.extend(guards.check_price(candidate, limits, as_of=as_of))

        unit_cost = candidate.price.unit_cost
        max_loss, max_loss_check = guards.unit_max_loss(candidate)
        checks.append(max_loss_check)

        checks.append(self._score_check(candidate))
        checks.append(self._duplicate_check(candidate, campaign))
        checks.extend(self._position_count_checks(candidate, campaign, new_positions_this_run))
        checks.append(self._daily_loss_check(campaign))

        # Capacity checks need a price and a loss model. Without them there is
        # nothing to compare against a limit, and inventing a figure to keep
        # the check list uniform would be the one thing this engine must never
        # do — so they are skipped, and the preconditions above have already
        # failed the candidate.
        if unit_cost is not None and max_loss is not None:
            checks.extend(self._capacity_checks(candidate, campaign, account, unit_cost, max_loss))

        failed = [check for check in checks if check.outcome is RiskCheckOutcome.FAIL]

        if failed:
            outcome = RiskOutcome.REJECTED
            reason_codes = _ordered_reasons(failed)
        else:
            outcome = RiskOutcome.APPROVED
            reason_codes = [RiskReasonCode.OK]

        exposure = exposure_for(
            campaign,
            candidate,
            capital=Decimal("0"),
            risk=Decimal("0"),
        )

        return RiskEvaluation(
            evaluation_id=evaluation_identifier(
                opportunity_id=candidate.opportunity_id,
                as_of=as_of,
                outcome=outcome,
                reason_codes=reason_codes,
            ),
            opportunity_id=candidate.opportunity_id,
            symbol=candidate.symbol,
            as_of=as_of,
            outcome=outcome,
            reason_codes=reason_codes,
            checks=checks,
            limits=limits,
            exposure=exposure,
            trading_mode=trading_mode,
            unit_cost=unit_cost,
            unit_max_loss=max_loss,
            max_loss_basis=candidate.risk_profile.max_loss_basis,
            currency=candidate.price.currency,
            campaign_snapshot_as_of=campaign.as_of,
            account_snapshot_id=account.snapshot_id if account else None,
        )

    # --- individual checks -------------------------------------------------
    def _score_check(self, candidate: AllocationCandidate) -> RiskCheck:
        floor = self._limits.min_opportunity_score
        score = candidate.score.total
        if score < floor:
            return RiskCheck(
                name="min_opportunity_score",
                scope=RiskLimitScope.CAMPAIGN,
                outcome=RiskCheckOutcome.FAIL,
                reason_code=RiskReasonCode.BELOW_MIN_OPPORTUNITY_SCORE,
                actual=f"{score:.2f}",
                limit=f"{floor:.2f}",
                detail="ranked, but below the configured floor for deserving capital",
            )
        return RiskCheck(
            name="min_opportunity_score",
            scope=RiskLimitScope.CAMPAIGN,
            outcome=RiskCheckOutcome.PASS,
            actual=f"{score:.2f}",
            limit=f"{floor:.2f}",
        )

    def _duplicate_check(
        self, candidate: AllocationCandidate, campaign: CampaignSnapshot
    ) -> RiskCheck:
        """Idempotency: one opportunity, one reservation.

        The opportunity id is derived from the research report, the strategy
        decision and the contract selection, so re-running the stage over the
        same upstream artifacts recognises the existing reservation instead of
        committing the capital a second time.
        """
        existing = campaign.holds(candidate.opportunity_id)
        if existing is not None:
            return RiskCheck(
                name="duplicate_opportunity",
                scope=RiskLimitScope.CAMPAIGN,
                outcome=RiskCheckOutcome.FAIL,
                reason_code=RiskReasonCode.DUPLICATE_OPPORTUNITY,
                actual=existing.allocation_id,
                limit="no existing reservation",
                detail=(
                    f"this exact opportunity already holds {existing.capital_committed} of "
                    f"authorised capital under allocation {existing.allocation_id}"
                ),
            )
        return RiskCheck(
            name="duplicate_opportunity",
            scope=RiskLimitScope.CAMPAIGN,
            outcome=RiskCheckOutcome.PASS,
            actual="none",
            limit="no existing reservation",
        )

    def _position_count_checks(
        self,
        candidate: AllocationCandidate,
        campaign: CampaignSnapshot,
        new_positions_this_run: int,
    ) -> list[RiskCheck]:
        limits = self._limits
        checks: list[RiskCheck] = []

        current = campaign.position_count
        if current + 1 > limits.max_open_positions:
            checks.append(
                RiskCheck(
                    name="max_open_positions",
                    scope=limits.scopes.get("max_open_positions", RiskLimitScope.CAMPAIGN),
                    outcome=RiskCheckOutcome.FAIL,
                    reason_code=RiskReasonCode.MAX_POSITIONS_EXCEEDED,
                    actual=str(current + 1),
                    limit=str(limits.max_open_positions),
                    detail="the campaign already holds as many positions as it may",
                )
            )
        else:
            checks.append(
                RiskCheck(
                    name="max_open_positions",
                    scope=limits.scopes.get("max_open_positions", RiskLimitScope.CAMPAIGN),
                    outcome=RiskCheckOutcome.PASS,
                    actual=str(current + 1),
                    limit=str(limits.max_open_positions),
                )
            )

        in_underlying = campaign.positions_in(candidate.symbol)
        if in_underlying + 1 > limits.max_positions_per_underlying:
            checks.append(
                RiskCheck(
                    name="max_positions_per_underlying",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.FAIL,
                    reason_code=RiskReasonCode.MAX_POSITIONS_PER_UNDERLYING_EXCEEDED,
                    actual=str(in_underlying + 1),
                    limit=str(limits.max_positions_per_underlying),
                    detail=(
                        f"{candidate.symbol} already carries {in_underlying} structure(s); two "
                        f"structures on one name are one bet held twice, and correlation is "
                        f"not modelled here"
                    ),
                )
            )
        else:
            checks.append(
                RiskCheck(
                    name="max_positions_per_underlying",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.PASS,
                    actual=str(in_underlying + 1),
                    limit=str(limits.max_positions_per_underlying),
                )
            )

        if new_positions_this_run + 1 > limits.max_new_positions_per_run:
            checks.append(
                RiskCheck(
                    name="max_new_positions_per_run",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.FAIL,
                    reason_code=RiskReasonCode.MAX_NEW_POSITIONS_PER_RUN_REACHED,
                    actual=str(new_positions_this_run + 1),
                    limit=str(limits.max_new_positions_per_run),
                    detail="one run may not commit more of the campaign than policy allows",
                )
            )
        else:
            checks.append(
                RiskCheck(
                    name="max_new_positions_per_run",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.PASS,
                    actual=str(new_positions_this_run + 1),
                    limit=str(limits.max_new_positions_per_run),
                )
            )
        return checks

    def _daily_loss_check(self, campaign: CampaignSnapshot) -> RiskCheck:
        """The daily loss limit, or an honest record that it could not be checked.

        Realised profit and loss is not tracked until positions exist, so
        today this is usually ``NOT_EVALUATED``. That is deliberately not the
        same as ``PASS``: reading an untracked figure as "no losses today"
        would be the same mistake as reading a missing volume as a satisfied
        threshold. Configuration decides whether the unknown blocks a trade,
        via ``campaign.account.require_daily_loss_tracking``.
        """
        limits = self._limits
        realized = campaign.realized_pnl_today
        if realized is None:
            untracked = (
                "realised profit and loss for the day is not tracked yet (Milestone 9), so "
                "the daily loss limit could not be evaluated"
            )
            if limits.require_daily_loss_tracking:
                return RiskCheck(
                    name="daily_loss",
                    scope=RiskLimitScope.GLOBAL,
                    outcome=RiskCheckOutcome.FAIL,
                    reason_code=RiskReasonCode.DAILY_LOSS_NOT_TRACKED,
                    limit=str(limits.max_daily_loss),
                    detail=(
                        f"{untracked}, and configuration requires it before capital may be "
                        f"authorised"
                    ),
                )
            return RiskCheck(
                name="daily_loss",
                scope=RiskLimitScope.GLOBAL,
                outcome=RiskCheckOutcome.NOT_EVALUATED,
                limit=str(limits.max_daily_loss),
                detail=(
                    f"{untracked}. Recorded as unevaluated rather than passed: an unevaluated "
                    f"limit is not a satisfied one"
                ),
            )
        loss = -realized if realized < 0 else Decimal("0")
        if loss > limits.max_daily_loss:
            return RiskCheck(
                name="daily_loss",
                scope=RiskLimitScope.GLOBAL,
                outcome=RiskCheckOutcome.FAIL,
                reason_code=RiskReasonCode.DAILY_LOSS_LIMIT_REACHED,
                actual=str(loss),
                limit=str(limits.max_daily_loss),
                detail="the day's realised loss has reached the configured limit",
            )
        return RiskCheck(
            name="daily_loss",
            scope=RiskLimitScope.GLOBAL,
            outcome=RiskCheckOutcome.PASS,
            actual=str(loss),
            limit=str(limits.max_daily_loss),
        )

    def _capacity_checks(
        self,
        candidate: AllocationCandidate,
        campaign: CampaignSnapshot,
        account: AccountSnapshot | None,
        unit_cost: Decimal,
        unit_max_loss: Decimal,
    ) -> list[RiskCheck]:
        """Whether there is room for *one* unit. Sizing happens elsewhere.

        One unit is the right granularity: a candidate that cannot fit a single
        contract is not permitted at all, and how many contracts beyond the
        first are affordable is a question about the budget rather than about
        permission.
        """
        limits = self._limits
        checks: list[RiskCheck] = []

        available = campaign.available
        checks.append(
            _compare(
                "campaign_budget_available",
                limits.scopes.get("campaign_budget", RiskLimitScope.CAMPAIGN),
                actual=unit_cost,
                limit=available,
                reason=RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET,
                detail=(
                    f"one unit costs {unit_cost} and the campaign has {available} left of its "
                    f"{campaign.allocatable} allocatable budget "
                    f"({campaign.reserve} is held in reserve)"
                ),
            )
        )

        checks.append(
            _compare(
                "max_allocation_per_trade",
                limits.scopes.get("max_allocation_per_trade", RiskLimitScope.CAMPAIGN),
                actual=unit_cost,
                limit=limits.max_allocation_per_trade,
                reason=RiskReasonCode.MAX_ALLOCATION_PER_TRADE_EXCEEDED,
                detail="a single unit already exceeds the per-trade allocation ceiling",
            )
        )

        checks.append(
            _compare(
                "max_risk_per_trade",
                limits.scopes.get("max_risk_per_trade", RiskLimitScope.CAMPAIGN),
                actual=unit_max_loss,
                limit=limits.max_risk_per_trade,
                reason=RiskReasonCode.MAX_RISK_PER_TRADE_EXCEEDED,
                detail="a single unit already risks more than one trade may",
            )
        )

        remaining_risk = limits.max_total_open_risk - campaign.open_risk
        checks.append(
            _compare(
                "max_total_open_risk",
                RiskLimitScope.GLOBAL,
                actual=unit_max_loss,
                limit=max(remaining_risk, Decimal("0")),
                reason=RiskReasonCode.MAX_TOTAL_OPEN_RISK_EXCEEDED,
                detail=(
                    f"the book already risks {campaign.open_risk} of a permitted "
                    f"{limits.max_total_open_risk}"
                ),
            )
        )

        underlying_cap = limits.concentration_cap(limits.max_underlying_concentration_pct)
        underlying_room = underlying_cap - campaign.committed_to(candidate.symbol)
        checks.append(
            _compare(
                "max_underlying_concentration",
                RiskLimitScope.GLOBAL,
                actual=unit_cost,
                limit=max(underlying_room, Decimal("0")),
                reason=RiskReasonCode.UNDERLYING_CONCENTRATION_EXCEEDED,
                detail=(
                    f"{candidate.symbol} may hold {underlying_cap} "
                    f"({limits.max_underlying_concentration_pct}% of the campaign) and already "
                    f"holds {campaign.committed_to(candidate.symbol)}"
                ),
            )
        )

        strategy_cap = limits.concentration_cap(limits.max_strategy_concentration_pct)
        strategy_room = strategy_cap - campaign.committed_to_strategy(candidate.strategy)
        checks.append(
            _compare(
                "max_strategy_concentration",
                RiskLimitScope.GLOBAL,
                actual=unit_cost,
                limit=max(strategy_room, Decimal("0")),
                reason=RiskReasonCode.STRATEGY_CONCENTRATION_EXCEEDED,
                detail=(
                    f"{candidate.strategy.value} may hold {strategy_cap} "
                    f"({limits.max_strategy_concentration_pct}% of the campaign) and already "
                    f"holds {campaign.committed_to_strategy(candidate.strategy)}"
                ),
            )
        )

        view = candidate.risk_profile.directional_view
        directional_cap = limits.concentration_cap(limits.max_directional_exposure_pct)
        directional_room = directional_cap - campaign.committed_to_direction(view)
        checks.append(
            _compare(
                "max_directional_exposure",
                RiskLimitScope.GLOBAL,
                actual=unit_cost,
                limit=max(directional_room, Decimal("0")),
                reason=RiskReasonCode.DIRECTIONAL_EXPOSURE_EXCEEDED,
                detail=(
                    f"{view.value} exposure may reach {directional_cap} "
                    f"({limits.max_directional_exposure_pct}% of the campaign) and already "
                    f"stands at {campaign.committed_to_direction(view)}"
                ),
            )
        )

        if limits.max_contracts_per_trade < 1:
            checks.append(
                RiskCheck(
                    name="max_contracts_per_trade",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.FAIL,
                    reason_code=RiskReasonCode.MAX_CONTRACT_QUANTITY_EXCEEDED,
                    actual="1",
                    limit=str(limits.max_contracts_per_trade),
                    detail="configuration permits no contracts at all for a single trade",
                )
            )
        else:
            checks.append(
                RiskCheck(
                    name="max_contracts_per_trade",
                    scope=RiskLimitScope.CAMPAIGN,
                    outcome=RiskCheckOutcome.PASS,
                    actual="1",
                    limit=str(limits.max_contracts_per_trade),
                )
            )

        # The account is the other half of "the most restrictive limit wins".
        # A campaign with room to spare cannot spend money the broker says is
        # not there.
        if account is not None and account.spendable is not None:
            checks.append(
                _compare(
                    "broker_available_funds",
                    RiskLimitScope.GLOBAL,
                    actual=unit_cost,
                    limit=account.spendable,
                    reason=RiskReasonCode.INSUFFICIENT_BUYING_POWER,
                    detail=(
                        f"the broker reports {account.spendable} available; the campaign "
                        f"envelope is not permission to spend money the account does not have"
                    ),
                )
            )

        return checks


def _compare(
    name: str,
    scope: RiskLimitScope,
    *,
    actual: Decimal,
    limit: Decimal,
    reason: RiskReasonCode,
    detail: str,
) -> RiskCheck:
    """One ``actual <= limit`` comparison, in exact decimal."""
    if actual > limit:
        return RiskCheck(
            name=name,
            scope=scope,
            outcome=RiskCheckOutcome.FAIL,
            reason_code=reason,
            actual=str(actual),
            limit=str(limit),
            detail=detail,
        )
    return RiskCheck(
        name=name,
        scope=scope,
        outcome=RiskCheckOutcome.PASS,
        actual=str(actual),
        limit=str(limit),
    )


def _ordered_reasons(failed: list[RiskCheck]) -> list[RiskReasonCode]:
    """Reason codes in check order, de-duplicated, never empty.

    Order matters for reproducibility: the same failures must produce the same
    list, and sorting alphabetically would put the least informative reason
    first as often as not. Check order is the order the engine evaluates in,
    which is preconditions before capacity — the most actionable reason first.
    """
    seen: list[RiskReasonCode] = []
    for check in failed:
        if check.reason_code is not None and check.reason_code not in seen:
            seen.append(check.reason_code)
    return seen or [RiskReasonCode.CONFIGURATION_ERROR]
