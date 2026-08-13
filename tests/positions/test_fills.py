"""Recording broker fills (brief sections 9-11, 74).

The claims under test:

* a fill comes from a fill report and from nothing else;
* observing the same fill twice records one fill and one re-observation;
* an absent commission stays ``None``;
* an option fill that cannot be identified is refused rather than merged into
  the wrong strike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.positions.factories import (
    CALL_CONTRACT_ID,
    EXPIRATION,
    MASKED,
    NOW,
    broker_execution,
    execution_leg,
)
from trading_system.domain.enums import OptionRight, OrderSide, SecurityType
from trading_system.positions.fills import (
    ContractTerms,
    FillTranslationError,
    aggregate_by_contract,
    deduplicate_fills,
    new_fills,
    terms_from_legs,
    to_observed_fill,
    to_observed_fills,
)

pytestmark = pytest.mark.unit

TERMS = ContractTerms(
    expiration=EXPIRATION,
    strike=Decimal("180.00"),
    right=OptionRight.CALL,
    multiplier=100,
    trading_class="NVDA",
)


def test_one_fill_records_exactly_what_the_broker_said() -> None:
    fill = to_observed_fill(
        broker_execution(), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    assert fill.side is OrderSide.BUY
    assert fill.quantity == Decimal("2")
    assert fill.price == Decimal("5.95")
    assert fill.commission == Decimal("1.30")
    assert fill.broker_execution_id == "exec-1"
    assert fill.executed_at == NOW


def test_a_missing_commission_stays_none() -> None:
    """IBKR routinely reports a fill before its commission report arrives."""
    fill = to_observed_fill(
        broker_execution(commission=None),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
    )
    assert fill.commission is None


def test_the_brokers_execution_id_is_the_identity() -> None:
    first = to_observed_fill(
        broker_execution(), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    # Same trade, seen a minute later through a different poll.
    later = to_observed_fill(
        broker_execution(),
        observed_at=datetime(2026, 8, 10, 14, 31, tzinfo=UTC),
        account_reference=MASKED,
        terms=TERMS,
    )
    assert first.fill_id == later.fill_id
    assert first.identity_from_broker is True


def test_observing_the_same_fill_twice_records_one_fill() -> None:
    fill = to_observed_fill(
        broker_execution(), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    unique, repeats = deduplicate_fills([fill, fill, fill])
    assert len(unique) == 1
    assert len(repeats) == 2


def test_two_genuinely_different_fills_are_two_fills() -> None:
    first = to_observed_fill(
        broker_execution(execution_id="exec-1", quantity=Decimal("1")),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
    )
    second = to_observed_fill(
        broker_execution(execution_id="exec-2", quantity=Decimal("1")),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
    )
    unique, repeats = deduplicate_fills([first, second])
    assert len(unique) == 2
    assert repeats == []


def test_a_fill_without_a_broker_execution_id_uses_a_derived_identity_and_says_so() -> None:
    execution = broker_execution(execution_id=" ")
    fill = to_observed_fill(execution, observed_at=NOW, account_reference=MASKED, terms=TERMS)
    assert fill.identity_from_broker is False
    assert fill.broker_execution_id is None


def test_a_derived_identity_still_deduplicates_the_same_trade() -> None:
    """Weaker, but not useless: the same authoritative fields give the same id."""
    first = to_observed_fill(
        broker_execution(execution_id=" "), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    second = to_observed_fill(
        broker_execution(execution_id=" "),
        observed_at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
        account_reference=MASKED,
        terms=TERMS,
    )
    assert first.fill_id == second.fill_id


def test_an_unidentifiable_option_fill_is_refused_rather_than_merged() -> None:
    """Without a contract id or terms, every NVDA strike would key the same."""
    with pytest.raises(FillTranslationError, match="merge unrelated strikes"):
        to_observed_fill(
            broker_execution(contract_id=None),
            observed_at=NOW,
            account_reference=MASKED,
            terms=None,
        )


def test_a_stock_fill_without_a_contract_id_is_still_recordable() -> None:
    """The refusal is about option strikes, not about identity in general."""
    fill = to_observed_fill(
        broker_execution(contract_id=None, security_type=SecurityType.STOCK, symbol="SPY"),
        observed_at=NOW,
        account_reference=MASKED,
    )
    assert fill.key.startswith("sym:")


def test_one_refused_fill_does_not_lose_the_others() -> None:
    fills, refused = to_observed_fills(
        [
            broker_execution(execution_id="exec-1"),
            broker_execution(execution_id="exec-2", contract_id=None),
        ],
        observed_at=NOW,
        account_reference=MASKED,
        terms_by_contract={CALL_CONTRACT_ID: TERMS},
    )
    assert [fill.broker_execution_id for fill in fills] == ["exec-1"]
    assert len(refused) == 1


def test_contract_terms_are_indexed_from_execution_legs() -> None:
    terms = terms_from_legs([execution_leg()])
    assert terms[CALL_CONTRACT_ID].strike == Decimal("180.00")
    assert terms[CALL_CONTRACT_ID].right is OptionRight.CALL
    assert terms[CALL_CONTRACT_ID].multiplier == 100


def test_a_fill_is_linked_to_our_execution_through_the_broker_order_id() -> None:
    fills, _ = to_observed_fills(
        [broker_execution(broker_order_id="ord-9")],
        observed_at=NOW,
        account_reference=MASKED,
        terms_by_contract={CALL_CONTRACT_ID: TERMS},
        execution_by_order={"ord-9": "execution-1"},
        allocation_by_order={"ord-9": "allocation-1"},
    )
    assert fills[0].execution_id == "execution-1"
    assert fills[0].allocation_id == "allocation-1"
    assert fills[0].provenance.value == "SYSTEM_EXECUTION"


def test_a_fill_no_execution_of_ours_explains_keeps_unknown_provenance() -> None:
    fills, _ = to_observed_fills(
        [broker_execution(broker_order_id="someone-elses-order")],
        observed_at=NOW,
        account_reference=MASKED,
        terms_by_contract={CALL_CONTRACT_ID: TERMS},
    )
    assert fills[0].execution_id is None
    assert fills[0].provenance.value == "UNKNOWN"


def test_known_fills_are_recognised_as_re_observations() -> None:
    fill = to_observed_fill(
        broker_execution(), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    fresh, repeats = new_fills([fill], {fill.fill_id})
    assert fresh == []
    assert repeats == [fill]


def test_buys_add_and_sells_subtract() -> None:
    """Explicit arithmetic. Nothing infers direction from a strategy name."""
    buy = to_observed_fill(
        broker_execution(execution_id="e1", quantity=Decimal("3")),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
    )
    sell = to_observed_fill(
        broker_execution(execution_id="e2", side=OrderSide.SELL, quantity=Decimal("1")),
        observed_at=NOW,
        account_reference=MASKED,
        terms=TERMS,
    )
    assert aggregate_by_contract([buy, sell]) == {buy.key: Decimal("2")}


def test_timestamps_are_utc_aware() -> None:
    fill = to_observed_fill(
        broker_execution(), observed_at=NOW, account_reference=MASKED, terms=TERMS
    )
    assert fill.executed_at.tzinfo is not None
    assert fill.observed_at.tzinfo is not None
    assert fill.executed_at.utcoffset() == UTC.utcoffset(None)
