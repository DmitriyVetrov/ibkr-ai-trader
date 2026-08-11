"""Fixtures for the universe suites.

Two rules hold across every test here, the same ones the data suites use:

* **No network, no broker, no model.** The universe service is constructed with
  a fake :class:`LLMClient` or none at all. Nothing in these tests can reach an
  API, and no test needs a credential.
* **No writing into the repository's own ``data/``.** Every store is rooted at
  ``tmp_path``, so a run leaves nothing behind and cannot be influenced by an
  artifact a previous run left.

The data these tests run against is *stored through the real repository*, not
mocked at the repository interface. A filter that only works against a
hand-built object has not been tested against the thing it will actually see.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.agents.base import (
    AgentUnavailableError,
    LLMResponse,
    ModelIdentity,
    StructuredRequest,
)
from trading_system.data.models import (
    DataQualityReport,
    DataSourceMetadata,
    MarketQuote,
    OptionChain,
)
from trading_system.data.repository import FilesystemDataRepository, build_snapshot
from trading_system.domain.enums import (
    DataQualityIssue,
    DataType,
    MarketDataOrigin,
    OptionRight,
    SecurityType,
    SourceTier,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import (
    AiRankingConfig,
    OptionabilityPolicy,
    Settings,
    SystemConfig,
    UniverseConfig,
    UniverseFilterConfig,
    UniverseSourceConfig,
    load_config,
)
from trading_system.universe.service import UniverseSelectionService
from trading_system.universe.store import FilesystemUniverseRepository

#: A weekday inside the calendar's covered years, during US market hours.
UNIVERSE_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


@pytest.fixture
def universe_now() -> datetime:
    return UNIVERSE_NOW


@pytest.fixture
def universe_clock() -> FixedClock:
    return FixedClock(UNIVERSE_NOW)


@pytest.fixture
def data_repo(tmp_path: Path, universe_clock: FixedClock) -> FilesystemDataRepository:
    return FilesystemDataRepository(tmp_path / "data", clock=universe_clock)


@pytest.fixture
def universe_repo(tmp_path: Path) -> FilesystemUniverseRepository:
    return FilesystemUniverseRepository(tmp_path / "data" / "universe")


# ---------------------------------------------------------------------------
# Storing evidence through the real repository
# ---------------------------------------------------------------------------
@pytest.fixture
def store_quote(data_repo: FilesystemDataRepository) -> Callable[..., MarketQuote]:
    """Persist a market quote snapshot the way the collector would.

    Goes through :func:`build_snapshot` and ``save_snapshot`` so the ledger,
    the hashing and the point-in-time visibility rules are all exercised —
    ``retrieved_at`` in particular, which is what look-ahead protection turns
    on.
    """

    def _store(
        symbol: str = "SPY",
        *,
        as_of: datetime = UNIVERSE_NOW,
        retrieved_at: datetime | None = None,
        last: Decimal | None = Decimal("500.15"),
        close: Decimal | None = Decimal("499.80"),
        bid: Decimal | None = Decimal("500.10"),
        ask: Decimal | None = Decimal("500.20"),
        volume: Decimal | None = Decimal("75000000"),
        currency: str | None = "USD",
        exchange: str | None = "ARCA",
        security_type: SecurityType = SecurityType.STOCK,
        provider: str = "IBKR",
        origin: MarketDataOrigin = MarketDataOrigin.BROKER_DELAYED,
        research_usable: bool = True,
        freshness_valid: bool = True,
        issues: list[DataQualityIssue] | None = None,
    ) -> MarketQuote:
        retrieved = retrieved_at or as_of
        quote = MarketQuote(
            as_of=as_of,
            source=DataSourceMetadata(
                provider=provider,
                source_name=provider,
                source_tier=SourceTier.TIER_1,
                origin=origin,
                retrieved_at=retrieved,
                source_timestamp=as_of,
                observed_at=as_of,
                source_identifier=f"{provider.lower()}:{symbol}",
            ),
            symbol=symbol,
            security_type=security_type,
            currency=currency,
            exchange=exchange,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            volume=volume,
            quality=DataQualityReport(
                research_usable=research_usable,
                freshness_valid=freshness_valid,
                plausibility_valid=research_usable,
                issues=issues or [],
                evaluated_at=retrieved,
            ),
        )
        snapshot = build_snapshot(
            data_type=DataType.MARKET_QUOTE,
            key=symbol.upper(),
            records=[quote],
            provider=provider,
            source_tier=SourceTier.TIER_1,
            origin=origin,
            as_of=as_of,
            retrieved_at=retrieved,
            quality=quote.quality,
        )
        data_repo.save_snapshot(snapshot)
        return quote

    return _store


@pytest.fixture
def store_chain(data_repo: FilesystemDataRepository) -> Callable[..., OptionChain]:
    """Persist an option chain snapshot, which is what establishes optionability."""

    def _store(
        symbol: str = "SPY",
        *,
        as_of: datetime = UNIVERSE_NOW,
        retrieved_at: datetime | None = None,
        expirations: list[date] | None = None,
        strikes: list[Decimal] | None = None,
        provider: str = "IBKR",
    ) -> OptionChain:
        retrieved = retrieved_at or as_of
        chain = OptionChain(
            as_of=as_of,
            source=DataSourceMetadata(
                provider=provider,
                source_name=provider,
                source_tier=SourceTier.TIER_1,
                origin=MarketDataOrigin.BROKER_DELAYED,
                retrieved_at=retrieved,
                source_timestamp=as_of,
            ),
            underlying=symbol,
            exchange="SMART",
            trading_class=symbol,
            multiplier=100,
            expirations=(
                expirations if expirations is not None else [date(2026, 8, 21), date(2026, 9, 18)]
            ),
            strikes=strikes if strikes is not None else [Decimal("495"), Decimal("500")],
            rights=[OptionRight.CALL, OptionRight.PUT],
        )
        snapshot = build_snapshot(
            data_type=DataType.OPTION_CHAIN,
            key=symbol.upper(),
            records=[chain],
            provider=provider,
            source_tier=SourceTier.TIER_1,
            origin=MarketDataOrigin.BROKER_DELAYED,
            as_of=as_of,
            retrieved_at=retrieved,
            quality=DataQualityReport(evaluated_at=retrieved),
        )
        data_repo.save_snapshot(snapshot)
        return chain

    return _store


@pytest.fixture
def optionable_symbols(
    store_quote: Callable[..., MarketQuote], store_chain: Callable[..., OptionChain]
) -> Callable[..., list[str]]:
    """Store a clean, eligible quote and chain for each of several symbols."""

    def _store(symbols: list[str] | None = None, **kwargs: object) -> list[str]:
        chosen = symbols or ["SPY", "QQQ", "NVDA"]
        for index, symbol in enumerate(chosen):
            store_quote(
                symbol,
                volume=Decimal(90_000_000 - index * 1_000_000),
                **kwargs,
            )
            store_chain(symbol)
        return chosen

    return _store


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.fixture
def make_universe_config(system_config: SystemConfig) -> Callable[..., SystemConfig]:
    """Build a SystemConfig whose universe policy the test controls.

    Everything outside ``universe`` stays as shipped, so a test never silently
    depends on a made-up risk limit or freshness window.
    """

    def _make(
        *,
        symbols: list[str] | None = None,
        min_price: Decimal = Decimal("5.00"),
        min_volume: int = 1_000_000,
        max_data_age_seconds: int = 86_400,
        require_research_usable: bool = True,
        optionability_policy: OptionabilityPolicy = OptionabilityPolicy.REQUIRED,
        exclusions: list[str] | None = None,
        max_candidates: int = 50,
        max_selected: int = 10,
        allowed_currencies: list[str] | None = None,
        allowed_exchanges: list[str] | None = None,
        allowed_security_types: list[SecurityType] | None = None,
        ai_enabled: bool = True,
        allow_fallback: bool = False,
    ) -> SystemConfig:
        universe = UniverseConfig(
            config_version="test-universe-1",
            source=UniverseSourceConfig(
                kind=system_config.universe.source.kind,
                name="test-universe",
                version="test-1",
                symbols=symbols if symbols is not None else ["SPY", "QQQ", "NVDA"],
            ),
            filters=UniverseFilterConfig(
                allowed_security_types=allowed_security_types or [SecurityType.STOCK],
                allowed_currencies=allowed_currencies or ["USD"],
                allowed_exchanges=allowed_exchanges or [],
                min_price=min_price,
                min_average_daily_volume=min_volume,
                max_data_age_seconds=max_data_age_seconds,
                require_research_usable=require_research_usable,
                optionability_policy=optionability_policy,
                exclusions=exclusions or [],
                max_candidates=max_candidates,
                max_selected_assets=max_selected,
            ),
            ai_ranking=AiRankingConfig(
                enabled=ai_enabled,
                model_provider="ANTHROPIC",
                model_name="claude-opus-5",
                prompt_version="test-1.0.0",
                timeout_seconds=30.0,
                max_output_tokens=4000,
                effort="low",
                allow_deterministic_fallback=allow_fallback,
            ),
        )
        return system_config.model_copy(update={"universe": universe})

    return _make


# ---------------------------------------------------------------------------
# Fake LLM clients
# ---------------------------------------------------------------------------
class FakeLLMClient:
    """Returns a canned response. Records the request it was given.

    Deliberately not a mocking-library double: several tests assert on what the
    agent actually *sent* — that the payload carries no repository handle, no
    rejected asset, no raw provider record — and a real object makes that
    readable.

    The canned text may contain ``__RUN_ID__``, which is replaced with the run
    id the service derived. Run ids are content-derived, so a fixture cannot
    predict one, and hard-coding it would make every test brittle to an
    unrelated change in what a run consumes.
    """

    def __init__(self, text: str, *, stop_reason: str | None = "end_turn") -> None:
        self._text = text
        self._stop_reason = stop_reason
        self.requests: list[StructuredRequest] = []

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="FAKE",
            model_name="fake-model-1",
            prompt_version="test-1.0.0",
            prompt_fingerprint="0" * 32,
            model_version="fake-model-1-20260810",
        )

    def complete(self, request: StructuredRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=self._text.replace(_RUN_ID_PLACEHOLDER, _run_id_of(request)),
            identity=self.identity,
            generated_at=UNIVERSE_NOW + timedelta(seconds=1),
            latency_ms=42.0,
            input_tokens=1200,
            output_tokens=340,
            stop_reason=self._stop_reason,
        )


def _run_id_of(request: StructuredRequest) -> str:
    """The run id the service put in the request it sent."""
    payload = json.loads(request.user_content)
    return str(payload["run_id"])


class UnavailableLLMClient:
    """Always fails, the way an unreachable or unconfigured model does."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or AgentUnavailableError("the model is unreachable")

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="FAKE",
            model_name="unreachable",
            prompt_version="test-1.0.0",
            prompt_fingerprint="0" * 32,
        )

    def complete(self, request: StructuredRequest) -> LLMResponse:
        raise self._error


