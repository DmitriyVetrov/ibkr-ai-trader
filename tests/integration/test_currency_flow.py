"""The whole currency flow, end to end, through the real services.

.. code-block:: text

    IBKR account            EUR 100,000 cash, USD 0, base currency EUR
          |
          |  ExchangeRate rows, read in the SAME account summary
          v
    account snapshot        balance and rate, one observation, stored
          |
          v
    campaign envelope       EUR 5,000 declared  ->  USD 5,555 to spend
          |
          v
    risk limits             every money limit converted once, at one rate
          |
          v
    position sizing         USD room / USD unit cost
          |
          v
    order validation        the contract's own currency, never converted

Six cases, from the brief, checked against the assembled chain rather than
against a unit under test — because the failure this milestone removes lives in
the *seams*: a figure produced in one currency and compared in another looks
correct everywhere it is produced and everywhere it is consumed, and is wrong
only where the two meet.

There is no broker connection here beyond the simulator, and every run reports
zero submitted orders.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.integration.test_research_to_allocation import (  # noqa: F401
    NOW,
    _run_everything,
    broker,
    workflow,
)

from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationRunStatus,
    FxRateOrigin,
    FxStatus,
    RiskReasonCode,
    TradingMode,
)
from trading_system.fx.models import FxRate, FxRateTable
from trading_system.risk.account import build_account_snapshot
from trading_system.risk.limits import resolve_limits
from trading_system.risk.store import FilesystemAccountSnapshotRepository

pytestmark = pytest.mark.integration


def _accounts(tmp_path: Path) -> FilesystemAccountSnapshotRepository:
    return FilesystemAccountSnapshotRepository(tmp_path / "data" / "accounts")


def _restate_account(tmp_path: Path, **overrides: object):
    """Replace the stored account snapshot with one the test controls.

    The chain reads whichever snapshot is newest, so overwriting the store is
    how a test says "the broker reported this instead" without reaching for a
    broker it is not allowed to hold.
    """
    repository = _accounts(tmp_path)
    stored = repository.latest()
    assert stored is not None
    replaced = stored.model_copy(update=dict(overrides))
    # A different content means a different id; the store is immutable and
    # would refuse a second record under the first one's identity.
    replaced = replaced.model_copy(update={"snapshot_id": f"{stored.snapshot_id}-restated"})
    repository.save(replaced)
    return replaced


# ---------------------------------------------------------------------------
# Case 1 and Case 4: a EUR account trading USD options
# ---------------------------------------------------------------------------
def test_case_1_a_eur_account_funds_a_usd_campaign(workflow) -> None:  # noqa: F811
    """The mismatch is expected, and a conversion path resolves it.

    This is the case that used to end ``CURRENCY_MISMATCH`` on every candidate,
    forever, because the only alternative on offer was declaring a dollar equal
    to a euro. Neither happens now: the capital stays EUR, the campaign trades
    USD, and an explicit rate connects them.
    """
    _, _, _, run = _run_everything(workflow)
    result = run.result

    assert result.status is AllocationRunStatus.SUCCESS
    [allocation] = result.allocations
    assert allocation.outcome is AllocationOutcome.APPROVED
    assert RiskReasonCode.CURRENCY_MISMATCH not in allocation.reason_codes
    assert RiskReasonCode.FX_RATE_UNAVAILABLE not in allocation.reason_codes


def test_case_4_the_account_and_the_instrument_need_not_agree(workflow, tmp_path) -> None:  # noqa: F811
    """``account_currency == instrument_currency`` is never required.

    What is required is a path between them, and the path runs through the
    campaign's target currency rather than directly.
    """
    _, _, _, run = _run_everything(workflow)
    [allocation] = run.result.allocations
    account = _accounts(tmp_path).latest()

    assert account is not None
    assert account.currency == "EUR"
    assert allocation.currency == "USD"
    assert run.result.currency == "USD"


def test_the_declared_envelope_survives_the_conversion(workflow) -> None:  # noqa: F811
    """5,000 EUR is still 5,000 EUR in the record, and still labelled EUR.

    The converted figure is what a position is sized against; the declared one
    is what the operator actually holds. Replacing the second with the first
    would make "how much of my own money is committed" unanswerable.
    """
    _, _, _, run = _run_everything(workflow)
    result = run.result

    assert result.declared_budget == Decimal("5000")
    assert result.declared_currency == "EUR"
    assert result.budget != result.declared_budget
    assert result.fx is not None and result.fx.from_currency == "EUR"
    assert result.fx.to_currency == "USD"


def test_the_rate_comes_from_the_broker_read_that_captured_the_balance(
    workflow,  # noqa: F811
    tmp_path,
) -> None:
    """One observation, so a balance can never be converted at another moment's rate."""
    _run_everything(workflow)
    account = _accounts(tmp_path).latest()

    assert account is not None
    assert account.fx_rates.rates, "the capture stored the broker's own rates"
    for rate in account.fx_rates.rates:
        assert rate.as_of == account.as_of
        assert rate.origin is FxRateOrigin.SIMULATED
    # And the cash ledger is recorded per currency, unconverted and unsummed.
    assert set(account.cash_by_currency) == {"EUR", "USD"}


