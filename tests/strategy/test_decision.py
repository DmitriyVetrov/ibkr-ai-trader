"""Deterministic validation of a strategy decision (brief section 9).

The prompt asks the agent to stay inside its boundaries. These tests are about
what happens when it does not. Every case ends the same way: the whole decision
is rejected. Nothing is repaired — not an unknown strategy swapped for the
nearest eligible one, not an over-claimed confidence quietly lowered, not a
refuted reason code dropped from the list. A repaired decision would be stored
as the model's own, and a system that edits its AI's answers cannot be audited.
"""

from __future__ import annotations

import pytest

from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    ExpectedMagnitude,
    MarketHypothesis,
    StrategyAction,
    StrategySelectionReason,
    StrategyType,
)
from trading_system.strategies.context import build_selection_input
from trading_system.strategies.models import StrategyAgentOutput, StrategySelectionInput
from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.validation import (
    StrategyOutputInvalidError,
    validate_agent_output,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(system_config) -> StrategyRegistry:
    return StrategyRegistry.from_config(system_config)


@pytest.fixture
def make_input(registry: StrategyRegistry, make_report):
    def _make(**report_kwargs) -> StrategySelectionInput:
        report = make_report(**report_kwargs)
        assert report.hypothesis is not None
        return build_selection_input(
            run_id="strategy-run-test",
            report=report,
            eligible=registry.options_for(report.hypothesis),
        )

    return _make


def _output(selection_input: StrategySelectionInput, **overrides) -> StrategyAgentOutput:
    payload = {
        "run_id": selection_input.run_id,
        "symbol": selection_input.symbol,
        "action": StrategyAction.BUY,
        "selected_strategy": next(iter(sorted(selection_input.strategy_ids, key=str))),
        "confidence": ConfidenceLevel.MEDIUM,
        "reasons": [StrategySelectionReason.HYPOTHESIS_MATCH],
        "rationale": "the hypothesis and the payoff agree over the stated horizon",
    }
    payload.update(overrides)
    return StrategyAgentOutput.model_validate(payload)


def _validate(output, selection_input, config) -> None:
    validate_agent_output(output, selection_input, config=config)


def _codes(error: StrategyOutputInvalidError) -> set[str]:
    return set(error.codes)


# ---------------------------------------------------------------------------
# A well-formed decision passes
# ---------------------------------------------------------------------------
def test_a_matching_decision_is_accepted(make_input, strategy_stage_config) -> None:
    selection_input = make_input(hypothesis=MarketHypothesis.B)

    _validate(_output(selection_input), selection_input, strategy_stage_config)


def test_a_no_trade_decision_is_accepted(make_input, strategy_stage_config) -> None:
    """Declining is a correct answer, and needs no strategy to justify it."""
    selection_input = make_input()

    _validate(
        _output(
            selection_input,
            action=StrategyAction.NO_TRADE,
            selected_strategy=None,
            reasons=[StrategySelectionReason.RESEARCH_INCOMPATIBLE],
        ),
        selection_input,
        strategy_stage_config,
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_a_response_for_another_run_is_rejected(make_input, strategy_stage_config) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, run_id="some-other-run"),
            selection_input,
            strategy_stage_config,
        )

    assert "RUN_ID_MISMATCH" in _codes(error.value)


def test_a_response_about_another_symbol_is_rejected(make_input, strategy_stage_config) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(_output(selection_input, symbol="AAPL"), selection_input, strategy_stage_config)

    assert "SYMBOL_MISMATCH" in _codes(error.value)


# ---------------------------------------------------------------------------
# 7-8. The strategy must exist and must be eligible
# ---------------------------------------------------------------------------
def test_a_strategy_that_was_not_offered_is_rejected(make_input, strategy_stage_config) -> None:
    """Hypothesis B offers only the long call; naming another is inventing one."""
    selection_input = make_input(hypothesis=MarketHypothesis.B)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, selected_strategy=StrategyType.LONG_STRADDLE),
            selection_input,
            strategy_stage_config,
        )

    assert "UNKNOWN_STRATEGY" in _codes(error.value)


