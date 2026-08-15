"""The artifacts: what they refuse to say, and that they are never rewritten.

The most important tests in this file are the ones that assert a record
*cannot be constructed*. "This system does not adopt the position" is a
property of the model here, not a promise made in a docstring — an operator
cannot point a cleanup at a real allocation, and no future refactor can quietly
default the field to something that parses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.cleanup.conftest import (
    NOW,
    ORPHAN_CALL_ID,
    ORPHAN_CALL_KEY,
    orphan_position,
    target_from,
)
from tests.positions.factories import MASKED, versions
from trading_system.cleanup.models import (
    CleanupOutcome,
    CleanupOutcomeStatus,
    CleanupRunStatus,
    OrphanCleanupRequest,
    OrphanCleanupRun,
    cleanup_request_identifier,
    cleanup_run_identifier,
)
from trading_system.cleanup.store import CleanupStoreError, FilesystemCleanupRepository
from trading_system.domain.enums import (
    ExecutionIntent,
    ExecutionState,
    LegAction,
    OptionRight,
    OrderType,
    TimeInForce,
    TradingMode,
)
from trading_system.execution.models import ExecutionLeg, ExecutionRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The execution record refuses to adopt
# ---------------------------------------------------------------------------
def _cleanup_record(**overrides: object) -> ExecutionRecord:
    payload: dict[str, object] = {
        "execution_id": "execution-cleanup-1",
        "execution_request_id": "clean-req-1",
        "order_intent_id": "intent-cleanup-1",
        "campaign_id": "campaign-001",
        "created_at": NOW,
        "updated_at": NOW,
        "underlying": "SMH",
        "intent": ExecutionIntent.CLEANUP,
        "legs": [
            ExecutionLeg(
                leg_index=0,
                contract_id=ORPHAN_CALL_ID,
                action=LegAction.SELL,
                right=OptionRight.CALL,
                underlying="SMH",
                expiration=date(2026, 8, 21),
                strike=Decimal("540.00"),
                multiplier=100,
            )
        ],
        "quantity": 1,
        "multiplier": 100,
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.DAY,
        "trading_mode": TradingMode.PAPER,
        "broker": "SIMULATOR",
        "state": ExecutionState.VALIDATED,
        "cleanup_request_id": "cleanup-req-1",
        "broker_position_key": ORPHAN_CALL_KEY,
        "policy_version": "test",
        "versions": versions(),
    }
    return ExecutionRecord(**(payload | overrides))


def test_a_cleanup_record_can_be_built_without_any_authorisation() -> None:
    record = _cleanup_record()

    assert record.intent is ExecutionIntent.CLEANUP
    assert record.allocation_id is None
    assert record.purchase_card_id is None
    assert record.risk_decision_id is None
    assert record.opportunity_id is None
    assert record.strategy is None


@pytest.mark.parametrize(
    "field",
    ["allocation_id", "purchase_card_id", "risk_decision_id", "opportunity_id"],
)
def test_cleanup_does_not_adopt_position(field: str) -> None:
    """The rule made structural: it cannot borrow an authorisation."""
    with pytest.raises(ValueError, match="did not open the holding"):
        _cleanup_record(**{field: "alloc-real-001"})


def test_a_cleanup_cannot_claim_a_strategy() -> None:
    from trading_system.domain.enums import StrategyType

    with pytest.raises(ValueError, match="did not open the holding"):
        _cleanup_record(strategy=StrategyType.LONG_CALL)


def test_a_cleanup_must_name_its_authorising_request() -> None:
    with pytest.raises(ValueError, match="must name the cleanup_request_id"):
        _cleanup_record(cleanup_request_id=None)


def test_a_cleanup_must_name_the_broker_contract_it_targets() -> None:
    with pytest.raises(ValueError, match="A symbol is not an identity"):
        _cleanup_record(broker_position_key=None)


def test_cleanup_does_not_mutate_campaign_budget() -> None:
    with pytest.raises(ValueError, match="charge the campaign budget"):
        _cleanup_record(capital_commitment=Decimal("1210.00"))


def test_a_cleanup_removes_no_campaign_risk() -> None:
    with pytest.raises(ValueError, match="never the campaign's to carry"):
        _cleanup_record(maximum_loss=Decimal("1210.00"))


def test_a_cleanup_carries_no_exit_decision() -> None:
    with pytest.raises(ValueError, match="three different acts"):
        _cleanup_record(exit_decision_id="exit-decision-1", position_id="strategypos-1")


def test_an_ordinary_execution_still_requires_its_authorisation() -> None:
    """The regression: the conditional must not weaken the ordinary shape."""
    with pytest.raises(ValueError, match="must carry"):
        _cleanup_record(
            intent=ExecutionIntent.OPEN, cleanup_request_id=None, broker_position_key=None
        )


def test_an_ordinary_execution_cannot_carry_cleanup_provenance() -> None:
    from trading_system.domain.enums import StrategyType

    with pytest.raises(ValueError, match="carries orphan-cleanup provenance"):
        _cleanup_record(
            intent=ExecutionIntent.OPEN,
            allocation_id="alloc-1",
            purchase_card_id="card-1",
            risk_decision_id="risk-1",
            opportunity_id="opp-1",
            strategy=StrategyType.LONG_CALL,
        )


def test_a_cleanup_leg_may_honestly_lack_a_trading_class() -> None:
    assert _cleanup_record().legs[0].trading_class is None


def test_an_authorised_leg_may_not_lack_a_trading_class() -> None:
    from trading_system.domain.enums import StrategyType

    with pytest.raises(ValueError, match="no trading class"):
        _cleanup_record(
            intent=ExecutionIntent.OPEN,
            allocation_id="alloc-1",
            purchase_card_id="card-1",
            risk_decision_id="risk-1",
            opportunity_id="opp-1",
            strategy=StrategyType.LONG_CALL,
            cleanup_request_id=None,
            broker_position_key=None,
        )


def test_a_cleanup_is_excluded_from_the_reservation_ledger() -> None:
    assert ExecutionIntent.CLEANUP.establishes_position is False
    assert ExecutionIntent.CLEANUP.carries_an_authorisation is False


def test_a_cleanup_is_excluded_from_the_expected_position_ledger() -> None:
    assert ExecutionIntent.CLEANUP.adjusts_expected_positions is False
    assert ExecutionIntent.OPEN.adjusts_expected_positions is True
    assert ExecutionIntent.CLOSE.adjusts_expected_positions is True


# ---------------------------------------------------------------------------
# The request refuses "sell everything"
# ---------------------------------------------------------------------------
def _request(targets=None, **overrides: object) -> OrphanCleanupRequest:
    payload: dict[str, object] = {
        "cleanup_request_id": "cleanup-req-1",
        "source_reconciliation_id": "reconciliation-1",
        "account_reference": MASKED,
        "campaign_id": "campaign-001",
        "requested_at": NOW,
        "targets": targets if targets is not None else [target_from(orphan_position())],
        "cleanup_authorized": True,
        "trading_mode": TradingMode.PAPER,
        "policy_version": "test",
        "versions": versions(),
    }
    return OrphanCleanupRequest(**(payload | overrides))


def test_an_unauthorised_request_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="cleanup_authorized=True"):
        _request(cleanup_authorized=False)


def test_a_request_with_no_targets_cannot_be_constructed() -> None:
    """There is no shape meaning "everything"; the model is a list of contracts."""
    with pytest.raises(ValueError):
        _request(targets=[])


def test_a_request_cannot_name_the_same_holding_twice() -> None:
    target = target_from(orphan_position())
    with pytest.raises(ValueError, match="more than once"):
        _request(targets=[target, target])


def test_a_request_carries_exactly_one_reason() -> None:
    with pytest.raises(ValueError, match="second policy nobody wrote down"):
        _request(reason="TIDYING_UP")


def test_the_request_id_ignores_target_order() -> None:
    keys = ["cid:2", "cid:1"]
    forward = cleanup_request_identifier(
        account_reference=MASKED,
        reconciliation_id="reconciliation-1",
        contract_keys=keys,
        trading_mode=TradingMode.PAPER,
        policy_version="test",
    )
    reversed_ = cleanup_request_identifier(
        account_reference=MASKED,
        reconciliation_id="reconciliation-1",
        contract_keys=list(reversed(keys)),
        trading_mode=TradingMode.PAPER,
        policy_version="test",
    )
    assert forward == reversed_


def test_the_request_id_changes_with_the_reconciliation_that_identified_them() -> None:
    """The operator reviewed a specific report; a different one is a different act."""

    def identity(reconciliation_id: str) -> str:
        return cleanup_request_identifier(
            account_reference=MASKED,
            reconciliation_id=reconciliation_id,
            contract_keys=["cid:1"],
            trading_mode=TradingMode.PAPER,
            policy_version="test",
        )

    assert identity("reconciliation-a") != identity("reconciliation-b")


# ---------------------------------------------------------------------------
# The run refuses to overstate what happened
# ---------------------------------------------------------------------------
def _outcome(**overrides: object) -> CleanupOutcome:
    payload: dict[str, object] = {
        "key": ORPHAN_CALL_KEY,
        "contract_id": ORPHAN_CALL_ID,
        "symbol": "SMH",
        "describe": "SMH 2026-08-21 540.0 CALL",
        "status": CleanupOutcomeStatus.WORKING,
        "observed_quantity_before": Decimal("1"),
        "requested_quantity": 1,
    }
    return CleanupOutcome(**(payload | overrides))


def test_closed_requires_a_broker_observation_afterwards() -> None:
    """A fill report is a claim about an order, not about the account."""
    with pytest.raises(ValueError, match="without a broker observation"):
        _outcome(status=CleanupOutcomeStatus.CLOSED, filled_quantity=1)


def test_closed_requires_the_broker_to_report_none_of_it() -> None:
    with pytest.raises(ValueError, match="still holds"):
        _outcome(
            status=CleanupOutcomeStatus.CLOSED,
            filled_quantity=1,
            observed_quantity_after=Decimal("1"),
        )


def test_already_closed_cannot_have_submitted_an_order() -> None:
    with pytest.raises(ValueError, match="contradictory claims"):
        _outcome(status=CleanupOutcomeStatus.ALREADY_CLOSED, orders_submitted=1)


def test_a_refusal_cannot_have_reached_the_broker() -> None:
    """REFUSED is "nothing left this process", and it is not a spelling of
    "the broker turned it down" — that is REJECTED, and it counts an attempt."""
    with pytest.raises(ValueError, match="nothing left this process"):
        _outcome(
            status=CleanupOutcomeStatus.REFUSED,
            orders_submitted=1,
            gate_failures=["FAIL X: y"],
        )


def test_a_broker_rejection_may_count_an_attempt() -> None:
    rejected = _outcome(
        status=CleanupOutcomeStatus.REJECTED,
        orders_submitted=1,
        broker_order_id="sim-000001",
        detail="the broker refused the order",
    )
    assert rejected.orders_submitted == 1


def test_a_refusal_names_a_reason() -> None:
    with pytest.raises(ValueError, match="names no reason code"):
        _outcome(status=CleanupOutcomeStatus.REFUSED)


def _run(**overrides: object) -> OrphanCleanupRun:
    payload: dict[str, object] = {
        "run_id": "cleanuprun-1",
        "cleanup_request_id": "cleanup-req-1",
        "source_reconciliation_id": "reconciliation-1",
        "account_reference": MASKED,
        "campaign_id": "campaign-001",
        "as_of": NOW,
        "generated_at": NOW,
        "status": CleanupRunStatus.DRY_RUN,
        "trading_mode": TradingMode.PAPER,
        "dry_run": True,
        "broker": "SIMULATOR",
        "policy_version": "test",
        "versions": versions(),
    }
    return OrphanCleanupRun(**(payload | overrides))


def test_a_dry_run_cannot_report_a_submitted_order() -> None:
    with pytest.raises(ValueError, match="must submit no orders"):
        _run(orders_submitted=1)


def test_a_dry_run_outcome_cannot_carry_a_broker_order_id() -> None:
    with pytest.raises(ValueError, match="cannot carry a broker order id"):
        _run(outcomes=[_outcome(broker_order_id="sim-000001")])


def test_a_run_never_places_a_corrective_order() -> None:
    with pytest.raises(ValueError, match="never hedges"):
        _run(corrective_orders=1)


def test_the_run_count_must_reconcile_with_its_outcomes() -> None:
    """One of the two would otherwise be wrong about a real order."""
    with pytest.raises(ValueError, match="account for"):
        _run(dry_run=False, status=CleanupRunStatus.PARTIAL, orders_submitted=2, outcomes=[])


def test_the_run_id_includes_what_the_run_concluded() -> None:
    """The lesson this repository has now learned five times."""
    submitted = cleanup_run_identifier(
        request_id="cleanup-req-1",
        as_of=NOW,
        outcomes=[f"{ORPHAN_CALL_KEY}:CLOSED"],
        dry_run=False,
    )
    already = cleanup_run_identifier(
        request_id="cleanup-req-1",
        as_of=NOW,
        outcomes=[f"{ORPHAN_CALL_KEY}:ALREADY_CLOSED"],
        dry_run=False,
    )
    assert submitted != already


# ---------------------------------------------------------------------------
# Immutability on disk
# ---------------------------------------------------------------------------
def test_cleanup_record_is_immutable(tmp_path) -> None:
    repository = FilesystemCleanupRepository(tmp_path / "cleanup")
    run = _run(dry_run=False, status=CleanupRunStatus.COMPLETE)

    _, first = repository.save_run(run)
    assert first is True

    # Same evidence, same conclusion, later look: a re-observation, not a rewrite.
    _, second = repository.save_run(run.model_copy(update={"generated_at": NOW}))
    assert second is False
    reobserved = [entry.reobserved for entry in repository.history()]
    assert sorted(reobserved) == [False, True], "the second look is recorded, not the record"

    # A different conclusion under the same id is a contradiction, and raises.
    with pytest.raises(CleanupStoreError, match="immutable"):
        repository.save_run(run.model_copy(update={"status": CleanupRunStatus.PARTIAL}))


def test_a_stored_run_reads_back_identically(tmp_path) -> None:
    repository = FilesystemCleanupRepository(tmp_path / "cleanup")
    run = _run(dry_run=False, status=CleanupRunStatus.COMPLETE)
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == run.model_dump(mode="json")


def test_a_stored_request_is_immutable(tmp_path) -> None:
    repository = FilesystemCleanupRepository(tmp_path / "cleanup")
    request = _request()
    _, first = repository.save_request(request)
    assert first is True
    _, second = repository.save_request(request)
    assert second is False

    with pytest.raises(CleanupStoreError, match="immutable"):
        repository.save_request(request.model_copy(update={"requested_by": "somebody-else"}))
