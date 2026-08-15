"""Readiness evidence collectors (Milestone 12).

The **impure** half of the milestone. Collectors run commands, read git, scan
stores, open a read-only broker connection and probe HTTP endpoints; they
produce :class:`~trading_system.readiness.evidence.EvidenceRecord` values and
nothing else. The evaluator then reads those records and can do none of it.

That split is brief section 26, and it buys two things worth the separation:

* ``evaluate(bundle, policy)`` stays a pure function, so a stored assessment
  can be recomputed and checked rather than merely trusted;
* the boundary test that forbids the evaluator from importing a broker, an LLM
  client, a Docker client or a socket has somewhere to put the code that
  legitimately needs them.

**No collector can place an order.** The broker collector goes through
:func:`~trading_system.broker.factory.build_broker`, which returns a read-only
connection whatever the settings say, and asserts the broker's own
submitted-order counter did not move — the same belt-and-braces the Milestone 9
position ledger uses. ``build_execution_broker``, the only writable
constructor, is not imported here and a boundary test asserts it.

Every collector is **failure-tolerant in one direction only**: a collector that
cannot complete records an evidence record saying so, and no predicate reads
``PASS`` from an uncollected record. A collector never raises into the caller,
because a readiness run that crashed on its fourth probe would report nothing
about the three that succeeded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_system.domain.enums import ReadinessEvidenceKind
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig
from trading_system.readiness.evidence import EvidenceRecord

__all__ = [
    "CommandResult",
    "collect_cardinality",
    "collect_configuration",
    "collect_daily_loss",
    "collect_execution_safety",
    "collect_format",
    "collect_git",
    "collect_lint",
    "collect_masking",
    "collect_operational_history",
    "collect_pnl",
    "collect_reconciliation",
    "collect_safety_suite",
    "collect_scheduler",
    "collect_secrets",
    "collect_test_isolation",
    "collect_test_suite",
    "collect_typecheck",
    "run_command",
]

_logger = get_logger(__name__)

#: Bound on every subprocess. A collector that hung would turn "readiness is
#: slow" into "readiness never finishes", which is the same class of failure
#: the Milestone 2 request timeout exists to prevent.
DEFAULT_COMMAND_TIMEOUT = 3600.0
GIT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one executed command actually did.

    Brief section 6A is explicit that the readiness report must preserve enough
    to reproduce the evidence rather than a ``tests_passed = true``. This is
    that: the command line, the exit code, the duration, and the parsed counts
    where the tool reports them.
    """

    command: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    env: dict[str, str] | None = None,
    full_output: bool = False,
) -> CommandResult:
    """Run one command and capture its result. Never raises.

    A command that could not be started at all records ``exit_code=None``,
    which no predicate reads as a pass — "the tool is not installed" and "the
    tool ran and found nothing wrong" must not look alike.

    ``full_output`` keeps stdout untruncated. Off by default, because a failing
    suite emits megabytes and an immutable record holding all of it is a store
    nobody can read — but on for output that is *counted* rather than read.
    ``git status --porcelain`` is the case that matters: truncating it to the
    last twenty lines made ``changed_files`` report 20 for a tree with 40
    changes, understating exactly the fact the criterion exists to surface.
    Found by a test that dirtied forty files.
    """
    started = datetime.now(UTC)
    environment = dict(os.environ)
    if env:
        environment.update(env)
    try:
        completed = subprocess.run(  # fixed argv, never a shell
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=tuple(command),
            exit_code=None,
            duration_seconds=(datetime.now(UTC) - started).total_seconds(),
            stdout_tail="",
            stderr_tail="",
            timed_out=True,
            error=f"timed out after {timeout}s",
        )
    except (OSError, ValueError) as exc:
        return CommandResult(
            command=tuple(command),
            exit_code=None,
            duration_seconds=(datetime.now(UTC) - started).total_seconds(),
            stdout_tail="",
            stderr_tail="",
            error=str(exc),
        )
    return CommandResult(
        command=tuple(command),
        exit_code=completed.returncode,
        duration_seconds=(datetime.now(UTC) - started).total_seconds(),
        stdout_tail=(completed.stdout if full_output else _tail(completed.stdout)),
        stderr_tail=_tail(completed.stderr),
    )


