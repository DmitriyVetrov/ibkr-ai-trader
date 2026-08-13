"""Broker holdings, orders and fills with no internal history (brief sections 58, 87-89).

The first reconciliation of a real account that traded before this system
existed is *expected* to report several orphans. That is not a failure, and the
only correct response to one is to report it:

* nothing is sold, hedged or closed;
* nothing is adopted into the internal ledger;
* no allocation, execution, strategy, thesis or purchase date is invented for
  it, and its acquisition provenance stays ``UNKNOWN``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.positions.factories import (
    ACCOUNT,
    broker_execution,
    broker_order,
    option_position,
    stock_position,
)
from trading_system.broker.simulator import SimulatedBrokerState
from trading_system.domain.enums import (
    AcquisitionProvenance,
    ReconciliationFindingType,
    ReconciliationSeverity,
)
from trading_system.infrastructure.settings import PositionsConfig, ReconciliationConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def legacy_account() -> SimulatedBrokerState:
    """An account holding four positions from before this system existed."""
    return SimulatedBrokerState(
        account_id=ACCOUNT,
        currency="EUR",
        positions=[
            option_position(contract_id=1, strike=Decimal("180.00")),
            option_position(contract_id=2, strike=Decimal("190.00")),
            option_position(contract_id=3, strike=Decimal("200.00")),
            stock_position(),
        ],
        open_orders=[broker_order(broker_order_id="legacy-order")],
        executions=[broker_execution(execution_id="legacy-fill", broker_order_id="legacy-order")],
    )


def test_a_pre_existing_account_reports_every_holding_as_an_orphan(
    make_service, legacy_account
) -> None:
    service = make_service(legacy_account)
    run = service.run()

    orphans = run.result.by_type(ReconciliationFindingType.ORPHAN_BROKER_POSITION)
    assert len(orphans) == 4
    assert run.result.counts.orphan_positions == 4


def test_an_orphan_position_keeps_unknown_acquisition_provenance(
    make_service, legacy_account
) -> None:
    service = make_service(legacy_account)
    run = service.run()

    assert all(
        position.provenance is AcquisitionProvenance.UNKNOWN
        for position in run.capture.snapshot.positions
    )


def test_no_internal_position_is_manufactured_to_make_it_match(
    make_service, legacy_account
) -> None:
    """The instruction the brief gives in as many words: do NOT manufacture."""
    service = make_service(legacy_account)
    run = service.run()

    assert run.projection.positions == ()
    assert run.result.expected_position_count == 0
    assert run.result.matched is False


def test_an_orphan_order_is_reported_and_not_cancelled(make_service, legacy_account) -> None:
    service = make_service(legacy_account)
    run = service.run()

    [finding] = run.result.by_type(ReconciliationFindingType.ORPHAN_BROKER_ORDER)
    assert finding.broker_order_id == "legacy-order"
    assert run.orders_submitted == 0
    assert run.corrective_orders == 0


def test_an_orphan_fill_is_reported_and_explains_nothing_away(make_service, legacy_account) -> None:
    service = make_service(legacy_account)
    run = service.run()

    [finding] = run.result.by_type(ReconciliationFindingType.ORPHAN_BROKER_FILL)
    assert finding.expected_value is None
    assert "no internal execution accounts for" in finding.summary


def test_orphans_are_never_assigned_to_a_campaign(make_service, legacy_account) -> None:
    service = make_service(legacy_account)
    run = service.run()

    for finding in run.result.by_type(ReconciliationFindingType.ORPHAN_BROKER_POSITION):
        assert finding.allocation_id is None
        assert finding.opportunity_id is None
        assert finding.execution_id is None


def test_reconciling_a_legacy_account_submits_no_orders(make_service, legacy_account) -> None:
    service = make_service(legacy_account)
    run = service.run()

    assert run.orders_submitted == 0
    assert service.broker.orders_submitted == 0
    assert run.result.orders_submitted == 0


def test_automatic_adoption_fails_to_load_in_configuration() -> None:
    """A named refusal, so its absence is visible rather than merely absent."""
    with pytest.raises(ValidationError, match="never absorbed"):
        ReconciliationConfig(auto_adopt_orphan_positions=True, severity=_full_severity())


def test_automatic_adoption_fails_to_load_in_the_position_policy() -> None:
    with pytest.raises(ValidationError, match="inventing an allocation"):
        PositionsConfig(adopt_orphan_positions=True)


def test_corrective_trading_fails_to_load_in_configuration() -> None:
    with pytest.raises(ValidationError, match="would make"):
        ReconciliationConfig(corrective_orders_permitted=True, severity=_full_severity())


def _full_severity() -> dict[ReconciliationFindingType, ReconciliationSeverity]:
    return {finding: ReconciliationSeverity.INFO for finding in ReconciliationFindingType}
