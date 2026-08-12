"""Deterministic fixtures for the execution suite.

Two things matter more here than convenience:

* **Nothing reaches a real broker.** Every fixture broker is either the
  in-process simulator or an explicit fake. A test that reached a gateway would
  be a bug in the test, and in this suite it would be a bug that placed an
  order.
* **The authorisation is the real thing.** ``approved_allocation`` reuses the
  shared ``campaign_allocation`` fixture, which is produced by running the
  actual Milestone 7 allocation engine. A hand-built stand-in would eventually
  disagree with what an authorisation really looks like — in exactly the
  direction that makes an execution test pass for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_system.broker.base import Broker, BrokerError
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.domain.enums import (
    LegAction,
    OptionRight,
    OrderStatus,
    OrderType,
    StrategyType,
    TimeInForce,
    TradingMode,
)
from trading_system.domain.models import (
    ExecutionResult,
    Fill,
    OptionLeg,
    OrderIntent,
    SystemVersions,
)
from trading_system.execution.models import (
    ExecutionRecord,
    ExecutionRequest,
    execution_identifier,
    execution_request_identifier,
)
from trading_system.execution.store import FilesystemExecutionRepository
from trading_system.infrastructure.clock import FixedClock

#: The suite's one instant. The market calendar says 2026-08-10 14:30 UTC is
#: inside the regular NYSE session, which several validation tests rely on.
NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = datetime(2026, 8, 31, tzinfo=UTC).date()


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def approved_allocation(campaign_allocation):
    """A real, APPROVED Milestone 7 authorisation with full provenance.

    ``research_run_id`` is filled in because the execution service looks the
    research report up through its run — the shared fixture leaves it unset,
    and an execution test wants to exercise the found path, not the missing one.
    """
    return campaign_allocation.model_copy(
        update={"research_run_id": "research-run-001", "decided_at": NOW, "as_of": NOW}
    )


@pytest.fixture
def executable_card(purchase_card):
    """The shared purchase card, with broker contract ids on its legs.

    The shared fixture deliberately leaves ``broker_contract_id`` unset — it
    describes the Milestone 1 boundary, where a card can exist before a
    contract has been resolved. Execution needs the resolved ids, and adding
    them here rather than to the shared fixture keeps the Milestone 1 contract
    tests testing what they were written to test.
    """
    legs = [
        leg.model_copy(update={"broker_contract_id": 771234567 + index})
        for index, leg in enumerate(purchase_card.contract.legs)
    ]
    return purchase_card.model_copy(
        update={"contract": purchase_card.contract.model_copy(update={"legs": legs})}
    )


@pytest.fixture
def repository(tmp_path: Path) -> FilesystemExecutionRepository:
    """A store rooted in tmp_path, so no test writes into the repository's data/."""
    return FilesystemExecutionRepository(tmp_path / "execution")


# ---------------------------------------------------------------------------
# Wiring the service without touching the repository's own data/
#
# The upstream repositories are stubs that answer from fixtures. That is not a
# shortcut: it keeps an execution test failing for an execution reason, rather
# than for something that went wrong three milestones upstream.
# ---------------------------------------------------------------------------
@dataclass
class _StubRuns:
    """A repository that answers with one stored run, by id or as the latest."""

    run: Any = None

    def get(self, run_id: str) -> Any:
        if self.run is None:
            return None
        return self.run if run_id == getattr(self.run, "run_id", None) else None

    def latest(self) -> Any:
        return self.run

    def all_runs(self) -> list[Any]:
        return [] if self.run is None else [self.run]

    def save(self, result: Any) -> str:  # pragma: no cover - never called here
        raise AssertionError("execution must not write upstream artifacts")

    def history(self, limit: int | None = None) -> list[Any]:
        return []

    def symbol_history(self, symbol: str, limit: int | None = None) -> list[Any]:
        return []


@pytest.fixture
def settings_paper():
    """PAPER mode, which is what the whole suite runs in."""
    from trading_system.infrastructure.settings import Settings

    return Settings(trading_mode="PAPER")


@pytest.fixture
def stub_repositories(
    tmp_path: Path,
    approved_allocation,
    allocation_run,
    market_research_report,
    strategy_decision_record,
    market_research_run,
    versions,
):
    """Upstream repositories answering from fixtures, plus a real execution store."""
    from trading_system.domain.enums import StrategySelectionStatus
    from trading_system.strategies.models import StrategyRunCounts, StrategyRunResult

    allocation_run = allocation_run.model_copy(
        update={"allocations": [approved_allocation], "research_run_id": "research-run-001"}
    )
    strategy_run = StrategyRunResult(
        run_id="strategy-run-001",
        as_of=NOW,
        generated_at=NOW,
        status=StrategySelectionStatus.SUCCESS,
        research_run_id="research-run-001",
        decisions=[strategy_decision_record],
        counts=StrategyRunCounts(researched_assets=1, considered=1, proposed=1),
        versions=versions,
    )
    return {
        "execution_repository": FilesystemExecutionRepository(tmp_path / "execution"),
        "allocation_repository": _StubRuns(allocation_run),
        "research_repository": _StubRuns(market_research_run),
        "strategy_repository": _StubRuns(strategy_run),
    }


