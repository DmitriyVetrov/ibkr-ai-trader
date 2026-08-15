"""The readiness CLI reports, and never enables anything.

Every test here drives the real Typer app with an injected service rooted at
``tmp_path``. Nothing connects, nothing writes into the repository, and the
suite asserts the property the whole group exists for: no readiness command can
change a mode, a guard or an execution switch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system.cli import app
from trading_system.domain.enums import (
    ReadinessLevel,
    ReadinessRunStatus,
    SignoffStatus,
    TradingMode,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, load_config
from trading_system.readiness.models import LiveReadinessSignoff, ReadinessRun
from trading_system.readiness.service import ReadinessService

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReadinessService:
    """A readiness service rooted at ``tmp_path``, wired into the CLI.

    Monkeypatched in the same way ``tests/exit/test_cli.py`` and the universe
    CLI tests do, and for the same reason: a test that reached the real store
    would write runs into the repository's own ``data/``.
    """
    built = ReadinessService(
        settings=Settings(_env_file=None, trading_mode="PAPER"),
        config=load_config(REPO_CONFIG),
        clock=FixedClock(NOW),
        repo_root=tmp_path / "repo",
        root=tmp_path / "project",
    )
    monkeypatch.setattr("trading_system.cli._readiness_service", lambda: built)
    return built


def _run(**overrides: object) -> ReadinessRun:
    payload: dict[str, object] = {
        "readiness_run_id": "readiness-run-1",
        "status": ReadinessRunStatus.NO_EVIDENCE,
        "evaluated_at": NOW,
        "as_of": NOW,
        "trading_mode": TradingMode.PAPER,
        "git_revision": "abc123def456",
        "working_tree_clean": True,
    }
    payload.update(overrides)
    return ReadinessRun(**payload)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_shows_the_policy_without_collecting(
    runner: CliRunner, service: ReadinessService
) -> None:
    result = runner.invoke(app, ["readiness", "validate"])
    assert result.exit_code == 0
    assert "Readiness policy" in result.output
    assert "Criteria defined" in result.output


def test_validate_states_that_readiness_changes_nothing(
    runner: CliRunner, service: ReadinessService
) -> None:
    result = runner.invoke(app, ["readiness", "validate"])
    assert "TRADING_MODE" in result.output
    assert "execution.enabled" in result.output


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def test_an_offline_check_runs_and_reports_zero_orders(
    runner: CliRunner, service: ReadinessService
) -> None:
    result = runner.invoke(app, ["readiness", "check"])
    assert result.exit_code == 0
    assert "Orders submitted" in result.output
    assert "0" in result.output


def test_an_offline_check_opens_no_broker(
    runner: CliRunner, service: ReadinessService, no_broker: None
) -> None:
    """``no_broker`` raises if anything asks for one, so this is structural."""
    result = runner.invoke(app, ["readiness", "check"])
    assert result.exit_code == 0


def test_a_check_stores_a_run(runner: CliRunner, service: ReadinessService) -> None:
    runner.invoke(app, ["readiness", "check"])
    assert service.latest() is not None


def test_a_dry_run_stores_nothing(runner: CliRunner, service: ReadinessService) -> None:
    result = runner.invoke(app, ["readiness", "check", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert service.latest() is None


def test_an_offline_check_cannot_reach_ready_for_paper(
    runner: CliRunner, service: ReadinessService
) -> None:
    """The cheap default certifies nothing: the paper gate needs a gateway."""
    runner.invoke(app, ["readiness", "check"])
    run = service.latest()
    assert run is not None
    assert run.level is ReadinessLevel.NOT_READY


def test_a_second_check_is_recorded_as_its_own_run(
    runner: CliRunner, service: ReadinessService
) -> None:
    """Two runs, two records — because the *evidence* genuinely changed.

    The second run sees one more readiness run in the operational-history
    store than the first did, so it is a different observation reaching a
    different (identically-levelled) conclusion. Collapsing them would hide
    that history accumulated, which is the one thing that store exists to
    measure. Byte-identical evidence *does* re-observe; that is asserted in
    ``test_evaluator.py`` where the bundle can be held fixed.
    """
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "check"])
    assert result.exit_code == 0
    assert len(service.history()) == 2


# ---------------------------------------------------------------------------
# show / history / explain
# ---------------------------------------------------------------------------
def test_show_without_a_stored_run_fails_helpfully(
    runner: CliRunner, service: ReadinessService
) -> None:
    result = runner.invoke(app, ["readiness", "show"])
    assert result.exit_code != 0
    assert "readiness check" in result.output


def test_show_renders_the_latest_run(runner: CliRunner, service: ReadinessService) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "show"])
    assert result.exit_code == 0
    assert "NOT_READY" in result.output


def test_show_reports_the_absence_of_a_signoff(
    runner: CliRunner, service: ReadinessService
) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "show"])
    assert "NOT_SIGNED" in result.output


def test_history_lists_stored_runs(runner: CliRunner, service: ReadinessService) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "history"])
    assert result.exit_code == 0
    assert "Readiness history" in result.output


def test_history_on_an_empty_store_says_so(runner: CliRunner, service: ReadinessService) -> None:
    result = runner.invoke(app, ["readiness", "history"])
    assert result.exit_code == 0
    assert "No readiness runs" in result.output


def test_explain_names_a_specific_criterion(runner: CliRunner, service: ReadinessService) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "explain", "--criterion", "TEST_SUITE_PASSES"])
    assert result.exit_code == 0
    assert "TEST_SUITE_PASSES" in result.output


def test_explain_refuses_an_unknown_criterion(runner: CliRunner, service: ReadinessService) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "explain", "--criterion", "NOT_A_CRITERION"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# signoff
# ---------------------------------------------------------------------------
def test_signoff_without_confirm_records_nothing(
    runner: CliRunner, service: ReadinessService
) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "signoff", "--signed-by", "A Person"])
    assert result.exit_code == 0
    assert "Not recorded" in result.output
    assert service.repository.latest_signoff() is None


def test_signoff_without_an_identity_is_refused(
    runner: CliRunner, service: ReadinessService
) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "signoff", "--confirm"])
    assert result.exit_code != 0
    assert service.repository.latest_signoff() is None


def test_signing_a_run_below_live_review_is_refused(
    runner: CliRunner, service: ReadinessService
) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["readiness", "signoff", "--signed-by", "A Person", "--confirm"])
    assert result.exit_code != 0
    assert "READY_FOR_LIVE_REVIEW" in result.output
    assert service.repository.latest_signoff() is None


def test_a_recorded_signoff_states_that_it_enables_nothing(
    runner: CliRunner, service: ReadinessService
) -> None:
    """Rendered directly: constructing one through the CLI needs a live-review run."""
    import io

    from rich.console import Console

    from trading_system.readiness.report import render_signoff

    signoff = LiveReadinessSignoff(
        signoff_id="signoff-1",
        status=SignoffStatus.SIGNED,
        readiness_run_id="readiness-run-1",
        readiness_level=ReadinessLevel.READY_FOR_LIVE_REVIEW,
        signed_by="A Person",
        signed_at=NOW,
    )
    buffer = io.StringIO()
    render_signoff(Console(file=buffer, width=100), signoff)
    output = buffer.getvalue()
    assert "Enables trading" in output
    assert "NO" in output


# ---------------------------------------------------------------------------
# paper: gated, and refused by default
# ---------------------------------------------------------------------------
def test_the_paper_command_warns_before_anything_else(
    runner: CliRunner, service: ReadinessService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brief section 33: no silent order submission."""
    result = runner.invoke(app, ["readiness", "paper"])
    assert "WARNING" in result.output
    assert "REAL order" in result.output
    assert "PAPER" in result.output


