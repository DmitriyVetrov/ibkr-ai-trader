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
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
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

    from trading_system.allocation.service import AllocationService
    from trading_system.cleanup.service import CleanupService
    from trading_system.data.collectors import CollectionReport
    from trading_system.data.service import DataService
    from trading_system.domain.enums import DataType
    from trading_system.execution.service import ExecutionService
    from trading_system.exit.service import ExitService
    from trading_system.operations.service import OperationsService
    from trading_system.pnl.service import PnLService
    from trading_system.positions.service import PositionService
    from trading_system.readiness.service import ReadinessService
    from trading_system.reconciliation.service import ReconciliationService
    from trading_system.research.service import ResearchService
    from trading_system.reservations.service import ReservationService
    from trading_system.strategies.service import ContractSelectionService, StrategyService
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
strategy_app = typer.Typer(
    help=(
        "Choose an option strategy for each researched underlying, and inspect "
        "the decisions. Every command here is read-only with respect to the "
        "broker and submits zero orders. The agent selects a strategy, never a "
        "contract."
    ),
    no_args_is_help=True,
)
contract_app = typer.Typer(
    help=(
        "Select concrete option contracts for strategy decisions, and inspect "
        "the selections. Deterministic: no model is involved. Every command "
        "here is read-only with respect to the broker and submits zero orders."
    ),
    no_args_is_help=True,
)
risk_app = typer.Typer(
    help=(
        "Evaluate and inspect deterministic risk. No model is involved and no "
        "broker is constructed: the engine reads a stored account snapshot. "
        "Every command here submits zero orders."
    ),
    no_args_is_help=True,
)
allocation_app = typer.Typer(
    help=(
        "Allocate campaign capital across purchase candidates, and inspect the "
        "authorisations. Deterministic: no model decides a quantity or an "
        "amount of money. An authorisation is not an order; every command here "
        "submits zero orders."
    ),
    no_args_is_help=True,
)
execution_app = typer.Typer(
    help=(
        "Submit approved authorisations to the broker, and inspect what was "
        "sent. The ONLY command group in this system that can place an order. "
        "It requires both execution.enabled in configuration and an explicit "
        "--confirm; --dry-run builds and shows an order without opening a "
        "broker connection at all. (mutates broker state)"
    ),
    no_args_is_help=True,
)
positions_app = typer.Typer(
    help=(
        "Inspect what the broker holds and what this system believes it holds. "
        "Every command distinguishes BROKER OBSERVED positions from INTERNAL "
        "EXPECTED positions, because they are different claims. Read-only with "
        "respect to the broker; submits zero orders."
    ),
    no_args_is_help=True,
)
reservations_app = typer.Typer(
    help=(
        "Inspect the campaign capital committed to authorisations, and what "
        "became of it. Committed is not invested, and UNKNOWN is not released: "
        "an execution whose outcome was never learned keeps its capital. "
        "Submits zero orders."
    ),
    no_args_is_help=True,
)
exit_app = typer.Typer(
    help=(
        "Decide whether an already-open position should be closed, and inspect "
        "the decisions. Deterministic: no model is consulted anywhere in this "
        "group. Evaluation NEVER submits an order — closing a position needs "
        "execution.enabled AND an explicit --confirm, exactly as opening one "
        "does. (evaluation is read-only; 'exit run --confirm' mutates broker state)"
    ),
    no_args_is_help=True,
)
reconciliation_app = typer.Typer(
    help=(
        "Compare internal records against broker reality. The broker wins every "
        "time, and comparison REPORTS discrepancies — it never repairs one, "
        "adopts a position, cancels an order or places a corrective trade. "
        "Every command here submits zero orders EXCEPT cleanup-orphans, which "
        "closes explicitly named pre-existing holdings and needs --confirm."
    ),
    no_args_is_help=True,
)
reports_app = typer.Typer(
    help="Generate and inspect reports. (read-only)",
    no_args_is_help=True,
)

ops_app = typer.Typer(
    help=(
        "Operations: health, the scheduler, jobs, alerts and metrics. "
        "Everything here READS, notifies or orchestrates; nothing decides a trade. "
        "(read-only unless a command says otherwise)"
    ),
    no_args_is_help=True,
)

pnl_app = typer.Typer(
    help=(
        "Realised profit and loss, from broker-confirmed fills only. "
        "Never from a limit price, a reference price or an estimate. (read-only)"
    ),
    no_args_is_help=True,
)

readiness_app = typer.Typer(
    help=(
        "Live-trading readiness: the acceptance gate, and the evidence behind it. "
        "REPORTS ONLY — nothing here changes TRADING_MODE, execution.enabled, "
        "IBKR_READ_ONLY or either live guard. "
        "(read-only, except 'paper' which SUBMITS a real paper order)"
    ),
    no_args_is_help=True,
)

app.add_typer(run_app, name="run")
app.add_typer(test_app, name="test")
app.add_typer(data_app, name="data")
app.add_typer(universe_app, name="universe")
app.add_typer(research_app, name="research")
app.add_typer(strategy_app, name="strategy")
app.add_typer(contract_app, name="contract")
app.add_typer(risk_app, name="risk")
app.add_typer(allocation_app, name="allocation")
app.add_typer(execution_app, name="execution")
app.add_typer(positions_app, name="positions")
app.add_typer(reservations_app, name="reservations")
app.add_typer(exit_app, name="exit")
app.add_typer(reconciliation_app, name="reconciliation")
app.add_typer(reports_app, name="reports")
app.add_typer(ops_app, name="ops")
app.add_typer(pnl_app, name="pnl")
app.add_typer(readiness_app, name="readiness")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _not_implemented(feature: str, milestone: str, hint: str | None = None) -> None:
    """Report honestly that a command exists but is not built yet.

    ``hint`` names a command that already does part of the job, where one
    exists. Pointing at it is not the same as pretending this command works —
    the exit code still says "not built".
    """
    err_console.print(
        f"[yellow]NOT IMPLEMENTED[/yellow]  {feature}\n"
        f"This command is defined by the specification but is delivered in "
        f"[bold]{milestone}[/bold].\n"
        f"No broker connection was attempted and no data was fabricated."
        + (f"\nAvailable today: {hint}" if hint else "")
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
    _bootstrap_telemetry()


def _bootstrap_telemetry() -> None:
    """Start telemetry, if it is configured. Never fails a command.

    Runs before every command. Four outcomes are possible — disabled, SDK
    absent, misconfigured, active — and all four produce identical trading
    behaviour, which is the property ``tests/observability/`` asserts by
    running the same operations under each and comparing stored artifacts.

    Wrapped in its own ``try`` on top of the runtime's: a CLI that failed to
    start because a collector was unreachable would be telemetry deciding
    whether the system runs, which is precisely inverted.
    """
    try:
        from trading_system.observability.logging import install_correlation
        from trading_system.observability.runtime import configure_telemetry

        settings = Settings()
        config = load_config(settings.config_dir)
        observability = settings.resolved_observability(config.observability)
        configure_telemetry(config=observability, service_version=__version__)
        if observability.logging.correlate_traces:
            install_correlation(
                service_name=observability.service_name,
                trading_mode=settings.trading_mode.value,
                include_service_context=observability.logging.include_service_context,
                export_otlp=observability.logging.export_otlp,
            )
    except Exception:
        return


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
def opportunities() -> None:
    """Show the current ranked opportunities. (read-only)"""
    _not_implemented(
        "opportunity ranking across the whole discovery loop",
        "Milestone 8 (execution)",
        hint="'allocation show' lists the ranked, risk-evaluated candidates of the latest run.",
    )


@app.command()
def reconcile(
    simulated: SimulatedOption = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compare and print; write nothing at all."),
    ] = False,
) -> None:
    """Reconcile against the broker and persist the result. (mutates state)

    An alias for ``reconciliation run``, kept because the specification names
    this command. It reads broker state and writes this system's own records;
    it never places, cancels or modifies an order, and it submits zero.
    """
    _run_reconciliation(simulated=simulated, dry_run=dry_run)


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
    _not_implemented(
        "the end-to-end opportunity scan",
        "Milestone 8 (execution)",
        hint=(
            "run the stages in order: 'universe run', 'research run', 'strategy run', "
            "'contract select', 'allocation run'."
        ),
    )


@run_app.command("position-monitor")
def run_position_monitor() -> None:
    """Run the scheduled position-monitor job once. (mutates local state)

    The same job the scheduler fires: capture what the broker holds, evaluate
    the exit policy against it, and submit nothing. Submitting is a separate
    job (``exit_management``) behind two switches, for the same reason
    ``exit evaluate`` and ``exit run --confirm`` are separate commands.
    """
    _run_scheduled_job("position_monitor")


@run_app.command("exit-management")
def run_exit_management() -> None:
    """Run the scheduled exit-submission job once. (CAN SUBMIT AN ORDER)

    Refuses unless **both** switches are on: ``authorize_exits`` on this job in
    ``config/schedules.yaml`` *and* ``execution.enabled`` in
    ``config/execution.yaml``. Neither implies the other. The decision is
    Milestone 10's and the order is Milestone 8's; this job chooses nothing.
    """
    _run_scheduled_job("exit_management")


@run_app.command("pnl-settlement")
def run_pnl_settlement() -> None:
    """Run the scheduled settlement job once. (mutates local state)

    Computes realised results for confirmed-closed positions and returns their
    capital to the campaign. Submits no order — this package holds no broker.
    """
    _run_scheduled_job("pnl_settlement")


@run_app.command("operational-health")
def run_operational_health() -> None:
    """Run the scheduled health job once. (mutates local state)

    Records trading health and observability health, separately, and evaluates
    the alert rules. Reads only; notifies only.
    """
    _run_scheduled_job("operational_health")


def _run_scheduled_job(name: str) -> None:
    """Invoke one registered job through the scheduler's own guards.

    Deliberately *through* the scheduler rather than by calling the service
    directly: a job run by hand must not be able to do something the cadence
    would have refused. The market calendar, the enabled switch and the
    duplicate check against the stored run for this instant all still apply.
    """
    from trading_system.domain.enums import JobStatus

    scheduler = _scheduler()
    record = scheduler.run_job(name)
    style = {
        JobStatus.SUCCESS: "green",
        JobStatus.SKIPPED: "yellow",
        JobStatus.FAILED: "red",
        JobStatus.UNKNOWN: "yellow",
        JobStatus.BLOCKED: "yellow",
    }.get(record.status, "white")
    console.print(f"\n[{style}]{record.status.value}[/{style}]  {record.job}")
    if record.skip_reason is not None:
        console.print(f"Reason  : {record.skip_reason.value}")
    console.print(f"Summary : {record.summary or '-'}")
    if record.error_type:
        console.print(f"[red]{record.error_type}[/red]: {record.error_message}")
    submitted = record.orders_submitted
    console.print(f"Orders submitted : [{'green' if submitted == 0 else 'yellow'}]{submitted}[/]")
    if record.status is JobStatus.FAILED:
        raise typer.Exit(code=EXIT_ERROR)


@run_app.command("thesis-monitor")
def run_thesis_monitor() -> None:
    """Re-check whether entry theses still hold. (mutates state)"""
    _not_implemented(
        "the separate thesis monitor (VALID / WEAKENING / INVALIDATED / UNKNOWN)",
        "a later milestone",
        hint=(
            "'exit evaluate' checks each position's stored invalidation conditions "
            "deterministically as part of the exit policy. What is absent is the judgement "
            "that a thesis has WEAKENED without being falsified, and nothing fabricates one"
        ),
    )


@run_app.command("reconciliation")
def run_reconciliation() -> None:
    """Run the scheduled reconciliation job once. (mutates local state)

    Compares internal records against broker reality and reports. It cannot
    place, cancel or modify an order, and the submitted-order count is read off
    the broker rather than asserted.
    """
    _run_scheduled_job("reconciliation")


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
    """Run the scheduled end-of-day roll-up once. (mutates local state)

    Aggregates the session's realised results from the profit-and-loss ledger.
    A day on which nothing closed produces no roll-up rather than a zero: a
    flat day and a day with no trades are different facts.
    """
    _run_scheduled_job("end_of_day_report")


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

        console.print(f"Contract id: {_or_dash(snapshot.contract_id)}")
        console.print(f"Data origin: {snapshot.origin.value}")
        console.print(f"Quality    : {snapshot.data_quality.value}")
        console.print(f"As of      : {snapshot.as_of.isoformat()}")
        console.print(f"Retrieved  : {_now().isoformat()}")
        console.print(f"Bid/Ask    : {_or_dash(snapshot.bid)} / {_or_dash(snapshot.ask)}")
        console.print(f"Last/Close : {_or_dash(snapshot.last)} / {_or_dash(snapshot.close)}")

        # Both volume fields, side by side and unmodified. This is the
        # mid-session validation surface for the tick-74 finding: session
        # volume is displayed exactly as the broker sent it, however
        # implausible, next to the tick-21 average the liquidity floor reads.
        console.print(
            f"Volume     : {_or_dash(snapshot.volume)} "
            f"(session, IBKR tick 8/74 — raw, never rescaled)"
        )
        console.print(
            f"Avg volume : {_or_dash(snapshot.average_daily_volume)} "
            f"(90d average, IBKR tick 21 via generic tick 165)"
        )
        if snapshot.average_daily_volume is None:
            console.print(
                "[yellow]note[/yellow]  no average daily volume was reported; the "
                "universe liquidity floor rejects rather than falling back to session volume"
            )

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
    """Show the latest stored strategy decision for one underlying. (read-only)

    Inspection, not execution: it reads what was decided and why. To produce a
    new decision — which calls a model — use ``strategy run``.
    """
    from trading_system.strategies.report import render_decision

    service = _strategy_service()
    result = service.latest()
    decision = result.decision(ticker) if result is not None else None
    if decision is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no stored strategy decision for "
            f"{ticker.strip().upper()}. Run 'strategy run' first; this command inspects "
            f"decisions rather than making one."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_decision(decision))
    console.print("\nOrders submitted: 0  (strategy selection has no order path)")


@test_app.command("contract-selection")
def test_contract_selection(
    ticker: Annotated[str, typer.Option(help="Underlying to evaluate.")],
) -> None:
    """Show the latest stored contract selection for one underlying. (read-only)

    Inspection, not execution. To produce a new selection use
    ``contract select`` — which is deterministic and consults no model.
    """
    from trading_system.strategies.report import render_selection

    service = _contract_service()
    result = service.latest()
    selection = result.selection(ticker) if result is not None else None
    if selection is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no stored contract selection for "
            f"{ticker.strip().upper()}. Run 'contract select' first; this command inspects "
            f"selections rather than making one."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_selection(selection))
    console.print("\nOrders submitted: 0  (contract selection has no order path)")


@test_app.command("allocation")
def test_allocation(
    ticker: Annotated[
        str | None,
        typer.Option("--ticker", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest stored allocation decisions. (read-only)

    Inspection, not execution: it reads what was authorised and why. To produce
    new authorisations use ``allocation run`` — which is deterministic and
    consults no model.
    """
    from trading_system.allocation.report import render_allocation, render_allocation_run

    service = _allocation_service()
    result = service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no stored allocation run. Run 'allocation run' "
            "first; this command inspects authorisations rather than making them."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    if ticker is None:
        console.print(render_allocation_run(result, verbose=True))
    else:
        allocation = result.allocation(ticker)
        if allocation is None:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no stored allocation for {ticker.strip().upper()}."
            )
            raise typer.Exit(code=EXIT_OK)
        console.print(render_allocation(allocation))
    console.print("\nOrders submitted: 0  (allocation authorises capital; it places no orders)")


@test_app.command("risk")
def test_risk(
    ticker: Annotated[
        str | None,
        typer.Option("--ticker", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest stored risk verdicts, check by check. (read-only)

    Inspection, not execution. To evaluate a contract run without persisting
    anything, use ``risk evaluate``.
    """
    from trading_system.allocation.report import render_evaluation

    service = _allocation_service()
    result = service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no stored risk verdict. Run 'allocation run' "
            "first; this command inspects verdicts rather than making them."
        )
        raise typer.Exit(code=EXIT_OK)

    wanted = ticker.strip().upper() if ticker else None
    console.print()
    for allocation in result.allocations:
        if wanted and allocation.symbol != wanted:
            continue
        console.print(render_evaluation(allocation.risk_evaluation))
        console.print()
    console.print("Orders submitted: 0  (the risk engine has no order path)")


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
        console.print(
            f"  avg volume   : {_or_dash(selected.average_daily_volume)} "
            f"(underlying, 90d average — the liquidity floor reads this)"
        )
        console.print(
            f"  volume       : {_or_dash(selected.underlying_volume)} (underlying, session)"
        )
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
# strategy
#
# Every command here is read-only with respect to the broker. The strategy
# service constructs no broker, opens no connection and has no order path: it
# consumes a stored research run and stored data, and writes a decision record.
# A strategy decision is a proposal, never an order.
# ---------------------------------------------------------------------------
def _strategy_service() -> StrategyService:
    """Build the strategy layer from configuration, or fail with a diagnostic."""
    from trading_system.strategies.service import StrategyService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return StrategyService(settings=settings, config=config)


def _contract_service() -> ContractSelectionService:
    """Build the contract-selection layer, or fail with a diagnostic."""
    from trading_system.strategies.service import ContractSelectionService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return ContractSelectionService(settings=settings, config=config)


@strategy_app.command("run")
def strategy_run_command(
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            "--research-run-id",
            help="The research run to act on. Defaults to the most recent.",
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="ISO-8601 instant recorded on the run."),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run everything but do not persist. Never reaches a broker either way.",
        ),
    ] = False,
) -> None:
    """Choose a strategy per underlying. (writes decisions; submits no orders)

    Reads a stored research run only. It never re-researches, never connects to
    a broker and has no reachable order path. ``NO_TRADE`` is a first-class
    outcome, and a decision is a proposal — no contract is selected here.
    """
    from trading_system.domain.enums import StrategySelectionStatus
    from trading_system.strategies.report import render_strategy_run

    service = _strategy_service()
    instant = _parse_instant(as_of) if as_of else None

    run = service.run(
        as_of=instant,
        dry_run=dry_run,
        symbols=list(symbol) if symbol else None,
        research_run_id=run_id,
    )
    console.print()
    console.print(render_strategy_run(run.result, verbose=True))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  Nothing was persisted. Authoritative history is unchanged."
        )
    else:
        console.print(f"\n[green]Stored[/green] run {run.result.run_id}")

    if run.result.status is not StrategySelectionStatus.SUCCESS:
        _fail(
            f"strategy selection ended as {run.result.status.value}; no decision was "
            f"reached and no downstream stage may consume this run"
        )