@pytest.fixture
def make_intent(versions: SystemVersions) -> Callable[..., OrderIntent]:
    """A well-formed single-leg order intent."""

    def _make(**overrides: Any) -> OrderIntent:
        legs = overrides.pop(
            "legs",
            [
                OptionLeg(
                    underlying="NVDA",
                    right=OptionRight.CALL,
                    strike=Decimal("180.00"),
                    expiration=EXPIRATION,
                    action=LegAction.BUY,
                    multiplier=100,
                    broker_contract_id=771234567,
                )
            ],
        )
        fields: dict[str, Any] = {
            "intent_id": "intent-0001",
            "purchase_card_id": "card-0001",
            "risk_decision_id": "risk-0001",
            "created_at": NOW,
            "underlying": "NVDA",
            "strategy_type": StrategyType.LONG_CALL,
            "legs": legs,
            "quantity": 1,
            "order_type": OrderType.LIMIT,
            "limit_price": Decimal("6.05"),
            "time_in_force": TimeInForce.DAY,
            "trading_mode": TradingMode.PAPER,
            "versions": versions,
        }
        fields.update(overrides)
        return OrderIntent(**fields)

    return _make


@pytest.fixture
def straddle_legs() -> list[OptionLeg]:
    """Two legs on one strike: a straddle, which must be sent as ONE order."""
    return [
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.CALL,
            strike=Decimal("180.00"),
            expiration=EXPIRATION,
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=771234567,
        ),
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.PUT,
            strike=Decimal("180.00"),
            expiration=EXPIRATION,
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=771234568,
        ),
    ]


@pytest.fixture
def make_record(versions: SystemVersions) -> Callable[..., ExecutionRecord]:
    """A record in SUBMISSION_PENDING: what the engine expects to be handed."""

    def _make(**overrides: Any) -> ExecutionRecord:
        from trading_system.domain.enums import ExecutionState
        from trading_system.execution.models import ExecutionLeg

        request_id = overrides.pop("execution_request_id", "exec-req-test-0001")
        attempt = overrides.pop("attempt", 0)
        legs = overrides.pop(
            "legs",
            [
                ExecutionLeg(
                    leg_index=0,
                    contract_id=771234567,
                    action=LegAction.BUY,
                    right=OptionRight.CALL,
                    underlying="NVDA",
                    expiration=EXPIRATION,
                    strike=Decimal("180.00"),
                    multiplier=100,
                    trading_class="NVDA",
                    currency="EUR",
                )
            ],
        )
        fields: dict[str, Any] = {
            "execution_id": execution_identifier(execution_request_id=request_id, attempt=attempt),
            "execution_request_id": request_id,
            "allocation_id": "allocation-0001",
            "purchase_card_id": "card-0001",
            "risk_decision_id": "risk-0001",
            "order_intent_id": "intent-0001",
            "campaign_id": "campaign-001",
            "opportunity_id": "opportunity-nvda-0001",
            "created_at": NOW,
            "updated_at": NOW,
            "underlying": "NVDA",
            "strategy": StrategyType.LONG_CALL,
            "legs": legs,
            "quantity": 1,
            "multiplier": 100,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.DAY,
            "reference_price": Decimal("605.00"),
            "reference_quote": Decimal("6.05"),
            "submitted_price": Decimal("6.05"),
            "capital_commitment": Decimal("605.00"),
            "maximum_loss": Decimal("605.00"),
            "currency": "EUR",
            "trading_mode": TradingMode.PAPER,
            "dry_run": False,
            "broker": "SIMULATOR",
            "state": ExecutionState.SUBMISSION_PENDING,
            "policy_version": "2026.08.10-1",
            "versions": versions,
        }
        fields.update(overrides)
        return ExecutionRecord(**fields)

    return _make


@pytest.fixture
def make_request(versions: SystemVersions) -> Callable[..., ExecutionRequest]:
    def _make(**overrides: Any) -> ExecutionRequest:
        fields: dict[str, Any] = {
            "execution_request_id": execution_request_identifier(
                allocation_id="allocation-0001",
                trading_mode=TradingMode.PAPER,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                policy_version="2026.08.10-1",
            ),
            "allocation_id": "allocation-0001",
            "campaign_id": "campaign-001",
            "opportunity_id": "opportunity-nvda-0001",
            "requested_at": NOW,
            "execution_authorized": True,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.DAY,
            "trading_mode": TradingMode.PAPER,
            "policy_version": "2026.08.10-1",
            "versions": versions,
        }
        fields.update(overrides)
        return ExecutionRequest(**fields)

    return _make


@pytest.fixture
def writable_simulator(clock: FixedClock) -> SimulatedBroker:
    """A connected simulator that is allowed to execute.

    ``read_only=False`` is the only way to reach the submission path, and the
    simulator is the only broker a test may ever have one of.
    """
    broker = SimulatedBroker(
        SimulatedBrokerState(),
        clock=clock,
        trading_mode=TradingMode.PAPER,
        read_only=False,
    )
    broker.connect()
    return broker


