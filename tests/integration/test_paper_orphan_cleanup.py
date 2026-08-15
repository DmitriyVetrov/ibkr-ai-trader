"""Orphan-cleanup validation against a real IBKR Paper account.

**The submitting half of this file can sell real holdings out of a real paper
account.** It is therefore behind *three* variables rather than the usual two,
and the third is deliberately not ``--confirm`` and not a marker:

.. code-block:: text

    ALLOW_LIVE_TESTS=true              unlock the gateway at all
    RUN_PAPER_EXECUTION_TESTS=true     authorise an order from the test suite
    RUN_ORPHAN_CLEANUP_PAPER_TEST=true authorise selling out of orphan holdings

The first two are the existing gate every paper-execution test uses, and they
are not weakened here. The third exists because they are not enough: a
developer who unlocked the suite to validate *buying* one contract has not
thereby authorised *liquidating whatever the account already held*. The two
acts differ in what they can destroy.

The read-only half runs under the ordinary two-variable gate and is the more
useful test most of the time. It validates, against a live gateway:

1. reconciliation identifies the account's real orphan holdings;
2. the cleanup review selects them by broker contract id, evaluates every
   gate, builds every order — and submits **zero**;
3. the broker's own submitted-order counter confirms it.

Run it with:

.. code-block:: bash

    ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true TRADING_MODE=PAPER \\
      IBKR_READ_ONLY=false .venv/bin/pytest -m paper_execution \\
      tests/integration/test_paper_orphan_cleanup.py -s
"""

from __future__ import annotations

import os

import pytest

from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.settings import ConfigError, Settings, load_config

pytestmark = [pytest.mark.paper_execution, pytest.mark.ibkr]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def paper_settings() -> Settings:
    """Settings for a real paper session, or a refusal to run at all."""
    if os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() != "true":
        pytest.skip("RUN_PAPER_EXECUTION_TESTS is not set")

    try:
        settings = Settings()
    except Exception as exc:  # a LIVE guard violation must stop the run
        pytest.fail(f"settings refused to load: {exc}")

    if settings.trading_mode is not TradingMode.PAPER:
        pytest.fail(
            f"TRADING_MODE is {settings.trading_mode.value}; this test can sell real holdings "
            f"and runs against PAPER only. Nothing was sent."
        )
    if settings.live_trading_confirmed or settings.live_readiness_checklist_signed_off:
        pytest.fail(
            "a LIVE guard is active. This test stops rather than reasoning about which mode "
            "it is really in. Nothing was sent."
        )
    return settings


@pytest.fixture(scope="module")
def config():
    try:
        return load_config()
    except ConfigError as exc:  # pragma: no cover - environment dependent
        pytest.fail(f"configuration did not load: {exc}")


def _cleanup_service(settings: Settings, config):
    from trading_system.cleanup.service import CleanupService

    return CleanupService(settings=settings, config=config)


# ---------------------------------------------------------------------------
# The review, against a live gateway
# ---------------------------------------------------------------------------
def test_the_review_against_the_real_account_submits_nothing(
    paper_settings: Settings, config
) -> None:
    """The central claim, checked against a gateway rather than a simulator.

    Reported rather than only asserted, because the point of this test is the
    evidence it produces about a real account.
    """
    print("\n" + "=" * 72)
    print("PAPER ORPHAN CLEANUP — REVIEW PHASE (submits nothing)")
    print("=" * 72)

    service = _cleanup_service(paper_settings, config)
    outcome = service.run(authorized=False)

    selection = outcome.plan.selection
    print(f"Reconciliation     : {selection.reconciliation_id}")
    print(f"Account (masked)   : {selection.account_reference}")
    print(f"Orphan findings    : {selection.orphan_count}")
    print(f"Targetable         : {len(selection.targets)}")
    for target in selection.targets:
        print(
            f"  {target.key}  {target.describe()}  held={target.quantity}  "
            f"price={target.market_price}"
        )
    for candidate in selection.rejected:
        print(f"  SKIP {candidate.key}: {candidate.reason}")
    print("\nGATES")
    for verdict in outcome.plan.run_gates:
        print(f"  {verdict.render()}")
    print(f"\nMODE               = {paper_settings.trading_mode.value}")
    print(f"ORDERS_SUBMITTED   = {outcome.run.orders_submitted}")
    print("=" * 72)

    assert outcome.run.orders_submitted == 0, "a review must never submit an order"
    assert outcome.run.corrective_orders == 0
    assert outcome.run.dry_run is True
    # Every target is addressed by the broker's own contract id, never by symbol.
    assert all(target.key.startswith("cid:") for target in selection.targets)


def test_the_review_stores_no_cleanup_record(paper_settings: Settings, config) -> None:
    """Looking at an account is not an event worth an immutable record."""
    service = _cleanup_service(paper_settings, config)
    before = len(service.repository.history())

    service.run(authorized=False)

    assert len(service.repository.history()) == before