@strategy_app.command("show")
def strategy_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest (or a named) strategy run. (read-only)"""
    from trading_system.strategies.report import render_decision, render_strategy_run

    service = _strategy_service()
    result = service.get(run_id) if run_id else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no strategy run with id {run_id!r}."
                if run_id
                else "no strategy run exists yet. Run 'strategy run' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    if symbol is None:
        console.print(render_strategy_run(result, verbose=True))
        return

    decision = result.decision(symbol)
    if decision is None:
        console.print(
            f"[yellow]{symbol.strip().upper()} was not decided in run {result.run_id}.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)
    console.print(render_decision(decision))


@strategy_app.command("history")
def strategy_history(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="One underlying's decision history."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum entries to show.")] = 20,
) -> None:
    """Show past strategy runs, or one underlying's decisions. (read-only)

    History is append-only: a decision is never overwritten by a later one, so
    "what did we decide then, and did it change" stays answerable.
    """
    service = _strategy_service()

    if symbol is not None:
        entries = service.symbol_history(symbol, limit=limit)
        if not entries:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no strategy history for {symbol.strip().upper()}."
            )
            raise typer.Exit(code=EXIT_OK)
        table = Table(
            title=f"Strategy history — {symbol.strip().upper()}",
            show_header=True,
            header_style="bold",
        )
        for column in ("Generated", "Run id", "Status", "Action", "Strategy", "Hypothesis"):
            table.add_column(column)
        for entry in entries:
            style = "green" if entry.status == "SUCCESS" else "yellow"
            table.add_row(
                entry.generated_at.isoformat(timespec="seconds"),
                entry.run_id,
                f"[{style}]{entry.status}[/{style}]",
                entry.action,
                entry.strategy or "-",
                entry.hypothesis or "-",
            )
        console.print(table)
        console.print(f"{len(entries)} decision(s). Nothing here is ever rewritten.")
        return

    runs = service.history(limit=limit)
    if not runs:
        console.print("[yellow]UNAVAILABLE[/yellow]  no strategy runs have been recorded yet.")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Strategy runs", show_header=True, header_style="bold")
    for column in ("Generated", "Run id", "As of", "Status", "Proposed", "No trade", "Model"):
        table.add_column(column)
    for run in runs:
        style = "green" if run.status == "SUCCESS" else "yellow"
        table.add_row(
            run.generated_at.isoformat(timespec="seconds"),
            run.run_id,
            run.as_of.isoformat(timespec="seconds"),
            f"[{style}]{run.status}[/{style}]",
            str(run.proposed),
            str(run.no_trade),
            run.model_name or "-",
        )
    console.print(table)
    console.print(f"{len(runs)} run(s). Nothing here is ever rewritten.")


@strategy_app.command("validate")
def strategy_validate(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Re-check a stored run instead of the configuration."),
    ] = None,
) -> None:
    """Validate the strategy configuration, or re-check a stored run. (read-only)

    Without ``--run-id`` this prints the registry as it actually resolved —
    including the hypothesis mapping, which is derived from each strategy's own
    ``applicable_hypotheses`` rather than declared in a second place. With one,
    it re-checks the stored run's invariants: that no failed decision carries a
    strategy, that every proposal names an eligible one, and that no decision
    smuggled a contract into its rationale.
    """
    if run_id is not None:
        _strategy_validate_run(run_id)
        return

    service = _strategy_service()
    settings = _load_settings()
    config = load_config(settings.config_dir)
    stage = config.strategy

    console.print("\n[bold]STRATEGY CONFIGURATION[/bold]")
    console.print(f"Config version : {stage.config_version}")
    console.print(
        f"Agent          : {'enabled' if stage.agent.enabled else 'disabled'} "
        f"({stage.agent.model_provider}/{stage.agent.model_name}, "
        f"prompt {stage.agent.prompt_version})"
    )
    console.print(
        "Fallback       : [green]fail closed[/green] — no strategy is ever chosen in place "
        "of an unreachable model"
    )
    console.print(
        f"Risk window    : DTE {config.risk.dte_min}-{config.risk.dte_max}, "
        f"option price {config.risk.min_option_price_eur}-{config.risk.max_option_price_eur}, "
        f"spread <= {config.risk.max_bid_ask_spread_pct}%"
    )

    registry = service.registry
    table = Table(title="Strategy registry", show_header=True, header_style="bold")
    for column in ("Strategy", "Version", "Hypotheses", "Legs", "DTE", "Expiration", "Strikes"):
        table.add_column(column)
    for specification in registry.all():
        table.add_row(
            specification.strategy_id.value + ("" if specification.enabled else " (disabled)"),
            specification.version,
            ", ".join(h.value for h in specification.applicable_hypotheses),
            " + ".join(specification.structure.describe_legs()),
            f"{specification.dte_min}-{specification.dte_max}",
            specification.expiration_rule.value,
            ", ".join(leg.strike_policy.value for leg in specification.legs),
        )
    console.print(table)

    mapping = registry.hypothesis_map()
    console.print("\nHypothesis mapping (derived from each strategy, not declared twice):")
    for hypothesis, strategies in mapping.items():
        names = ", ".join(s.value for s in strategies) or "[yellow]NO_TRADE[/yellow]"
        console.print(f"  {hypothesis.value} -> {names}")

    if stage.agent.enabled and settings.anthropic_api_key is None:
        console.print(
            "\n[yellow]ANTHROPIC_API_KEY is not set[/yellow]; every symbol would end as "
            "AI_UNAVAILABLE."
        )

    research = service.research()
    if research is None:
        console.print(
            "\n[yellow]No research run exists yet.[/yellow] The strategy stage consumes an "
            "outlook; run 'research run' first."
        )
        raise typer.Exit(code=EXIT_OK)
    outlooks = [report.symbol for report in research.reports if report.succeeded]
    console.print(
        f"\nResearch       : {research.run_id} ({research.status.value}), "
        f"{len(outlooks)} outlook(s): {', '.join(outlooks) or 'none'}"
    )
    console.print("[green]PASS[/green]  Configuration is valid. No orders were submitted.")


def _strategy_validate_run(run_id: str) -> None:
    """Re-check a stored strategy run's own invariants."""
    from trading_system.domain.enums import StrategyAction, StrategySelectionStatus

    service = _strategy_service()
    result = service.get(run_id)
    if result is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no strategy run with id {run_id!r}.")
        raise typer.Exit(code=EXIT_OK)

    console.print(f"\n[bold]VALIDATING[/bold] strategy run {result.run_id}")
    problems: list[str] = []
    for decision in result.decisions:
        prefix = decision.symbol
        if decision.status is not StrategySelectionStatus.SUCCESS:
            if decision.selected_strategy is not None:
                problems.append(f"{prefix}: a {decision.status.value} decision names a strategy")
            continue
        if decision.action is StrategyAction.BUY:
            if decision.selected_strategy is None:
                problems.append(f"{prefix}: a BUY decision names no strategy")
            elif decision.selected_strategy not in decision.eligible_strategies:
                problems.append(
                    f"{prefix}: {decision.selected_strategy.value} was not among the "
                    f"eligible strategies recorded for the decision"
                )
            if not decision.rationale:
                problems.append(f"{prefix}: a BUY decision carries no rationale")
        elif decision.selected_strategy is not None:
            problems.append(f"{prefix}: a NO_TRADE decision names a strategy")

    table = Table(show_header=True, header_style="bold")
    for column in ("Symbol", "Status", "Action", "Strategy", "Eligible", "Reasons"):
        table.add_column(column)
    for decision in sorted(result.decisions, key=lambda d: d.symbol):
        style = "green" if decision.succeeded else "yellow"
        table.add_row(
            decision.symbol,
            f"[{style}]{decision.status.value}[/{style}]",
            decision.action.value,
            decision.selected_strategy.value if decision.selected_strategy else "-",
            str(len(decision.eligible_strategies)),
            str(len(decision.reasons)),
        )
    console.print(table)

    if problems:
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
        _fail(f"{len(problems)} problem(s) found in stored run {result.run_id}")
    console.print(
        f"[green]PASS[/green]  {len(result.decisions)} decision(s) satisfy the stored-run "
        f"invariants. No orders were submitted."
    )


# ---------------------------------------------------------------------------
# contract
#
# Deterministic end to end. The contract-selection service constructs no LLM
# client and no broker: it reads a stored chain through the repository and
# applies configured policy, so both "no model" and "zero orders" are
# structural rather than checks performed at the end.
# ---------------------------------------------------------------------------
@contract_app.command("select")
def contract_select_command(
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            "--strategy-run-id",
            help="The strategy run to select contracts for. Defaults to the most recent.",
        ),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run everything but do not persist."),
    ] = False,
) -> None:
    """Select contracts for a strategy run. (writes selections; submits no orders)

    Deterministic: no model is consulted, not once per selection and not once
    per strike. There is no ``--as-of`` — the instant comes from each decision,
    so a selection reconstructs exactly the data that was visible when the
    strategy was chosen.
    """
    from trading_system.domain.enums import ContractSelectionStatus
    from trading_system.strategies.report import render_contract_run

    service = _contract_service()
    run = service.select(
        dry_run=dry_run,
        strategy_run_id=run_id,
        symbols=list(symbol) if symbol else None,
    )
    console.print()
    console.print(render_contract_run(run.result, verbose=True))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  Nothing was persisted. Authoritative history is unchanged."
        )
    else:
        console.print(f"\n[green]Stored[/green] run {run.result.run_id}")

    if run.result.status is not ContractSelectionStatus.SUCCESS:
        _fail(
            f"contract selection ended as {run.result.status.value}; no contract was "
            f"selected. No contract is a valid outcome — nothing approximate is returned."
        )


@contract_app.command("show")
def contract_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest (or a named) contract selection run. (read-only)"""
    from trading_system.strategies.report import render_contract_run, render_selection

    service = _contract_service()
    result = service.get(run_id) if run_id else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no contract run with id {run_id!r}."
                if run_id
                else "no contract selection exists yet. Run 'contract select' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    if symbol is None:
        console.print(render_contract_run(result, verbose=True))
        return

    selection = result.selection(symbol)
    if selection is None:
        console.print(
            f"[yellow]{symbol.strip().upper()} had no selection in run {result.run_id}.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)
    console.print(render_selection(selection))


@contract_app.command("history")
def contract_history(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="One underlying's selection history."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum entries to show.")] = 20,
) -> None:
    """Show past contract selections. (read-only)"""
    service = _contract_service()

    if symbol is not None:
        entries = service.symbol_history(symbol, limit=limit)
        if not entries:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no contract history for {symbol.strip().upper()}."
            )
            raise typer.Exit(code=EXIT_OK)
        table = Table(
            title=f"Contract history — {symbol.strip().upper()}",
            show_header=True,
            header_style="bold",
        )
        for column in ("Generated", "Run id", "Status", "Strategy", "Expiration", "DTE", "Legs"):
            table.add_column(column)
        for entry in entries:
            style = "green" if entry.status == "SUCCESS" else "yellow"
            table.add_row(
                entry.generated_at.isoformat(timespec="seconds"),
                entry.run_id,
                f"[{style}]{entry.status}[/{style}]",
                entry.strategy or "-",
                entry.expiration or "-",
                str(entry.dte if entry.dte is not None else "-"),
                str(entry.legs),
            )
        console.print(table)
        console.print(f"{len(entries)} selection(s). Nothing here is ever rewritten.")
        return

    runs = service.history(limit=limit)
    if not runs:
        console.print("[yellow]UNAVAILABLE[/yellow]  no contract runs have been recorded yet.")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Contract runs", show_header=True, header_style="bold")
    for column in ("Generated", "Run id", "As of", "Status", "Selected", "No contract", "Policy"):
        table.add_column(column)
    for run in runs:
        style = "green" if run.status == "SUCCESS" else "yellow"
        table.add_row(
            run.generated_at.isoformat(timespec="seconds"),
            run.run_id,
            run.as_of.isoformat(timespec="seconds"),
            f"[{style}]{run.status}[/{style}]",
            str(run.selected),
            str(run.no_contract),
            run.selection_policy_version or "-",
        )
    console.print(table)
    console.print(f"{len(runs)} run(s). Nothing here is ever rewritten.")


@contract_app.command("validate")
def contract_validate(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Re-check a stored run instead of the configuration."),
    ] = None,
) -> None:
    """Validate the contract-selection policy, or re-check a stored run. (read-only)

    Without ``--run-id`` this prints the deterministic policy in force. With
    one, it re-checks the stored run: that every selected leg carries a broker
    contract id and a trading class, that multi-leg selections share an
    expiration, and that no failed selection carries a leg.
    """
    if run_id is not None:
        _contract_validate_run(run_id)
        return

    settings = _load_settings()
    config = load_config(settings.config_dir)
    policy = config.contract_selection

    console.print("\n[bold]CONTRACT SELECTION POLICY[/bold]")
    console.print(f"Config version : {policy.config_version}")
    console.print(f"Policy version : {policy.selection_policy_version}")
    console.print("Model involved : [green]none[/green] — selection is deterministic")

    table = Table(title="Policy", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in (
        ("expiration rule (default)", policy.expiration.rule.value),
        ("target DTE (default)", str(policy.expiration.target_dte)),
        ("event alignment window (days)", str(policy.expiration.event_max_days_after)),
        ("require trading day", str(policy.expiration.require_trading_day)),
        ("max strike distance (%)", str(policy.strike.max_strike_distance_pct)),
        (
            "reference price fields",
            ", ".join(field.value for field in policy.strike.reference_price_fields),
        ),
        ("option underlying price allowed", str(policy.strike.allow_option_underlying_price)),
        ("require quote", str(policy.quotes.require_quote)),
        ("max quote age (s)", str(policy.quotes.max_quote_age_seconds)),
        ("unknown option liquidity", policy.quotes.unknown_liquidity_policy.value),
        ("max candidates", str(policy.limits.max_candidates)),
        ("rejections recorded", str(policy.limits.max_rejected_recorded)),
    ):
        table.add_row(name, value)
    console.print(table)
    console.print(
        "\nUnderlying liquidity is never accepted as evidence of option liquidity, and a "
        "missing measurement is never read as zero."
    )
    console.print("[green]PASS[/green]  Policy is valid. No orders were submitted.")


def _contract_validate_run(run_id: str) -> None:
    """Re-check a stored contract run's own invariants."""
    service = _contract_service()
    result = service.get(run_id)
    if result is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no contract run with id {run_id!r}.")
        raise typer.Exit(code=EXIT_OK)

    console.print(f"\n[bold]VALIDATING[/bold] contract run {result.run_id}")
    problems: list[str] = []
    for selection in result.selections:
        prefix = selection.symbol
        if not selection.succeeded:
            if selection.legs:
                problems.append(
                    f"{prefix}: a {selection.selection_status.value} selection carries legs"
                )
            continue
        for leg in selection.legs:
            if not leg.trading_class.strip():
                problems.append(f"{prefix}: leg {leg.leg_index} has no trading class")
            if leg.underlying != selection.symbol:
                problems.append(f"{prefix}: leg {leg.leg_index} names another underlying")
        if len({leg.expiration for leg in selection.legs}) > 1:
            problems.append(f"{prefix}: legs resolved to different expirations")

    table = Table(show_header=True, header_style="bold")
    for column in ("Symbol", "Status", "Strategy", "Expiration", "DTE", "Legs", "Rejected"):
        table.add_column(column)
    for selection in sorted(result.selections, key=lambda s: s.symbol):
        style = "green" if selection.succeeded else "yellow"
        table.add_row(
            selection.symbol,
            f"[{style}]{selection.selection_status.value}[/{style}]",
            selection.strategy.value if selection.strategy else "-",
            selection.expiration.isoformat() if selection.expiration else "-",
            str(selection.dte if selection.dte is not None else "-"),
            str(len(selection.legs)),
            str(len(selection.rejected_candidates)),
        )
    console.print(table)

    if problems:
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
        _fail(f"{len(problems)} problem(s) found in stored run {result.run_id}")
    console.print(
        f"[green]PASS[/green]  {len(result.selections)} selection(s) satisfy the stored-run "
        f"invariants. No orders were submitted."
    )