# ---------------------------------------------------------------------------
# Case 2 and Case 6: no rate means no trade, and no parity
# ---------------------------------------------------------------------------
def test_case_2_no_rate_authorises_nothing(workflow, tmp_path) -> None:  # noqa: F811
    """The account is funded, the contract is fine, and nothing is authorised."""
    research, strategy, contract, allocation = workflow
    strategy_run = strategy.run(research_run_id=research.run_id, as_of=NOW)
    contract_run = contract.select(strategy_run_id=strategy_run.result.run_id)
    _restate_account(tmp_path, fx_rates=FxRateTable())

    run = allocation.run(contract_run_id=contract_run.result.run_id)

    assert run.result.status is AllocationRunStatus.NO_ALLOCATION
    [rejected] = run.result.allocations
    assert rejected.outcome is not AllocationOutcome.APPROVED
    assert RiskReasonCode.FX_RATE_UNAVAILABLE in rejected.reason_codes


def test_case_6_no_rate_computes_no_position_size(workflow, tmp_path) -> None:  # noqa: F811
    """Not "quantity zero after sizing" — no sizing happens at all.

    This is the comparison the brief singles out: dividing a EUR balance by a
    USD unit cost would authorise as many contracts as an equal number of
    dollars does, which is wrong by the exchange rate and looks like an
    ordinary answer.
    """
    research, strategy, contract, allocation = workflow
    strategy_run = strategy.run(research_run_id=research.run_id, as_of=NOW)
    contract_run = contract.select(strategy_run_id=strategy_run.result.run_id)
    _restate_account(tmp_path, fx_rates=FxRateTable())

    run = allocation.run(contract_run_id=contract_run.result.run_id)
    [rejected] = run.result.allocations

    assert rejected.quantity == 0
    assert rejected.capital_committed == Decimal("0")
    assert rejected.calculation is None, "no ceiling was computed against an unconverted figure"


def test_a_stale_rate_is_refused_by_its_own_name(workflow, tmp_path, system_config) -> None:  # noqa: F811
    research, strategy, contract, allocation = workflow
    strategy_run = strategy.run(research_run_id=research.run_id, as_of=NOW)
    contract_run = contract.select(strategy_run_id=strategy_run.result.run_id)

    window = system_config.campaign.currency_policy.max_rate_age_seconds
    old = NOW - timedelta(seconds=window + 1)
    stale = FxRateTable(
        rates=(
            FxRate(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.10"),
                as_of=old,
                origin=FxRateOrigin.CONFIGURED,
                source="TEST_FIXTURE",
            ),
        )
    )
    _restate_account(tmp_path, fx_rates=stale)

    run = allocation.run(contract_run_id=contract_run.result.run_id)
    [rejected] = run.result.allocations

    assert RiskReasonCode.FX_RATE_STALE in rejected.reason_codes
    assert RiskReasonCode.FX_RATE_UNAVAILABLE not in rejected.reason_codes


# ---------------------------------------------------------------------------
# Case 3: an explicitly injected test rate, and the arithmetic it produces
# ---------------------------------------------------------------------------
def test_case_3_an_injected_rate_produces_the_stated_arithmetic(system_config) -> None:
    """EURUSD 1.10, EUR 5,000 -> USD 5,500, through the real resolution.

    The rate is supplied by the test rather than defaulted, which is the brief's
    requirement for deterministic tests: a fixed rate is stated, never assumed,
    and 1.0 is never encoded as production behaviour anywhere.
    """
    rates = FxRateTable(
        rates=(
            FxRate(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.10"),
                as_of=NOW,
                origin=FxRateOrigin.CONFIGURED,
                source="TEST_FIXTURE",
            ),
        )
    )

    limits = resolve_limits(system_config, fx_rates=rates, as_of=NOW)

    assert limits.fx is not None and limits.fx.status is FxStatus.VALID
    assert limits.campaign_budget == Decimal("5500.00")
    assert limits.declared["campaign_budget"] == Decimal("5000")


# ---------------------------------------------------------------------------
# Case 5: the paper account follows the same model
# ---------------------------------------------------------------------------
def test_case_5_the_paper_account_uses_the_same_currency_semantics(broker) -> None:  # noqa: F811
    """No 1:1 shortcut for paper, and no separate code path for it.

    The simulated broker reports a EUR base, a per-currency cash ledger and a
    deliberately non-unity rate — the same shape a real account reports, so the
    offline path exercises the conversion rather than a tidier version of it.
    """
    account = broker.get_account()

    assert account.currency == "EUR"
    assert account.cash_by_currency["USD"] == Decimal("0.00")
    assert account.exchange_rates, "a paper account still reports rates"
    assert all(rate != Decimal(1) for rate in account.exchange_rates.values()), (
        "a simulator quoting parity would let every cross-currency defect pass "
        "the whole suite and fail only against a real account"
    )

    snapshot = build_account_snapshot(
        account,
        broker.get_positions(),
        broker=broker.name,
        trading_mode=TradingMode.PAPER,
        captured_at=NOW,
        simulated=True,
    )
    conversion = snapshot.spendable_in("USD", as_of=NOW, max_rate_age_seconds=86400)

    assert conversion is not None and conversion.ok
    assert conversion.converted_amount != snapshot.spendable


# ---------------------------------------------------------------------------
# Nothing here can place an order
# ---------------------------------------------------------------------------
def test_the_whole_currency_flow_submits_no_orders(workflow, broker) -> None:  # noqa: F811
    _run_everything(workflow)

    assert broker.orders_submitted == 0
