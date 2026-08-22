"""Universe configuration (brief sections 8, 10, 27).

Two things are being protected. First, that thresholds live in
``config/universe.yaml`` rather than in business logic — a change to what the
system considers research-ready must show up in a diff. Second, that the
configuration cannot express something incoherent: a maximum selection larger
than the candidate pool, a file source with no file, a static source with no
symbols.

The shipped configuration is tested directly, not a fixture of it. A rule that
only holds under test-only values has not been tested.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.domain.enums import SecurityType, UniverseSourceKind
from trading_system.infrastructure.settings import (
    AiRankingConfig,
    ConfigError,
    OptionabilityPolicy,
    UniverseFilterConfig,
    UniverseSourceConfig,
    load_config,
)
from trading_system.universe.source import (
    IMPLEMENTED_KINDS,
    UniverseSourceError,
    load_symbols,
    raw_symbols,
    source_reference,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The shipped configuration
# ---------------------------------------------------------------------------
def test_the_shipped_configuration_loads(shipped_config) -> None:
    universe = shipped_config.universe

    assert universe.config_version
    assert universe.source.kind in IMPLEMENTED_KINDS
    assert universe.filters.max_selected_assets > 0


def test_the_shipped_universe_is_liquid_and_fits_the_candidate_cap(shipped_config) -> None:
    """Brief section 9: start with liquid, optionable US underlyings.

    The upper bound is ``max_candidates`` rather than a fixed number, because
    that is the size the pool actually has to respect. A source listing more
    symbols than the agent may be shown is not a wider search: the moment every
    one of them passes the filters, the tail is dropped in the source's own
    order and recorded as ``CANDIDATE_LIMIT_EXCEEDED`` — a cut nobody chose,
    made by list position rather than by any property of the asset.
    """
    symbols = shipped_config.universe.source.symbols
    cap = shipped_config.universe.filters.max_candidates

    assert 5 <= len(symbols) <= cap, "a curated pool, not a discovery sweep"
    assert "SPY" in symbols
    assert len(set(symbols)) == len(symbols)


def test_the_shipped_filters_require_optionability(shipped_config) -> None:
    """The honest default for an options system."""
    assert shipped_config.universe.filters.optionability_policy is OptionabilityPolicy.REQUIRED


def test_the_shipped_configuration_fails_closed_on_ai_failure(shipped_config) -> None:
    """Brief section 24: a deterministic fallback must be an explicit choice."""
    assert shipped_config.universe.ai_ranking.allow_deterministic_fallback is False


def test_the_shipped_configuration_holds_no_secret(repo_root: Path) -> None:
    """Brief section 27: no secret belongs in YAML."""
    text = (repo_root / "config" / "universe.yaml").read_text(encoding="utf-8")

    for marker in ("sk-ant", "api_key:", "apikey", "ANTHROPIC_API_KEY:"):
        assert marker not in text


def test_money_in_the_universe_config_is_an_exact_decimal(shipped_config) -> None:
    """An unquoted 5.00 is a binary float, and is rejected by design."""
    min_price = shipped_config.universe.filters.min_price

    assert isinstance(min_price, Decimal)
    assert min_price == Decimal("5.00")


def test_an_unquoted_money_value_is_rejected(tmp_config_dir: Path) -> None:
    path = tmp_config_dir / "universe.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('min_price: "5.00"', "min_price: 5.00"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="floating point"):
        load_config(tmp_config_dir)


def test_an_unknown_universe_key_fails_loudly(tmp_config_dir: Path) -> None:
    """Config models are extra=forbid, so a typo cannot silently do nothing."""
    path = tmp_config_dir / "universe.yaml"
    path.write_text(path.read_text(encoding="utf-8") + '\nmin_prcie: "5.00"\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(tmp_config_dir)


def test_a_missing_universe_file_is_a_configuration_error(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "universe.yaml").unlink()

    with pytest.raises(ConfigError, match=r"universe\.yaml"):
        load_config(tmp_config_dir)


# ---------------------------------------------------------------------------
# Configuration coherence
# ---------------------------------------------------------------------------
def test_selecting_more_than_the_candidate_pool_is_refused() -> None:
    """The agent cannot select more assets than it is shown."""
    with pytest.raises(ValueError, match="exceeds"):
        UniverseFilterConfig(
            allowed_security_types=[SecurityType.STOCK],
            allowed_currencies=["USD"],
            min_price=Decimal("1"),
            min_average_daily_volume=0,
            max_data_age_seconds=3600,
            max_candidates=5,
            max_selected_assets=10,
        )


def test_a_static_source_without_symbols_is_refused() -> None:
    with pytest.raises(ValueError, match=r"requires 'symbols'"):
        UniverseSourceConfig(kind=UniverseSourceKind.STATIC, name="empty", version="1", symbols=[])


def test_a_file_source_without_a_location_is_refused() -> None:
    with pytest.raises(ValueError, match=r"requires 'location'"):
        UniverseSourceConfig(kind=UniverseSourceKind.FILE, name="f", version="1")


def test_an_unknown_effort_level_is_refused() -> None:
    with pytest.raises(ValueError, match="effort"):
        AiRankingConfig(prompt_version="1", effort="maximum")


# ---------------------------------------------------------------------------
# 8. Source kinds
# ---------------------------------------------------------------------------
def test_a_static_source_resolves_to_its_symbols() -> None:
    config = UniverseSourceConfig(
        kind=UniverseSourceKind.STATIC, name="t", version="1", symbols=["spy", " qqq ", "SPY"]
    )

    assert load_symbols(config) == ["SPY", "QQQ"], "normalised and deduplicated"
    assert raw_symbols(config) == ["SPY", "QQQ", "SPY"], "duplicates preserved for reporting"


def test_a_file_source_reads_a_symbol_list(tmp_path: Path) -> None:
    path = tmp_path / "symbols.txt"
    path.write_text("SPY\n# a comment\nQQQ  # trailing\n\nNVDA\n", encoding="utf-8")
    config = UniverseSourceConfig(
        kind=UniverseSourceKind.FILE, name="f", version="1", location="symbols.txt"
    )

    assert load_symbols(config, root=tmp_path) == ["SPY", "QQQ", "NVDA"]


def test_a_missing_source_file_is_an_error_not_an_empty_universe(tmp_path: Path) -> None:
    """Silence would be indistinguishable from "nothing qualified"."""
    config = UniverseSourceConfig(
        kind=UniverseSourceKind.FILE, name="f", version="1", location="absent.txt"
    )

    with pytest.raises(UniverseSourceError, match="not found"):
        load_symbols(config, root=tmp_path)


@pytest.mark.parametrize(
    "kind",
    [
        UniverseSourceKind.SP500,
        UniverseSourceKind.NASDAQ100,
        UniverseSourceKind.ETF_UNIVERSE,
        UniverseSourceKind.IBKR_DISCOVERED,
    ],
)
def test_an_unimplemented_source_fails_rather_than_approximating(
    kind: UniverseSourceKind,
) -> None:
    """A constituent list written from memory would be invented data."""
    config = UniverseSourceConfig(kind=kind, name="x", version="1", symbols=["SPY"])

    with pytest.raises(UniverseSourceError, match="not implemented"):
        load_symbols(config)


def test_the_index_kinds_say_what_they_would_need() -> None:
    """An error that only says "no" leaves the operator with nowhere to go."""
    config = UniverseSourceConfig(kind=UniverseSourceKind.SP500, name="x", version="1", symbols=[])

    with pytest.raises(UniverseSourceError, match="authoritative source"):
        load_symbols(config)


def test_the_source_reference_is_versioned(shipped_config) -> None:
    """Brief section 8: the source must be explicit and versioned."""
    reference = source_reference(shipped_config.universe.source)

    assert reference.kind is shipped_config.universe.source.kind
    assert reference.name
    assert reference.version


# ---------------------------------------------------------------------------
# 10. Nothing is hard-coded in business logic
# ---------------------------------------------------------------------------
def test_the_filters_read_every_threshold_from_configuration(repo_root: Path) -> None:
    """A threshold in code would not show up in a configuration diff."""
    source = (repo_root / "src" / "trading_system" / "universe" / "filters.py").read_text(
        encoding="utf-8"
    )

    for literal in ("5.00", "1000000", "86400", '"USD"', "min_price =", "min_volume ="):
        assert literal not in source, f"filters.py hard-codes {literal!r}"


def test_the_optionability_policy_is_configurable(shipped_config) -> None:
    for policy in OptionabilityPolicy:
        config = shipped_config.universe.filters.model_copy(update={"optionability_policy": policy})
        assert config.optionability_policy is policy
