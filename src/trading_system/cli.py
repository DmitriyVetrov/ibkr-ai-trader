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
from typing import TYPE_CHECKING, Annotated

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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from trading_system.data.collectors import CollectionReport
    from trading_system.data.service import DataService
    from trading_system.domain.enums import DataType
    from trading_system.research.service import ResearchService
    from trading_system.universe.service import UniverseSelectionService

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
universe_app = typer.Typer(
    help=(
        "Select and inspect the research universe. Every command here is "
        "read-only with respect to the broker and submits zero orders."
    ),
    no_args_is_help=True,
)
research_app = typer.Typer(
    help=(
        "Research the selected universe and inspect the resulting outlooks. "
        "Every command here is read-only with respect to the broker and "
        "submits zero orders. Research produces a hypothesis, never a contract."
    ),
    no_args_is_help=True,
)
reports_app = typer.Typer(
    help="Generate and inspect reports. (read-only)",
    no_args_is_help=True,
)

app.add_typer(run_app, name="run")
app.add_typer(test_app, name="test")
app.add_typer(data_app, name="data")
app.add_typer(universe_app, name="universe")
app.add_typer(research_app, name="research")
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
def run_universe(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant; rebuild the universe as it was then."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run everything but do not persist the result."),
    ] = False,
) -> None:
    """Rebuild the candidate universe. (writes a universe run; submits no orders)

    Runs the ``universe_refresh`` job from ``config/schedules.yaml`` once. It
    reads stored data only — no broker connection is opened and no data is
    collected, so run ``data collect`` first if the store is empty.
    """
    _universe_run(as_of=as_of, dry_run=dry_run)


@run_app.command("research")
def run_research(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant; research as of that moment."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run everything but do not persist the result."),
    ] = False,
) -> None:
    """Research the current universe. (writes research reports; submits no orders)

    Runs the ``opportunity_scan``'s research stage once. It reads stored data
    only — no broker connection is opened and no data is collected, so run
    ``data collect`` and ``universe run`` first if the store is empty.
    """
    _research_run(as_of=as_of, dry_run=dry_run, symbols=None, universe_run_id=None)


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
def run_data_collection(simulated: SimulatedOption = False) -> None:
    """Collect and persist market/option snapshots. (collects data; submits no orders)

    Runs the ``data_collection`` job from ``config/schedules.yaml`` once, over
    the symbols configured in ``config/data.yaml``. Each data type is collected
    independently, so one provider being down does not stop the others from
    accumulating history.
    """
    service = _data_service(simulated)
    symbols = service.configured_symbols()
    if not symbols:
        console.print("[yellow]No symbols are configured in config/data.yaml.[/yellow]")
        return

    failures = 0
    for symbol in symbols:
        for report in service.collect_all(symbol):
            _print_collection(report)
            failures += 0 if report.succeeded else 1

    if failures:
        _fail(f"{failures} collection(s) failed; existing history was not modified")
    console.print("[green]PASS[/green]  Collection complete. No orders were submitted.")


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
#
# Every command here is read-only with respect to the broker: they retrieve and
# they write to local storage, and none of them can reach an order path. The
# broker connections they open are read-only, and each run reports the orders
# it submitted, which is always zero.
# ---------------------------------------------------------------------------
def _data_service(simulated: bool) -> DataService:
    """Build the data layer from configuration, or fail with a diagnostic."""
    from trading_system.data.service import DataService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return DataService(settings=settings, config=config, simulated=simulated)


def _print_collection(report: CollectionReport) -> None:
    """Print one collection result, leading with what the data actually is."""
    status_styles = {
        "REAL": "green",
        "SIMULATED": "yellow",
        "CACHED": "cyan",
        "HISTORICAL": "cyan",
        "UNAVAILABLE": "red",
    }
    status = report.display_status
    style = status_styles.get(status, "white")
    console.print(
        f"[{style}]{status:<12}[/{style}] {report.data_type.value:<20} "
        f"{report.key:<8} provider={report.provider}"
    )
    console.print(
        f"             outcome={report.outcome.value} records={report.records_normalized} "
        f"snapshots_created={report.snapshots_created} "
        f"duration={report.duration_seconds:.2f}s"
    )
    if report.research_usable is not None:
        style = "green" if report.research_usable else "yellow"
        console.print(f"             research_usable=[{style}]{report.research_usable}[/{style}]")
    if report.quality_issues:
        console.print(
            f"             quality_issues={', '.join(i.value for i in report.quality_issues)}"
        )
    if report.error:
        console.print(f"             [red]error[/red]={report.error}")


