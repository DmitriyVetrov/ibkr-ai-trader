"""The strategy and contract CLI (brief sections 48, 49).

Every command must be discoverable, must be read-only with respect to the
broker, and must fail *safely* when data is missing — an empty store produces a
clear "nothing has been decided" rather than a traceback or, worse, a
plausible-looking decision.

The tests repoint both services at ``tmp_path`` so no command touches the
repository's own ``data/``. A CLI test that wrote into the real store, or that
reached a real model, would be a bug in the test — and this project has been
bitten by exactly that before.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system.cli import EXIT_ERROR, EXIT_OK, app

from .conftest import FakeLLMClient, UnavailableLLMClient

pytestmark = pytest.mark.unit

runner = CliRunner()


def _text(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except ValueError:  # stderr not separately captured
        stderr = ""
    return stdout + stderr


@pytest.fixture
def cli_services(
    monkeypatch: pytest.MonkeyPatch,
    make_strategy_config,
    data_repo,
    research_repo,
    strategy_repo,
    contract_repo,
    strategy_clock,
) -> Iterator[dict[str, object]]:
    """Point both CLI factories at temporary stores and a fake model."""
    from trading_system.infrastructure.settings import Settings
    from trading_system.strategies.service import ContractSelectionService, StrategyService

    state: dict[str, object] = {"client": None, "config_kwargs": {}}

    def _strategy() -> StrategyService:
        return StrategyService(
            settings=Settings(_env_file=None),
            config=make_strategy_config(**state["config_kwargs"]),  # type: ignore[arg-type]
            clock=strategy_clock,
            data_repository=data_repo,
            research_repository=research_repo,
            strategy_repository=strategy_repo,
            llm_client=state["client"],  # type: ignore[arg-type]
        )

    def _contract() -> ContractSelectionService:
        return ContractSelectionService(
            settings=Settings(_env_file=None),
            config=make_strategy_config(**state["config_kwargs"]),  # type: ignore[arg-type]
            clock=strategy_clock,
            data_repository=data_repo,
            research_repository=research_repo,
            strategy_repository=strategy_repo,
            contract_repository=contract_repo,
        )

    monkeypatch.setattr("trading_system.cli._strategy_service", _strategy)
    monkeypatch.setattr("trading_system.cli._contract_service", _contract)
    yield state


# ---------------------------------------------------------------------------
# 48. Discovery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("group", ["strategy", "contract"])
def test_the_group_is_discoverable(group: str) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == EXIT_OK
    assert group in _text(result)


@pytest.mark.parametrize("command", ["run", "show", "validate", "history"])
def test_each_required_strategy_command_exists(command: str) -> None:
    result = runner.invoke(app, ["strategy", "--help"])

    assert result.exit_code == EXIT_OK
    assert command in _text(result)


@pytest.mark.parametrize("command", ["select", "show", "validate", "history"])
def test_each_required_contract_command_exists(command: str) -> None:
    result = runner.invoke(app, ["contract", "--help"])

    assert result.exit_code == EXIT_OK
    assert command in _text(result)


@pytest.mark.parametrize("group", ["strategy", "contract"])
def test_the_group_declares_it_submits_no_orders(group: str) -> None:
    text = _text(runner.invoke(app, [group, "--help"]))

    assert "read-only" in text
    assert "zero orders" in text


def test_the_contract_group_declares_that_no_model_is_involved() -> None:
    assert "Deterministic" in _text(runner.invoke(app, ["contract", "--help"]))


# ---------------------------------------------------------------------------
# Nothing decided yet
# ---------------------------------------------------------------------------
def test_strategy_show_reports_an_empty_store(cli_services) -> None:
    result = runner.invoke(app, ["strategy", "show"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


def test_contract_show_reports_an_empty_store(cli_services) -> None:
    result = runner.invoke(app, ["contract", "show"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


def test_a_strategy_run_without_research_fails_loudly(cli_services) -> None:
    """It must not produce a plausible-looking empty decision."""
    result = runner.invoke(app, ["strategy", "run", "--dry-run"])

    assert result.exit_code == EXIT_ERROR
    text = " ".join(_text(result).split())
    assert "NO_RESEARCH" in text
    assert "no downstream stage may consume this run" in text


def test_a_contract_select_without_a_strategy_run_fails_loudly(cli_services) -> None:
    result = runner.invoke(app, ["contract", "select", "--dry-run"])

    assert result.exit_code == EXIT_ERROR
    text = " ".join(_text(result).split())
    assert "no strategy run exists yet" in text


# ---------------------------------------------------------------------------
# 49. Dry run
# ---------------------------------------------------------------------------
def test_a_strategy_dry_run_persists_nothing(
    cli_services, make_report, store_research, researchable, decision_text, strategy_repo
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    cli_services["client"] = FakeLLMClient(decision_text)

    result = runner.invoke(app, ["strategy", "run", "--dry-run"])

    assert result.exit_code == EXIT_OK, _text(result)
    assert "DRY RUN" in _text(result)
    assert strategy_repo.history() == []


def test_a_strategy_run_reports_zero_orders(
    cli_services, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    cli_services["client"] = FakeLLMClient(decision_text)

    text = _text(runner.invoke(app, ["strategy", "run", "--dry-run"]))

    assert "Orders submitted: 0" in text
    assert "A strategy decision is not an order" in text


# ---------------------------------------------------------------------------
# The whole chain: research -> strategy -> contract
# ---------------------------------------------------------------------------
@pytest.fixture
def completed_chain(cli_services, make_report, store_research, tradeable, decision_text):
    tradeable("NVDA")
    store_research([make_report()])
    cli_services["client"] = FakeLLMClient(decision_text)

    strategy = runner.invoke(app, ["strategy", "run"])
    assert strategy.exit_code == EXIT_OK, _text(strategy)
    contract = runner.invoke(app, ["contract", "select"])
    assert contract.exit_code == EXIT_OK, _text(contract)
    return cli_services


def test_the_stored_strategy_run_can_be_shown(completed_chain) -> None:
    text = _text(runner.invoke(app, ["strategy", "show"]))

    assert "NVDA" in text
    assert "LONG_CALL" in text
    assert "RATIONALE" in text


def test_a_strategy_decision_can_be_narrowed_to_one_symbol(completed_chain) -> None:
    text = _text(runner.invoke(app, ["strategy", "show", "--symbol", "NVDA"]))

    assert "hypothesis  : B" in text
    assert "eligible" in text


def test_the_stored_contract_selection_can_be_shown(completed_chain) -> None:
    text = _text(runner.invoke(app, ["contract", "show"]))

    assert "NVDA" in text
    assert "LEGS" in text
    assert "contract id" in text
    assert "trading class" in text


def test_the_contract_selection_explains_its_rejections(completed_chain) -> None:
    text = _text(runner.invoke(app, ["contract", "show", "--symbol", "NVDA"]))

    assert "REJECTED CANDIDATES" in text
    assert "SELECTION REASONING" in text


def test_the_contract_run_reports_zero_orders(completed_chain) -> None:
    text = _text(runner.invoke(app, ["contract", "show"]))

    assert "Orders submitted: 0" in text


def test_strategy_history_lists_the_run(completed_chain) -> None:
    text = _text(runner.invoke(app, ["strategy", "history"]))

    assert "Strategy runs" in text
    assert "Nothing here is ever rewritten" in text


def test_contract_history_lists_the_run(completed_chain) -> None:
    text = _text(runner.invoke(app, ["contract", "history"]))

    assert "Contract runs" in text


def test_history_can_be_narrowed_to_one_symbol(completed_chain) -> None:
    assert "Strategy history" in _text(
        runner.invoke(app, ["strategy", "history", "--symbol", "NVDA"])
    )
    assert "Contract history" in _text(
        runner.invoke(app, ["contract", "history", "--symbol", "NVDA"])
    )


def test_the_diagnostics_inspect_the_stored_decision(completed_chain) -> None:
    strategy = _text(runner.invoke(app, ["test", "strategy-selection", "--ticker", "NVDA"]))
    contract = _text(runner.invoke(app, ["test", "contract-selection", "--ticker", "NVDA"]))

    assert "LONG_CALL" in strategy
    assert "Orders submitted: 0" in strategy
    assert "LEGS" in contract
    assert "Orders submitted: 0" in contract


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_strategy_validate_prints_the_registry_and_the_mapping(cli_services) -> None:
    result = runner.invoke(app, ["strategy", "validate"])

    text = _text(result)
    assert "STRATEGY CONFIGURATION" in text
    assert "Strategy registry" in text
    assert "Hypothesis mapping" in text
    assert "NO_TRADE" in text, "hypothesis E maps to no strategy"
    assert "fail closed" in text


def test_strategy_validate_re_checks_a_stored_run(completed_chain, strategy_repo) -> None:
    run_id = strategy_repo.history()[0].run_id

    result = runner.invoke(app, ["strategy", "validate", "--run-id", run_id])

    text = _text(result)
    assert result.exit_code == EXIT_OK, text
    assert "PASS" in text
    assert "stored-run invariants" in text


def test_contract_validate_prints_the_deterministic_policy(cli_services) -> None:
    result = runner.invoke(app, ["contract", "validate"])

    text = _text(result)
    assert "CONTRACT SELECTION POLICY" in text
    assert "Model involved : none" in text
    assert "Underlying liquidity is never accepted" in text


def test_contract_validate_re_checks_a_stored_run(completed_chain, contract_repo) -> None:
    run_id = contract_repo.history()[0].run_id

    result = runner.invoke(app, ["contract", "validate", "--run-id", run_id])

    text = _text(result)
    assert result.exit_code == EXIT_OK, text
    assert "PASS" in text


def test_validate_reports_an_unknown_run(cli_services) -> None:
    for group in ("strategy", "contract"):
        result = runner.invoke(app, [group, "validate", "--run-id", "no-such-run"])
        assert result.exit_code == EXIT_OK
        assert "UNAVAILABLE" in _text(result)


# ---------------------------------------------------------------------------
# Failures stay honest
# ---------------------------------------------------------------------------
def test_an_unreachable_model_fails_the_command_without_a_decision(
    cli_services, make_report, store_research, researchable
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    cli_services["client"] = UnavailableLLMClient()

    result = runner.invoke(app, ["strategy", "run", "--dry-run"])

    text = _text(result)
    assert result.exit_code == EXIT_ERROR
    assert "AI_UNAVAILABLE" in text


def test_a_symbol_research_did_not_cover_is_refused(
    cli_services, make_report, store_research, researchable, decision_text
) -> None:
    researchable("NVDA")
    store_research([make_report()])
    cli_services["client"] = FakeLLMClient(decision_text)

    result = runner.invoke(app, ["strategy", "run", "--dry-run", "--symbol", "TSLA"])

    assert result.exit_code == EXIT_ERROR
    assert "cannot extend it" in " ".join(_text(result).split())


def test_a_contract_selection_with_no_option_data_says_so(
    cli_services, make_report, store_research, researchable, decision_text
) -> None:
    """The chain is visible but no quotes were collected — the honest outcome."""
    researchable("NVDA")
    store_research([make_report()])
    cli_services["client"] = FakeLLMClient(decision_text)
    assert runner.invoke(app, ["strategy", "run"]).exit_code == EXIT_OK

    result = runner.invoke(app, ["contract", "select"])

    text = " ".join(_text(result).split())
    assert result.exit_code == EXIT_ERROR
    assert "REQUIRED_DATA_UNAVAILABLE" in text
    assert "No contract is a valid outcome" in text


def test_an_invalid_as_of_is_rejected(cli_services) -> None:
    result = runner.invoke(app, ["strategy", "run", "--as-of", "not-a-date"])

    assert result.exit_code == EXIT_ERROR
    assert "ISO-8601" in _text(result)


# ---------------------------------------------------------------------------
# No command touches the repository's own data/
# ---------------------------------------------------------------------------
def test_no_run_is_left_in_the_repository(repo_root: Path, completed_chain) -> None:
    """A CLI test that wrote into the real store would be a bug in the test."""
    assert not (repo_root / "data" / "strategy" / "history.jsonl").exists()
    assert not (repo_root / "data" / "contracts" / "history.jsonl").exists()