def test_an_ineligible_strategy_cannot_even_reach_the_agent(registry, make_report) -> None:
    """Structural, not merely validated: the input refuses to carry it."""
    report = make_report(hypothesis=MarketHypothesis.B)
    straddle = registry.require(StrategyType.LONG_STRADDLE).to_option()

    with pytest.raises(ValueError, match="must never reach the agent"):
        build_selection_input(
            run_id="strategy-run-test",
            report=report,
            eligible=[*registry.options_for(MarketHypothesis.B), straddle],
        )


def test_a_directional_strategy_against_the_wrong_direction_is_rejected(
    registry, make_report, strategy_stage_config
) -> None:
    """A long put for a bullish outlook is a contradiction, not a nuance."""
    report = make_report(hypothesis=MarketHypothesis.C, direction=Direction.BULLISH)
    selection_input = build_selection_input(
        run_id="strategy-run-test",
        report=report,
        eligible=registry.options_for(MarketHypothesis.C),
    )

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, selected_strategy=StrategyType.LONG_PUT),
            selection_input,
            strategy_stage_config,
        )

    assert "DIRECTION_CONTRADICTS_RESEARCH" in _codes(error.value)


# ---------------------------------------------------------------------------
# 11. Confidence
# ---------------------------------------------------------------------------
def test_confidence_above_the_research_is_rejected_not_lowered(
    make_input, strategy_stage_config
) -> None:
    selection_input = make_input(confidence=ConfidenceLevel.LOW)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, confidence=ConfidenceLevel.HIGH),
            selection_input,
            strategy_stage_config,
        )

    assert "CONFIDENCE_EXCEEDS_RESEARCH" in _codes(error.value)


def test_confidence_at_or_below_the_research_is_accepted(make_input, strategy_stage_config) -> None:
    selection_input = make_input(confidence=ConfidenceLevel.HIGH)

    for band in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH):
        _validate(_output(selection_input, confidence=band), selection_input, strategy_stage_config)


# ---------------------------------------------------------------------------
# Reason codes are checked against the research
# ---------------------------------------------------------------------------
def test_claiming_an_event_the_research_does_not_name_is_rejected(
    make_input, strategy_stage_config
) -> None:
    selection_input = make_input(hypothesis=MarketHypothesis.B, event_days=None)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, reasons=[StrategySelectionReason.EVENT_IN_HORIZON]),
            selection_input,
            strategy_stage_config,
        )

    assert "UNSUPPORTED_REASON" in _codes(error.value)


def test_claiming_a_large_move_the_research_does_not_expect_is_rejected(
    make_input, strategy_stage_config
) -> None:
    selection_input = make_input(magnitude=ExpectedMagnitude.SMALL)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, reasons=[StrategySelectionReason.LARGE_MOVE_EXPECTED]),
            selection_input,
            strategy_stage_config,
        )

    assert "UNSUPPORTED_REASON" in _codes(error.value)


def test_claiming_sufficient_confidence_over_a_low_one_is_rejected(
    make_input, strategy_stage_config
) -> None:
    selection_input = make_input(confidence=ConfidenceLevel.LOW)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(
                selection_input,
                confidence=ConfidenceLevel.LOW,
                reasons=[StrategySelectionReason.CONFIDENCE_SUFFICIENT],
            ),
            selection_input,
            strategy_stage_config,
        )

    assert "UNSUPPORTED_REASON" in _codes(error.value)


def test_claiming_no_eligible_strategy_while_being_offered_some_is_rejected(
    make_input, strategy_stage_config
) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(
                selection_input,
                action=StrategyAction.NO_TRADE,
                selected_strategy=None,
                confidence=ConfidenceLevel.LOW,
                reasons=[StrategySelectionReason.NO_ELIGIBLE_STRATEGY],
            ),
            selection_input,
            strategy_stage_config,
        )

    assert "UNSUPPORTED_REASON" in _codes(error.value)


def test_a_judgement_about_fit_is_never_second_guessed(make_input, strategy_stage_config) -> None:
    """Facts are enforced; opinions are the agent's to hold."""
    selection_input = make_input()

    _validate(
        _output(
            selection_input,
            action=StrategyAction.NO_TRADE,
            selected_strategy=None,
            confidence=ConfidenceLevel.LOW,
            reasons=[StrategySelectionReason.RESEARCH_INCOMPATIBLE],
        ),
        selection_input,
        strategy_stage_config,
    )


