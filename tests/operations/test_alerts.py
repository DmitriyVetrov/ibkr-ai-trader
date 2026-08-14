"""Alert rules, and the one thing an alert must never be able to do.

An alert is a **notification**. Safety is enforced by the domain — the risk
engine refuses a trade, the exit engine blocks a position, reconciliation
reports a mismatch, the reservation ledger refuses to release ``UNKNOWN``
capital. These tests check that alerting says so and does nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trading_system.domain.enums import (
    AlertCategory,
    AlertCode,
    AlertSeverity,
    DailyPnLStatus,
    TradingMode,
)
from trading_system.infrastructure.settings import AlertThresholdConfig
from trading_system.operations.alerts import CATEGORY_OF, AlertFacts, AlertRules
from trading_system.operations.models import Alert, alert_identifier

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 35, tzinfo=UTC)


def facts(**overrides) -> AlertFacts:
    fields: dict[str, Any] = {"as_of": NOW, "trading_mode": TradingMode.PAPER}
    fields.update(overrides)
    return AlertFacts(**fields)


# ---------------------------------------------------------------------------
# The conditions the brief names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("given", "code"),
    [
        ({"broker_connection_errors": 5}, AlertCode.BROKER_CONNECTION_ERRORS),
        ({"broker_timeouts": 5}, AlertCode.BROKER_TIMEOUTS),
        ({"broker_unavailable": True}, AlertCode.BROKER_UNAVAILABLE),
        ({"unknown_execution_ids": ("execution-1",)}, AlertCode.EXECUTION_UNKNOWN),
        ({"execution_rejections": 5}, AlertCode.EXECUTION_REJECTION_RATE),
        ({"duplicate_execution_attempts": 2}, AlertCode.EXECUTION_DUPLICATE_ATTEMPT),
        ({"llm_errors": 5}, AlertCode.LLM_ERROR_RATE),
        ({"research_failures": 5}, AlertCode.RESEARCH_FAILURE_RATE),
        ({"failed_jobs": ("reconciliation", "position_monitor")}, AlertCode.SCHEDULER_JOB_FAILED),
        ({"unknown_jobs": ("reconciliation",)}, AlertCode.SCHEDULER_JOB_UNKNOWN),
        (
            {"critical_reconciliation_findings": ("POSITION_QUANTITY_MISMATCH",)},
            AlertCode.RECONCILIATION_MISMATCH,
        ),
        ({"exit_unknown_position_ids": ("position-1",)}, AlertCode.EXIT_UNKNOWN),
        (
            {"positions_close_not_confirmed": ("position-1",)},
            AlertCode.POSITION_CLOSE_NOT_CONFIRMED,
        ),
        ({"positions_near_expiration": ("position-1",)}, AlertCode.EXPIRATION_APPROACHING),
        ({"blocked_settlements": ("reservation-1",)}, AlertCode.SETTLEMENT_BLOCKED),
    ],
)
def test_a_condition_raises_its_alert(system_config, given, code) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(facts(**given))

    assert code in {alert.code for alert in alerts}


def test_a_live_execution_attempt_is_critical(system_config) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(trading_mode=TradingMode.LIVE, live_execution_attempts=1)
    )

    alert = next(a for a in alerts if a.code is AlertCode.LIVE_EXECUTION_ATTEMPT)
    assert alert.severity is AlertSeverity.CRITICAL


def test_a_daily_loss_breach_raises_an_alert(system_config) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(daily_loss_exceeded=True, daily_loss="800.00", daily_loss_limit="750")
    )

    alert = next(a for a in alerts if a.code is AlertCode.DAILY_LOSS_THRESHOLD_EXCEEDED)
    assert alert.severity is AlertSeverity.CRITICAL


def test_an_unknown_daily_result_has_its_own_alert(system_config) -> None:
    """'We could not determine today's result' is not 'there were no losses',
    and the two must not share an alert."""
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(daily_pnl_status=DailyPnLStatus.UNKNOWN)
    )

    codes = {alert.code for alert in alerts}
    assert AlertCode.DAILY_LOSS_UNAVAILABLE in codes
    assert AlertCode.DAILY_LOSS_THRESHOLD_EXCEEDED not in codes


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
def test_a_count_below_the_threshold_raises_nothing(system_config) -> None:
    """One broker timeout on a Monday morning is weather."""
    threshold = system_config.alerts.rules["BROKER_TIMEOUTS"].threshold
    alerts = AlertRules(system_config.alerts).evaluate(facts(broker_timeouts=threshold - 1))

    assert AlertCode.BROKER_TIMEOUTS not in {alert.code for alert in alerts}


def test_a_count_at_the_threshold_fires(system_config) -> None:
    threshold = system_config.alerts.rules["BROKER_TIMEOUTS"].threshold
    alerts = AlertRules(system_config.alerts).evaluate(facts(broker_timeouts=threshold))

    assert AlertCode.BROKER_TIMEOUTS in {alert.code for alert in alerts}


def test_one_unknown_execution_is_enough(system_config) -> None:
    """It may be a live order. There is no count at which that becomes routine."""
    assert system_config.alerts.rules["EXECUTION_UNKNOWN"].threshold == 1


def test_a_quiet_system_raises_nothing(system_config) -> None:
    assert AlertRules(system_config.alerts).evaluate(facts()) == []


def test_a_disabled_rule_does_not_fire(system_config) -> None:
    rules = dict(system_config.alerts.rules)
    rules["BROKER_TIMEOUTS"] = rules["BROKER_TIMEOUTS"].model_copy(
        update={"enabled": False, "severity": AlertSeverity.WARNING}
    )
    config = system_config.alerts.model_copy(update={"rules": rules})

    alerts = AlertRules(config).evaluate(facts(broker_timeouts=99))

    assert AlertCode.BROKER_TIMEOUTS not in {alert.code for alert in alerts}


def test_a_critical_rule_cannot_be_disabled() -> None:
    """Turning off the notification does not turn off the condition, and the
    safety alerts are the ones most easily muted by accident."""
    with pytest.raises(ValueError, match="CRITICAL"):
        AlertThresholdConfig(enabled=False, severity=AlertSeverity.CRITICAL)


def test_the_shipped_configuration_defines_every_required_rule(system_config) -> None:
    for required in type(system_config.alerts).REQUIRED_RULES:
        assert required in system_config.alerts.rules


def test_every_alert_code_has_a_category() -> None:
    """A code with no category would be an alert nobody could route."""
    assert set(CATEGORY_OF) == set(AlertCode)
    assert set(CATEGORY_OF.values()) <= set(AlertCategory)


# ---------------------------------------------------------------------------
# Identity and noise
# ---------------------------------------------------------------------------
def test_the_same_condition_in_the_same_window_is_the_same_alert(system_config) -> None:
    """Re-firing an unresolved condition every five minutes is how a channel
    becomes noise and then becomes ignored."""
    rules = AlertRules(system_config.alerts)
    first = rules.evaluate(facts(unknown_execution_ids=("execution-1",)))
    second = rules.evaluate(facts(unknown_execution_ids=("execution-1",)))

    assert first[0].alert_id == second[0].alert_id


def test_a_different_window_is_a_different_alert() -> None:
    first = alert_identifier(
        code=AlertCode.EXECUTION_UNKNOWN, subject="execution", window_start=NOW
    )
    from datetime import timedelta

    second = alert_identifier(
        code=AlertCode.EXECUTION_UNKNOWN,
        subject="execution",
        window_start=NOW + timedelta(hours=2),
    )

    assert first != second


def test_alerts_are_ordered_most_severe_first(system_config) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(
            broker_timeouts=5,
            unknown_execution_ids=("execution-1",),
            telemetry_export_failures=10,
        )
    )

    severities = [alert.severity for alert in alerts]
    assert severities[0] is AlertSeverity.CRITICAL
    assert severities[-1] is AlertSeverity.INFO


# ---------------------------------------------------------------------------
# An alert cannot trade
# ---------------------------------------------------------------------------
def test_an_alert_never_recommends_a_trade() -> None:
    """The moment an operational message contains 'sell 4 NVDA calls',
    somebody automates it, and the safety boundary has a hole nobody
    reviewed."""
    with pytest.raises(ValueError, match="never names a trade"):
        Alert(
            alert_id="alert-1",
            code=AlertCode.EXIT_UNKNOWN,
            category=AlertCategory.POSITION,
            severity=AlertSeverity.CRITICAL,
            subject="position",
            summary="an exit is unresolved",
            raised_at=NOW,
            window_start=NOW,
            window_end=NOW,
            trading_mode=TradingMode.PAPER,
            recommended_action="sell the remaining contracts at market",
        )


def test_every_shipped_recommendation_is_addressed_to_a_person(system_config) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(
            broker_unavailable=True,
            unknown_execution_ids=("execution-1",),
            critical_reconciliation_findings=("POSITION_QUANTITY_MISMATCH",),
            exit_unknown_position_ids=("position-1",),
            blocked_settlements=("reservation-1",),
            daily_pnl_status=DailyPnLStatus.UNKNOWN,
        )
    )

    assert alerts
    for alert in alerts:
        # Construction already refuses an instruction to trade; this asserts
        # every shipped rule actually says something useful instead.
        assert alert.recommended_action
        assert len(alert.recommended_action) > 20


def test_an_alert_carries_ids_rather_than_payloads(system_config) -> None:
    alerts = AlertRules(system_config.alerts).evaluate(
        facts(unknown_execution_ids=("execution-abc",))
    )
    alert = next(a for a in alerts if a.code is AlertCode.EXECUTION_UNKNOWN)

    assert alert.references["execution_ids"] == "execution-abc"
    assert not any(key in alert.references for key in ("account", "balance", "prompt", "portfolio"))