def _tail(text: str, *, lines: int = 20, limit: int = 4000) -> str:
    """The last few lines of output, bounded.

    Bounded because a failing suite emits megabytes and an immutable record
    holding all of it is a store nobody can read. The tail is where the summary
    line lives, which is the part a reader needs.
    """
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return tail if len(tail) <= limit else tail[-limit:]


def _command_detail(result: CommandResult, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "command": " ".join(result.command),
        "exit_code": result.exit_code,
        "duration_seconds": round(result.duration_seconds, 3),
        "timed_out": result.timed_out,
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
    }
    if result.error:
        detail["error"] = result.error
    if extra:
        detail.update(extra)
    return detail


# ---------------------------------------------------------------------------
# Source control
# ---------------------------------------------------------------------------
def collect_git(*, repo_root: Path, observed_at: datetime) -> EvidenceRecord:
    """The revision and whether the working tree matches it (brief section 29).

    ``git status --porcelain`` rather than ``git diff --quiet``: the porcelain
    output also reports untracked files, and a readiness result claiming to
    describe a revision while sitting on top of an untracked module is claiming
    something false.
    """
    revision = _git(["rev-parse", "HEAD"], repo_root)
    # Untruncated: this output is counted, not read.
    status = _git(["status", "--porcelain"], repo_root, full_output=True)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)

    if revision is None:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.SOURCE_CONTROL,
            source="git rev-parse HEAD",
            observed_at=observed_at,
            collected=False,
            error="git could not report a revision; is this a repository?",
            detail={"git_revision": None, "working_tree_clean": None},
        )

    changed = [line for line in (status or "").splitlines() if line.strip()]
    clean = not changed
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SOURCE_CONTROL,
        source="git rev-parse HEAD; git status --porcelain",
        observed_at=observed_at,
        detail={
            "git_revision": revision,
            "branch": branch,
            "working_tree_clean": clean,
            "changed_files": len(changed),
            # Bounded: a tree with a thousand changed files should not produce
            # a thousand-line audit record, and the count is the fact.
            "changed_sample": sorted(entry[3:] for entry in changed)[:20],
        },
        git_revision=revision,
    )


def _git(arguments: list[str], repo_root: Path, *, full_output: bool = False) -> str | None:
    result = run_command(
        ["git", *arguments], cwd=repo_root, timeout=GIT_TIMEOUT, full_output=full_output
    )
    if not result.passed:
        return None
    return result.stdout_tail.strip() or ("" if arguments[0] == "status" else None)


# ---------------------------------------------------------------------------
# Software quality (brief section 6A)
# ---------------------------------------------------------------------------
def collect_test_suite(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, python: str
) -> EvidenceRecord:
    """Run the offline suite and preserve its counts.

    Runs with the *default* marker selection, which is what an ordinary
    developer runs: gateway-backed and paid-API tests are skipped by
    ``tests/conftest.py`` unless explicitly unlocked. That is deliberate —
    section 6C wants the ordinary suite reported separately from the gated
    ones, and running the ordinary one here is how the ordinary one is proven
    hermetic.
    """
    result = run_command([python, "-m", "pytest", "-q", "--no-header"], cwd=repo_root)
    counts = _parse_pytest_summary(result.stdout_tail)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source="pytest -q",
        observed_at=observed_at,
        detail=_command_detail(result, counts),
        git_revision=git_revision,
    )


def collect_targeted_suite(
    paths: Sequence[str],
    *,
    label: str,
    repo_root: Path,
    observed_at: datetime,
    git_revision: str | None,
    python: str,
    extra: dict[str, Any] | None = None,
) -> EvidenceRecord:
    """Run one named subset of the suite and preserve its counts.

    Used for every criterion that asserts a *specific* safety property —
    execution gates, exit precedence, agent boundaries, point-in-time. Running
    the whole suite would satisfy them all at once and tell a reader nothing
    about which property actually held.
    """
    result = run_command([python, "-m", "pytest", "-q", "--no-header", *paths], cwd=repo_root)
    detail = _command_detail(result, _parse_pytest_summary(result.stdout_tail))
    detail["suite"] = label
    detail["paths"] = list(paths)
    if extra:
        detail.update(extra)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source=f"pytest {' '.join(paths)}",
        observed_at=observed_at,
        detail=detail,
        git_revision=git_revision,
    )


