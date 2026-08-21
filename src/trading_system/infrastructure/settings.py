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

import os
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_system.domain.enums import (
    EXIT_POLICY_PRECEDENCE,
    AlertCode,
    AlertSeverity,
    AllocationPolicy,
    ConfidenceLevel,
    DataQualityIssue,
    ExitPolicyKind,
    ExitQuoteField,
    ExpirationSelectionPolicy,
    LegAction,
    MarketHypothesis,
    OptionDataField,
    OptionRight,
    OrderType,
    PriceSource,
    ReadinessCriterionId,
    ReconciliationFindingType,
    ReconciliationSeverity,
    SecurityType,
    SourceTier,
    StrategyType,
    StrikeSelectionPolicy,
    TimeInForce,
    TradingMode,
    UniverseSourceKind,
)
from trading_system.domain.models import Money

__all__ = [
    "AiRankingConfig",
    "AlertThresholdConfig",
    "AlertsConfig",
    "ApplicationConfig",
    "BrokerBackend",
    "CacheConfig",
    "CampaignAccountConfig",
    "CampaignAllocationConfig",
    "CampaignConfig",
    "CampaignCurrencyConfig",
    "CampaignLimitsConfig",
    "CampaignRankingConfig",
    "CollectionConfig",
    "ConfigError",
    "ContractExpirationConfig",
    "ContractLimitsConfig",
    "ContractQuoteConfig",
    "ContractSelectionConfig",
    "ContractStrikeConfig",
    "DataConfig",
    "DeduplicationConfig",
    "ExecutionConfig",
    "ExitConfig",
    "ExitDataQualityConfig",
    "ExitExpirationConfig",
    "ExitMaxLossConfig",
    "ExitOrderConfig",
    "ExitPolicyConfig",
    "ExitTakeProfitConfig",
    "ExitThesisConfig",
    "ExitTrailingConfig",
    "FreshnessConfig",
    "LiquidityConfig",
    "MarketCalendarConfig",
    "MarketContextConfig",
    "NotificationChannelConfig",
    "ObservabilityAcceptanceConfig",
    "ObservabilityConfig",
    "ObservabilityExporterConfig",
    "ObservabilityLoggingConfig",
    "ObservabilityMetricsConfig",
    "ObservabilityPrivacyConfig",
    "ObservabilitySamplingConfig",
    "OperationalHistoryConfig",
    "OptionabilityPolicy",
    "PlausibilityConfig",
    "PnLConfig",
    "PnLCurrencyConfig",
    "PnLSettlementConfig",
    "PositionExpectationConfig",
    "PositionFillConfig",
    "PositionSnapshotConfig",
    "PositionsConfig",
    "ProvidersConfig",
    "ReadinessConfig",
    "ReadinessFreshnessConfig",
    "ReadinessFreshnessWindows",
    "ReadinessLevelConfig",
    "ReadinessLevelsConfig",
    "ReadinessPaperExecutionConfig",
    "ReadinessSignoffConfig",
    "ReconciliationConfig",
    "ReferencePriceField",
    "ResearchAgentConfig",
    "ResearchConfidenceConfig",
    "ResearchConfig",
    "ResearchEvidenceConfig",
    "ResearchHorizonConfig",
    "ResearchLimitsConfig",
    "ResearchUsabilityConfig",
    "ResearchWindowConfig",
    "ReservationPolicyConfig",
    "RiskConfig",
    "ScheduleJob",
    "SchedulesConfig",
    "Settings",
    "SourcesConfig",
    "StorageConfig",
    "StrategyAgentConfig",
    "StrategyConfig",
    "StrategyEligibilityConfig",
    "StrategyExpirationConfig",
    "StrategyLegConfig",
    "StrategyLimitsConfig",
    "StrategyStageConfig",
    "SystemConfig",
    "UniverseConfig",
    "UniverseFilterConfig",
    "UniverseSourceConfig",
    "UnknownLiquidityPolicy",
    "UnusableQuotePolicy",
    "default_config_dir",
    "load_config",
    "load_settings",
    "project_root",
]


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed or inconsistent."""


class BrokerBackend(StrEnum):
    """Which broker implementation the runtime should construct."""

    SIMULATOR = "SIMULATOR"
    IBKR = "IBKR"


def project_root() -> Path:
    """Locate the directory holding ``config/`` and ``schemas/``.

    Three layouts have to work, and deriving the path from ``__file__`` alone
    only handles the first:

    * a repo checkout or editable install — four levels up from this file;
    * an installed package (the container image), where this file lives in
      ``site-packages`` and the data directories sit at the working directory;
    * an explicit ``PROJECT_ROOT``, which wins over both.

    Getting this wrong is not cosmetic: the runtime silently reports "no
    configuration" and "0 schemas" instead of failing at startup.
    """
    override = os.environ.get("PROJECT_ROOT")
    if override:
        return Path(override).resolve()

    from_source = Path(__file__).resolve().parents[3]
    if (from_source / "config").is_dir():
        return from_source

    working_directory = Path.cwd()
    if (working_directory / "config").is_dir():
        return working_directory

    return from_source


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

    # --- IBKR --------------------------------------------------------------
    #
    # Port 4002 is IB Gateway paper. The four gateway/TWS ports differ and are
    # easy to confuse, so nothing here is hard-coded elsewhere in the codebase:
    #
    #   IB Gateway paper 4002   IB Gateway live 4001
    #   TWS paper        7497   TWS live        7496
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_username: SecretStr | None = None
    ibkr_password: SecretStr | None = None
    ibkr_account: SecretStr | None = None

    #: Opens the IBKR API connection read-only, so the broker itself rejects
    #: order placement. Milestone 2 refuses to run with this disabled.
    ibkr_read_only: bool = True
    ibkr_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    #: Bound on every individual IBKR request. ``ib_async``'s synchronous
    #: wrappers default to waiting forever, and against the validated TWS
    #: environment a second uncached round trip on one connection can go
    #: unanswered indefinitely — so "forever" is a real outcome, not a
    #: theoretical one. Must stay positive; there is no unbounded setting.
    ibkr_request_timeout_seconds: float = Field(default=30.0, gt=0)
    #: IBKR market data type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
    #: Delayed is the default because it needs no paid subscription, and a
    #: quote that is honestly labelled delayed beats one that fails to arrive.
    ibkr_market_data_type: int = Field(default=3, ge=1, le=4)

    #: Which broker implementation to use. Unset resolves from the trading
    #: mode: DRY_RUN uses the simulator, PAPER and LIVE use IBKR.
    broker_backend: BrokerBackend | None = None

    # --- Anthropic (Milestone 4+) -----------------------------------------
    anthropic_api_key: SecretStr | None = None

    # --- Telegram (Milestone 10) ------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- telemetry (Milestone 11) -----------------------------------------
    #
    # Deployment switches, not policy. What telemetry *does* — sampling,
    # privacy, the cardinality guard, which metrics exist — lives in
    # config/observability.yaml, where it is reviewable in a diff. What lives
    # here is where this particular process should send it, which genuinely
    # differs between a laptop and a container and cannot be committed.
    #
    # All three are None by default, meaning "use the YAML". None of them can
    # turn a trading policy on or off; a telemetry setting has no path to a
    # risk, execution or exit value, and a test asserts it.

    #: Overrides ``observability.enabled``. Set false in a deployment that must
    #: emit nothing at all; set true in one where the collector is reachable.
    observability_enabled: bool | None = None
    #: Overrides the OTLP endpoint. In a container the collector is a service
    #: name, on a laptop it is localhost, and neither belongs in the file the
    #: policy lives in.
    otel_exporter_otlp_endpoint: str | None = None
    #: Overrides ``observability.environment``, so one committed configuration
    #: serves several deployments.
    deployment_environment: str | None = None

    def resolved_observability(self, config: ObservabilityConfig) -> ObservabilityConfig:
        """The telemetry configuration with this deployment's overrides applied.

        Returns the YAML configuration unchanged when no override is set, which
        is the ordinary case. The overrides deliberately cover only *where*
        telemetry goes and *whether* it is emitted — never sampling, never the
        privacy rules, never the cardinality guard. Those are safety-adjacent
        and belong in a file somebody reviews.
        """
        updates: dict[str, Any] = {}
        if self.observability_enabled is not None:
            updates["enabled"] = self.observability_enabled
        if self.deployment_environment:
            updates["environment"] = self.deployment_environment
        if self.otel_exporter_otlp_endpoint:
            updates["exporter"] = config.exporter.model_copy(
                update={"endpoint": self.otel_exporter_otlp_endpoint}
            )
        return config.model_copy(update=updates) if updates else config

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

    @property
    def resolved_broker_backend(self) -> BrokerBackend:
        """Which broker to construct, derived from the mode unless overridden."""
        if self.broker_backend is not None:
            return self.broker_backend
        return (
            BrokerBackend.SIMULATOR
            if self.trading_mode is TradingMode.DRY_RUN
            else BrokerBackend.IBKR
        )

    @property
    def ibkr_account_id(self) -> str | None:
        """The configured account number, unwrapped from its secret container."""
        if self.ibkr_account is None:
            return None
        value = self.ibkr_account.get_secret_value().strip()
        return value or None


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


class CampaignLimitsConfig(_ConfigModel):
    """Position and concentration limits owned by the campaign layer.

    These have no counterpart in ``risk.yaml`` — they are finer-grained than
    the global policy — so the campaign is their single authoritative source
    rather than a second copy of one.
    """

    max_positions_per_underlying: int = Field(default=1, ge=0)
    max_contracts_per_trade: int = Field(default=20, ge=0)
    max_new_positions_per_run: int = Field(default=3, ge=0)


class CampaignAccountConfig(_ConfigModel):
    """What the campaign requires to know about the account before committing."""

    require_account_snapshot: bool = True
    max_snapshot_age_seconds: int = Field(default=86_400, ge=0)
    #: Whether the daily-loss limit must be evaluable before capital is
    #: authorised. Milestone 11 built the ledger that makes it evaluable; this
    #: stays false so a deployment that has never closed a position is not
    #: blocked by a limit it has no data for. The check is then recorded as
    #: ``NOT_EVALUATED``, never as passed.
    require_daily_loss_tracking: bool = False
    #: Whether an *unknown* daily figure blocks new capital (Milestone 11).
    #: Deliberately separate from ``require_daily_loss_tracking`` and
    #: defaulting to true: "we have never tracked this" and "we tracked it,
    #: positions closed today, and the number is not trustworthy" are different
    #: facts, and only the second is evidence that something is wrong. An
    #: unknown loss is never read as no loss either way.
    block_on_unknown_daily_loss: bool = True


class CampaignCurrencyConfig(_ConfigModel):
    """Currency policy. No arbitrary rate is ever applied."""

    allow_conversion: bool = False
    #: Currencies accepted as the campaign currency without conversion. Empty
    #: by default: treating USD and EUR as interchangeable would be inventing
    #: an exchange rate, which is the same failure as inventing a price.
    treat_as_campaign_currency: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _conversion_needs_a_rate_source(self) -> CampaignCurrencyConfig:
        if self.allow_conversion:
            raise ValueError(
                "campaign.currency_policy.allow_conversion is set, but no deterministic FX "
                "rate source is configured. Converting at an arbitrary rate would invent a "
                "price; a mismatched currency is a rejection until a rate source exists."
            )
        return self


class CampaignAllocationConfig(_ConfigModel):
    """How the campaign budget is distributed. Named, never implied."""

    policy: AllocationPolicy = AllocationPolicy.PRIORITY_FIRST_FIT
    #: Stamped onto every stored allocation, so a past authorisation traces to
    #: the policy that produced it.
    policy_version: str = Field(default="1.0.0", min_length=1)
    price_source: PriceSource = PriceSource.ASK_DEBIT
    max_candidates_recorded: int = Field(default=50, ge=0)


class CampaignRankingConfig(_ConfigModel):
    """Weights for the deterministic opportunity score.

    Weights, not a model: every input is a structured upstream fact and every
    component is recorded on the stored decision, so a score can be recomputed
    by hand from the record. There is no hidden scoring anywhere.
    """

    research_confidence_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    strategy_confidence_weight: float = Field(default=0.20, ge=0.0, le=1.0)
    expected_magnitude_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    spread_quality_weight: float = Field(default=0.20, ge=0.0, le=1.0)
    data_quality_weight: float = Field(default=0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> CampaignRankingConfig:
        total = (
            self.research_confidence_weight
            + self.strategy_confidence_weight
            + self.expected_magnitude_weight
            + self.spread_quality_weight
            + self.data_quality_weight
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"campaign.ranking weights must sum to 1.0, got {total}. A score whose "
                f"weights do not sum to one is not on the 0-100 scale it claims to be."
            )
        return self


class CampaignConfig(_ConfigModel):
    """Campaign capital, independent of IBKR account buying power.

    The child of ``risk.yaml`` in the limit hierarchy: it may narrow a global
    limit and may never widen one. The cross-file check lives on
    :class:`SystemConfig`, because it is a relationship between two files and
    neither one can see the other.
    """

    campaign_id: str
    currency: str = "EUR"
    budget_eur: Money = Field(ge=0)
    reserve_fraction: float = Field(ge=0.0, le=1.0)
    min_allocation_eur: Money = Field(ge=0)
    max_allocation_per_trade_eur: Money = Field(ge=0)
    max_open_positions: int = Field(ge=0)
    min_opportunity_score: float = Field(ge=0.0, le=100.0)
    max_risk_per_trade_eur: Money = Field(default=Decimal("1500"), ge=0)

    limits: CampaignLimitsConfig = Field(default_factory=CampaignLimitsConfig)
    account: CampaignAccountConfig = Field(default_factory=CampaignAccountConfig)
    currency_policy: CampaignCurrencyConfig = Field(default_factory=CampaignCurrencyConfig)
    allocation: CampaignAllocationConfig = Field(default_factory=CampaignAllocationConfig)
    ranking: CampaignRankingConfig = Field(default_factory=CampaignRankingConfig)

    @model_validator(mode="after")
    def _internal_limits_are_ordered(self) -> CampaignConfig:
        if self.min_allocation_eur > self.max_allocation_per_trade_eur:
            raise ValueError(
                f"campaign: min_allocation_eur {self.min_allocation_eur} exceeds "
                f"max_allocation_per_trade_eur {self.max_allocation_per_trade_eur}; no trade "
                f"could ever satisfy both"
            )
        if self.min_allocation_eur > self.allocatable_budget_eur:
            raise ValueError(
                f"campaign: min_allocation_eur {self.min_allocation_eur} exceeds the "
                f"allocatable budget {self.allocatable_budget_eur} (budget {self.budget_eur} "
                f"less a {self.reserve_fraction:.0%} reserve); no trade could ever be funded"
            )
        return self

    @property
    def reserve_eur(self) -> Decimal:
        """Capital held back by policy, exact to the cent.

        Rounded *up*, so the reserve is never quietly smaller than configured.
        """
        fraction = Decimal(str(self.reserve_fraction))
        return (self.budget_eur * fraction).quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    @property
    def allocatable_budget_eur(self) -> Decimal:
        """The most the allocator may ever commit across the whole campaign."""
        return self.budget_eur - self.reserve_eur


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


class StrategyLegConfig(_ConfigModel):
    """One leg of a configured strategy (Milestone 6 brief section 30).

    The leg says *which policy* picks its strike; the numeric parameter comes
    from the leg when it states one and from the strategy otherwise, so a
    strategy that uses one delta or one offset for every leg states it once and
    cannot drift between legs.

    There is deliberately no strike, no expiry and no contract id here: this is
    a template the deterministic selector resolves against a real chain, not a
    contract.
    """

    action: LegAction = LegAction.BUY
    right: OptionRight
    ratio: int = Field(default=1, ge=1)
    strike_policy: StrikeSelectionPolicy
    target_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    strike_offset_pct: float | None = Field(default=None, ge=0.0)


class StrategyExpirationConfig(_ConfigModel):
    """How a strategy's expiration is chosen (brief section 17).

    Named rather than inferred. ``EVENT_ALIGNED`` is only honoured for a
    strategy whose configuration asks for it, and only against an event the
    research report actually carried.
    """

    rule: ExpirationSelectionPolicy = ExpirationSelectionPolicy.TARGET_DTE
    target_dte: int | None = Field(default=None, ge=0)
    #: ``EVENT_ALIGNED`` only: how far after the event an expiration may fall
    #: and still count as aligned to it.
    event_max_days_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _rule_has_what_it_needs(self) -> StrategyExpirationConfig:
        if self.rule is ExpirationSelectionPolicy.TARGET_DTE and self.target_dte is None:
            raise ValueError("expiration rule TARGET_DTE requires 'target_dte'")
        return self


class StrategyConfig(_ConfigModel):
    """One entry from ``config/strategies/``. Absence here means untradeable."""

    name: str
    strategy_type: StrategyType
    spec_version: str
    enabled: bool = True
    description: str = ""

    applicable_hypotheses: list[MarketHypothesis] = Field(min_length=1)
    #: Instrument classes this strategy may be written on. An underlying whose
    #: security type is not listed is not tradeable with it.
    allowed_underlying_types: list[SecurityType] = Field(
        default_factory=lambda: [SecurityType.STOCK], min_length=1
    )

    dte_min: int = Field(ge=0)
    dte_max: int = Field(ge=0)
    target_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    strike_offset_pct: float | None = Field(default=None, ge=0.0)

    #: The legs this strategy is made of. Checked against the structural
    #: definition in :mod:`trading_system.strategies` when the registry is
    #: built, so a configuration cannot quietly turn a straddle into a spread.
    legs: list[StrategyLegConfig] = Field(min_length=1)
    expiration_policy: StrategyExpirationConfig
    #: Fields a candidate contract must actually carry. A required field that is
    #: absent is a named rejection, never a value filled in from somewhere else.
    required_option_fields: list[OptionDataField] = Field(default_factory=list)
    #: Whether option-level liquidity must be established. Underlying liquidity
    #: is never accepted as evidence that an option is liquid.
    require_option_liquidity: bool = True

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

    @model_validator(mode="after")
    def _every_leg_can_resolve_its_policy(self) -> StrategyConfig:
        """A strike policy with no parameter would have to be guessed at."""
        for index, leg in enumerate(self.legs):
            if leg.strike_policy is StrikeSelectionPolicy.TARGET_DELTA:
                delta = self.leg_target_delta(leg)
                if delta is None:
                    raise ValueError(
                        f"{self.name}: leg {index} uses TARGET_DELTA but neither the leg nor "
                        f"the strategy states a target_delta"
                    )
                if leg.right is OptionRight.CALL and delta < 0:
                    raise ValueError(f"{self.name}: leg {index} is a CALL with a negative delta")
                if leg.right is OptionRight.PUT and delta > 0:
                    raise ValueError(f"{self.name}: leg {index} is a PUT with a positive delta")
            if (
                leg.strike_policy is StrikeSelectionPolicy.OTM_PERCENT
                and self.leg_offset_pct(leg) is None
            ):
                raise ValueError(
                    f"{self.name}: leg {index} uses OTM_PERCENT but neither the leg nor the "
                    f"strategy states a strike_offset_pct"
                )
        return self

    def leg_target_delta(self, leg: StrategyLegConfig) -> float | None:
        """The delta this leg targets: its own, or the strategy's."""
        return leg.target_delta if leg.target_delta is not None else self.target_delta

    def leg_offset_pct(self, leg: StrategyLegConfig) -> float | None:
        """The out-of-the-money offset this leg targets, as a percentage."""
        if leg.strike_offset_pct is not None:
            return leg.strike_offset_pct
        return self.strike_offset_pct


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
    """One scheduled job's cadence and its safety envelope.

    Every field beyond ``cron``/``enabled``/``description`` was added by
    Milestone 11, which is the milestone that actually runs these. They all
    default to the conservative reading, so a job description written before
    the scheduler existed keeps working and gains no new permission.
    """

    cron: str
    enabled: bool = True
    description: str = ""

    #: Only run while the exchange is open. ``False`` means the cadence alone
    #: decides — correct for reconciliation, which is most valuable exactly
    #: when nobody is watching.
    market_hours_only: bool = False
    #: Bound on one run. A job that hangs must not stop every other job, and
    #: the scheduler records an over-running job as ``UNKNOWN`` rather than
    #: ``FAILED``: the work may still be in flight.
    timeout_seconds: float = Field(default=300.0, gt=0)
    #: How late a missed firing may still run. A process that was down for an
    #: hour must not fire twelve backlogged monitoring cycles on restart; it
    #: runs the most recent one, once.
    misfire_grace_seconds: int = Field(default=300, ge=0)
    #: Whether missed firings are replayed at all. False everywhere by design:
    #: replaying a monitoring cycle for an instant that has passed would
    #: evaluate an exit against prices nobody can act on any more.
    catch_up: bool = False
    #: The second switch on the one job that can close a position. The first
    #: is ``execution.enabled``; neither implies the other, exactly as an entry
    #: needs both ``execution.enabled`` and ``--confirm``.
    authorize_exits: bool = False


