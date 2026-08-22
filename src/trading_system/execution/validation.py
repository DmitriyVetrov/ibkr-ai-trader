"""Deterministic preconditions for submitting an authorisation.

Everything that can be checked *without touching a broker* is checked here,
before a connection is opened. That ordering is deliberate: Milestone 2
established that only the first uncached round trip on a TWS connection is
reliably answered, so the execution path must not spend its one reliable round
trip discovering something it already knew.

The validator is a pure function of its arguments — the authorisation, the
request, the policy, the calendar and an injected ``execution_now``. No clock is
read (brief section 48), so a validation is reproducible and a test does not
have to wait for a window to expire to prove one closes.

The rule it exists to enforce: **it validates, it never repairs.** No check here
reduces a quantity to fit, moves a price to make a drift test pass, or extends a
window that has closed. Milestone 7 authorised a specific trade; if that trade
is no longer executable, the answer is a named refusal and a new authorisation,
not a different trade wearing the old one's identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.allocation.models import CampaignAllocation
from trading_system.data.market_calendar import MarketCalendar, MarketCalendarError
from trading_system.domain.enums import (
    AllocationOutcome,
    ExecutionReasonCode,
    LegAction,
    OrderType,
    TradingMode,
)
from trading_system.infrastructure.settings import CampaignConfig, ExecutionConfig

__all__ = ["ExecutionValidation", "ExecutionValidator", "ValidationFailure"]


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """One refused precondition: a code to act on and prose to read."""

    reason_code: ExecutionReasonCode
    detail: str

    def __str__(self) -> str:
        return f"{self.reason_code.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ExecutionValidation:
    """The verdict. ``ok`` only when nothing was refused.

    Every failure is collected rather than the first one raised, because an
    operator fixing a refused execution wants the whole list — a stale price
    *and* a closed market is two edits, and discovering them one run at a time
    is how a window expires while you work.
    """

    failures: tuple[ValidationFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def reason_codes(self) -> list[ExecutionReasonCode]:
        seen: list[ExecutionReasonCode] = []
        for failure in self.failures:
            if failure.reason_code not in seen:
                seen.append(failure.reason_code)
        return seen

    @property
    def detail(self) -> str:
        return "; ".join(str(failure) for failure in self.failures)


@dataclass
class _Checks:
    failures: list[ValidationFailure] = field(default_factory=list)

    def refuse(self, reason_code: ExecutionReasonCode, detail: str) -> None:
        self.failures.append(ValidationFailure(reason_code, detail))


class ExecutionValidator:
    """Answers one question: may this authorisation be sent, exactly as it is?

    It cannot answer *should we trade* — Milestone 7 answered that — and it has
    no way to change what would be sent. It holds no broker, no repository and
    no clock.
    """

    def __init__(
        self,
        *,
        config: ExecutionConfig,
        campaign: CampaignConfig,
        calendar: MarketCalendar | None = None,
    ) -> None:
        self._config = config
        self._campaign = campaign
        self._calendar = calendar

    def validate(
        self,
        allocation: CampaignAllocation,
        *,
        execution_now: datetime,
        trading_mode: TradingMode,
        order_type: OrderType,
        dry_run: bool = False,
        observed_unit_price: Decimal | None = None,
    ) -> ExecutionValidation:
        """Check every deterministic precondition.

        ``observed_unit_price`` is optional and is never fetched by this layer:
        Milestone 8 does not read a fresh quote before submitting, because a
        second uncached round trip on the submission's connection is exactly
        what Milestone 2 found to be unreliable. When a caller supplies one
        explicitly, drift is checked against it.
        """
        checks = _Checks()

        self._check_authorisation(checks, allocation, dry_run=dry_run)
        self._check_mode(checks, trading_mode, dry_run=dry_run)
        self._check_order_policy(checks, order_type)
        self._check_quantity(checks, allocation)
        self._check_structure(checks, allocation)
        self._check_currency(checks, allocation)
        self._check_price(checks, allocation, observed_unit_price=observed_unit_price)
        self._check_windows(checks, allocation, execution_now=execution_now)
        self._check_session(checks, execution_now=execution_now)

        return ExecutionValidation(tuple(checks.failures))

    # --- authorisation -----------------------------------------------------
    def _check_authorisation(
        self, checks: _Checks, allocation: CampaignAllocation, *, dry_run: bool
    ) -> None:
        if allocation.outcome is not AllocationOutcome.APPROVED:
            checks.refuse(
                ExecutionReasonCode.ALLOCATION_NOT_APPROVED,
                f"allocation {allocation.allocation_id} is {allocation.outcome.value}. "
                f"REJECTED, NO_TRADE and ALREADY_ALLOCATED are all inexecutable and none of "
                f"them is a near miss",
            )
        if allocation.dry_run:
            checks.refuse(
                ExecutionReasonCode.ALLOCATION_IS_DRY_RUN,
                f"allocation {allocation.allocation_id} came from a dry run; it is a "
                f"diagnostic record and authorises nothing",
            )
        # A dry run is allowed to build and inspect an order while submission is
        # switched off — that is what makes the switch reviewable.
        if not self._config.enabled and not dry_run:
            checks.refuse(
                ExecutionReasonCode.EXECUTION_DISABLED,
                "execution.enabled is false in config/execution.yaml, so no order may be "
                "sent. Run with --dry-run to inspect what would be submitted",
            )

    def _check_mode(self, checks: _Checks, trading_mode: TradingMode, *, dry_run: bool) -> None:
        if trading_mode is TradingMode.LIVE:
            checks.refuse(
                ExecutionReasonCode.LIVE_GUARD_FAILED,
                "TRADING_MODE=LIVE reached the execution stage. Live trading is delivered in "
                "Milestone 12 behind a signed-off readiness checklist and is refused here, in "
                "the broker factory and in the adapter",
            )
            return
        if trading_mode is TradingMode.DRY_RUN and not dry_run:
            checks.refuse(
                ExecutionReasonCode.PAPER_MODE_REQUIRED,
                "TRADING_MODE=DRY_RUN never reaches a broker; run with --dry-run, or set "
                "TRADING_MODE=PAPER to submit",
            )

    def _check_order_policy(self, checks: _Checks, order_type: OrderType) -> None:
        if order_type not in self._config.permitted_order_types:
            checks.refuse(
                ExecutionReasonCode.ORDER_TYPE_NOT_PERMITTED,
                f"{order_type.value} is not in execution.permitted_order_types "
                f"{[t.value for t in self._config.permitted_order_types]}",
            )

    # --- the trade ---------------------------------------------------------
    def _check_quantity(self, checks: _Checks, allocation: CampaignAllocation) -> None:
        if allocation.quantity < 1:
            checks.refuse(
                ExecutionReasonCode.INVALID_QUANTITY,
                f"allocation {allocation.allocation_id} authorises {allocation.quantity} "
                f"contracts. Execution uses the authorised quantity exactly and never "
                f"recalculates one",
            )

    def _check_structure(self, checks: _Checks, allocation: CampaignAllocation) -> None:
        if not allocation.legs:
            checks.refuse(
                ExecutionReasonCode.CONTRACT_INVALID,
                f"allocation {allocation.allocation_id} carries no legs",
            )
            return

        for leg in allocation.legs:
            if not leg.contract_id or leg.contract_id <= 0:
                checks.refuse(
                    ExecutionReasonCode.CONTRACT_ID_MISSING,
                    f"leg {leg.leg_index} has no broker contract id. It is never re-derived "
                    f"from symbol, strike and expiration: that would be selecting a contract "
                    f"at execution time",
                )
            if leg.multiplier <= 0:
                checks.refuse(
                    ExecutionReasonCode.MULTIPLIER_MISSING,
                    f"leg {leg.leg_index} has no contract multiplier; 100 is common and is "
                    f"not a default anything may assume",
                )
            if leg.action is LegAction.SELL and not self._config.allow_short_legs:
                checks.refuse(
                    ExecutionReasonCode.SHORT_LEG_NOT_SUPPORTED,
                    f"leg {leg.leg_index} is a SELL. Every strategy this system can select is "
                    f"a long-premium structure, so a short leg here is an upstream bug, not a "
                    f"trade to place",
                )
            if leg.underlying != allocation.symbol:
                checks.refuse(
                    ExecutionReasonCode.CONTRACT_INVALID,
                    f"leg {leg.leg_index} references {leg.underlying}, not {allocation.symbol}",
                )

        expirations = {leg.expiration for leg in allocation.legs}
        if len(expirations) > 1:
            checks.refuse(
                ExecutionReasonCode.MULTI_LEG_UNSUPPORTED,
                f"legs expire on {sorted(d.isoformat() for d in expirations)}. No shipped "
                f"strategy spans expirations and the combo builder assumes one",
            )
        contract_ids = [leg.contract_id for leg in allocation.legs]
        if len(set(contract_ids)) != len(contract_ids):
            checks.refuse(
                ExecutionReasonCode.CONTRACT_INVALID,
                f"the structure repeats a contract id {contract_ids}; two legs of one strategy "
                f"are two different contracts",
            )
        multipliers = {leg.multiplier for leg in allocation.legs}
        if len(multipliers) > 1:
            checks.refuse(
                ExecutionReasonCode.CONTRACT_INVALID,
                f"legs carry different multipliers {sorted(multipliers)}; one structure is one "
                f"contract size",
            )
        if len(allocation.legs) > 1 and not self._config.multi_leg_as_combo:
            checks.refuse(
                ExecutionReasonCode.MULTI_LEG_UNSUPPORTED,
                "execution.multi_leg_as_combo is false, leaving no way to send this structure "
                "as one order. Independent leg orders can half-fill into a position nobody "
                "authorised",
            )

    def _check_currency(self, checks: _Checks, allocation: CampaignAllocation) -> None:
        """Whether this contract is quoted in the currency the campaign trades.

        An instrument price is never converted, here least of all: the number
        this stage is about to turn into a limit price has to be the one the
        exchange expects. Milestone 7 has already made the same check against
        the same target currency; repeating it is deliberate, because this is
        the stage where being wrong costs money rather than an authorisation.

        Note what is *not* checked: the currency the operator's capital is held
        in. That conversion happened at allocation, against a rate captured
        with the account, and its result is what authorised the amount this
        order spends. Re-converting here would apply a second rate to a figure
        that was already committed.
        """
        target = self._campaign.target_currency
        currencies = {leg.currency.upper() for leg in allocation.legs if leg.currency}
        if allocation.currency:
            currencies.add(allocation.currency.upper())
        unsupported = sorted(currencies - {target})
        if unsupported:
            checks.refuse(
                ExecutionReasonCode.CURRENCY_MISMATCH,
                f"contract is quoted in {', '.join(unsupported)} and this campaign trades in "
                f"{target}. An instrument price is never converted: the limit price that "
                f"reaches the broker has to be in the contract's own currency, so a converted "
                f"one would be the wrong number on the wire. Set "
                f"campaign.currency_policy.target_currency to the currency this campaign "
                f"actually trades. This is Milestone 7's refusal, preserved",
            )

    def _check_price(
        self,
        checks: _Checks,
        allocation: CampaignAllocation,
        *,
        observed_unit_price: Decimal | None,
    ) -> None:
        reference = allocation.unit_cost
        if reference is None:
            checks.refuse(
                ExecutionReasonCode.PRICE_UNAVAILABLE,
                f"allocation {allocation.allocation_id} carries no reference price, so no "
                f"limit price can be derived from one. Execution never invents a price",
            )
            return
        if reference <= 0:
            checks.refuse(
                ExecutionReasonCode.INVALID_PRICE,
                f"reference price {reference} is not a price anything can be bought at",
            )
            return

        if observed_unit_price is None:
            return
        if observed_unit_price <= 0:
            checks.refuse(
                ExecutionReasonCode.INVALID_PRICE,
                f"observed price {observed_unit_price} is not usable",
            )
            return
        drift = abs(observed_unit_price - reference) / reference * Decimal(100)
        if drift > Decimal(str(self._config.max_price_drift_pct)):
            checks.refuse(
                ExecutionReasonCode.PRICE_DRIFT,
                f"observed price {observed_unit_price} differs from the authorised reference "
                f"{reference} by {drift:.2f}%, beyond the "
                f"{self._config.max_price_drift_pct}% ceiling. Execution does not chase the "
                f"market: a changed trade needs a new authorisation",
            )

    # --- time --------------------------------------------------------------
    def _check_windows(
        self, checks: _Checks, allocation: CampaignAllocation, *, execution_now: datetime
    ) -> None:
        if execution_now.tzinfo is None:
            raise ValueError("execution_now must be timezone-aware")
        now = execution_now.astimezone(UTC)

        if allocation.decided_at > now:
            checks.refuse(
                ExecutionReasonCode.POINT_IN_TIME_ERROR,
                f"allocation was decided at {allocation.decided_at.isoformat()}, after the "
                f"execution instant {now.isoformat()}. A future authorisation is a "
                f"correctness bug, not a market outcome",
            )
        elif self._config.allocation_validity_minutes:
            deadline = allocation.decided_at + timedelta(
                minutes=self._config.allocation_validity_minutes
            )
            if now > deadline:
                age = (now - allocation.decided_at).total_seconds() / 60
                checks.refuse(
                    ExecutionReasonCode.EXECUTION_WINDOW_EXPIRED,
                    f"the authorisation is {age:.0f} minutes old and the execution window is "
                    f"{self._config.allocation_validity_minutes} minutes. It is not silently "
                    f"extended: re-run allocation to authorise the trade against current "
                    f"prices",
                )

        quote_times = [leg.quote_as_of for leg in allocation.legs if leg.quote_as_of is not None]
        if not quote_times:
            return
        # The weakest link: a structure is only as fresh as its stalest leg.
        oldest = min(quote_times)
        newest = max(quote_times)
        if newest > now:
            checks.refuse(
                ExecutionReasonCode.POINT_IN_TIME_ERROR,
                f"a leg quote is stamped {newest.isoformat()}, after the execution instant "
                f"{now.isoformat()}. A future price would read as the freshest possible one",
            )
            return
        if self._config.price_validity_seconds:
            age = (now - oldest).total_seconds()
            if age > self._config.price_validity_seconds:
                checks.refuse(
                    ExecutionReasonCode.PRICE_REFERENCE_STALE,
                    f"the price behind this authorisation is {age:.0f}s old and policy permits "
                    f"{self._config.price_validity_seconds}s. A stale price is not silently "
                    f"used, and execution does not fetch a new one",
                )

    def _check_session(self, checks: _Checks, *, execution_now: datetime) -> None:
        if not self._config.require_market_open or self._calendar is None:
            return
        try:
            if self._calendar.is_open(execution_now):
                return
        except MarketCalendarError as exc:
            if self._config.block_on_unknown_session:
                checks.refuse(
                    ExecutionReasonCode.MARKET_CLOSED,
                    f"the exchange calendar cannot say whether the market is open: {exc}",
                )
            return
        checks.refuse(
            ExecutionReasonCode.MARKET_CLOSED,
            f"the {self._calendar.exchange} calendar says regular trading is not in progress "
            f"at {execution_now.isoformat()}. The session is determined from the transcribed "
            f"calendar, never invented",
        )
