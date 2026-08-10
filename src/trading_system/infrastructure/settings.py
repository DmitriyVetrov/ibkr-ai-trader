"""Environment settings and YAML configuration loading.

Two distinct concerns live here:

* :class:`Settings` — *secrets and deployment switches*, read from the
  environment / ``.env``. Never committed.
* :class:`SystemConfig` — *trading policy*, read from ``config/*.yaml``.
  Committed, reviewable, and version-stamped into every trade record.

The split matters: a reviewer must be able to read the risk policy in Git
without ever seeing a credential, and changing a risk limit must show up in a
diff.

Monetary values in YAML must be quoted (``"0.50"``) or written as integers.
An unquoted ``0.50`` is parsed as a binary float and is rejected, by design
(specification section 21).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_system.domain.enums import MarketHypothesis, StrategyType, TradingMode
from trading_system.domain.models import Money

__all__ = [
    "ApplicationConfig",
    "CampaignConfig",
    "ConfigError",
    "ExitPolicyConfig",
    "LiquidityConfig",
    "RiskConfig",
    "ScheduleJob",
    "SchedulesConfig",
    "Settings",
    "SourcesConfig",
    "StrategyConfig",
    "SystemConfig",
    "default_config_dir",
    "load_config",
    "load_settings",
    "project_root",
]


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed or inconsistent."""


def project_root() -> Path:
    """Repository root, derived from this file's location.

    ``src/trading_system/infrastructure/settings.py`` -> four levels up.
    """
    return Path(__file__).resolve().parents[3]


def default_config_dir() -> Path:
    """Default location of the YAML configuration tree."""
    return project_root() / "config"