@data_app.command("providers")
def data_providers(simulated: SimulatedOption = False) -> None:
    """List registered data providers and their tier, cost and availability. (read-only)"""
    service = _data_service(simulated)
    descriptions = service.providers()

    table = Table(title="Data providers", show_header=True, header_style="bold")
    for column in ("Provider", "Tier", "Cost", "Origin", "Data types", "Availability"):
        table.add_column(column)
    for description in descriptions:
        cost_style = "red" if description.cost.value == "PAID" else "green"
        table.add_row(
            description.provider_id,
            description.tier.value,
            f"[{cost_style}]{description.cost.value}[/{cost_style}]",
            description.origin.value,
            ", ".join(sorted(t.value for t in description.data_types)),
            description.availability.value,
        )
    console.print(table)

    paid = [d.provider_id for d in descriptions if d.cost.value == "PAID"]
    if paid:
        _fail(f"paid providers are configured: {', '.join(paid)}")
    console.print("[green]No paid data provider is configured or required.[/green]")


@data_app.command("collect")
def data_collect(
    symbol: SymbolOption = "SPY",
    simulated: SimulatedOption = False,
) -> None:
    """Collect a market quote snapshot. (collects data; submits no orders)"""
    service = _data_service(simulated)
    report = service.collect_quote(symbol)
    _print_collection(report)
    if not report.succeeded:
        _fail(f"collection failed: {report.error}")
    console.print("[green]PASS[/green]  Snapshot stored. No orders were submitted.")


@data_app.command("collect-options")
def data_collect_options(
    symbol: SymbolOption = "SPY",
    simulated: SimulatedOption = False,
    quotes: Annotated[
        bool,
        typer.Option("--quotes", help="Also collect per-contract option quotes where available."),
    ] = False,
) -> None:
    """Collect an option chain snapshot. Selects no contract. (collects data)"""
    service = _data_service(simulated)
    reports = [service.collect_option_chain(symbol)]
    if quotes:
        reports.append(service.collect_option_quotes(symbol))
    for report in reports:
        _print_collection(report)
    if not reports[0].succeeded:
        _fail(f"collection failed: {reports[0].error}")
    console.print("[green]PASS[/green]  Chain stored. No contract was selected.")


@data_app.command("snapshot")
def data_snapshot(
    symbol: SymbolOption = "SPY",
    data_type: Annotated[
        str, typer.Option("--type", help="Data type, e.g. MARKET_QUOTE or OPTION_CHAIN.")
    ] = "MARKET_QUOTE",
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant; shows what was known then."),
    ] = None,
    simulated: SimulatedOption = False,
) -> None:
    """Show a stored snapshot, optionally as of a past instant. (read-only)

    With ``--as-of`` this answers "what did the system know at time T", using
    only records that had actually been retrieved by then.
    """
    service = _data_service(simulated)
    kind = _parse_data_type(data_type)

    if as_of is None:
        snapshot = service.latest(kind, symbol)
        label = "latest"
    else:
        snapshot = service.as_of(kind, symbol, _parse_instant(as_of))
        label = f"as of {as_of}"

    if snapshot is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no {kind.value} snapshot for "
            f"{symbol.upper()} ({label})."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print(f"\n[bold]SNAPSHOT[/bold] ({label})")
    console.print(f"Snapshot id : {snapshot.snapshot_id}")
    console.print(f"Data type   : {snapshot.data_type.value}")
    console.print(f"Key         : {snapshot.key}")
    console.print(f"Provider    : {snapshot.provider} (tier {snapshot.source_tier.value})")
    console.print(f"Data origin : {snapshot.data_origin.value}")
    console.print(f"As of       : {snapshot.as_of.isoformat()}")
    console.print(f"Retrieved   : {snapshot.retrieved_at.isoformat()}")
    console.print(f"Schema      : {snapshot.schema_version} / app {snapshot.application_version}")
    console.print(f"Payload hash: {snapshot.payload_hash}")
    console.print(f"Records     : {snapshot.record_count}")
    console.print(f"Research use: {snapshot.data_quality.research_usable}")