# ---------------------------------------------------------------------------
# risk and allocation (Milestone 7)
#
# Deterministic end to end. The allocation service constructs no LLM client and
# no broker: it reads stored contract selections, a stored account snapshot and
# its own append-only ledger, and applies configured policy. Both "no model"
# and "zero orders" are structural rather than checks performed at the end.
#
# The one place broker state enters is `risk capture-account`, which reads and
# stores an account snapshot. It opens a read-only connection, makes a single
# retrieval, and reports the order counter it read off the broker.
# ---------------------------------------------------------------------------
def _allocation_service() -> AllocationService:
    from trading_system.allocation.service import AllocationService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return AllocationService(settings=settings, config=config)


@risk_app.command("capture-account")
def risk_capture_account(simulated: SimulatedOption = False) -> None:
    """Capture and store an immutable account snapshot. (writes a snapshot)

    The single boundary between broker reality and deterministic risk. The
    engines never hold a broker, so this is what gives them a view of the
    account — captured once, stored, and read back by id.

    Read-only with respect to the broker and structurally incapable of
    ordering: the count of submitted orders is read off the broker itself and
    printed, so the zero is evidence rather than a claim.
    """
    from trading_system.risk.account import build_account_snapshot
    from trading_system.risk.store import FilesystemAccountSnapshotRepository

    service = _allocation_service()
    with _connected_broker(simulated) as (settings, broker):
        _print_header("ACCOUNT SNAPSHOT CAPTURE", settings, broker)
        account = broker.get_account()
        positions = broker.get_positions()
        snapshot = build_account_snapshot(
            account,
            positions,
            broker=broker.name,
            trading_mode=settings.trading_mode,
            captured_at=account.as_of,
            orders_submitted=broker.orders_submitted,
            read_only=broker.read_only,
            simulated=broker.name == "SIMULATOR",
        )
        _print_zero_orders(broker)

    repository = service.account_repository
    assert isinstance(repository, FilesystemAccountSnapshotRepository)
    repository.save(snapshot)

    table = Table(title="Account snapshot", show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    for name, value in (
        ("snapshot id", snapshot.snapshot_id),
        ("as of", snapshot.as_of.isoformat()),
        ("account", _mask_account(snapshot.account_id)),
        ("currency", snapshot.currency),
        ("cash", _or_dash(snapshot.cash)),
        ("net liquidation", _or_dash(snapshot.net_liquidation)),
        ("buying power", _or_dash(snapshot.buying_power)),
        ("available funds", _or_dash(snapshot.available_funds)),
        ("spendable (most restrictive)", _or_dash(snapshot.spendable)),
        ("positions", str(len(snapshot.positions))),
        ("simulated", str(snapshot.simulated)),
    ):
        table.add_row(name, value)
    console.print(table)
    console.print(
        "\nThis balance is [bold]not[/bold] the campaign budget. The campaign spends its own "
        "envelope; where the account holds less, the account wins."
    )
    console.print(f"[green]Stored[/green] {snapshot.snapshot_id}")


@risk_app.command("validate")
def risk_validate() -> None:
    """Print the deterministic limits in force, layer by layer. (read-only)"""
    service = _allocation_service()
    limits = service.limits()

    console.print("\n[bold]RISK LIMITS IN FORCE[/bold]")
    console.print(f"Campaign       : {limits.campaign_id}")
    console.print(f"Risk config    : {limits.risk_config_version}")
    console.print("Model involved : [green]none[/green] — risk is deterministic")

    table = Table(title="Effective limits", show_header=True, header_style="bold")
    for column in ("Limit", "Value", "Owned by"):
        table.add_column(column)
    for name, value in (
        ("campaign_budget", str(limits.campaign_budget)),
        ("campaign_reserve", str(limits.campaign_reserve)),
        ("min_allocation_per_trade", str(limits.min_allocation_per_trade)),
        ("max_allocation_per_trade", str(limits.max_allocation_per_trade)),
        ("max_risk_per_trade", str(limits.max_risk_per_trade)),
        ("max_total_open_risk", str(limits.max_total_open_risk)),
        ("max_daily_loss", str(limits.max_daily_loss)),
        ("max_open_positions", str(limits.max_open_positions)),
        ("max_positions_per_underlying", str(limits.max_positions_per_underlying)),
        ("max_new_positions_per_run", str(limits.max_new_positions_per_run)),
        ("max_contracts_per_trade", str(limits.max_contracts_per_trade)),
        ("max_underlying_concentration_pct", str(limits.max_underlying_concentration_pct)),
        ("max_strategy_concentration_pct", str(limits.max_strategy_concentration_pct)),
        ("max_directional_exposure_pct", str(limits.max_directional_exposure_pct)),
        ("min_opportunity_score", str(limits.min_opportunity_score)),
        ("max_market_data_age_seconds", str(limits.max_market_data_age_seconds)),
        ("max_account_snapshot_age_seconds", str(limits.max_account_snapshot_age_seconds)),
    ):
        scope = limits.scopes.get(name)
        table.add_row(name, value, scope.value if scope else "-")
    console.print(table)

    console.print(
        f"\nAllocatable: [bold]{limits.campaign_budget - limits.campaign_reserve}[/bold] "
        f"(budget {limits.campaign_budget} less a reserve of {limits.campaign_reserve} that is "
        f"never spent)."
    )
    console.print(
        "A child layer may narrow a parent limit and may never widen one; configuration "
        "loading refuses rather than clamping."
    )
    if not limits.require_daily_loss_tracking:
        console.print(
            "[yellow]Note[/yellow]  realised daily profit and loss is not tracked yet, so the "
            "daily-loss limit is recorded as NOT_EVALUATED rather than passed."
        )
    console.print("[green]PASS[/green]  Limits are valid. No orders were submitted.")


@risk_app.command("evaluate")
def risk_evaluate(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", "--contract-run-id", help="The contract run to evaluate."),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
) -> None:
    """Evaluate risk for a contract run without sizing or persisting. (read-only)

    Answers only *would this be permitted?* — the quantity is the allocation
    engine's answer, and nothing here is written to the ledger.
    """
    from trading_system.allocation.report import render_evaluation

    # A dry run computes every verdict and persists nothing, which is exactly
    # what this command wants; the evaluations it prints are the ones the
    # engine actually produced rather than a second, parallel calculation.
    service = _allocation_service()
    run = service.run(
        dry_run=True, contract_run_id=run_id, symbols=list(symbol) if symbol else None
    )

    if not run.result.allocations:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  {run.result.status.value}: "
            f"{run.result.status_detail or 'nothing to evaluate.'}"
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    for allocation in run.result.allocations:
        console.print(render_evaluation(allocation.risk_evaluation))
        console.print()
    console.print("Nothing was persisted, no quantity was authorised and 0 orders were submitted.")


@risk_app.command("show")
def risk_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific allocation run. Defaults to the most recent."),
    ] = None,
) -> None:
    """Show the risk verdicts of a stored allocation run. (read-only)"""
    from trading_system.allocation.report import render_evaluation

    service = _allocation_service()
    result = service.get(run_id) if run_id else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no allocation run with id {run_id!r}."
                if run_id
                else "no allocation run exists yet. Run 'allocation run' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    for allocation in result.allocations:
        console.print(render_evaluation(allocation.risk_evaluation))
        console.print()


@risk_app.command("explain")
def risk_explain(
    symbol: Annotated[str, typer.Option("--symbol", help="Underlying to explain.")],
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific allocation run. Defaults to the most recent."),
    ] = None,
) -> None:
    """Explain one underlying's risk verdict, check by check. (read-only)"""
    from trading_system.allocation.report import render_evaluation

    service = _allocation_service()
    result = service.get(run_id) if run_id else service.latest()
    allocation = result.allocation(symbol) if result else None
    if result is None or allocation is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no stored risk verdict for "
            f"{symbol.strip().upper()}. Run 'allocation run' first."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_evaluation(allocation.risk_evaluation))
    console.print(
        "\nEvery line above is generated from the stored check list. No model wrote any of it, "
        "and no model could have changed any of it."
    )


@allocation_app.command("validate")
def allocation_validate(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Re-check a stored run instead of the configuration."),
    ] = None,
) -> None:
    """Validate the campaign and allocation policy, or re-check a stored run. (read-only)

    Without ``--run-id`` this prints the campaign envelope and the policy in
    force, and reports whether an account snapshot is available. With one, it
    re-checks the stored run's own accounting.
    """
    if run_id is not None:
        _allocation_validate_run(run_id)
        return

    service = _allocation_service()
    limits = service.limits()
    campaign = service.campaign_snapshot(_now())

    console.print("\n[bold]CAMPAIGN AND ALLOCATION POLICY[/bold]")
    console.print(f"Campaign       : {campaign.campaign_id}")
    console.print(f"Currency       : {campaign.currency}")
    console.print(f"Budget source  : {campaign.budget_source}")
    console.print("Model involved : [green]none[/green] — allocation is deterministic")

    table = Table(title="Campaign", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in (
        ("budget", str(campaign.budget)),
        ("reserve (never spent)", str(campaign.reserve)),
        ("allocatable", str(campaign.allocatable)),
        ("already allocated", str(campaign.allocated)),
        ("available", str(campaign.available)),
        ("open reservations", str(campaign.position_count)),
        ("open risk", str(campaign.open_risk)),
    ):
        table.add_row(name, value)
    console.print(table)

    account = service.account_snapshot(_now())
    if account is None:
        console.print(
            "[yellow]No account snapshot.[/yellow]  Capture one with "
            "'risk capture-account'. Without it, allocation fails closed rather than "
            "assuming the money is there."
        )
    else:
        console.print(
            f"Account snapshot: {account.snapshot_id} "
            f"(as of {account.as_of.isoformat()}, spendable {_or_dash(account.spendable)})"
        )

    console.print(
        "\nThe campaign budget is independent of the broker account balance. The most "
        "restrictive relevant limit wins."
    )
    if limits.require_account_snapshot and account is None:
        _fail("configuration requires an account snapshot and none is available")
    console.print("[green]PASS[/green]  Configuration is valid. No orders were submitted.")


@allocation_app.command("run")
def allocation_run_command(
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            "--contract-run-id",
            help="The contract run to allocate against. Defaults to the most recent.",
        ),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
    account_snapshot: Annotated[
        str | None,
        typer.Option("--account-snapshot", help="A specific stored account snapshot id."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run everything but persist nothing."),
    ] = False,
) -> None:
    """Allocate campaign capital across a contract run. (writes authorisations)

    Submits no orders and constructs no order. An authorisation says how much
    capital and risk is permitted; turning one into an order is Milestone 8.

    There is deliberately no ``--as-of``: the instant comes from the contract
    run, so an authorisation reconstructs exactly the prices that were visible
    when the contract was chosen.
    """
    from trading_system.allocation.report import render_allocation_run
    from trading_system.domain.enums import AllocationRunStatus

    service = _allocation_service()
    run = service.run(
        dry_run=dry_run,
        contract_run_id=run_id,
        symbols=list(symbol) if symbol else None,
        account_snapshot_id=account_snapshot,
    )
    console.print()
    console.print(render_allocation_run(run.result, verbose=True))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  Nothing was persisted and no capital was reserved. "
            "Authoritative history is unchanged."
        )
    else:
        console.print(f"\n[green]Stored[/green] run {run.result.run_id}")

    if run.result.status is not AllocationRunStatus.SUCCESS:
        _fail(
            f"allocation ended as {run.result.status.value}; no capital was authorised. "
            f"NO_ALLOCATION is a valid outcome — a valid strategy is not an entitlement "
            f"to capital."
        )


@allocation_app.command("show")
def allocation_show(
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="Restrict to one underlying."),
    ] = None,
) -> None:
    """Show the latest (or a named) allocation run. (read-only)"""
    from trading_system.allocation.report import render_allocation, render_allocation_run

    service = _allocation_service()
    result = service.get(run_id) if run_id else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  "
            + (
                f"no allocation run with id {run_id!r}."
                if run_id
                else "no allocation run exists yet. Run 'allocation run' first."
            )
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    if symbol is None:
        console.print(render_allocation_run(result, verbose=True))
        return

    allocation = result.allocation(symbol)
    if allocation is None:
        console.print(
            f"[yellow]{symbol.strip().upper()} had no decision in run {result.run_id}.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)
    console.print(render_allocation(allocation))


@allocation_app.command("explain")
def allocation_explain(
    symbol: Annotated[str, typer.Option("--symbol", help="Underlying to explain.")],
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific run. Defaults to the most recent."),
    ] = None,
) -> None:
    """Explain one allocation: the quantity, and every ceiling it fitted. (read-only)"""
    from trading_system.allocation.report import render_allocation

    service = _allocation_service()
    result = service.get(run_id) if run_id else service.latest()
    allocation = result.allocation(symbol) if result else None
    if result is None or allocation is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no stored allocation for "
            f"{symbol.strip().upper()}. Run 'allocation run' first."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_allocation(allocation))
    console.print(
        "\nEvery figure above is derived from the stored record. No model determined the "
        "quantity, the capital or the maximum loss."
    )


@allocation_app.command("history")
def allocation_history(
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", help="One underlying's allocation history."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum entries to show.")] = 20,
) -> None:
    """Show past allocation decisions. (read-only)"""
    service = _allocation_service()

    if symbol is not None:
        entries = service.symbol_history(symbol, limit=limit)
        if not entries:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no allocation history for {symbol.strip().upper()}."
            )
            raise typer.Exit(code=EXIT_OK)
        table = Table(
            title=f"Allocation history — {symbol.strip().upper()}",
            show_header=True,
            header_style="bold",
        )
        for column in ("Decided", "Run id", "Outcome", "Strategy", "Qty", "Capital", "Max loss"):
            table.add_column(column)
        for entry in entries:
            style = "green" if entry.outcome == "APPROVED" else "yellow"
            table.add_row(
                entry.decided_at.isoformat(timespec="seconds"),
                entry.run_id,
                f"[{style}]{entry.outcome}[/{style}]",
                entry.strategy,
                str(entry.quantity),
                entry.capital_committed,
                entry.max_loss,
            )
        console.print(table)
        console.print(f"{len(entries)} decision(s). Nothing here is ever rewritten.")
        return

    runs = service.history(limit=limit)
    if not runs:
        console.print("[yellow]UNAVAILABLE[/yellow]  no allocation runs have been recorded yet.")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Allocation runs", show_header=True, header_style="bold")
    for column in ("Generated", "Run id", "Status", "Approved", "Allocated", "Available after"):
        table.add_column(column)
    for run in runs:
        style = "green" if run.status == "SUCCESS" else "yellow"
        table.add_row(
            run.generated_at.isoformat(timespec="seconds"),
            run.run_id,
            f"[{style}]{run.status}[/{style}]",
            str(run.approved),
            run.allocated,
            run.available_after,
        )
    console.print(table)
    console.print(f"{len(runs)} run(s). Nothing here is ever rewritten.")


def _allocation_validate_run(run_id: str) -> None:
    """Re-check a stored allocation run's own accounting."""
    service = _allocation_service()
    result = service.get(run_id)
    if result is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no allocation run with id {run_id!r}.")
        raise typer.Exit(code=EXIT_OK)

    console.print(f"\n[bold]VALIDATING[/bold] allocation run {result.run_id}")
    problems: list[str] = []
    committed = sum(a.capital_committed for a in result.allocations if a.approved)
    if committed != result.allocated_this_run:
        problems.append(
            f"approved allocations total {committed} but the run reports "
            f"{result.allocated_this_run}"
        )
    total = result.allocated_before + result.allocated_this_run + result.available_after
    if total != result.budget - result.reserve:
        problems.append(
            f"allocated plus available is {total}, not the allocatable "
            f"{result.budget - result.reserve}"
        )
    for allocation in result.allocations:
        if allocation.approved and allocation.risk_outcome.value != "APPROVED":
            problems.append(f"{allocation.symbol}: approved over a risk rejection")
        if not allocation.approved and allocation.quantity:
            problems.append(f"{allocation.symbol}: a refusal carries a quantity")

    table = Table(show_header=True, header_style="bold")
    for column in ("Symbol", "Outcome", "Qty", "Capital", "Max loss", "Bound by"):
        table.add_column(column)
    for allocation in sorted(result.allocations, key=lambda a: (a.rank, a.symbol)):
        style = "green" if allocation.approved else "yellow"
        table.add_row(
            allocation.symbol,
            f"[{style}]{allocation.outcome.value}[/{style}]",
            str(allocation.quantity),
            str(allocation.capital_committed),
            str(allocation.total_max_loss),
            allocation.calculation.binding_constraint.value if allocation.calculation else "-",
        )
    console.print(table)

    if problems:
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
        _fail(f"{len(problems)} problem(s) found in stored run {result.run_id}")
    console.print(
        f"[green]PASS[/green]  {len(result.allocations)} decision(s) satisfy the stored-run "
        f"invariants and the campaign accounting balances. No orders were submitted."
    )


