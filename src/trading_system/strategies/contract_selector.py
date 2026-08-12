"""The deterministic contract selector.

This is the half of Milestone 6 that has no AI in it, and that is the point:

.. code-block:: text

    STRATEGY DECISION   what to do        <- chosen by an agent, then validated
          |
    CONTRACT SELECTOR   which contract    <- this module. Arithmetic, not judgement
          |
    FUTURE RISK ENGINE  how much
          |
    FUTURE EXECUTION    how

No model is consulted here — not once per selection, and certainly not once per
strike. Given the same decision, the same stored chain and quotes, the same
configuration and the same ``as_of``, this module returns the same contracts;
:mod:`tests.contract_selection.test_determinism` asserts exact equality across
repeated runs.

Everything it decides comes from configuration:

* the DTE window is ``config/risk.yaml`` intersected with the strategy's own;
* the expiration rule and the strike policy come from
  ``config/strategies/*.yaml``, falling back to ``config/contract_selection.yaml``;
* the liquidity floors, the price bounds and the spread ceiling are the
  strategy's, already narrowed against the risk policy by the registry.

And everything it refuses to do is refused explicitly:

* **No contract is invented.** A required field the data does not carry is a
  named rejection. A bid that was never collected is not a zero, a missing
  delta is not an estimate, and a chain with no quotes yields
  ``REQUIRED_DATA_UNAVAILABLE`` rather than a contract nobody can price.
* **No contract is approximated.** "No valid contract" is a valid outcome. The
  nearest miss is recorded as a rejection, never returned as a consolation.
* **No trading class is derived.** It is copied from the chain the contract
  came from. Milestone 2 found ``SMART/SPY`` and ``SMART/2SPY`` coexisting for
  the same underlying; a class inferred from the ticker names a contract that
  may not exist.
* **No look-ahead.** Chain and quotes are read as of the decision's instant. An
  expiration after that instant is the entire point of an option; a *quote*
  retrieved after it is a bug, and raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trading_system import __version__ as application_version
from trading_system.data.market_calendar import MarketCalendar, TradingDayStatus
from trading_system.data.point_in_time import LookAheadError
from trading_system.data.repository import DataRepository
from trading_system.domain.enums import (
    ContractRejectionReason,
    ContractSelectionStatus,
    ExpirationSelectionPolicy,
    OptionDataField,
    OptionRight,
    StrikeSelectionPolicy,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.settings import (
    ContractSelectionConfig,
    SystemConfig,
    UnknownLiquidityPolicy,
)
from trading_system.strategies.base import StrikeRelationship
from trading_system.strategies.chain import ChainReader, ChainView, ContractCandidate
from trading_system.strategies.models import (
    STRATEGY_SCHEMA_VERSION,
    ContractCostEstimate,
    ContractSelectionResult,
    RejectedContract,
    SelectedLeg,
    StrategyDecisionRecord,
    selection_identifier,
)
from trading_system.strategies.registry import LegSpecification, StrategySpecification

__all__ = ["ContractSelector", "SelectionContext"]

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class SelectionContext:
    """Everything one selection needs that is not configuration.

    ``event_time`` comes from the structured research report, never from prose
    and never from a model: an event-aligned expiration is only as trustworthy
    as the date it aligns to.
    """

    run_id: str
    decision: StrategyDecisionRecord
    specification: StrategySpecification
    as_of: datetime
    event_time: datetime | None = None
    research_report_id: str | None = None
    strategy_run_id: str | None = None


@dataclass
class _Attempt:
    """Working state for one selection, so failures explain themselves."""

    rejections: list[RejectedContract] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    considered: int = 0

    def reject(
        self,
        reason: ContractRejectionReason,
        candidate: ContractCandidate | None = None,
        *,
        leg_index: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.rejections.append(
            RejectedContract(
                reason=reason,
                leg_index=leg_index,
                contract_id=candidate.contract_id if candidate else None,
                expiration=candidate.expiration if candidate else None,
                strike=candidate.strike if candidate else None,
                right=candidate.right if candidate else None,
                detail=detail,
            )
        )


class ContractSelector:
    """Chooses concrete contracts for a strategy decision. Deterministic.

    Constructed with a repository, a calendar and configuration — never a
    broker, never an LLM client, never a provider. A test asserts the module
    cannot reach any of them, transitively.
    """

    def __init__(
        self,
        repository: DataRepository,
        *,
        config: SystemConfig,
        calendar: MarketCalendar | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._policy: ContractSelectionConfig = config.contract_selection
        self._calendar = calendar or MarketCalendar(config.data.market_calendar)
        self._clock = clock or SystemClock()
        self._reader = ChainReader(repository, config=self._policy)

    # --- entry point -------------------------------------------------------
    def select(self, context: SelectionContext) -> ContractSelectionResult:
        """Select contracts for one decision, or explain why none qualify."""
        attempt = _Attempt()
        try:
            view = self._reader.read(context.decision.symbol, context.as_of)
        except LookAheadError as exc:
            return self._failure(
                context,
                ContractSelectionStatus.POINT_IN_TIME_ERROR,
                attempt,
                detail=(
                    f"a stored option record was not knowable at {context.as_of.isoformat()}: "
                    f"{exc}. This is a correctness bug in storage, not a market outcome, and "
                    f"no contract is selected from it."
                ),
            )

        if view is None:
            return self._failure(
                context,
                ContractSelectionStatus.OPTION_CHAIN_UNAVAILABLE,
                attempt,
                detail=(
                    f"no option chain for {context.decision.symbol} was visible at "
                    f"{context.as_of.isoformat()}; collect one first "
                    f"(data collect-options --symbol {context.decision.symbol})."
                ),
            )

        invalid = self._chain_problem(view)
        if invalid is not None:
            return self._failure(
                context, ContractSelectionStatus.INVALID_CHAIN, attempt, detail=invalid, view=view
            )

        if len(view.candidates) > self._policy.limits.max_candidates:
            return self._failure(
                context,
                ContractSelectionStatus.INVALID_CHAIN,
                attempt,
                detail=(
                    f"the stored chain carries {len(view.candidates)} candidate contracts, "
                    f"beyond the configured ceiling of {self._policy.limits.max_candidates}. "
                    f"Selecting from a subset would be a choice nobody made; raise the limit "
                    f"deliberately instead."
                ),
                view=view,
            )

        attempt.considered = len(view.candidates)
        expirations = self._ordered_expirations(context, view, attempt)
        if not expirations:
            return self._failure(
                context,
                ContractSelectionStatus.NO_VALID_EXPIRATION,
                attempt,
                detail=(
                    f"no expiration in the stored chain falls inside the effective "
                    f"{context.specification.dte_min}-{context.specification.dte_max} day "
                    f"window at {context.as_of.isoformat()}"
                ),
                view=view,
            )

        last_status = ContractSelectionStatus.NO_VALID_CONTRACT
        for expiration, dte, expiration_reason in expirations:
            legs, status = self._legs_for(context, view, expiration, dte, attempt)
            if legs is None:
                last_status = status
                attempt.reasons.append(
                    f"expiration {expiration.isoformat()} (DTE {dte}) rejected: {status.value}"
                )
                continue
            attempt.reasons.insert(0, expiration_reason)
            return self._success(context, view, expiration, dte, legs, attempt)

        return self._failure(context, last_status, attempt, view=view)

    # --- chain sanity ------------------------------------------------------
    def _chain_problem(self, view: ChainView) -> str | None:
        chain = view.chain
        if chain.underlying.upper() != view.symbol:
            return (
                f"the stored chain describes {chain.underlying} but was filed under "
                f"{view.symbol}; a chain that disagrees with its own key cannot be trusted"
            )
        if not chain.expirations:
            return (
                f"the stored chain for {view.symbol} lists no expirations. An empty chain is "
                f"not a chain with nothing worth trading — it is a collection that did not "
                f"return one."
            )
        return None

    # --- expirations -------------------------------------------------------
    def _ordered_expirations(
        self, context: SelectionContext, view: ChainView, attempt: _Attempt
    ) -> list[tuple[date, int, str]]:
        """Valid expirations, most preferred first, each with its reason.

        Ordered rather than singular so that an expiration whose strikes turn
        out to be unusable can be followed by the next preference instead of
        failing the whole selection. The order is fully determined by the
        configured rule, so the outcome stays reproducible.
        """
        specification = context.specification
        reference_day = self._local_date(context.as_of)
        rule = specification.expiration_rule
        target = specification.target_dte
        if target is None:
            target = self._policy.expiration.target_dte

        valid: list[tuple[date, int]] = []
        for expiration in view.expirations:
            dte = (expiration - reference_day).days
            if dte < specification.dte_min or dte > specification.dte_max:
                attempt.reasons.append(
                    f"expiration {expiration.isoformat()} rejected: DTE {dte} outside "
                    f"[{specification.dte_min}, {specification.dte_max}]"
                )
                continue
            if (
                self._policy.expiration.require_trading_day
                and self._calendar.status(expiration) == TradingDayStatus.CLOSED
            ):
                attempt.reject(
                    ContractRejectionReason.EXPIRATION_NOT_A_TRADING_DAY,
                    detail=(
                        f"{expiration.isoformat()} is a market holiday or a weekend on the "
                        f"{self._calendar.exchange} calendar"
                    ),
                )
                continue
            valid.append((expiration, dte))

        if not valid:
            return []

        event_day = self._event_day(context)
        if rule is ExpirationSelectionPolicy.EVENT_ALIGNED and event_day is not None:
            window = specification.event_max_days_after
            if window is None:
                window = self._policy.expiration.event_max_days_after
            aligned = [
                (expiration, dte)
                for expiration, dte in valid
                if expiration >= event_day and (expiration - event_day).days <= window
            ]
            if aligned:
                ordered = sorted(aligned, key=lambda pair: (pair[0], pair[1]))
                return [
                    (
                        expiration,
                        dte,
                        (
                            f"expiration {expiration.isoformat()} (DTE {dte}) chosen by "
                            f"EVENT_ALIGNED: the first listed expiration on or after the "
                            f"research event on {event_day.isoformat()}, within {window} days "
                            f"of it"
                        ),
                    )
                    for expiration, dte in ordered
                ]
            attempt.reasons.append(
                f"EVENT_ALIGNED found no expiration within {window} days after the research "
                f"event on {event_day.isoformat()}; falling back to the configured target DTE"
            )
        elif rule is ExpirationSelectionPolicy.EVENT_ALIGNED:
            attempt.reasons.append(
                "EVENT_ALIGNED requested but the research report names no event; falling back "
                "to the configured target DTE. No event date is ever inferred."
            )

        if rule is ExpirationSelectionPolicy.NEAREST_VALID:
            ordered = sorted(valid, key=lambda pair: (pair[1], pair[0]))
            label = "NEAREST_VALID: the smallest DTE inside the window"
        else:
            ordered = sorted(valid, key=lambda pair: (abs(pair[1] - target), pair[1], pair[0]))
            label = f"TARGET_DTE: the DTE closest to the configured target of {target}"

        return [
            (
                expiration,
                dte,
                f"expiration {expiration.isoformat()} (DTE {dte}) chosen by {label}",
            )
            for expiration, dte in ordered
        ]

    def _event_day(self, context: SelectionContext) -> date | None:
        if context.event_time is None:
            return None
        return self._local_date(context.event_time)

    def _local_date(self, instant: datetime) -> date:
        """The exchange-local date of an instant.

        DTE is a count of calendar days to an expiration, and an expiration is a
        date at the exchange. Counting from a UTC date would be wrong by one for
        every instant after the exchange's local midnight, which in this system
        is most of the evening.
        """
        return instant.astimezone(self._calendar.timezone).date()

    # --- legs --------------------------------------------------------------
    def _legs_for(
        self,
        context: SelectionContext,
        view: ChainView,
        expiration: date,
        dte: int,
        attempt: _Attempt,
    ) -> tuple[list[SelectedLeg] | None, ContractSelectionStatus]:
        specification = context.specification
        candidates = view.for_expiration(expiration)
        if not candidates:
            attempt.reject(
                ContractRejectionReason.MISSING_QUOTE,
                detail=(
                    f"the chain lists {expiration.isoformat()} but the store holds no "
                    f"contracts for it"
                ),
            )
            return None, ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE

        eligible: dict[int, list[ContractCandidate]] = {}
        for leg in specification.legs:
            usable = [
                candidate
                for candidate in candidates
                if candidate.right is leg.right
                and self._is_usable(candidate, leg, specification, view, attempt)
            ]
            if not usable:
                return None, self._empty_leg_status(attempt)
            eligible[leg.index] = usable

        reference = view.reference.value if view.reference is not None else None
        if specification.strike_relationship is StrikeRelationship.SAME:
            return self._same_strike_legs(
                context, view, expiration, dte, eligible, reference, attempt
            )
        return self._independent_legs(context, view, expiration, dte, eligible, reference, attempt)

    def _empty_leg_status(self, attempt: _Attempt) -> ContractSelectionStatus:
        """Distinguish "the data was missing" from "nothing satisfied the policy"."""
        data_reasons = {
            ContractRejectionReason.MISSING_QUOTE,
            ContractRejectionReason.MISSING_DELTA,
            ContractRejectionReason.MISSING_CONTRACT_ID,
            ContractRejectionReason.MISSING_STRIKE,
            ContractRejectionReason.MISSING_EXPIRATION,
            ContractRejectionReason.MISSING_IMPLIED_VOLATILITY,
            ContractRejectionReason.MISSING_REQUIRED_FIELD,
            ContractRejectionReason.INVALID_TRADING_CLASS,
            ContractRejectionReason.OPTION_LIQUIDITY_UNKNOWN,
        }
        if any(rejection.reason in data_reasons for rejection in attempt.rejections):
            return ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE
        return ContractSelectionStatus.NO_VALID_STRIKE

    def _independent_legs(
        self,
        context: SelectionContext,
        view: ChainView,
        expiration: date,
        dte: int,
        eligible: dict[int, list[ContractCandidate]],
        reference: Decimal | None,
        attempt: _Attempt,
    ) -> tuple[list[SelectedLeg] | None, ContractSelectionStatus]:
        selected: list[SelectedLeg] = []
        for leg in context.specification.legs:
            chosen = self._choose(leg, eligible[leg.index], reference, attempt)
            if chosen is None:
                return None, ContractSelectionStatus.NO_VALID_STRIKE
            candidate, target, reason = chosen
            for other in eligible[leg.index]:
                if other is not candidate:
                    attempt.reject(
                        ContractRejectionReason.NOT_SELECTED_BY_POLICY,
                        other,
                        leg_index=leg.index,
                        detail="valid, but another strike matched the policy more closely",
                    )
            selected.append(
                self._leg(context, view, leg, candidate, dte, target, reference, reason)
            )

        problem = self._relationship_problem(context, selected)
        if problem is not None:
            for chosen_leg in selected:
                attempt.reject(
                    ContractRejectionReason.INCOMPATIBLE_LEG,
                    leg_index=chosen_leg.leg_index,
                    detail=problem,
                )
            attempt.reasons.append(problem)
            return None, ContractSelectionStatus.NO_VALID_CONTRACT
        return selected, ContractSelectionStatus.SUCCESS

    def _same_strike_legs(
        self,
        context: SelectionContext,
        view: ChainView,
        expiration: date,
        dte: int,
        eligible: dict[int, list[ContractCandidate]],
        reference: Decimal | None,
        attempt: _Attempt,
    ) -> tuple[list[SelectedLeg] | None, ContractSelectionStatus]:
        """Choose one strike that satisfies every leg at once.

        Selecting each leg independently and then checking that the strikes
        happen to match would fail whenever one leg's best strike is illiquid,
        even though a perfectly good shared strike exists one step away. A
        straddle is one position, so its strike is one decision.
        """
        specification = context.specification
        targets = {
            self._target_strike(leg, reference)
            for leg in specification.legs
            if leg.strike_policy is not StrikeSelectionPolicy.TARGET_DELTA
        }
        if len(targets) != len(specification.legs) and any(
            leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA for leg in specification.legs
        ):
            attempt.reasons.append(
                "a same-strike strategy cannot use a delta-targeted leg: two legs targeting "
                "the same delta do not share a strike"
            )
            return None, ContractSelectionStatus.CONFIGURATION_ERROR
        if len(targets) != 1:
            attempt.reasons.append(
                "a same-strike strategy whose legs target different strikes is not "
                "expressible; check the strike policies in the strategy specification"
            )
            return None, ContractSelectionStatus.CONFIGURATION_ERROR

        target = targets.pop()
        if target is None:
            attempt.reasons.append(
                "no reference price was visible, so an at-the-money strike cannot be "
                "identified. It is never assumed."
            )
            return None, ContractSelectionStatus.REQUIRED_DATA_UNAVAILABLE

        by_strike: dict[Decimal, dict[int, ContractCandidate]] = {}
        for index, candidates in eligible.items():
            for candidate in candidates:
                assert candidate.strike is not None  # guaranteed by _is_usable
                by_strike.setdefault(candidate.strike, {})[index] = candidate

        shared = sorted(
            (strike for strike, legs in by_strike.items() if len(legs) == len(eligible)),
            key=lambda strike: (abs(strike - target), strike),
        )
        if not shared:
            attempt.reasons.append(
                f"no single strike carries a usable contract for every leg at "
                f"{expiration.isoformat()}"
            )
            return None, ContractSelectionStatus.NO_VALID_STRIKE

        strike = shared[0]
        distance = self._distance_pct(strike, target, reference)
        if distance is not None and distance > self._policy.strike.max_strike_distance_pct:
            for index, candidates in eligible.items():
                for candidate in candidates:
                    if candidate.strike == strike:
                        attempt.reject(
                            ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED,
                            candidate,
                            leg_index=index,
                            detail=(
                                f"the nearest shared strike sits {distance:.2f}% from the "
                                f"target, beyond the configured "
                                f"{self._policy.strike.max_strike_distance_pct}%"
                            ),
                        )
            return None, ContractSelectionStatus.NO_VALID_STRIKE

        selected: list[SelectedLeg] = []
        for leg in specification.legs:
            candidate = by_strike[strike][leg.index]
            for other in eligible[leg.index]:
                if other is not candidate:
                    attempt.reject(
                        ContractRejectionReason.NOT_SELECTED_BY_POLICY,
                        other,
                        leg_index=leg.index,
                        detail="valid, but a different strike is closer to the shared target",
                    )
            reason = (
                f"{leg.strike_policy.value}: {strike} is the listed strike nearest the "
                f"target of {target}, shared by every leg"
            )
            selected.append(
                self._leg(context, view, leg, candidate, dte, target, reference, reason)
            )

        problem = self._relationship_problem(context, selected)
        if problem is not None:
            attempt.reasons.append(problem)
            return None, ContractSelectionStatus.NO_VALID_CONTRACT
        return selected, ContractSelectionStatus.SUCCESS

    # --- candidate validity ------------------------------------------------
    def _is_usable(
        self,
        candidate: ContractCandidate,
        leg: LegSpecification,
        specification: StrategySpecification,
        view: ChainView,
        attempt: _Attempt,
    ) -> bool:
        """Whether a candidate survives every check, recording it if it does not."""
        problem = self._problem_with(candidate, leg, specification, view)
        if problem is None:
            return True
        reason, detail = problem
        attempt.reject(reason, candidate, leg_index=leg.index, detail=detail)
        return False

    def _problem_with(
        self,
        candidate: ContractCandidate,
        leg: LegSpecification,
        specification: StrategySpecification,
        view: ChainView,
    ) -> tuple[ContractRejectionReason, str | None] | None:
        """The first check this candidate fails, with the reason it earns.

        Ordered from identity outwards: a contract that is not identified at all
        should be rejected as unidentified rather than as illiquid, because the
        first is a fact about our data and the second would be a claim about a
        market.
        """
        if candidate.expiration is None:
            return (ContractRejectionReason.MISSING_EXPIRATION, None)
        if candidate.strike is None or candidate.strike <= 0:
            return (ContractRejectionReason.MISSING_STRIKE, None)
        if candidate.right is not leg.right:
            return (ContractRejectionReason.WRONG_RIGHT, None)
        if candidate.contract.underlying.upper() != view.symbol:
            return (ContractRejectionReason.WRONG_UNDERLYING, None)
        if candidate.contract_id is None:
            return (
                ContractRejectionReason.MISSING_CONTRACT_ID,
                "the store holds no broker contract id; a contract nobody can identify is "
                "not a contract that can be selected",
            )
        if not (candidate.trading_class or "").strip():
            return (
                ContractRejectionReason.INVALID_TRADING_CLASS,
                "the broker reported no trading class. It is never derived from the ticker: "
                "SPY options have been observed under class 2SPY",
            )
        if candidate.multiplier is None or candidate.multiplier < 1:
            return (ContractRejectionReason.INVALID_MULTIPLIER, None)

        quotes = self._policy.quotes
        if quotes.require_quote and not candidate.has_quote:
            return (
                ContractRejectionReason.MISSING_QUOTE,
                "no quote for this contract was collected; it cannot be priced, and an "
                "unpriced contract is not a selection",
            )
        if not candidate.research_usable and quotes.require_research_usable:
            return (ContractRejectionReason.QUOTE_NOT_RESEARCH_USABLE, None)
        if candidate.quote is not None:
            age = candidate.quote.source.age_seconds(view.as_of)
            if age > quotes.max_quote_age_seconds:
                return (
                    ContractRejectionReason.QUOTE_STALE,
                    f"the quote is {age:.0f}s old, beyond the configured "
                    f"{quotes.max_quote_age_seconds}s",
                )

        missing = _missing_fields(candidate, specification.required_option_fields)
        if missing:
            reason = {
                OptionDataField.DELTA: ContractRejectionReason.MISSING_DELTA,
                OptionDataField.IMPLIED_VOLATILITY: (
                    ContractRejectionReason.MISSING_IMPLIED_VOLATILITY
                ),
            }.get(missing[0], ContractRejectionReason.MISSING_REQUIRED_FIELD)
            return (
                reason,
                f"the strategy requires {', '.join(f.value for f in missing)}, which this "
                f"contract does not carry. It is not approximated from anything else.",
            )

        if leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA and candidate.delta is None:
            return (
                ContractRejectionReason.MISSING_DELTA,
                "the strike policy targets a delta and this contract has none; a delta is "
                "never estimated from memory or from a pricing model",
            )

        price = _limit_price(candidate)
        if price is not None and (
            price < specification.min_option_price_eur or price > specification.max_option_price_eur
        ):
            return (
                ContractRejectionReason.OPTION_PRICE_OUT_OF_RANGE,
                f"price {price} is outside [{specification.min_option_price_eur}, "
                f"{specification.max_option_price_eur}]",
            )

        spread = candidate.spread_pct
        if spread is not None and float(spread) > specification.max_bid_ask_spread_pct:
            return (
                ContractRejectionReason.SPREAD_TOO_WIDE,
                f"the quoted spread is {spread:.2f}% of the midpoint, beyond the "
                f"{specification.max_bid_ask_spread_pct}% ceiling",
            )

        implied = candidate.implied_volatility
        if implied is not None:
            low = specification.min_implied_volatility
            high = specification.max_implied_volatility
            if (low is not None and float(implied) < low) or (
                high is not None and float(implied) > high
            ):
                return (
                    ContractRejectionReason.IMPLIED_VOLATILITY_OUT_OF_RANGE,
                    f"implied volatility {implied} is outside [{low}, {high}]",
                )

        if specification.require_option_liquidity:
            return self._liquidity_problem(candidate, specification)
        return None

    def _liquidity_problem(
        self, candidate: ContractCandidate, specification: StrategySpecification
    ) -> tuple[ContractRejectionReason, str] | None:
        """Option-level liquidity only. Underlying volume is never a substitute.

        That the underlying trades hundreds of millions of shares says nothing
        about whether *this contract* has a market, and no rule here reads the
        first as evidence of the second.
        """
        unknown = self._policy.quotes.unknown_liquidity_policy is UnknownLiquidityPolicy.REJECT
        open_interest = candidate.open_interest
        volume = candidate.volume

        if open_interest is None or volume is None:
            if not unknown:
                return None
            absent = [
                name
                for name, value in (("open interest", open_interest), ("volume", volume))
                if value is None
            ]
            return (
                ContractRejectionReason.OPTION_LIQUIDITY_UNKNOWN,
                f"option-level {' and '.join(absent)} was never reported for this contract. "
                f"Missing is not zero and it is not 'fine'; set "
                f"quotes.unknown_liquidity_policy to ALLOW to admit it deliberately.",
            )
        if open_interest < specification.min_open_interest:
            return (
                ContractRejectionReason.LOW_OPTION_LIQUIDITY,
                f"open interest {open_interest} is below the strategy's floor of "
                f"{specification.min_open_interest}",
            )
        if volume < specification.min_daily_volume:
            return (
                ContractRejectionReason.LOW_OPTION_LIQUIDITY,
                f"option volume {volume} is below the strategy's floor of "
                f"{specification.min_daily_volume}",
            )
        return None

    # --- strike policies ---------------------------------------------------
    def _target_strike(self, leg: LegSpecification, reference: Decimal | None) -> Decimal | None:
        """What the leg's policy is aiming at, in strike terms.

        ``None`` for a delta-targeted leg — a delta is not a strike, and
        pretending otherwise is how a policy quietly becomes an approximation.
        """
        if reference is None:
            return None
        match leg.strike_policy:
            case StrikeSelectionPolicy.ATM:
                return reference
            case StrikeSelectionPolicy.OTM_PERCENT:
                offset = Decimal(str(leg.strike_offset_pct or 0.0)) / _HUNDRED
                if leg.right is OptionRight.CALL:
                    return reference * (Decimal(1) + offset)
                return reference * (Decimal(1) - offset)
            case StrikeSelectionPolicy.TARGET_DELTA:
                return None
        return None

    def _choose(
        self,
        leg: LegSpecification,
        candidates: Sequence[ContractCandidate],
        reference: Decimal | None,
        attempt: _Attempt,
    ) -> tuple[ContractCandidate, Decimal | None, str] | None:
        """Apply one leg's strike policy. Ties break on the lower strike."""
        if leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA:
            target_delta = Decimal(str(leg.target_delta))
            ordered = sorted(
                candidates,
                key=lambda c: (
                    abs((c.delta if c.delta is not None else Decimal(0)) - target_delta),
                    c.strike if c.strike is not None else Decimal(0),
                    c.contract_id or 0,
                ),
            )
            chosen = ordered[0]
            return (
                chosen,
                None,
                f"TARGET_DELTA: delta {chosen.delta} is the closest listed contract to the "
                f"configured target of {target_delta}",
            )

        target = self._target_strike(leg, reference)
        if target is None:
            attempt.reasons.append(
                f"leg {leg.index} uses {leg.strike_policy.value}, which needs a reference "
                f"price, and none was visible. A reference price is never assumed."
            )
            return None

        side: list[ContractCandidate] = []
        for candidate in candidates:
            if self._on_the_right_side(leg, candidate, reference):
                side.append(candidate)
            else:
                attempt.reject(
                    ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED,
                    candidate,
                    leg_index=leg.index,
                    detail=(
                        f"an out-of-the-money {leg.right.value} must sit "
                        f"{'above' if leg.right is OptionRight.CALL else 'below'} the "
                        f"reference price of {reference}"
                    ),
                )
        if not side:
            return None

        ordered = sorted(
            side,
            key=lambda c: (
                abs((c.strike if c.strike is not None else Decimal(0)) - target),
                c.strike if c.strike is not None else Decimal(0),
                c.contract_id or 0,
            ),
        )
        chosen = ordered[0]
        distance = self._distance_pct(chosen.strike, target, reference)
        if distance is not None and distance > self._policy.strike.max_strike_distance_pct:
            attempt.reject(
                ContractRejectionReason.STRIKE_POLICY_NOT_SATISFIED,
                chosen,
                leg_index=leg.index,
                detail=(
                    f"the nearest listed strike sits {distance:.2f}% from the target of "
                    f"{target}, beyond the configured "
                    f"{self._policy.strike.max_strike_distance_pct}%. The chain is too "
                    f"coarse to express this policy."
                ),
            )
            return None
        return (
            chosen,
            target,
            f"{leg.strike_policy.value}: {chosen.strike} is the listed strike nearest the "
            f"target of {target}",
        )

    @staticmethod
    def _on_the_right_side(
        leg: LegSpecification, candidate: ContractCandidate, reference: Decimal | None
    ) -> bool:
        """Out-of-the-money means the correct side of the reference price."""
        if leg.strike_policy is not StrikeSelectionPolicy.OTM_PERCENT or reference is None:
            return True
        strike = candidate.strike
        if strike is None:
            return False
        return strike >= reference if leg.right is OptionRight.CALL else strike <= reference

    @staticmethod
    def _distance_pct(
        strike: Decimal | None, target: Decimal | None, reference: Decimal | None
    ) -> float | None:
        if strike is None or target is None or reference is None or reference <= 0:
            return None
        return float(abs(strike - target) / reference * _HUNDRED)

    # --- multi-leg integrity ------------------------------------------------
    def _relationship_problem(
        self, context: SelectionContext, legs: Sequence[SelectedLeg]
    ) -> str | None:
        """Whether the chosen legs actually form the strategy they claim to.

        A multi-leg strategy is accepted whole or not at all: one leg that does
        not fit invalidates the structure, and a partially accepted straddle is
        a directional position nobody chose.
        """
        specification = context.specification
        if len(legs) != len(specification.legs):
            return "one or more legs could not be filled; a multi-leg strategy is never partial"
        if len({leg.expiration for leg in legs}) > 1 and specification.structure.same_expiration:
            return "the legs resolved to different expirations"
        if len({leg.multiplier for leg in legs}) > 1:
            return "the legs resolved to different contract multipliers"
        if len({leg.trading_class for leg in legs}) > 1:
            return "the legs resolved to different trading classes"

        match specification.strike_relationship:
            case StrikeRelationship.SAME:
                if len({leg.strike for leg in legs}) != 1:
                    return (
                        f"a {specification.strategy_id.value} requires one shared strike, but "
                        f"the legs resolved to "
                        f"{sorted(str(leg.strike) for leg in legs)}"
                    )
            case StrikeRelationship.CALL_ABOVE_PUT:
                call = next((leg for leg in legs if leg.right is OptionRight.CALL), None)
                put = next((leg for leg in legs if leg.right is OptionRight.PUT), None)
                if call is None or put is None:
                    return "a strangle needs one call and one put"
                if call.strike <= put.strike:
                    return (
                        f"a {specification.strategy_id.value} requires the call strike "
                        f"({call.strike}) strictly above the put strike ({put.strike}); the "
                        f"chain is too coarse to separate them, and the result would be a "
                        f"straddle under another name"
                    )
            case StrikeRelationship.NONE:
                pass
        return None

    # --- assembly ----------------------------------------------------------
    def _leg(
        self,
        context: SelectionContext,
        view: ChainView,
        leg: LegSpecification,
        candidate: ContractCandidate,
        dte: int,
        target: Decimal | None,
        reference: Decimal | None,
        reason: str,
    ) -> SelectedLeg:
        assert candidate.expiration is not None  # guaranteed by _is_usable
        assert candidate.strike is not None
        assert candidate.contract_id is not None
        assert candidate.multiplier is not None
        contract = candidate.contract
        return SelectedLeg(
            leg_index=leg.index,
            action=leg.action,
            right=leg.right,
            ratio=leg.ratio,
            underlying=view.symbol,
            expiration=candidate.expiration,
            dte=dte,
            strike=candidate.strike,
            multiplier=candidate.multiplier,
            trading_class=str(candidate.trading_class),
            contract_id=candidate.contract_id,
            exchange=contract.exchange,
            local_symbol=contract.local_symbol,
            currency=contract.currency,
            strike_policy=leg.strike_policy,
            target_strike=target,
            strike_distance_pct=self._distance_pct(candidate.strike, target, reference),
            reference_price=reference,
            selection_reason=reason,
            bid=candidate.bid,
            ask=candidate.ask,
            last=candidate.last,
            implied_volatility=candidate.implied_volatility,
            delta=candidate.delta,
            gamma=candidate.quote.gamma if candidate.quote else None,
            theta=candidate.quote.theta if candidate.quote else None,
            vega=candidate.quote.vega if candidate.quote else None,
            volume=candidate.volume,
            open_interest=candidate.open_interest,
            chain_snapshot_id=candidate.chain_snapshot_id,
            quote_snapshot_id=candidate.quote_snapshot_id,
            quote_as_of=candidate.quote_as_of,
        )

    def _success(
        self,
        context: SelectionContext,
        view: ChainView,
        expiration: date,
        dte: int,
        legs: list[SelectedLeg],
        attempt: _Attempt,
    ) -> ContractSelectionResult:
        return self._result(
            context,
            ContractSelectionStatus.SUCCESS,
            attempt,
            view=view,
            legs=legs,
            expiration=expiration,
            dte=dte,
            cost=_cost(legs),
        )

    def _failure(
        self,
        context: SelectionContext,
        status: ContractSelectionStatus,
        attempt: _Attempt,
        *,
        detail: str | None = None,
        view: ChainView | None = None,
    ) -> ContractSelectionResult:
        return self._result(context, status, attempt, view=view, detail=detail)

    def _result(
        self,
        context: SelectionContext,
        status: ContractSelectionStatus,
        attempt: _Attempt,
        *,
        view: ChainView | None,
        legs: list[SelectedLeg] | None = None,
        expiration: date | None = None,
        dte: int | None = None,
        cost: ContractCostEstimate | None = None,
        detail: str | None = None,
    ) -> ContractSelectionResult:
        limit = self._policy.limits.max_rejected_recorded
        rejections = sorted(
            attempt.rejections,
            key=lambda r: (
                r.reason.value,
                r.expiration.isoformat() if r.expiration else "",
                str(r.strike or ""),
                r.right.value if r.right else "",
            ),
        )
        specification = context.specification
        selected = legs or []
        return ContractSelectionResult(
            selection_id=selection_identifier(
                run_id=context.run_id,
                symbol=context.decision.symbol,
                as_of=context.as_of,
                status=status,
                strategy=specification.strategy_id,
                contract_ids=[leg.contract_id for leg in selected],
            ),
            run_id=context.run_id,
            symbol=context.decision.symbol,
            as_of=context.as_of,
            generated_at=self._clock.now(),
            selection_status=status,
            schema_version=STRATEGY_SCHEMA_VERSION,
            strategy=specification.strategy_id,
            strategy_version=specification.version,
            strategy_run_id=context.strategy_run_id,
            strategy_decision_id=context.decision.decision_id,
            research_report_id=context.research_report_id or context.decision.research_report_id,
            legs=selected,
            expiration=expiration,
            dte=dte,
            expiration_policy=specification.expiration_rule,
            expiration_reason=attempt.reasons[0] if attempt.reasons else None,
            reference_price=view.reference.value if view and view.reference else None,
            reference_price_field=view.reference.field if view and view.reference else None,
            cost=cost,
            selection_policy_version=self._policy.selection_policy_version,
            input_snapshot_ids=list(view.snapshot_ids) if view else [],
            reasons=attempt.reasons,
            rejected_candidates=rejections[:limit],
            candidates_considered=attempt.considered,
            rejections_recorded_truncated=len(rejections) > limit,
            versions=self._versions(specification.version),
            status_detail=detail,
        )

    def _versions(self, strategy_version: str) -> SystemVersions:
        return SystemVersions(
            application_version=application_version,
            config_version=self._config.application.config_version,
            strategy_spec_version=strategy_version,
            data_source_versions={
                "contract_selection": self._policy.config_version,
                "risk": self._config.risk.config_version,
            },
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _missing_fields(
    candidate: ContractCandidate, required: Iterable[OptionDataField]
) -> list[OptionDataField]:
    """Which required fields this candidate does not carry, in order."""
    available: dict[OptionDataField, object | None] = {
        OptionDataField.CONTRACT_ID: candidate.contract_id,
        OptionDataField.TRADING_CLASS: candidate.trading_class,
        OptionDataField.MULTIPLIER: candidate.multiplier,
        OptionDataField.BID: candidate.bid,
        OptionDataField.ASK: candidate.ask,
        OptionDataField.LAST: candidate.last,
        OptionDataField.IMPLIED_VOLATILITY: candidate.implied_volatility,
        OptionDataField.DELTA: candidate.delta,
        OptionDataField.GAMMA: candidate.quote.gamma if candidate.quote else None,
        OptionDataField.THETA: candidate.quote.theta if candidate.quote else None,
        OptionDataField.VEGA: candidate.quote.vega if candidate.quote else None,
        OptionDataField.VOLUME: candidate.volume,
        OptionDataField.OPEN_INTEREST: candidate.open_interest,
    }
    return [field_name for field_name in required if available.get(field_name) is None]


def _limit_price(candidate: ContractCandidate) -> Decimal | None:
    """The price a per-contract limit is measured against.

    The midpoint where both sides were quoted, otherwise the last trade,
    otherwise the ask. Never a value derived from one side of a quote and
    presented as a midpoint.
    """
    mid = candidate.mid
    if mid is not None:
        return mid
    if candidate.last is not None:
        return candidate.last
    return candidate.ask


def _cost(legs: Sequence[SelectedLeg]) -> ContractCostEstimate:
    """What one unit of the structure would cost, when the data supports it.

    The ask, not the midpoint, is the honest cost of buying: a midpoint fill is
    a hope, and this figure feeds a later sizing decision. The midpoint is
    reported alongside it where both sides exist, and nothing is reported at all
    when any leg is unquoted.

    There is no quantity here. This is the cost of the structure once, and
    turning it into a position size is the allocation engine's job.
    """
    missing = [leg for leg in legs if leg.ask is None]
    if missing:
        return ContractCostEstimate(
            available=False,
            unavailable_reason=(
                f"leg(s) {', '.join(str(leg.leg_index) for leg in missing)} have no ask; the "
                f"cost of buying an unquoted contract is unknown, and a midpoint is never "
                f"invented to stand in for one"
            ),
        )

    debit = Decimal(0)
    mid_debit: Decimal | None = Decimal(0)
    spreads: list[float] = []
    for leg in legs:
        assert leg.ask is not None
        units = Decimal(leg.multiplier) * Decimal(leg.ratio)
        debit += leg.ask * units
        mid = leg.mid
        if mid is None or mid_debit is None:
            mid_debit = None
        else:
            mid_debit += mid * units
        spread = leg.spread_pct
        if spread is not None:
            spreads.append(float(spread))

    return ContractCostEstimate(
        available=True,
        currency=next((leg.currency for leg in legs if leg.currency), None),
        estimated_debit=debit,
        estimated_mid_debit=mid_debit,
        max_leg_spread_pct=max(spreads) if spreads else None,
    )