@data_app.command("quality")
def data_quality(
    symbol: SymbolOption = "SPY",
    data_type: Annotated[
        str, typer.Option("--type", help="Data type to inspect.")
    ] = "MARKET_QUOTE",
    simulated: SimulatedOption = False,
) -> None:
    """Show the quality verdict on the latest stored snapshot. (read-only)"""
    service = _data_service(simulated)
    kind = _parse_data_type(data_type)
    snapshot = service.latest(kind, symbol)
    if snapshot is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no {kind.value} snapshot for {symbol.upper()}. "
            f"Run 'data collect --symbol {symbol.upper()}' first."
        )
        raise typer.Exit(code=EXIT_OK)

    report = snapshot.data_quality
    table = Table(title=f"Data quality — {symbol.upper()} {kind.value}", show_header=True)
    table.add_column("Dimension")
    table.add_column("Valid")
    for name, value in (
        ("transport", report.transport_valid),
        ("schema", report.schema_valid),
        ("source", report.source_valid),
        ("timestamp", report.timestamp_valid),
        ("freshness", report.freshness_valid),
        ("completeness", report.completeness_valid),
        ("plausibility", report.plausibility_valid),
        ("consistency", report.consistency_valid),
    ):
        table.add_row(
            name, f"[{'green' if value else 'red'}]{value}[/{'green' if value else 'red'}]"
        )
    table.add_row(
        "[bold]research_usable[/bold]",
        f"[{'green' if report.research_usable else 'yellow'}]{report.research_usable}"
        f"[/{'green' if report.research_usable else 'yellow'}]",
    )
    console.print(table)
    console.print(f"Classification: {report.classification.value}")
    if report.issues:
        console.print("\nIssues:")
        for issue in report.issues:
            console.print(f"  - {issue.value}")
        for detail in report.details[:20]:
            console.print(f"    {detail}")
        console.print(
            "\n[yellow]Flagged values are preserved exactly as received.[/yellow] "
            "Nothing was corrected, smoothed or dropped."
        )


@data_app.command("history")
def data_history(
    symbol: SymbolOption = "SPY",
    data_type: Annotated[
        str, typer.Option("--type", help="Data type to inspect.")
    ] = "MARKET_QUOTE",
    limit: Annotated[int, typer.Option("--limit", help="Maximum ledger entries to show.")] = 20,
    simulated: SimulatedOption = False,
) -> None:
    """Show the append-only collection history for a symbol. (read-only)"""
    service = _data_service(simulated)
    kind = _parse_data_type(data_type)
    entries = service.history(kind, symbol)
    if not entries:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no collection history for {symbol.upper()} "
            f"{kind.value}."
        )
        raise typer.Exit(code=EXIT_OK)

    table = Table(title=f"History — {symbol.upper()} {kind.value}", show_header=True)
    for column in ("Recorded", "Event", "Provider", "As of", "Records", "Usable", "Detail"):
        table.add_column(column)
    for entry in entries[-limit:]:
        table.add_row(
            entry.recorded_at.isoformat(timespec="seconds"),
            entry.event,
            entry.provider,
            entry.as_of.isoformat(timespec="seconds") if entry.as_of else "-",
            str(entry.record_count if entry.record_count is not None else "-"),
            "-" if entry.research_usable is None else str(entry.research_usable),
            (entry.outcome or entry.detail or "")[:60],
        )
    console.print(table)
    console.print(
        f"{len(entries)} ledger entries. History is append-only: nothing here is ever rewritten."
    )


@data_app.command("status")
def data_status(simulated: SimulatedOption = False) -> None:
    """Summarise what data has been collected so far. (read-only)"""
    service = _data_service(simulated)
    statuses = service.status()

    console.print(f"\n[bold]DATA STATUS[/bold]  ({service.data_root})")
    console.print(f"Configured symbols       : {', '.join(service.configured_symbols()) or 'none'}")
    console.print(
        f"Configured option symbols: {', '.join(service.configured_option_symbols()) or 'none'}"
    )
    console.print(f"Registered providers     : {len(service.registry)}")

    if not statuses:
        console.print(
            "\n[yellow]No data has been collected yet.[/yellow] "
            "History accumulates going forward; nothing is backfilled."
        )
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("Provider", "Data type", "Key", "Last success", "Snapshots", "Fails", "Gap"):
        table.add_column(column)
    for status in statuses:
        state = status.state
        gap_style = "green" if status.gap.status.value == "NO_GAP" else "yellow"
        table.add_row(
            state.provider,
            state.data_type.value,
            state.key,
            state.last_successful_collection.isoformat(timespec="seconds")
            if state.last_successful_collection
            else "never",
            str(state.snapshot_count),
            str(state.consecutive_failures),
            f"[{gap_style}]{status.gap.status.value}[/{gap_style}]",
        )
    console.print(table)


def _parse_data_type(value: str) -> DataType:
    from trading_system.domain.enums import DataType

    try:
        return DataType(value.strip().upper())
    except ValueError:
        _fail(f"unknown data type {value!r}. Valid values: {', '.join(t.value for t in DataType)}")
        raise AssertionError("unreachable") from None  # pragma: no cover


