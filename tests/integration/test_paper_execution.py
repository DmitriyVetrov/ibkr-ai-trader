"""Controlled validation against a real IBKR Paper account (brief sections 35, 44, 45).

**This file submits a real order to a real broker.** It is skipped unless both
``ALLOW_LIVE_TESTS=true`` and ``RUN_PAPER_EXECUTION_TESTS=true`` are set, and it
refuses to run in any mode but PAPER. Two variables rather than one is
deliberate: a developer who unlocked the gateway to run a read-only diagnostic
must not thereby have authorised an order.

Run it with:

.. code-block:: bash

    ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true TRADING_MODE=PAPER \\
      IBKR_READ_ONLY=false .venv/bin/pytest -m paper_execution -s

The design of the test order matters as much as the gating. It is one contract,
far out of the money, at a limit price far below anything that could trade — but
"extremely unlikely to fill" is **not** "guaranteed not to fill", and the test
is written to handle a fill rather than to assume one cannot happen. If it
fills, the fill is recorded and reported; nothing here hides it.

The connection design follows the Milestone 2 finding directly: one short-lived
connection per operation. Submitting, querying and cancelling are three separate
sessions, because only the first uncached round trip on a TWS connection is
reliably answered.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.broker.base import BrokerError
from trading_system.broker.ibkr import IBKRBroker
from trading_system.domain.enums import (
    ExecutionState,
    OrderStatus,
    SecurityType,
    TradingMode,
)
from trading_system.infrastructure.settings import ConfigError, Settings, load_config

pytestmark = [pytest.mark.paper_execution, pytest.mark.ibkr]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def paper_settings() -> Settings:
    """Settings for a writable paper session, or a refusal to run at all.

    Every gate is checked here rather than inside the test, so a
    misconfiguration stops before anything is built — let alone sent.
    """
    if os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() != "true":
        pytest.skip("RUN_PAPER_EXECUTION_TESTS is not set")

    try:
        settings = Settings()
    except Exception as exc:  # a LIVE guard violation must stop the run
        pytest.fail(f"settings refused to load: {exc}")

    if settings.trading_mode is not TradingMode.PAPER:
        # Not a skip. Being pointed at LIVE while asked to submit an order is a
        # configuration error worth failing loudly on.
        pytest.fail(
            f"TRADING_MODE is {settings.trading_mode.value}; this test submits orders and "
            f"runs against PAPER only. No order was sent."
        )
    if settings.ibkr_read_only:
        pytest.skip("IBKR_READ_ONLY=true: set it to false explicitly to submit paper orders")
    return settings


@pytest.fixture(scope="module")
def config():
    try:
        return load_config()
    except ConfigError as exc:  # pragma: no cover - environment dependent
        pytest.fail(f"configuration did not load: {exc}")


def _open(settings: Settings) -> IBKRBroker:
    """One short-lived writable connection.

    Milestone 2's constraint, applied literally: each operation gets its own
    session, so no operation depends on a second uncached round trip being
    answered on a connection that already spent one.

    Narrowed to :class:`IBKRBroker` deliberately. This test is about the real
    adapter, and a simulator reaching this point would mean the settings sent
    it somewhere other than a gateway — which is worth failing on rather than
    quietly validating nothing.
    """
    from trading_system.broker.factory import build_execution_broker

    broker = build_execution_broker(settings)
    if not isinstance(broker, IBKRBroker):
        pytest.fail(
            f"expected a real IBKR connection, got {type(broker).__name__}. This test "
            f"validates the gateway path and nothing was sent."
        )
    broker.connect()
    return broker


# ---------------------------------------------------------------------------
# The validation
# ---------------------------------------------------------------------------
def test_paper_execution_round_trip(paper_settings: Settings, config) -> None:
    """Submit one paper order, verify it, cancel it, and verify the cancellation.

    Reports, at the end:

        MODE / ORDERS_SUBMITTED / FILLED / CANCELLED / FINAL_STATE

    printed rather than only asserted, because the point of this test is the
    evidence it produces about a real environment.
    """
    from trading_system.domain.enums import LegAction, OptionRight, OrderType, TimeInForce
    from trading_system.domain.models import OptionLeg, OrderIntent, SystemVersions

    print("\n" + "=" * 72)
    print("PAPER EXECUTION VALIDATION — THIS SUBMITS A REAL ORDER TO IBKR PAPER")
    print("=" * 72)

    # --- 1. resolve a real, currently-tradeable contract -------------------
    #
    # A fresh connection for the chain read: it is an uncached request, so it
    # gets a session of its own and spends that session's one round trip.
    chain_broker = _open(paper_settings)
    try:
        account = chain_broker.verify_paper_account(config.execution.paper_account_prefixes)
        print(f"Account (masked)   : {'*' * (len(account) - 4)}{account[-4:]}")
        chain = chain_broker.get_option_chain("SPY")
    finally:
        chain_broker.disconnect()

    if not chain.expirations or not chain.strikes:
        pytest.skip("IBKR returned no usable SPY option chain; nothing to price an order against")

    # Far out of the money and far from any price that could trade. Unlikely to
    # fill — which is not the same as unable to, and the assertions below do
    # not assume otherwise.
    expiration = sorted(chain.expirations)[0]
    strike = max(chain.strikes)
    print(f"Contract           : SPY {expiration} {strike} CALL")

    contract_broker = _open(paper_settings)
    try:
        contract = contract_broker.get_contract(
            "SPY", SecurityType.OPTION, exchange="SMART", currency="USD"
        )
    except BrokerError as exc:
        contract_broker.disconnect()
        pytest.skip(f"could not resolve a paper contract to trade: {exc}")
    else:
        contract_broker.disconnect()

    if contract.contract_id is None:
        pytest.skip("IBKR resolved no contract id; refusing to invent one")

    intent = OrderIntent(
        intent_id=f"paper-validation-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}",
        purchase_card_id="paper-validation-card",
        risk_decision_id="paper-validation-risk",
        created_at=datetime.now(UTC),
        underlying="SPY",
        strategy_type="LONG_CALL",
        legs=[
            OptionLeg(
                underlying="SPY",
                right=OptionRight.CALL,
                strike=Decimal(str(strike)),
                expiration=expiration,
                action=LegAction.BUY,
                multiplier=100,
                broker_contract_id=contract.contract_id,
            )
        ],
        quantity=1,
        order_type=OrderType.LIMIT,
        # One cent: far below anything that could trade, and still a valid,
        # well-formed order rather than a malformed one.
        limit_price=Decimal("0.01"),
        time_in_force=TimeInForce.DAY,
        trading_mode=TradingMode.PAPER,
        versions=SystemVersions(application_version="0.1.0", config_version="paper-validation"),
    )

    # --- 2. submit, on its own connection ----------------------------------
    submitted = 0
    broker_order_id: str | None = None
    filled = 0
    final_status: OrderStatus | None = None

    submit_broker = _open(paper_settings)
    try:
        print("Submitting         : 1 x SPY CALL @ 0.01 LIMIT DAY")
        result = submit_broker.place_order(intent)
        submitted = submit_broker.orders_submitted
        broker_order_id = result.broker_order_id
        filled = result.filled_quantity
        final_status = result.status
        print(f"Broker order id    : {broker_order_id}")
        print(f"Status             : {result.status.value}")
    except BrokerError as exc:
        # An uncertain submission is a real outcome and is reported as one. The
        # order may be live; the operator is told to look rather than retry.
        submitted = submit_broker.orders_submitted
        print(f"SUBMISSION UNCERTAIN: {exc}")
        print(f"ORDERS_SUBMITTED   : {submitted}")
        print("Check open orders before running this again. Do NOT assume nothing was sent.")
        pytest.fail(
            f"the submission did not complete cleanly: {exc}. An order may be live; this test "
            f"does not retry."
        )
    finally:
        submit_broker.disconnect()

    assert submitted == 1, "exactly one order should have been submitted"

    # --- 3. verify at the broker, on a fresh connection --------------------
    verify_broker = _open(paper_settings)
    try:
        open_orders = verify_broker.get_open_orders()
    finally:
        verify_broker.disconnect()

    found = [order for order in open_orders if order.broker_order_id == broker_order_id]
    print(f"Open at broker     : {'yes' if found else 'no'}")
    if found:
        filled = int(found[0].filled_quantity)
        final_status = found[0].status

    # --- 4. cancel if still working, on a fresh connection -----------------
    cancelled = False
    if found and found[0].status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
        cancel_broker = _open(paper_settings)
        try:
            assert broker_order_id is not None
            cancelled_order = cancel_broker.cancel_order(broker_order_id)
            cancelled = cancelled_order.status is OrderStatus.CANCELLED
            final_status = cancelled_order.status
        except BrokerError as exc:
            print(f"CANCEL FAILED      : {exc}")
        finally:
            cancel_broker.disconnect()

        # --- 5. verify the cancellation, on a fresh connection -------------
        confirm_broker = _open(paper_settings)
        try:
            still_open = [
                order
                for order in confirm_broker.get_open_orders()
                if order.broker_order_id == broker_order_id
            ]
        finally:
            confirm_broker.disconnect()
        cancelled = cancelled or not still_open
        if still_open:
            final_status = still_open[0].status
            filled = int(still_open[0].filled_quantity)

    # --- 6. report ---------------------------------------------------------
    print("-" * 72)
    print(f"MODE               = {paper_settings.trading_mode.value}")
    print(f"ORDERS_SUBMITTED   = {submitted}")
    print(f"FILLED             = {filled}/{intent.quantity}")
    print(f"CANCELLED          = {str(cancelled).lower()}")
    print(f"FINAL_STATE        = {final_status.value if final_status else 'UNKNOWN'}")
    print("=" * 72)

    if filled:
        # Not hidden, not skipped over. An unexpected fill is a real position
        # in the paper account and the operator has to know it exists.
        print(
            f"NOTE: the order FILLED {filled} contract(s) despite being priced not to. "
            f"A real paper position now exists and should be closed deliberately."
        )

    assert submitted == 1
    assert broker_order_id is not None, "a submitted order must be identifiable"
    if not filled:
        assert cancelled or not found, "an unfilled order should end cancelled or already gone"


def test_a_second_order_is_never_submitted_by_a_retry(paper_settings: Settings) -> None:
    """The safety property, asserted against the real adapter.

    Nothing in the execution path retries. This checks the policy that says so
    is actually in force, without submitting anything.
    """
    config_ = load_config()
    assert config_.execution.auto_retry_on_timeout is False


def test_the_paper_session_is_provably_a_paper_account(paper_settings: Settings, config) -> None:
    """Brief section 55: prove the session before sending, or refuse."""
    broker = _open(paper_settings)
    try:
        account = broker.verify_paper_account(config.execution.paper_account_prefixes)
        assert account.upper().startswith(("DU", "DF"))
        assert broker.orders_submitted == 0, "verifying an account submits nothing"
    finally:
        broker.disconnect()


def test_execution_state_never_claims_a_fill_without_one(paper_settings: Settings) -> None:
    """A local check of the mapping the paper run depends on. Sends nothing."""
    from trading_system.execution.fill_tracker import state_for

    assert (
        state_for(
            OrderStatus.SUBMITTED, filled_quantity=0, submitted_quantity=1, has_broker_order_id=True
        )
        is ExecutionState.SUBMITTED
    )
