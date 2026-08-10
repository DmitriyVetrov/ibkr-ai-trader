"""Command line interface.

Entry point::

    python -m trading_system.cli --help

Two conventions hold throughout:

* Every command is tagged ``(read-only)`` or ``(mutates state)`` in its help
  text, so the blast radius of a command is visible before running it
  (specification section 49).
* Commands whose implementation belongs to a later milestone exit with
  :data:`EXIT_NOT_IMPLEMENTED` and say which milestone owns them. They never
  print plausible-looking fake output — a stub that pretends to have reached
  IBKR is worse than no command at all.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from trading_system import __version__
from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.settings import (
    ConfigError,
    Settings,
    load_config,
    project_root,
)

__all__ = ["app", "main"]

#: Exit codes. 0 success, 1 runtime error, 2 usage (Typer), 3 not yet built.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 3

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="trading-system",
    help=(
        "Autonomous IBKR options trading system.\n\n"
        "Commands are tagged (read-only) or (mutates state). "
        "Default trading mode is PAPER; LIVE requires explicit configuration."
    ),
    no_args_is_help=True,
    add_completion=False,
)

run_app = typer.Typer(
    help="Execute a scheduled job once. (mutates state)",
    no_args_is_help=True,
)
test_app = typer.Typer(
    help="Diagnostics and safe end-to-end checks.",
    no_args_is_help=True,
)
data_app = typer.Typer(
    help="Inspect and collect market/research data.",
    no_args_is_help=True,
)
reports_app = typer.Typer(
    help="Generate and inspect reports. (read-only)",
    no_args_is_help=True,
)

app.add_typer(run_app, name="run")
app.add_typer(test_app, name="test")
app.add_typer(data_app, name="data")
app.add_typer(reports_app, name="reports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _not_implemented(feature: str, milestone: str) -> None:
    """Report honestly that a command exists but is not built yet."""
    err_console.print(
        f"[yellow]NOT IMPLEMENTED[/yellow]  {feature}\n"
        f"This command is defined by the specification but is delivered in "
        f"[bold]{milestone}[/bold].\n"
        f"No broker connection was attempted and no data was fabricated."
    )
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # configuration/guard failure must be fatal
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
@app.callback()
def _main_callback() -> None:
    """Autonomous options trading system."""


@app.command()
def version() -> None:
    """Show the application version. (read-only)"""
    console.print(f"trading-system {__version__}")


@app.command()
def health() -> None:
    """Report configuration, mode and schema availability. (read-only)

    Makes no network calls and touches no broker.
    """
    settings = _load_settings()

    table = Table(title="Health", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")

    table.add_row("application version", __version__)
    table.add_row("python", sys.version.split()[0])

    mode_style = "green" if settings.trading_mode is not TradingMode.LIVE else "bold red"
    table.add_row("trading mode", f"[{mode_style}]{settings.trading_mode.value}[/{mode_style}]")
    table.add_row("live tests allowed", "yes" if settings.allow_live_tests else "no")

    ok = True
    try:
        config = load_config(settings.config_dir)
        table.add_row("configuration", f"[green]OK[/green] ({settings.config_dir})")
        table.add_row("config version", config.application.config_version)
        table.add_row(
            "strategies enabled", ", ".join(sorted(config.enabled_strategies())) or "none"
        )
        table.add_row("scheduled jobs", str(len(config.schedules.jobs)))
    except ConfigError as exc:
        ok = False
        table.add_row("configuration", f"[red]FAILED[/red] {exc}")

    schema_dir = project_root() / "schemas"
    schema_count = len(list(schema_dir.glob("*.json"))) if schema_dir.is_dir() else 0
    table.add_row("workflow schemas", f"{schema_count} found in {schema_dir}")
    if schema_count == 0:
        ok = False

    console.print(table)

    if not ok:
        raise typer.Exit(code=EXIT_ERROR)
    console.print("[green]PASS[/green]  No broker connection attempted.")


@app.command()
def config(
    show: Annotated[bool, typer.Option("--show", help="Print the resolved configuration.")] = True,
) -> None:
    """Validate and display the YAML configuration. (read-only)"""
    settings = _load_settings()
    try:
        loaded = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    console.print(f"[green]Configuration valid[/green] ({settings.config_dir})")
    if show:
        console.print_json(loaded.model_dump_json(indent=2))


@app.command()
def portfolio() -> None:
    """Show broker account and positions. (read-only)"""
    _not_implemented("portfolio read", "Milestone 2 (broker connectivity)")


@app.command()
def positions() -> None:
    """List tracked positions and their lifecycle state. (read-only)"""
    _not_implemented("position listing", "Milestone 9 (position lifecycle)")


@app.command()
def research(
    ticker: Annotated[str | None, typer.Option(help="Restrict to one underlying.")] = None,
) -> None:
    """Show the latest stored research reports. (read-only)"""
    _not_implemented(f"research inspection{f' for {ticker}' if ticker else ''}", "Milestone 5")


@app.command()
def opportunities() -> None:
    """Show the current ranked opportunities. (read-only)"""
    _not_implemented("opportunity ranking", "Milestone 7 (allocation and risk)")


@app.command()
def reconcile() -> None:
    """Compare internal state against the broker. (mutates state)

    Broker state is authoritative; a mismatch blocks new executions.
    """
    _not_implemented("reconciliation", "Milestone 2 (broker connectivity)")


# ---------------------------------------------------------------------------
# run  — one scheduled job, once
# ---------------------------------------------------------------------------
@run_app.command("universe")
def run_universe() -> None:
    """Rebuild the candidate universe. (mutates state)"""
    _not_implemented("universe selection", "Milestone 4 (universe)")


@run_app.command("research")
def run_research() -> None:
    """Run market research for the current universe. (mutates state)"""
    _not_implemented("market research", "Milestone 5 (research)")


@run_app.command("opportunities")
def run_opportunities() -> None:
    """Run the slow discovery loop through risk validation. (mutates state)"""
    _not_implemented("opportunity scan", "Milestone 7 (allocation and risk)")


@run_app.command("position-monitor")
def run_position_monitor() -> None:
    """Run the fast position management loop once. (mutates state)"""
    _not_implemented("position monitor", "Milestone 9 (position lifecycle)")


@run_app.command("thesis-monitor")
def run_thesis_monitor() -> None:
    """Re-check whether entry theses still hold. (mutates state)"""
    _not_implemented("thesis monitor", "Milestone 9 (position lifecycle)")


@run_app.command("reconciliation")
def run_reconciliation() -> None:
    """Reconcile internal state against IBKR. (mutates state)"""
    _not_implemented("reconciliation loop", "Milestone 2 (broker connectivity)")


@run_app.command("data-collection")
def run_data_collection() -> None:
    """Collect and persist market/option snapshots. (mutates state)"""
    _not_implemented("data collection", "Milestone 3 (data layer)")


@run_app.command("end-of-day-report")
def run_end_of_day_report() -> None:
    """Produce the daily report. (mutates state)"""
    _not_implemented("end-of-day report", "Milestone 11 (evaluation)")


# ---------------------------------------------------------------------------
# test  — diagnostics
# ---------------------------------------------------------------------------
@test_app.command("ibkr-connection")
def test_ibkr_connection() -> None:
    """Connect to IBKR and report status. Submits zero orders. (read-only)"""
    _not_implemented("IBKR connection test", "Milestone 2 (broker connectivity)")


@test_app.command("ibkr-portfolio")
def test_ibkr_portfolio() -> None:
    """Read positions and P&L from IBKR. Submits zero orders. (read-only)"""
    _not_implemented("IBKR portfolio read", "Milestone 2 (broker connectivity)")


@test_app.command("ibkr-market-data")
def test_ibkr_market_data() -> None:
    """Read market data from IBKR. Submits zero orders. (read-only)"""
    _not_implemented("IBKR market data read", "Milestone 2 (broker connectivity)")


@test_app.command("ibkr-option-chain")
def test_ibkr_option_chain() -> None:
    """Read an option chain from IBKR. Submits zero orders. (read-only)"""
    _not_implemented("IBKR option chain read", "Milestone 2 (broker connectivity)")


@test_app.command("ibkr-order-simulation")
def test_ibkr_order_simulation() -> None:
    """Exercise order construction against the simulator. (mutates state)

    Uses the simulator unless explicitly configured otherwise.
    """
    _not_implemented("order simulation", "Milestone 8 (execution)")


@test_app.command("workflow")
def test_workflow(
    stage: Annotated[str, typer.Argument(help="Workflow stage, e.g. research.")],
) -> None:
    """Exercise a single workflow stage against fixtures. (read-only)"""
    _not_implemented(f"workflow stage '{stage}'", "Milestone 5 onwards")


@test_app.command("strategy-selection")
def test_strategy_selection(
    ticker: Annotated[str, typer.Option(help="Underlying to evaluate.")],
) -> None:
    """Exercise strategy selection for one underlying. (read-only)"""
    _not_implemented(f"strategy selection for {ticker}", "Milestone 6 (strategy)")


@test_app.command("contract-selection")
def test_contract_selection(
    ticker: Annotated[str, typer.Option(help="Underlying to evaluate.")],
) -> None:
    """Exercise deterministic contract selection. (read-only)"""
    _not_implemented(f"contract selection for {ticker}", "Milestone 6 (strategy)")


@test_app.command("allocation")
def test_allocation() -> None:
    """Exercise campaign budget allocation against fixtures. (read-only)"""
    _not_implemented("allocation", "Milestone 7 (allocation and risk)")


@test_app.command("risk")
def test_risk() -> None:
    """Exercise the risk engine against fixtures. (read-only)"""
    _not_implemented("risk engine", "Milestone 7 (allocation and risk)")


@test_app.command("reconciliation")
def test_reconciliation() -> None:
    """Exercise reconciliation against a simulated broker. (read-only)"""
    _not_implemented("reconciliation test", "Milestone 2 (broker connectivity)")


@test_app.command("e2e-dry-run")
def test_e2e_dry_run() -> None:
    """Full lifecycle in simulation. No broker order is submitted. (read-only)"""
    _not_implemented("end-to-end dry run", "Milestone 8 (execution)")


@test_app.command("e2e-paper")
def test_e2e_paper() -> None:
    """Full lifecycle against the IBKR PAPER account. (mutates state)

    Explicitly labelled: this reaches a real broker account.
    """
    _not_implemented("end-to-end paper run", "Milestone 8 (execution)")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
@data_app.command("status")
def data_status() -> None:
    """Summarise what data has been collected so far. (read-only)"""
    _not_implemented("data status", "Milestone 3 (data layer)")


@data_app.command("collect")
def data_collect() -> None:
    """Collect a data snapshot now. (mutates state)"""
    _not_implemented("data collection", "Milestone 3 (data layer)")


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
@reports_app.command("daily")
def reports_daily() -> None:
    """Show the most recent daily report. (read-only)"""
    _not_implemented("daily report", "Milestone 11 (evaluation)")


@reports_app.command("performance")
def reports_performance() -> None:
    """Show campaign performance attribution. (read-only)"""
    _not_implemented("performance report", "Milestone 11 (evaluation)")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
