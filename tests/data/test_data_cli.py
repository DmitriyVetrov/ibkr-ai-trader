"""The ``data`` command group.

Everything here runs ``--simulated``, so no test reaches a gateway or the
network. Two properties are load-bearing:

* the commands say what the data *is* — REAL, SIMULATED, CACHED, HISTORICAL or
  UNAVAILABLE — not merely whether the command succeeded;
* nothing in the group can reach an order path, and the read-only guarantee is
  asserted rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_system.cli import EXIT_ERROR, EXIT_OK, app

runner = CliRunner()

pytestmark = pytest.mark.unit


def _text(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    try:
        stderr = getattr(result, "stderr", "") or ""
    except ValueError:  # stderr not separately captured
        stderr = ""
    return stdout + stderr


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the data layer at a scratch directory.

    Without this a CLI test would write snapshots into the repository's own
    ``data/`` tree and read whatever a previous run left there.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr("trading_system.data.service.project_root", lambda: root)
    # Rich truncates table cells to the terminal width, and the default 80
    # columns would cut the values these tests assert on.
    monkeypatch.setenv("COLUMNS", "220")
    return root


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    ["providers", "collect", "collect-options", "snapshot", "quality", "history", "status"],
)
def test_every_required_data_command_is_discoverable(command: str) -> None:
    result = runner.invoke(app, ["data", "--help"])

    assert result.exit_code == EXIT_OK
    assert command in _text(result)


def test_the_data_group_help_works() -> None:
    assert runner.invoke(app, ["data", "--help"]).exit_code == EXIT_OK


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def test_providers_lists_registered_providers() -> None:
    result = runner.invoke(app, ["data", "providers", "--simulated"])

    assert result.exit_code == EXIT_OK
    text = _text(result)
    assert "SIMULATOR" in text
    assert "TIER" in text


def test_providers_proves_no_paid_provider_is_configured() -> None:
    result = runner.invoke(app, ["data", "providers", "--simulated"])

    assert "No paid data provider is configured or required." in _text(result)
    assert "PAID" not in _text(result)


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def test_collect_stores_a_snapshot_and_says_it_is_simulated() -> None:
    result = runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK, _text(result)
    text = _text(result)
    assert "SIMULATED" in text
    assert "REAL" not in text
    assert "PASS" in text
    assert "No orders were submitted" in text


def test_collect_writes_into_the_isolated_root(isolated_data_root: Path) -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])

    assert list((isolated_data_root / "data" / "snapshots").rglob("*.json"))
    assert list((isolated_data_root / "data" / "raw").rglob("*.json"))


def test_collect_options_stores_a_chain_and_selects_nothing() -> None:
    result = runner.invoke(app, ["data", "collect-options", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK, _text(result)
    text = _text(result)
    assert "OPTION_CHAIN" in text
    assert "No contract was selected" in text


def test_collect_options_can_also_collect_quotes() -> None:
    result = runner.invoke(
        app, ["data", "collect-options", "--symbol", "SPY", "--simulated", "--quotes"]
    )

    assert result.exit_code == EXIT_OK, _text(result)
    assert "OPTION_QUOTE" in _text(result)


def test_a_second_collection_reports_that_nothing_changed() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK
    assert "SKIPPED_UNCHANGED" in _text(result)
    assert "snapshots_created=0" in _text(result)


# ---------------------------------------------------------------------------
# snapshot / quality / history
# ---------------------------------------------------------------------------
def test_snapshot_reports_unavailable_before_anything_is_collected() -> None:
    """No data is a state to report, not a state to fake."""
    result = runner.invoke(app, ["data", "snapshot", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


def test_snapshot_shows_provenance_and_version_stamps() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(app, ["data", "snapshot", "--symbol", "SPY", "--simulated"])

    text = _text(result)
    assert "Snapshot id" in text
    assert "Provider" in text
    assert "Data origin" in text
    assert "Payload hash" in text
    assert "Schema" in text


def test_snapshot_accepts_a_point_in_time_query() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(
        app,
        [
            "data",
            "snapshot",
            "--symbol",
            "SPY",
            "--simulated",
            "--as-of",
            "2000-01-01T00:00:00+00:00",
        ],
    )

    # Nothing had been retrieved by the year 2000.
    assert "UNAVAILABLE" in _text(result)


def test_a_naive_as_of_is_refused() -> None:
    result = runner.invoke(
        app, ["data", "snapshot", "--symbol", "SPY", "--simulated", "--as-of", "2026-08-10"]
    )

    assert result.exit_code == EXIT_ERROR
    assert "timezone" in _text(result)


def test_an_unknown_data_type_is_refused_with_the_valid_values() -> None:
    result = runner.invoke(
        app, ["data", "snapshot", "--symbol", "SPY", "--simulated", "--type", "NONSENSE"]
    )

    assert result.exit_code == EXIT_ERROR
    assert "MARKET_QUOTE" in _text(result)


def test_quality_reports_every_dimension() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(app, ["data", "quality", "--symbol", "SPY", "--simulated"])

    text = _text(result)
    for dimension in (
        "transport",
        "schema",
        "source",
        "timestamp",
        "freshness",
        "completeness",
        "plausibility",
        "consistency",
        "research_usable",
    ):
        assert dimension in text


def test_quality_without_data_says_so() -> None:
    result = runner.invoke(app, ["data", "quality", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


def test_history_shows_the_append_only_ledger() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(app, ["data", "history", "--symbol", "SPY", "--simulated"])

    text = _text(result)
    assert "SNAPSHOT_CREATED" in text
    assert "SNAPSHOT_REOBSERVED" in text
    assert "append-only" in text


def test_history_without_data_says_so() -> None:
    result = runner.invoke(app, ["data", "history", "--symbol", "SPY", "--simulated"])

    assert result.exit_code == EXIT_OK
    assert "UNAVAILABLE" in _text(result)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_an_empty_store_honestly() -> None:
    result = runner.invoke(app, ["data", "status", "--simulated"])

    assert result.exit_code == EXIT_OK
    text = _text(result)
    assert "No data has been collected yet" in text
    assert "backfilled" in text


def test_status_reports_collected_symbols() -> None:
    runner.invoke(app, ["data", "collect", "--symbol", "SPY", "--simulated"])
    result = runner.invoke(app, ["data", "status", "--simulated"])

    text = _text(result)
    assert "SPY" in text
    assert "MARKET_QUOTE" in text
    assert "NO_GAP" in text


def test_status_lists_the_configured_symbols() -> None:
    result = runner.invoke(app, ["data", "status", "--simulated"])

    assert "Configured symbols" in _text(result)


# ---------------------------------------------------------------------------
# run data-collection
# ---------------------------------------------------------------------------
def test_the_scheduled_job_collects_the_configured_symbols() -> None:
    result = runner.invoke(app, ["run", "data-collection", "--simulated"])

    assert result.exit_code == EXIT_OK, _text(result)
    text = _text(result)
    assert "SPY" in text
    assert "No orders were submitted" in text


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def test_no_data_command_can_reach_an_order_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every data command against a mutation-recording writable broker.

    A read-only broker refuses an order before the submission hook, which would
    make "nothing was attempted" indistinguishable from "an attempt was
    blocked". Writable, any attempt is recorded — so an empty record is proof.
    """
    from trading_system.broker.base import OrderSubmissionNotImplementedError
    from trading_system.broker.simulator import SimulatedBroker
    from trading_system.domain.enums import TradingMode

    class _Recording(SimulatedBroker):
        """Writable, so an attempted mutation is recorded rather than refused."""

        def __init__(self) -> None:
            super().__init__(read_only=False, trading_mode=TradingMode.DRY_RUN)
            self.mutation_attempts: list[str] = []

        def _submit_order(self, intent):
            self.mutation_attempts.append("place_order")
            raise OrderSubmissionNotImplementedError(self.name)

        def _cancel_order(self, broker_order_id: str):
            self.mutation_attempts.append("cancel_order")
            raise OrderSubmissionNotImplementedError(self.name)

    brokers: list[_Recording] = []

    def _factory(*args: object, **kwargs: object) -> _Recording:
        broker = _Recording()
        brokers.append(broker)
        return broker

    monkeypatch.setattr("trading_system.data.service.build_broker", _factory)

    for args in (
        ["data", "providers"],
        ["data", "collect", "--symbol", "SPY"],
        ["data", "collect-options", "--symbol", "SPY"],
        ["data", "snapshot", "--symbol", "SPY"],
        ["data", "quality", "--symbol", "SPY"],
        ["data", "history", "--symbol", "SPY"],
        ["data", "status"],
        ["run", "data-collection"],
    ):
        runner.invoke(app, args)

    for broker in brokers:
        assert broker.mutation_attempts == []
        assert broker.orders_submitted == 0


def test_the_data_commands_are_tagged_in_help() -> None:
    """A reader must see the blast radius before running a command."""
    text = _text(runner.invoke(app, ["data", "--help"]))

    assert "read-only" in text
    assert "collects data" in text
