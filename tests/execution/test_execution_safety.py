"""The execution safety gates, asserted as a hierarchy.

Regression suite for the Milestone 11 acceptance blocker. The Milestone 11
commit shipped ``config/execution.yaml`` with ``enabled: true`` and a developer
``.env`` carrying ``IBKR_READ_ONLY=false``; together those two lines meant an
ordinary ``pytest`` run reached ``build_execution_broker``, obtained a
*writable* IBKR broker and attempted a connection to 127.0.0.1:4002. The
gateway happened to be closed, so what came back was ``BROKER_UNAVAILABLE``
rather than an order — the failure was reported as a wrong status, and the
thing that actually went wrong was that the question reached the broker at all.

So the tests here assert the gates in order, and the strongest of them assert
what was *not* called rather than what came back:

    LIVE_TRADING_CONFIRMED + LIVE_READINESS_CHECKLIST_SIGNED_OFF   (G)
        -> trading_mode                                            (E, G)
            -> execution.enabled                                   (A, B, C)
                -> explicit authorisation                          (A)
                    -> IBKR_READ_ONLY                              (D, E)
                        -> broker

A failure at any level must prevent every level below it from being evaluated.
"Try the broker and see" is the shape this suite exists to forbid, because the
broker is where the irreversible thing happens.

Nothing here constructs a writable broker. ``build_execution_broker`` is called
only inside ``pytest.raises``, which is the exemption
``test_zero_orders.py::test_no_ordinary_test_actually_builds_a_writable_broker``
grants for asserting that a guard refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trading_system.broker.base import BrokerConfigurationError
from trading_system.domain.enums import (
    ExecutionReasonCode,
    ExecutionRunStatus,
    TradingMode,
)
from trading_system.execution.service import ExecutionService
from trading_system.infrastructure.settings import BrokerBackend, Settings

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A factory that proves it was never asked
# ---------------------------------------------------------------------------
class NeverCalledBrokerFactory:
    """A broker factory that records every request and answers none.

    The distinction this class exists to draw: a test asserting that a run
    reported ``BROKER_UNAVAILABLE`` proves the broker was *unsuccessful*, which
    is a much weaker claim than the one the safety contract makes. A disabled
    execution must not reach the factory at all, and the only way to show that
    is to count the calls.

    It raises rather than returning ``None`` so a call is loud at the point it
    happens as well as visible in ``calls`` afterwards. ``AssertionError`` is
    deliberately not a ``BrokerError``: the execution service catches
    ``BrokerError`` and converts it into a status, which would turn a breach of
    this guarantee into a tidy line of output.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        raise AssertionError(
            "a broker was requested while execution is disabled. The gate must "
            "terminate before broker construction, not after a failed connection"
        )

    @property
    def never_called(self) -> bool:
        return self.calls == []


@pytest.fixture
def never_called(monkeypatch: pytest.MonkeyPatch) -> NeverCalledBrokerFactory:
    """Replace every broker constructor in the system with a recorder.

    Both are patched, not only the writable one: a path that reached for a
    read-only broker on the way to submitting would still be a path that
    reached the network while execution was switched off.
    """
    from trading_system.broker import factory

    sentinel = NeverCalledBrokerFactory()
    monkeypatch.setattr(factory, "build_execution_broker", sentinel)
    monkeypatch.setattr(factory, "build_broker", sentinel)
    return sentinel


# ---------------------------------------------------------------------------
# A. execution disabled + --confirm -> EXECUTION_DISABLED, zero orders
# ---------------------------------------------------------------------------
def test_confirm_while_disabled_is_refused_and_submits_nothing(
    settings_paper, execution_disabled_config, clock, tmp_path, stub_repositories, fake_broker
) -> None:
    """``--confirm`` is one switch of two, and it is not the master one."""
    broker = fake_broker()
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        broker_factory=lambda *args, **kwargs: broker,
        **stub_repositories,
    )

    run = service.run(allocation_ids=None, authorized=True)

    assert run.result.status is ExecutionRunStatus.EXECUTION_DISABLED
    assert run.result.orders_submitted == 0
    assert run.result.executions == []
    assert broker.orders_submitted == 0