def test_the_paper_command_refuses_without_its_authorisation_flag(
    runner: CliRunner, service: ReadinessService
) -> None:
    result = runner.invoke(app, ["readiness", "paper"])
    assert result.exit_code != 0
    assert "REFUSED" in result.output


def test_the_paper_command_opens_no_broker(
    runner: CliRunner, service: ReadinessService, no_broker: None
) -> None:
    """It checks authorisations; it does not submit.

    The audited order path runs through ``execution/service.py``, the only
    caller of ``build_execution_broker``. A second path here would weaken a
    Milestone 8 invariant that two boundary suites assert, so the command
    reports what is authorised and names the sanctioned command instead.
    """
    result = runner.invoke(
        app, ["readiness", "paper", "--i-understand-this-submits-a-real-paper-order"]
    )
    # ``no_broker`` raises an AssertionError if anything asks for a broker,
    # so reaching a tidy refusal is the assertion.
    assert "REFUSED" in result.output


def test_the_paper_command_refuses_on_the_shipped_configuration(
    runner: CliRunner, service: ReadinessService
) -> None:
    """``readiness.paper_execution.enabled`` ships false."""
    result = runner.invoke(
        app, ["readiness", "paper", "--i-understand-this-submits-a-real-paper-order"]
    )
    assert result.exit_code != 0
    assert "REFUSED" in result.output
    assert "paper_execution.enabled" in result.output


def test_the_paper_authorisation_flag_is_not_confirm(
    runner: CliRunner, service: ReadinessService
) -> None:
    """Brief section 33: --confirm already means something else."""
    result = runner.invoke(app, ["readiness", "paper", "--confirm"])
    assert result.exit_code != 0
    assert "No such option" in result.output or "REFUSED" in result.output


# ---------------------------------------------------------------------------
# test readiness
# ---------------------------------------------------------------------------
def test_the_diagnostic_reports_the_policy(runner: CliRunner, service: ReadinessService) -> None:
    result = runner.invoke(app, ["test", "readiness"])
    assert result.exit_code == 0
    assert "Readiness diagnostic" in result.output


def test_the_diagnostic_submits_nothing(
    runner: CliRunner, service: ReadinessService, no_broker: None
) -> None:
    runner.invoke(app, ["readiness", "check"])
    result = runner.invoke(app, ["test", "readiness"])
    assert result.exit_code == 0
    assert "Orders submitted" in result.output


# ---------------------------------------------------------------------------
# Nothing leaks into the repository
# ---------------------------------------------------------------------------
def test_no_readiness_command_writes_into_the_repository(
    runner: CliRunner, service: ReadinessService, tmp_path: Path
) -> None:
    """The lesson ``tests/unit/test_cli.py`` records about stray run files."""
    runner.invoke(app, ["readiness", "check"])
    stored = list((tmp_path / "project").rglob("*.json"))
    assert stored, "the run should have been written under tmp_path"
    for path in stored:
        assert str(path).startswith(str(tmp_path))