def test_a_duplicated_reason_is_rejected(make_input, strategy_stage_config) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(
                selection_input,
                reasons=[
                    StrategySelectionReason.HYPOTHESIS_MATCH,
                    StrategySelectionReason.HYPOTHESIS_MATCH,
                ],
            ),
            selection_input,
            strategy_stage_config,
        )

    assert "DUPLICATE_REASON" in _codes(error.value)


def test_a_supported_reason_is_accepted(make_input, strategy_stage_config) -> None:
    selection_input = make_input(
        hypothesis=MarketHypothesis.A,
        direction=Direction.UNCERTAIN,
        magnitude=ExpectedMagnitude.LARGE,
        event_days=None,
    )

    _validate(
        _output(
            selection_input,
            selected_strategy=StrategyType.LONG_STRADDLE,
            reasons=[
                StrategySelectionReason.HYPOTHESIS_MATCH,
                StrategySelectionReason.DIRECTION_UNCERTAIN,
                StrategySelectionReason.LARGE_MOVE_EXPECTED,
                StrategySelectionReason.NO_EVENT_IN_HORIZON,
            ],
        ),
        selection_input,
        strategy_stage_config,
    )


# ---------------------------------------------------------------------------
# 12-16. A contract, a size or a price smuggled into prose
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rationale", "code"),
    [
        ("buy the 190 call, which fits the thesis", "CONTRACT_RECOMMENDED"),
        ("take the strike at 185 for the best convexity", "STRIKE_RECOMMENDED"),
        ("use the 2026-09-18 expiration", "EXPIRATION_RECOMMENDED"),
        ("open with 5 contracts and add later", "QUANTITY_STATED"),
        ("allocate EUR 1500 to this idea", "ALLOCATION_STATED"),
        ("size it at $2,000 of premium", "ALLOCATION_STATED"),
    ],
    ids=["contract", "strike", "expiration", "quantity", "money", "currency"],
)
def test_a_contract_or_a_size_in_prose_is_rejected(
    make_input, strategy_stage_config, rationale: str, code: str
) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(selection_input, rationale=rationale), selection_input, strategy_stage_config
        )

    assert code in _codes(error.value)


def test_the_guard_applies_to_a_no_trade_too(make_input, strategy_stage_config) -> None:
    selection_input = make_input()

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(
                selection_input,
                action=StrategyAction.NO_TRADE,
                selected_strategy=None,
                confidence=ConfidenceLevel.LOW,
                reasons=[StrategySelectionReason.RESEARCH_INCOMPATIBLE],
                rationale="not now; the 200 call would be too expensive anyway",
            ),
            selection_input,
            strategy_stage_config,
        )

    assert "STRIKE_RECOMMENDED" in _codes(error.value) or "CONTRACT_RECOMMENDED" in _codes(
        error.value
    )


def test_ordinary_analysis_is_not_rejected(make_input, strategy_stage_config) -> None:
    """The guard is narrow on purpose: legitimate reasoning must survive it."""
    selection_input = make_input()

    _validate(
        _output(
            selection_input,
            rationale=(
                "the outlook is directional over a 21-day horizon, which this strategy's "
                "14-30 day window can express, and the evidence is consistent"
            ),
        ),
        selection_input,
        strategy_stage_config,
    )


# ---------------------------------------------------------------------------
# Every problem is reported, not just the first
# ---------------------------------------------------------------------------
def test_all_problems_are_collected(make_input, strategy_stage_config) -> None:
    selection_input = make_input(confidence=ConfidenceLevel.LOW)

    with pytest.raises(StrategyOutputInvalidError) as error:
        _validate(
            _output(
                selection_input,
                run_id="wrong-run",
                confidence=ConfidenceLevel.HIGH,
                rationale="buy the 190 call",
            ),
            selection_input,
            strategy_stage_config,
        )

    assert {"RUN_ID_MISMATCH", "CONFIDENCE_EXCEEDS_RESEARCH", "CONTRACT_RECOMMENDED"} <= _codes(
        error.value
    )
