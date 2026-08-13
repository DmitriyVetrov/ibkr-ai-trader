"""Comparing expected positions against broker reality (brief sections 29, 34, 58-62).

The four outcomes, and the two that matter most:

* ``EXPECTED_POSITION_MISSING`` — every downstream risk figure is computed from
  a holding that does not exist;
* ``ORPHAN_BROKER_POSITION`` — the broker holds something real that no
  execution of ours explains, and nothing here sells it, adopts it or assigns
  it to a campaign.

Every finding must show both sides, the difference, both provenances and both
clocks. "Positions differ" is not something anyone can act on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.positions.factories import (
    CALL_KEY,
    EXPIRATION,
    MASKED,
    NOW,
    option_position,
    stock_position,
)
from trading_system.domain.enums import (
    AcquisitionProvenance,
    OptionRight,
    ReconciliationFindingType,
    ReconciliationSeverity,
    SecurityType,
    StructureStatus,
)
from trading_system.positions.models import (
    ExpectedPosition,
    StrategyLegPosition,
    StrategyPosition,
    position_identifier,
)
from trading_system.reconciliation.positions import compare_positions, compare_structures

pytestmark = pytest.mark.unit


def _expected(
    *,
    key: str = CALL_KEY,
    quantity: Decimal = Decimal("2"),
    contract_id: int | None = 100001,
    executions: list[str] | None = None,
) -> ExpectedPosition:
    return ExpectedPosition(
        position_id=position_identifier(account_reference=MASKED, key=key),
        account_reference=MASKED,
        key=key,
        as_of=NOW,
        underlying="NVDA",
        asset_class=SecurityType.OPTION,
        symbol="NVDA",
        contract_id=contract_id,
        expiration=EXPIRATION,
        strike=Decimal("180.00"),
        right=OptionRight.CALL,
        multiplier=100,
        currency="EUR",
        quantity=quantity,
        bought_quantity=max(quantity, Decimal("0")),
        sold_quantity=max(-quantity, Decimal("0")),
        fill_ids=["fill-1"] if quantity else [],
        execution_ids=executions if executions is not None else ["execution-1"],
        provenance=AcquisitionProvenance.SYSTEM_EXECUTION,
    )


def _severity(finding: ReconciliationFindingType) -> ReconciliationSeverity:
    return ReconciliationSeverity.CRITICAL


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def test_a_perfect_match_is_recorded_as_agreement(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[_expected()],
        snapshot=snapshot_of([option_position()]),
        severity=policy.severity_of,
    )
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.POSITION_MATCH
    assert finding.agreement is True
    assert finding.delta == "0"


def test_an_empty_account_that_expects_nothing_is_agreement(snapshot_of, policy) -> None:
    findings = compare_positions(expected=[], snapshot=snapshot_of([]), severity=policy.severity_of)
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.BROKER_RETURNED_EMPTY
    assert finding.agreement is True


# ---------------------------------------------------------------------------
# Quantity mismatch (brief section 60)
# ---------------------------------------------------------------------------
def test_a_quantity_mismatch_reports_both_sides_and_the_difference(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[_expected(quantity=Decimal("10"))],
        snapshot=snapshot_of([option_position(quantity=Decimal("7"))]),
        severity=policy.severity_of,
    )
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.POSITION_QUANTITY_MISMATCH
    assert finding.expected_value == "10"
    assert finding.observed_value == "7"
    assert finding.delta == "-3"


def test_a_quantity_mismatch_names_the_contract_and_both_provenances(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[_expected(quantity=Decimal("10"))],
        snapshot=snapshot_of([option_position(quantity=Decimal("7"))]),
        severity=policy.severity_of,
    )
    [finding] = findings
    assert finding.identifier == CALL_KEY
    assert finding.contract_id == 100001
    assert finding.expected_provenance == "executions execution-1"
    assert finding.broker_provenance == "SIMULATOR"
    assert finding.observed_at is not None
    assert finding.broker_timestamp is not None


def test_a_mismatch_does_not_assume_the_difference_was_cancelled(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[_expected(quantity=Decimal("10"))],
        snapshot=snapshot_of([option_position(quantity=Decimal("7"))]),
        severity=policy.severity_of,
    )
    [finding] = findings
    assert "authoritative" in (finding.detail or "")
    assert "ACTION REQUIRED" in (finding.recommended_action or "")


# ---------------------------------------------------------------------------
# Missing and orphan (brief sections 58-59)
# ---------------------------------------------------------------------------
def test_a_position_we_expect_that_the_broker_does_not_hold_is_reported(
    snapshot_of, policy
) -> None:
    findings = compare_positions(
        expected=[_expected()], snapshot=snapshot_of([]), severity=policy.severity_of
    )
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.EXPECTED_POSITION_MISSING
    assert finding.expected_value == "2"
    assert finding.observed_value == "0"
    assert finding.delta == "-2"


def test_a_missing_position_never_proposes_a_replacement_order(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[_expected()], snapshot=snapshot_of([]), severity=policy.severity_of
    )
    [finding] = findings
    action = (finding.recommended_action or "").lower()
    assert "no replacement order" in action
    assert "buy" not in action.replace("buying", "")
    assert "sell" not in action


def test_a_broker_position_no_execution_explains_is_an_orphan(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[], snapshot=snapshot_of([stock_position()]), severity=policy.severity_of
    )
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.ORPHAN_BROKER_POSITION
    assert finding.observed_value == "10"
    assert finding.expected_value is None


def test_an_orphan_is_never_sold_adopted_or_assigned_to_a_campaign(snapshot_of, policy) -> None:
    findings = compare_positions(
        expected=[], snapshot=snapshot_of([stock_position()]), severity=policy.severity_of
    )
    [finding] = findings
    detail = (finding.detail or "").lower()
    assert "provenance is unknown" in detail
    assert "no allocation, execution, strategy or research thesis is invented" in detail
    assert finding.allocation_id is None
    assert finding.opportunity_id is None


def test_a_closed_position_is_not_reported_as_missing(snapshot_of, policy) -> None:
    """Expected zero and broker zero is nothing to report, not a discrepancy."""
    findings = compare_positions(
        expected=[_expected(quantity=Decimal("0"))],
        snapshot=snapshot_of([]),
        severity=policy.severity_of,
    )
    assert [f.finding_type for f in findings] == [ReconciliationFindingType.BROKER_RETURNED_EMPTY]


# ---------------------------------------------------------------------------
# Contract mismatch
# ---------------------------------------------------------------------------
def test_the_same_instrument_under_a_different_contract_id_is_one_finding(
    snapshot_of, policy
) -> None:
    """What a corporate action that adjusted the contract looks like."""
    findings = compare_positions(
        expected=[_expected(key="cid:100001", contract_id=100001)],
        snapshot=snapshot_of([option_position(contract_id=555555)]),
        severity=policy.severity_of,
    )
    kinds = {finding.finding_type for finding in findings}
    assert ReconciliationFindingType.POSITION_CONTRACT_MISMATCH in kinds
    assert ReconciliationFindingType.EXPECTED_POSITION_MISSING not in kinds


# ---------------------------------------------------------------------------
# Broker data unavailable (brief section 54)
# ---------------------------------------------------------------------------
def test_an_unreadable_broker_compares_nothing_at_all(unreadable_snapshot, policy) -> None:
    findings = compare_positions(
        expected=[_expected()], snapshot=unreadable_snapshot, severity=policy.severity_of
    )
    [finding] = findings
    assert finding.finding_type is ReconciliationFindingType.BROKER_DATA_UNAVAILABLE


def test_an_unreadable_broker_does_not_report_every_position_as_missing(
    unreadable_snapshot, policy
) -> None:
    """The mistake this refusal exists to prevent."""
    findings = compare_positions(
        expected=[_expected(), _expected(key="cid:2", contract_id=2)],
        snapshot=unreadable_snapshot,
        severity=policy.severity_of,
    )
    assert len(findings) == 1
    assert all(
        finding.finding_type is not ReconciliationFindingType.EXPECTED_POSITION_MISSING
        for finding in findings
    )


def test_an_unreadable_broker_says_it_is_not_an_empty_account(unreadable_snapshot, policy) -> None:
    [finding] = compare_positions(
        expected=[], snapshot=unreadable_snapshot, severity=policy.severity_of
    )
    assert "NOT an empty account" in (finding.detail or "")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_the_comparison_is_deterministic(snapshot_of, policy) -> None:
    expected = [_expected(), _expected(key="cid:2", contract_id=2)]
    snapshot = snapshot_of([option_position(), stock_position()])
    first = compare_positions(expected=expected, snapshot=snapshot, severity=policy.severity_of)
    second = compare_positions(
        expected=list(reversed(expected)), snapshot=snapshot, severity=policy.severity_of
    )
    assert [f.finding_id for f in first] == [f.finding_id for f in second]


def test_the_same_disagreement_produces_the_same_finding_id(snapshot_of, policy) -> None:
    """Which is what makes a repeated reconciliation recognisable as a repeat."""
    first = compare_positions(
        expected=[_expected()], snapshot=snapshot_of([]), severity=policy.severity_of
    )
    later = compare_positions(
        expected=[_expected()], snapshot=snapshot_of([]), severity=policy.severity_of
    )
    assert first[0].finding_id == later[0].finding_id


# ---------------------------------------------------------------------------
# Structures (brief section 62)
# ---------------------------------------------------------------------------
def test_a_partial_structure_is_reported(policy) -> None:
    structure = StrategyPosition(
        strategy_position_id="strategypos-1",
        account_reference=MASKED,
        as_of=NOW,
        underlying="NVDA",
        strategy="LONG_STRADDLE",
        status=StructureStatus.PARTIAL,
        authorized_quantity=1,
        filled_quantity=Decimal("1"),
        legs=[
            StrategyLegPosition(
                leg_index=0,
                key="cid:1",
                underlying="NVDA",
                right=OptionRight.CALL,
                strike=Decimal("180.00"),
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("1"),
            ),
            StrategyLegPosition(
                leg_index=1,
                key="cid:2",
                underlying="NVDA",
                right=OptionRight.PUT,
                strike=Decimal("180.00"),
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("0"),
            ),
        ],
        opportunity_id="opportunity-1",
    )
    [finding] = compare_structures([structure], severity=policy.severity_of)
    assert finding.finding_type is ReconciliationFindingType.PARTIAL_STRUCTURE
    assert "1 of 2 legs" in finding.summary
    assert "PUT" in (finding.detail or "")
    assert "not the risk that was authorised" in (finding.detail or "")


def test_a_complete_structure_produces_no_finding(policy) -> None:
    structure = StrategyPosition(
        strategy_position_id="strategypos-1",
        account_reference=MASKED,
        as_of=NOW,
        underlying="NVDA",
        strategy="LONG_CALL",
        status=StructureStatus.COMPLETE,
        authorized_quantity=1,
        filled_quantity=Decimal("1"),
        legs=[
            StrategyLegPosition(
                leg_index=0,
                key="cid:1",
                underlying="NVDA",
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("1"),
            )
        ],
        opportunity_id="opportunity-1",
    )
    assert compare_structures([structure], severity=policy.severity_of) == []


def test_a_partial_structure_is_never_hedged_automatically(policy) -> None:
    structure = StrategyPosition(
        strategy_position_id="strategypos-1",
        account_reference=MASKED,
        as_of=NOW,
        underlying="NVDA",
        strategy="LONG_STRADDLE",
        status=StructureStatus.PARTIAL,
        authorized_quantity=1,
        filled_quantity=Decimal("1"),
        legs=[
            StrategyLegPosition(
                leg_index=0,
                key="cid:1",
                underlying="NVDA",
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("1"),
            ),
            StrategyLegPosition(
                leg_index=1,
                key="cid:2",
                underlying="NVDA",
                expected_quantity=Decimal("1"),
                observed_quantity=Decimal("0"),
            ),
        ],
        opportunity_id="opportunity-1",
    )
    [finding] = compare_structures([structure], severity=policy.severity_of)
    assert "Nothing here completes or closes it" in (finding.recommended_action or "")
