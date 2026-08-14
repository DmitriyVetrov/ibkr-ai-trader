"""The daily loss limit, and the three states its input can be in.

Milestone 7 recorded the limit as ``NOT_EVALUATED`` because nothing tracked
realised profit and loss. Milestone 11 tracks it, and the interesting part is
not the arithmetic — it is that there are now **three** answers rather than two,
and collapsing any pair of them is how an unmeasured day passes a loss limit:

``TRACKED``
    every closure produced a usable figure. Evaluate against a real number.
``UNKNOWN``
    closures happened and at least one produced nothing. Not zero loss.
``NOT_TRACKED``
    no ledger was consulted. Also not zero loss, and a different fact again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.domain.enums import (
    DailyPnLStatus,
    RiskCheckOutcome,
    RiskReasonCode,
)
from trading_system.risk.engine import RiskEngine
from trading_system.risk.models import CampaignSnapshot

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


def _daily_check(engine: RiskEngine, campaign: CampaignSnapshot, candidate, account):
    evaluation = engine.evaluate(candidate, campaign, as_of=NOW, account=account)
    return next(check for check in evaluation.checks if check.name == "daily_loss")


# ---------------------------------------------------------------------------
# TRACKED: a real number
# ---------------------------------------------------------------------------
def test_a_profitable_day_passes(make_campaign, make_candidate, make_account, risk_limits):
    campaign = make_campaign(realized_pnl_today=Decimal("450.00"))
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.PASS
    assert check.actual == "0"


def test_a_losing_day_within_the_limit_passes(
    make_campaign, make_candidate, make_account, risk_limits
):
    campaign = make_campaign(realized_pnl_today=Decimal("-100.00"))
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.PASS
    assert check.actual == "100.00"


def test_a_loss_exactly_at_the_limit_still_passes(
    make_campaign, make_candidate, make_account, risk_limits
):
    """The limit is a ceiling that may be reached, not one that may be neared.

    Enforced with ``>`` rather than ``>=``, deliberately: a limit of 750 that
    refused at 750 would be a limit of 749.99 nobody wrote down.
    """
    limit = risk_limits.max_daily_loss
    campaign = make_campaign(realized_pnl_today=-limit)
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.PASS


def test_a_loss_beyond_the_limit_is_rejected(
    make_campaign, make_candidate, make_account, risk_limits
):
    campaign = make_campaign(realized_pnl_today=-(risk_limits.max_daily_loss + Decimal("0.01")))
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.FAIL
    assert check.reason_code is RiskReasonCode.DAILY_LOSS_LIMIT_REACHED


# ---------------------------------------------------------------------------
# UNKNOWN: an absence of knowledge about a day on which money moved
# ---------------------------------------------------------------------------
def test_an_unknown_day_is_not_treated_as_zero_loss(
    make_campaign, make_candidate, make_account, risk_limits
):
    campaign = make_campaign(
        daily_pnl_status=DailyPnLStatus.UNKNOWN,
        unavailable_pnl_position_ids=["position-nvda-1"],
    )
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is not RiskCheckOutcome.PASS
    assert check.reason_code is RiskReasonCode.DAILY_LOSS_UNKNOWN


def test_an_unknown_day_blocks_by_default(make_campaign, make_candidate, make_account, risk_limits):
    """``block_on_unknown_daily_loss`` ships true: once the ledger exists, a
    figure it could not produce is evidence that something is wrong."""
    assert risk_limits.block_on_unknown_daily_loss is True

    campaign = make_campaign(
        daily_pnl_status=DailyPnLStatus.UNKNOWN,
        unavailable_pnl_position_ids=["position-nvda-1"],
    )
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.FAIL


def test_configuration_can_let_an_unknown_day_proceed_unevaluated(
    make_campaign, make_candidate, make_account, risk_limits
):
    """Explicit, and still never rendered as a pass."""
    limits = risk_limits.model_copy(
        update={"block_on_unknown_daily_loss": False, "require_daily_loss_tracking": False}
    )
    campaign = make_campaign(
        daily_pnl_status=DailyPnLStatus.UNKNOWN,
        unavailable_pnl_position_ids=["position-nvda-1"],
    )
    check = _daily_check(RiskEngine(limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.NOT_EVALUATED
    assert check.reason_code is RiskReasonCode.DAILY_LOSS_UNKNOWN


def test_an_unknown_day_names_the_position_that_produced_no_result(
    make_campaign, make_candidate, make_account, risk_limits
):
    campaign = make_campaign(
        daily_pnl_status=DailyPnLStatus.UNKNOWN,
        unavailable_pnl_position_ids=["position-nvda-1", "position-spy-2"],
    )
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.detail is not None
    assert "position-nvda-1" in check.detail


# ---------------------------------------------------------------------------
# NOT_TRACKED: a different fact again
# ---------------------------------------------------------------------------
def test_an_untracked_day_is_unevaluated_rather_than_passed(
    make_campaign, make_candidate, make_account, risk_limits
):
    campaign = make_campaign()
    check = _daily_check(RiskEngine(risk_limits), campaign, make_candidate(), make_account())

    assert check.outcome is RiskCheckOutcome.NOT_EVALUATED
    assert check.actual is None


def test_untracked_and_unknown_are_different_reason_codes(
    make_campaign, make_candidate, make_account, risk_limits
):
    """'We have no tracking' and 'we tracked it and the number is not
    trustworthy' call for different responses."""
    limits = risk_limits.model_copy(update={"require_daily_loss_tracking": True})

    untracked = _daily_check(RiskEngine(limits), make_campaign(), make_candidate(), make_account())
    unknown = _daily_check(
        RiskEngine(limits),
        make_campaign(
            daily_pnl_status=DailyPnLStatus.UNKNOWN,
            unavailable_pnl_position_ids=["position-nvda-1"],
        ),
        make_candidate(),
        make_account(),
    )

    assert untracked.reason_code is RiskReasonCode.DAILY_LOSS_NOT_TRACKED
    assert unknown.reason_code is RiskReasonCode.DAILY_LOSS_UNKNOWN