def test_the_cli_refuses_confirm_while_disabled(
    monkeypatch, tmp_path: Path, execution_disabled_config, stub_repositories, fake_broker
) -> None:
    """The same refusal at the command line, where an operator meets it."""
    from trading_system import cli

    broker = fake_broker()

    def _service() -> ExecutionService:
        return ExecutionService(
            settings=Settings(_env_file=None, trading_mode="PAPER"),
            config=execution_disabled_config,
            clock=_fixed_clock(),
            root=tmp_path,
            broker_factory=lambda *args, **kwargs: broker,
            **stub_repositories,
        )

    monkeypatch.setattr(cli, "_execution_service", _service)
    result = CliRunner().invoke(cli.app, ["execution", "run", "--confirm"])

    # A refusal exits non-zero: an operator who confirmed an order and got
    # none has to be able to tell that from a run that sent one.
    assert result.exit_code != 0
    assert "EXECUTION_DISABLED" in result.output
    assert "execution.enabled is false" in result.output
    assert "Orders submitted (read off the broker): 0" in result.output
    assert broker.orders_submitted == 0


def _fixed_clock():
    from trading_system.infrastructure.clock import FixedClock

    from .conftest import NOW

    return FixedClock(NOW)


# ---------------------------------------------------------------------------
# B. execution disabled + an authorised allocation -> no broker is requested
# ---------------------------------------------------------------------------
def test_a_disabled_execution_never_asks_for_a_broker(
    settings_paper,
    execution_disabled_config,
    clock,
    tmp_path,
    stub_repositories,
    approved_allocation,
    never_called: NeverCalledBrokerFactory,
) -> None:
    """The gate is evaluated before construction, not after a failed connect.

    This is the test the acceptance blocker was missing. Every other assertion
    in the suite would still have passed with the gate placed *after* the
    broker call — the status would have been wrong, but only after a real
    connection attempt to whatever ``IBKR_HOST`` names.
    """
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    run = service.run(allocation_ids=[approved_allocation.allocation_id], authorized=True)

    assert run.result.status is ExecutionRunStatus.EXECUTION_DISABLED
    assert run.result.orders_submitted == 0
    assert never_called.never_called, f"a broker was requested: {never_called.calls}"


def test_the_refusal_names_the_switch_that_refused(
    settings_paper,
    execution_disabled_config,
    clock,
    tmp_path,
    stub_repositories,
    approved_allocation,
) -> None:
    """An operator has to know which of the two switches to look at."""
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    run = service.run(allocation_ids=[approved_allocation.allocation_id], authorized=True)

    assert "execution.enabled" in (run.result.status_detail or "")
    assert "config/execution.yaml" in (run.result.status_detail or "")


def test_a_disabled_execution_stores_no_attempt(
    settings_paper,
    execution_disabled_config,
    clock,
    tmp_path,
    stub_repositories,
    approved_allocation,
    never_called: NeverCalledBrokerFactory,
) -> None:
    """Nothing was sent, so there is no attempt for a later run to resolve."""
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    service.run(allocation_ids=[approved_allocation.allocation_id], authorized=True)

    assert service.repository.history() == []
    assert never_called.never_called


# ---------------------------------------------------------------------------
# C. execution disabled + an authorised EXIT -> no broker is requested
# ---------------------------------------------------------------------------
def test_a_disabled_execution_refuses_an_authorised_exit(
    settings_paper,
    execution_disabled_config,
    clock,
    tmp_path,
    stub_repositories,
    exit_request_and_entry,
    never_called: NeverCalledBrokerFactory,
) -> None:
    """Milestone 10 reuses this boundary; it does not carry a second one.

    An exit reaches a broker through exactly one function —
    ``ExecutionService.submit_exit`` — so the master switch governs closing a
    position for the same reason and in the same place it governs opening one.
    """
    request, entry = exit_request_and_entry
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    submission = service.submit_exit(request, entry=entry, authorized=True)

    assert ExecutionReasonCode.EXECUTION_DISABLED in submission.reason_codes
    assert submission.orders_submitted == 0
    assert submission.record is None
    assert never_called.never_called, f"a broker was requested: {never_called.calls}"


