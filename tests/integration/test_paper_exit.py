"""Controlled exit validation against a real IBKR Paper account (Milestone 10).

**This file can submit a real order to a real broker.** It is skipped unless
both ``ALLOW_LIVE_TESTS=true`` and ``RUN_PAPER_EXECUTION_TESTS=true`` are set,
and it refuses to run in any mode but PAPER — the same two-variable gate
Milestone 8's paper test uses, and for the same reason: a developer who
unlocked the gateway for a read-only diagnostic must not thereby have
authorised an order.

Run it with:

.. code-block:: bash

    ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true TRADING_MODE=PAPER \\
      IBKR_READ_ONLY=false .venv/bin/pytest -m paper_execution \\
      tests/integration/test_paper_exit.py -s

What it validates, in order, is the whole Milestone 10 seam against a real
gateway:

1. the exit subsystem's own read path reaches a live broker only through
   Milestone 9's read-only connection, and evaluating submits nothing;
2. an exit decision produces a ``SELL`` order for contracts the account
   actually holds;
3. the order is observed, cancelled if it is still working, and observed again.

**The exit order is deliberately priced not to fill**: a limit far above any
bid, so a *sale* at that price is implausible. "Implausible" is not
"impossible", and every assertion below is written to handle a fill rather than
to assume one cannot happen — if it fills, the position really closed, and the
report says so.

If the account holds no option position at all, the test skips rather than
opening one. Milestone 10 closes positions; it does not create them, and a test
that opened one to have something to close would be testing Milestone 8 through
the wrong door.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from trading_system.broker.base import BrokerError
from trading_system.broker.ibkr import IBKRBroker
from trading_system.domain.enums import OrderStatus, SecurityType, TradingMode
from trading_system.infrastructure.settings import ConfigError, Settings, load_config

pytestmark = [pytest.mark.paper_execution, pytest.mark.ibkr]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def paper_settings() -> Settings:
    """Settings for a writable paper session, or a refusal to run at all."""
    if os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() != "true":
        pytest.skip("RUN_PAPER_EXECUTION_TESTS is not set")

    try:
        settings = Settings()
    except Exception as exc:  # a LIVE guard violation must stop the run
        pytest.fail(f"settings refused to load: {exc}")

    if settings.trading_mode is not TradingMode.PAPER:
        pytest.fail(
            f"TRADING_MODE is {settings.trading_mode.value}; this test can submit an order and "
            f"runs against PAPER only. Nothing was sent."
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


def _open_read_only(settings: Settings) -> IBKRBroker:
    """One short-lived **read-only** connection, for observation."""
    from trading_system.broker.factory import build_broker

    broker = build_broker(settings)
    if not isinstance(broker, IBKRBroker):
        pytest.fail(
            f"expected a real IBKR connection, got {type(broker).__name__}. Nothing was sent."
        )
    broker.connect()
    return broker


def _open_writable(settings: Settings) -> IBKRBroker:
    """One short-lived writable connection, for the one submission."""
    from trading_system.broker.factory import build_execution_broker

    broker = build_execution_broker(settings)
    if not isinstance(broker, IBKRBroker):
        pytest.fail(
            f"expected a real IBKR connection, got {type(broker).__name__}. Nothing was sent."
        )
    broker.connect()
    return broker


# ---------------------------------------------------------------------------
# Evaluation reaches a broker read-only and submits nothing
# ---------------------------------------------------------------------------
def test_evaluating_exits_against_the_real_account_submits_nothing(
    paper_settings: Settings, config
) -> None:
    """The most important claim in the milestone, checked against a gateway.

    One short-lived read-only connection, four cache-backed reads, and the
    broker's own counter afterwards. Reported rather than only asserted.
    """
    print("\n" + "=" * 72)
    print("PAPER EXIT VALIDATION — EVALUATION PHASE (submits nothing)")
    print("=" * 72)

    broker = _open_read_only(paper_settings)
    try:
        account = broker.verify_paper_account(config.execution.paper_account_prefixes)
        positions = broker.get_positions()
        submitted = broker.orders_submitted
    finally:
        broker.disconnect()

    masked = f"{'*' * (len(account) - 4)}{account[-4:]}"
    options = [p for p in positions if p.security_type is SecurityType.OPTION]
    print(f"Account (masked)   : {masked}")
    print(f"Option positions   : {len(options)}")
    print(f"MODE               = {paper_settings.trading_mode.value}")
    print(f"ORDERS_SUBMITTED   = {submitted}")
    print("=" * 72)

    assert submitted == 0, "reading positions for an exit evaluation must submit nothing"


# ---------------------------------------------------------------------------
# One controlled exit order
# ---------------------------------------------------------------------------
def test_paper_exit_round_trip(paper_settings: Settings, config) -> None:
    """Sell one contract the account actually holds, observe it, cancel it.

    Reports, at the end:

        MODE / ORDERS_SUBMITTED / FILLED / CANCELLED / FINAL_STATE

    printed rather than only asserted, because the point of this test is the
    evidence it produces about a real environment.
    """
    from datetime import UTC, datetime

    from trading_system.domain.enums import LegAction, OrderType, TimeInForce
    from trading_system.domain.models import OptionLeg, OrderIntent, SystemVersions

    print("\n" + "=" * 72)
    print("PAPER EXIT VALIDATION — THIS CAN SUBMIT A REAL SELL ORDER TO IBKR PAPER")
    print("=" * 72)

    # --- 1. what does the account actually hold? ---------------------------
    read_broker = _open_read_only(paper_settings)
    try:
        positions = read_broker.get_positions()
    finally:
        read_broker.disconnect()

    held = [
        position
        for position in positions
        if position.security_type is SecurityType.OPTION
        and position.quantity > 0
        and position.contract_id
        and position.expiration
        and position.strike
        and position.right
    ]
    if not held:
        pytest.skip(
            "the paper account holds no long option position to exit. Milestone 10 closes "
            "positions; it does not open one to have something to close."
        )

    position = held[0]
    assert position.expiration is not None
    assert position.strike is not None
    assert position.right is not None
    assert position.contract_id is not None
    print(
        f"Position           : {position.symbol} {position.expiration} "
        f"{position.strike} {position.right.value} x{position.quantity}"
    )

    # A limit far ABOVE any plausible bid: a *sale* at this price is
    # implausible, which is the mirror of Milestone 8's far-below buy limit.
    limit = Decimal("9999.00")
    intent = OrderIntent(
        intent_id=f"paper-exit-validation-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}",
        purchase_card_id="paper-exit-validation-card",
        risk_decision_id="paper-exit-validation-risk",
        created_at=datetime.now(UTC),
        underlying=position.symbol,
        strategy_type="LONG_CALL",
        legs=[
            OptionLeg(
                underlying=position.symbol,
                right=position.right,
                strike=position.strike,
                expiration=position.expiration,
                # SELL: this closes what is held. The whole point of the test.
                action=LegAction.SELL,
                multiplier=position.multiplier or 100,
                broker_contract_id=position.contract_id,
            )
        ],
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=limit,
        time_in_force=TimeInForce.DAY,
        trading_mode=TradingMode.PAPER,
        versions=SystemVersions(
            application_version="0.1.0", config_version="paper-exit-validation"
        ),
    )

    # --- 2. submit, on its own connection ----------------------------------
    submitted = 0
    broker_order_id: str | None = None
    filled = 0
    final_status: OrderStatus | None = None

    submit_broker = _open_writable(paper_settings)
    try:
        print(f"Submitting         : SELL 1 x {position.symbol} @ {limit} LIMIT DAY")
        result = submit_broker.place_order(intent)
        submitted = submit_broker.orders_submitted
        broker_order_id = result.broker_order_id
        filled = result.filled_quantity
        final_status = result.status
        print(f"Broker order id    : {broker_order_id}")
        print(f"Status             : {result.status.value}")
    except BrokerError as exc:
        submitted = submit_broker.orders_submitted
        print(f"SUBMISSION UNCERTAIN: {exc}")
        print(f"ORDERS_SUBMITTED   : {submitted}")
        print("Check open orders before running this again. Do NOT assume nothing was sent.")
        pytest.fail(
            f"the exit submission did not complete cleanly: {exc}. An order may be live; this "
            f"test does not retry."
        )
    finally:
        submit_broker.disconnect()

    assert submitted == 1, "exactly one exit order should have been submitted"

    # --- 3. observe, on a fresh connection ---------------------------------
    verify_broker = _open_read_only(paper_settings)
    try:
        open_orders = verify_broker.get_open_orders()
    finally:
        verify_broker.disconnect()

    found = [order for order in open_orders if order.broker_order_id == broker_order_id]
    print(f"Open at broker     : {'yes' if found else 'no'}")
    if found:
        filled = int(found[0].filled_quantity)
        final_status = found[0].status
        assert found[0].side.value == "SELL", "an exit order must be a sale"

    # --- 4. cancel if still working ----------------------------------------
    cancelled = False
    if found and found[0].status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
        cancel_broker = _open_writable(paper_settings)
        try:
            assert broker_order_id is not None
            cancelled_order = cancel_broker.cancel_order(broker_order_id)
            cancelled = cancelled_order.status is OrderStatus.CANCELLED
            final_status = cancelled_order.status
        except BrokerError as exc:
            print(f"CANCEL FAILED      : {exc}")
        finally:
            cancel_broker.disconnect()

        confirm_broker = _open_read_only(paper_settings)
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

    # --- 5. report ---------------------------------------------------------
    print("-" * 72)
    print(f"MODE               = {paper_settings.trading_mode.value}")
    print(f"ORDERS_SUBMITTED   = {submitted}")
    print(f"FILLED             = {filled}/{intent.quantity}")
    print(f"CANCELLED          = {str(cancelled).lower()}")
    print(f"FINAL_STATE        = {final_status.value if final_status else 'UNKNOWN'}")
    print("=" * 72)

    if filled:
        print(
            f"NOTE: the exit FILLED {filled} contract(s) despite being priced not to. The "
            f"paper position is genuinely smaller now; reconcile before running this again."
        )

    assert submitted == 1
    assert broker_order_id is not None, "a submitted order must be identifiable"
    if not filled:
        assert cancelled or not found, "an unfilled exit should end cancelled or already gone"


# ---------------------------------------------------------------------------
# Local checks the paper run depends on. These send nothing.
# ---------------------------------------------------------------------------
def test_no_configuration_permits_re_sending_an_unknown_exit(paper_settings: Settings) -> None:
    """The policy that says an ambiguous exit is never retried is in force."""
    loaded = load_config()

    assert loaded.execution.auto_retry_on_timeout is False
    assert loaded.exit.order.require_explicit_authorization is True
    assert loaded.exit.allow_independent_leg_exit is False


def test_the_paper_session_is_provably_a_paper_account(paper_settings: Settings, config) -> None:
    """Prove the session before sending, or refuse."""
    broker = _open_read_only(paper_settings)
    try:
        account = broker.verify_paper_account(config.execution.paper_account_prefixes)
        assert account.upper().startswith(("DU", "DF"))
        assert broker.orders_submitted == 0, "verifying an account submits nothing"
    finally:
        broker.disconnect()