@pytest.fixture
def read_only_simulator(clock: FixedClock) -> SimulatedBroker:
    broker = SimulatedBroker(SimulatedBrokerState(), clock=clock, trading_mode=TradingMode.PAPER)
    broker.connect()
    return broker


# ---------------------------------------------------------------------------
# A controlled fake broker
#
# The simulator covers the ordinary paths. This covers the ones a real broker
# reaches and a simulator will not: a submission that times out *after* the
# broker accepted the order, a response with no order id, a broker that
# disagrees with itself. Its counters are the assertions.
# ---------------------------------------------------------------------------
@dataclass
class FakeBrokerScript:
    """What the fake broker will do, decided before it does it."""

    #: Raised instead of answering. The order is still recorded as received,
    #: which is the point: this models the ambiguous case.
    raise_on_submit: Exception | None = None
    status: OrderStatus = OrderStatus.SUBMITTED
    broker_order_id: str | None = "fake-order-1"
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    fills: list[Fill] = field(default_factory=list)
    message: str | None = None
    raise_on_cancel: Exception | None = None
    open_orders: list[Any] = field(default_factory=list)


class FakeBroker(Broker):
    """A broker whose every answer is arranged by the test.

    Deliberately a real :class:`Broker` subclass so it goes through the same
    ``place_order`` guard as everything else — including the counter, which is
    what several tests assert on.
    """

    def __init__(
        self,
        script: FakeBrokerScript | None = None,
        *,
        trading_mode: TradingMode = TradingMode.PAPER,
        read_only: bool = False,
        connected: bool = True,
    ) -> None:
        super().__init__(trading_mode=trading_mode, read_only=read_only)
        self.script = script or FakeBrokerScript()
        self._connected = connected
        #: Every intent this broker was asked to send, in order. The record of
        #: what actually left the system.
        self.received: list[OrderIntent] = []
        self.cancelled: list[str] = []

    @property
    def name(self) -> str:
        return "FAKE"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self):
        self._connected = True
        return self.health_check()

    def disconnect(self) -> None:
        self._connected = False

    def health_check(self):
        from trading_system.domain.enums import BrokerConnectionState
        from trading_system.domain.models import BrokerHealth

        return BrokerHealth(
            broker=self.name,
            state=(
                BrokerConnectionState.CONNECTED
                if self._connected
                else BrokerConnectionState.DISCONNECTED
            ),
            as_of=NOW,
            trading_mode=self.trading_mode,
            read_only=self.read_only,
        )

    def get_account(self):
        raise BrokerError("the fake broker reports no account")

    def get_account_summary(self) -> dict[str, str]:
        return {}

    def get_positions(self):
        return []

    def get_open_orders(self):
        return list(self.script.open_orders)

    def get_executions(self):
        return []

    def get_contract(self, symbol, security_type=None, *, exchange=None, currency=None):
        raise BrokerError("the fake broker resolves no contracts")

    def get_market_data(self, symbol, security_type=None):
        raise BrokerError("the fake broker quotes nothing")

    def get_option_chain(self, underlying):
        raise BrokerError("the fake broker has no chain")

    def _submit_order(self, intent: OrderIntent) -> ExecutionResult:
        # Recorded before any failure, exactly as a real broker would have
        # received the bytes before failing to answer.
        self.received.append(intent)
        if self.script.raise_on_submit is not None:
            raise self.script.raise_on_submit
        return ExecutionResult(
            intent_id=intent.intent_id,
            broker=self.name,
            broker_order_id=self.script.broker_order_id,
            status=self.script.status,
            orders_submitted=1,
            filled_quantity=self.script.filled_quantity,
            average_fill_price=self.script.average_fill_price,
            fills=list(self.script.fills),
            submitted_at=NOW,
            last_update_at=NOW,
            message=self.script.message,
            trading_mode=self.trading_mode,
        )

    def _cancel_order(self, broker_order_id: str):
        self.cancelled.append(broker_order_id)
        if self.script.raise_on_cancel is not None:
            raise self.script.raise_on_cancel
        from trading_system.domain.enums import OrderSide, SecurityType
        from trading_system.domain.models import BrokerOrder

        return BrokerOrder(
            broker_order_id=broker_order_id,
            as_of=NOW,
            source=self.name,
            symbol="NVDA",
            security_type=SecurityType.OPTION,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type="LMT",
            status=OrderStatus.CANCELLED,
            filled_quantity=Decimal(self.script.filled_quantity),
            remaining_quantity=Decimal("0"),
            updated_at=NOW,
        )


@pytest.fixture
def fake_broker() -> Callable[..., FakeBroker]:
    def _make(**overrides: Any) -> FakeBroker:
        connected = overrides.pop("connected", True)
        read_only = overrides.pop("read_only", False)
        trading_mode = overrides.pop("trading_mode", TradingMode.PAPER)
        return FakeBroker(
            FakeBrokerScript(**overrides),
            trading_mode=trading_mode,
            read_only=read_only,
            connected=connected,
        )

    return _make
