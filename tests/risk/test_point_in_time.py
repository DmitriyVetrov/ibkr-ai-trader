"""Look-ahead protection in the risk engine (brief sections 12, 37.7).

Every test here asserts the same thing from a different direction: a record
that was not knowable at the decision instant must **fail the candidate**, not
be filtered out, not be repaired, and not be quietly used anyway.

The distinction that makes this worth its own file is that a look-ahead leak is
a *correctness bug*, never a market outcome. It gets its own reason code for
that reason: "we declined because the campaign was full" and "we used tomorrow's
price by mistake" want completely different responses, and a system that
reported them identically would train someone to ignore both.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import RiskOutcome, RiskReasonCode
from trading_system.risk.engine import RiskEngine

from .conftest import NOW

pytestmark = pytest.mark.unit

FUTURE = NOW + timedelta(hours=1)


def _evaluate(limits, candidate, campaign, **kwargs):
    return RiskEngine(limits).evaluate(candidate, campaign, as_of=NOW, **kwargs)


def _rejects_for_look_ahead(evaluation) -> bool:
    return (
        evaluation.outcome is RiskOutcome.REJECTED
        and RiskReasonCode.POINT_IN_TIME_ERROR in evaluation.reason_codes
    )


def test_current_information_is_accepted(risk_limits, make_candidate, make_campaign, make_account):
    """The control: everything stamped exactly at the decision instant passes."""
    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED


def test_historical_information_is_accepted(
    risk_limits, make_candidate, make_campaign, make_account
):
    earlier = NOW - timedelta(minutes=2)
    candidate = make_candidate(price_overrides={"quote_as_of": earlier})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED


def test_the_exact_timestamp_boundary_is_visible(
    risk_limits, make_candidate, make_campaign, make_account
):
    """A record stamped exactly at T was knowable at T."""
    candidate = make_candidate(as_of=NOW, price_overrides={"quote_as_of": NOW})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert evaluation.outcome is RiskOutcome.APPROVED


def test_a_future_candidate_fails_closed(risk_limits, make_candidate, make_campaign, make_account):
    evaluation = _evaluate(
        risk_limits, make_candidate(as_of=FUTURE), make_campaign(), account=make_account()
    )

    assert _rejects_for_look_ahead(evaluation)


def test_a_future_quote_fails_closed(risk_limits, make_candidate, make_campaign, make_account):
    """A price that had not printed yet cannot size a position."""
    candidate = make_candidate(price_overrides={"quote_as_of": FUTURE})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert _rejects_for_look_ahead(evaluation)


def test_a_future_leg_quote_fails_closed(
    risk_limits, make_candidate, make_campaign, make_account, make_leg
):
    candidate = make_candidate(legs=[make_leg(quote_as_of=FUTURE)])

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert _rejects_for_look_ahead(evaluation)


def test_a_future_campaign_snapshot_fails_closed(
    risk_limits, make_candidate, make_campaign, make_account
):
    evaluation = _evaluate(
        risk_limits, make_candidate(), make_campaign(as_of=FUTURE), account=make_account()
    )

    assert _rejects_for_look_ahead(evaluation)


def test_a_future_account_snapshot_fails_closed(
    risk_limits, make_candidate, make_campaign, make_account
):
    account = make_account(as_of=FUTURE, captured_at=FUTURE)

    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=account)

    assert _rejects_for_look_ahead(evaluation)


def test_an_account_captured_later_than_it_describes_fails_closed(
    risk_limits, make_candidate, make_campaign, make_account
):
    """Retrieval binds: a balance we had not yet fetched was not a balance we had."""
    account = make_account(as_of=NOW, captured_at=FUTURE)

    evaluation = _evaluate(risk_limits, make_candidate(), make_campaign(), account=account)

    assert _rejects_for_look_ahead(evaluation)


def test_a_future_reservation_fails_closed(
    risk_limits, make_candidate, make_campaign, make_account, make_reservation
):
    """Capital committed after the decision instant did not constrain it."""
    campaign = make_campaign(
        open_positions=[
            make_reservation(
                authorized_at=FUTURE,
                capital_committed=Decimal("100.00"),
                max_loss=Decimal("100.00"),
            )
        ]
    )

    evaluation = _evaluate(risk_limits, make_candidate(), campaign, account=make_account())

    assert _rejects_for_look_ahead(evaluation)


def test_a_look_ahead_violation_is_not_repaired(
    risk_limits, make_candidate, make_campaign, make_account
):
    """The offending record stays exactly as stored; nothing is filtered away."""
    candidate = make_candidate(price_overrides={"quote_as_of": FUTURE})

    evaluation = _evaluate(risk_limits, candidate, make_campaign(), account=make_account())

    assert _rejects_for_look_ahead(evaluation)
    assert candidate.price.quote_as_of == FUTURE
    check = next(c for c in evaluation.checks if c.name == "point_in_time")
    assert "not knowable at" in (check.detail or "")


def test_a_look_ahead_error_is_distinct_from_a_market_outcome(
    risk_limits, make_candidate, make_campaign, make_account
):
    """A correctness bug must not be reported as a limit breach."""
    candidate = make_candidate(as_of=FUTURE)

    codes = set(
        _evaluate(risk_limits, candidate, make_campaign(), account=make_account()).reason_codes
    )

    assert RiskReasonCode.POINT_IN_TIME_ERROR in codes
    assert RiskReasonCode.INSUFFICIENT_CAMPAIGN_BUDGET not in codes
    assert RiskReasonCode.BELOW_MIN_OPPORTUNITY_SCORE not in codes