class SchedulesConfig(_ConfigModel):
    """Job cadences. Never hard-coded inside an agent (specification section 23)."""

    config_version: str
    timezone: str = "UTC"
    jobs: dict[str, ScheduleJob]

    #: Master switch for the whole scheduler (Milestone 11). With this false
    #: every job is still described, still individually runnable from the CLI,
    #: and nothing fires on a cadence.
    enabled: bool = True
    #: How often the scheduler asks "what is due?". Cron expressions have
    #: one-minute resolution, so anything above 60 would silently miss firings.
    tick_seconds: int = Field(default=30, ge=1, le=60)
    #: Bound on one whole tick. Jobs are run one at a time, deliberately: two
    #: monitoring cycles overlapping would read the same ledger concurrently.
    tick_timeout_seconds: float = Field(default=900.0, gt=0)
    #: Whether a job whose completion was never recorded may be re-run.
    #: Milestone 11's jobs are all idempotent against persisted state, which is
    #: what makes this safe; the flag exists so a job that ever stops being
    #: idempotent can be excluded without editing code.
    rerun_unknown_jobs: bool = True

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

    @model_validator(mode="after")
    def _no_job_replays_a_missed_firing(self) -> SchedulesConfig:
        """``catch_up`` is refused everywhere, and that is not a stub.

        A monitoring cycle evaluates an exit against the prices visible at its
        instant. Replaying yesterday's missed firing would produce a decision
        about a market that no longer exists — and, if the job is the one
        permitted to submit, act on it. The scheduler runs the *current*
        cycle, once.
        """
        replaying = sorted(name for name, job in self.jobs.items() if job.catch_up)
        if replaying:
            raise ValueError(
                f"schedules.yaml sets catch_up on {', '.join(replaying)}. A missed firing is "
                f"not replayed: a monitoring cycle judged against prices that have already "
                f"moved would act on a market that no longer exists."
            )
        return self


class ObservabilityExporterConfig(_ConfigModel):
    """Where telemetry goes, and how hard it tries to get there.

    Every value here is about the *side channel*. None of it may change a
    trading decision, and a failure to reach any of it is not a trading
    failure — which is why the timeouts are short and the queue is bounded:
    telemetry that backed up would be telemetry slowing down a monitoring
    cycle, and an operational nicety must never do that.
    """

    #: OTLP endpoint of the collector. The application talks to a collector and
    #: never to Tempo, Prometheus, Loki or Grafana directly.
    endpoint: str = "http://localhost:4318"
    protocol: str = "http/protobuf"
    #: Bound on one export attempt. Deliberately short.
    timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)
    #: How often batched spans and metrics are flushed.
    export_interval_seconds: float = Field(default=15.0, gt=0)
    max_queue_size: int = Field(default=2048, ge=1)
    max_export_batch_size: int = Field(default=512, ge=1)
    #: Never true. A blocking exporter would let a dead collector stall a
    #: trading operation, which is the one thing telemetry must not do.
    block_on_full_queue: bool = False
    #: Print spans and metrics to stderr instead of exporting. For local
    #: development and for tests that want to see what would be emitted.
    console: bool = False

    @model_validator(mode="after")
    def _the_exporter_never_blocks_a_trading_operation(self) -> ObservabilityExporterConfig:
        if self.block_on_full_queue:
            raise ValueError(
                "observability.exporter.block_on_full_queue is set. A telemetry queue that "
                "applies back-pressure would let an unreachable collector stall a monitoring "
                "cycle or an exit submission. Telemetry is a side channel; it drops."
            )
        if self.protocol not in ("http/protobuf", "grpc"):
            raise ValueError(
                f"observability.exporter.protocol {self.protocol!r} is not one of "
                f"'http/protobuf' or 'grpc'"
            )
        return self


class ObservabilitySamplingConfig(_ConfigModel):
    """How much is recorded. Sampling changes what is *seen*, never what is done."""

    ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Errors are always recorded whatever the ratio says. A sampled-away
    #: failure is the one trace an operator actually needed.
    always_sample_errors: bool = True


class ObservabilityMetricsConfig(_ConfigModel):
    """Metric emission policy, including the cardinality guard."""

    enabled: bool = True
    export_interval_seconds: float = Field(default=15.0, gt=0)
    #: Attribute names that must never become metric labels. A domain id in a
    #: label is an unbounded time series per trade, and the guard is enforced
    #: in code as well as stated here — ``observability/metrics.py`` refuses
    #: them, and a test asserts the refusal.
    forbidden_labels: list[str] = Field(
        default_factory=lambda: [
            "trace_id",
            "span_id",
            "campaign_id",
            "universe_selection_id",
            "research_run_id",
            "strategy_decision_id",
            "contract_selection_id",
            "risk_evaluation_id",
            "allocation_id",
            "execution_id",
            "position_id",
            "exit_id",
            "broker_order_id",
            "account",
            "account_id",
            "symbol",
        ]
    )

    @model_validator(mode="after")
    def _the_guard_is_not_empty(self) -> ObservabilityMetricsConfig:
        if self.enabled and not self.forbidden_labels:
            raise ValueError(
                "observability.metrics.forbidden_labels is empty, which switches off the "
                "cardinality guard entirely. One time series per execution id is how a "
                "metrics backend falls over."
            )
        return self


class ObservabilityLoggingConfig(_ConfigModel):
    """How logs and traces are correlated."""

    #: Inject ``trace_id``/``span_id`` into every structured log emitted inside
    #: a span. This is what makes trace-to-log navigation work in Grafana, and
    #: it costs nothing when telemetry is off — there is no span, so there is
    #: nothing to inject.
    correlate_traces: bool = True
    #: Bind ``service`` and ``trading_mode`` onto every record.
    include_service_context: bool = True
    #: Also export structured logs over OTLP, so they reach the collector's
    #: ``logs`` pipeline and from there a log backend.
    #:
    #: Added in Milestone 12. Milestone 11 shipped ``deploy/otel/collector.yaml``
    #: with a logs pipeline wired to Loki and no application-side log exporter
    #: at all, so the pipeline was correct and permanently unfed — a gap the M12
    #: acceptance gate found by asking Loki whether a line had actually arrived.
    #:
    #: Ships **off**, like every other telemetry switch: logs go to stdout
    #: regardless, carrying their trace ids, and a machine with no collector
    #: should not spend a millisecond trying to reach one.
    export_otlp: bool = False