def _now() -> datetime:
    """Wall clock, for the read-only inspection commands only.

    Never used inside a calculation: both engines take the decision instant as
    an argument, and every stored decision is anchored at the contract run's
    ``as_of`` rather than at whenever someone happened to run a command.
    """
    from trading_system.infrastructure.clock import utc_now

    return utc_now()


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


# ---------------------------------------------------------------------------
# Execution (Milestone 8)
#
# The only command group in this system that can place an order, and the only
# one whose commands can change broker state. Two independent switches must
# both be on before anything is sent — `execution.enabled` in configuration and
# `--confirm` on the command line — and neither implies the other. `--dry-run`
# never constructs a broker at all, so its promise is structural.
# ---------------------------------------------------------------------------
def _execution_service() -> ExecutionService:
    from trading_system.execution.service import ExecutionService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return ExecutionService(settings=settings, config=config)


def _print_execution_mode(settings: Settings, *, dry_run: bool, enabled: bool) -> None:
    """State the mode plainly, at the top, before anything else is printed.

    An operator reading terminal scrollback must never have to infer whether
    the run they are looking at sent real orders.
    """
    if dry_run:
        console.print("\n[bold yellow]EXECUTION DRY RUN[/bold yellow]")
        console.print("Broker submission : [green]NOT PERFORMED[/green]")
    else:
        colour = "red" if settings.trading_mode is TradingMode.LIVE else "cyan"
        console.print(f"\n[bold {colour}]EXECUTION — {settings.trading_mode.value}[/bold {colour}]")
    console.print(f"Mode              : {settings.trading_mode.value}")
    console.print(f"Submission enabled: {'yes' if enabled else 'no (execution.enabled=false)'}")


@execution_app.command("validate")
def execution_validate(
    execution_id: Annotated[
        str | None,
        typer.Option("--execution-id", help="Re-check a stored execution instead of the policy."),
    ] = None,
) -> None:
    """Validate the execution policy, or re-check a stored execution. (read-only)

    Without ``--execution-id`` this prints the policy in force and the switches
    that would have to be on for an order to be sent. It opens no broker
    connection and submits nothing.
    """
    service = _execution_service()
    settings = _load_settings()

    if execution_id is not None:
        record = service.get(execution_id)
        if record is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no stored execution {execution_id}")
            raise typer.Exit(code=EXIT_OK)
        from trading_system.execution.report import render_execution

        console.print()
        console.print(render_execution(record))
        raise typer.Exit(code=EXIT_OK)

    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover
    policy = config.execution

    console.print("\n[bold]EXECUTION POLICY[/bold]")
    console.print("Model involved : [green]none[/green] — execution is deterministic")

    table = Table(title="Policy in force", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in (
        ("enabled", str(policy.enabled)),
        ("paper only", str(policy.paper_only)),
        ("allow live", str(policy.allow_live)),
        ("requires explicit authorisation", str(policy.require_explicit_authorization)),
        ("order types", ", ".join(t.value for t in policy.permitted_order_types)),
        ("time in force", policy.time_in_force.value),
        ("limit price offset %", str(policy.limit_price_offset_pct)),
        ("price increment", str(policy.price_increment)),
        ("allocation validity (min)", str(policy.allocation_validity_minutes)),
        ("price validity (s)", str(policy.price_validity_seconds)),
        ("max price drift %", str(policy.max_price_drift_pct)),
        ("requires market open", str(policy.require_market_open)),
        ("multi-leg as combo", str(policy.multi_leg_as_combo)),
        ("independent leg orders", str(policy.allow_independent_leg_orders)),
        ("short legs", str(policy.allow_short_legs)),
        ("auto retry on timeout", str(policy.auto_retry_on_timeout)),
        ("verify paper account", str(policy.verify_paper_account)),
    ):
        table.add_row(name, value)
    console.print(table)

    console.print(
        "\nTwo switches must BOTH be on for an order to be sent: [bold]execution.enabled[/bold] "
        "in config/execution.yaml, and [bold]--confirm[/bold] on 'execution run'. Naming an "
        "allocation is not permission to trade."
    )
    console.print(
        "A timeout is never retried: an order that may already be at the broker is resolved by "
        "looking, not by sending a second one."
    )
    console.print("[green]PASS[/green]  Policy is valid. No orders were submitted.")


@execution_app.command("run")
def execution_run_command(
    allocation_id: Annotated[
        list[str] | None,
        typer.Option("--allocation-id", help="Execute these authorisations. Repeatable."),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            "--allocation-run-id",
            help="The allocation run to execute. Defaults to the most recent.",
        ),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", help="Restrict to these underlyings. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build and show the order; never contact a broker."),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Explicitly authorise submission. Required for any real order.",
        ),
    ] = False,
) -> None:
    """Submit approved authorisations to the broker. (MUTATES BROKER STATE)

    The only command in this system that can place an order.

    ``--dry-run`` loads the authorisation, validates it, builds the purchase
    card and the broker order, and prints exactly what would be sent — without
    constructing a broker at all. ``--confirm`` is the deliberate execution
    request; without it, and outside a dry run, nothing is built and nothing is
    sent, because an allocation id is not permission to trade.

    Nothing here recalculates a quantity, a price or a risk limit: every figure
    comes from the Milestone 7 authorisation. If the broker refuses because the
    market moved, that is recorded as a failure — never retried as a smaller
    order that fits.
    """
    from trading_system.domain.enums import ExecutionRunStatus
    from trading_system.execution.report import render_execution_run

    if dry_run and confirm:
        _fail(
            "--dry-run and --confirm contradict each other. A dry run submits nothing; "
            "--confirm authorises submission. Pick one."
        )

    service = _execution_service()
    settings = _load_settings()
    _print_execution_mode(settings, dry_run=dry_run, enabled=service.enabled)

    run = service.run(
        allocation_ids=list(allocation_id) if allocation_id else None,
        allocation_run_id=run_id,
        symbols=list(symbol) if symbol else None,
        dry_run=dry_run,
        authorized=confirm,
    )

    console.print()
    console.print(render_execution_run(run.result))

    if dry_run:
        console.print(
            "\n[yellow]DRY RUN[/yellow]  No broker connection was opened, no order was sent "
            "and no broker state changed."
        )
        for plan in run.plans:
            from trading_system.execution.report import render_plan

            console.print()
            console.print(render_plan(plan))
        raise typer.Exit(code=EXIT_OK)

    style = "green" if run.result.orders_submitted == 0 else "bold cyan"
    console.print(
        f"\nOrders submitted (read off the broker): "
        f"[{style}]{run.result.orders_submitted}[/{style}]"
    )
    if run.stored:
        console.print(f"[green]Stored[/green] run {run.result.run_id}")

    if run.result.counts.uncertain:
        _fail(
            f"{run.result.counts.uncertain} submission(s) are UNCERTAIN. An order may be live "
            f"at the broker. Do NOT re-run: resolve with 'execution explain --execution-id "
            f"<ID>', which asks the broker what it actually has."
        )
    if run.result.status not in (ExecutionRunStatus.SUCCESS, ExecutionRunStatus.DRY_RUN):
        _fail(
            f"execution ended as {run.result.status.value}. "
            f"{run.result.status_detail or 'No order was submitted.'}"
        )


@execution_app.command("show")
def execution_show(
    execution_id: Annotated[
        str | None,
        typer.Option("--execution-id", help="One execution. Defaults to the latest run."),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="A specific execution run."),
    ] = None,
) -> None:
    """Show an execution, or the latest execution run. (read-only)"""
    from trading_system.execution.report import render_execution, render_execution_run

    service = _execution_service()
    if execution_id is not None:
        record = service.get(execution_id)
        if record is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no stored execution {execution_id}")
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_execution(record))
        return

    result = service.get_run(run_id) if run_id else service.latest_run()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no execution run has been recorded. "
            "Run 'execution run --dry-run' to see what would be submitted."
        )
        raise typer.Exit(code=EXIT_OK)
    console.print()
    console.print(render_execution_run(result))


@execution_app.command("history")
def execution_history(
    allocation_id: Annotated[
        str | None,
        typer.Option("--allocation-id", help="Only executions of this authorisation."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
) -> None:
    """List recorded executions, newest first. (read-only)"""
    service = _execution_service()
    entries = service.history(limit=None)
    if allocation_id:
        entries = [entry for entry in entries if entry.allocation_id == allocation_id]
    entries = entries[:limit]

    if not entries:
        console.print("[yellow]No executions recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Execution history", show_header=True, header_style="bold")
    for column in ("created", "execution", "symbol", "qty", "state", "mode", "broker order"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.created_at.isoformat(),
            entry.execution_id,
            entry.symbol,
            str(entry.quantity),
            entry.state,
            entry.trading_mode,
            entry.broker_order_id or "-",
        )
    console.print(table)


@execution_app.command("explain")
def execution_explain(
    execution_id: Annotated[str, typer.Option("--execution-id", help="The execution to explain.")],
    resolve: Annotated[
        bool,
        typer.Option(
            "--resolve",
            help="Ask the broker what it actually has, and record the answer.",
        ),
    ] = False,
) -> None:
    """Explain one execution from its stored event history. (read-only by default)

    With ``--resolve`` it opens a short-lived broker connection and asks what
    the broker actually holds, appending the answer to the history. That is the
    supported response to an uncertain submission — it reads state and never
    submits an order.
    """
    from trading_system.execution.report import render_execution

    service = _execution_service()
    record = service.get(execution_id)
    if record is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no stored execution {execution_id}")
        raise typer.Exit(code=EXIT_OK)

    if resolve:
        console.print(
            "[cyan]Asking the broker what it has...[/cyan]  (reads state; submits nothing)"
        )
        record = service.resolve(execution_id) or record

    console.print()
    console.print(render_execution(record))

    events = service.repository.events(execution_id)
    table = Table(title="Event history (append-only)", show_header=True, header_style="bold")
    for column in ("#", "observed", "event", "state", "reason", "detail"):
        table.add_column(column)
    for event in events:
        table.add_row(
            str(event.sequence),
            event.observed_at.isoformat(),
            event.event_type.value,
            event.state.value,
            event.reason_code.value if event.reason_code else "-",
            (event.detail or "")[:60],
        )
    console.print(table)
    console.print(
        "\nThe record above is folded from these events. Nothing was rewritten: a later fill "
        "appends, it does not edit."
    )


@execution_app.command("cancel")
def execution_cancel(
    execution_id: Annotated[str, typer.Option("--execution-id", help="The execution to cancel.")],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Explicitly authorise the cancellation."),
    ] = False,
) -> None:
    """Cancel a live order. (MUTATES BROKER STATE)

    Narrow by design: this closes a submitted order's lifecycle. It is not an
    exit, and this milestone ships no automated exits — trailing stops, take
    profits and thesis exits are Milestone 9.
    """
    from trading_system.execution.report import render_execution

    if not confirm:
        _fail("cancelling changes broker state. Pass --confirm to authorise it explicitly.")

    service = _execution_service()
    record = service.get(execution_id)
    if record is None:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no stored execution {execution_id}")
        raise typer.Exit(code=EXIT_OK)
    if not record.submitted:
        _fail(
            f"execution {execution_id} is {record.state.value}; there is no live order to cancel."
        )

    updated = service.cancel(execution_id) or record
    console.print()
    console.print(render_execution(updated))


# ---------------------------------------------------------------------------
# Positions, reservations and reconciliation (Milestone 9)
#
# Three groups, and none of them can place an order. They build their broker
# through the read-only factory, assert the broker's own submitted-order
# counter is still zero after every read, and every rendering prints that
# count next to a corrective-order count that is always zero. It must be
# impossible to mistake any of this for trading.
#
# The distinction every command here preserves:
#
#   BROKER OBSERVED POSITION   what the broker says the account holds
#   INTERNAL EXPECTED POSITION what confirmed fills say should exist
#
# They are labelled separately everywhere, because a reader who cannot tell
# them apart cannot tell a fact from a belief.
# ---------------------------------------------------------------------------
def _services(simulated: bool) -> tuple[Settings, ReconciliationService]:
    """Build the Milestone 9 composition root, optionally against the simulator."""
    from trading_system.reconciliation.service import ReconciliationService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    factory = None
    if simulated:

        def factory(resolved: Settings, **kwargs: Any) -> Broker:
            return build_broker(resolved, backend=BrokerBackend.SIMULATOR, **kwargs)

    return settings, ReconciliationService(settings=settings, config=config, broker_factory=factory)


def _position_service(simulated: bool) -> PositionService:
    return _services(simulated)[1].positions


def _reservation_service() -> ReservationService:
    return _services(False)[1].reservations


def _print_zero_order_footer(orders_submitted: int, *, corrective: int = 0) -> None:
    """State plainly that nothing was traded. Read off the broker, not asserted."""
    style = "green" if orders_submitted == 0 else "bold red"
    console.print(f"\nOrders submitted  : [{style}]{orders_submitted}[/{style}]")
    console.print(f"Corrective orders : [green]{corrective}[/green]")
    if orders_submitted or corrective:
        _fail(
            f"a read-only stage reported {orders_submitted} submitted and {corrective} "
            f"corrective order(s). This must never happen"
        )


# --- positions -------------------------------------------------------------
@positions_app.command("snapshot")
def positions_snapshot(
    simulated: SimulatedOption = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Read the broker and store nothing."),
    ] = False,
) -> None:
    """Capture what the broker holds right now. (mutates local state)

    Opens one short-lived read-only connection and reads account, positions,
    open orders and fills from it — all served by the gateway's startup cache,
    so no second uncached round trip is needed. A failed read is stored as a
    failed read: it can never be mistaken for an empty account.
    """
    from trading_system.positions.report import render_capture

    service = _position_service(simulated)
    capture = service.capture(store=not dry_run, record_fills=not dry_run)
    console.print()
    console.print(render_capture(capture))
    if dry_run:
        console.print("\n[yellow]DRY RUN[/yellow]  nothing was written.")
    _print_zero_order_footer(capture.orders_submitted)
    if not capture.snapshot.usable:
        _fail(
            "broker position state could not be read. This is NOT an empty account: no "
            "comparison should be made against it."
        )


@positions_app.command("show")
def positions_show(
    contract_id: Annotated[
        str | None,
        typer.Option("--contract-id", help="Only this broker contract id."),
    ] = None,
    symbol: Annotated[str | None, typer.Option("--symbol", help="Only this underlying.")] = None,
    expected: Annotated[
        bool,
        typer.Option("--expected", help="Show the INTERNAL EXPECTED ledger instead."),
    ] = False,
) -> None:
    """Show stored positions. (read-only)

    Without ``--expected`` this shows BROKER OBSERVED positions from the latest
    stored snapshot. With it, the INTERNAL EXPECTED ledger derived from
    confirmed fills. The two are different claims and are never merged.
    """
    from trading_system.positions.report import (
        render_expected,
        render_observed,
        render_projection,
        render_snapshot,
    )

    service = _position_service(False)
    if expected:
        projection = service.expected(snapshot=service.latest_usable_snapshot())
        positions = [
            position
            for position in projection.positions
            if (symbol is None or position.underlying == symbol.strip().upper())
            and (contract_id is None or str(position.contract_id) == contract_id)
        ]
        if symbol or contract_id:
            console.print("\n[bold]INTERNAL EXPECTED POSITIONS[/bold] (filtered)\n")
            for position in positions:
                console.print(render_expected(position))
            if not positions:
                console.print("  (none)")
            return
        console.print()
        console.print(render_projection(projection))
        return

    snapshot = service.latest_snapshot()
    if snapshot is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no position snapshot has been captured. "
            "Run 'positions snapshot' first."
        )
        raise typer.Exit(code=EXIT_OK)

    if symbol or contract_id:
        chosen = [
            position
            for position in snapshot.positions
            if (symbol is None or position.underlying == symbol.strip().upper())
            and (contract_id is None or str(position.contract_id) == contract_id)
        ]
        console.print("\n[bold]BROKER OBSERVED POSITIONS[/bold] (filtered)\n")
        for observed in chosen:
            console.print(render_observed(observed))
        if not chosen:
            console.print("  (none)")
        return

    console.print()
    console.print(render_snapshot(snapshot))


@positions_app.command("history")
def positions_history(
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
    contract_id: Annotated[
        str | None,
        typer.Option("--contract-id", help="History of one instrument instead."),
    ] = None,
) -> None:
    """List captured broker snapshots, newest first. (read-only)"""
    from trading_system.positions.report import render_observed

    service = _position_service(False)
    if contract_id is not None:
        key = contract_id if contract_id.startswith(("cid:", "sym:")) else f"cid:{contract_id}"
        observations = service.repository.by_contract(key, limit=limit)
        console.print(f"\n[bold]BROKER OBSERVED history[/bold] for {key}\n")
        for observation in observations:
            console.print(f"{observation.observed_at.isoformat()}  {render_observed(observation)}")
        if not observations:
            console.print("  (none)")
        return

    entries = service.repository.history(limit=limit)
    if not entries:
        console.print("[yellow]No position snapshots recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Broker position snapshots", show_header=True, header_style="bold")
    for column in ("observed", "snapshot", "broker", "account", "read", "positions", "note"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.observed_at.isoformat(),
            entry.snapshot_id,
            entry.broker,
            entry.account_reference,
            entry.read_status,
            str(entry.positions),
            "re-observation" if entry.reobserved else "",
        )
    console.print(table)


