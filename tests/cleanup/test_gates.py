"""The safety gates, and the configuration values that fail to load.

Two kinds of test here, and both matter:

* **Gate verdicts**, which are what a run reads before deciding to submit;
* **Configuration refusals**, which are what stops a dangerous value being set
  in the first place. A gate that could be switched off in a file is a gate
  nobody can rely on, so the file refuses to load instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tests.cleanup.conftest import NOW, ORPHAN_CALL_ID, orphan_position, target_from
from tests.cleanup.factories import reconciliation_result
from tests.positions.factories import ACCOUNT
from trading_system.cleanup.gates import evaluate_run_gates, evaluate_target_gates
from trading_system.domain.enums import ReconciliationRunStatus
from trading_system.infrastructure.settings import CleanupConfig, Settings, SystemConfig
from trading_system.reconciliation.models import ReconciliationResult

pytestmark = pytest.mark.unit


def _result(
    *,
    status: ReconciliationRunStatus = ReconciliationRunStatus.MISMATCH,
    observed_at: datetime = NOW,
) -> ReconciliationResult:
    return reconciliation_result(status=status, observed_at=observed_at)


def _verdicts(
    settings: Settings,
    config: SystemConfig,
    *,
    authorized: bool = True,
    dry_run: bool = False,
    result: ReconciliationResult | None = None,
    target_count: int = 1,
    at=NOW,
    broker_account_id: str | None = ACCOUNT,
) -> dict[str, bool]:
    verdicts = evaluate_run_gates(
        settings=settings,
        cleanup=config.cleanup,
        execution=config.execution,
        authorized=authorized,
        dry_run=dry_run,
        result=result or _result(),
        target_count=target_count,
        at=at,
        broker_account_id=broker_account_id,
    )
    return {verdict.name: verdict.passed for verdict in verdicts}


# ---------------------------------------------------------------------------
# Mode and the live guards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["DRY_RUN", "LIVE"])
def test_cleanup_refuses_non_paper(mode: str, cleanup_enabled_config: SystemConfig) -> None:
    settings = Settings(
        _env_file=None,
        trading_mode=mode,
        live_trading_confirmed=mode == "LIVE",
        live_readiness_checklist_signed_off=mode == "LIVE",
        ibkr_account=ACCOUNT,
    )
    assert _verdicts(settings, cleanup_enabled_config)["TRADING_MODE_IS_PAPER"] is False


def test_paper_passes_the_mode_gate(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    assert _verdicts(settings, cleanup_enabled_config)["TRADING_MODE_IS_PAPER"] is True


def test_an_active_live_guard_refuses_even_in_paper(
    cleanup_enabled_config: SystemConfig,
) -> None:
    """Ambiguity fails closed: this stops before a connection exists."""
    settings = Settings(
        _env_file=None,
        trading_mode="PAPER",
        live_trading_confirmed=True,
        ibkr_account=ACCOUNT,
    )
    verdicts = _verdicts(settings, cleanup_enabled_config)
    assert verdicts["LIVE_TRADING_NOT_CONFIRMED"] is False
    assert verdicts["TRADING_MODE_IS_PAPER"] is True


def test_a_signed_off_checklist_refuses_too(cleanup_enabled_config: SystemConfig) -> None:
    settings = Settings(
        _env_file=None,
        trading_mode="PAPER",
        live_readiness_checklist_signed_off=True,
        ibkr_account=ACCOUNT,
    )
    assert _verdicts(settings, cleanup_enabled_config)["LIVE_CHECKLIST_NOT_SIGNED_OFF"] is False


# ---------------------------------------------------------------------------
# Authorisation and the master switches
# ---------------------------------------------------------------------------
def test_cleanup_requires_confirmation(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    assert (
        _verdicts(settings, cleanup_enabled_config, authorized=False)["EXPLICIT_AUTHORIZATION"]
        is False
    )


def test_the_shipped_configuration_disables_cleanup(system_config: SystemConfig) -> None:
    """The single tripwire for a checkout that could sell without an edit."""
    assert system_config.cleanup.enabled is False


def test_the_shipped_configuration_disables_execution(system_config: SystemConfig) -> None:
    assert system_config.execution.enabled is False


def test_both_master_switches_are_separate_gates(
    settings: Settings, system_config: SystemConfig
) -> None:
    cleanup_only = system_config.model_copy(
        update={
            "cleanup": system_config.cleanup.model_copy(update={"enabled": True}),
            "execution": system_config.execution.model_copy(update={"enabled": False}),
        }
    )
    verdicts = _verdicts(settings, cleanup_only)
    assert verdicts["CLEANUP_ENABLED"] is True
    assert verdicts["EXECUTION_ENABLED"] is False


# ---------------------------------------------------------------------------
# The evidence the targets rest on
# ---------------------------------------------------------------------------
def test_a_stale_reconciliation_is_refused(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    old = _result(observed_at=NOW - timedelta(hours=4))
    assert _verdicts(settings, cleanup_enabled_config, result=old)["RECONCILIATION_FRESH"] is False


@pytest.mark.parametrize(
    "status",
    [
        ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE,
        ReconciliationRunStatus.INTERNAL_DATA_UNAVAILABLE,
        ReconciliationRunStatus.CONFIGURATION_ERROR,
    ],
)
def test_a_reconciliation_that_compared_nothing_authorises_nothing(
    status: ReconciliationRunStatus, settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    """ "We could not look" is not "nothing accounts for this"."""
    verdicts = _verdicts(settings, cleanup_enabled_config, result=_result(status=status))
    assert verdicts["RECONCILIATION_USABLE"] is False


def test_a_match_is_a_usable_comparison(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    verdicts = _verdicts(
        settings, cleanup_enabled_config, result=_result(status=ReconciliationRunStatus.MATCH)
    )
    assert verdicts["RECONCILIATION_USABLE"] is True


def test_the_target_ceiling_stops_a_misconfiguration_becoming_a_liquidation(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    limit = cleanup_enabled_config.cleanup.max_targets_per_run
    assert _verdicts(settings, cleanup_enabled_config, target_count=limit)[
        "TARGET_COUNT_WITHIN_LIMIT"
    ]
    assert not _verdicts(settings, cleanup_enabled_config, target_count=limit + 1)[
        "TARGET_COUNT_WITHIN_LIMIT"
    ]


def test_no_targets_is_a_failed_gate_rather_than_a_silent_pass(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    assert _verdicts(settings, cleanup_enabled_config, target_count=0)["TARGETS_PRESENT"] is False


# ---------------------------------------------------------------------------
# The connected account
# ---------------------------------------------------------------------------
def test_the_connected_account_must_prove_it_is_a_paper_account(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    live_looking = Settings(_env_file=None, trading_mode="PAPER", ibkr_account="U1234567")
    verdicts = _verdicts(live_looking, cleanup_enabled_config, broker_account_id="U1234567")
    assert verdicts["BROKER_ACCOUNT_IS_PAPER"] is False


def test_an_unresolved_account_fails_rather_than_being_assumed(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    verdicts = _verdicts(settings, cleanup_enabled_config, broker_account_id=None)
    assert verdicts["BROKER_ACCOUNT_IS_PAPER"] is False
    assert verdicts["BROKER_ACCOUNT_MATCHES_EXPECTED"] is False


def test_a_connected_account_that_is_not_the_configured_one_is_refused(
    cleanup_enabled_config: SystemConfig,
) -> None:
    settings = Settings(_env_file=None, trading_mode="PAPER", ibkr_account="DU7654321")
    verdicts = _verdicts(settings, cleanup_enabled_config, broker_account_id="DU1234567")
    assert verdicts["BROKER_ACCOUNT_IS_PAPER"] is True
    assert verdicts["BROKER_ACCOUNT_MATCHES_EXPECTED"] is False


def test_the_account_gates_are_not_evaluated_on_a_review(
    settings: Settings, cleanup_enabled_config: SystemConfig
) -> None:
    """A review opens no connection, so it asserts nothing about one."""
    verdicts = _verdicts(settings, cleanup_enabled_config, dry_run=True, broker_account_id=None)
    assert "BROKER_ACCOUNT_IS_PAPER" not in verdicts


def test_every_gate_is_evaluated_even_after_one_fails(
    cleanup_enabled_config: SystemConfig,
) -> None:
    """An operator debugging a refusal needs the whole picture, not the first."""
    settings = Settings(
        _env_file=None,
        trading_mode="LIVE",
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
        ibkr_account="U1",
    )
    verdicts = evaluate_run_gates(
        settings=settings,
        cleanup=cleanup_enabled_config.cleanup,
        execution=cleanup_enabled_config.execution,
        authorized=False,
        dry_run=False,
        result=_result(status=ReconciliationRunStatus.BROKER_DATA_UNAVAILABLE),
        target_count=0,
        at=NOW,
        broker_account_id=None,
    )
    failed = [verdict.name for verdict in verdicts if not verdict.passed]
    assert len(failed) >= 5


# ---------------------------------------------------------------------------
# Per-holding gates
# ---------------------------------------------------------------------------
def _target_verdicts(target, config: SystemConfig, *, working=frozenset()) -> dict[str, bool]:
    return {
        verdict.name: verdict.passed
        for verdict in evaluate_target_gates(
            target=target, cleanup=config.cleanup, working_order_contract_ids=working
        )
    }


def test_a_working_broker_order_blocks_its_holding(
    cleanup_enabled_config: SystemConfig,
) -> None:
    target = target_from(orphan_position())
    assert _target_verdicts(target, cleanup_enabled_config)["NO_WORKING_BROKER_ORDER"] is True
    blocked = _target_verdicts(target, cleanup_enabled_config, working=frozenset({ORPHAN_CALL_ID}))
    assert blocked["NO_WORKING_BROKER_ORDER"] is False


def test_a_holding_the_broker_cannot_price_is_blocked(
    cleanup_enabled_config: SystemConfig,
) -> None:
    target = target_from(orphan_position(market_value=None))
    assert _target_verdicts(target, cleanup_enabled_config)["REFERENCE_PRICE_AVAILABLE"] is False


def test_a_holding_with_no_multiplier_is_blocked(
    cleanup_enabled_config: SystemConfig,
) -> None:
    target = target_from(orphan_position()).model_copy(update={"multiplier": None})
    assert _target_verdicts(target, cleanup_enabled_config)["MULTIPLIER_REPORTED"] is False


def test_a_short_holding_is_blocked(cleanup_enabled_config: SystemConfig) -> None:
    target = target_from(orphan_position(quantity=Decimal("-1")))
    assert _target_verdicts(target, cleanup_enabled_config)["HOLDING_IS_LONG"] is False


# ---------------------------------------------------------------------------
# Configuration that must fail to load
# ---------------------------------------------------------------------------
def _cleanup(**overrides: object) -> CleanupConfig:
    return CleanupConfig(**overrides)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("allow_live", True, "not less irreversible"),
        ("paper_only", False, "PAPER is the only mode"),
        ("require_explicit_authorization", False, "would let listing the orphan"),
        ("require_orphan_finding", False, "liquidates the account"),
        ("allow_short_positions", True, "unbounded above"),
        ("allow_partial_continuation", True, "not a trading strategy"),
    ],
)
def test_a_dangerous_cleanup_value_fails_to_load(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _cleanup(**{field: value})


def test_a_market_cleanup_order_fails_to_load() -> None:
    from trading_system.infrastructure.settings import CleanupOrderConfig

    with pytest.raises(ValueError, match="unbounded price"):
        CleanupOrderConfig(order_type="MARKET")


def test_reference_substitution_fails_to_load() -> None:
    from trading_system.infrastructure.settings import CleanupOrderConfig

    with pytest.raises(ValueError, match="not the average cost"):
        CleanupOrderConfig(allow_reference_substitution=True)


def test_a_positive_offset_fails_to_load() -> None:
    """The offset may only ever ask for less than the broker's own price."""
    from trading_system.infrastructure.settings import CleanupOrderConfig

    with pytest.raises(ValueError):
        CleanupOrderConfig(limit_price_offset_pct=5.0)


def test_the_shipped_order_policy_is_a_limit_below_the_reference(
    system_config: SystemConfig,
) -> None:
    order = system_config.cleanup.order
    assert order.order_type.value == "LIMIT"
    assert order.limit_price_offset_pct < 0
    assert order.price_increment > 0
    assert order.reference_price_field.value == "MARKET_PRICE"