def _parse_pytest_summary(output: str) -> dict[str, Any]:
    """Pull the counts out of pytest's summary line.

    Best effort, and honest about it: a line that cannot be parsed yields no
    counts rather than zeros. ``failed=0`` inferred from an unparsed summary
    would be a fabricated reassurance, and the exit code is the load-bearing
    signal anyway.
    """
    counts: dict[str, Any] = {}
    for line in reversed(output.splitlines()):
        stripped = line.strip().strip("=").strip()
        if not stripped or (" in " not in stripped and "no tests ran" not in stripped):
            continue
        for keyword in ("passed", "failed", "skipped", "error", "errors", "xfailed", "xpassed"):
            token = f" {keyword}"
            if token in f" {stripped}":
                parts = stripped.replace(",", " ").split()
                for index, part in enumerate(parts):
                    if part.rstrip(",") == keyword and index > 0:
                        try:
                            key = "errors" if keyword == "error" else keyword
                            counts[key] = int(parts[index - 1])
                        except ValueError:
                            continue
        if counts:
            counts["summary"] = stripped
            break
    return counts


def collect_lint(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, ruff: str
) -> EvidenceRecord:
    result = run_command([ruff, "check", "src", "tests"], cwd=repo_root, timeout=600.0)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source="ruff check src tests",
        observed_at=observed_at,
        detail=_command_detail(result),
        git_revision=git_revision,
    )


def collect_format(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, ruff: str
) -> EvidenceRecord:
    result = run_command([ruff, "format", "--check", "src", "tests"], cwd=repo_root, timeout=600.0)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source="ruff format --check src tests",
        observed_at=observed_at,
        detail=_command_detail(result),
        git_revision=git_revision,
    )


def collect_typecheck(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, mypy: str
) -> EvidenceRecord:
    result = run_command([mypy], cwd=repo_root, timeout=1800.0)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.COMMAND,
        source="mypy",
        observed_at=observed_at,
        detail=_command_detail(result),
        git_revision=git_revision,
    )


def collect_execution_safety(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, python: str
) -> EvidenceRecord:
    """The order-submission gates, plus the zero-orders claim.

    ``orders_submitted`` is recorded as ``0`` only because the suite these
    tests live in asserts it — ``NeverCalledBrokerFactory`` fails the run if a
    gate is reached after a broker was requested. Recording the number here
    without that suite passing would be exactly the fabricated evidence section
    39 forbids, which is why the two facts share one record: if the suite did
    not pass, the count is not written.
    """
    record = collect_targeted_suite(
        ["tests/execution/test_execution_safety.py", "tests/execution/test_zero_orders.py"],
        label="execution safety",
        repo_root=repo_root,
        observed_at=observed_at,
        git_revision=git_revision,
        python=python,
    )
    if record.detail.get("exit_code") == 0:
        return EvidenceRecord.of(
            kind=record.kind,
            source=record.source,
            observed_at=record.observed_at,
            detail={**record.detail, "orders_submitted": 0},
            git_revision=record.git_revision,
        )
    return record


#: Which suite proves which safety claim.
#:
#: One table rather than eight near-identical wrappers, so a reader can see the
#: whole mapping at once — and so adding a claim means adding a row rather than
#: a function nobody will find.
SAFETY_SUITES: dict[str, tuple[str, tuple[str, ...]]] = {
    "position_lifecycle": (
        "position lifecycle",
        ("tests/positions", "tests/reservations"),
    ),
    "exit_management": ("exit management", ("tests/exit",)),
    "agents": ("agent contracts", ("tests/agents",)),
    "agent_boundaries": (
        "agent boundaries",
        (
            "tests/research/test_boundaries.py",
            "tests/strategy/test_boundaries.py",
            "tests/universe",
        ),
    ),
    "data": (
        "data and point-in-time",
        ("tests/data", "tests/contract_selection/test_point_in_time.py"),
    ),
    "privacy": ("telemetry privacy", ("tests/observability",)),
}