@positions_app.command("validate")
def positions_validate() -> None:
    """Validate the position ledger policy in force. (read-only)"""
    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover
    policy = config.positions

    console.print("\n[bold]POSITION LEDGER POLICY[/bold]")
    console.print("Model involved : [green]none[/green] — the position ledger is deterministic")

    table = Table(title="Policy in force", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in (
        ("account mask (visible chars)", str(policy.account_mask_visible_characters)),
        ("prefer broker contract id", str(policy.prefer_broker_contract_id)),
        ("store empty snapshots", str(policy.snapshot.store_empty_snapshots)),
        ("snapshot max age (s)", str(policy.snapshot.max_age_seconds)),
        ("deduplicate fills by broker id", str(policy.fills.deduplicate_by_broker_execution_id)),
        ("flag fills without broker id", str(policy.fills.flag_fills_without_broker_execution_id)),
        (
            "expected from confirmed fills only",
            str(policy.expected_positions.from_confirmed_fills_only),
        ),
        ("reflect partial fills", str(policy.expected_positions.reflect_partial_fills)),
        ("report partial structures", str(policy.expected_positions.report_partial_structures)),
        ("adopt orphan positions", str(policy.adopt_orphan_positions)),
    ):
        table.add_row(name, value)
    console.print(table)
    console.print(
        "\nOnly a CONFIRMED BROKER FILL establishes an internal expected position. An "
        "allocation, a submitted order, an acknowledgement and an UNKNOWN submission all "
        "establish nothing."
    )
    console.print("[green]PASS[/green]  Policy is valid. No orders were submitted.")


@positions_app.command("explain")
def positions_explain(
    contract_id: Annotated[str, typer.Option("--contract-id", help="The instrument to explain.")],
) -> None:
    """Explain one instrument: what we expect, what the broker holds. (read-only)"""
    from trading_system.positions.report import render_expected, render_observed

    service = _position_service(False)
    key = contract_id if contract_id.startswith(("cid:", "sym:")) else f"cid:{contract_id}"
    snapshot = service.latest_usable_snapshot()
    projection = service.expected(snapshot=snapshot)

    console.print(f"\n[bold]{key}[/bold]\n")
    console.print("[bold]INTERNAL EXPECTED[/bold] (from confirmed fills)")
    expected_position = projection.by_key(key)
    console.print(
        f"  {render_expected(expected_position)}" if expected_position else "  (nothing expected)"
    )

    console.print("\n[bold]BROKER OBSERVED[/bold]")
    if snapshot is None:
        console.print("  (no usable snapshot — run 'positions snapshot')")
    else:
        observed_position = snapshot.by_key(key)
        console.print(
            f"  {render_observed(observed_position)}"
            if observed_position
            else "  (broker holds none)"
        )

    fills = service.fills.for_contract(key)
    console.print(f"\n[bold]RECORDED FILLS[/bold] ({len(fills)})")
    from trading_system.positions.report import render_fill

    for fill in fills:
        console.print(f"  {render_fill(fill)}")
    if not fills:
        console.print("  (none)")


# --- reservations ----------------------------------------------------------
@reservations_app.command("show")
def reservations_show(
    reservation_id: Annotated[
        str | None, typer.Option("--reservation-id", help="One reservation, in full.")
    ] = None,
) -> None:
    """Show committed campaign capital. (read-only)"""
    from trading_system.reservations.report import (
        render_capital,
        render_reservation,
        render_reservations,
    )

    service = _reservation_service()
    service.sync()
    if reservation_id is not None:
        reservation = service.get(reservation_id)
        if reservation is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no reservation {reservation_id}")
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_reservation(reservation))
        return

    console.print()
    console.print(render_capital(service.capital()))
    console.print()
    console.print(render_reservations(service.all()))


@reservations_app.command("history")
def reservations_history(
    reservation_id: Annotated[
        str | None, typer.Option("--reservation-id", help="Events for one reservation.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
) -> None:
    """List reservations, or one reservation's economic history. (read-only)"""
    service = _reservation_service()
    if reservation_id is not None:
        events = service.repository.events(reservation_id)
        if not events:
            console.print(f"[yellow]No events recorded for {reservation_id}.[/yellow]")
            raise typer.Exit(code=EXIT_OK)
        table = Table(title="Reservation history", show_header=True, header_style="bold")
        for column in ("#", "observed", "event", "state", "consumed Δ", "released Δ", "reason"):
            table.add_column(column)
        for event in events:
            table.add_row(
                str(event.sequence),
                event.observed_at.isoformat(),
                event.event_type.value,
                event.state.value,
                str(event.consumed_delta),
                str(event.released_delta),
                event.reason_code.value if event.reason_code else "-",
            )
        console.print(table)
        console.print(
            "\nThe reservation above is folded from these events. Nothing was rewritten: a "
            "consumption appends, it does not edit."
        )
        return

    entries = service.repository.history(limit=limit)
    if not entries:
        console.print("[yellow]No reservations recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)
    table = Table(title="Reservations", show_header=True, header_style="bold")
    for column in ("created", "reservation", "symbol", "allocation", "authorised"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.created_at.isoformat(),
            entry.reservation_id,
            entry.symbol,
            entry.allocation_id,
            f"{entry.authorized_amount} {entry.currency}",
        )
    console.print(table)


@reservations_app.command("validate")
def reservations_validate() -> None:
    """Show what would move, and why, without moving it. (read-only)

    Evaluates every reservation against the execution ledger and prints the
    conclusion. Nothing is written: this is the safe way to see the effect of a
    reconciliation on committed capital before running one.
    """
    from trading_system.reservations.report import render_capital, render_update

    service = _reservation_service()
    service.sync()
    updates = service.apply_executions(dry_run=True)

    console.print()
    console.print(render_capital(service.capital()))
    console.print()
    if not updates:
        console.print("No reservations to evaluate.")
        return
    for update in updates:
        console.print(render_update(update))
        console.print()
    console.print(
        "[green]PASS[/green]  Nothing was written and no order was submitted. Run "
        "'reconciliation run' to apply these conclusions against fresh broker state."
    )


@reservations_app.command("release")
def reservations_release(
    reservation_id: Annotated[
        str, typer.Option("--reservation-id", help="The reservation to release.")
    ],
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Explicitly authorise the release.")
    ] = False,
) -> None:
    """Release a reservation's capital, if there is proof it was not spent. (mutates state)

    Deliberately narrow. It refuses outright while any execution against the
    authorisation is UNKNOWN — an order may be live at the broker, and freeing
    its capital is how the campaign funds the same trade twice. There is no
    force-release, by design: resolve the execution against the broker instead,
    and the resolved state releases the capital on its own.
    """
    from trading_system.reservations.report import render_reservation, render_update

    if not confirm:
        _fail("releasing capital changes campaign state. Pass --confirm to authorise it.")

    service = _reservation_service()
    service.sync()
    try:
        update = service.release(reservation_id)
    except KeyError:
        console.print(f"[yellow]UNAVAILABLE[/yellow]  no reservation {reservation_id}")
        raise typer.Exit(code=EXIT_OK) from None

    console.print()
    console.print(render_update(update))
    console.print()
    console.print(render_reservation(update.reservation))
    if not update.applied:
        _fail(f"nothing was released: {update.outcome.reason_code.value}. {update.outcome.detail}")


# --- reconciliation --------------------------------------------------------
def _run_reconciliation(*, simulated: bool, dry_run: bool) -> None:
    """Shared by 'reconciliation run' and the top-level 'reconcile' alias."""
    from trading_system.reconciliation.report import render_run

    settings, service = _services(simulated)
    console.print(f"\n[bold cyan]RECONCILIATION — {settings.trading_mode.value}[/bold cyan]")
    console.print("Broker access : READ-ONLY")
    console.print("Corrective trading : [green]not possible[/green] — this stage reports only")

    run = service.run(dry_run=dry_run)
    console.print()
    console.print(render_run(run))
    _print_zero_order_footer(run.orders_submitted, corrective=run.corrective_orders)

    if run.result.counts.critical:
        _fail(
            f"{run.result.counts.critical} critical finding(s). ACTION REQUIRED: nothing was "
            f"corrected automatically. Do not open new positions until these are resolved."
        )


@reconciliation_app.command("run")
def reconciliation_run(
    simulated: SimulatedOption = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compare and print; write nothing at all."),
    ] = False,
) -> None:
    """Compare internal records against broker reality. (mutates local state)

    Reads the broker once, over one short-lived read-only connection, and
    records what it finds. It resolves ambiguous submissions by *observing* the
    broker and moves committed capital only on proof — never on elapsed time.

    It cannot place, cancel or modify an order. ``--dry-run`` additionally
    writes nothing at all: no snapshot, no fill, no execution resolution, no
    reservation movement and no result.
    """
    _run_reconciliation(simulated=simulated, dry_run=dry_run)


def _cleanup_service(simulated: bool) -> CleanupService:
    """Build the orphan-cleanup composition root.

    Deliberately reuses :func:`_services`, so the reconciliation this operation
    reads and the reconciliation ``reconciliation run`` writes are the same
    service with the same policy. A cleanup that saw a differently-configured
    comparison would be acting on a different account than the one an operator
    reviewed.
    """
    from trading_system.cleanup.service import CleanupService

    settings, reconciliation = _services(simulated)
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return CleanupService(settings=settings, config=config, reconciliation_service=reconciliation)


@reconciliation_app.command("cleanup-orphans")
def reconciliation_cleanup_orphans(
    contract_id: Annotated[
        list[int] | None,
        typer.Option(
            "--contract-id",
            help="Restrict to these broker contract ids. Repeatable. Narrows only.",
        ),
    ] = None,
    reconciliation_id: Annotated[
        str | None,
        typer.Option(
            "--reconciliation-id",
            help="Use a stored comparison instead of running a fresh one.",
        ),
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Explicitly authorise submission. Required for any real order.",
        ),
    ] = False,
    simulated: SimulatedOption = False,
) -> None:
    """Close pre-existing ORPHAN broker positions. (MUTATES BROKER STATE)

    The controlled, PAPER-only closure of holdings this system never opened —
    the ones reconciliation reports as ``ORPHAN_BROKER_POSITION`` and correctly
    refuses to adopt.

    **Without ``--confirm`` this is a review.** It reads the broker, selects
    the targets, evaluates every safety gate, builds the exact order it would
    send for each holding and prints all of it — while never constructing a
    writable broker at all, so "a review cannot place an order" is structural
    rather than a flag anyone has to check correctly. Ordinary
    ``reconciliation run`` stays read-only and is unaffected by any of this.

    Nothing here adopts a position. No allocation, purchase card, risk
    decision, opportunity or strategy is created for a holding this system did
    not buy, no campaign capital is committed or released, and no profit or
    loss is attributed. The record it writes says exactly one thing: we sold
    something the broker said was there, and here is everything about it.

    ``--confirm`` needs ``cleanup.enabled`` and ``execution.enabled`` on top of
    it, plus ``TRADING_MODE=PAPER`` with both live guards off and a connected
    account that proves it is a paper account. It is not, and can never be,
    permission for LIVE trading.
    """
    from trading_system.cleanup.models import CleanupRunStatus
    from trading_system.cleanup.report import (
        render_confirmation_summary,
        render_plan,
        render_run,
    )

    service = _cleanup_service(simulated)
    settings = _load_settings()

    console.print()
    console.print(
        f"Mode: [bold]{settings.trading_mode.value}[/bold]   "
        f"cleanup.enabled: [bold]{service.enabled}[/bold]   "
        f"execution.enabled: [bold]{service.executions.enabled}[/bold]   "
        f"authorised: [bold]{confirm}[/bold]"
    )

    if not confirm:
        # The review path. It never reaches submit_cleanup with authorisation,
        # so no writable broker is constructed anywhere in this branch.
        outcome = service.run(
            authorized=False,
            contract_ids=list(contract_id) if contract_id else None,
            reconciliation_id=reconciliation_id,
        )
        console.print()
        console.print(render_plan(outcome.plan))
        console.print()
        console.print(
            "[yellow]REVIEW[/yellow]  No writable broker was constructed, no order was sent "
            "and no broker state changed."
        )
        _print_zero_order_footer(outcome.run.orders_submitted)
        console.print(
            "\nTo close these holdings, review the target list above and re-run with "
            "[bold]--confirm[/bold]."
        )
        raise typer.Exit(code=EXIT_OK)

    # The authorised path. The summary is printed before anything is sent.
    review = service.run(
        authorized=False,
        contract_ids=list(contract_id) if contract_id else None,
        reconciliation_id=reconciliation_id,
    )
    console.print()
    console.print(render_plan(review.plan))
    if review.plan.request is None:
        console.print("\n[green]Nothing to close.[/green]")
        raise typer.Exit(code=EXIT_OK)
    console.print(
        render_confirmation_summary(
            review.plan.request,
            account_reference=review.run.account_reference,
            mode=settings.trading_mode,
        )
    )

    outcome = service.run(
        authorized=True,
        contract_ids=list(contract_id) if contract_id else None,
        reconciliation_id=reconciliation_id,
    )
    console.print()
    console.print(render_run(outcome))
    style = "green" if outcome.run.orders_submitted == 0 else "bold cyan"
    console.print(
        f"\nOrders submitted (read off the broker): "
        f"[{style}]{outcome.run.orders_submitted}[/{style}]"
    )
    console.print(
        f"Corrective orders                     : [green]{outcome.run.corrective_orders}[/green]"
    )
    if outcome.stored:
        console.print(f"[green]Stored[/green] cleanup run {outcome.run.run_id}")

    if outcome.run.uncertain:
        _fail(
            f"{outcome.run.uncertain} submission(s) are UNCERTAIN. An order may be live at the "
            f"broker. Do NOT re-run this command: resolve with 'execution explain "
            f"--execution-id <ID> --resolve', which asks the broker what it actually has."
        )
    if outcome.run.status not in (CleanupRunStatus.COMPLETE, CleanupRunStatus.DRY_RUN):
        _fail(
            f"cleanup ended as {outcome.run.status.value}. "
            f"{outcome.run.detail or 'Not every targeted holding is confirmed gone.'}"
        )


@reconciliation_app.command("show")
def reconciliation_show(
    reconciliation_id: Annotated[
        str | None,
        typer.Option("--reconciliation-id", help="One comparison. Defaults to the latest."),
    ] = None,
    all_findings: Annotated[
        bool,
        typer.Option("--all", help="Include findings that record agreement."),
    ] = False,
) -> None:
    """Show a stored reconciliation. (read-only)"""
    from trading_system.reconciliation.report import render_reconciliation

    _, service = _services(False)
    result = service.get(reconciliation_id) if reconciliation_id is not None else service.latest()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no reconciliation has been recorded. "
            "Run 'reconciliation run' first."
        )
        raise typer.Exit(code=EXIT_OK)
    console.print()
    console.print(render_reconciliation(result, include_agreements=all_findings))


@reconciliation_app.command("history")
def reconciliation_history(
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
) -> None:
    """List recorded reconciliations, newest first. (read-only)"""
    _, service = _services(False)
    entries = service.history(limit=limit)
    if not entries:
        console.print("[yellow]No reconciliations recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Reconciliation history", show_header=True, header_style="bold")
    for column in (
        "observed",
        "reconciliation",
        "status",
        "findings",
        "critical",
        "orders",
        "note",
    ):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.observed_at.isoformat(),
            entry.reconciliation_id,
            entry.status,
            str(entry.mismatches),
            str(entry.critical),
            str(entry.orders_submitted),
            "re-observation" if entry.reobserved else "",
        )
    console.print(table)


