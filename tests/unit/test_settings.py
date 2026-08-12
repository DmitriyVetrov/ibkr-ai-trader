"""Configuration loading, and the guards that keep LIVE mode out of reach."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.domain.enums import MarketHypothesis, StrategyType, TradingMode
from trading_system.infrastructure.settings import (
    ConfigError,
    Settings,
    SystemConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# Environment settings
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_paper_is_the_default_mode() -> None:
    settings = Settings(_env_file=None)
    assert settings.trading_mode is TradingMode.PAPER
    assert settings.is_live is False


@pytest.mark.unit
def test_live_tests_are_disabled_by_default() -> None:
    assert Settings(_env_file=None).allow_live_tests is False


@pytest.mark.unit
def test_live_mode_is_refused_without_guards() -> None:
    """Setting one variable must not be enough to unlock real money."""
    with pytest.raises(ValueError, match="LIVE refused"):
        Settings(_env_file=None, trading_mode=TradingMode.LIVE)


@pytest.mark.unit
def test_live_mode_is_refused_with_only_one_guard() -> None:
    with pytest.raises(ValueError, match="LIVE_READINESS_CHECKLIST_SIGNED_OFF"):
        Settings(
            _env_file=None,
            trading_mode=TradingMode.LIVE,
            live_trading_confirmed=True,
        )


@pytest.mark.unit
def test_live_mode_requires_every_guard() -> None:
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE,
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
    )
    assert settings.is_live is True


@pytest.mark.unit
def test_dry_run_does_not_reach_a_broker() -> None:
    assert Settings(_env_file=None, trading_mode=TradingMode.DRY_RUN).submits_real_orders is False
    assert Settings(_env_file=None, trading_mode=TradingMode.PAPER).submits_real_orders is True


@pytest.mark.unit
def test_secrets_are_masked_in_representations() -> None:
    settings = Settings(_env_file=None, ibkr_password="hunter2")
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert settings.ibkr_password is not None
    assert settings.ibkr_password.get_secret_value() == "hunter2"


@pytest.mark.unit
def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "DRY_RUN")
    assert Settings(_env_file=None).trading_mode is TradingMode.DRY_RUN


# ---------------------------------------------------------------------------
# YAML configuration
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_configuration_loads(system_config: SystemConfig) -> None:
    assert system_config.application.config_version
    assert system_config.campaign.budget_eur == Decimal("5000")
    assert system_config.risk.dte_min == 14
    assert system_config.risk.dte_max == 30


@pytest.mark.unit
def test_money_in_config_is_decimal_not_float(system_config: SystemConfig) -> None:
    for value in (
        system_config.campaign.budget_eur,
        system_config.risk.min_option_price_eur,
        system_config.risk.max_daily_loss_eur,
    ):
        assert isinstance(value, Decimal)
    # 0.30 as a binary float is 0.29999...; as an exact decimal it is not.
    assert system_config.risk.min_option_price_eur == Decimal("0.30")


@pytest.mark.unit
def test_all_four_strategies_are_defined(system_config: SystemConfig) -> None:
    defined = {s.strategy_type for s in system_config.strategies.values()}
    assert defined == {
        StrategyType.LONG_CALL,
        StrategyType.LONG_PUT,
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
    }


@pytest.mark.unit
def test_every_strategy_declares_applicable_hypotheses(system_config: SystemConfig) -> None:
    for name, strategy in system_config.strategies.items():
        assert strategy.applicable_hypotheses, f"{name} matches no hypothesis"
        assert all(isinstance(h, MarketHypothesis) for h in strategy.applicable_hypotheses)


@pytest.mark.unit
def test_multi_leg_strategies_are_closed_as_a_unit(system_config: SystemConfig) -> None:
    """Independent leg exits must be opt-in per strategy, and are off by default."""
    for name in ("long_straddle", "long_strangle"):
        assert system_config.strategies[name].exit_policy.allow_independent_leg_exit is False


@pytest.mark.unit
def test_strategy_dte_windows_sit_inside_the_risk_window(system_config: SystemConfig) -> None:
    for name, strategy in system_config.strategies.items():
        assert strategy.dte_min >= system_config.risk.dte_min, name
        assert strategy.dte_max <= system_config.risk.dte_max, name


@pytest.mark.unit
def test_required_scheduled_jobs_are_present(system_config: SystemConfig) -> None:
    for job in (
        "data_collection",
        "universe_refresh",
        "opportunity_scan",
        "position_monitor",
        "thesis_monitor",
        "reconciliation",
        "end_of_day_report",
    ):
        assert job in system_config.schedules.jobs


@pytest.mark.unit
def test_campaign_reserves_part_of_the_budget(system_config: SystemConfig) -> None:
    """The allocator must not be able to spend the whole budget."""
    assert system_config.campaign.reserve_fraction > 0


@pytest.mark.unit
def test_source_tiers_are_ordered_by_trust(system_config: SystemConfig) -> None:
    assert "sec.gov" in system_config.sources.tier_1
    assert "reuters.com" in system_config.sources.tier_2


# ---------------------------------------------------------------------------
# Configuration failure modes
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_missing_config_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope")


@pytest.mark.unit
def test_unknown_key_is_rejected(tmp_config_dir: Path) -> None:
    """A typo in a risk limit must fail loudly, not be silently ignored."""
    risk = tmp_config_dir / "risk.yaml"
    risk.write_text(risk.read_text(encoding="utf-8") + '\nmax_dialy_loss_eur: "100"\n')

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


@pytest.mark.unit
def test_unquoted_decimal_in_config_is_rejected(tmp_config_dir: Path) -> None:
    """An unquoted 0.30 is a binary float and must not reach a money field."""
    risk = tmp_config_dir / "risk.yaml"
    risk.write_text(
        risk.read_text(encoding="utf-8").replace(
            'min_option_price_eur: "0.30"', "min_option_price_eur: 0.30"
        )
    )

    with pytest.raises(ConfigError, match="binary floating point"):
        load_config(tmp_config_dir)


@pytest.mark.unit
def test_strategy_widening_the_risk_dte_window_is_rejected(tmp_config_dir: Path) -> None:
    call = tmp_config_dir / "strategies" / "long_call.yaml"
    call.write_text(call.read_text(encoding="utf-8").replace("dte_max: 30", "dte_max: 45"))

    with pytest.raises(ConfigError, match="widens a global risk limit"):
        load_config(tmp_config_dir)


@pytest.mark.unit
def test_strategy_widening_any_other_risk_limit_is_rejected(tmp_config_dir: Path) -> None:
    """The rule is about which layer owns a limit, not about DTE specifically.

    A strategy file that could lower the open-interest floor would let a
    strategy specification overrule the risk policy — the inversion the whole
    architecture exists to prevent.
    """
    call = tmp_config_dir / "strategies" / "long_call.yaml"
    call.write_text(
        call.read_text(encoding="utf-8").replace("min_open_interest: 500", "min_open_interest: 1")
    )

    with pytest.raises(ConfigError, match="min_open_interest"):
        load_config(tmp_config_dir)


@pytest.mark.unit
def test_missing_required_job_is_rejected(tmp_config_dir: Path) -> None:
    schedules = tmp_config_dir / "schedules.yaml"
    content = schedules.read_text(encoding="utf-8")
    start = content.index("  reconciliation:")
    end = content.index("  end_of_day_report:")
    schedules.write_text(content[:start] + content[end:])

    with pytest.raises(ConfigError, match="reconciliation"):
        load_config(tmp_config_dir)


@pytest.mark.unit
def test_missing_strategy_directory_is_an_error(tmp_config_dir: Path) -> None:
    import shutil

    shutil.rmtree(tmp_config_dir / "strategies")
    with pytest.raises(ConfigError, match="strategy directory"):
        load_config(tmp_config_dir)


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_project_root_finds_the_config_tree() -> None:
    from trading_system.infrastructure.settings import project_root

    root = project_root()
    assert (root / "config").is_dir()
    assert (root / "schemas").is_dir()


@pytest.mark.unit
def test_project_root_honours_an_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_system.infrastructure.settings import project_root

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()


@pytest.mark.unit
def test_project_root_falls_back_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed-package layout: config/ sits at the working directory.

    Without this the container silently reports "no configuration" instead of
    failing loudly at startup.
    """
    import trading_system.infrastructure.settings as settings_module
    from trading_system.infrastructure.settings import project_root

    (tmp_path / "config").mkdir()
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # Simulate site-packages: the source-derived path has no config/ beside it.
    monkeypatch.setattr(settings_module, "__file__", str(tmp_path / "sp" / "a" / "b" / "s.py"))

    assert project_root() == tmp_path