def _parse_instant(value: str) -> datetime:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(f"--as-of must be an ISO-8601 instant, got {value!r}")
        raise AssertionError("unreachable") from None  # pragma: no cover
    if parsed.tzinfo is None:
        _fail("--as-of must carry a timezone; a naive instant has no position on the timeline")
    return parsed


# ---------------------------------------------------------------------------
# universe
#
# Every command here is read-only with respect to the broker. The universe
# service constructs no broker, opens no connection and has no order path: it
# consumes stored data through the repository and writes a run record. The
# zero-order property is therefore structural, not a check performed at the end.
# ---------------------------------------------------------------------------
def _universe_service() -> UniverseSelectionService:
    """Build the universe layer from configuration, or fail with a diagnostic."""
    from trading_system.universe.service import UniverseSelectionService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return UniverseSelectionService(settings=settings, config=config)


def _universe_run(*, as_of: str | None, dry_run: bool) -> None:
    """Execute one universe run and print its report."""
    from trading_system.domain.enums import UniverseSelectionStatus
    from trading_system.universe.report import render_report

    service = _universe_service()
    instant = _parse_instant(as_of) if as_of else None

    run = service.run(as_of=instant, dry_run=dry_run)
    console.print()
    console.print(render_report(run.result))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  Nothing was persisted. Authoritative history is unchanged."
        )
    else:
        console.print(f"\n[green]Stored[/green] run {run.result.run_id}")

    if run.result.status is not UniverseSelectionStatus.SUCCESS:
        _fail(
            f"universe selection ended as {run.result.status.value}; "
            f"no universe was produced and no downstream stage may consume this run"
        )


@universe_app.command("show")
def universe_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
) -> None:
    """Show the current (or a named) universe. (read-only)"""
    from trading_system.universe.report import render_report

    service = _universe_service()
    result = service.get(run_id) if run_id else service.latest()

    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no universe run with id {run_id!r}."
                if run_id
                else "no universe has been selected yet. Run 'universe run' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_report(result))


@universe_app.command("validate")
def universe_validate() -> None:
    """Validate the universe configuration and its data readiness. (read-only)

    Checks the configuration loads, the source resolves to symbols, and reports
    which of those symbols actually have data stored. A symbol without data is
    not an error here — it is a fact, and one worth seeing before a run reports
    it as a rejection.
    """
    from trading_system.domain.enums import DataType
    from trading_system.universe.source import UniverseSourceError

    service = _universe_service()
    settings = _load_settings()
    config = load_config(settings.config_dir)
    universe = config.universe

    console.print("\n[bold]UNIVERSE CONFIGURATION[/bold]")
    console.print(f"Config version : {universe.config_version}")
    console.print(
        f"Source         : {universe.source.kind.value} "
        f"{universe.source.name} v{universe.source.version}"
    )

    try:
        symbols = service.configured_symbols()
    except UniverseSourceError as exc:
        console.print(f"Symbols        : [red]FAILED[/red]\n{exc}")
        _fail("the configured universe source cannot be resolved")
        return

    console.print(f"Symbols        : {len(symbols)} ({', '.join(symbols)})")

    filters = universe.filters
    table = Table(title="Deterministic filters", show_header=True, header_style="bold")
    table.add_column("Rule")
    table.add_column("Value")
    for name, value in (
        ("allowed security types", ", ".join(t.value for t in filters.allowed_security_types)),
        ("allowed currencies", ", ".join(filters.allowed_currencies)),
        ("allowed exchanges", ", ".join(filters.allowed_exchanges) or "any"),
        ("min price", str(filters.min_price)),
        ("min underlying volume", str(filters.min_average_daily_volume)),
        ("max data age (s)", str(filters.max_data_age_seconds)),
        ("require research usable", str(filters.require_research_usable)),
        ("optionability policy", filters.optionability_policy.value),
        ("exclusions", ", ".join(filters.exclusions) or "none"),
        ("max candidates", str(filters.max_candidates)),
        ("max selected assets", str(filters.max_selected_assets)),
    ):
        table.add_row(name, value)
    console.print(table)

    ranking = universe.ai_ranking
    console.print(
        f"\nAI ranking     : {'enabled' if ranking.enabled else 'disabled'} "
        f"({ranking.model_provider}/{ranking.model_name}, prompt {ranking.prompt_version})"
    )
    console.print(
        "Fallback       : "
        + (
            "[yellow]deterministic ordering permitted[/yellow]"
            if ranking.allow_deterministic_fallback
            else "[green]fail closed[/green] — no ordering is substituted"
        )
    )
    if ranking.enabled and settings.anthropic_api_key is None:
        console.print(
            "[yellow]ANTHROPIC_API_KEY is not set[/yellow]; a run would end as AI_UNAVAILABLE."
        )

    data_table = Table(title="Stored data", show_header=True, header_style="bold")
    for column in ("Symbol", "Quote snapshot", "Option chain"):
        data_table.add_column(column)
    missing = 0
    for symbol in symbols:
        quote = service.data_repository.get_latest(DataType.MARKET_QUOTE, symbol)
        chain = service.data_repository.get_latest(DataType.OPTION_CHAIN, symbol)
        if quote is None:
            missing += 1
        data_table.add_row(
            symbol,
            quote.as_of.isoformat(timespec="seconds") if quote else "[yellow]none[/yellow]",
            chain.as_of.isoformat(timespec="seconds") if chain else "[yellow]none[/yellow]",
        )
    console.print(data_table)

    if missing:
        console.print(
            f"[yellow]{missing} symbol(s) have no stored quote.[/yellow] They would be "
            f"rejected as DATA_UNAVAILABLE. Collect data first: "
            f"'data collect --symbol <SYMBOL>'."
        )
    console.print("[green]PASS[/green]  Configuration is valid. No orders were submitted.")