def collect_safety_suite(
    slot: str,
    *,
    repo_root: Path,
    observed_at: datetime,
    git_revision: str | None,
    python: str,
) -> EvidenceRecord:
    """Run the suite that proves one named safety claim."""
    label, paths = SAFETY_SUITES[slot]
    return collect_targeted_suite(
        list(paths),
        label=label,
        repo_root=repo_root,
        observed_at=observed_at,
        git_revision=git_revision,
        python=python,
    )


def collect_test_isolation(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None
) -> EvidenceRecord:
    """Whether an ordinary ``pytest`` can reach a gateway or a real credential.

    Read out of ``tests/conftest.py`` rather than by running the suite twice.
    The three facts that matter are structural: the safety-critical environment
    is clamped per variable, the gateway-backed markers are skipped without
    ``ALLOW_LIVE_TESTS``, and the order-submitting marker needs a *second*
    variable checked independently of the first.
    """
    conftest = repo_root / "tests" / "conftest.py"
    if not conftest.is_file():
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.STORE_SCAN,
            source=str(conftest),
            observed_at=observed_at,
            collected=False,
            error="tests/conftest.py not found",
        )
    source = conftest.read_text(encoding="utf-8")
    clamped = [
        name
        for name in (
            "TRADING_MODE",
            "ALLOW_LIVE_TESTS",
            "LIVE_TRADING_CONFIRMED",
            "LIVE_READINESS_CHECKLIST_SIGNED_OFF",
            "IBKR_READ_ONLY",
        )
        if f'"{name}"' in source
    ]
    hermetic = len(clamped) == 5 and "autouse=True" in source
    live_gated = "ALLOW_LIVE_TESTS" in source and "add_marker(skip" in source
    double_gated = "RUN_PAPER_EXECUTION_TESTS" in source and "paper_execution" in source

    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.STORE_SCAN,
        source="tests/conftest.py",
        observed_at=observed_at,
        detail={
            "suite_is_hermetic": hermetic,
            "live_suites_gated": live_gated,
            "paper_execution_double_gated": double_gated,
            "clamped_variables": sorted(clamped),
        },
        git_revision=git_revision,
    )


# ---------------------------------------------------------------------------
# Configuration (brief section 6B)
# ---------------------------------------------------------------------------
def collect_configuration(
    *,
    settings: Settings,
    config: SystemConfig | None,
    config_error: str | None,
    observed_at: datetime,
) -> EvidenceRecord:
    """The mode, the guards and the execution switch, as they actually are.

    Reads *loaded* settings rather than the files, so what is recorded is what
    the process would actually run under — a ``.env`` that overrides a YAML
    value is the value that matters, and inspecting the file would miss it.

    No secret is recorded. The account number is not read at all here; only
    whether one is configured.
    """
    detail: dict[str, Any] = {
        "config_loaded": config is not None,
        "trading_mode": settings.trading_mode.value,
        "live_trading_confirmed": settings.live_trading_confirmed,
        "live_readiness_checklist_signed_off": settings.live_readiness_checklist_signed_off,
        "allow_live_tests": settings.allow_live_tests,
        "ibkr_read_only": settings.ibkr_read_only,
        "broker_backend": settings.resolved_broker_backend.value,
        "account_configured": settings.ibkr_account_id is not None,
    }
    if config_error:
        detail["config_error"] = config_error
    if config is not None:
        detail.update(
            {
                "execution_enabled": config.execution.enabled,
                "execution_allow_live": config.execution.allow_live,
                "require_explicit_authorization": (config.execution.require_explicit_authorization),
                "config_version": config.application.config_version,
                "readiness_config_version": config.readiness.config_version,
                "observability_enabled": config.observability.enabled,
            }
        )
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.CONFIGURATION,
        source="Settings + config/",
        observed_at=observed_at,
        collected=config is not None,
        error=config_error,
        detail=detail,
    )