@reconciliation_app.command("validate")
def reconciliation_validate(
    reconciliation_id: Annotated[
        str | None,
        typer.Option("--reconciliation-id", help="Re-check a stored comparison instead."),
    ] = None,
) -> None:
    """Validate the reconciliation policy, or re-check a stored result. (read-only)"""
    from trading_system.reconciliation.report import render_reconciliation

    settings, service = _services(False)
    if reconciliation_id is not None:
        result = service.get(reconciliation_id)
        if result is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no reconciliation {reconciliation_id}")
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_reconciliation(result))
        raise typer.Exit(code=EXIT_OK)

    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover
    policy = config.reconciliation

    console.print("\n[bold]RECONCILIATION POLICY[/bold]")
    console.print("Model involved : [green]none[/green] — reconciliation is deterministic")

    table = Table(title="Policy in force", show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in (
        ("enabled", str(policy.enabled)),
        ("require broker account", str(policy.require_broker_account)),
        ("require broker positions", str(policy.require_broker_positions)),
        ("require broker orders", str(policy.require_broker_orders)),
        ("require broker fills", str(policy.require_broker_fills)),
        ("broker fills are complete history", str(policy.treat_broker_fills_as_complete_history)),
        ("one connection per read", str(policy.one_connection_per_read)),
        ("max broker data age (s)", str(policy.max_broker_data_age_seconds)),
        ("corrective orders permitted", str(policy.corrective_orders_permitted)),
        ("auto-adopt orphan positions", str(policy.auto_adopt_orphan_positions)),
        ("resolve UNKNOWN executions", str(policy.resolve_unknown_executions)),
        ("release on execution FAILED", str(policy.reservations.release_on_execution_failed)),
        ("release on broker rejection", str(policy.reservations.release_on_broker_rejected)),
        (
            "release on cancel without fill",
            str(policy.reservations.release_on_cancelled_without_fill),
        ),
        ("release on UNKNOWN", str(policy.reservations.release_on_unknown)),
        ("release when never executed", str(policy.reservations.release_when_never_executed)),
        ("use actual fill economics", str(policy.reservations.use_actual_fill_economics)),
        ("allow currency conversion", str(policy.reservations.allow_currency_conversion)),
    ):
        table.add_row(name, value)
    console.print(table)

    console.print(
        "\nReconciliation REPORTS. It cannot place, cancel or modify an order: "
        "corrective_orders_permitted and auto_adopt_orphan_positions both fail to load if set."
    )
    console.print(
        "An UNKNOWN execution never releases its capital. There is no configuration that "
        "permits it, and no command that forces it."
    )
    console.print("[green]PASS[/green]  Policy is valid. No orders were submitted.")


@reconciliation_app.command("explain")
def reconciliation_explain(
    reconciliation_id: Annotated[
        str | None,
        typer.Option("--reconciliation-id", help="The comparison to explain."),
    ] = None,
) -> None:
    """Explain one reconciliation from its stored event history. (read-only)"""
    from trading_system.reconciliation.report import render_reconciliation

    _, service = _services(False)
    result = service.get(reconciliation_id) if reconciliation_id is not None else service.latest()
    if result is None:
        console.print("[yellow]UNAVAILABLE[/yellow]  no reconciliation has been recorded.")
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_reconciliation(result, include_agreements=True))

    events = service.repository.events(result.reconciliation_id)
    table = Table(title="Event history (append-only)", show_header=True, header_style="bold")
    for column in ("#", "observed", "event", "detail"):
        table.add_column(column)
    for event in events:
        table.add_row(
            str(event.sequence),
            event.observed_at.isoformat(),
            event.event_type.value,
            (event.detail or "")[:70],
        )
    console.print(table)
    console.print(
        f"\nOrders submitted: [green]{result.orders_submitted}[/green]   "
        f"Corrective orders: [green]{result.corrective_orders}[/green]"
    )


# ---------------------------------------------------------------------------
# Exit management and position lifecycle (Milestone 10)
#
# One group, and it can place exactly one kind of order: an exit for a position
# the broker already reports holding. Everything about *how* that order is sent
# is Milestone 8's, including both of its switches, so `exit run --confirm` is
# not a weaker confirmation path — it is the same one.
#
# The distinction every command here preserves:
#
#   EVALUATE   decide whether a position should close. Submits nothing, ever,
#              and constructs no broker at all.
#   EXECUTE    hand a triggered decision to Milestone 8. Needs execution.enabled
#              AND --confirm.
#
# Three verdicts, and the third is why the group exists: WAIT keeps a position
# on evidence, EXIT closes it on a named policy, and BLOCK refuses to judge it
# because the records, the price or the broker are not in a state where any
# judgement may be acted on.
# ---------------------------------------------------------------------------
def _exit_service(simulated: bool = False) -> ExitService:
    """Build the Milestone 10 composition root, optionally against the simulator."""
    from trading_system.exit.service import ExitService

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    factory = None
    if simulated:

        def factory(resolved: Settings, **kwargs: Any) -> Broker:
            return build_broker(resolved, backend=BrokerBackend.SIMULATOR, **kwargs)

    return ExitService(settings=settings, config=config, broker_factory=factory)


def _print_exit_run(run: object, *, authorized: bool) -> None:
    """Render one run and state plainly what it submitted."""
    from trading_system.exit.report import render_run

    result = run.result  # type: ignore[attr-defined]
    console.print()
    console.print(render_run(result))
    submitted = result.orders_submitted
    style = "green" if submitted == 0 else "yellow"
    console.print(f"\nOrders submitted  : [{style}]{submitted}[/{style}]")
    if not authorized and submitted:
        _fail(
            f"an unauthorised exit run reported {submitted} submitted order(s). "
            f"Evaluating whether a position should close must never close one."
        )


@exit_app.command("evaluate")
def exit_evaluate(
    position_id: Annotated[
        str | None,
        typer.Option("--position-id", help="Evaluate one position instead of every open one."),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Evaluate as of a past instant (ISO-8601, UTC)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Evaluate and print; write nothing at all."),
    ] = False,
    simulated: SimulatedOption = False,
) -> None:
    """Decide whether open positions should be closed. (mutates local state)

    Submits nothing. This command constructs no writable broker, builds no
    order and hands nothing to Milestone 8 — an ``EXIT`` verdict here is a
    recorded decision, and closing the position needs ``exit run --confirm``.

    ``--dry-run`` additionally writes nothing at all: no evaluation, no
    decision, no trailing state and no lifecycle event.
    """
    service = _exit_service(simulated)
    instant = _parse_instant(as_of) if as_of else None
    run = service.monitor(
        as_of=instant,
        position_ids=[position_id] if position_id else None,
        authorized=False,
        dry_run=dry_run,
    )
    _print_exit_run(run, authorized=False)
    if dry_run:
        console.print("[yellow]DRY RUN[/yellow]  nothing was written.")


@exit_app.command("run")
def exit_run(
    position_id: Annotated[
        str | None,
        typer.Option("--position-id", help="Act on one position instead of every open one."),
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Authorise SUBMITTING exit orders to the broker."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be submitted. Opens no broker."),
    ] = False,
) -> None:
    """Evaluate open positions and SUBMIT the exits that triggered. (MUTATES BROKER STATE)

    The only command in this group that can place an order, and it needs both
    switches Milestone 8 established: ``execution.enabled`` in
    ``config/execution.yaml`` *and* an explicit ``--confirm``. Without
    ``--confirm`` this evaluates and submits nothing.

    A position whose exit is already submitted, or whose outcome is UNKNOWN,
    never receives a second order — the lifecycle refuses it before anything is
    built.
    """
    if confirm and dry_run:
        _fail("--confirm and --dry-run are mutually exclusive: one submits, the other cannot.")

    service = _exit_service(False)
    run = service.monitor(
        position_ids=[position_id] if position_id else None,
        authorized=confirm,
        dry_run=dry_run,
    )
    _print_exit_run(run, authorized=confirm)

    if not confirm and not dry_run:
        console.print(
            "\n[yellow]NOT AUTHORISED[/yellow]  no exit order was built or sent. "
            "Pass --confirm to authorise, or --dry-run to inspect."
        )
    for outcome in run.outcomes:
        submission = outcome.submission
        if submission is None:
            continue
        if submission.reason_codes:
            console.print(
                f"[yellow]REFUSED[/yellow]  {outcome.position.position_id}: "
                f"{', '.join(code.value for code in submission.reason_codes)}\n"
                f"  {submission.detail or ''}"
            )
        elif submission.record is not None:
            console.print(
                f"[green]SUBMITTED[/green]  {outcome.position.position_id}: execution "
                f"{submission.record.execution_id} is {submission.record.state.value}"
            )


@exit_app.command("show")
def exit_show(
    position_id: Annotated[
        str | None,
        typer.Option("--position-id", help="One position, in full."),
    ] = None,
    evaluation: Annotated[
        bool,
        typer.Option("--evaluation", help="Show every policy outcome, not only the verdict."),
    ] = False,
) -> None:
    """Show the latest exit decision, or the latest run. (read-only)"""
    from trading_system.exit.report import render_decision, render_evaluation, render_run

    service = _exit_service(False)
    if position_id is not None:
        decision = service.repository.latest_for_position(position_id)
        if decision is None:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no exit decision recorded for {position_id}. "
                f"Run 'exit evaluate' first."
            )
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_decision(decision))
        if evaluation:
            stored = service.repository.get_evaluation(decision.evaluation_id)
            if stored is not None:
                console.print()
                console.print(render_evaluation(stored))
        return

    result = service.latest_run()
    if result is None:
        console.print(
            "[yellow]UNAVAILABLE[/yellow]  no exit evaluation has been run. "
            "Run 'exit evaluate' first."
        )
        raise typer.Exit(code=EXIT_OK)
    console.print()
    console.print(render_run(result))


@exit_app.command("history")
def exit_history(
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
    position_id: Annotated[
        str | None,
        typer.Option("--position-id", help="Only this position's judgements."),
    ] = None,
) -> None:
    """List recorded exit evaluations, newest first. (read-only)"""
    service = _exit_service(False)
    entries = service.history(limit=limit, position_id=position_id)
    if not entries:
        console.print("[yellow]No exit evaluations recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)

    table = Table(title="Exit evaluations", show_header=True, header_style="bold")
    for column in ("evaluated", "position", "symbol", "decision", "reason", "lifecycle", "note"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.evaluated_at.isoformat(),
            entry.position_id,
            entry.underlying,
            entry.decision,
            entry.reason_code,
            entry.lifecycle_state,
            "re-observation" if entry.reobserved else "",
        )
    console.print(table)


@exit_app.command("validate")
def exit_validate(
    position_id: Annotated[
        str | None,
        typer.Option("--position-id", help="Re-check a stored decision instead."),
    ] = None,
) -> None:
    """Validate the exit policy in force, or re-check a stored decision. (read-only)"""
    from trading_system.domain.enums import EXIT_POLICY_PRECEDENCE
    from trading_system.exit.report import render_decision
    from trading_system.exit.validation import configuration_report

    service = _exit_service(False)
    if position_id is not None:
        decision = service.repository.latest_for_position(position_id)
        if decision is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no exit decision for {position_id}")
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_decision(decision))
        raise typer.Exit(code=EXIT_OK)

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover
    policy = config.exit

    console.print("\n[bold]EXIT MANAGEMENT POLICY[/bold]")
    console.print("Model involved : [green]none[/green] — every exit decision is deterministic")
    console.print(f"Policy version : {policy.policy_version}")
    console.print(f"Evaluation     : {'enabled' if policy.enabled else 'DISABLED'}")
    console.print(
        f"Execution      : "
        f"{'enabled' if config.execution.enabled else 'DISABLED'} in config/execution.yaml, "
        f"and always additionally requires --confirm"
    )

    order = Table(title="Policy precedence (safety before profit-taking)", show_header=True)
    order.add_column("#")
    order.add_column("Policy")
    for index, kind in enumerate(EXIT_POLICY_PRECEDENCE, start=1):
        order.add_row(str(index), kind.value)
    console.print(order)

    globals_table = Table(title="Global envelope", show_header=True, header_style="bold")
    globals_table.add_column("Setting")
    globals_table.add_column("Value")
    for name, value in (
        ("expiration.warning_dte", str(policy.expiration.warning_dte)),
        ("expiration.force_exit_dte", str(policy.expiration.force_exit_dte)),
        ("expiration.block_on_unknown_calendar", str(policy.expiration.block_on_unknown_calendar)),
        ("data_quality.quote_field", policy.data_quality.quote_field.value),
        ("data_quality.max_quote_age_seconds", str(policy.data_quality.max_quote_age_seconds)),
        ("data_quality.on_unavailable", policy.data_quality.on_unavailable.value),
        ("data_quality.on_stale", policy.data_quality.on_stale.value),
        (
            "data_quality.allow_quote_field_substitution",
            str(policy.data_quality.allow_quote_field_substitution),
        ),
        ("trailing.activation_return_pct", str(policy.trailing.activation_return_pct)),
        ("trailing.trail_distance_pct", str(policy.trailing.trail_distance_pct)),
        ("trailing.allow_level_to_fall", str(policy.trailing.allow_level_to_fall)),
        ("take_profit.return_pct", str(policy.take_profit.return_pct)),
        ("max_loss.loss_pct", str(policy.max_loss.loss_pct)),
        ("max_loss.block_on_unavailable_basis", str(policy.max_loss.block_on_unavailable_basis)),
        ("thesis.exit_on_invalidated", str(policy.thesis.exit_on_invalidated)),
        ("thesis.allow_prose_interpretation", str(policy.thesis.allow_prose_interpretation)),
        ("order.limit_price_offset_pct", str(policy.order.limit_price_offset_pct)),
        ("allow_independent_leg_exit", str(policy.allow_independent_leg_exit)),
        ("require_broker_confirmation", str(policy.require_broker_confirmation)),
    ):
        globals_table.add_row(name, value)
    console.print(globals_table)

    layers = Table(title="Per-strategy narrowing", show_header=True, header_style="bold")
    for column in ("limit", "effective", "scope", "global", "strategy", "narrowing rule"):
        layers.add_column(column)
    for row in configuration_report(config):
        layers.add_row(
            row.name,
            row.value,
            row.scope.value,
            row.global_value,
            row.strategy_value or "-",
            row.narrowing_rule,
        )
    console.print(layers)

    console.print(
        "\nA strategy may NARROW any global safety limit and may never widen one. Widening is a "
        "configuration load failure naming the strategy and the limit — never a silent clamp."
    )
    console.print(
        "A multi-leg structure exits as ONE combo order or the decision is refused: "
        "allow_independent_leg_exit fails to load if set, globally or per strategy."
    )
    console.print(
        "An UNKNOWN exit is never re-sent. It is resolved by observing the broker, and no "
        "amount of elapsed time is evidence."
    )
    console.print("[green]PASS[/green]  Policy is valid. No orders were submitted.")


@exit_app.command("explain")
def exit_explain(
    position_id: Annotated[str, typer.Option("--position-id", help="The position to explain.")],
) -> None:
    """Explain one position: lifecycle, trailing state and every policy. (read-only)"""
    from trading_system.exit.report import (
        render_evaluation,
        render_lifecycle,
        render_lifecycle_event,
        render_trailing,
    )

    service = _exit_service(False)
    lifecycle = service.lifecycle(position_id)
    if lifecycle is None:
        console.print(
            f"[yellow]UNAVAILABLE[/yellow]  no lifecycle recorded for {position_id}. "
            f"Run 'exit evaluate' first."
        )
        raise typer.Exit(code=EXIT_OK)

    console.print()
    console.print(render_lifecycle(lifecycle))

    trailing = service.repository.trailing(position_id)
    if trailing is not None:
        console.print()
        console.print(render_trailing(trailing))

    decision = service.repository.latest_for_position(position_id)
    if decision is not None:
        stored = service.repository.get_evaluation(decision.evaluation_id)
        if stored is not None:
            console.print()
            console.print(render_evaluation(stored))

    events = service.repository.lifecycle_events(position_id)
    console.print(f"\n[bold]LIFECYCLE HISTORY[/bold] (append-only, {len(events)} event(s))")
    for event in events:
        console.print(f"  {render_lifecycle_event(event)}")
    if not events:
        console.print("  (none)")


