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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from trading_system import __version__
from trading_system.broker.base import Broker, BrokerError
from trading_system.broker.factory import build_broker
from trading_system.domain.enums import SecurityType, TradingMode
from trading_system.infrastructure.settings import (
    BrokerBackend,
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


#: Flag shared by every broker diagnostic.
SimulatedOption = Annotated[
    bool,
    typer.Option(
        "--simulated",
        help="Run against the deterministic simulator instead of IBKR.",
    ),
]

SymbolOption = Annotated[str, typer.Option("--symbol", help="Underlying symbol, e.g. SPY.")]


def _fail(message: str) -> None:
    """Print a FAIL banner and exit non-zero. Never falls back to fake data."""
    err_console.print(f"[red]FAIL[/red]  {message}")
    raise typer.Exit(code=EXIT_ERROR)


@contextmanager
def _connected_broker(simulated: bool) -> Iterator[tuple[Settings, Broker]]:
    """Open a read-only broker connection for a diagnostic, and always close it.

    Any broker failure is reported as FAIL with a diagnostic. Nothing is
    substituted for real data: if the broker cannot answer, the command fails.
    """
    settings = _load_settings()
    backend = BrokerBackend.SIMULATOR if simulated else None

    try:
        broker = build_broker(settings, backend=backend)
    except BrokerError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover

    try:
        broker.connect()
    except BrokerError as exc:
        broker.disconnect()
        _fail(str(exc))

    try:
        yield settings, broker
    finally:
        broker.disconnect()


def _print_header(title: str, settings: Settings, broker: Broker) -> None:
    access = "SIMULATED" if broker.name == "SIMULATOR" else settings.trading_mode.value
    console.print(f"\n[bold]{title}[/bold]")
    console.print("-" * len(title))
    console.print(f"Access     : READ-ONLY / {access}")
    console.print(f"Broker     : {broker.name}")
    console.print(f"Mode       : {settings.trading_mode.value}")


def _print_zero_orders(broker: Broker) -> None:
    """Report — and verify — that the diagnostic submitted nothing.

    The count is read off the broker rather than hard-coded, so the line is
    evidence rather than decoration.
    """
    submitted = broker.orders_submitted
    style = "green" if submitted == 0 else "bold red"
    console.print(f"Orders submitted: [{style}]{submitted}[/{style}]")
    if submitted != 0:
        _fail(f"read-only diagnostic submitted {submitted} order(s)")


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
def health(
    broker: Annotated[
        bool,
        typer.Option("--broker", help="Also probe the broker connection."),
    ] = False,
    simulated: SimulatedOption = False,
) -> None:
    """Report configuration, mode and schema availability. (read-only)

    Offline by default: makes no network calls and touches no broker. Pass
    ``--broker`` to additionally probe broker connectivity. Never submits an
    order either way.
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

    backend = BrokerBackend.SIMULATOR if simulated else settings.resolved_broker_backend
    table.add_row("broker backend", backend.value)
    table.add_row("broker read-only", "yes" if settings.ibkr_read_only else "[red]NO[/red]")
    if backend is BrokerBackend.IBKR:
        table.add_row("broker endpoint", f"{settings.ibkr_host}:{settings.ibkr_port}")

    console.print(table)

    if not broker:
        if not ok:
            raise typer.Exit(code=EXIT_ERROR)
        console.print("[green]PASS[/green]  No broker connection attempted.")
        return

    with _connected_broker(simulated) as (_, connection):
        probe = connection.health_check()
        console.print(
            f"\nBroker     : {probe.broker}\n"
            f"Status     : {probe.state.value}\n"
            f"Account    : {_mask_account(probe.account_id)}"
        )
        if probe.error:
            console.print(f"Error      : {probe.error}")
        _print_zero_orders(connection)
        if not probe.is_usable:
            _fail(f"broker is {probe.state.value}")

    if not ok:
        raise typer.Exit(code=EXIT_ERROR)
    console.print("[green]PASS[/green]")


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
    """Reconcile and persist the result. (mutates state)

    The read-only comparison exists today as `test reconciliation`. This
    command additionally persists the outcome and gates execution on it, which
    needs the position repository from Milestone 9.
    """
    _not_implemented(
        "persisted reconciliation (use 'test reconciliation' for the read-only check)",
        "Milestone 9 (position lifecycle)",
    )


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
def test_ibkr_connection(simulated: SimulatedOption = False) -> None:
    """Connect to the broker and report status. Submits zero orders. (read-only)"""
    with _connected_broker(simulated) as (settings, broker):
        health = broker.health_check()
        _print_header("BROKER CONNECTION TEST", settings, broker)
        console.print(f"Host       : {health.host}")
        console.print(f"Port       : {health.port}")
        console.print(f"Status     : {health.state.value}")
        console.print(f"Account    : {_mask_account(health.account_id)}")
        console.print(f"Read-only  : {health.read_only}")
        if health.latency_ms is not None:
            console.print(f"Latency    : {health.latency_ms:.1f} ms")
        if health.error:
            console.print(f"Error      : {health.error}")

        if not health.is_usable:
            _print_zero_orders(broker)
            _fail(f"broker is {health.state.value}")

        try:
            summary = broker.get_account_summary()
        except BrokerError as exc:
            _print_zero_orders(broker)
            _fail(str(exc))
        else:
            console.print(f"Summary tags: {len(summary)}")

        _print_zero_orders(broker)
        console.print("[green]PASS[/green]  No orders were submitted.\n")


@test_app.command("ibkr-portfolio")
def test_ibkr_portfolio(simulated: SimulatedOption = False) -> None:
    """Read account, positions, orders and fills. Submits zero orders. (read-only)"""
    with _connected_broker(simulated) as (settings, broker):
        _print_header("BROKER PORTFOLIO READ", settings, broker)
        try:
            account = broker.get_account()
            positions = broker.get_positions()
            orders = broker.get_open_orders()
            executions = broker.get_executions()
        except BrokerError as exc:
            _print_zero_orders(broker)
            _fail(str(exc))
            return

        console.print(f"Account    : {_mask_account(account.account_id)}")
        console.print(f"Currency   : {account.currency}")
        for label, value in (
            ("Cash", account.cash),
            ("Net liquidation", account.net_liquidation),
            ("Buying power", account.buying_power),
            ("Available funds", account.available_funds),
            ("Excess liquidity", account.excess_liquidity),
            ("Unrealized P&L", account.unrealized_pnl),
        ):
            # "not reported" is a distinct fact from a zero balance.
            console.print(f"{label:<18}: {value if value is not None else '(not reported)'}")

        console.print(f"\nPositions  : {len(positions)}")
        if positions:
            table = Table(show_header=True, header_style="bold")
            for column in ("Symbol", "Type", "Qty", "Avg cost", "Mkt value", "Unrealized"):
                table.add_column(column)
            for position in positions:
                table.add_row(
                    position.local_symbol or position.symbol,
                    position.security_type.value,
                    str(position.quantity),
                    _or_dash(position.average_cost),
                    _or_dash(position.market_value),
                    _or_dash(position.unrealized_pnl),
                )
            console.print(table)

        console.print(f"Open orders: {len(orders)}")
        for order in orders:
            console.print(
                f"  {order.broker_order_id} {order.side.value} {order.quantity} "
                f"{order.symbol} [{order.status.value}] filled={order.filled_quantity}"
            )
        console.print(f"Executions : {len(executions)}")

        _print_zero_orders(broker)
        console.print("[green]PASS[/green]  Read-only portfolio read completed.\n")


@test_app.command("ibkr-market-data")
def test_ibkr_market_data(
    symbol: SymbolOption = "SPY",
    simulated: SimulatedOption = False,
) -> None:
    """Read a quote for one symbol. Submits zero orders. (read-only)"""
    with _connected_broker(simulated) as (settings, broker):
        _print_header("BROKER MARKET DATA TEST", settings, broker)
        console.print(f"Symbol     : {symbol.upper()}")
        try:
            snapshot = broker.get_market_data(symbol, SecurityType.STOCK)
        except BrokerError as exc:
            # Explicitly not a fallback to a made-up price.
            console.print("Data origin: [yellow]MARKET_DATA_UNAVAILABLE[/yellow]")
            _print_zero_orders(broker)
            _fail(str(exc))
            return

        console.print(f"Data origin: {snapshot.origin.value}")
        console.print(f"Quality    : {snapshot.data_quality.value}")
        console.print(f"As of      : {snapshot.as_of.isoformat()}")
        console.print(f"Bid/Ask    : {_or_dash(snapshot.bid)} / {_or_dash(snapshot.ask)}")
        console.print(f"Last/Close : {_or_dash(snapshot.last)} / {_or_dash(snapshot.close)}")

        _print_zero_orders(broker)
        console.print("[green]PASS[/green]  Quote retrieved.\n")


@test_app.command("ibkr-option-chain")
def test_ibkr_option_chain(
    symbol: SymbolOption = "SPY",
    simulated: SimulatedOption = False,
) -> None:
    """Read and normalise an option chain. Submits zero orders. (read-only)

    Proves the chain can be requested and normalised. It selects no contract
    and recommends no trade — that is Milestone 6.
    """
    with _connected_broker(simulated) as (settings, broker):
        _print_header("BROKER OPTION CHAIN TEST", settings, broker)
        console.print(f"Underlying : {symbol.upper()}")
        try:
            chain = broker.get_option_chain(symbol)
        except BrokerError as exc:
            _print_zero_orders(broker)
            _fail(str(exc))
            return

        console.print(f"Data origin: {chain.origin.value}")
        console.print(f"Exchange   : {chain.exchange or '-'}")
        console.print(f"Multiplier : {chain.multiplier or '-'}")
        console.print(f"Expirations: {len(chain.expirations)}")
        if chain.expirations:
            shown = ", ".join(d.isoformat() for d in chain.expirations[:6])
            console.print(f"  first six: {shown}")
        console.print(f"Strikes    : {len(chain.strikes)}")
        if chain.strikes:
            console.print(f"  range    : {chain.strikes[0]} - {chain.strikes[-1]}")
        console.print(f"Rights     : {', '.join(r.value for r in chain.rights)}")

        _print_zero_orders(broker)
        console.print("[green]PASS[/green]  Chain retrieved. No contract selected.\n")


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
def test_reconciliation(simulated: SimulatedOption = False) -> None:
    """Compare internal state against broker state. Submits zero orders. (read-only)

    Milestone 2 has no position repository yet, so internal state is empty and
    every broker position shows up as a discrepancy. That is the correct,
    fail-safe result: unexplained broker positions must block trading.
    """
    from trading_system.broker.ibkr.reconciliation import Reconciler

    with _connected_broker(simulated) as (settings, broker):
        _print_header("RECONCILIATION TEST", settings, broker)
        report = Reconciler().reconcile(broker)

        console.print(f"Status     : {report.status.value}")
        console.print(f"Positions  : {report.positions_compared} compared")
        console.print(f"Orders     : {report.orders_compared} compared")
        console.print(f"Executions : {report.executions_compared} compared")
        console.print(f"Blocks new executions: {report.blocks_new_executions}")

        if report.discrepancies:
            console.print(f"\nDiscrepancies ({len(report.discrepancies)}):")
            for discrepancy in report.discrepancies:
                console.print(
                    f"  [{discrepancy.discrepancy_type.value}] {discrepancy.identifier}: "
                    f"{discrepancy.description}"
                )
                console.print(
                    f"      internal={discrepancy.internal_value!r} "
                    f"broker={discrepancy.broker_value!r}"
                )
            console.print(
                "\n[yellow]No resolution attempted.[/yellow] The broker is authoritative; "
                "discrepancies are reported, never silently corrected."
            )

        _print_zero_orders(broker)
        console.print("[green]PASS[/green]  Reconciliation completed read-only.\n")


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


def _mask_account(account_id: str | None) -> str:
    """Show only the last characters of an account number.

    Account numbers end up in logs and pasted terminal output; the full value
    is not needed to confirm the right account is connected.
    """
    if not account_id:
        return "(unknown)"
    if len(account_id) <= 4:
        return "*" * len(account_id)
    return f"{'*' * (len(account_id) - 4)}{account_id[-4:]}"


def _or_dash(value: object | None) -> str:
    return "-" if value is None else str(value)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