def collect_secrets(*, repo_root: Path, observed_at: datetime) -> EvidenceRecord:
    """Whether any secret-bearing file is tracked in git (brief section 20)."""
    candidates = (".env", ".env.local", ".env.production", "secrets")
    tracked: list[str] = []
    for candidate in candidates:
        result = run_command(
            ["git", "ls-files", "--error-unmatch", candidate], cwd=repo_root, timeout=GIT_TIMEOUT
        )
        if result.passed and result.stdout_tail.strip():
            tracked.append(candidate)
    ignored = run_command(["git", "check-ignore", ".env"], cwd=repo_root, timeout=GIT_TIMEOUT)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SOURCE_CONTROL,
        source="git ls-files / git check-ignore",
        observed_at=observed_at,
        detail={
            "tracked_secret_files": sorted(tracked),
            "dotenv_ignored": ignored.passed,
            "checked": list(candidates),
        },
    )


def collect_masking(
    *, data_root: Path, observed_at: datetime, account: str | None
) -> EvidenceRecord:
    """Whether any stored artifact carries a full account number.

    Scans the stores Milestone 9 onwards writes. With no account configured
    there is nothing to search for, and the record says so rather than
    reporting a clean scan — an unsearchable scan that reads "no leaks found"
    is the same false reassurance as an unexamined test.
    """
    if not account:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.STORE_SCAN,
            source=str(data_root),
            observed_at=observed_at,
            collected=False,
            error=(
                "no account number is configured, so stored artifacts cannot be searched "
                "for one. This is reported rather than recorded as a clean scan"
            ),
        )
    offenders: list[str] = []
    scanned = 0
    for directory in ("positions", "reconciliation", "pnl", "readiness", "operations", "fills"):
        root = data_root / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json*")):
            if not path.is_file():
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - defensive
                continue
            if account in text:
                offenders.append(str(path.relative_to(data_root)))
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.STORE_SCAN,
        source=f"{data_root} (positions, reconciliation, pnl, readiness, operations, fills)",
        observed_at=observed_at,
        detail={
            "account_identifiers_masked": not offenders,
            "files_scanned": scanned,
            # The paths, never the account number itself.
            "offending_files": sorted(offenders)[:20],
        },
    )


def collect_cardinality(
    *, config: SystemConfig | None, exposition: str | None, observed_at: datetime
) -> EvidenceRecord:
    """Whether a domain identifier reached a metric label (brief section 14).

    Checked against *live exposition* when the collector's Prometheus endpoint
    was reachable, and against the configured guard otherwise. Both spellings
    are searched — ``execution_id`` and ``trading.execution.id`` — because a
    guard that catches one and not the other catches nothing.
    """
    if config is None:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.CONFIGURATION,
            source="config/observability.yaml",
            observed_at=observed_at,
            collected=False,
            error="configuration did not load, so the cardinality guard could not be read",
        )
    forbidden = list(config.observability.metrics.forbidden_labels)
    found: list[str] = []
    if exposition:
        for label in forbidden:
            dotted = label.replace("_", ".")
            for spelling in {f"{label}=", f'{label}="', f"{dotted}=", f'{dotted}="'}:
                if spelling in exposition:
                    found.append(label)
                    break
    return EvidenceRecord.of(
        kind=(
            ReadinessEvidenceKind.SERVICE_PROBE
            if exposition
            else ReadinessEvidenceKind.CONFIGURATION
        ),
        source=("collector metric exposition" if exposition else "config/observability.yaml"),
        observed_at=observed_at,
        detail={
            "forbidden_labels_found": sorted(set(found)),
            "guarded_labels": len(forbidden),
            "exposition_checked": exposition is not None,
            "exposition_bytes": len(exposition) if exposition else 0,
        },
    )