def test_an_exit_is_refused_by_the_same_switch_as_an_entry(
    settings_paper,
    execution_disabled_config,
    clock,
    tmp_path,
    stub_repositories,
    exit_request_and_entry,
) -> None:
    """One policy, quoted by both paths. A second one could drift from the first."""
    request, entry = exit_request_and_entry
    service = ExecutionService(
        settings=settings_paper,
        config=execution_disabled_config,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    submission = service.submit_exit(request, entry=entry, authorized=True)

    assert "execution.enabled" in (submission.detail or "")
    assert "config/execution.yaml" in (submission.detail or "")


def test_the_exit_path_reads_the_switch_from_the_execution_service(repo_root: Path) -> None:
    """``exit/service.py`` must not define a policy of its own.

    Asserted against the source because the property is an absence: the exit
    service has no ``execution.enabled`` check to get wrong, which is what
    makes "the exit path respects the execution gate" true by construction
    rather than by two implementations agreeing today.
    """
    source = (repo_root / "src" / "trading_system" / "exit" / "service.py").read_text(
        encoding="utf-8"
    )

    assert "submit_exit(" in source, "the exit path must reach the broker through Milestone 8"
    assert "ExecutionReasonCode.EXECUTION_DISABLED" not in source
    assert "build_execution_broker" not in source


# ---------------------------------------------------------------------------
# D. execution enabled + IBKR_READ_ONLY=true -> still cannot submit
# ---------------------------------------------------------------------------
def test_the_read_only_setting_refuses_even_with_execution_enabled() -> None:
    """Two independent mechanisms, and the second one still binds.

    ``execution.enabled`` is a policy about this system; ``IBKR_READ_ONLY`` is
    a property of the connection to the account. Collapsing them would mean one
    edit opened both.
    """
    settings = Settings(_env_file=None, trading_mode="PAPER", ibkr_read_only=True)

    with pytest.raises(BrokerConfigurationError, match="IBKR_READ_ONLY"):
        from trading_system.broker.factory import build_execution_broker

        build_execution_broker(settings, backend=BrokerBackend.IBKR)


def test_an_enabled_run_against_a_read_only_setting_submits_nothing(
    system_config, clock, tmp_path, stub_repositories, approved_allocation
) -> None:
    """The refusal becomes a status, and the status carries no order.

    Execution is switched on here, so the master switch is out of the way and
    what refuses is the broker setting alone.
    """
    enabled = system_config.model_copy(
        update={"execution": system_config.execution.model_copy(update={"enabled": True})}
    )
    service = ExecutionService(
        settings=Settings(
            _env_file=None,
            trading_mode="PAPER",
            ibkr_read_only=True,
            broker_backend="IBKR",
        ),
        config=enabled,
        clock=clock,
        root=tmp_path,
        **stub_repositories,
    )

    run = service.run(allocation_ids=[approved_allocation.allocation_id], authorized=True)

    assert run.result.status is ExecutionRunStatus.BROKER_UNAVAILABLE
    assert run.result.orders_submitted == 0
    assert "IBKR_READ_ONLY" in (run.result.status_detail or "")


# ---------------------------------------------------------------------------
# E. the one combination that can submit, established by exhausting the others
# ---------------------------------------------------------------------------
#: Every mode/flag combination and whether a writable IBKR connection is
#: obtainable from it. Only one row is ``True``, and this suite deliberately
#: does not exercise that row: constructing it is what the ``paper_execution``
#: tests are for, behind ``ALLOW_LIVE_TESTS`` *and*
#: ``RUN_PAPER_EXECUTION_TESTS``.
_WRITABLE_IBKR_COMBINATIONS = [
    (TradingMode.DRY_RUN, True, False),
    (TradingMode.DRY_RUN, False, False),
    (TradingMode.PAPER, True, False),
    (TradingMode.PAPER, False, True),
]


@pytest.mark.parametrize(("mode", "read_only", "writable"), _WRITABLE_IBKR_COMBINATIONS)
def test_only_paper_with_read_only_disabled_can_reach_a_writable_ibkr_broker(
    mode: TradingMode, read_only: bool, writable: bool
) -> None:
    """Proven by exhaustion, because the affirmative case must not be run here.

    Each refusing combination is asserted to raise. The one permitting
    combination is asserted only to be the one this suite leaves alone — the
    explicit Paper execution path owns it, and a unit test that constructed it
    would be a unit test one connection away from a real account.
    """
    from trading_system.broker.factory import build_execution_broker

    settings = Settings(_env_file=None, trading_mode=mode.value, ibkr_read_only=read_only)

    if writable:
        assert (mode, read_only) == (TradingMode.PAPER, False)
        return

    with pytest.raises(BrokerConfigurationError):
        build_execution_broker(settings, backend=BrokerBackend.IBKR)


def test_live_can_never_reach_a_writable_broker() -> None:
    """Refused in the factory as well as in the settings and in the adapter."""
    from trading_system.broker.factory import build_execution_broker

    live = Settings(
        _env_file=None,
        trading_mode="LIVE",
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
        ibkr_read_only=False,
    )

    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        build_execution_broker(live, backend=BrokerBackend.IBKR)


def test_the_read_only_constructor_is_read_only_whatever_the_settings_say() -> None:
    """``build_broker`` is the one every upstream stage holds."""
    from trading_system.broker.factory import build_broker

    broker = build_broker(
        Settings(_env_file=None, trading_mode="DRY_RUN", ibkr_read_only=False),
        backend=BrokerBackend.SIMULATOR,
    )

    assert broker.read_only is True
    assert broker.orders_submitted == 0


# ---------------------------------------------------------------------------
# F. an ordinary pytest run is deterministic whatever the developer's .env says
# ---------------------------------------------------------------------------
def test_the_suite_runs_against_safe_settings_whatever_the_local_env_holds() -> None:
    """A bare ``Settings()`` — which does read ``.env`` — is still safe here.

    The clamp lives in ``tests/conftest.py`` and works by setting environment
    variables, which outrank ``.env`` in pydantic-settings. The application
    keeps its ``.env`` support; only the test process is pinned.
    """
    settings = Settings()

    assert settings.trading_mode is TradingMode.PAPER
    assert settings.ibkr_read_only is True
    assert settings.live_trading_confirmed is False
    assert settings.live_readiness_checklist_signed_off is False
    assert settings.allow_live_tests is False


def test_an_env_file_cannot_override_the_pinned_settings(tmp_path: Path) -> None:
    """The precedence, demonstrated against a deliberately hostile file.

    Written rather than assumed: the whole isolation mechanism rests on
    environment variables outranking ``.env``, and a pydantic-settings upgrade
    that reordered the two would silently unpin every gate above.
    """
    hostile = tmp_path / ".env"
    hostile.write_text(
        "TRADING_MODE=DRY_RUN\n"
        "IBKR_READ_ONLY=false\n"
        "LIVE_TRADING_CONFIRMED=true\n"
        "LIVE_READINESS_CHECKLIST_SIGNED_OFF=true\n"
        "ALLOW_LIVE_TESTS=true\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=hostile)

    assert settings.trading_mode is TradingMode.PAPER
    assert settings.ibkr_read_only is True
    assert settings.live_trading_confirmed is False
    assert settings.live_readiness_checklist_signed_off is False
    assert settings.allow_live_tests is False


def test_every_safety_critical_variable_is_pinned(repo_root: Path) -> None:
    """The clamp list is the contract; read it off the conftest that applies it."""
    from tests.conftest import SAFETY_CRITICAL_ENVIRONMENT

    assert SAFETY_CRITICAL_ENVIRONMENT == {
        "TRADING_MODE": "PAPER",
        "ALLOW_LIVE_TESTS": "false",
        "LIVE_TRADING_CONFIRMED": "false",
        "LIVE_READINESS_CHECKLIST_SIGNED_OFF": "false",
        "IBKR_READ_ONLY": "true",
    }


def test_the_clamp_is_lifted_only_for_the_tests_that_may_submit(repo_root: Path) -> None:
    """Read off the conftest that applies it, exactly as the marker gate is.

    The exemption has to exist — the ``paper_execution`` tests need
    ``IBKR_READ_ONLY=false`` and are the only tests permitted to submit — and it
    has to be narrow. A clamp lifted by anything other than that marker would
    let an ordinary test obtain a writable connection again.
    """
    source = (repo_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    exemption = source[source.index("def _force_safe_mode") : source.index("def pytest_collection")]

    assert "paper_execution" in exemption
    assert "IBKR_READ_ONLY" in exemption
    # Exactly one setting is ever lifted, and only for that marker.
    assert exemption.count("continue") == 1


def test_a_local_env_file_that_disagrees_is_reported_rather_than_obeyed(
    repo_root: Path,
) -> None:
    """If the checkout has a ``.env``, the clamp is what the suite actually ran under.

    Skipped where there is no ``.env`` — a container or CI — because there the
    property is vacuously true and asserting it would prove nothing.
    """
    env_file = repo_root / ".env"
    if not env_file.exists():
        pytest.skip("no local .env in this checkout")

    from tests.conftest import SAFETY_CRITICAL_ENVIRONMENT

    contradictions: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name, value = name.strip(), value.strip()
        pinned = SAFETY_CRITICAL_ENVIRONMENT.get(name)
        if pinned is not None and value.lower() != pinned.lower():
            contradictions[name] = value

    settings = Settings()
    for name, value in contradictions.items():
        pinned = SAFETY_CRITICAL_ENVIRONMENT[name]
        actual = getattr(settings, name.lower())
        if isinstance(actual, bool):
            assert actual is (pinned.lower() == "true"), (
                f"{name}={value} leaked from .env into the test process; the suite "
                f"must run with {name}={pinned}"
            )
        else:
            assert str(getattr(actual, "value", actual)) == pinned, (
                f"{name}={value} leaked from .env into the test process"
            )


# ---------------------------------------------------------------------------
# G. LIVE still needs both guards, and neither alone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("confirmed", "signed_off"),
    [(False, False), (True, False), (False, True)],
)
def test_live_is_refused_unless_both_guards_are_set(confirmed: bool, signed_off: bool) -> None:
    """Neither guard implies the other, exactly as the two execution switches."""
    with pytest.raises(ValueError, match="TRADING_MODE=LIVE refused"):
        Settings(
            _env_file=None,
            trading_mode="LIVE",
            live_trading_confirmed=confirmed,
            live_readiness_checklist_signed_off=signed_off,
        )


def test_both_guards_together_construct_but_still_cannot_execute() -> None:
    """The guards permit the *mode*; they do not permit an order.

    Milestone 12 is where a signed-off checklist means something. Until then a
    fully-guarded LIVE configuration reaches a factory that refuses it, which
    is the hierarchy holding at the level below the one that was satisfied.
    """
    from trading_system.broker.factory import build_broker, build_execution_broker

    settings = Settings(
        _env_file=None,
        trading_mode="LIVE",
        live_trading_confirmed=True,
        live_readiness_checklist_signed_off=True,
    )

    assert settings.trading_mode is TradingMode.LIVE
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        build_execution_broker(settings)
    with pytest.raises(BrokerConfigurationError, match="LIVE"):
        build_broker(settings)


def test_live_is_refused_in_the_execution_configuration_as_well(system_config) -> None:
    """Three independent refusals for the one irreversible action."""
    assert system_config.execution.allow_live is False
    assert system_config.execution.paper_only is True