@positions_app.command("monitor")
def positions_monitor(
    simulated: SimulatedOption = False,
    capture: Annotated[
        bool,
        typer.Option("--capture", help="Read the broker first instead of using the last snapshot."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Evaluate and print; write nothing at all."),
    ] = False,
) -> None:
    """Evaluate every open position for exit. (mutates local state)

    The scheduled operation. Safe to run repeatedly and safe to run from a
    scheduler: nothing is held in process memory, and a re-run over unchanged
    state re-observes rather than deciding again.

    It submits nothing. A position that triggers an exit is *recorded* as
    requiring one; closing it needs ``exit run --confirm``.
    """
    service = _exit_service(simulated)
    run = service.monitor(capture=capture, authorized=False, dry_run=dry_run)
    _print_exit_run(run, authorized=False)
    if dry_run:
        console.print("[yellow]DRY RUN[/yellow]  nothing was written.")


@test_app.command("exit")
def test_exit(simulated: SimulatedOption = True) -> None:
    """Diagnose the exit subsystem end to end, submitting nothing. (read-only)

    Loads the policy, resolves the per-strategy narrowing, lists every open
    position, evaluates each of them and prints the verdicts — all without
    constructing a writable broker or building a single order.
    """
    from trading_system.exit.report import render_run

    service = _exit_service(simulated)
    console.print("\n[bold]EXIT SUBSYSTEM DIAGNOSTIC[/bold]")
    console.print(f"Evaluation enabled : {service.enabled}")
    console.print(f"Policy precedence  : {len(service.engine.precedence)} policies")
    console.print("Model involved     : [green]none[/green]")

    positions = service.open_positions()
    console.print(f"Open positions     : {len(positions)}")
    for position in positions:
        console.print(
            f"  {position.position_id}  {position.underlying} {position.strategy.value}  "
            f"held={_or_dash(position.observed_units)}  "
            f"lifecycle={position.lifecycle.state.value}"
        )

    run = service.monitor(authorized=False, dry_run=True)
    console.print()
    console.print(render_run(run.result))
    console.print(f"\nOrders submitted: [green]{run.orders_submitted}[/green]")
    if run.orders_submitted:
        _fail("a diagnostic submitted an order. This must never happen.")
    console.print("[green]PASS[/green]  Exit evaluation reached no broker and sent nothing.")


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


# ---------------------------------------------------------------------------
# Operations (Milestone 11)
# ---------------------------------------------------------------------------
#
# Two rules hold across this whole group, and both are asserted by tests:
#
# * nothing here decides a trade. Health reads, alerts notify, the scheduler
#   orchestrates services that already made their decisions;
# * only one scheduled job can submit an order, it needs two switches, and
#   every command prints the count it read off the service it invoked rather
#   than asserting zero.
def _operations_service() -> OperationsService:
    """Build the Milestone 11 operations root, or fail with a diagnostic."""
    from trading_system.operations.service import OperationsService as _Service

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return _Service(settings=settings, config=config)


def _scheduler() -> Any:
    """Build the scheduler, surfacing an unusable cadence as a fatal error."""
    from trading_system.operations.scheduler import Scheduler, SchedulerError

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    try:
        return Scheduler(settings=settings, config=config)
    except (SchedulerError, KeyError) as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover


_HEALTH_STYLES = {
    "HEALTHY": "green",
    "DEGRADED": "yellow",
    "UNAVAILABLE": "red",
    "BLOCKED": "bold red",
    "UNKNOWN": "yellow",
}


@ops_app.command("health")
def ops_health(
    probe_broker: Annotated[
        bool,
        typer.Option("--broker", help="Also open one read-only connection and probe it."),
    ] = False,
    store: Annotated[bool, typer.Option("--store/--no-store", help="Record the report.")] = True,
) -> None:
    """Report trading health and observability health, separately. (read-only)

    The separation is the point: an unreachable Grafana degrades observability
    health and leaves trading health untouched. A system that reported a
    telemetry outage as a trading fault would train its operators to ignore the
    banner that means a broker is unreachable.

    The broker is **not** probed unless asked. A probe costs one of the
    connection's reliable round trips, and an unprobed broker is reported
    ``UNKNOWN`` rather than healthy — "all green" must not be achievable by not
    looking.
    """
    from trading_system.domain.enums import HealthDomain, HealthStatus

    service = _operations_service()
    report = service.health(probe_broker=probe_broker, store=store)

    def styled(status: HealthStatus) -> str:
        style = _HEALTH_STYLES.get(status.value, "white")
        return f"[{style}]{status.value}[/{style}]"

    console.print("\n[bold]OPERATIONAL HEALTH[/bold]")
    console.print(f"As of      : {report.as_of.isoformat()}")
    console.print(f"Mode       : {report.trading_mode.value}")
    console.print(f"Versions   : app {report.application_version}, config {report.config_version}")
    console.print(f"\nTRADING        : {styled(report.trading_status)}")
    console.print(f"OBSERVABILITY  : {styled(report.observability_status)}")

    for domain in (HealthDomain.TRADING, HealthDomain.OBSERVABILITY):
        components = report.for_domain(domain)
        if not components:
            continue
        table = Table(title=f"{domain.value} components", show_header=True, header_style="bold")
        table.add_column("Component")
        table.add_column("Status")
        table.add_column("Summary")
        for component in components:
            table.add_row(component.component.value, styled(component.status), component.summary)
        console.print(table)

    console.print(
        "\n[dim]An unreachable telemetry backend degrades OBSERVABILITY and cannot move "
        "TRADING. Trading health is derived from trading components alone.[/dim]"
    )
    if report.trading_status in (HealthStatus.BLOCKED, HealthStatus.UNAVAILABLE):
        raise typer.Exit(code=EXIT_ERROR)


@ops_app.command("scheduler")
def ops_scheduler(
    action: Annotated[
        str,
        typer.Argument(help="plan | tick | start | status"),
    ] = "plan",
    max_ticks: Annotated[
        int | None,
        typer.Option("--max-ticks", help="Stop after this many ticks (start only)."),
    ] = None,
    within: Annotated[
        int, typer.Option("--within", help="Minutes ahead to show (plan only).")
    ] = 60,
) -> None:
    """Inspect or run the scheduler. (``tick`` and ``start`` mutate state)

    ``plan``    what would run now and what fires next. Side-effect free.
    ``status``  the last tick, and any job whose completion was never recorded.
    ``tick``    run everything due once, each job isolated and bounded.
    ``start``   tick on the configured cadence until stopped.

    Only ``exit_management`` can place an order, and only with
    ``execution.enabled`` **and** ``authorize_exits`` on that job. Every run
    prints the order count read off the services it invoked.
    """
    from trading_system.domain.enums import JobStatus

    scheduler = _scheduler()

    if action == "plan":
        plans = scheduler.plan()
        table = Table(title="Scheduler plan", show_header=True, header_style="bold")
        for column in ("job", "cron", "due", "will run", "why not", "next fire", "can submit"):
            table.add_column(column)
        for plan in plans:
            table.add_row(
                plan.job,
                plan.schedule.cron,
                "yes" if plan.due else "no",
                "[green]yes[/green]" if plan.will_run else "no",
                plan.skip_reason.value if plan.skip_reason else "-",
                plan.next_fire_at.isoformat() if plan.next_fire_at else "never",
                "[yellow]ORDERS[/yellow]" if plan.definition.can_submit_orders else "no",
            )
        console.print(table)
        console.print(
            f"\n[dim]Timezone {scheduler.timezone}. Scheduler "
            f"{'enabled' if scheduler.enabled else 'DISABLED'} in config/schedules.yaml.[/dim]"
        )
        upcoming = scheduler.upcoming(within_minutes=within)
        console.print(f"{len(upcoming)} job(s) fire within {within} minutes.")
        return

    if action == "status":
        latest = scheduler.repository.latest_scheduler_run()
        if latest is None:
            console.print("[yellow]No scheduler tick has been recorded.[/yellow]")
            raise typer.Exit(code=EXIT_OK)
        console.print(f"\n[bold]LAST TICK[/bold]  {latest.status.value}")
        console.print(f"Scheduled for : {latest.scheduled_for.isoformat()}")
        console.print(f"Jobs          : {len(latest.runs)}")
        console.print(f"Orders        : {latest.orders_submitted}")
        unfinished = scheduler.repository.unfinished_job_runs()
        if unfinished:
            console.print(
                f"\n[yellow]{len(unfinished)} job run(s) never recorded a result.[/yellow]\n"
                f"They are neither failures nor successes — the work may have completed. "
                f"Starting the scheduler reclassifies them as UNKNOWN and, because every job "
                f"is idempotent against persisted state, the next firing establishes the "
                f"answer."
            )
            for run in unfinished:
                console.print(f"  {run.job}  scheduled {run.scheduled_for.isoformat()}")
        return

    if action in ("tick", "start"):
        scheduler.recover()
        ticks = (
            [scheduler.tick()] if action == "tick" else scheduler.serve(max_ticks=max_ticks or 1)
        )
        submitted = sum(tick.orders_submitted for tick in ticks)
        for tick in ticks:
            console.print(
                f"\n[bold]TICK[/bold] {tick.scheduled_for.isoformat()}  {tick.status.value}"
            )
            for run in tick.runs:
                style = {
                    JobStatus.SUCCESS: "green",
                    JobStatus.SKIPPED: "dim",
                    JobStatus.FAILED: "red",
                    JobStatus.UNKNOWN: "yellow",
                    JobStatus.BLOCKED: "yellow",
                }.get(run.status, "white")
                console.print(
                    f"  [{style}]{run.status.value:<8}[/{style}] {run.job:<20} {run.summary or ''}"
                )
        style = "green" if submitted == 0 else "yellow"
        console.print(f"\nOrders submitted : [{style}]{submitted}[/{style}]")
        return

    _fail(f"unknown scheduler action {action!r}; use plan, tick, start or status")


@ops_app.command("jobs")
def ops_jobs(
    job: Annotated[str | None, typer.Option("--job", help="Only this job's runs.")] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run this job once, now, outside the cadence."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many runs to show.")] = 20,
) -> None:
    """List registered jobs and their run history. (``--run`` mutates state)

    ``--run`` invokes one job immediately. It still goes through every guard
    the cadence applies — the market calendar, the enabled switch, the
    duplicate check against the *stored* run for this instant — so running a
    job by hand cannot do something the scheduler would have refused.
    """
    from trading_system.domain.enums import JobStatus

    scheduler = _scheduler()

    if run is not None:
        if run not in scheduler.registry:
            _fail(f"no job named {run!r}. Registered: {', '.join(sorted(scheduler.registry))}")
        record = scheduler.run_job(run)
        style = "green" if record.status is JobStatus.SUCCESS else "yellow"
        console.print(
            f"\n[{style}]{record.status.value}[/{style}]  {record.job}\n{record.summary or ''}"
        )
        if record.error_type:
            console.print(f"[red]{record.error_type}[/red]: {record.error_message}")
        console.print(f"Orders submitted : {record.orders_submitted}")
        if record.status is JobStatus.FAILED:
            raise typer.Exit(code=EXIT_ERROR)
        return

    registry = Table(title="Registered jobs", show_header=True, header_style="bold")
    for column in ("job", "cron", "enabled", "market hours", "timeout", "can submit"):
        registry.add_column(column)
    for name, definition in sorted(scheduler.registry.items()):
        schedule = scheduler._config.schedules.jobs[name]
        registry.add_row(
            name,
            schedule.cron,
            "yes" if schedule.enabled else "[dim]no[/dim]",
            "yes" if schedule.market_hours_only else "no",
            f"{schedule.timeout_seconds:g}s",
            "[yellow]ORDERS[/yellow]" if definition.can_submit_orders else "no",
        )
    console.print(registry)

    runs = scheduler.repository.job_runs(limit=limit, job=job)
    if not runs:
        console.print("\n[yellow]No job runs recorded.[/yellow]")
        return
    history = Table(title="Recent runs", show_header=True, header_style="bold")
    for column in ("scheduled", "job", "status", "why", "duration", "orders", "summary"):
        history.add_column(column)
    for record in runs:
        history.add_row(
            record.scheduled_for.strftime("%Y-%m-%d %H:%M"),
            record.job,
            record.status.value,
            (record.skip_reason.value if record.skip_reason else record.error_type) or "-",
            f"{record.duration_seconds:.2f}s" if record.duration_seconds is not None else "-",
            str(record.orders_submitted),
            (record.summary or "")[:60],
        )
    console.print(history)


@ops_app.command("alerts")
def ops_alerts(
    evaluate: Annotated[
        bool,
        typer.Option("--evaluate", help="Evaluate the rules now and notify. (mutates state)"),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many alerts to show.")] = 20,
) -> None:
    """Show recorded alerts, or evaluate the rules now. (read-only by default)

    An alert is a **notification**. Nothing in the alerting path can place,
    cancel or modify an order, and a boundary test walks the import graph to
    prove it. Safety is enforced by the domain; this is how a person finds out.
    """
    service = _operations_service()

    if evaluate:
        alerts = service.evaluate_alerts()
        if not alerts:
            console.print("[green]No alert condition is currently met.[/green]")
            return
    else:
        alerts = service.alerts(limit=limit)
        if not alerts:
            console.print("[yellow]No alerts recorded.[/yellow]")
            return

    table = Table(title="Alerts", show_header=True, header_style="bold")
    for column in ("raised", "severity", "code", "subject", "count", "summary", "notified"):
        table.add_column(column)
    for alert in alerts:
        style = {"CRITICAL": "bold red", "WARNING": "yellow", "INFO": "dim"}.get(
            alert.severity.value, "white"
        )
        table.add_row(
            alert.raised_at.strftime("%Y-%m-%d %H:%M"),
            f"[{style}]{alert.severity.value}[/{style}]",
            alert.code.value,
            alert.subject,
            f"{alert.occurrences}/{alert.threshold}",
            alert.summary[:60],
            ", ".join(alert.notified_channels) or "-",
        )
    console.print(table)
    console.print(
        "\n[dim]Alerts notify. They never execute a trade, and no configuration makes "
        "them able to.[/dim]"
    )


@ops_app.command("metrics")
def ops_metrics() -> None:
    """Show the telemetry configuration and the metric vocabulary. (read-only)

    Also prints the **cardinality guard**: the labels refused at the point of
    recording, whatever configuration says. A domain identifier as a metric
    label is one time series per trade, and the consequence lands on a system
    this one does not own.
    """
    from trading_system.observability import metrics as observability_metrics
    from trading_system.observability.runtime import telemetry_status

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError("unreachable") from exc  # pragma: no cover
    observability = settings.resolved_observability(config.observability)

    console.print("\n[bold]TELEMETRY[/bold]")
    console.print(f"Enabled        : {'yes' if observability.enabled else 'no (shipped default)'}")
    console.print(f"Status         : {telemetry_status().value}")
    console.print(f"Service        : {observability.service_name}")
    console.print(f"Environment    : {observability.environment}")
    console.print(
        f"Endpoint       : {observability.exporter.endpoint}  (a COLLECTOR, never a backend)"
    )
    console.print(f"Protocol       : {observability.exporter.protocol}")
    console.print(f"Sampling       : {observability.sampling.ratio}")
    console.print(f"Metrics        : {'on' if observability.metrics.enabled else 'off'}")
    console.print(
        f"Fail open      : {observability.fail_open}  "
        f"(a telemetry failure can never change a trading decision)"
    )

    table = Table(title="Instruments", show_header=True, header_style="bold")
    table.add_column("#")
    table.add_column("Metric")
    for index, name in enumerate(observability_metrics.METRIC_NAMES, start=1):
        table.add_row(str(index), name)
    console.print(table)

    console.print("\n[bold]CARDINALITY GUARD[/bold]")
    console.print(
        "These are refused as metric labels at the point of recording, whatever "
        "configuration says. They belong in traces and logs:"
    )
    console.print("  " + ", ".join(sorted(observability_metrics.FORBIDDEN_LABELS)))


# ---------------------------------------------------------------------------
# Realised profit and loss (Milestone 11)
# ---------------------------------------------------------------------------
def _pnl_service() -> PnLService:
    """Build the Milestone 11 profit-and-loss root, or fail with a diagnostic."""
    from trading_system.pnl.service import PnLService as _Service

    settings = _load_settings()
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        err_console.print(f"[red]CONFIGURATION ERROR[/red]\n{exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc
    return _Service(settings=settings, config=config)


@pnl_app.command("show")
def pnl_show(
    position_id: Annotated[
        str | None, typer.Option("--position-id", help="One position's result, in full.")
    ] = None,
    pnl_id: Annotated[str | None, typer.Option("--pnl-id", help="One stored result.")] = None,
    daily: Annotated[
        bool, typer.Option("--daily", help="Today's roll-up instead of individual trades.")
    ] = False,
) -> None:
    """Show realised results. (read-only)

    Every figure comes from **broker-confirmed fills** and nothing else.
    ``NOT_AVAILABLE`` is a real answer and prints no number: a result assembled
    from a guessed commission or an assumed multiplier would be used by the
    daily loss limit as though it had been measured.
    """
    from trading_system.pnl.report import render_daily, render_realized, render_summary

    service = _pnl_service()

    if daily:
        rollup = service.daily_rollup(store=False)
        if rollup is None:
            console.print(
                "[yellow]UNAVAILABLE[/yellow]  no position closed in this session, so there "
                "is no daily result. A day with no trades is not a day that broke even."
            )
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_daily(rollup))
        return

    if pnl_id is not None:
        stored = service.get(pnl_id)
        if stored is None:
            console.print(f"[yellow]UNAVAILABLE[/yellow]  no realised result {pnl_id}")
            raise typer.Exit(code=EXIT_OK)
        console.print()
        console.print(render_realized(stored))
        return

    if position_id is not None:
        records = service.for_position(position_id)
        if not records:
            console.print(
                f"[yellow]UNAVAILABLE[/yellow]  no realised result for {position_id}. "
                f"A result exists once the broker confirms the position is closed."
            )
            raise typer.Exit(code=EXIT_OK)
        for record in records:
            console.print()
            console.print(render_realized(record))
        return

    console.print()
    console.print(render_summary(service.repository.all(limit=50)))


@pnl_app.command("history")
def pnl_history(
    limit: Annotated[int, typer.Option("--limit", help="How many entries to show.")] = 20,
    daily: Annotated[bool, typer.Option("--daily", help="Daily roll-ups instead.")] = False,
) -> None:
    """List realised results or daily roll-ups, newest first. (read-only)"""
    service = _pnl_service()

    if daily:
        records = service.daily_history(limit=limit)
        if not records:
            console.print("[yellow]No daily results recorded.[/yellow]")
            raise typer.Exit(code=EXIT_OK)
        table = Table(title="Daily realised results", show_header=True, header_style="bold")
        for column in ("session", "status", "realised", "loss", "closed", "unavailable"):
            table.add_column(column)
        for record in records:
            table.add_row(
                record.session_date.isoformat(),
                record.status.value,
                _or_dash(record.realized_pnl),
                _or_dash(record.realized_loss),
                str(record.positions_closed),
                str(record.positions_without_result),
            )
        console.print(table)
        console.print(
            "\n[dim]An UNKNOWN day reports no total. That is not zero loss: it is an "
            "absence of knowledge about a day on which money moved.[/dim]"
        )
        return

    entries = service.history(limit=limit)
    if not entries:
        console.print("[yellow]No realised results recorded.[/yellow]")
        raise typer.Exit(code=EXIT_OK)
    table = Table(title="Realised results", show_header=True, header_style="bold")
    for column in ("computed", "position", "symbol", "strategy", "status", "result", "session"):
        table.add_column(column)
    for entry in entries:
        table.add_row(
            entry.computed_at.strftime("%Y-%m-%d %H:%M"),
            entry.pnl_id,
            entry.underlying,
            entry.strategy,
            entry.status,
            _or_dash(entry.realized_pnl),
            entry.session_date or "-",
        )
    console.print(table)


@pnl_app.command("settle")
def pnl_settle(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compute everything and move no capital."),
    ] = False,
) -> None:
    """Compute results for closed positions and settle their capital. (mutates state)

    Capital returns to the campaign on **broker-confirmed closure** and on
    nothing weaker: not a requested exit, not a submitted one, not a reported
    fill. An ``UNKNOWN`` execution never settles, and no configuration permits
    it. Every refusal names the evidence that was missing.

    Safe to run repeatedly: results are content-addressed, settlement outcomes
    are deltas, and the reservation ledger recognises a replayed event — so the
    second run over unchanged evidence returns no capital.
    """
    from trading_system.pnl.report import render_run, render_settlement

    service = _pnl_service()
    run = service.run(dry_run=dry_run)
    console.print()
    console.print(render_run(run.result))

    for record in run.settlements:
        if record.settlement.status.value == "BLOCKED" or record.applied:
            console.print()
            console.print(render_settlement(record.settlement))

    if dry_run:
        console.print("\n[yellow]DRY RUN[/yellow]  nothing was written and no capital moved.")
    console.print(f"\nOrders submitted : [green]{run.orders_submitted}[/green]")