# ---------------------------------------------------------------------------
# Stored operational state
# ---------------------------------------------------------------------------
def collect_scheduler(*, data_root: Path, observed_at: datetime) -> EvidenceRecord:
    """Scheduler ticks and job outcomes, read from the operations store.

    ``SKIPPED``, ``FAILED`` and ``UNKNOWN`` are counted separately and stay
    separate all the way to the criterion. A job that deliberately did not run
    is not an error, and a job whose completion was never recorded is a
    question rather than a failure.
    """
    from trading_system.operations.store import FilesystemOperationsRepository

    repository = FilesystemOperationsRepository(data_root / "operations")
    try:
        ticks = repository.scheduler_runs(limit=50)
        history = repository.job_history(limit=500)
    except Exception as exc:  # pragma: no cover - defensive
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.STORE_SCAN,
            source=str(data_root / "operations"),
            observed_at=observed_at,
            collected=False,
            error=f"the operations store could not be read: {exc}",
        )

    latest = ticks[0] if ticks else None
    statuses = [entry.status for entry in history]
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.STORE_SCAN,
        source=str(data_root / "operations"),
        observed_at=observed_at,
        detail={
            "scheduler_ran": bool(ticks),
            "scheduler_ticks": len(ticks),
            "last_tick_at": (
                latest.started_at.astimezone(UTC).isoformat()
                if latest is not None and getattr(latest, "started_at", None)
                else None
            ),
            "job_runs": len(history),
            "failed_jobs": sum(1 for status in statuses if status == "FAILED"),
            "unknown_jobs": sum(1 for status in statuses if status == "UNKNOWN"),
            "skipped_jobs": sum(1 for status in statuses if status == "SKIPPED"),
            "successful_jobs": sum(1 for status in statuses if status == "SUCCESS"),
        },
        artifact_ids=tuple(
            getattr(tick, "scheduler_run_id", "") for tick in ticks[:5] if tick is not None
        ),
    )


def collect_daily_loss(*, data_root: Path, observed_at: datetime) -> EvidenceRecord:
    """Today's realised figure, and which of its three states it is in.

    ``TRACKED`` / ``UNKNOWN`` / ``NOT_TRACKED`` are read straight off the
    Milestone 11 record and never collapsed. A comfortable number next to "we
    could not measure today" is exactly how an unmeasured day passes a loss
    limit.
    """
    from trading_system.pnl.store import FilesystemPnLRepository

    repository = FilesystemPnLRepository(data_root / "pnl")
    try:
        history = repository.daily_history(limit=10)
    except Exception as exc:  # pragma: no cover - defensive
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.STORE_SCAN,
            source=str(data_root / "pnl"),
            observed_at=observed_at,
            collected=False,
            error=f"the profit-and-loss store could not be read: {exc}",
        )
    if not history:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.STORE_SCAN,
            source=str(data_root / "pnl"),
            observed_at=observed_at,
            detail={
                "daily_pnl_status": "NOT_TRACKED",
                "note": (
                    "no daily roll-up has ever been recorded. For a deployment that has "
                    "never closed a position this is the ordinary state"
                ),
            },
        )
    latest = history[0]
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.ARTIFACT,
        source=str(data_root / "pnl"),
        observed_at=observed_at,
        detail={
            "daily_pnl_status": latest.status.value,
            "session_date": latest.session_date.isoformat(),
            "positions_closed": latest.positions_closed,
            # Deliberately not the figure. Telemetry and readiness artifacts
            # carry references and statuses; the money stays in the ledger.
            "has_figure": latest.realized_pnl is not None,
        },
        artifact_ids=(latest.daily_pnl_id,),
    )