@universe_app.command("run")
def universe_run_command(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant; rebuild the universe as it was then."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run everything but do not persist. Never reaches a broker either way.",
        ),
    ] = False,
) -> None:
    """Select the research universe. (writes a universe run; submits no orders)

    Reads stored data only. It never collects, never connects to a broker and
    has no reachable order path.
    """
    _universe_run(as_of=as_of, dry_run=dry_run)


@universe_app.command("history")
def universe_history(
    limit: Annotated[int, typer.Option("--limit", help="Maximum runs to show.")] = 20,
) -> None:
    """Show past universe runs. (read-only)

    History is append-only: a run is never overwritten by a later one, so a
    past research decision stays explainable.
    """
    service = _universe_service()
    entries = service.history(limit=limit)
    if not entries:
        console.print("[yellow]UNAVAILABLE[/yellow]  no universe runs have been recorded yet.")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Universe runs", show_header=True, header_style="bold")
    for column in ("Generated", "Run id", "As of", "Status", "Method", "Selected", "Model"):
        table.add_column(column)
    for entry in entries:
        style = "green" if entry.status == "SUCCESS" else "yellow"
        table.add_row(
            entry.generated_at.isoformat(timespec="seconds"),
            entry.run_id,
            entry.as_of.isoformat(timespec="seconds"),
            f"[{style}]{entry.status}[/{style}]",
            entry.selection_method,
            str(entry.selected_count),
            entry.model_name or "-",
        )
    console.print(table)
    console.print(f"{len(entries)} run(s). Nothing here is ever rewritten.")


