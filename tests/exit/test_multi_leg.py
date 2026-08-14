"""A multi-leg structure is one position, one decision and one order.

A straddle whose call is sold and whose put is not is a naked long put: a
position nobody authorised, sized against limits nobody checked for it. That is
the same failure Milestone 8 refuses on the way in, and it is refused here on
the way out — in the models, in the configuration, in the engine and in the
order builder.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.exit import factories
from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitQuoteField,
    ExitReasonCode,
    LegAction,
    OptionRight,
    StrategyType,
    StructureStatus,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# One lifecycle, one decision
# ---------------------------------------------------------------------------
def test_a_straddle_is_one_position_with_two_legs(open_straddle) -> None:
    service, _entry = open_straddle

    positions = service.open_positions()

    assert len(positions) == 1
    position = positions[0]
    assert position.strategy is StrategyType.LONG_STRADDLE
    assert len(position.legs) == 2
    assert position.observed_units == 2
    assert position.structure.status is StructureStatus.COMPLETE


def test_a_straddle_produces_exactly_one_decision(open_straddle) -> None:
    service, _ = open_straddle

    run = service.monitor()

    assert run.result.counts.evaluated == 1
    assert len(run.result.decisions) == 1
    assert run.result.decisions[0].close_whole_strategy is True


def test_the_structure_is_priced_as_the_net_of_its_legs(open_straddle) -> None:
    """6.50 for the call plus 4.20 for the put is 10.70 for the straddle."""
    service, _ = open_straddle

    run = service.monitor()

    valuation = run.result.evaluations[0].valuation
    assert valuation.exit_quote == Decimal("10.70")
    assert valuation.exit_value == Decimal("1070.00")
    assert [leg.price for leg in valuation.legs] == [Decimal("6.50"), Decimal("4.20")]


def test_one_unpriced_leg_leaves_the_whole_structure_unpriced(
    build_exit_service, data_repo, stored_research
) -> None:
    """Half a straddle is a directional bet, so a structure priced from the leg
    that happened to be quoted would be a valuation of a different position."""
    factories.store_quotes(data_repo, [factories.option_quote()])
    service = build_exit_service(
        executions=[
            factories.entry_execution(
                legs=factories.straddle_legs(),
                strategy=StrategyType.LONG_STRADDLE,
                research_report_id=stored_research,
            )
        ],
        snapshot=factories.position_snapshot(
            [
                factories.broker_position(),
                factories.broker_position(
                    contract_id=factories.PUT_CONTRACT_ID, right=OptionRight.PUT
                ),
            ]
        ),
    )

    run = service.monitor()

    valuation = run.result.evaluations[0].valuation
    assert valuation.exit_quote is None
    assert valuation.unpriced_legs == [1]
    assert run.result.decisions[0].decision is ExitDecisionType.BLOCK


def test_a_half_held_straddle_blocks_rather_than_being_exited(
    build_exit_service, data_repo, stored_research
) -> None:
    """The risk of what is actually held is not the risk that was authorised."""
    factories.store_quotes(
        data_repo,
        [
            factories.option_quote(),
            factories.option_quote(
                contract_id=factories.PUT_CONTRACT_ID,
                right=OptionRight.PUT,
                bid=Decimal("4.20"),
                ask=Decimal("4.40"),
            ),
        ],
    )
    service = build_exit_service(
        executions=[
            factories.entry_execution(
                legs=factories.straddle_legs(),
                strategy=StrategyType.LONG_STRADDLE,
                research_report_id=stored_research,
            )
        ],
        # The call is held; the put is not.
        snapshot=factories.position_snapshot([factories.broker_position()]),
    )

    run = service.monitor()
    decision = run.result.decisions[0]

    assert decision.decision is ExitDecisionType.BLOCK
    assert decision.primary_reason is ExitReasonCode.PARTIAL_STRUCTURE
    assert run.orders_submitted == 0


def test_the_weakest_leg_decides_how_many_units_are_held(
    build_exit_service, data_repo, stored_research
) -> None:
    """Two calls and one put is one straddle and a spare call, and an exit for
    two would sell contracts that do not exist."""
    factories.store_quotes(
        data_repo,
        [
            factories.option_quote(),
            factories.option_quote(
                contract_id=factories.PUT_CONTRACT_ID,
                right=OptionRight.PUT,
                bid=Decimal("4.20"),
                ask=Decimal("4.40"),
            ),
        ],
    )
    service = build_exit_service(
        executions=[
            factories.entry_execution(
                legs=factories.straddle_legs(),
                strategy=StrategyType.LONG_STRADDLE,
                research_report_id=stored_research,
            )
        ],
        snapshot=factories.position_snapshot(
            [
                factories.broker_position(quantity=Decimal("2")),
                factories.broker_position(
                    contract_id=factories.PUT_CONTRACT_ID,
                    right=OptionRight.PUT,
                    quantity=Decimal("1"),
                ),
            ]
        ),
    )

    assert service.open_positions()[0].observed_units == 1


# ---------------------------------------------------------------------------
# One order
# ---------------------------------------------------------------------------
def test_the_exit_intent_is_one_combo_with_both_legs_reversed(
    system_config,
) -> None:
    """Never two independent orders, and every leg inverted."""
    from trading_system.domain.models import OptionLeg
    from trading_system.execution.order_builder import build_exit_order_intent

    legs = [
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.CALL,
            strike=Decimal("180.00"),
            expiration=date(2026, 9, 18),
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=factories.CALL_CONTRACT_ID,
        ),
        OptionLeg(
            underlying="NVDA",
            right=OptionRight.PUT,
            strike=Decimal("180.00"),
            expiration=date(2026, 9, 18),
            action=LegAction.BUY,
            multiplier=100,
            broker_contract_id=factories.PUT_CONTRACT_ID,
        ),
    ]

    intent = build_exit_order_intent(
        position_id="strategypos-1",
        exit_request_id="exit-req-1",
        purchase_card_id="card-1",
        risk_decision_id="risk-1",
        underlying="NVDA",
        strategy_type=StrategyType.LONG_STRADDLE,
        legs=legs,
        quantity=2,
        reference_quote=Decimal("10.70"),
        config=system_config.exit.order,
        execution_config=system_config.execution,
        trading_mode=system_config.execution.time_in_force
        and __import__("trading_system.domain.enums", fromlist=["TradingMode"]).TradingMode.PAPER,
        created_at=NOW,
        versions=factories.versions(),
    )

    assert len(intent.legs) == 2
    assert [leg.action for leg in intent.legs] == [LegAction.SELL, LegAction.SELL]
    assert [leg.broker_contract_id for leg in intent.legs] == [
        factories.CALL_CONTRACT_ID,
        factories.PUT_CONTRACT_ID,
    ]
    assert intent.quantity == 2


def test_an_independent_leg_exit_cannot_be_configured() -> None:
    """Globally and per strategy: ``true`` fails to load."""
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExitConfig

    with pytest.raises(ValidationError, match="naked long"):
        ExitConfig(allow_independent_leg_exit=True)


def test_no_shipped_strategy_permits_an_independent_leg_exit(system_config) -> None:
    for name, strategy in system_config.strategies.items():
        assert strategy.exit_policy.allow_independent_leg_exit is False, name


def test_a_strategy_permitting_an_independent_leg_exit_fails_to_load(
    tmp_config_dir,
) -> None:
    """The configuration hierarchy refuses it, not only the global block."""
    from trading_system.infrastructure.settings import ConfigError, load_config

    path = tmp_config_dir / "strategies" / "long_straddle.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "allow_independent_leg_exit: false", "allow_independent_leg_exit: true"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="allow_independent_leg_exit"):
        load_config(tmp_config_dir)


def test_a_strangle_behaves_exactly_as_a_straddle_does(
    build_exit_service, data_repo, stored_research
) -> None:
    """Two legs on different strikes; still one position and one decision."""
    legs = [
        factories.execution_leg(leg_index=0, strike=Decimal("185.00")),
        factories.execution_leg(
            leg_index=1,
            contract_id=factories.PUT_CONTRACT_ID,
            right=OptionRight.PUT,
            strike=Decimal("175.00"),
        ),
    ]
    factories.store_quotes(
        data_repo,
        [
            factories.option_quote(strike=Decimal("185.00")),
            factories.option_quote(
                contract_id=factories.PUT_CONTRACT_ID,
                right=OptionRight.PUT,
                strike=Decimal("175.00"),
                bid=Decimal("3.10"),
                ask=Decimal("3.30"),
            ),
        ],
    )
    service = build_exit_service(
        executions=[
            factories.entry_execution(
                legs=legs,
                strategy=StrategyType.LONG_STRANGLE,
                research_report_id=stored_research,
            )
        ],
        snapshot=factories.position_snapshot(
            [
                factories.broker_position(strike=Decimal("185.00")),
                factories.broker_position(
                    contract_id=factories.PUT_CONTRACT_ID,
                    right=OptionRight.PUT,
                    strike=Decimal("175.00"),
                ),
            ]
        ),
    )

    run = service.monitor()

    assert len(run.result.decisions) == 1
    assert run.result.evaluations[0].valuation.exit_quote == Decimal("9.60")
    assert run.result.decisions[0].close_whole_strategy is True


def test_every_leg_of_the_valuation_names_the_field_it_was_priced_at(
    open_straddle,
) -> None:
    service, _ = open_straddle

    run = service.monitor()

    for leg in run.result.evaluations[0].valuation.legs:
        assert leg.quote_field is ExitQuoteField.BID