def collect_operational_history(
    *, data_root: Path, observed_at: datetime, minimums: dict[str, int]
) -> EvidenceRecord:
    """How much operating this system has actually seen (brief section 21).

    Counts readiness runs, the distinct days they span, reconciliation runs and
    scheduler ticks, and lists every shortfall by name. "Insufficient history"
    is not actionable; "2 readiness runs, 3 required" is.
    """
    from trading_system.readiness.store import FilesystemReadinessRepository

    readiness_runs = 0
    distinct_days: set[str] = set()
    span_days = 0
    try:
        entries = FilesystemReadinessRepository(data_root / "readiness").history()
        unique = {entry.readiness_run_id: entry for entry in entries}.values()
        readiness_runs = len(unique)
        dates = sorted(entry.evaluated_at.astimezone(UTC).date() for entry in unique)
        distinct_days = {value.isoformat() for value in dates}
        if dates:
            span_days = (dates[-1] - dates[0]).days
    except Exception:  # pragma: no cover - an unreadable store is zero history
        pass

    reconciliation_runs = _count_lines(data_root / "reconciliation" / "history.jsonl")
    scheduler_ticks = _count_files(data_root / "operations" / "ticks")

    observed = {
        "min_readiness_runs": readiness_runs,
        "min_distinct_days": len(distinct_days),
        "min_reconciliation_runs": reconciliation_runs,
        "min_scheduler_ticks": scheduler_ticks,
        "min_history_days": span_days,
    }
    shortfalls = [
        f"{key.removeprefix('min_')}: {observed[key]} recorded, {required} required"
        for key, required in sorted(minimums.items())
        if observed.get(key, 0) < required
    ]
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.STORE_SCAN,
        source=str(data_root),
        observed_at=observed_at,
        detail={
            "shortfalls": shortfalls,
            "readiness_runs": readiness_runs,
            "distinct_days": len(distinct_days),
            "reconciliation_runs": reconciliation_runs,
            "scheduler_ticks": scheduler_ticks,
            "history_span_days": span_days,
            "required": dict(sorted(minimums.items())),
        },
    )


def collect_pnl(
    *, repo_root: Path, observed_at: datetime, git_revision: str | None, python: str
) -> EvidenceRecord:
    """The profit-and-loss suite, plus the settlement idempotency claim."""
    record = collect_targeted_suite(
        ["tests/pnl", "tests/integration/test_operations_lifecycle.py"],
        label="profit and loss",
        repo_root=repo_root,
        observed_at=observed_at,
        git_revision=git_revision,
        python=python,
    )
    if record.detail.get("exit_code") == 0:
        return EvidenceRecord.of(
            kind=record.kind,
            source=record.source,
            observed_at=record.observed_at,
            detail={**record.detail, "settlement_idempotent": True},
            git_revision=record.git_revision,
        )
    return record


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _count_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.rglob("*.json") if path.is_file())


# ---------------------------------------------------------------------------
# Broker and reconciliation (brief sections 6D, 6E, 6F)
# ---------------------------------------------------------------------------
def collect_broker(
    *,
    settings: Settings,
    config: SystemConfig,
    project_root: Path,
    observed_at: datetime,
    simulated: bool = False,
) -> EvidenceRecord:
    """One short-lived READ-ONLY connection, four handshake-cached reads.

    Reuses the Milestone 9 position service rather than opening a second IBKR
    client (brief section 6D). That matters for more than tidiness: account
    summary, positions, open orders and fills are all served from
    ``ib_async``'s ``StartupFetchALL`` cache, so one connection answers all four
    without a second uncached round trip — the Milestone 2 constraint this
    system is built around.

    The account number is **masked** before it reaches the record.
    """
    from trading_system.positions.service import PositionService

    try:
        service = PositionService(
            settings=settings,
            config=config,
            root=project_root,
            broker_factory=_simulator_factory() if simulated else None,
        )
        state = service.read_broker_state(want_orders=True, want_executions=True)
    except Exception as exc:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.BROKER_READ,
            source="positions service (read-only)",
            observed_at=observed_at,
            collected=False,
            error=str(exc),
            detail={"connected": False, "trading_mode": settings.trading_mode.value},
        )

    if state.orders_submitted:  # pragma: no cover - the read path cannot submit
        raise RuntimeError(
            f"the readiness broker read submitted {state.orders_submitted} order(s). This "
            f"path is read-only by construction and must never mutate broker state."
        )

    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.BROKER_READ,
        source=f"{state.broker} (read-only)",
        observed_at=observed_at,
        collected=state.connected,
        error=state.detail if not state.connected else None,
        detail={
            "connected": state.connected,
            "broker": state.broker,
            "read_only": state.read_only,
            "trading_mode": settings.trading_mode.value,
            "orders_submitted": state.orders_submitted,
            "account_status": state.account_status.value,
            "positions_status": state.positions_status.value,
            "orders_status": state.orders_status.value,
            "executions_status": state.executions_status.value,
            "account": _mask(getattr(state.account, "account_id", None)),
            "position_count": len(state.positions),
            "open_order_count": len(state.orders),
            "execution_count": len(state.executions),
            "detail": state.detail,
        },
    )