@pytest.fixture
def ranking_text() -> Callable[..., str]:
    """Build a well-formed model response selecting the given symbols in order.

    Returned as *text*, not as a parsed object, so the agent's own parsing and
    validation run in every test that uses it. A fixture that handed back a
    constructed model would skip the code most likely to break.
    """

    def _build(
        selected: list[str],
        *,
        not_selected: list[str] | None = None,
        run_id: str | None = None,
        reasons: list[str] | None = None,
        confidence: str = "HIGH",
    ) -> str:
        rankings = [
            {
                "symbol": symbol,
                "selection": "SELECTED",
                "rank": index + 1,
                "selection_score": float(95 - index * 5),
                "reasons": reasons or ["OPTIONS_AVAILABLE", "SUFFICIENT_DATA_QUALITY"],
                "confidence": confidence,
                "rationale": f"{symbol} has usable data and an established option chain.",
            }
            for index, symbol in enumerate(selected)
        ]
        rankings += [
            {
                "symbol": symbol,
                "selection": "NOT_SELECTED",
                "rank": None,
                "selection_score": None,
                "reasons": ["UNIVERSE_SIZE_LIMIT"],
                "confidence": "MEDIUM",
                "rationale": None,
            }
            for symbol in (not_selected or [])
        ]
        return json.dumps({"run_id": run_id or _RUN_ID_PLACEHOLDER, "rankings": rankings})

    return _build


#: Replaced by :class:`EchoRunIdClient` with the run id the service generated,
#: so a fixture never has to predict a derived identifier.
_RUN_ID_PLACEHOLDER = "__RUN_ID__"


@pytest.fixture
def make_service(
    make_universe_config: Callable[..., SystemConfig],
    data_repo: FilesystemDataRepository,
    universe_repo: FilesystemUniverseRepository,
    universe_clock: FixedClock,
) -> Callable[..., UniverseSelectionService]:
    """Build a universe service wired to temporary stores and a fake model."""

    def _make(
        *,
        llm_client: object | None = None,
        config: SystemConfig | None = None,
        **config_kwargs: object,
    ) -> UniverseSelectionService:
        return UniverseSelectionService(
            settings=Settings(_env_file=None),
            config=config or make_universe_config(**config_kwargs),
            clock=universe_clock,
            data_repository=data_repo,
            universe_repository=universe_repo,
            llm_client=llm_client,  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture(scope="session")
def shipped_config() -> SystemConfig:
    """The real ``config/`` tree, for tests that must exercise shipped policy."""
    return load_config()