@universe_app.command("explain")
def universe_explain(
    run_id: Annotated[str, typer.Option("--run-id", help="The run to explain.")],
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Explain why each asset was selected or rejected in a run. (read-only)"""
    from trading_system.universe.report import render_report, render_summary

    service = _universe_service()
    result = service.get(run_id)
    if result is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no universe run with id {run_id!r}.")
        raise typer.Exit(code=EXIT_OK)

    if symbol is None:
        console.print()
        console.print(render_report(result))
        return

    wanted = symbol.strip().upper()
    console.print()
    console.print(render_summary(result))
    console.print()

    selected = next((a for a in result.selected_assets if a.symbol == wanted), None)
    if selected is not None:
        console.print(f"[green]{wanted} was SELECTED[/green] at rank {selected.rank}")
        console.print(f"  reasons      : {', '.join(r.value for r in selected.reasons)}")
        console.print(f"  confidence   : {selected.confidence.value}")
        console.print(f"  score        : {_or_dash(selected.selection_score)}")
        console.print(f"  optionability: {selected.optionability.value}")
        console.print(f"  price        : {_or_dash(selected.reference_price)}")
        console.print(f"  volume       : {_or_dash(selected.underlying_volume)} (underlying)")
        console.print(
            f"  data quality : {selected.data_quality.classification.value} "
            f"(research_usable={selected.data_quality.research_usable})"
        )
        if selected.source is not None:
            console.print(f"  provider     : {selected.source.provider}")
            console.print(f"  snapshots    : {', '.join(selected.source.snapshot_ids) or 'none'}")
        if selected.rationale:
            console.print(f"  rationale    : {selected.rationale}")
        return

    rejected = next((a for a in result.rejected_assets if a.symbol == wanted), None)
    if rejected is not None:
        console.print(f"[yellow]{wanted} was REJECTED[/yellow]")
        console.print(f"  reason       : {rejected.reason.value}")
        console.print(f"  eligibility  : {rejected.deterministic_eligibility.value}")
        console.print(f"  optionability: {rejected.optionability.value}")
        if rejected.detail:
            console.print(f"  detail       : {rejected.detail}")
        if rejected.source is not None:
            console.print(f"  snapshots    : {', '.join(rejected.source.snapshot_ids) or 'none'}")
        return

    console.print(f"[yellow]{wanted} was not considered in run {run_id}.[/yellow]")


# ---------------------------------------------------------------------------
# research
#
# Every command here is read-only with respect to the broker. The research
# service constructs no broker, opens no connection and has no order path: it
# consumes stored data and a stored universe through repositories, and writes a
# run record. The zero-order property is therefore structural, not a check
# performed at the end.
# ---------------------------------------------------------------------------
def _research_service() -> ResearchService:
    """Build the research layer from configuration, or fail with a diagnostic."""
    from trading_system.research.service import ResearchService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return ResearchService(settings=settings, config=config)


def _research_run(
    *,
    as_of: str | None,
    dry_run: bool,
    symbols: list[str] | None,
    universe_run_id: str | None,
) -> None:
    """Execute one research run and print its report."""
    from trading_system.domain.enums import ResearchStatus
    from trading_system.research.report import render_run

    service = _research_service()
    instant = _parse_instant(as_of) if as_of else None

    run = service.run(
        as_of=instant,
        dry_run=dry_run,
        symbols=symbols,
        universe_run_id=universe_run_id,
    )
    console.print()
    console.print(render_run(run.result, verbose=True))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  Nothing was persisted. Authoritative history is unchanged."
        )
        _print_dry_run_inputs(run)
    else:
        console.print(f"\n[green]Stored[/green] run {run.result.run_id}")

    if run.result.status is not ResearchStatus.SUCCESS:
        _fail(
            f"research ended as {run.result.status.value}; no outlook was produced "
            f"and no downstream stage may consume this run"
        )


def _print_dry_run_inputs(run: object) -> None:
    """Show what each underlying was actually shown, for inspection.

    Only on a dry run: the inputs are not persisted inside the run record,
    because the report already names the snapshots it rests on and a second
    stored copy of the evidence could drift from the first.
    """
    inputs = getattr(run, "inputs", {})
    if not inputs:
        return
    console.print("\n[bold]RESEARCH INPUTS[/bold] (what each underlying was shown)")
    for symbol, research_input in sorted(inputs.items()):
        console.print(
            f"  {symbol:<8} facts={len(research_input.all_evidence)} "
            f"(usable {research_input.usable_evidence_count}) "
            f"news={len(research_input.news)} events={len(research_input.events)} "
            f"filings={len(research_input.regulatory_events)} "
            f"fundamentals={len(research_input.fundamentals)} "
            f"snapshots={len(research_input.data_snapshot_ids)}"
        )
        gaps = research_input.data_quality_summary.gaps
        if gaps:
            console.print(f"           gaps: {', '.join(g.value for g in gaps)}")
        if research_input.limits.truncated:
            console.print(f"           truncated: {', '.join(research_input.limits.truncated)}")


@research_app.command("run")
def research_run_command(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant; research as it was then."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run everything but do not persist. Never reaches a broker either way.",
        ),
    ] = False,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
    universe_run_id: Annotated[
        str | None,
        typer.Option("--universe-run-id", help="Research a specific universe run."),
    ] = None,
) -> None:
    """Research the selected universe. (writes research reports; submits no orders)

    Reads stored data and a stored universe run only. It never collects, never
    connects to a broker and has no reachable order path. ``--symbol`` narrows
    the run to part of the universe; it cannot widen it, because research
    consumes a universe rather than selecting one.
    """
    _research_run(
        as_of=as_of,
        dry_run=dry_run,
        symbols=list(symbol) if symbol else None,
        universe_run_id=universe_run_id,
    )


@research_app.command("show")
def research_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest (or a named) research run. (read-only)"""
    from trading_system.research.report import render_report, render_run

    service = _research_service()
    result = service.get(run_id) if run_id else service.latest()

    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no research run with id {run_id!r}."
                if run_id
                else "no research has been run yet. Run 'research run' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    if symbol is None:
        console.print(render_run(result, verbose=True))
        return

    report = result.report(symbol)
    if report is None:
        console.print(
            f"[yellow]{symbol.strip().upper()} was not researched in run {result.run_id}.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)
    console.print(render_report(report))


@research_app.command("explain")
def research_explain(
    symbol: Annotated[str, typer.Option("--symbol", help="The underlying to explain.")],
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
) -> None:
    """Explain one underlying's outlook and everything it rests on. (read-only)

    Every conclusion is traceable to the exact evidence, source and data
    snapshot that supported it at the research instant.
    """
    from trading_system.research.report import render_report

    service = _research_service()
    result = service.get(run_id) if run_id else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (f"no research run with id {run_id!r}." if run_id else "no research run exists yet.")
        )
        raise typer.Exit(code=EXIT_OK)

    report = result.report(symbol)
    if report is None:
        console.print(
            f"[yellow]{symbol.strip().upper()} was not researched in run {result.run_id}.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_report(report))


@research_app.command("history")
def research_history(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="One underlying's report history."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum entries to show.")] = 20,
) -> None:
    """Show past research runs, or one underlying's history. (read-only)

    History is append-only: a report is never overwritten by a later one, so
    "what did we believe then, and did the view change" stays answerable.
    """
    service = _research_service()

    if symbol is not None:
        entries = service.symbol_history(symbol, limit=limit)
        if not entries:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no research history for {symbol.strip().upper()}."
            )
            raise typer.Exit(code=EXIT_OK)

        table = Table(
            title=f"Research history — {symbol.strip().upper()}",
            show_header=True,
            header_style="bold",
        )
        for column in ("Generated", "Run id", "As of", "Status", "Hypothesis", "Conf.", "Evidence"):
            table.add_column(column)
        for entry in entries:
            style = "green" if entry.status == "SUCCESS" else "yellow"
            table.add_row(
                entry.generated_at.isoformat(timespec="seconds"),
                entry.run_id,
                entry.as_of.isoformat(timespec="seconds"),
                f"[{style}]{entry.status}[/{style}]",
                entry.hypothesis or "-",
                entry.confidence or "-",
                str(entry.evidence_count),
            )
        console.print(table)
        console.print(f"{len(entries)} report(s). Nothing here is ever rewritten.")
        return

    runs = service.history(limit=limit)
    if not runs:
        console.print("[yellow]UNAVAILABLE[/yellow]  no research runs have been recorded yet.")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Research runs", show_header=True, header_style="bold")
    for column in ("Generated", "Run id", "As of", "Status", "Outlooks", "Failed", "Model"):
        table.add_column(column)
    for run in runs:
        style = "green" if run.status == "SUCCESS" else "yellow"
        table.add_row(
            run.generated_at.isoformat(timespec="seconds"),
            run.run_id,
            run.as_of.isoformat(timespec="seconds"),
            f"[{style}]{run.status}[/{style}]",
            str(run.succeeded),
            str(run.failed),
            run.model_name or "-",
        )
    console.print(table)
    console.print(f"{len(runs)} run(s). Nothing here is ever rewritten.")


@research_app.command("validate")
def research_validate(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Re-check a stored run instead of the configuration."),
    ] = None,
) -> None:
    """Validate the research configuration, or re-check a stored run. (read-only)

    Without ``--run-id`` this checks that the configuration loads, that a
    universe is available to research, and which of its underlyings actually
    have data stored. With one, it re-checks the stored run's own invariants:
    that no report cites a snapshot it does not name, that every successful
    report carries an invalidation condition, and that no failed report smuggled
    an outlook through.
    """
    if run_id is not None:
        _research_validate_run(run_id)
        return

    service = _research_service()
    settings = _load_settings()
    config = load_config(settings.config_dir)
    research = config.research

    console.print("\n[bold]RESEARCH CONFIGURATION[/bold]")
    console.print(f"Config version : {research.config_version}")
    console.print(f"Horizon        : {research.horizon.min_days}-{research.horizon.max_days} days")
    console.print(
        f"Agent          : {'enabled' if research.agent.enabled else 'disabled'} "
        f"({research.agent.model_provider}/{research.agent.model_name}, "
        f"prompt {research.agent.prompt_version})"
    )
    console.print(
        "Fallback       : [green]fail closed[/green] — no outlook is ever synthesised "
        "in place of an unreachable model"
    )
    console.print(
        f"Source policy  : {config.sources.config_version}, "
        f"min {config.sources.min_sources_per_report} source(s) per report"
    )

    table = Table(title="Limits and windows", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    limits, window = research.limits, research.window
    for name, value in (
        ("max assets per run", str(limits.max_assets_per_run)),
        ("max evidence items", str(limits.max_evidence_items)),
        ("max news items", str(limits.max_news_items)),
        ("max events", str(limits.max_events)),
        ("max input characters", str(limits.max_input_characters)),
        ("news lookback (days)", str(window.news_lookback_days)),
        ("event lookahead (days)", str(window.event_lookahead_days)),
        ("historical lookback (days)", str(window.historical_lookback_days)),
        ("deduplication", "on" if research.deduplication.enabled else "off"),
        ("HIGH needs evidence items", str(research.confidence.min_evidence_items_for_high)),
        ("HIGH needs tier", research.confidence.min_source_tier_for_high.value),
    ):
        table.add_row(name, value)
    console.print(table)

    if research.agent.enabled and settings.anthropic_api_key is None:
        console.print(
            "[yellow]ANTHROPIC_API_KEY is not set[/yellow]; every symbol would end as "
            "AI_UNAVAILABLE."
        )

    universe = service.universe()
    if universe is None:
        console.print(
            "\n[yellow]No universe has been selected yet.[/yellow] "
            "Research consumes a universe; run 'universe run' first."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print(
        f"\nUniverse       : {universe.run_id} ({universe.status.value}), "
        f"{len(universe.selected_assets)} underlying(s): "
        f"{', '.join(universe.symbols) or 'none'}"
    )
    if universe.symbols:
        _print_research_readiness(service, universe.symbols)
    console.print("[green]PASS[/green]  Configuration is valid. No orders were submitted.")


def _print_research_readiness(service: ResearchService, symbols: list[str]) -> None:
    """Which data types each universe symbol actually has stored."""
    from trading_system.domain.enums import DataType

    table = Table(title="Stored research data", show_header=True, header_style="bold")
    for column in ("Symbol", "Quote", "News", "Events", "Filings", "Fundamentals", "Chain"):
        table.add_column(column)
    kinds = (
        DataType.MARKET_QUOTE,
        DataType.NEWS_ARTICLE,
        DataType.CORPORATE_EVENT,
        DataType.REGULATORY_EVENT,
        DataType.FUNDAMENTAL_SNAPSHOT,
        DataType.OPTION_CHAIN,
    )
    for symbol in symbols:
        cells = []
        for kind in kinds:
            snapshot = service.data_repository.get_latest(kind, symbol)
            cells.append(
                snapshot.as_of.isoformat(timespec="seconds")
                if snapshot
                else "[yellow]none[/yellow]"
            )
        table.add_row(symbol, *cells)
    console.print(table)
    console.print(
        "A missing data type is not an error — it is recorded as an explicit gap on the "
        "report and constrains the confidence the agent is allowed to state."
    )


def _research_validate_run(run_id: str) -> None:
    """Re-check a stored run's own invariants and report every finding."""
    from trading_system.domain.enums import ResearchStatus

    service = _research_service()
    result = service.get(run_id)
    if result is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no research run with id {run_id!r}.")
        raise typer.Exit(code=EXIT_OK)

    console.print(f"\n[bold]VALIDATING[/bold] research run {result.run_id}")
    problems: list[str] = []

    for report in result.reports:
        prefix = f"{report.symbol}"
        if report.status is ResearchStatus.SUCCESS:
            if not report.invalidation_conditions:
                problems.append(f"{prefix}: a successful report states no invalidation condition")
            if not report.evidence:
                problems.append(f"{prefix}: a successful report cites no evidence")
            if report.horizon_days is not None and not report.horizon.contains(report.horizon_days):
                problems.append(
                    f"{prefix}: horizon_days {report.horizon_days} is outside "
                    f"{report.horizon.min_days}-{report.horizon.max_days}"
                )
        elif report.hypothesis is not None:
            problems.append(f"{prefix}: a {report.status.value} report carries a hypothesis")

        named = set(report.input_snapshot_ids)
        for item in report.evidence:
            if item.source.snapshot_id not in named:
                problems.append(
                    f"{prefix}: evidence {item.evidence_id} cites snapshot "
                    f"{item.source.snapshot_id}, which the report does not list"
                )

    table = Table(show_header=True, header_style="bold")
    for column in ("Symbol", "Status", "Hypothesis", "Evidence", "Invalidations", "Snapshots"):
        table.add_column(column)
    for report in sorted(result.reports, key=lambda r: r.symbol):
        style = "green" if report.succeeded else "yellow"
        table.add_row(
            report.symbol,
            f"[{style}]{report.status.value}[/{style}]",
            report.hypothesis.value if report.hypothesis else "-",
            str(len(report.evidence)),
            str(len(report.invalidation_conditions)),
            str(len(report.input_snapshot_ids)),
        )
    console.print(table)

    if problems:
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
        _fail(f"{len(problems)} problem(s) found in stored run {result.run_id}")
    console.print(
        f"[green]PASS[/green]  {len(result.reports)} report(s) satisfy the stored-run "
        f"invariants. No orders were submitted."
    )


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