def test_the_review_adopts_nothing(paper_settings: Settings, config) -> None:
    """The real account's orphans stay orphans until somebody says otherwise."""
    service = _cleanup_service(paper_settings, config)
    outcome = service.run(authorized=False)

    projection = service.reconciliation.positions.expected()
    targeted = {target.key for target in outcome.plan.selection.targets}
    assert not (targeted & {position.key for position in projection.positions}), (
        "a targeted orphan appeared in the internal expected-position ledger"
    )


# ---------------------------------------------------------------------------
# The submission, behind a third variable
# ---------------------------------------------------------------------------
@pytest.fixture
def cleanup_authorised(paper_settings: Settings, config) -> Settings:
    """The third gate. Deliberately not ``--confirm`` and not a marker.

    ``RUN_PAPER_EXECUTION_TESTS`` authorises the suite to *buy* one contract it
    chose. Selling whatever the account already held is a different act with a
    different blast radius, and inheriting the authorisation would be exactly
    the weakening this file exists not to do.
    """
    if os.environ.get("RUN_ORPHAN_CLEANUP_PAPER_TEST", "false").lower() != "true":
        pytest.skip(
            "RUN_ORPHAN_CLEANUP_PAPER_TEST is not set. This test SELLS the account's "
            "pre-existing holdings; unlocking the suite for a paper buy does not authorise it."
        )
    if paper_settings.ibkr_read_only:
        pytest.skip("IBKR_READ_ONLY=true: set it to false explicitly to submit paper orders")
    if not config.cleanup.enabled:
        pytest.skip("cleanup.enabled is false in config/cleanup.yaml")
    if not config.execution.enabled:
        pytest.skip("execution.enabled is false in config/execution.yaml")
    return paper_settings


def test_paper_orphan_cleanup_round_trip(cleanup_authorised: Settings, config) -> None:
    """Close the account's orphan holdings, and observe what actually happened.

    Reports, at the end:

        MODE / ORDERS_SUBMITTED / CORRECTIVE / CLOSED / UNCERTAIN / REMAINING

    printed rather than only asserted, because the point of this test is the
    evidence it produces about a real environment.
    """
    print("\n" + "=" * 72)
    print("PAPER ORPHAN CLEANUP — THIS SELLS REAL HOLDINGS IN THE PAPER ACCOUNT")
    print("=" * 72)

    service = _cleanup_service(cleanup_authorised, config)
    review = service.run(authorized=False)
    if not review.plan.selection.targets:
        pytest.skip("the paper account reports no targetable orphan holding")

    from trading_system.cleanup.report import render_confirmation_summary

    assert review.plan.request is not None
    print(
        render_confirmation_summary(
            review.plan.request,
            account_reference=review.run.account_reference,
            mode=cleanup_authorised.trading_mode,
        )
    )

    outcome = service.run(authorized=True)
    run = outcome.run

    print(f"MODE               = {run.trading_mode.value}")
    print(f"ORDERS_SUBMITTED   = {run.orders_submitted}")
    print(f"CORRECTIVE         = {run.corrective_orders}")
    print(f"CLOSED             = {run.closed} of {len(run.outcomes)}")
    print(f"UNCERTAIN          = {run.uncertain}")
    for item in run.outcomes:
        print(
            f"  {item.key}  {item.status.value}  order={item.broker_order_id}  "
            f"filled={item.filled_quantity}  held_after={item.observed_quantity_after}"
        )
    print("=" * 72)

    assert run.trading_mode is TradingMode.PAPER
    assert run.corrective_orders == 0
    # One order per target at most. Never more, whatever happened.
    assert run.orders_submitted <= len(run.outcomes)
    # Every outcome is accounted for by a real broker fact.
    for item in run.outcomes:
        assert item.requested_quantity == int(item.observed_quantity_before)
        assert item.filled_quantity <= item.requested_quantity

    if run.uncertain:
        pytest.fail(
            f"{run.uncertain} submission(s) are UNCERTAIN: an order may be live at the broker. "
            f"Do NOT re-run. Resolve with 'execution explain --execution-id <ID> --resolve'."
        )


def test_a_second_confirmed_run_sells_nothing_further(cleanup_authorised: Settings, config) -> None:
    """Idempotency, against a real gateway. Runs after the round trip above."""
    service = _cleanup_service(cleanup_authorised, config)
    outcome = service.run(authorized=True)

    print(f"\nSECOND RUN ORDERS_SUBMITTED = {outcome.run.orders_submitted}")
    assert outcome.run.orders_submitted == 0, (
        "a second confirmed run submitted an order; the holdings it targeted were either "
        "already closed or already have an order at the broker"
    )