class ObservabilityPrivacyConfig(_ConfigModel):
    """What telemetry may never carry.

    Telemetry is not an audit archive. The audit archive is the immutable
    domain artifact; a span carries the *id* of one, never its contents.
    """

    #: Refuse any attribute whose name matches one of these, at any depth.
    #: Enforced in :mod:`trading_system.observability.privacy`, with tests.
    forbidden_attribute_substrings: list[str] = Field(
        default_factory=lambda: [
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "credential",
            "prompt",
            "completion",
            "response_text",
            "portfolio",
            "balance",
            "account_number",
        ]
    )
    #: Never emit a full account number. The masked form is what reaches a span.
    mask_account_identifiers: bool = True
    #: Truncate any string attribute at this length. A long value in a span is
    #: a payload trying to become an archive.
    max_attribute_length: int = Field(default=256, ge=16, le=4096)
    #: Emit monetary values as attributes at all. Off by default: counts,
    #: durations, statuses and references answer operational questions, and a
    #: money figure in a trace is financial truth in the wrong place.
    allow_monetary_attributes: bool = False

    @model_validator(mode="after")
    def _the_account_is_always_masked(self) -> ObservabilityPrivacyConfig:
        if not self.mask_account_identifiers:
            raise ValueError(
                "observability.privacy.mask_account_identifiers=false would put a real IBKR "
                "account number into a telemetry backend. There is no configuration that "
                "permits it."
            )
        if not self.forbidden_attribute_substrings:
            raise ValueError(
                "observability.privacy.forbidden_attribute_substrings is empty, which removes "
                "the redaction guard entirely."
            )
        return self


class ObservabilityConfig(_ConfigModel):
    """Telemetry configuration (``config/observability.yaml``, Milestone 11).

    Ships **enabled: false**. Telemetry is an operational side channel: the
    system must run identically without it, and the honest default for a
    machine with no collector on it is off. Turning it on changes what is
    *observed* and nothing that is *decided* — a claim
    ``tests/observability/`` asserts by running the same operations with
    telemetry on and off and comparing the artifacts.
    """

    config_version: str = "2026.08.14-1"
    enabled: bool = False
    service_name: str = "trading-system"
    #: Left unset to inherit the application version, so a deploy cannot
    #: report telemetry under a version it is not running.
    service_version: str | None = None
    environment: str = "local"

    exporter: ObservabilityExporterConfig = Field(default_factory=ObservabilityExporterConfig)
    sampling: ObservabilitySamplingConfig = Field(default_factory=ObservabilitySamplingConfig)
    metrics: ObservabilityMetricsConfig = Field(default_factory=ObservabilityMetricsConfig)
    logging: ObservabilityLoggingConfig = Field(default_factory=ObservabilityLoggingConfig)
    privacy: ObservabilityPrivacyConfig = Field(default_factory=ObservabilityPrivacyConfig)

    #: Whether a telemetry failure may ever propagate into the caller. False,
    #: always: the whole point of this package is that an unreachable collector
    #: cannot change what the trading system does.
    fail_open: bool = True

    @model_validator(mode="after")
    def _telemetry_never_fails_a_trading_operation(self) -> ObservabilityConfig:
        if not self.fail_open:
            raise ValueError(
                "observability.fail_open=false would let an exporter error propagate into a "
                "trading operation. A collector that is down must never be able to reject a "
                "trade, approve one, or stop an exit from being evaluated."
            )
        return self


class PnLCurrencyConfig(_ConfigModel):
    """Currency policy for realised profit and loss. No rate is ever invented."""

    #: Refused, exactly as ``campaign.currency_policy.allow_conversion`` is.
    #: A profit and loss figure converted at a guessed rate is a made-up
    #: number in the one place the system must be exact.
    allow_conversion: bool = False
    #: Decimal places money is quantised to when a proration is unavoidable.
    precision: int = Field(default=2, ge=0, le=8)

    @model_validator(mode="after")
    def _no_invented_exchange_rate(self) -> PnLCurrencyConfig:
        if self.allow_conversion:
            raise ValueError(
                "pnl.currency.allow_conversion is set, but no deterministic FX rate source "
                "exists. A cross-currency result is NOT_AVAILABLE; it is never a guess."
            )
        return self


class PnLSettlementConfig(_ConfigModel):
    """When committed capital may return to the campaign (Milestone 11).

    The whole file exists for the three switches that fail to load. Each one
    would, if permitted, let capital come back on something weaker than
    broker-confirmed closure — which is the single failure this milestone is
    shaped to prevent.
    """

    enabled: bool = True
    #: Only broker-confirmed closure settles. Not a submitted exit, not a
    #: reported fill, not a decision to exit.
    require_broker_confirmed_closure: bool = True
    #: An execution whose outcome was never learned never releases capital.
    release_on_unknown: bool = False
    #: A critical reconciliation finding touching the position stops it.
    require_clean_reconciliation: bool = True
    #: Whether realised profit is added to the campaign's spendable envelope.
    #: Off: the campaign budget is a policy figure from ``campaign.yaml``, and
    #: a system that quietly grew its own budget on a winning trade would be
    #: compounding without anybody deciding to.
    return_realized_pnl_to_campaign: bool = False

    @model_validator(mode="after")
    def _capital_returns_on_evidence_only(self) -> PnLSettlementConfig:
        if self.release_on_unknown:
            raise ValueError(
                "pnl.settlement.release_on_unknown is set. An execution whose outcome was "
                "never learned may be a live order right now; returning its capital is how a "
                "campaign funds the same trade twice. There is no configuration that permits "
                "it, exactly as there is none in reconciliation.yaml."
            )
        if not self.require_broker_confirmed_closure:
            raise ValueError(
                "pnl.settlement.require_broker_confirmed_closure=false would let a submitted "
                "exit return capital. Only broker reality closes a position (Milestone 10)."
            )
        return self


class PnLConfig(_ConfigModel):
    """Realised profit and loss policy (``config/pnl.yaml``, Milestone 11).

    Realised profit and loss is computed from **broker-confirmed fills** and
    from nothing else: not a limit price, not a reference price, not a
    midpoint, not an estimate of what an exit ought to have made.
    """

    config_version: str = "2026.08.14-1"
    enabled: bool = True

    #: Exchange-local timezone the trading *day* is bounded by. A closure at
    #: 21:30 UTC belongs to the New York session that has just ended, and
    #: bounding the day in UTC would file it under tomorrow.
    day_boundary_timezone: str = "America/New_York"

    #: Whether a missing commission makes the *net* figure unavailable. True:
    #: IBKR reports fills before commission reports, and treating an absent
    #: commission as zero understates the cost of every trade the feed was
    #: slow about. The gross figure is still reported.
    require_commission_for_net: bool = True
    #: Never assume a contract multiplier. A fill with no multiplier yields no
    #: money figure at all.
    assume_multiplier: bool = False

    #: Whether unrealised profit and loss may be mixed into the daily figure.
    #: False, and there is no path that mixes them silently: a daily loss limit
    #: evaluated against marked-to-market paper losses is a different limit
    #: from the one the risk policy describes.
    include_unrealized_in_daily: bool = False

    currency: PnLCurrencyConfig = Field(default_factory=PnLCurrencyConfig)
    settlement: PnLSettlementConfig = Field(default_factory=PnLSettlementConfig)

    @model_validator(mode="after")
    def _nothing_is_assumed(self) -> PnLConfig:
        if self.assume_multiplier:
            raise ValueError(
                "pnl.assume_multiplier is set. A standard US equity option is 100 and a "
                "system that assumed so would misprice the first one that is not — silently, "
                "in the figure the daily loss limit is evaluated against."
            )
        if self.include_unrealized_in_daily:
            raise ValueError(
                "pnl.include_unrealized_in_daily is set. Realised and unrealised results are "
                "different claims; the daily loss limit is stated over realised results, and "
                "mixing them would enforce a limit nobody wrote."
            )
        return self


class AlertThresholdConfig(_ConfigModel):
    """One alert rule's threshold and severity."""

    enabled: bool = True
    severity: AlertSeverity = AlertSeverity.WARNING
    #: Count of occurrences within the window that fires the alert.
    threshold: int = Field(default=1, ge=1)
    #: The window the count is taken over.
    window_minutes: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _a_disabled_rule_is_not_a_silent_one(self) -> AlertThresholdConfig:
        if not self.enabled and self.severity is AlertSeverity.CRITICAL:
            raise ValueError(
                "a CRITICAL alert rule cannot be disabled in configuration. Turning off the "
                "notification does not turn off the condition, and the safety alerts are the "
                "ones an operator most needs to have not been quietly muted."
            )
        return self


class NotificationChannelConfig(_ConfigModel):
    """One outbound channel. Vendor-specific behaviour lives behind an adapter."""

    #: ``console``, ``file`` or ``webhook``. Nothing here names a vendor; a
    #: Telegram or e-mail channel is a ``webhook`` adapter's concern.
    kind: str = "console"
    enabled: bool = True
    #: For ``file``: a path relative to the data root. For ``webhook``: the
    #: environment variable holding the URL — never the URL itself, which
    #: frequently carries a token.
    target: str | None = None
    minimum_severity: AlertSeverity = AlertSeverity.WARNING
    timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)

    @model_validator(mode="after")
    def _no_secret_in_configuration(self) -> NotificationChannelConfig:
        if self.kind not in ("console", "file", "webhook"):
            raise ValueError(
                f"alerts.channels kind {self.kind!r} is not one of 'console', 'file', "
                f"'webhook'. Vendor-specific delivery belongs behind a webhook adapter."
            )
        if self.kind == "webhook":
            if not self.target:
                raise ValueError(
                    "a webhook channel must name the environment variable holding its URL"
                )
            if "://" in self.target:
                raise ValueError(
                    "alerts.channels target for a webhook must be the NAME of an environment "
                    "variable, not a URL. A webhook URL usually embeds a token, and this file "
                    "is committed."
                )
        return self


class AlertsConfig(_ConfigModel):
    """Operational alerting (``config/alerts.yaml``, Milestone 11).

    An alert is a notification and nothing else. Nothing in this file, and
    nothing in :mod:`trading_system.operations.alerts`, can place, cancel or
    modify an order — a test walks the import graph and asserts it. Safety is
    enforced by the domain; alerting is how a person finds out.
    """

    config_version: str = "2026.08.14-1"
    enabled: bool = True
    #: Alerts are stored whether or not any channel accepts them. A
    #: notification that failed to send is not an alert that did not happen.
    persist: bool = True

    rules: dict[str, AlertThresholdConfig] = Field(default_factory=dict)
    channels: list[NotificationChannelConfig] = Field(default_factory=list)

    #: Rules the milestone requires to exist, so a deployment cannot quietly
    #: ship without the safety alerts. ClassVar, not a config field.
    REQUIRED_RULES: ClassVar[tuple[str, ...]] = (
        "BROKER_CONNECTION_ERRORS",
        "BROKER_TIMEOUTS",
        "EXECUTION_UNKNOWN",
        "EXECUTION_REJECTION_RATE",
        "LIVE_EXECUTION_ATTEMPT",
        "RECONCILIATION_MISMATCH",
        "SCHEDULER_JOB_FAILED",
        "EXIT_UNKNOWN",
        "EXPIRATION_APPROACHING",
        "DAILY_LOSS_THRESHOLD_EXCEEDED",
        "DAILY_LOSS_UNAVAILABLE",
    )

    @model_validator(mode="after")
    def _every_rule_names_a_known_alert(self) -> AlertsConfig:
        known = {code.value for code in AlertCode}
        unknown = sorted(set(self.rules) - known)
        if unknown:
            raise ValueError(
                f"alerts.yaml defines rules for unknown alert codes: {', '.join(unknown)}. "
                f"The vocabulary is closed; add a member to AlertCode first."
            )
        missing = [rule for rule in self.REQUIRED_RULES if rule not in self.rules]
        if missing:
            raise ValueError(f"alerts.yaml is missing required rules: {', '.join(missing)}")
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


