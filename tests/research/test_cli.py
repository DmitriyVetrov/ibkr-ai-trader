"""The research CLI (brief sections 43, 44, 62).

Every command must be discoverable, must be read-only with respect to the
broker, and must fail *safely* when data is missing — an empty store produces a
clear "nothing has been researched" rather than a traceback or, worse, a
plausible-looking outlook.

The tests repoint the service at ``tmp_path`` so no command touches the
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

from .conftest import RESEARCH_NOW, FakeLLMClient, UnavailableLLMClient

pytestmark = pytest.mark.unit

runner = CliRunner()

AS_OF = RESEARCH_NOW.isoformat()


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
    make_research_config,
    data_repo,
    universe_repo,
    research_repo,
    research_clock,
) -> Iterator[dict[str, object]]:
    """Point the CLI at temporary stores and a fake model.

    The CLI builds its own service, so the seam is the factory rather than the
    service object. Everything downstream of it is the real code path.
    """
    from trading_system.infrastructure.settings import Settings
    from trading_system.research.service import ResearchService

    state: dict[str, object] = {"client": None, "config_kwargs": {}}

    def _factory() -> ResearchService:
        return ResearchService(
            settings=Settings(_env_file=None),
            config=make_research_config(**state["config_kwargs"]),  # type: ignore[arg-type]
            clock=research_clock,
            data_repository=data_repo,
            universe_repository=universe_repo,
            research_repository=research_repo,
            llm_client=state["client"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr("trading_system.cli._research_service", _factory)
    yield state


# ---------------------------------------------------------------------------
# 43. Discovery
# ---------------------------------------------------------------------------
def test_the_research_group_is_discoverable() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == EXIT_OK
    assert "research" in _text(result)


@pytest.mark.parametrize("command", ["run", "show", "explain", "history", "validate"])
def test_each_required_command_exists(command: str) -> None:
    result = runner.invoke(app, ["research", "--help"])

    assert result.exit_code == EXIT_OK
    assert command in _text(result)


def test_the_group_declares_it_submits_no_orders() -> None:
    text = _text(runner.invoke(app, ["research", "--help"]))

    assert "read-only" in text
    assert "zero orders" in text


def test_run_research_is_a_scheduled_job() -> None:
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == EXIT_OK
    assert "research" in _text(result)


# ---------------------------------------------------------------------------
# Nothing collected yet
# ---------------------------------------------------------------------------
def test_show_reports_that_nothing_has_been_researched(cli_service) -> None:
    result = runner.invoke(app, ["research", "show"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)
    assert "research run" in _text(result)


def test_history_reports_an_empty_store(cli_service) -> None:
    result = runner.invoke(app, ["research", "history"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


def test_a_run_without_a_universe_fails_loudly(cli_service) -> None:
    """It must not produce a plausible-looking empty outlook."""
    result = runner.invoke(app, ["research", "run", "--dry-run", "--as-of", AS_OF])

    assert result.exit_code == EXIT_ERROR
    # Rich wraps at the terminal width, so the assertion is on words that
    # survive a line break rather than on a whole sentence.
    text = " ".join(_text(result).split())
    assert "NO_DATA" in text
    assert "no downstream stage may consume this run" in text


# ---------------------------------------------------------------------------
# 44. Dry run
# ---------------------------------------------------------------------------
def test_a_dry_run_persists_nothing(
    cli_service, store_universe, researchable_symbol, outlook_text, research_repo
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = FakeLLMClient(outlook_text)

    result = runner.invoke(app, ["research", "run", "--dry-run", "--as-of", AS_OF])

    assert result.exit_code == EXIT_OK
    assert "DRY RUN" in _text(result)
    assert research_repo.history() == []


def test_a_dry_run_shows_the_inputs_and_the_evidence(
    cli_service, store_universe, researchable_symbol, outlook_text
) -> None:
    """Brief section 44: the inputs and the evidence must be inspectable."""
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = FakeLLMClient(outlook_text)

    text = _text(runner.invoke(app, ["research", "run", "--dry-run", "--as-of", AS_OF]))

    assert "RESEARCH INPUTS" in text
    assert "EVIDENCE" in text
    assert "snapshots=" in text


def test_a_run_reports_zero_orders(
    cli_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = FakeLLMClient(outlook_text)

    text = _text(runner.invoke(app, ["research", "run", "--dry-run", "--as-of", AS_OF]))

    assert "Orders submitted: 0" in text


# ---------------------------------------------------------------------------
# A real run, then inspection
# ---------------------------------------------------------------------------
@pytest.fixture
def completed_run(cli_service, store_universe, researchable_symbol, outlook_text):
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = FakeLLMClient(outlook_text)
    result = runner.invoke(app, ["research", "run", "--as-of", AS_OF])
    assert result.exit_code == EXIT_OK, _text(result)
    return cli_service


def test_a_stored_run_can_be_shown(completed_run) -> None:
    text = _text(runner.invoke(app, ["research", "show"]))

    assert "NVDA" in text
    assert "hypothesis  : B" in text
    assert "confidence" in text


def test_show_can_be_narrowed_to_one_symbol(completed_run) -> None:
    text = _text(runner.invoke(app, ["research", "show", "--symbol", "NVDA"]))

    assert "NVDA" in text
    assert "THESIS" in text


def test_show_says_so_when_a_symbol_was_not_researched(completed_run) -> None:
    result = runner.invoke(app, ["research", "show", "--symbol", "AAPL"])

    assert result.exit_code == EXIT_OK
    assert "was not researched" in _text(result)


def test_explain_traces_a_conclusion_back_to_its_evidence(completed_run) -> None:
    """Brief section 41: every conclusion must be traceable."""
    text = _text(runner.invoke(app, ["research", "explain", "--symbol", "NVDA"]))

    assert "EVIDENCE" in text
    assert "source:" in text
    assert "snapshot" in text
    assert "INVALIDATION CONDITIONS" in text


def test_explain_shows_the_model_and_prompt_that_produced_it(completed_run) -> None:
    text = _text(runner.invoke(app, ["research", "explain", "--symbol", "NVDA"]))

    assert "fake-model-1" in text
    assert "test-1.0.0" in text


def test_history_lists_the_run(completed_run) -> None:
    text = _text(runner.invoke(app, ["research", "history"]))

    assert "Research runs" in text
    assert "Nothing here is ever rewritten" in text


def test_history_can_be_narrowed_to_one_symbol(completed_run) -> None:
    text = _text(runner.invoke(app, ["research", "history", "--symbol", "NVDA"]))

    assert "NVDA" in text
    assert "Research history" in text


def test_history_for_an_unresearched_symbol_says_so(completed_run) -> None:
    result = runner.invoke(app, ["research", "history", "--symbol", "AAPL"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_reports_the_configuration(cli_service, store_universe) -> None:
    store_universe(["NVDA"])

    result = runner.invoke(app, ["research", "validate"])

    text = _text(result)
    assert "RESEARCH CONFIGURATION" in text
    assert "Horizon        : 14-31 days" in text
    assert "fail closed" in text
    assert "Stored research data" in text


def test_validate_says_when_no_universe_exists(cli_service) -> None:
    result = runner.invoke(app, ["research", "validate"])

    assert result.exit_code == EXIT_OK
    assert "No universe has been selected yet" in _text(result)


def test_validate_re_checks_a_stored_run(completed_run, research_repo) -> None:
    run_id = research_repo.history()[0].run_id

    result = runner.invoke(app, ["research", "validate", "--run-id", run_id])

    text = _text(result)
    assert result.exit_code == EXIT_OK, text
    assert "PASS" in text
    assert "stored-run invariants" in text


def test_validate_reports_an_unknown_run(cli_service) -> None:
    result = runner.invoke(app, ["research", "validate", "--run-id", "no-such-run"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


# ---------------------------------------------------------------------------
# Failures stay honest
# ---------------------------------------------------------------------------
def test_an_unreachable_model_fails_the_command_without_an_outlook(
    cli_service, store_universe, researchable_symbol
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = UnavailableLLMClient()

    result = runner.invoke(app, ["research", "run", "--dry-run", "--as-of", AS_OF])

    text = _text(result)
    assert result.exit_code == EXIT_ERROR
    assert "AI_UNAVAILABLE" in text
    assert "no outlook" in text


def test_an_unknown_symbol_is_refused_rather_than_researched(
    cli_service, store_universe, researchable_symbol, outlook_text
) -> None:
    store_universe(["NVDA"])
    researchable_symbol("NVDA")
    cli_service["client"] = FakeLLMClient(outlook_text)

    result = runner.invoke(
        app, ["research", "run", "--dry-run", "--as-of", AS_OF, "--symbol", "TSLA"]
    )

    assert result.exit_code == EXIT_ERROR
    assert "cannot extend it" in _text(result)


def test_an_invalid_as_of_is_rejected(cli_service) -> None:
    result = runner.invoke(app, ["research", "run", "--as-of", "not-a-date"])

    assert result.exit_code == EXIT_ERROR
    assert "ISO-8601" in _text(result)


def test_a_naive_as_of_is_rejected(cli_service) -> None:
    result = runner.invoke(app, ["research", "run", "--as-of", "2026-08-10T14:30:00"])

    assert result.exit_code == EXIT_ERROR
    assert "timezone" in _text(result)


# ---------------------------------------------------------------------------
# No command touches the repository's own data/
# ---------------------------------------------------------------------------
def test_no_research_run_is_left_in_the_repository(repo_root: Path, completed_run) -> None:
    """A CLI test that wrote into the real store would be a bug in the test."""
    assert not (repo_root / "data" / "research" / "history.jsonl").exists()