# ---------------------------------------------------------------------------
# The shapes that cannot exist
# ---------------------------------------------------------------------------
def test_a_snapshot_cannot_carry_a_figure_it_does_not_trust() -> None:
    """A comfortable number next to 'we could not measure today' is exactly how
    an unmeasured day would pass a loss limit."""
    with pytest.raises(ValueError, match="not trustworthy"):
        CampaignSnapshot(
            campaign_id="campaign-001",
            as_of=NOW,
            currency="EUR",
            budget=Decimal("5000"),
            reserve=Decimal("1000.00"),
            realized_pnl_today=Decimal("-10.00"),
            daily_pnl_status=DailyPnLStatus.UNKNOWN,
        )


def test_a_tracked_snapshot_must_carry_a_figure() -> None:
    with pytest.raises(ValueError, match="TRACKED"):
        CampaignSnapshot(
            campaign_id="campaign-001",
            as_of=NOW,
            currency="EUR",
            budget=Decimal("5000"),
            reserve=Decimal("1000.00"),
            realized_pnl_today=None,
            daily_pnl_status=DailyPnLStatus.TRACKED,
        )


# ---------------------------------------------------------------------------
# The day boundary
# ---------------------------------------------------------------------------
def test_the_day_is_bounded_in_exchange_local_time() -> None:
    """A closure at 21:30 UTC belongs to the New York session that has ended.

    Bounding the day in UTC would file an afternoon's losses under tomorrow,
    and a losing day would look like two quiet ones to the limit.
    """
    from trading_system.pnl.calculator import session_date_of

    late_afternoon_new_york = datetime(2026, 8, 10, 21, 30, tzinfo=UTC)
    assert session_date_of(late_afternoon_new_york, "America/New_York") == date(2026, 8, 10)

    just_after_midnight_utc = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
    assert session_date_of(just_after_midnight_utc, "America/New_York") == date(2026, 8, 10)
    assert just_after_midnight_utc.date() == date(2026, 8, 11)
