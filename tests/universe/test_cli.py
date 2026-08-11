"""The universe CLI (brief sections 28-29, 37).

Every command here must be discoverable, must be read-only with respect to the
broker, and must fail *safely* when data is missing — an empty store produces a
clear "nothing has been collected" rather than a traceback or, worse, a
plausible-looking universe.

The tests repoint the service at ``tmp_path`` so no command touches the
repository's own ``data/``. A CLI test that wrote into the real store, or that
reached the real gateway, would be a bug in the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system.cli import EXIT_ERROR, EXIT_OK, app

from .conftest import UNIVERSE_NOW, FakeLLMClient

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
def cli_service(
    monkeypatch: pytest.MonkeyPatch,
    make_universe_config,
    data_repo,
    universe_repo,
    universe_clock,
) -> Iterator[dict[str, object]]:
    """Point the CLI at temporary stores and a fake model.

    The CLI builds its own service, so the seam is the factory rather than the
    service object. Everything downstream of it is the real code path.
    """
    from trading_system.infrastructure.settings import Settings
    from trading_system.universe.service import UniverseSelectionService

    state: dict[str, object] = {"client": None, "config_kwargs": {}}

    def _factory() -> UniverseSelectionService:
        return UniverseSelectionService(
            settings=Settings(_env_file=None),
            config=make_universe_config(**state["config_kwargs"]),  # type: ignore[arg-type]
            clock=universe_clock,
            data_repository=data_repo,
            universe_repository=universe_repo,
            llm_client=state["client"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr("trading_system.cli._universe_service", _factory)
    yield state


# ---------------------------------------------------------------------------
# 28. Discovery
# ---------------------------------------------------------------------------
def test_the_universe_group_is_discoverable() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == EXIT_OK
    assert "universe" in _text(result)


@pytest.mark.parametrize("command", ["show", "validate", "run", "history", "explain"])
def test_each_required_command_exists(command: str) -> None:
    result = runner.invoke(app, ["universe", "--help"])

    assert result.exit_code == EXIT_OK
    assert command in _text(result)


def test_the_group_declares_it_submits_no_orders() -> None:
    text = _text(runner.invoke(app, ["universe", "--help"]))

    assert "read-only" in text
    assert "zero orders" in text


@pytest.mark.parametrize("command", ["show", "validate", "run", "history", "explain"])
def test_each_command_help_exits_cleanly(command: str) -> None:
    assert runner.invoke(app, ["universe", command, "--help"]).exit_code == EXIT_OK


def test_run_universe_is_still_a_scheduled_job() -> None:
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == EXIT_OK
    assert "universe" in _text(result)


# ---------------------------------------------------------------------------
# 37. Safe failure when data is unavailable
# ---------------------------------------------------------------------------
def test_show_reports_that_no_universe_exists_yet(cli_service) -> None:
    result = runner.invoke(app, ["universe", "show"])

    assert result.exit_code == EXIT_OK
    assert "no universe has been selected yet" in _text(result)


def test_history_reports_an_empty_history_cleanly(cli_service) -> None:
    result = runner.invoke(app, ["universe", "history"])

    assert result.exit_code == EXIT_OK
    assert "no universe runs" in _text(result)


def test_explain_reports_an_unknown_run_id_cleanly(cli_service) -> None:
    result = runner.invoke(app, ["universe", "explain", "--run-id", "nope"])

    assert result.exit_code == EXIT_OK
    assert "no universe run with id" in _text(result)


def test_validate_reports_which_symbols_have_no_data(cli_service) -> None:
    result = runner.invoke(app, ["universe", "validate"])

    assert result.exit_code == EXIT_OK
    text = _text(result)
    assert "have no stored quote" in text
    assert "DATA_UNAVAILABLE" in text


def test_a_run_with_no_data_fails_rather_than_inventing_a_universe(cli_service) -> None:
    result = runner.invoke(app, ["universe", "run"])

    assert result.exit_code == EXIT_ERROR
    assert "DATA_UNAVAILABLE" in _text(result)


# ---------------------------------------------------------------------------
# The working path
# ---------------------------------------------------------------------------
def test_a_run_produces_a_report_and_stores_it(
    cli_service, optionable_symbols, ranking_text, universe_repo
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    result = runner.invoke(app, ["universe", "run", "--as-of", UNIVERSE_NOW.isoformat()])

    text = _text(result)
    assert result.exit_code == EXIT_OK
    assert "Universe Run:" in text
    assert "SELECTED" in text and "SPY" in text
    assert "Orders submitted: 0" in text
    assert len(universe_repo.history()) == 1


def test_the_report_shows_provenance_for_every_selected_asset(
    cli_service, optionable_symbols, ranking_text
) -> None:
    """Brief section 42: a selected asset must show where its evidence came from."""
    symbols = optionable_symbols(["SPY"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    text = _text(runner.invoke(app, ["universe", "run", "--as-of", UNIVERSE_NOW.isoformat()]))

    assert "provenance" in text
    assert "snapshots" in text
    assert "IBKR" in text


def test_the_report_names_the_rejection_reason(
    cli_service, optionable_symbols, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY", "QQQ"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    text = _text(runner.invoke(app, ["universe", "run", "--as-of", UNIVERSE_NOW.isoformat()]))

    assert "REJECTED" in text
    assert "NOT_SELECTED_BY_RANKING" in text


# ---------------------------------------------------------------------------
# 29. Dry run
# ---------------------------------------------------------------------------
def test_a_dry_run_does_not_mutate_history(
    cli_service, optionable_symbols, ranking_text, universe_repo
) -> None:
    symbols = optionable_symbols(["SPY"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    result = runner.invoke(
        app, ["universe", "run", "--dry-run", "--as-of", UNIVERSE_NOW.isoformat()]
    )

    assert result.exit_code == EXIT_OK
    assert "DRY RUN" in _text(result)
    assert "Nothing was persisted" in _text(result)
    assert universe_repo.history() == []


def test_a_dry_run_still_shows_the_proposed_result(
    cli_service, optionable_symbols, ranking_text
) -> None:
    symbols = optionable_symbols(["SPY"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    text = _text(
        runner.invoke(app, ["universe", "run", "--dry-run", "--as-of", UNIVERSE_NOW.isoformat()])
    )

    assert "SPY" in text
    assert "SELECTED" in text


def test_the_scheduled_job_supports_a_dry_run_too(
    cli_service, optionable_symbols, ranking_text, universe_repo
) -> None:
    symbols = optionable_symbols(["SPY"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    result = runner.invoke(
        app, ["run", "universe", "--dry-run", "--as-of", UNIVERSE_NOW.isoformat()]
    )

    assert result.exit_code == EXIT_OK
    assert universe_repo.history() == []


# ---------------------------------------------------------------------------
# show / history / explain against a real run
# ---------------------------------------------------------------------------
@pytest.fixture
def stored_run(cli_service, optionable_symbols, ranking_text) -> str:
    symbols = optionable_symbols(["SPY", "QQQ"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"], not_selected=["QQQ"]))
    cli_service["config_kwargs"] = {"symbols": symbols}
    runner.invoke(app, ["universe", "run", "--as-of", UNIVERSE_NOW.isoformat()])
    from trading_system.cli import _universe_service

    history = _universe_service().history()
    return history[0].run_id


def test_show_renders_the_latest_universe(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "show"])

    assert result.exit_code == EXIT_OK
    assert stored_run in _text(result)


def test_show_can_render_a_named_run(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "show", "--run-id", stored_run])

    assert result.exit_code == EXIT_OK
    assert stored_run in _text(result)


def test_history_lists_the_stored_run(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "history"])

    text = _text(result)
    assert result.exit_code == EXIT_OK
    assert "SUCCESS" in text
    assert "ever rewritten" in text, "history states its append-only nature"


def test_explain_describes_a_selected_asset(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "explain", "--run-id", stored_run, "--symbol", "SPY"])

    text = _text(result)
    assert result.exit_code == EXIT_OK
    assert "was SELECTED" in text
    assert "reasons" in text
    assert "snapshots" in text


def test_explain_describes_a_rejected_asset(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "explain", "--run-id", stored_run, "--symbol", "QQQ"])

    text = _text(result)
    assert result.exit_code == EXIT_OK
    assert "was REJECTED" in text
    assert "NOT_SELECTED_BY_RANKING" in text


def test_explain_reports_an_asset_that_was_never_considered(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "explain", "--run-id", stored_run, "--symbol", "TSLA"])

    assert result.exit_code == EXIT_OK
    assert "not considered" in _text(result)


def test_explain_without_a_symbol_renders_the_whole_report(cli_service, stored_run) -> None:
    result = runner.invoke(app, ["universe", "explain", "--run-id", stored_run])

    assert result.exit_code == EXIT_OK
    assert "Universe Run:" in _text(result)


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
def test_a_naive_as_of_is_refused(cli_service) -> None:
    result = runner.invoke(app, ["universe", "run", "--as-of", "2026-08-10T14:30:00"])

    assert result.exit_code == EXIT_ERROR
    assert "timezone" in _text(result)


def test_a_malformed_as_of_is_refused(cli_service) -> None:
    result = runner.invoke(app, ["universe", "run", "--as-of", "yesterday"])

    assert result.exit_code == EXIT_ERROR
    assert "ISO-8601" in _text(result)


def test_no_cli_test_writes_into_the_repositorys_own_data(
    cli_service, optionable_symbols, ranking_text, universe_repo, repo_root: Path
) -> None:
    """A test that touched the real store would be a bug in the test."""
    symbols = optionable_symbols(["SPY"])
    cli_service["client"] = FakeLLMClient(ranking_text(["SPY"]))
    cli_service["config_kwargs"] = {"symbols": symbols}

    runner.invoke(app, ["universe", "run", "--as-of", UNIVERSE_NOW.isoformat()])

    assert not (repo_root / "data" / "universe" / "history.jsonl").exists()
    assert universe_repo.history(), "and the temporary store did receive it"
