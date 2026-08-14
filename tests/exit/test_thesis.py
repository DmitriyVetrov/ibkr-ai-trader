"""Thesis invalidation: what is checked, and what is deliberately not.

The rule this file exists to pin down is a refusal. An invalidation condition
that cannot be checked against a structured fact is ``NOT_EVALUATED`` and is
never interpreted — because a judgement made by pattern-matching on words is
worse than no judgement at all: nobody can predict, reproduce or review it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.exit.factories import NOW
from trading_system.domain.enums import (
    ExitDecisionType,
    ExitReasonCode,
    ThesisConditionOutcome,
    ThesisStatus,
)
from trading_system.exit.thesis import (
    ThesisView,
    check_conditions,
    evaluate_thesis,
    thesis_status_of,
)
from trading_system.infrastructure.settings import SystemConfig

pytestmark = pytest.mark.unit


def _view(**overrides: object) -> ThesisView:
    payload: dict[str, object] = {
        "conditions": (("Guidance is cut at the results.", "The issuer guidance range."),),
        "as_of": NOW,
        "horizon_days": 21,
        "catalysts": (),
        "direction": "BULLISH",
    }
    payload.update(overrides)
    return ThesisView(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Prose is labelled, never interpreted
# ---------------------------------------------------------------------------
def test_a_prose_condition_is_not_evaluated() -> None:
    checks = check_conditions(_view(), at=NOW)

    assert len(checks) == 1
    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED
    assert checks[0].evidence is None
    assert "labelled rather than interpreted" in (checks[0].detail or "")


def test_a_condition_full_of_bearish_words_is_still_not_evaluated() -> None:
    """A pattern matcher would fire here. This engine does not have one."""
    checks = check_conditions(
        _view(
            conditions=(("The stock crashes, the thesis fails, sell immediately.", None),),
            catalysts=(),
            direction=None,
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
    )

    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED


def test_the_condition_text_is_preserved_verbatim() -> None:
    """An operator reads it; the engine does not."""
    condition = "Guidance is cut at the results."
    checks = check_conditions(_view(), at=NOW)

    assert checks[0].condition == condition
    assert checks[0].observable == "The issuer guidance range."


# ---------------------------------------------------------------------------
# What IS checkable
# ---------------------------------------------------------------------------
def test_a_passed_catalyst_violates_the_condition_that_named_it() -> None:
    """A structured fact about a calendar, matched against research's own name."""
    event = ("NVDA results", NOW - timedelta(days=1))
    checks = check_conditions(
        _view(
            conditions=(("The NVDA results do not produce the move.", None),),
            catalysts=(event,),
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
    )

    assert checks[0].outcome is ThesisConditionOutcome.VIOLATED
    assert "NVDA results" in (checks[0].evidence or "")


def test_a_future_catalyst_settles_nothing() -> None:
    checks = check_conditions(
        _view(
            conditions=(("The NVDA results do not produce the move.", None),),
            catalysts=(("NVDA results", NOW + timedelta(days=5)),),
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
    )

    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED


def test_matching_is_against_researchs_own_catalyst_names() -> None:
    """Never against a vocabulary of trading words invented in this module.

    A condition that names no catalyst research recorded matches nothing, even
    though a catalyst has passed.
    """
    checks = check_conditions(
        _view(
            conditions=(("Something unrelated happens.", None),),
            catalysts=(("NVDA results", NOW - timedelta(days=1)),),
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
    )

    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED


def test_a_decisive_adverse_move_violates_a_directional_condition() -> None:
    checks = check_conditions(
        _view(
            conditions=(("The BULLISH view is wrong.", None),),
            catalysts=(),
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
        return_pct=Decimal("-70"),
    )

    assert checks[0].outcome is ThesisConditionOutcome.VIOLATED


def test_an_ordinary_drawdown_does_not_violate_a_directional_condition() -> None:
    """Anything tighter would make this a second maximum-loss policy under
    another name."""
    checks = check_conditions(
        _view(
            conditions=(("The BULLISH view is wrong.", None),),
            catalysts=(),
            horizon_days=None,
            as_of=None,
        ),
        at=NOW,
        return_pct=Decimal("-20"),
    )

    assert checks[0].outcome is ThesisConditionOutcome.NOT_EVALUATED


def test_an_expired_horizon_holds_rather_than_invalidating() -> None:
    """A forecast whose window closed was not proved wrong.

    Treating it as an invalidation would exit every position on a schedule
    that duplicates the expiration policy while pretending to be a judgement
    about the market.
    """
    checks = check_conditions(_view(horizon_days=5), at=NOW + timedelta(days=30))

    assert checks[0].outcome is ThesisConditionOutcome.HOLDS
    assert "not an invalidation" in (checks[0].detail or "")


# ---------------------------------------------------------------------------
# Collapsing onto the Milestone 1 vocabulary
# ---------------------------------------------------------------------------
def test_nothing_checkable_is_unknown_rather_than_valid() -> None:
    checks = check_conditions(_view(), at=NOW)

    assert thesis_status_of(checks) is ThesisStatus.UNKNOWN


def test_the_engine_never_reports_weakening() -> None:
    """Deciding a thesis has weakened without being falsified is a judgement,
    and this engine makes none."""
    from trading_system.exit.models import ThesisConditionCheck

    combinations = [
        [],
        [ThesisConditionCheck(condition="a")],
        [
            ThesisConditionCheck(condition="a", outcome=ThesisConditionOutcome.HOLDS, evidence="x"),
            ThesisConditionCheck(condition="b"),
        ],
        [
            ThesisConditionCheck(
                condition="a", outcome=ThesisConditionOutcome.VIOLATED, evidence="x"
            )
        ],
    ]
    for checks in combinations:
        assert thesis_status_of(checks) is not ThesisStatus.WEAKENING


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def test_an_invalidated_thesis_exits(system_config: SystemConfig) -> None:
    view = _view(
        conditions=(("The NVDA results do not produce the move.", None),),
        catalysts=(("NVDA results", NOW - timedelta(days=1)),),
        horizon_days=None,
        as_of=None,
    )
    checks = check_conditions(view, at=NOW)

    outcome = evaluate_thesis(view, checks, config=system_config.exit.thesis)

    assert outcome.decision is ExitDecisionType.EXIT
    assert outcome.reason_code is ExitReasonCode.THESIS_INVALIDATED


def test_nothing_checkable_is_not_evaluated_rather_than_intact(
    system_config: SystemConfig,
) -> None:
    """Exactly as an untested risk limit is NOT_EVALUATED rather than PASS."""
    view = _view()
    checks = check_conditions(view, at=NOW)

    outcome = evaluate_thesis(view, checks, config=system_config.exit.thesis)

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.NOT_EVALUATED
    assert outcome.evaluated is False


def test_an_unreadable_report_blocks(system_config: SystemConfig) -> None:
    """ "We could not look" and "the thesis holds" are different facts."""
    outcome = evaluate_thesis(
        ThesisView(unavailable=True, detail="not in the store"),
        [],
        config=system_config.exit.thesis,
    )

    assert outcome.decision is ExitDecisionType.BLOCK
    assert outcome.reason_code is ExitReasonCode.THESIS_DATA_UNAVAILABLE


def test_an_intact_thesis_waits(system_config: SystemConfig) -> None:
    view = _view(horizon_days=5)
    checks = check_conditions(view, at=NOW + timedelta(days=30))

    outcome = evaluate_thesis(view, checks, config=system_config.exit.thesis)

    assert outcome.decision is ExitDecisionType.WAIT
    assert outcome.reason_code is ExitReasonCode.THESIS_INTACT


def test_prose_interpretation_cannot_be_switched_on() -> None:
    """Enabling it would need a model, and Milestone 10 has none."""
    from pydantic import ValidationError

    from trading_system.infrastructure.settings import ExitThesisConfig

    with pytest.raises(ValidationError, match="allow_prose_interpretation"):
        ExitThesisConfig(allow_prose_interpretation=True)


def test_the_view_carries_no_evidence_no_sources_and_no_confidence() -> None:
    """A shape that cannot carry them cannot be tempted to interpret them."""
    fields = set(ThesisView.__dataclass_fields__)

    assert "evidence" not in fields
    assert "sources" not in fields
    assert "confidence" not in fields
    assert "thesis" not in fields


def test_the_view_is_built_from_dated_events_not_from_undated_catalyst_summaries(
    market_research_report,
) -> None:
    """A ``Catalyst`` has no date, so nothing about it is checkable; a
    ``ReportedEvent`` carries ``expected_event_time``, which is."""
    from trading_system.exit.service import _thesis_view_of

    view = _thesis_view_of(market_research_report)

    assert view.unavailable is False
    assert view.conditions
    assert view.catalysts
    assert all(isinstance(when, datetime) for _, when in view.catalysts)
    assert all(when.tzinfo is UTC for _, when in view.catalysts if when)
    assert view.direction == "BULLISH"
    assert view.horizon_days == 21