# ---------------------------------------------------------------------------
# Readiness (Milestone 12)
#
# The acceptance gate. Everything in this group REPORTS: no command here can
# change TRADING_MODE, LIVE_TRADING_CONFIRMED, LIVE_READINESS_CHECKLIST_SIGNED_OFF,
# execution.enabled or IBKR_READ_ONLY, and there is no
# "readiness == true -> enable execution" path anywhere in the package.
#
# The one exception is `readiness paper`, which submits a real paper order and
# is behind four independent gates — see its docstring. It is separated from
# everything else in this group by more than a flag.
# ---------------------------------------------------------------------------
def _readiness_service() -> ReadinessService:
    """Build the Milestone 12 composition root.

    Tolerates a configuration that will not load: an assessor that could not
    start because the configuration is broken would be unable to report the one
    thing it most needs to.
    """
    from trading_system.readiness.service import ReadinessService

    return ReadinessService.build(settings=_load_settings())


@readiness_app.command("validate")
def readiness_validate() -> None:
    """Show the readiness policy in force. Collects nothing. (read-only)

    What "ready" *means* — which criteria block which level, how long each kind
    of evidence stays usable — without gathering any evidence or reaching any
    conclusion.
    """
    from trading_system.readiness.criteria import READINESS_CRITERIA

    service = _readiness_service()
    policy = service.policy
    if policy is None:
        _fail("configuration did not load; the readiness policy cannot be shown")
        return

    console.print(f"\n[bold]Readiness policy[/bold]  version {policy.config_version}")
    console.print(f"Criteria defined : {len(READINESS_CRITERIA)}")
    console.print(f"Blocking paper   : {len(policy.paper_blocking)}")
    console.print(f"Blocking review  : {len(policy.live_review_blocking)}")
    console.print(f"Revision-bound   : {len(policy.revision_bound)}")

    unknown = policy.unknown_criteria()
    if unknown:
        console.print(
            "\n[red]CONFIGURATION PROBLEM[/red]  config/readiness.yaml names criteria that "
            "no definition covers. They can never be satisfied, so the level they block can "
            "never open:"
        )
        for criterion in unknown:
            console.print(f"  - {criterion.value}")
        raise typer.Exit(code=EXIT_ERROR)

    table = Table(title="Readiness criteria", expand=True)
    table.add_column("Criterion", width=36)
    table.add_column("Domain", width=22)
    table.add_column("Blocks", width=12)
    table.add_column("Freshness", width=24)
    for definition in READINESS_CRITERIA:
        levels = policy.blocking_levels(definition.criterion_id)
        blocks = (
            "+".join("paper" if "PAPER" in level.value else "live" for level in levels)
            if levels
            else "advisory"
        )
        if policy.is_revision_bound(definition.criterion_id):
            freshness = "git revision"
        elif definition.window:
            seconds = policy.window_seconds(definition.window)
            freshness = f"{definition.window} ({seconds:.0f}s)" if seconds else definition.window
        else:
            freshness = "-"
        table.add_row(definition.criterion_id.value, definition.domain.value, blocks, freshness)
    console.print(table)
    console.print(
        "\n[dim]Readiness reports. Nothing in this package can change TRADING_MODE, "
        "LIVE_TRADING_CONFIRMED, LIVE_READINESS_CHECKLIST_SIGNED_OFF, execution.enabled "
        "or IBKR_READ_ONLY.[/dim]"
    )


@readiness_app.command("check")
def readiness_check(
    full: Annotated[
        bool,
        typer.Option("--full", help="Run every collector: toolchain, broker, observability."),
    ] = False,
    toolchain: Annotated[
        bool, typer.Option("--toolchain", help="Run pytest, ruff and mypy. Takes minutes.")
    ] = False,
    suites: Annotated[
        bool, typer.Option("--suites", help="Run the targeted safety suites.")
    ] = False,
    broker: Annotated[
        bool, typer.Option("--broker", help="One short-lived READ-ONLY broker connection.")
    ] = False,
    reconciliation: Annotated[
        bool, typer.Option("--reconciliation", help="Run reconciliation against the broker.")
    ] = False,
    observability: Annotated[
        bool,
        typer.Option("--observability", help="Probe a running observability stack over HTTP."),
    ] = False,
    simulated: SimulatedOption = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Evaluate and store nothing.")] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Show NOT_TESTED criteria too.")
    ] = False,
) -> None:
    """Assess readiness from evidence. Submits 0 orders. (mutates state)

    With no flags this is the **offline** scope: configuration, git, test
    isolation, secrets, masking, the stores. Seconds, no subprocess, no
    network. Everything not collected is reported ``NOT_TESTED`` rather than
    passing — "we never looked" and "we looked and it was fine" are different
    facts, and the cheap default deliberately cannot certify anything.

    The result is immutable and content-addressed: re-running over unchanged
    evidence records a re-observation rather than a second, contradictory copy.
    """
    from trading_system.readiness.report import render_run
    from trading_system.readiness.service import CheckScope

    scope = (
        CheckScope.full(simulated=simulated)
        if full
        else CheckScope(
            toolchain=toolchain,
            safety_suites=suites,
            broker=broker,
            reconciliation=reconciliation,
            observability=observability,
            simulated=simulated,
        )
    )

    service = _readiness_service()
    if scope.toolchain or scope.safety_suites:
        console.print("[dim]Running the toolchain and suites. This takes minutes.[/dim]")

    result = service.check(scope, store=not dry_run)
    console.print()
    render_run(console, result.run, verbose=verbose)

    for warning in result.warnings:
        console.print(f"[yellow]WARNING[/yellow]  {warning}")

    if dry_run:
        console.print("\n[yellow]DRY RUN[/yellow]  nothing was stored.")
    elif result.stored:
        state = "recorded" if result.is_new else "re-observed (identical to a stored run)"
        console.print(f"\nStored: {result.run.readiness_run_id} — {state}")

    console.print(f"Orders submitted : [green]{result.run.orders_submitted}[/green]")


@readiness_app.command("show")
def readiness_show(
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="A specific readiness run.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Show NOT_TESTED criteria too.")
    ] = False,
) -> None:
    """Show a stored readiness run, latest by default. (read-only)"""
    from trading_system.readiness.report import render_run, render_signoff

    service = _readiness_service()
    run = service.get(run_id) if run_id else service.latest()
    if run is None:
        _fail(
            f"no readiness run {'with id ' + run_id + ' ' if run_id else ''}is stored. "
            f"Run `readiness check` first."
        )
        return

    console.print()
    render_run(console, run, verbose=verbose)
    render_signoff(console, service.repository.latest_signoff())


@readiness_app.command("history")
def readiness_history(
    limit: Annotated[int, typer.Option("--limit", help="How many runs to list.")] = 20,
) -> None:
    """List stored readiness runs, newest first. (read-only)"""
    service = _readiness_service()
    entries = service.history(limit)
    if not entries:
        console.print("No readiness runs are stored. Run `readiness check` first.")
        return

    table = Table(title="Readiness history", expand=True)
    table.add_column("Evaluated", width=22)
    table.add_column("Level", width=22)
    table.add_column("Status", width=12)
    table.add_column("Revision", width=14)
    table.add_column("Tree", width=7)
    table.add_column("Run id", overflow="fold")
    for entry in entries:
        table.add_row(
            entry.evaluated_at.isoformat(timespec="seconds"),
            entry.level,
            entry.status,
            (entry.git_revision or "-")[:12],
            "clean" if entry.working_tree_clean else "dirty",
            entry.readiness_run_id,
        )
    console.print(table)


@readiness_app.command("explain")
def readiness_explain(
    criterion: Annotated[str | None, typer.Option("--criterion", help="One criterion id.")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="A specific run.")] = None,
) -> None:
    """Explain one criterion, or everything holding the next level shut. (read-only)"""
    from trading_system.domain.enums import ReadinessCriterionId
    from trading_system.readiness.report import STATUS_STYLE

    service = _readiness_service()
    run = service.get(run_id) if run_id else service.latest()
    if run is None or run.assessment is None:
        _fail("no readiness assessment is stored. Run `readiness check` first.")
        return

    criteria = list(run.assessment.criteria)
    if criterion:
        try:
            wanted = ReadinessCriterionId(criterion.upper())
        except ValueError:
            _fail(f"{criterion!r} is not a readiness criterion. See `readiness validate`.")
            return
        criteria = [item for item in criteria if item.criterion_id is wanted]

    for item in criteria:
        if not criterion and item.satisfied:
            continue
        console.print(
            f"\n[{STATUS_STYLE[item.status]}]{item.status.value}[/]  "
            f"[bold]{item.criterion_id.value}[/bold]  ({item.domain.value})"
        )
        console.print(f"  Asserts   : {item.title}")
        console.print(f"  Reason    : {item.reason_code.value}")
        console.print(f"  Detail    : {item.detail}")
        console.print(f"  Evidence  : {item.evidence_id or 'none collected'}")
        if item.evidence_source:
            console.print(f"  Source    : {item.evidence_source}")
        if item.observed_at:
            console.print(f"  Observed  : {item.observed_at.isoformat()}")
        if item.evidence_age_seconds is not None:
            console.print(f"  Age       : {item.evidence_age_seconds:.0f}s")
        if item.artifact_ids:
            console.print(f"  Artifacts : {', '.join(item.artifact_ids)}")
        if item.blocking_for:
            console.print(f"  Blocks    : {', '.join(level.value for level in item.blocking_for)}")


@readiness_app.command("signoff")
def readiness_signoff(
    signed_by: Annotated[
        str | None,
        typer.Option("--signed-by", help="Who is signing. Never inferred from the environment."),
    ] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="The run being signed.")] = None,
    note: Annotated[str | None, typer.Option("--note", help="Free text from the signer.")] = None,
    revoke: Annotated[bool, typer.Option("--revoke", help="Withdraw a previous sign-off.")] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Required. Records the decision.")
    ] = False,
) -> None:
    """Record a human live-readiness sign-off. ENABLES NOTHING. (mutates state)

    Signing records that a named person reviewed specific evidence at a
    specific revision. It does **not** set ``TRADING_MODE``,
    ``LIVE_TRADING_CONFIRMED``, ``LIVE_READINESS_CHECKLIST_SIGNED_OFF``,
    ``execution.enabled`` or ``IBKR_READ_ONLY`` — those stay in the
    environment, where a human sets them deliberately and a reviewer sees the
    diff. There is no automatic transition from READY_FOR_LIVE_REVIEW to LIVE.

    The signer is required and is never inferred: ``$USER`` is whoever ran the
    process and a git ``user.name`` is a string anybody can set.
    """
    from trading_system.domain.enums import SignoffStatus
    from trading_system.readiness.report import render_signoff
    from trading_system.readiness.signoff import (
        SignoffRefusedError,
        SignoffRequest,
        build_signoff,
    )

    service = _readiness_service()
    if service.config is None:
        _fail("configuration did not load; refusing to record a sign-off against it")
        return

    run = service.get(run_id) if run_id else service.latest()
    if run is None:
        _fail("no readiness run is stored. Run `readiness check` first.")
        return

    if not confirm:
        console.print(
            "\n[yellow]Not recorded.[/yellow] Pass --confirm to record the sign-off.\n"
            f"Would sign readiness run [bold]{run.readiness_run_id}[/bold] "
            f"({run.level.value}) at revision {(run.git_revision or 'unknown')[:12]}.\n"
            "[dim]A sign-off records a human decision. It enables no trading.[/dim]"
        )
        return

    try:
        signoff = build_signoff(
            SignoffRequest(
                run=run,
                signed_by=(signed_by or "").strip(),
                signed_at=service.now(),
                note=note,
                status=SignoffStatus.REVOKED if revoke else SignoffStatus.SIGNED,
            ),
            service.config.readiness.signoff,
        )
    except SignoffRefusedError as exc:
        _fail(str(exc))
        return

    service.repository.save_signoff(signoff)
    console.print()
    render_signoff(console, signoff)


@readiness_app.command("paper")
def readiness_paper(
    i_understand_this_submits_a_real_paper_order: Annotated[
        bool,
        typer.Option(
            "--i-understand-this-submits-a-real-paper-order",
            help="Required. Deliberately NOT --confirm.",
        ),
    ] = False,
) -> None:
    """Authorise the real-paper-order validation. Submits 0 orders. (read-only)

    Checks every gate that guards a real paper submission and reports which one
    refused. It **sends nothing itself**: the audited order path runs through
    `execution/service.py`, the only caller of `build_execution_broker`, and a
    second path here would weaken a Milestone 8 invariant that two boundary
    suites assert.

    Every gate must be satisfied and none implies another:

    \b
      1. readiness.paper_execution.enabled   config/readiness.yaml, ships false
      2. ALLOW_LIVE_TESTS=true               environment
      3. RUN_PAPER_EXECUTION_TESTS=true      environment
      4. --i-understand-this-submits-a-real-paper-order

    The fourth is deliberately not `--confirm`: that flag already authorises an
    ordinary execution run, and one word must not authorise two actions.
    """
    from trading_system.readiness.paper_gate import (
        WARNING_TEXT,
        PaperGateRefusedError,
        authorize_paper_validation,
    )

    console.print(Panel(WARNING_TEXT, border_style="red", title="[red]PAPER ORDER[/red]"))

    # The same settings and configuration the readiness runs are assessed
    # against. Building a second pair here would let the gates be checked
    # against a configuration nobody had assessed.
    service = _readiness_service()
    config = service.config
    if config is None:
        _fail("configuration did not load. Nothing was authorised.")
        return

    try:
        authorization = authorize_paper_validation(
            settings=service.settings,
            config=config,
            authorized=i_understand_this_submits_a_real_paper_order,
        )
    except PaperGateRefusedError as exc:
        console.print(f"\n[yellow]REFUSED[/yellow]  {exc}")
        raise typer.Exit(code=EXIT_ERROR) from exc

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("MODE", authorization.mode)
    table.add_row("AUTHORIZED", "YES")
    table.add_row("ORDERS_SUBMITTED", str(authorization.orders_submitted))
    for gate, satisfied in authorization.gates.items():
        table.add_row(gate, "[green]ok[/green]" if satisfied else "[red]no[/red]")
    console.print(Panel(table, title="Paper validation authorisation"))

    console.print(f"\n[dim]{authorization.detail}[/dim]")
    console.print("\nRun the validation with:\n")
    console.print(f"  {authorization.next_command}")


@test_app.command("readiness")
def test_readiness() -> None:
    """Readiness diagnostic: the policy, and the latest verdict. (read-only)

    Submits nothing, collects nothing and opens no connection — it reads what
    is already stored. Use `readiness check` to gather fresh evidence.
    """
    from trading_system.readiness.criteria import READINESS_CRITERIA
    from trading_system.readiness.report import render_summary

    service = _readiness_service()
    policy = service.policy
    console.print("\n[bold]Readiness diagnostic[/bold]")
    console.print(f"Criteria defined : {len(READINESS_CRITERIA)}")
    if policy is None:
        console.print("[red]Configuration did not load.[/red]")
        raise typer.Exit(code=EXIT_ERROR)
    console.print(f"Policy version   : {policy.config_version}")
    console.print(f"Blocking paper   : {len(policy.paper_blocking)}")
    console.print(f"Blocking review  : {len(policy.live_review_blocking)}")

    run = service.latest()
    if run is None:
        console.print("\nNo readiness run is stored yet. Run `readiness check`.")
        return
    console.print()
    render_summary(console, run)
    console.print(f"Orders submitted : [green]{run.orders_submitted}[/green]")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
