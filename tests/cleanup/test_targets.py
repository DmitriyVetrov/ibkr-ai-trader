"""Which holdings a cleanup may touch, and — mostly — which it may not.

Selection is the narrowest gate in this operation, because everything after it
is about *how* to close a holding rather than *whether*. So most of these tests
are refusals, and each one names a different way a broader rule would have sold
something nobody authorised.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.cleanup.conftest import NOW, ORPHAN_CALL_ID, ORPHAN_CALL_KEY, orphan_position
from tests.cleanup.factories import reconciliation_result
from tests.positions.factories import ACCOUNT, MASKED
from trading_system.cleanup.targets import orphan_findings, select_targets
from trading_system.domain.enums import (
    OptionRight,
    ReconciliationFindingType,
    ReconciliationSeverity,
    TradingMode,
)
from trading_system.positions.snapshot import build_position_snapshot
from trading_system.reconciliation.findings import make_finding
from trading_system.reconciliation.models import ReconciliationResult

pytestmark = pytest.mark.unit


def _snapshot(positions):
    return build_position_snapshot(
        list(positions),
        broker="SIMULATOR",
        account_id=ACCOUNT,
        trading_mode=TradingMode.PAPER,
        as_of=NOW,
        observed_at=NOW,
    )


def _finding(
    *,
    key: str = ORPHAN_CALL_KEY,
    contract_id: int | None = ORPHAN_CALL_ID,
    quantity: str = "1",
    finding_type: ReconciliationFindingType = ReconciliationFindingType.ORPHAN_BROKER_POSITION,
):
    return make_finding(
        finding_type,
        severity=lambda _type: ReconciliationSeverity.WARNING,
        identifier=key,
        summary=f"{key}: the broker holds {quantity} contract(s) nothing accounts for",
        observed_value=quantity,
        contract_id=contract_id,
        symbol="SMH",
    )


def _result(findings, *, reconciliation_id: str = "reconciliation-test") -> ReconciliationResult:
    return reconciliation_result(reconciliation_id=reconciliation_id, findings=list(findings))


def test_orphan_target_selection_takes_exactly_the_reported_orphans() -> None:
    position = orphan_position()
    other = orphan_position(contract_id=999111, strike=Decimal("542.50"))
    selection = select_targets(result=_result([_finding()]), snapshot=_snapshot([position, other]))

    assert [target.key for target in selection.targets] == [ORPHAN_CALL_KEY]
    assert selection.targets[0].contract_id == ORPHAN_CALL_ID
    # The second holding is at the broker and is not an orphan *finding*, so it
    # is not merely rejected — it never enters the candidate list at all.
    assert "cid:999111" not in {candidate.key for candidate in selection.candidates}


def test_a_finding_that_is_not_an_orphan_is_never_a_candidate() -> None:
    """A holding the ledger disagrees about is not a holding nobody claims."""
    mismatch = make_finding(
        ReconciliationFindingType.POSITION_QUANTITY_MISMATCH,
        severity=lambda _type: ReconciliationSeverity.WARNING,
        identifier=ORPHAN_CALL_KEY,
        summary="the broker holds 1 where 2 was expected",
        expected_value="2",
        observed_value="1",
        contract_id=ORPHAN_CALL_ID,
        symbol="SMH",
    )
    selection = select_targets(result=_result([mismatch]), snapshot=_snapshot([orphan_position()]))

    assert selection.candidates == ()
    assert selection.targets == ()


def test_target_uses_broker_contract_identity_and_refuses_anything_weaker() -> None:
    """A symbol is not an identity: adjusted contracts share all four terms."""
    by_symbol = _finding(key="sym:SMH|2026-09-18|540.0|CALL|USD", contract_id=None)
    selection = select_targets(result=_result([by_symbol]), snapshot=_snapshot([orphan_position()]))

    assert selection.targets == ()
    assert "contract id" in selection.rejected[0].reason


def test_a_target_cannot_be_built_without_a_contract_id_key() -> None:
    from trading_system.cleanup.models import CleanupTarget

    with pytest.raises(ValueError, match="not identified by a broker contract id"):
        CleanupTarget.model_validate(
            {
                "key": "sym:SMH",
                "contract_id": ORPHAN_CALL_ID,
                "position_id": "position-x",
                "account_reference": MASKED,
                "underlying": "SMH",
                "symbol": "SMH",
                "asset_class": "OPTION",
                "quantity": "1",
                "finding_id": "finding-x",
                "reconciliation_id": "reconciliation-x",
                "broker_source": "SIMULATOR",
                "observed_at": NOW.isoformat(),
            }
        )


def test_a_holding_the_broker_no_longer_reports_is_not_targeted() -> None:
    """The ordinary answer on a second run: nothing to close, nothing sent."""
    selection = select_targets(result=_result([_finding()]), snapshot=_snapshot([]))

    assert selection.targets == ()
    assert "no longer reports" in selection.rejected[0].reason


def test_a_quantity_that_changed_since_the_report_is_not_targeted() -> None:
    """The operator reviewed a report; the account has moved on since."""
    selection = select_targets(
        result=_result([_finding(quantity="1")]),
        snapshot=_snapshot([orphan_position(quantity=Decimal("3"))]),
    )

    assert selection.targets == ()
    assert "now holds 3" in selection.rejected[0].reason


def test_a_short_holding_is_never_targeted() -> None:
    selection = select_targets(
        result=_result([_finding(quantity="-1")]),
        snapshot=_snapshot([orphan_position(quantity=Decimal("-1"))]),
    )

    assert selection.targets == ()
    assert "unbounded above" in selection.rejected[0].reason


def test_a_fractional_quantity_is_never_targeted() -> None:
    selection = select_targets(
        result=_result([_finding(quantity="1.5")]),
        snapshot=_snapshot([orphan_position(quantity=Decimal("1.5"))]),
    )

    assert selection.targets == ()
    assert "fractional" in selection.rejected[0].reason


def test_contract_ids_narrow_the_set_and_can_never_widen_it() -> None:
    call = _finding()
    put = _finding(key="cid:848575500", contract_id=848575500)
    positions = [
        orphan_position(),
        orphan_position(contract_id=848575500, right=OptionRight.PUT),
        # At the broker, not reported as an orphan, and explicitly asked for.
        orphan_position(contract_id=777000),
    ]

    selection = select_targets(
        result=_result([call, put]),
        snapshot=_snapshot(positions),
        wanted_contract_ids=[ORPHAN_CALL_ID, 777000],
    )

    assert [target.contract_id for target in selection.targets] == [ORPHAN_CALL_ID]
    # Naming a contract the reconciliation did not report as an orphan does not
    # make it selectable. That is what makes the option safe to expose.
    assert 777000 not in {
        candidate.target.contract_id
        for candidate in selection.candidates
        if candidate.target is not None
    }


def test_the_target_copies_the_broker_and_completes_nothing() -> None:
    position = orphan_position()
    selection = select_targets(result=_result([_finding()]), snapshot=_snapshot([position]))
    target = selection.targets[0]

    assert target.quantity == position.quantity
    assert target.market_price == position.market_price
    assert target.average_cost == position.average_cost
    assert target.multiplier == position.multiplier
    assert target.currency == position.currency
    # Not reported by the broker's position list, and not derived from "SMH".
    assert target.trading_class is None


def test_selection_is_pure_and_repeatable() -> None:
    result = _result([_finding()])
    snapshot = _snapshot([orphan_position()])

    first = select_targets(result=result, snapshot=snapshot)
    second = select_targets(result=result, snapshot=snapshot)

    assert [t.model_dump() for t in first.targets] == [t.model_dump() for t in second.targets]


def test_orphan_findings_are_ordered_deterministically() -> None:
    findings = [
        _finding(key="cid:200", contract_id=200),
        _finding(key="cid:100", contract_id=100),
    ]
    assert [f.identifier for f in orphan_findings(_result(findings))] == ["cid:100", "cid:200"]


def test_rejections_are_kept_rather_than_quietly_dropped() -> None:
    """A shorter target list must never look like a tidier account."""
    selection = select_targets(result=_result([_finding()]), snapshot=_snapshot([]))

    assert selection.orphan_count == 1
    assert len(selection.rejected) == 1
    assert selection.rejected[0].finding_id


def test_an_unparseable_reported_quantity_skips_the_comparison_and_uses_the_broker() -> None:
    """Nothing is invented either way: the snapshot's figure is what is used."""
    selection = select_targets(
        result=_result([_finding(quantity="several")]),
        snapshot=_snapshot([orphan_position(quantity=Decimal("1"))]),
    )

    assert [target.quantity for target in selection.targets] == [Decimal("1")]


def test_the_selection_records_which_reconciliation_authorised_it() -> None:
    selection = select_targets(
        result=_result([_finding()], reconciliation_id="reconciliation-abc"),
        snapshot=_snapshot([orphan_position()]),
    )

    assert selection.reconciliation_id == "reconciliation-abc"
    assert selection.targets[0].reconciliation_id == "reconciliation-abc"
    assert selection.targets[0].observed_at == NOW
    assert NOW - selection.targets[0].observed_at < timedelta(seconds=1)