def collect_reconciliation(
    *,
    settings: Settings,
    config: SystemConfig,
    project_root: Path,
    observed_at: datetime,
    simulated: bool = False,
    run: bool = True,
) -> EvidenceRecord:
    """Reconciliation, run fresh or read from the store.

    An inability to observe the broker produces ``BROKER_DATA_UNAVAILABLE``,
    never a ``MATCH``. Orphan broker positions are counted and reported and
    nothing adopts them — ``reconciliation`` reports rather than repairs, and
    readiness inherits that rather than restating it.
    """
    from trading_system.reconciliation.service import ReconciliationService

    result: Any = None
    try:
        service = ReconciliationService(
            settings=settings,
            config=config,
            root=project_root,
            broker_factory=_simulator_factory() if simulated else None,
        )
        result = service.run(as_of=observed_at).result if run else service.latest()
    except Exception as exc:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.ARTIFACT,
            source="reconciliation service",
            observed_at=observed_at,
            collected=False,
            error=str(exc),
        )

    if result is None:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.ARTIFACT,
            source="reconciliation store",
            observed_at=observed_at,
            collected=False,
            error="no reconciliation result is stored and none was run",
        )

    findings = tuple(getattr(result, "findings", ()) or ())
    critical = sum(
        1
        for finding in findings
        if getattr(getattr(finding, "severity", None), "value", "") in {"CRITICAL"}
    )
    orphans = sum(
        1
        for finding in findings
        if getattr(getattr(finding, "finding_type", None), "value", "") == "ORPHAN_BROKER_POSITION"
    )
    unknown_executions = sum(
        1
        for finding in findings
        if "UNKNOWN" in getattr(getattr(finding, "finding_type", None), "value", "")
    )
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.ARTIFACT,
        source="reconciliation service",
        observed_at=getattr(result, "as_of", observed_at) or observed_at,
        detail={
            "status": getattr(getattr(result, "status", None), "value", None),
            "findings": len(findings),
            "critical_findings": critical,
            "orphan_positions": orphans,
            "unknown_executions": unknown_executions,
            "orders_submitted": getattr(result, "orders_submitted", 0),
            "corrective_orders": getattr(result, "corrective_orders", 0),
        },
        artifact_ids=(str(getattr(result, "reconciliation_id", "")),),
    )


def _simulator_factory() -> Any:
    """A broker factory pinned to the simulator, for offline collection.

    Still goes through :func:`~trading_system.broker.factory.build_broker`,
    which is read-only whatever the settings say. "Simulated" changes which
    broker answers, never whether it could place an order.
    """
    from trading_system.broker.factory import build_broker
    from trading_system.infrastructure.settings import BrokerBackend

    def factory(resolved: Settings, **kwargs: Any) -> Any:
        return build_broker(resolved, backend=BrokerBackend.SIMULATOR, **kwargs)

    return factory


def _mask(account: Any) -> str | None:
    """Mask an account identifier, keeping only enough to recognise it.

    Every Milestone 9 artifact stores a masked reference and a test asserts the
    full number never reaches a stored payload. A readiness artifact is stored
    in the same tree and read by the same people, so it obeys the same rule.
    """
    if account is None:
        return None
    text = str(account).strip()
    if not text:
        return None
    return text[:2] + "*" * max(0, len(text) - 4) + text[-2:] if len(text) > 4 else "*" * len(text)


def resolve_tool(repo_root: Path, name: str) -> str:
    """Locate a development tool, preferring the project's own virtualenv.

    A readiness run that invoked a *different* ruff or mypy from the one the
    developer runs would report a result about a toolchain nobody uses.
    """
    candidate = repo_root / ".venv" / "bin" / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which(name)
    return found or name


def load_json(path: Path) -> Any | None:
    """Read a JSON file, or ``None`` if it cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def within(observed_at: datetime, window: timedelta) -> bool:
    """Whether an instant falls inside a window ending now. For diagnostics."""
    return datetime.now(UTC) - observed_at <= window