# ---------------------------------------------------------------------------
# Data layer policy (Milestone 3)
# ---------------------------------------------------------------------------
class OptionQuoteCollectionConfig(_ConfigModel):
    """Which option contracts a collection run asks the broker to quote.

    This is *collection breadth*, deliberately not decision policy. It says how
    wide a net to cast; ``contract_selection.yaml`` then picks within what was
    caught, and reports ``REQUIRED_DATA_UNAVAILABLE`` honestly if the net was
    too narrow. Stating a target DTE here as well would declare the same policy
    twice, and the two copies would drift.

    The defaults are wider than the selection policy on purpose, so a
    ``target_dte`` of 21 and a 5% strike band both sit comfortably inside.
    """

    #: Expirations to consider, as a day range from the collection instant.
    min_dte: int = Field(default=7, ge=0)
    max_dte: int = Field(default=45, ge=0)
    #: Strikes to quote, as a percentage band around the underlying reference
    #: price. A band rather than a count, because a fixed count spans a very
    #: different amount of money on a $9 stock than on a $760 index.
    strike_window_pct: float = Field(default=8.0, gt=0.0, le=100.0)
    #: Hard ceiling on contracts requested per underlying per collection.
    #: SPY alone lists 491 strikes; a request that quietly expanded to a
    #: thousand subscriptions is how a market-data line gets throttled. When it
    #: binds, the strikes nearest the money are kept and the fact is recorded.
    max_contracts: int = Field(default=120, ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> OptionQuoteCollectionConfig:
        if self.min_dte > self.max_dte:
            raise ValueError(
                f"option_quotes.min_dte ({self.min_dte}) exceeds max_dte ({self.max_dte}); "
                f"no expiration could ever satisfy both"
            )
        return self


class CollectionConfig(_ConfigModel):
    """What the collectors fetch, until universe selection exists."""

    symbols: list[str] = Field(default_factory=list)
    option_chain_symbols: list[str] = Field(default_factory=list)
    max_records_per_snapshot: int = Field(default=20000, ge=1)
    option_quotes: OptionQuoteCollectionConfig = Field(default_factory=OptionQuoteCollectionConfig)


class FreshnessConfig(_ConfigModel):
    """Per-data-type and per-origin staleness windows.

    One global threshold cannot be right for both a realtime quote and a
    quarterly filing, so there deliberately is not one.
    """

    default_seconds: int = Field(default=3600, ge=0)
    by_data_type: dict[str, int] = Field(default_factory=dict)
    by_origin: dict[str, int] = Field(default_factory=dict)

    def window_seconds(self, data_type: str, origin: str | None = None) -> int:
        """The applicable window: the tightest configured bound that applies.

        Taking the minimum means adding an origin rule can only ever make the
        system more suspicious of a record, never less.
        """
        candidates = [self.by_data_type.get(data_type, self.default_seconds)]
        if origin is not None and origin in self.by_origin:
            candidates.append(self.by_origin[origin])
        return min(candidates)


class PlausibilityConfig(_ConfigModel):
    """Bounds that separate "implausible" from "merely unattractive".

    These catch a broken feed. Tradeability limits live in
    :class:`RiskConfig`; conflating the two would let a data check quietly
    become a trading rule.
    """

    min_price: Money = Field(ge=0)
    max_price: Money = Field(gt=0)
    max_spread_pct: float = Field(ge=0.0)

    max_equity_daily_volume: int = Field(ge=0)
    max_option_contract_volume: int = Field(ge=0)
    max_open_interest: int = Field(ge=0)

    min_implied_volatility: float = Field(ge=0.0)
    max_implied_volatility: float = Field(gt=0.0)
    max_abs_delta: float = Field(gt=0.0)

    max_strike: Money = Field(gt=0)
    allowed_multipliers: list[int] = Field(default_factory=list)
    max_expiration_horizon_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> PlausibilityConfig:
        if self.min_price > self.max_price:
            raise ValueError("data plausibility: min_price exceeds max_price")
        if self.min_implied_volatility > self.max_implied_volatility:
            raise ValueError("data plausibility: min_implied_volatility exceeds max")
        return self


class ResearchUsabilityConfig(_ConfigModel):
    """Which quality dimensions a record must satisfy to be research-usable.

    Separate from technical validity on purpose (Milestone 3 brief section 50):
    a well-formed record carrying an impossible number is valid and unusable at
    the same time.
    """

    require_transport: bool = True
    require_schema: bool = True
    require_source: bool = True
    require_timestamp: bool = True
    require_completeness: bool = True
    require_plausibility: bool = True
    require_consistency: bool = True
    #: False by default: staleness is contextual and the consumer decides.
    require_freshness: bool = False

    #: Plausibility findings an operator has deliberately decided to tolerate.
    #:
    #: All-or-nothing, and empty by default. A record whose plausibility
    #: dimension failed stays research-usable only when *every* plausibility
    #: finding it carries is listed here; one untolerated finding fails the
    #: record even when a tolerated one sits beside it. Tolerating a defect is
    #: an operator decision recorded in configuration, never a default.
    #:
    #: This is deliberately an allow-list rather than ``require_plausibility:
    #: false``, which would switch off every plausibility check at once --
    #: negative and zero prices, prices out of bounds, implausible implied
    #: volatility, deltas outside +/-1, strike bounds and expiration horizon.
    #: The precedent is ``observability/privacy.py``'s ``ALLOWED_EXACT_NAMES``:
    #: a blunt guard needs an explicit exception list, not a loosened pattern.
    #:
    #: Nothing here rescales, repairs or hides anything. The raw value, the
    #: finding and ``plausibility_valid=False`` are all unchanged; only the
    #: derived ``research_usable`` moves.
    tolerated_plausibility_issues: list[DataQualityIssue] = Field(default_factory=list)


class MarketCalendarConfig(_ConfigModel):
    """Trading calendar. Weekends alone are not enough (section 35)."""

    exchange: str = "XNYS"
    timezone: str = "America/New_York"
    regular_open: str = "09:30"
    regular_close: str = "16:00"
    early_close_time: str = "13:00"
    #: Years for which holidays have actually been verified. Outside these the
    #: calendar answers "unknown" instead of assuming a weekday is open.
    covered_years: list[int] = Field(default_factory=list)
    holidays: list[date] = Field(default_factory=list)
    early_closes: list[date] = Field(default_factory=list)


class StorageConfig(_ConfigModel):
    root: str = "data"


class CacheConfig(_ConfigModel):
    """Cache policy. Never authoritative, always regenerable."""

    enabled: bool = True
    default_ttl_seconds: int = Field(default=300, ge=0)


class ProvidersConfig(_ConfigModel):
    """Provider-level knobs. No paid provider is configured or required."""

    http_timeout_seconds: float = Field(default=15.0, gt=0)
    sec_user_agent: str = ""
    sec_base_url: str = "https://data.sec.gov"


class DataConfig(_ConfigModel):
    """All data-layer policy (``config/data.yaml``)."""

    config_version: str
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    plausibility: PlausibilityConfig
    research_usability: ResearchUsabilityConfig = Field(default_factory=ResearchUsabilityConfig)
    market_calendar: MarketCalendarConfig = Field(default_factory=MarketCalendarConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


# ---------------------------------------------------------------------------
# Universe selection policy (Milestone 4)
# ---------------------------------------------------------------------------
class OptionabilityPolicy(StrEnum):
    """What to do with an underlying whose optionability is not established.

    ``UNKNOWN`` is never rewritten into a definite answer; this only decides
    whether an unresolved candidate may proceed.
    """

    #: Only underlyings established to have listed options. UNKNOWN is rejected.
    REQUIRED = "REQUIRED"
    #: UNKNOWN passes, flagged, and the agent sees that it is unresolved.
    UNKNOWN_ALLOWED = "UNKNOWN_ALLOWED"
    #: Optionability is not considered at all.
    IGNORED = "IGNORED"


class UniverseSourceConfig(_ConfigModel):
    """Where the candidate pool comes from, explicitly and with a version.

    Versioned because "which assets were considered at time T" is part of
    reconstructing a decision: a symbol list that changed silently makes a past
    universe unreproducible.
    """

    kind: UniverseSourceKind
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    symbols: list[str] = Field(default_factory=list)
    #: Path to a newline-delimited symbol file, for ``kind: FILE``.
    location: str | None = None

    @model_validator(mode="after")
    def _kind_has_what_it_needs(self) -> UniverseSourceConfig:
        if self.kind is UniverseSourceKind.FILE and not (self.location or "").strip():
            raise ValueError("universe source kind FILE requires 'location'")
        if self.kind in (UniverseSourceKind.STATIC, UniverseSourceKind.CUSTOM) and not self.symbols:
            raise ValueError(f"universe source kind {self.kind.value} requires 'symbols'")
        return self


class UniverseFilterConfig(_ConfigModel):
    """Deterministic eligibility rules.

    These decide who reaches the agent. The agent runs afterwards and cannot
    restore anything excluded here — see
    :mod:`trading_system.universe.filters`.
    """

    allowed_security_types: list[SecurityType] = Field(min_length=1)
    allowed_currencies: list[str] = Field(min_length=1)
    #: Empty means any exchange.
    allowed_exchanges: list[str] = Field(default_factory=list)

    min_price: Money = Field(ge=0)
    #: Underlying share volume, never option volume (specification section 11
    #: of the Milestone 4 brief): stock liquidity does not establish that an
    #: option is liquid.
    min_average_daily_volume: int = Field(ge=0)
    max_data_age_seconds: int = Field(ge=0)
    require_research_usable: bool = True
    optionability_policy: OptionabilityPolicy = OptionabilityPolicy.REQUIRED
    exclusions: list[str] = Field(default_factory=list)

    max_candidates: int = Field(ge=0)
    max_selected_assets: int = Field(ge=0)

    @model_validator(mode="after")
    def _limits_are_ordered(self) -> UniverseFilterConfig:
        if self.max_selected_assets > self.max_candidates:
            raise ValueError(
                f"universe: max_selected_assets ({self.max_selected_assets}) exceeds "
                f"max_candidates ({self.max_candidates}); the agent cannot select more "
                f"assets than it is shown"
            )
        return self

    def excludes(self, symbol: str) -> bool:
        return symbol.upper() in {s.upper() for s in self.exclusions}


class AiRankingConfig(_ConfigModel):
    """How the ranking agent is invoked, and what happens when it cannot be.

    No credential appears here. ``ANTHROPIC_API_KEY`` comes from the
    environment, like every other secret.
    """

    enabled: bool = True
    model_provider: str = Field(default="ANTHROPIC", alias="provider", min_length=1)
    model_name: str = Field(default="claude-opus-5", alias="model", min_length=1)
    #: Bumped whenever the prompt changes in a way that could alter a ranking.
    #: Stamped into every stored run so a universe traces to its instructions.
    prompt_version: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_output_tokens: int = Field(default=8000, ge=1)
    effort: str = "medium"

    #: Fail closed by default. A deterministic ordering substituted for a failed
    #: model call would be indistinguishable from the model's judgement in the
    #: stored record, so it must be an explicit, recorded choice.
    allow_deterministic_fallback: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _effort_is_known(self) -> AiRankingConfig:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if self.effort not in allowed:
            raise ValueError(
                f"universe ai_ranking.effort {self.effort!r} is not one of {sorted(allowed)}"
            )
        return self


class UniverseConfig(_ConfigModel):
    """All universe-selection policy (``config/universe.yaml``)."""

    config_version: str
    source: UniverseSourceConfig
    filters: UniverseFilterConfig
    ai_ranking: AiRankingConfig


# ---------------------------------------------------------------------------
# Research policy (Milestone 5)
# ---------------------------------------------------------------------------
class ResearchHorizonConfig(_ConfigModel):
    """The window the research question is asked about.

    Configurable rather than fixed at 14-31 days because the horizon is the
    question: an outlook over three weeks and one over three years are
    different claims about the same asset, and the agent must be told which it
    is being asked for.
    """

    min_days: int = Field(ge=1, le=365)
    max_days: int = Field(ge=1, le=365)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> ResearchHorizonConfig:
        if self.min_days > self.max_days:
            raise ValueError("research horizon: min_days must not exceed max_days")
        return self

    def contains(self, days: int) -> bool:
        return self.min_days <= days <= self.max_days


class ResearchWindowConfig(_ConfigModel):
    """How much history and calendar the agent is shown.

    Bounded deliberately: unlimited history is neither affordable nor useful,
    and the stored input records the windows that were applied so a past report
    explains what it could and could not see.
    """

    news_lookback_days: int = Field(default=14, ge=0)
    event_lookahead_days: int = Field(default=45, ge=0)
    event_lookback_days: int = Field(default=14, ge=0)
    historical_lookback_days: int = Field(default=90, ge=0)
    fundamentals_lookback_days: int = Field(default=400, ge=0)
    regulatory_lookback_days: int = Field(default=120, ge=0)

    min_observations_for_volatility: int = Field(default=20, ge=2)
    volatility_annualization_days: int = Field(default=252, ge=1)


class ResearchLimitsConfig(_ConfigModel):
    """Cost ceilings. Every one truncates deterministically and visibly."""

    max_assets_per_run: int = Field(default=10, ge=0)
    max_evidence_items: int = Field(default=40, ge=1)
    max_news_items: int = Field(default=25, ge=0)
    max_events: int = Field(default=15, ge=0)
    max_regulatory_items: int = Field(default=10, ge=0)
    max_fundamental_periods: int = Field(default=4, ge=0)
    max_input_characters: int = Field(default=120_000, ge=1000)


class DeduplicationConfig(_ConfigModel):
    """News grouping policy (Milestone 5 brief section 31).

    Deliberately shallow. The objective is that one event syndicated ten times
    is not ten independent catalysts — not a semantic clustering engine.
    """

    enabled: bool = True
    headline_similarity: float = Field(default=0.8, ge=0.0, le=1.0)
    publication_window_hours: int = Field(default=48, ge=0)
    stopwords: list[str] = Field(default_factory=list)


class ResearchConfidenceConfig(_ConfigModel):
    """When the evidence licenses a stated confidence band.

    Confidence is not purely the model's decision (brief section 56). These are
    the deterministic constraints; a report that violates one is rejected, not
    downgraded — silently rewriting the band would store a judgement the agent
    never made.
    """

    require_research_usable_for_high: bool = True
    min_evidence_items_for_high: int = Field(default=3, ge=0)
    min_source_tier_for_high: SourceTier = SourceTier.TIER_2
    require_contradiction_resolution_for_high: bool = True
    min_evidence_items_for_medium: int = Field(default=1, ge=0)
    max_data_gaps_for_high: int = Field(default=3, ge=0)


class ResearchEvidenceConfig(_ConfigModel):
    """What a report must rest on before it is worth producing."""

    min_evidence_items: int = Field(default=1, ge=0)
    require_research_usable_evidence: bool = True
    min_distinct_sources: int = Field(default=1, ge=0)


class MarketContextConfig(_ConfigModel):
    """Broad-market references the agent may be shown, when the store has them.

    Read point-in-time like any other symbol. Absence is reported as
    ``MARKET_CONTEXT_UNAVAILABLE``; the research layer never fetches it itself
    and never substitutes a remembered value (brief section 21).
    """

    broad_index_symbol: str | None = None
    reference_symbols: list[str] = Field(default_factory=list)


class ResearchAgentConfig(_ConfigModel):
    """How the research agent is invoked, and what happens when it cannot be.

    There is deliberately no ``allow_deterministic_fallback`` here, unlike
    :class:`AiRankingConfig`. A universe can be ordered without a model; a
    market outlook cannot be formed without one, and anything this layer
    synthesised in place of an unreachable model would be a fabricated view.

    No credential appears here. ``ANTHROPIC_API_KEY`` comes from the
    environment, like every other secret.
    """

    enabled: bool = True
    model_provider: str = Field(default="ANTHROPIC", alias="provider", min_length=1)
    model_name: str = Field(default="claude-opus-5", alias="model", min_length=1)
    prompt_version: str = Field(min_length=1)
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_output_tokens: int = Field(default=12000, ge=1)
    effort: str = "high"

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _effort_is_known(self) -> ResearchAgentConfig:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if self.effort not in allowed:
            raise ValueError(
                f"research agent.effort {self.effort!r} is not one of {sorted(allowed)}"
            )
        return self


class ResearchConfig(_ConfigModel):
    """All market-research policy (``config/research.yaml``).

    Source trust is deliberately absent: it lives in ``config/sources.yaml``
    and is read from there, so there is exactly one source-ranking system.
    """

    config_version: str
    horizon: ResearchHorizonConfig
    window: ResearchWindowConfig = Field(default_factory=ResearchWindowConfig)
    limits: ResearchLimitsConfig = Field(default_factory=ResearchLimitsConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    confidence: ResearchConfidenceConfig = Field(default_factory=ResearchConfidenceConfig)
    evidence: ResearchEvidenceConfig = Field(default_factory=ResearchEvidenceConfig)
    market_context: MarketContextConfig = Field(default_factory=MarketContextConfig)
    agent: ResearchAgentConfig


# ---------------------------------------------------------------------------
# Strategy selection policy (Milestone 6)
# ---------------------------------------------------------------------------
class StrategyEligibilityConfig(_ConfigModel):
    """Deterministic gates applied before the strategy agent is consulted.

    Each one produces ``NO_TRADE`` without a model call. Spending a request to
    be told "not enough to go on" is waste, and an agent handed an inadequate
    outlook tends to fill the gap rather than decline.
    """

    #: Research confidence below this ends the symbol as NO_TRADE. ``LOW``
    #: admits everything and leaves the judgement to the agent, which is the
    #: shipped default: a low-confidence outlook can still justify NO_TRADE, and
    #: refusing it here would hide that reasoning.
    min_confidence: ConfidenceLevel = ConfidenceLevel.LOW
    #: The research report's own data must have been research-usable.
    require_research_usable: bool = True
    #: An underlying with no visible option chain cannot become an option trade.
    require_option_chain: bool = True
    min_evidence_items: int = Field(default=1, ge=0)
    #: The research horizon must overlap the strategy's DTE window; an outlook
    #: over three weeks is not expressible by a contract that expires in two.
    require_horizon_overlap: bool = True


class StrategyLimitsConfig(_ConfigModel):
    """Cost ceilings for the strategy stage. One model call per underlying."""

    max_symbols_per_run: int = Field(default=10, ge=0)
    max_input_characters: int = Field(default=60_000, ge=1000)


class StrategyAgentConfig(_ConfigModel):
    """How the strategy agent is invoked, and what happens when it cannot be.

    There is deliberately no ``allow_deterministic_fallback``, for the same
    reason there is none for research: a strategy chosen by this layer in place
    of an unreachable model would be recorded as the model's judgement. An
    unavailable model ends the symbol as ``AI_UNAVAILABLE`` with no strategy.

    No credential appears here. ``ANTHROPIC_API_KEY`` comes from the
    environment, like every other secret.
    """

    enabled: bool = True
    model_provider: str = Field(default="ANTHROPIC", alias="provider", min_length=1)
    model_name: str = Field(default="claude-opus-5", alias="model", min_length=1)
    prompt_version: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_output_tokens: int = Field(default=6000, ge=1)
    effort: str = "medium"

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _effort_is_known(self) -> StrategyAgentConfig:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if self.effort not in allowed:
            raise ValueError(
                f"strategy agent.effort {self.effort!r} is not one of {sorted(allowed)}"
            )
        return self


class StrategyStageConfig(_ConfigModel):
    """All strategy-selection policy (``config/strategy.yaml``).

    Distinct from ``strategies`` — the per-strategy specifications in
    ``config/strategies/``. This file says how the *stage* behaves; those say
    what each strategy *is*.
    """

    config_version: str
    eligibility: StrategyEligibilityConfig = Field(default_factory=StrategyEligibilityConfig)
    limits: StrategyLimitsConfig = Field(default_factory=StrategyLimitsConfig)
    agent: StrategyAgentConfig


# ---------------------------------------------------------------------------
# Contract selection policy (Milestone 6)
# ---------------------------------------------------------------------------
class ReferencePriceField(StrEnum):
    """Which quote field a reference price may be taken from.

    Recorded rather than assumed: "the price" is ambiguous when a quote carries
    a last, a close and a midpoint that disagree, and a strike chosen against an
    unrecorded field cannot be audited afterwards.
    """

    LAST = "LAST"
    CLOSE = "CLOSE"
    MID = "MID"
    BID = "BID"
    ASK = "ASK"


class UnknownLiquidityPolicy(StrEnum):
    """What to do with a contract whose option-level liquidity is unknown.

    Never coerced into a number. "No open interest was reported" and "open
    interest is zero" are different claims, and only the second is evidence.
    """

    #: Treat unknown liquidity as a rejection. The honest default: an option we
    #: have not established to be liquid may not be traded on that assumption.
    REJECT = "REJECT"
    #: Admit it, flagged, and let a later stage decide.
    ALLOW = "ALLOW"


class ContractExpirationConfig(_ConfigModel):
    """Global expiration policy, and the default a strategy may narrow."""

    rule: ExpirationSelectionPolicy = ExpirationSelectionPolicy.TARGET_DTE
    target_dte: int = Field(default=21, ge=0)
    event_max_days_after: int = Field(default=14, ge=0)
    #: An expiration on a day the calendar says the market is closed is a
    #: rejection. A day the calendar does not cover is accepted and recorded —
    #: an honest unknown, not a guess.
    require_trading_day: bool = True


class ContractStrikeConfig(_ConfigModel):
    """Global strike policy limits."""

    #: How far the chosen strike may sit from the policy's target, as a
    #: percentage of the reference price. A chain too coarse to express the
    #: policy is a rejection, not a rounding.
    max_strike_distance_pct: float = Field(default=5.0, ge=0.0)
    #: Which quote fields may supply the reference price, in order of
    #: preference. The field actually used is recorded on the selection.
    reference_price_fields: list[ReferencePriceField] = Field(
        default_factory=lambda: [
            ReferencePriceField.LAST,
            ReferencePriceField.CLOSE,
            ReferencePriceField.MID,
        ],
        min_length=1,
    )
    #: Whether an option quote's own ``underlying_price`` may be used when the
    #: underlying's quote is unavailable. It is the same measurement from the
    #: same provider, so it is preferred to failing outright — but it is
    #: recorded as such.
    allow_option_underlying_price: bool = True


class ContractQuoteConfig(_ConfigModel):
    """What a candidate's quote must satisfy before it can be selected."""

    require_quote: bool = True
    max_quote_age_seconds: int = Field(default=86_400, ge=0)
    require_research_usable: bool = True
    unknown_liquidity_policy: UnknownLiquidityPolicy = UnknownLiquidityPolicy.REJECT


class ContractLimitsConfig(_ConfigModel):
    """Bounds on the work and on the size of the stored record."""

    max_candidates: int = Field(default=5000, ge=1)
    #: Rejected candidates kept on the stored record. Bounded so a 500-strike
    #: chain does not produce an unreadable artifact; the count of everything
    #: considered is always recorded, so truncation is visible.
    max_rejected_recorded: int = Field(default=50, ge=0)


class ContractSelectionConfig(_ConfigModel):
    """All contract-selection policy (``config/contract_selection.yaml``).

    Every rule the deterministic selector applies lives here or in a strategy
    specification. Nothing about which strike or which expiry is chosen is
    hard-coded in the selector, and no LLM is involved at any point.
    """

    config_version: str
    #: Stamped onto every stored selection, so a past choice traces to the exact
    #: policy that produced it.
    selection_policy_version: str = Field(min_length=1)
    expiration: ContractExpirationConfig = Field(default_factory=ContractExpirationConfig)
    strike: ContractStrikeConfig = Field(default_factory=ContractStrikeConfig)
    quotes: ContractQuoteConfig = Field(default_factory=ContractQuoteConfig)
    limits: ContractLimitsConfig = Field(default_factory=ContractLimitsConfig)


class ExecutionConfig(_ConfigModel):
    """Execution policy (``config/execution.yaml``, Milestone 8).

    The only configuration in the system that can permit an order to be sent,
    so every default ships closed and several combinations fail to load rather
    than being silently narrowed. A clamped permission is a permission nobody
    can see, and here that would mean nobody could see what the system is
    allowed to trade.
    """

    #: Master switch. Ships OFF; with this false the stage validates and builds
    #: an order but never reaches a broker.
    enabled: bool = False
    allow_live: bool = False
    paper_only: bool = True
    require_explicit_authorization: bool = True

    default_order_type: OrderType = OrderType.LIMIT
    permitted_order_types: list[OrderType] = Field(default_factory=lambda: [OrderType.LIMIT])
    time_in_force: TimeInForce = TimeInForce.DAY

    limit_price_offset_pct: float = Field(default=0.0, ge=-50.0, le=50.0)
    price_increment: Money = Field(default=Decimal("0.01"), gt=0)

    allocation_validity_minutes: int = Field(default=60, ge=0)
    price_validity_seconds: int = Field(default=900, ge=0)

    max_price_drift_pct: float = Field(default=2.0, ge=0.0, le=100.0)
    allow_repricing: bool = False

    require_market_open: bool = True
    block_on_unknown_session: bool = True

    one_connection_per_submission: bool = True
    submission_timeout_seconds: float = Field(default=30.0, gt=0)
    auto_retry_on_timeout: bool = False
    verify_paper_account: bool = True
    paper_account_prefixes: list[str] = Field(default_factory=lambda: ["DU", "DF"])

    multi_leg_as_combo: bool = True
    allow_independent_leg_orders: bool = False
    allow_short_legs: bool = False

    @model_validator(mode="after")
    def _live_is_not_available(self) -> ExecutionConfig:
        if self.allow_live:
            raise ValueError(
                "execution.allow_live is set, but live trading is delivered in Milestone 12 "
                "behind a signed-off readiness checklist. This flag cannot enable it and "
                "must not look as though it could."
            )
        if not self.paper_only:
            raise ValueError(
                "execution.paper_only=false is refused: PAPER is the only mode this "
                "milestone submits orders in."
            )
        return self

    @model_validator(mode="after")
    def _authorisation_cannot_be_switched_off(self) -> ExecutionConfig:
        if not self.require_explicit_authorization:
            raise ValueError(
                "execution.require_explicit_authorization=false would let loading an "
                "allocation submit it. The explicit execution authorisation is the boundary "
                "this milestone exists to draw and there is no configuration that removes it."
            )
        return self

    @model_validator(mode="after")
    def _the_order_vocabulary_is_narrow(self) -> ExecutionConfig:
        if not self.permitted_order_types:
            raise ValueError("execution.permitted_order_types must list at least one order type")
        if self.default_order_type not in self.permitted_order_types:
            raise ValueError(
                f"execution.default_order_type {self.default_order_type.value} is not in "
                f"permitted_order_types "
                f"{[t.value for t in self.permitted_order_types]}"
            )
        if OrderType.MARKET in self.permitted_order_types:
            raise ValueError(
                "execution.permitted_order_types includes MARKET. A market order on an option "
                "is an unbounded price, and Milestone 7 authorised a specific amount of "
                "capital against a specific quoted cost. LIMIT only."
            )
        return self

    @model_validator(mode="after")
    def _multi_leg_stays_one_order(self) -> ExecutionConfig:
        if self.allow_independent_leg_orders:
            raise ValueError(
                "execution.allow_independent_leg_orders is set. A straddle submitted as two "
                "independent orders can half-fill into a naked long call — a position nobody "
                "authorised and no limit was checked against. A multi-leg structure is one "
                "order or it is MULTI_LEG_UNSUPPORTED."
            )
        if not self.multi_leg_as_combo:
            raise ValueError(
                "execution.multi_leg_as_combo=false leaves no way to submit a multi-leg "
                "structure as one order."
            )
        return self

    @model_validator(mode="after")
    def _no_automatic_retry(self) -> ExecutionConfig:
        if self.auto_retry_on_timeout:
            raise ValueError(
                "execution.auto_retry_on_timeout is set. A timeout does not mean the broker "
                "did not receive the order; retrying is how one intention becomes two "
                "positions. An uncertain submission is resolved by observation."
            )
        return self

    @model_validator(mode="after")
    def _short_legs_are_not_in_the_vocabulary(self) -> ExecutionConfig:
        if self.allow_short_legs:
            raise ValueError(
                "execution.allow_short_legs is set, but every strategy this system can select "
                "is a long-premium structure. Enabling it here would let an upstream bug "
                "become an uncovered short option."
            )
        return self

    @model_validator(mode="after")
    def _prefixes_are_needed_to_verify_an_account(self) -> ExecutionConfig:
        if self.verify_paper_account and not self.paper_account_prefixes:
            raise ValueError(
                "execution.verify_paper_account is set but paper_account_prefixes is empty, "
                "so no account could ever be proved to be a paper account."
            )
        return self


class PositionSnapshotConfig(_ConfigModel):
    """How broker position snapshots are stored (``positions.snapshot``)."""

    #: An account that genuinely holds nothing is a fact worth recording. A
    #: *failed* read is never stored as an empty snapshot — that distinction is
    #: enforced in the service, not here.
    store_empty_snapshots: bool = True
    max_age_seconds: int = Field(default=900, ge=0)


class PositionFillConfig(_ConfigModel):
    """How broker fills are recorded (``positions.fills``)."""

    deduplicate_by_broker_execution_id: bool = True
    flag_fills_without_broker_execution_id: bool = True
    #: Deliberately false. IBKR often reports a fill before its commission
    #: report arrives, and an absent commission is ``None`` — never zero.
    require_commission: bool = False

    @model_validator(mode="after")
    def _deduplication_cannot_be_switched_off(self) -> PositionFillConfig:
        if not self.deduplicate_by_broker_execution_id:
            raise ValueError(
                "positions.fills.deduplicate_by_broker_execution_id=false would record the "
                "same broker fill once per poll. Reconciliation observes the same fills "
                "repeatedly by design; identity is what keeps that idempotent."
            )
        return self


class PositionExpectationConfig(_ConfigModel):
    """What may contribute to an internal expected position."""

    from_confirmed_fills_only: bool = True
    reflect_partial_fills: bool = True
    report_strategy_structures: bool = True
    report_partial_structures: bool = True

    @model_validator(mode="after")
    def _only_a_fill_makes_a_position(self) -> PositionExpectationConfig:
        if not self.from_confirmed_fills_only:
            raise ValueError(
                "positions.expected_positions.from_confirmed_fills_only=false would let an "
                "allocation, a submitted order or an UNKNOWN submission count as a position. "
                "Only a confirmed broker fill establishes that something is held."
            )
        if not self.reflect_partial_fills:
            raise ValueError(
                "positions.expected_positions.reflect_partial_fills=false would record a "
                "partly filled order as either its full size or nothing. Four of ten is four."
            )
        return self

    @model_validator(mode="after")
    def _a_partial_structure_stays_visible(self) -> PositionExpectationConfig:
        if self.report_strategy_structures and not self.report_partial_structures:
            raise ValueError(
                "positions.expected_positions.report_partial_structures=false would round a "
                "half-present straddle to COMPLETE or MISSING. It is neither, and the case "
                "where it matters is a naked long call nobody authorised."
            )
        return self


class PositionsConfig(_ConfigModel):
    """Position ledger policy (``config/positions.yaml``, Milestone 9).

    Governs two deliberately separate records: what the broker says it holds,
    and what this system believes should exist. Nothing configured here can
    place, cancel or modify an order.
    """

    account_mask_visible_characters: int = Field(default=4, ge=0, le=8)
    prefer_broker_contract_id: bool = True
    snapshot: PositionSnapshotConfig = Field(default_factory=PositionSnapshotConfig)
    fills: PositionFillConfig = Field(default_factory=PositionFillConfig)
    expected_positions: PositionExpectationConfig = Field(default_factory=PositionExpectationConfig)
    adopt_orphan_positions: bool = False

    @model_validator(mode="after")
    def _identity_comes_from_the_broker(self) -> PositionsConfig:
        if not self.prefer_broker_contract_id:
            raise ValueError(
                "positions.prefer_broker_contract_id=false would identify an option position "
                "by symbol, strike, expiration and right alone. Adjusted contracts share those "
                "fields, so two different instruments would eventually merge into one record."
            )
        return self

    @model_validator(mode="after")
    def _orphans_are_never_adopted_automatically(self) -> PositionsConfig:
        if self.adopt_orphan_positions:
            raise ValueError(
                "positions.adopt_orphan_positions is set. Adopting a broker position with no "
                "execution behind it means inventing an allocation, an execution, a strategy "
                "and a research thesis for a holding this system knows nothing about. Report "
                "it as ORPHAN_BROKER_POSITION; controlled onboarding is a later milestone."
            )
        return self


class ReservationPolicyConfig(_ConfigModel):
    """When committed campaign capital may move (``reconciliation.reservations``).

    Every switch answers one question: *is there proof the capital was not
    spent?* Where there is not, it stays committed.
    """

    release_on_execution_failed: bool = True
    release_on_broker_rejected: bool = True
    release_on_cancelled_without_fill: bool = True
    release_on_expired_without_fill: bool = True
    #: Never. An UNKNOWN execution may be a live order at the broker.
    release_on_unknown: bool = False
    release_when_never_executed: bool = False
    consume_on_fill: bool = True
    use_actual_fill_economics: bool = True
    release_remainder_on_partial_fill: bool = False
    allow_currency_conversion: bool = False

    @model_validator(mode="after")
    def _an_unknown_execution_keeps_its_capital(self) -> ReservationPolicyConfig:
        if self.release_on_unknown:
            raise ValueError(
                "reconciliation.reservations.release_on_unknown is set. An UNKNOWN execution "
                "may be a live order at the broker right now; releasing its capital and "
                "allocating it again is exactly how one intention becomes two positions. "
                "UNKNOWN is resolved by observation, and only a resolved outcome frees it."
            )
        return self

    @model_validator(mode="after")
    def _an_unexecuted_authorisation_keeps_its_capital(self) -> ReservationPolicyConfig:
        if self.release_when_never_executed:
            raise ValueError(
                "reconciliation.reservations.release_when_never_executed is set. An "
                "authorisation nobody has executed yet is not evidence that nothing will be "
                "sent, and freeing its budget would let the same euro be authorised twice."
            )
        return self

    @model_validator(mode="after")
    def _a_partial_fill_does_not_free_the_remainder(self) -> ReservationPolicyConfig:
        if self.release_remainder_on_partial_fill:
            raise ValueError(
                "reconciliation.reservations.release_remainder_on_partial_fill is set, but the "
                "unfilled part of a partially filled order may still be working at the broker. "
                "Only a terminal outcome releases it."
            )
        return self

    @model_validator(mode="after")
    def _no_invented_exchange_rate(self) -> ReservationPolicyConfig:
        if self.allow_currency_conversion:
            raise ValueError(
                "reconciliation.reservations.allow_currency_conversion is set, but no "
                "deterministic FX rate source exists. Converting committed capital at an "
                "invented rate would misstate the campaign by an amount nobody recorded. "
                "This is Milestone 7's refusal, preserved rather than undone."
            )
        return self


class ReconciliationConfig(_ConfigModel):
    """Reconciliation policy (``config/reconciliation.yaml``, Milestone 9).

    Reconciliation reports; it does not repair. Two keys name the possibility
    of it doing more — ``corrective_orders_permitted`` and
    ``auto_adopt_orphan_positions`` — and both fail to load if set, so that
    their refusal is visible in the file rather than merely absent from it.
    """

    enabled: bool = True

    require_broker_account: bool = True
    require_broker_positions: bool = True
    require_broker_orders: bool = True
    #: Deliberately false: IBKR's execution list is session-scoped, so absence
    #: from it is not evidence that a fill never happened.
    require_broker_fills: bool = False
    treat_broker_fills_as_complete_history: bool = False

    one_connection_per_read: bool = True
    probe_connection_latency: bool = False
    max_broker_data_age_seconds: int = Field(default=300, ge=0)

    corrective_orders_permitted: bool = False
    auto_adopt_orphan_positions: bool = False
    resolve_unknown_executions: bool = True
    fail_on_failed_execution_with_broker_order: bool = True

    severity: dict[ReconciliationFindingType, ReconciliationSeverity] = Field(default_factory=dict)
    reservations: ReservationPolicyConfig = Field(default_factory=ReservationPolicyConfig)

    @model_validator(mode="after")
    def _reconciliation_never_trades(self) -> ReconciliationConfig:
        if self.corrective_orders_permitted:
            raise ValueError(
                "reconciliation.corrective_orders_permitted is set. Reconciliation compares "
                "records against broker reality; deciding what to do about a discrepancy is a "
                "separate judgement, and a comparison that could place an order would make "
                "every stale record a trading signal."
            )
        if self.auto_adopt_orphan_positions:
            raise ValueError(
                "reconciliation.auto_adopt_orphan_positions is set. A broker holding with no "
                "execution behind it is reported, never absorbed into the internal ledger with "
                "invented provenance."
            )
        return self

    @model_validator(mode="after")
    def _every_finding_has_a_stated_severity(self) -> ReconciliationConfig:
        """An unlisted finding type is a load failure, not a quiet default.

        A severity policy is a financial judgement — how alarming is a position
        we believe in that the broker does not hold? — and defaulting an
        unlisted one to something reassuring would make the most dangerous
        finding the easiest to miss.
        """
        missing = sorted(
            finding.value for finding in ReconciliationFindingType if finding not in self.severity
        )
        if missing:
            raise ValueError(
                f"reconciliation.severity does not state a severity for: {', '.join(missing)}. "
                f"Every finding type must be listed; an unlisted one would default to whatever "
                f"code happened to choose, which is not a policy anyone can see."
            )
        return self

    def severity_of(self, finding: ReconciliationFindingType) -> ReconciliationSeverity:
        """The configured severity. Present for every member by construction."""
        return self.severity[finding]

    @property
    def blocking_severities(self) -> frozenset[ReconciliationSeverity]:
        """Severities that should stop new trading. Fixed, not configurable.

        Making this a setting would let an operator switch off the consequence
        of a discrepancy without resolving it, which is the same as not
        reconciling at all.
        """
        return frozenset({ReconciliationSeverity.CRITICAL})


class UnusableQuotePolicy(StrEnum):
    """What to do when the price an exit rests on is not usable.

    Two honest answers and no third. ``BLOCK`` refuses to judge the position at
    all; ``WAIT`` keeps it and says the evaluation was made without a price.
    There is deliberately no ``SUBSTITUTE``: reaching for the ask, the last
    print or the price we paid when the bid is missing would value the position
    at something no seller could get, and every number downstream would
    faithfully reproduce it.
    """

    BLOCK = "BLOCK"
    WAIT = "WAIT"


class ExitExpirationConfig(_ConfigModel):
    """The global expiration safety window (``exit.expiration``).

    An option is not a stock: the position stops existing on a date, and the
    last week of a long-premium position is where theta takes what the thesis
    earned. Both thresholds are counted in calendar days from the
    **exchange-local** date of the evaluation instant to the expiration, through
    ``config/data.yaml``'s market calendar.
    """

    #: Below this, the position is near its deadline. Reported, not exited.
    warning_dte: int = Field(default=10, ge=0)
    #: At or below this, exit is required. A strategy may force an exit
    #: *earlier* (a larger ``close_at_dte``) and may never force one later.
    force_exit_dte: int = Field(default=5, ge=0)
    #: A year the calendar does not cover answers UNKNOWN. True blocks; false
    #: would let an expiration nobody verified pass as ordinary.
    block_on_unknown_calendar: bool = True
    #: Whether an expiration that has already passed is a block rather than a
    #: force-exit. It is: an expired option cannot be sold, and an order for one
    #: is not an exit.
    block_on_expired_contract: bool = True

    @model_validator(mode="after")
    def _the_warning_precedes_the_deadline(self) -> ExitExpirationConfig:
        if self.warning_dte < self.force_exit_dte:
            raise ValueError(
                f"exit.expiration.warning_dte {self.warning_dte} is below force_exit_dte "
                f"{self.force_exit_dte}; a warning that fires after the forced exit warns "
                f"nobody about anything"
            )
        return self


class ExitDataQualityConfig(_ConfigModel):
    """Which price an exit is measured against, and when it may not be used."""

    #: The exit is a *sale*, so the honest value of a long option is the bid.
    #: Recorded on every valuation, and never silently swapped for another
    #: field when it is missing.
    quote_field: ExitQuoteField = ExitQuoteField.BID
    #: How old the quote may be at the evaluation instant. Measured against the
    #: evaluation's own ``as_of``, exactly as ``risk.yaml``'s window is, so a
    #: historical replay is not penalised for being run today.
    max_quote_age_seconds: int = Field(default=900, ge=0)
    #: The data layer's own verdict, consumed rather than re-graded.
    require_research_usable: bool = True
    on_unavailable: UnusableQuotePolicy = UnusableQuotePolicy.BLOCK
    on_stale: UnusableQuotePolicy = UnusableQuotePolicy.BLOCK
    #: Must stay false. See :class:`UnusableQuotePolicy`.
    allow_quote_field_substitution: bool = False

    @model_validator(mode="after")
    def _no_field_is_substituted_for_another(self) -> ExitDataQualityConfig:
        if self.allow_quote_field_substitution:
            raise ValueError(
                "exit.data_quality.allow_quote_field_substitution is set. Valuing a position "
                "at the ask, the last print or the price we paid because the bid is missing "
                "invents a price no seller could get, and every trailing level, take-profit "
                "and maximum-loss figure derived from it would inherit the invention."
            )
        return self


class ExitTrailingConfig(_ConfigModel):
    """The global trailing-stop envelope (``exit.trailing``).

    Percentages are of the position's own economics, never of an index or an
    account: ``activation_return_pct`` is the gain over entry cost that arms
    the trail, and ``trail_distance_pct`` is how far below the peak favourable
    value the level sits.
    """

    enabled: bool = True
    activation_return_pct: float = Field(default=25.0, ge=0.0)
    #: The **widest** trail any strategy may use. A strategy trailing tighter
    #: gives back less and is a narrowing; a looser one would widen a global
    #: safety boundary and fails to load.
    trail_distance_pct: float = Field(default=40.0, gt=0.0, le=100.0)
    #: How far the peak must improve before the level is moved at all. Stops a
    #: level from being rewritten on every tick of noise, which would fill the
    #: history with events that changed nothing.
    min_improvement_pct: float = Field(default=1.0, ge=0.0)
    #: Whether a level may ever move against the position. It may not, and this
    #: key exists so the absence is a stated policy rather than an oversight.
    allow_level_to_fall: bool = False

    @model_validator(mode="after")
    def _a_trail_only_ratchets(self) -> ExitTrailingConfig:
        if self.allow_level_to_fall:
            raise ValueError(
                "exit.trailing.allow_level_to_fall is set. A trailing stop that follows a "
                "position down is not a stop: it guarantees the position is never sold, "
                "however much of its peak it has given back."
            )
        return self


class ExitTakeProfitConfig(_ConfigModel):
    """The global take-profit ceiling (``exit.take_profit``).

    Take profit is deliberately **not** a safety limit, and the narrowing rule
    reflects that: a strategy may take profit earlier (a lower percentage) or
    not at all (``take_profit_pct: null``). Never taking a profit risks giving
    a gain back, which the trailing stop and the expiration policy exist to
    bound; it cannot lose more than the position's declared maximum loss.
    """

    enabled: bool = True
    #: Return over entry cost, as a percentage. The highest target any strategy
    #: may hold out for.
    return_pct: float = Field(default=200.0, gt=0.0)


class ExitMaxLossConfig(_ConfigModel):
    """The global maximum-loss boundary (``exit.max_loss``).

    Measured against the strategy's declared
    :class:`~trading_system.domain.enums.MaxLossBasis`, which Milestone 7 owns.
    Nothing here defines a second maximum-loss formula, and a basis this system
    cannot compute is ``RISK_BASIS_UNAVAILABLE`` — a block, never an estimate.
    """

    enabled: bool = True
    #: The most of the position's declared maximum loss that may be given up
    #: before an exit is required, as a percentage. A strategy may set a
    #: tighter figure and may never set a looser one.
    loss_pct: float = Field(default=60.0, gt=0.0, le=100.0)
    #: Whether an unavailable risk basis blocks. It does: an unquantified loss
    #: is not a small one.
    block_on_unavailable_basis: bool = True


class ExitThesisConfig(_ConfigModel):
    """How a research thesis participates in an exit (``exit.thesis``).

    The thesis is *consumed*, never re-derived. Milestone 5's report states the
    invalidation conditions; this stage checks the ones that can be checked
    against a structured fact and labels the rest ``NOT_EVALUATED``. No model is
    consulted, here or anywhere in Milestone 10.
    """

    enabled: bool = True
    exit_on_invalidated: bool = True
    #: A weakening thesis is not an invalidated one. Shipping this false keeps
    #: a judgement call out of a deterministic engine.
    exit_on_weakening: bool = False
    #: Whether a report that cannot be read blocks the evaluation.
    block_on_unavailable_thesis: bool = True
    #: Whether a condition nobody can check deterministically may nonetheless
    #: trigger an exit. It may not: interpreting prose as a trading signal is
    #: exactly the judgement this milestone refuses to make.
    allow_prose_interpretation: bool = False

    @model_validator(mode="after")
    def _prose_is_never_a_signal(self) -> ExitThesisConfig:
        if self.allow_prose_interpretation:
            raise ValueError(
                "exit.thesis.allow_prose_interpretation is set. An invalidation condition "
                "written as prose is labelled NOT_EVALUATED; reading an arbitrary sentence as "
                "a sell signal would make this engine non-deterministic and would need a model "
                "to do it, which Milestone 10 has none of."
            )
        return self


class ExitOrderConfig(_ConfigModel):
    """How an exit order is priced (``exit.order``).

    Everything else about sending it — validation, the order intent, the broker
    call, ``UNKNOWN`` handling — is Milestone 8's and is reused unchanged. This
    section states only what is genuinely different about a closing order.
    """

    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.DAY
    #: Applied to the exit reference quote. **Negative** offers below the bid,
    #: which is what makes an exit likely to fill; the entry side's positive
    #: offset would do the opposite here. Rounding is always down, so an exit
    #: can only ever ask for less than the reference and never more.
    limit_price_offset_pct: float = Field(default=-1.0, ge=-50.0, le=50.0)
    price_increment: Money = Field(default=Decimal("0.01"), gt=0)
    #: An exit order needs its own explicit authorisation, exactly as an entry
    #: does. There is no configuration in which deciding to exit sends one.
    require_explicit_authorization: bool = True

    @model_validator(mode="after")
    def _authorisation_cannot_be_switched_off(self) -> ExitOrderConfig:
        if not self.require_explicit_authorization:
            raise ValueError(
                "exit.order.require_explicit_authorization=false would let an exit evaluation "
                "place an order. Deciding that a position should close and closing it are two "
                "acts, and Milestone 8's confirmation boundary applies to both."
            )
        if self.order_type is not OrderType.LIMIT:
            raise ValueError(
                f"exit.order.order_type is {self.order_type.value}. A market order to close an "
                f"option is an unbounded price in the one direction that cannot be recovered "
                f"from; LIMIT only, exactly as on the entry side."
            )
        return self


class ExitConfig(_ConfigModel):
    """Exit management policy (``config/exit.yaml``, Milestone 10).

    The global envelope. ``config/strategies/*.yaml`` may narrow every safety
    value here and may widen none; widening is a load failure in
    :class:`SystemConfig`, never a clamp, for the same reason a clamped risk
    limit is a limit nobody can see.

    No value here is an AI input and no field carries a model, a prompt or a
    rationale. Exit management is arithmetic over stored artifacts.
    """

    #: Whether exit *evaluation* runs at all. Distinct from
    #: ``execution.enabled``, which is what permits an order to be sent: a
    #: system may evaluate exits for a long time before it is allowed to act on
    #: one, and conflating the two would make "should we close this" impossible
    #: to answer safely.
    enabled: bool = True
    policy_version: str = "1.0.0"

    expiration: ExitExpirationConfig = Field(default_factory=ExitExpirationConfig)
    data_quality: ExitDataQualityConfig = Field(default_factory=ExitDataQualityConfig)
    trailing: ExitTrailingConfig = Field(default_factory=ExitTrailingConfig)
    take_profit: ExitTakeProfitConfig = Field(default_factory=ExitTakeProfitConfig)
    max_loss: ExitMaxLossConfig = Field(default_factory=ExitMaxLossConfig)
    thesis: ExitThesisConfig = Field(default_factory=ExitThesisConfig)
    order: ExitOrderConfig = Field(default_factory=ExitOrderConfig)

    #: A multi-leg structure is exited as one combo order or not at all.
    #: ``true`` fails to load: closing one leg of a straddle leaves a naked
    #: long option against limits nobody checked for one, which is the same
    #: failure Milestone 8 refuses on the way in.
    allow_independent_leg_exit: bool = False
    #: Whether an unresolved reconciliation blocks exit decisions. It does:
    #: acting on a position whose broker state is disputed is acting on a guess.
    block_on_reconciliation_findings: bool = True
    #: Whether a position the broker does not report may still be exited. It
    #: may not — there is nothing to sell, and an order for it is not an exit.
    require_broker_confirmation: bool = True

    @model_validator(mode="after")
    def _a_structure_exits_whole(self) -> ExitConfig:
        if self.allow_independent_leg_exit:
            raise ValueError(
                "exit.allow_independent_leg_exit is set. A straddle whose call is sold and "
                "whose put is not is a naked long put: a position nobody authorised, sized "
                "against limits nobody checked for it. A structure exits as one order or the "
                "decision is refused."
            )
        return self

    def precedence(self) -> tuple[ExitPolicyKind, ...]:
        """The order policies are evaluated in. Fixed, and deliberately not configurable.

        Making precedence a setting would let an operator move profit-taking
        ahead of a safety check without noticing that is what they had done.
        """
        return EXIT_POLICY_PRECEDENCE


# ---------------------------------------------------------------------------
# Readiness (Milestone 12)
# ---------------------------------------------------------------------------
class ReadinessFreshnessWindows(_ConfigModel):
    """Time-bounded freshness windows, in seconds.

    Every one of these describes something that can change without a commit,
    which is exactly what distinguishes them from the revision-bound evidence
    alongside. There is deliberately no single global window: a broker probe
    and a realised daily figure go stale on completely different timescales,
    and one number would be wrong for both.
    """

    broker_seconds: float = Field(default=900.0, gt=0)
    health_seconds: float = Field(default=3600.0, gt=0)
    reconciliation_seconds: float = Field(default=21600.0, gt=0)
    pnl_seconds: float = Field(default=86400.0, gt=0)
    scheduler_seconds: float = Field(default=86400.0, gt=0)
    observability_seconds: float = Field(default=3600.0, gt=0)
    configuration_seconds: float = Field(default=3600.0, gt=0)


class ReadinessFreshnessConfig(_ConfigModel):
    """How long each kind of readiness evidence stays usable.

    Two mechanisms, because evidence goes stale in two different ways. Most of
    it expires with the *clock*: a broker probe from an hour ago says nothing
    about now. Some of it expires with the *working tree*: a test result
    belongs to the commit it ran against, and no amount of elapsed time makes
    it wrong while the code is unchanged — nor does any amount of freshness
    make it right once the code has moved.
    """

    revision_bound: tuple[ReadinessCriterionId, ...] = ()
    windows: ReadinessFreshnessWindows = Field(default_factory=ReadinessFreshnessWindows)

    def is_revision_bound(self, criterion: ReadinessCriterionId) -> bool:
        """Whether this criterion's evidence expires with the code, not the clock."""
        return criterion in self.revision_bound


class ReadinessLevelConfig(_ConfigModel):
    """Which criteria block one readiness level."""

    blocking: tuple[ReadinessCriterionId, ...] = ()
    #: Whether this level also requires everything the paper gate requires.
    #: Live review inherits; expressing it as an extension means a criterion
    #: added to the paper gate cannot go missing from the live one.
    inherits_paper: bool = False


class ReadinessLevelsConfig(_ConfigModel):
    paper: ReadinessLevelConfig = Field(default_factory=ReadinessLevelConfig)
    live_review: ReadinessLevelConfig = Field(default_factory=ReadinessLevelConfig)


class OperationalHistoryConfig(_ConfigModel):
    """The floor on accumulated evidence for live review (brief section 21).

    "The system works" and "the system has been operated" are different
    claims, and a single green run establishes only the first.
    """

    min_readiness_runs: int = Field(default=3, ge=0)
    min_distinct_days: int = Field(default=2, ge=0)
    min_reconciliation_runs: int = Field(default=3, ge=0)
    min_scheduler_ticks: int = Field(default=1, ge=0)
    min_history_days: int = Field(default=2, ge=0)


class ReadinessPaperExecutionConfig(_ConfigModel):
    """The one readiness path that can send a real order.

    Ships disabled, and every widening below is refused at load rather than
    clamped. A clamped limit is a limit nobody can see.
    """

    enabled: bool = False
    submit_on_check: bool = False
    allow_live: bool = False
    max_orders: int = Field(default=1, ge=0, le=1)
    cancel_if_working: bool = True
    observation_seconds: float = Field(default=5.0, ge=0)

    @model_validator(mode="after")
    def _no_configuration_makes_a_check_submit(self) -> ReadinessPaperExecutionConfig:
        if self.submit_on_check:
            raise ValueError(
                "readiness.paper_execution.submit_on_check is set. There is no supported "
                "configuration in which evaluating readiness places an order as a side "
                "effect: reporting on a system and trading with it are different acts, and "
                "the whole point of this milestone is the first one."
            )
        if self.allow_live:
            raise ValueError(
                "readiness.paper_execution.allow_live is set. The readiness gate submits to "
                "PAPER or to nothing. LIVE is refused here, in the broker factory and in the "
                "adapter — three refusals for the one irreversible action this system can take."
            )
        if self.max_orders > 1:
            raise ValueError(
                f"readiness.paper_execution.max_orders is {self.max_orders}. The gate submits "
                f"exactly one controlled order; an uncontrolled sequence of orders is the "
                f"failure mode it exists to prevent (brief section 34)."
            )
        return self


class ObservabilityAcceptanceConfig(_ConfigModel):
    """Where the readiness collector looks for a running telemetry stack.

    Deployment facts, matching ``docker-compose.yml``. Nothing here can move a
    trading verdict: ``operations/health.py`` keeps the trading and
    observability domains disjoint and this inherits that split.
    """

    collector_metrics_url: str = "http://localhost:8888/metrics"
    collector_exporter_url: str = "http://localhost:8889/metrics"
    tempo_url: str = "http://localhost:3200"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    grafana_url: str = "http://localhost:3000"
    probe_timeout_seconds: float = Field(default=5.0, gt=0)
    propagation_timeout_seconds: float = Field(default=90.0, gt=0)
    propagation_poll_seconds: float = Field(default=3.0, gt=0)
    required_datasources: tuple[str, ...] = ("prometheus", "tempo", "loki")
    required_dashboards: tuple[str, ...] = ()


class ReadinessSignoffConfig(_ConfigModel):
    """Rules for the human live-readiness sign-off (brief section 22)."""

    require_explicit_identity: bool = True
    require_live_review: bool = True
    require_clean_working_tree: bool = True
    #: Refused if set. Signing records a decision; it never flips a guard.
    enables_trading: bool = False

    @model_validator(mode="after")
    def _signing_enables_nothing(self) -> ReadinessSignoffConfig:
        if self.enables_trading:
            raise ValueError(
                "readiness.signoff.enables_trading is set. A sign-off records that a human "
                "reviewed the evidence; it may never set TRADING_MODE, LIVE_TRADING_CONFIRMED, "
                "LIVE_READINESS_CHECKLIST_SIGNED_OFF, execution.enabled or IBKR_READ_ONLY. "
                "Those stay in the environment, where a person sets them deliberately."
            )
        return self


class ReadinessConfig(_ConfigModel):
    """Live-trading readiness policy (``config/readiness.yaml``, Milestone 12).

    This file decides what *ready* means. It never decides whether the system
    trades — the readiness package holds no writable broker, has no path to an
    order, and cannot change a mode or a guard.
    """

    config_version: str = "2026.08.15-1"
    freshness: ReadinessFreshnessConfig = Field(default_factory=ReadinessFreshnessConfig)
    levels: ReadinessLevelsConfig = Field(default_factory=ReadinessLevelsConfig)
    operational_history: OperationalHistoryConfig = Field(default_factory=OperationalHistoryConfig)
    paper_execution: ReadinessPaperExecutionConfig = Field(
        default_factory=ReadinessPaperExecutionConfig
    )
    observability_acceptance: ObservabilityAcceptanceConfig = Field(
        default_factory=ObservabilityAcceptanceConfig
    )
    signoff: ReadinessSignoffConfig = Field(default_factory=ReadinessSignoffConfig)

    def blocking_for_paper(self) -> frozenset[ReadinessCriterionId]:
        """Criteria that must PASS before the paper gate opens."""
        return frozenset(self.levels.paper.blocking)

    def blocking_for_live_review(self) -> frozenset[ReadinessCriterionId]:
        """Criteria that must PASS before live review may be recommended.

        A strict superset of the paper set when ``inherits_paper`` is on, which
        is the shipped configuration: a live review that did not require
        everything paper does would be the weaker gate wearing the stronger
        name.
        """
        blocking = set(self.levels.live_review.blocking)
        if self.levels.live_review.inherits_paper:
            blocking |= set(self.levels.paper.blocking)
        return frozenset(blocking)

    @model_validator(mode="after")
    def _live_review_never_narrows_the_paper_gate(self) -> ReadinessConfig:
        if not self.levels.live_review.inherits_paper:
            missing = sorted(
                criterion.value
                for criterion in set(self.levels.paper.blocking)
                - set(self.levels.live_review.blocking)
            )
            if missing:
                raise ValueError(
                    "readiness.levels.live_review does not require "
                    + ", ".join(missing)
                    + ", which the paper gate does. Live review is a stricter question than "
                    "paper readiness; a live gate that dropped a paper requirement would let "
                    "a system be recommended for live review that is not fit for paper. Set "
                    "inherits_paper: true, or list the criteria explicitly."
                )
        return self


class CleanupReferencePriceField(StrEnum):
    """Where an orphan cleanup's reference price may come from.

    One member, deliberately. There is no stored quote for a contract nobody
    selected, and fetching one would need a second uncached round trip on the
    submission connection — which Milestone 2 established is not reliably
    answered. The broker's own reported price for the holding is a real
    observation with real provenance, and it is the only one available at the
    moment the order is built.
    """

    MARKET_PRICE = "MARKET_PRICE"


class CleanupOrderConfig(_ConfigModel):
    """How an orphan-cleanup order is priced (``cleanup.order``).

    Everything else about sending it — validation, the order intent, the broker
    call, ``UNKNOWN`` handling, the immutable record — is Milestone 8's and is
    reused unchanged.
    """

    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.DAY
    reference_price_field: CleanupReferencePriceField = CleanupReferencePriceField.MARKET_PRICE
    #: Applied to the broker's reported price. **Negative** offers below it,
    #: which is what makes a closing order fill. Rounding is always down.
    limit_price_offset_pct: float = Field(default=-3.0, ge=-50.0, le=0.0)
    price_increment: Money = Field(default=Decimal("0.01"), gt=0)
    allow_reference_substitution: bool = False

    @model_validator(mode="after")
    def _the_price_is_never_conjured(self) -> CleanupOrderConfig:
        if self.order_type is not OrderType.LIMIT:
            raise ValueError(
                f"cleanup.order.order_type is {self.order_type.value}. A market order to sell an "
                f"option is an unbounded price in the one direction that cannot be recovered "
                f"from; LIMIT only, exactly as everywhere else in this system."
            )
        if self.allow_reference_substitution:
            raise ValueError(
                "cleanup.order.allow_reference_substitution is set. A missing market price is "
                "not the average cost, not the last close and not the strike. A holding the "
                "broker cannot price is PRICE_UNAVAILABLE and is left exactly where it is."
            )
        return self


class CleanupConfig(_ConfigModel):
    """Orphan-position cleanup policy (``config/cleanup.yaml``).

    The controlled closure of pre-existing broker holdings this system never
    opened. Every default ships closed, and the combinations that would make it
    dangerous **fail to load** rather than being clamped — a clamped safety
    value is a safety value nobody can see.
    """

    #: Master switch. Ships OFF. With this false the operation still selects
    #: targets and evaluates every gate; it simply never reaches a broker.
    enabled: bool = False
    allow_live: bool = False
    paper_only: bool = True
    require_explicit_authorization: bool = True

    #: Only a holding a stored reconciliation actually reported as an orphan.
    #: Deliberately *not* "every position not known internally": those two
    #: differ exactly when the internal ledger could not be read, and that is
    #: the one moment the weaker rule would sell the whole account.
    require_orphan_finding: bool = True
    max_reconciliation_age_seconds: int = Field(default=3600, ge=0)
    max_targets_per_run: int = Field(default=8, ge=1, le=50)
    allow_short_positions: bool = False
    allow_partial_continuation: bool = False

    order: CleanupOrderConfig = Field(default_factory=CleanupOrderConfig)

    @model_validator(mode="after")
    def _live_is_not_available(self) -> CleanupConfig:
        if self.allow_live:
            raise ValueError(
                "cleanup.allow_live is set. Selling a position is not less irreversible than "
                "buying one, and no cleanup is a reason to open a live order path."
            )
        if not self.paper_only:
            raise ValueError(
                "cleanup.paper_only=false is refused: PAPER is the only mode in which this "
                "operation submits anything."
            )
        return self

    @model_validator(mode="after")
    def _authorisation_cannot_be_switched_off(self) -> CleanupConfig:
        if not self.require_explicit_authorization:
            raise ValueError(
                "cleanup.require_explicit_authorization=false would let listing the orphan "
                "positions close them. Inspecting an account and selling out of it are two "
                "acts, and there is no configuration that merges them."
            )
        return self

    @model_validator(mode="after")
    def _only_a_reported_orphan_may_be_targeted(self) -> CleanupConfig:
        if not self.require_orphan_finding:
            raise ValueError(
                "cleanup.require_orphan_finding=false would let this operation target any "
                "holding the internal ledger does not recognise. When the ledger cannot be "
                "read *nothing* is recognised, so that rule liquidates the account precisely "
                "when the system is least able to tell what it owns."
            )
        return self

    @model_validator(mode="after")
    def _short_positions_are_not_unwound_automatically(self) -> CleanupConfig:
        if self.allow_short_positions:
            raise ValueError(
                "cleanup.allow_short_positions is set. Closing a short holding is a purchase "
                "whose cost is unbounded above, and nothing in this system is authorised to "
                "decide it. A short orphan is reported and left alone."
            )
        return self

    @model_validator(mode="after")
    def _there_is_no_retry_loop(self) -> CleanupConfig:
        if self.allow_partial_continuation:
            raise ValueError(
                "cleanup.allow_partial_continuation is set. A cleanup is not a trading "
                "strategy: after a partial fill the remainder is reported and nothing further "
                "is sent. A follow-on order decided by code is how one authorised sale becomes "
                "an unbounded sequence of them."
            )
        return self


class SystemConfig(_ConfigModel):
    """All YAML configuration, loaded and validated together."""

    alerts: AlertsConfig
    cleanup: CleanupConfig
    application: ApplicationConfig
    campaign: CampaignConfig
    contract_selection: ContractSelectionConfig
    data: DataConfig
    execution: ExecutionConfig
    exit: ExitConfig
    observability: ObservabilityConfig
    pnl: PnLConfig
    positions: PositionsConfig
    readiness: ReadinessConfig
    reconciliation: ReconciliationConfig
    research: ResearchConfig
    risk: RiskConfig
    schedules: SchedulesConfig
    sources: SourcesConfig
    strategy: StrategyStageConfig
    strategies: dict[str, StrategyConfig]
    universe: UniverseConfig

    @model_validator(mode="after")
    def _strategies_never_widen_risk_limits(self) -> SystemConfig:
        """A strategy may narrow a global risk limit; it may never widen one.

        The DTE window is the one the specification names explicitly, but the
        rule is not about DTE — it is about which layer owns a limit. A strategy
        file that could raise the spread ceiling or lower the open-interest
        floor would let a strategy specification overrule the risk policy, which
        is exactly the inversion the whole architecture exists to prevent.
        """
        risk = self.risk
        for name, strategy in self.strategies.items():
            violations: list[str] = []
            if strategy.dte_min < risk.dte_min or strategy.dte_max > risk.dte_max:
                violations.append(
                    f"DTE window [{strategy.dte_min}, {strategy.dte_max}] falls outside "
                    f"[{risk.dte_min}, {risk.dte_max}]"
                )
            if strategy.min_option_price_eur < risk.min_option_price_eur:
                violations.append(
                    f"min_option_price_eur {strategy.min_option_price_eur} is below the risk "
                    f"floor {risk.min_option_price_eur}"
                )
            if strategy.max_option_price_eur > risk.max_option_price_eur:
                violations.append(
                    f"max_option_price_eur {strategy.max_option_price_eur} is above the risk "
                    f"ceiling {risk.max_option_price_eur}"
                )
            if strategy.liquidity.min_open_interest < risk.min_open_interest:
                violations.append(
                    f"min_open_interest {strategy.liquidity.min_open_interest} is below the "
                    f"risk floor {risk.min_open_interest}"
                )
            if strategy.liquidity.min_daily_volume < risk.min_daily_volume:
                violations.append(
                    f"min_daily_volume {strategy.liquidity.min_daily_volume} is below the "
                    f"risk floor {risk.min_daily_volume}"
                )
            if strategy.liquidity.max_bid_ask_spread_pct > risk.max_bid_ask_spread_pct:
                violations.append(
                    f"max_bid_ask_spread_pct {strategy.liquidity.max_bid_ask_spread_pct} is "
                    f"above the risk ceiling {risk.max_bid_ask_spread_pct}"
                )
            if violations:
                raise ValueError(
                    f"strategy '{name}' widens a global risk limit: " + "; ".join(violations)
                )
        return self

    @model_validator(mode="after")
    def _campaign_never_widens_a_global_limit(self) -> SystemConfig:
        """The campaign is a child of ``risk.yaml`` and may only narrow it.

        Two limits are declared in both files, and that is deliberate rather
        than duplication: ``risk.yaml`` states the outer boundary of the whole
        system, ``campaign.yaml`` states what *this* campaign permits within
        it. What would be a bug is the campaign quietly permitting more than
        the risk policy does, so that is a load failure — never a clamp.
        """
        risk, campaign = self.risk, self.campaign
        violations: list[str] = []

        if campaign.max_allocation_per_trade_eur > risk.max_allocation_per_trade_eur:
            violations.append(
                f"max_allocation_per_trade_eur {campaign.max_allocation_per_trade_eur} is "
                f"above the risk ceiling {risk.max_allocation_per_trade_eur}"
            )
        if campaign.max_open_positions > risk.max_open_positions:
            violations.append(
                f"max_open_positions {campaign.max_open_positions} is above the risk ceiling "
                f"{risk.max_open_positions}"
            )
        if campaign.max_risk_per_trade_eur > risk.max_total_open_risk_eur:
            violations.append(
                f"max_risk_per_trade_eur {campaign.max_risk_per_trade_eur} is above the total "
                f"open-risk ceiling {risk.max_total_open_risk_eur}; one trade may not be "
                f"permitted more risk than the whole book"
            )
        if campaign.limits.max_positions_per_underlying > risk.max_open_positions:
            violations.append(
                f"limits.max_positions_per_underlying "
                f"{campaign.limits.max_positions_per_underlying} is above the risk ceiling on "
                f"total positions {risk.max_open_positions}"
            )
        if campaign.limits.max_new_positions_per_run > risk.max_open_positions:
            violations.append(
                f"limits.max_new_positions_per_run {campaign.limits.max_new_positions_per_run} "
                f"is above the risk ceiling on total positions {risk.max_open_positions}"
            )

        if violations:
            raise ValueError("campaign.yaml widens a global risk limit: " + "; ".join(violations))
        return self

    @model_validator(mode="after")
    def _strategies_never_widen_exit_limits(self) -> SystemConfig:
        """A strategy may narrow a global exit safety boundary; never widen one.

        The same rule Milestone 6 applies to DTE, liquidity and price, applied
        to the Milestone 10 envelope. Which direction counts as *narrowing*
        differs per limit and is stated for each, because getting the direction
        wrong here would enforce the inverse of the intended safety property:

        ``close_at_dte``
            Closing *earlier* is narrower, so a strategy's value must be at or
            above the global ``force_exit_dte``. A strategy that held a
            position closer to expiry than the global floor would overrule the
            system's expiration safety.
        ``trailing_stop_pct``
            Trailing *tighter* is narrower, so a strategy's value must be at or
            below the global ``trail_distance_pct``.
        ``max_loss_pct``
            Losing *less* is narrower, so a strategy's value must be at or
            below the global ``loss_pct``.
        ``take_profit_pct``
            Taking profit *earlier* is narrower, so a strategy's value must be
            at or below the global ``return_pct``. ``null`` is permitted and is
            not a widening: take profit is not a safety limit, and a position
            that never takes one is still bounded by the trailing stop, the
            maximum loss and the expiration policy.
        ``allow_independent_leg_exit``
            Refused outright in every strategy, exactly as it is globally.
        """
        exit_policy = self.exit
        for name, strategy in self.strategies.items():
            policy = strategy.exit_policy
            violations: list[str] = []

            if policy.close_at_dte < exit_policy.expiration.force_exit_dte:
                violations.append(
                    f"close_at_dte {policy.close_at_dte} is below the global force-exit "
                    f"threshold {exit_policy.expiration.force_exit_dte}; a strategy may close "
                    f"earlier than the global policy, never later"
                )
            if policy.trailing_stop_pct > exit_policy.trailing.trail_distance_pct:
                violations.append(
                    f"trailing_stop_pct {policy.trailing_stop_pct} is above the global trail "
                    f"distance {exit_policy.trailing.trail_distance_pct}; a wider trail gives "
                    f"back more of the peak than the global policy permits"
                )
            if policy.max_loss_pct > exit_policy.max_loss.loss_pct:
                violations.append(
                    f"max_loss_pct {policy.max_loss_pct} is above the global ceiling "
                    f"{exit_policy.max_loss.loss_pct}"
                )
            if (
                policy.take_profit_pct is not None
                and policy.take_profit_pct > exit_policy.take_profit.return_pct
            ):
                violations.append(
                    f"take_profit_pct {policy.take_profit_pct} is above the global ceiling "
                    f"{exit_policy.take_profit.return_pct}"
                )
            if policy.allow_independent_leg_exit:
                violations.append(
                    "allow_independent_leg_exit is set; a multi-leg structure is exited as one "
                    "order or the decision is refused"
                )

            if violations:
                raise ValueError(
                    f"strategy '{name}' widens a global exit limit: " + "; ".join(violations)
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
        "alerts": _read_yaml(directory / "alerts.yaml"),
        "application": _read_yaml(directory / "application.yaml"),
        "campaign": _read_yaml(directory / "campaign.yaml"),
        "cleanup": _read_yaml(directory / "cleanup.yaml"),
        "contract_selection": _read_yaml(directory / "contract_selection.yaml"),
        "data": _read_yaml(directory / "data.yaml"),
        "execution": _read_yaml(directory / "execution.yaml"),
        "exit": _read_yaml(directory / "exit.yaml"),
        "observability": _read_yaml(directory / "observability.yaml"),
        "pnl": _read_yaml(directory / "pnl.yaml"),
        "positions": _read_yaml(directory / "positions.yaml"),
        "readiness": _read_yaml(directory / "readiness.yaml"),
        "reconciliation": _read_yaml(directory / "reconciliation.yaml"),
        "research": _read_yaml(directory / "research.yaml"),
        "risk": _read_yaml(directory / "risk.yaml"),
        "schedules": _read_yaml(directory / "schedules.yaml"),
        "sources": _read_yaml(directory / "sources.yaml"),
        "strategy": _read_yaml(directory / "strategy.yaml"),
        "strategies": strategies,
        "universe": _read_yaml(directory / "universe.yaml"),
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