# ---------------------------------------------------------------------------
# Environment settings (secrets, deployment switches)
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Runtime settings sourced from the environment and ``.env``.

    Construction fails outright if ``TRADING_MODE=LIVE`` is requested without
    every live-mode guard being set. Refusing to start is the correct
    fail-safe: the alternative is a process that believes it is allowed to
    spend real money because one variable was set.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- mode and safety ---------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_confirmed: bool = False
    live_readiness_checklist_signed_off: bool = False
    allow_live_tests: bool = False

    # --- IBKR (Milestone 2) ------------------------------------------------
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_username: SecretStr | None = None
    ibkr_password: SecretStr | None = None
    ibkr_account: SecretStr | None = None

    # --- Anthropic (Milestone 4+) -----------------------------------------
    anthropic_api_key: SecretStr | None = None

    # --- Telegram (Milestone 10) ------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- campaign override -------------------------------------------------
    campaign_budget_eur: Money | None = None

    # --- persistence / logging --------------------------------------------
    database_url: str = "sqlite:///./data/trading_system.db"
    log_level: str = "INFO"
    log_format: str = "console"

    # --- paths -------------------------------------------------------------
    config_dir: Path = Field(default_factory=default_config_dir)

    @model_validator(mode="after")
    def _live_mode_requires_explicit_guards(self) -> Settings:
        if self.trading_mode is TradingMode.LIVE:
            missing = [
                name
                for name, value in (
                    ("LIVE_TRADING_CONFIRMED", self.live_trading_confirmed),
                    (
                        "LIVE_READINESS_CHECKLIST_SIGNED_OFF",
                        self.live_readiness_checklist_signed_off,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "TRADING_MODE=LIVE refused: set "
                    + ", ".join(missing)
                    + " explicitly. Live trading requires a signed-off readiness checklist "
                    "(specification section 46, Milestone 12)."
                )
        return self

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def submits_real_orders(self) -> bool:
        """Whether this mode reaches a real broker account (paper or live).

        ``DRY_RUN`` never leaves the process.
        """
        return self.trading_mode in (TradingMode.PAPER, TradingMode.LIVE)


# ---------------------------------------------------------------------------
# YAML configuration (trading policy)
# ---------------------------------------------------------------------------
class _ConfigModel(BaseModel):
    """Base for YAML-backed config. Unknown keys are an error, not a no-op."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplicationConfig(_ConfigModel):
    name: str = "trading-system"
    config_version: str
    display_timezone: str = "Europe/Madrid"
    data_dir: str = "data"
    trades_dir: str = "trades"
    reports_dir: str = "reports"


class CampaignConfig(_ConfigModel):
    """Campaign capital, independent of IBKR account buying power."""

    campaign_id: str
    currency: str = "EUR"
    budget_eur: Money = Field(ge=0)
    reserve_fraction: float = Field(ge=0.0, le=1.0)
    min_allocation_eur: Money = Field(ge=0)
    max_allocation_per_trade_eur: Money = Field(ge=0)
    max_open_positions: int = Field(ge=0)
    min_opportunity_score: float = Field(ge=0.0, le=100.0)


class LiquidityConfig(_ConfigModel):
    min_open_interest: int = Field(ge=0)
    min_daily_volume: int = Field(ge=0)
    max_bid_ask_spread_pct: float = Field(ge=0.0)


class ExitPolicyConfig(_ConfigModel):
    """Per-strategy exit policy.

    Deliberately per-strategy: the specification forbids one universal
    time-to-expiration number for every strategy (section 17B).
    """

    trailing_stop_pct: float = Field(ge=0.0, le=100.0)
    take_profit_pct: float | None = Field(default=None, ge=0.0)
    max_loss_pct: float = Field(ge=0.0, le=100.0)
    close_at_dte: int = Field(ge=0)
    allow_independent_leg_exit: bool = False


class StrategyConfig(_ConfigModel):
    """One entry from ``config/strategies/``. Absence here means untradeable."""

    name: str
    strategy_type: StrategyType
    spec_version: str
    enabled: bool = True
    description: str = ""

    applicable_hypotheses: list[MarketHypothesis] = Field(min_length=1)

    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)
    target_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    strike_offset_pct: float | None = Field(default=None, ge=0.0)

    min_option_price_eur: Money = Field(ge=0)
    max_option_price_eur: Money = Field(ge=0)
    min_implied_volatility: float | None = Field(default=None, ge=0.0)
    max_implied_volatility: float | None = Field(default=None, ge=0.0)

    liquidity: LiquidityConfig
    exit_policy: ExitPolicyConfig

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> StrategyConfig:
        if self.dte_min > self.dte_max:
            raise ValueError(f"{self.name}: dte_min must not exceed dte_max")
        if self.min_option_price_eur > self.max_option_price_eur:
            raise ValueError(f"{self.name}: min_option_price_eur exceeds max_option_price_eur")
        if (
            self.min_implied_volatility is not None
            and self.max_implied_volatility is not None
            and self.min_implied_volatility > self.max_implied_volatility
        ):
            raise ValueError(f"{self.name}: min_implied_volatility exceeds max")
        return self


class RiskConfig(_ConfigModel):
    """Deterministic risk limits (specification section 11).

    No AI agent may modify or override any value here at runtime.
    """

    config_version: str

    max_allocation_per_trade_eur: Money = Field(ge=0)
    max_open_positions: int = Field(ge=0)
    max_total_open_risk_eur: Money = Field(ge=0)
    max_underlying_concentration_pct: float = Field(ge=0.0, le=100.0)
    max_strategy_concentration_pct: float = Field(ge=0.0, le=100.0)
    max_directional_exposure_pct: float = Field(ge=0.0, le=100.0)
    max_daily_loss_eur: Money = Field(ge=0)

    min_option_price_eur: Money = Field(ge=0)
    max_option_price_eur: Money = Field(ge=0)
    max_bid_ask_spread_pct: float = Field(ge=0.0)
    min_open_interest: int = Field(ge=0)
    min_daily_volume: int = Field(ge=0)

    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)

    max_market_data_age_seconds: int = Field(ge=0)
    block_new_positions_on_reconciliation_error: bool = True

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> RiskConfig:
        if self.dte_min > self.dte_max:
            raise ValueError("risk: dte_min must not exceed dte_max")
        if self.min_option_price_eur > self.max_option_price_eur:
            raise ValueError("risk: min_option_price_eur exceeds max_option_price_eur")
        return self


class ScheduleJob(_ConfigModel):
    cron: str
    enabled: bool = True
    description: str = ""


class SchedulesConfig(_ConfigModel):
    """Job cadences. Never hard-coded inside an agent (specification section 23)."""

    config_version: str
    timezone: str = "UTC"
    jobs: dict[str, ScheduleJob]

    #: Jobs the specification requires to exist. ClassVar, not a config field.
    REQUIRED_JOBS: ClassVar[tuple[str, ...]] = (
        "data_collection",
        "universe_refresh",
        "opportunity_scan",
        "position_monitor",
        "thesis_monitor",
        "reconciliation",
        "end_of_day_report",
    )

    @model_validator(mode="after")
    def _required_jobs_present(self) -> SchedulesConfig:
        missing = [job for job in self.REQUIRED_JOBS if job not in self.jobs]
        if missing:
            raise ValueError(f"schedules.yaml missing required jobs: {', '.join(missing)}")
        return self


class SourcesConfig(_ConfigModel):
    """Research source trust policy (specification section 6)."""

    config_version: str
    tier_1: list[str] = Field(default_factory=list)
    tier_2: list[str] = Field(default_factory=list)
    tier_3: list[str] = Field(default_factory=list)
    tier_4: list[str] = Field(default_factory=list)
    require_source_attribution: bool = True
    min_sources_per_report: int = Field(default=1, ge=0)


class SystemConfig(_ConfigModel):
    """All YAML configuration, loaded and validated together."""

    application: ApplicationConfig
    campaign: CampaignConfig
    risk: RiskConfig
    schedules: SchedulesConfig
    sources: SourcesConfig
    strategies: dict[str, StrategyConfig]

    @model_validator(mode="after")
    def _strategies_respect_risk_dte_window(self) -> SystemConfig:
        """A strategy may narrow the global DTE window but never widen it."""
        for name, strategy in self.strategies.items():
            if strategy.dte_min < self.risk.dte_min or strategy.dte_max > self.risk.dte_max:
                raise ValueError(
                    f"strategy '{name}' DTE window [{strategy.dte_min}, {strategy.dte_max}] "
                    f"falls outside the risk limit [{self.risk.dte_min}, {self.risk.dte_max}]"
                )
        return self

    def enabled_strategies(self) -> dict[str, StrategyConfig]:
        return {name: s for name, s in self.strategies.items() if s.enabled}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"expected a mapping at the top level of {path}")
    return loaded


def load_config(config_dir: Path | str | None = None) -> SystemConfig:
    """Load and validate the whole YAML configuration tree.

    Raises :class:`ConfigError` if a file is missing, malformed, contains an
    unknown key, or violates a cross-file invariant.
    """
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    if not directory.is_dir():
        raise ConfigError(f"configuration directory not found: {directory}")

    strategies_dir = directory / "strategies"
    if not strategies_dir.is_dir():
        raise ConfigError(f"strategy directory not found: {strategies_dir}")

    strategies: dict[str, dict[str, Any]] = {}
    for path in sorted(strategies_dir.glob("*.yaml")):
        strategies[path.stem] = _read_yaml(path)
    if not strategies:
        raise ConfigError(f"no strategy definitions found in {strategies_dir}")

    payload = {
        "application": _read_yaml(directory / "application.yaml"),
        "campaign": _read_yaml(directory / "campaign.yaml"),
        "risk": _read_yaml(directory / "risk.yaml"),
        "schedules": _read_yaml(directory / "schedules.yaml"),
        "sources": _read_yaml(directory / "sources.yaml"),
        "strategies": strategies,
    }

    try:
        return SystemConfig.model_validate(payload)
    except Exception as exc:
        raise ConfigError(f"invalid configuration in {directory}: {exc}") from exc


def load_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings`, surfacing guard violations as :class:`ConfigError`."""
    try:
        return Settings(**overrides)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
