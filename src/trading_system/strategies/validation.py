"""Deterministic validation of what the strategy agent returned.

The prompt asks the agent to stay inside its boundaries. This module is what
*enforces* them, and the difference matters: a prompt is a request, and a
request is not a control.

The response to a violation is always the same: **reject the whole decision**.
Nothing is repaired — an unknown strategy is not swapped for the nearest
eligible one, an over-claimed confidence is not quietly lowered, a reason the
research refutes is not dropped from the list. Repairing would store a decision
the model did not make while recording it as the model's own, and a system that
edits its AI's answers cannot be audited. The symbol ends as
``SEMANTIC_VALIDATION_FAILED`` and proposes no trade.

What is checked, and why each is a real failure mode rather than a formality:

* **The strategy exists and was offered.** A strategy absent from the input was
  never validated against the hypothesis, the registry or the risk policy.
  Naming one is indistinguishable from inventing one.
* **Direction agrees with structure.** A long put for a bullish outlook is not
  an unusual view worth preserving, it is a contradiction between the payoff
  chosen and the reason given for choosing it.
* **The horizon is expressible.** A strategy whose DTE window cannot reach the
  horizon the research states cannot express that research, however well it
  matches the hypothesis.
* **Confidence is bounded by the research.** A decision cannot be more
  confident than the outlook it rests on. Confidence is a band, never a
  probability, and this is the one property that survives that.
* **Reason codes survive contact with the evidence.** A code whose precondition
  the research report contradicts is a fabricated justification even though it
  comes from the allowed vocabulary.
* **No contract, no size, no money.** Structurally there is no field for any of
  them; a narrow textual guard additionally catches one smuggled into prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    ExpectedMagnitude,
    StrategyAction,
    StrategySelectionReason,
)
from trading_system.infrastructure.settings import StrategyStageConfig
from trading_system.strategies.models import (
    ResearchSummary,
    StrategyAgentOutput,
    StrategyOption,
    StrategySelectionInput,
    confidence_rank,
)

__all__ = [
    "StrategyOutputInvalidError",
    "ValidationIssue",
    "validate_agent_output",
]


class StrategyOutputInvalidError(RuntimeError):
    """The agent's decision cannot be trusted, so none of it is used."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))

    @property
    def codes(self) -> list[str]:
        return [issue.code for issue in self.issues]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One specific way the response failed, with the field it concerns."""

    code: str
    message: str
    reference: str | None = None


#: Prose that would amount to selecting a contract. Deliberately narrow, for
#: the same reason as the research layer's guard: the structural guarantee is
#: that no output field can hold a contract, and this only catches an
#: instruction written into a sentence.
_CONTRACT_INSTRUCTION = re.compile(
    r"(?:\b(?:buy|sell|purchase|write|short|go\s+long)\b[^.]{0,60}?)?"
    r"\b\d+(?:\.\d+)?\s*(?:strike\s+)?(?:call|put)s?\b",
    re.IGNORECASE,
)
#: A specific strike, however phrased.
_STRIKE_INSTRUCTION = re.compile(
    r"\b(?:strikes?\s+(?:of\s+|at\s+)?\$?\d+(?:\.\d+)?|\$?\d+(?:\.\d+)?\s+strike)\b",
    re.IGNORECASE,
)
#: A specific expiration date. The agent is told a DTE *window*, never a date.
_EXPIRATION_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
#: A position size.
_QUANTITY = re.compile(r"\b\d+\s+contracts?\b", re.IGNORECASE)
#: An amount of money, in either order: "EUR 1500" and "1500 EUR" are the same
#: instruction, and only one of them looks like a currency amount to a naive
#: pattern. Sizing belongs to the allocation engine, not here.
_MONEY = re.compile(
    r"(?:[$€£]\s?\d"
    r"|\b\d+(?:[.,]\d+)?\s*(?:eur|usd|dollars?|euros?)\b"
    r"|\b(?:eur|usd)\s?\d)",
    re.IGNORECASE,
)


def validate_agent_output(
    output: StrategyAgentOutput,
    selection_input: StrategySelectionInput,
    *,
    config: StrategyStageConfig,
) -> None:
    """Check a decision against the input that produced it and the policy.

    Raises :class:`StrategyOutputInvalidError` listing every problem found,
    rather than stopping at the first: an operator debugging a misbehaving
    prompt needs the whole picture, and collecting them costs nothing.
    """
    issues: list[ValidationIssue] = []

    issues.extend(_check_identity(output, selection_input))
    issues.extend(_check_strategy(output, selection_input))
    issues.extend(_check_confidence(output, selection_input))
    issues.extend(_check_reasons(output, selection_input, config))
    issues.extend(_check_no_contract_selection(output))

    if issues:
        raise StrategyOutputInvalidError(issues)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def _check_identity(
    output: StrategyAgentOutput, selection_input: StrategySelectionInput
) -> list[ValidationIssue]:
    """A stale response for another run or symbol is not this run's answer."""
    issues: list[ValidationIssue] = []
    if output.run_id != selection_input.run_id:
        issues.append(
            ValidationIssue(
                "RUN_ID_MISMATCH",
                f"response is for run {output.run_id!r} but the request was "
                f"{selection_input.run_id!r}",
            )
        )
    if output.symbol != selection_input.symbol:
        issues.append(
            ValidationIssue(
                "SYMBOL_MISMATCH",
                f"response is about {output.symbol!r} but the request was about "
                f"{selection_input.symbol!r}; strategy contexts are isolated per underlying",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# The strategy itself
# ---------------------------------------------------------------------------
def _check_strategy(
    output: StrategyAgentOutput, selection_input: StrategySelectionInput
) -> list[ValidationIssue]:
    """The chosen strategy must be one the input offered, and must fit."""
    if output.action is StrategyAction.NO_TRADE:
        return []

    strategy = output.selected_strategy
    assert strategy is not None  # narrowing; guaranteed by the model validator

    option = selection_input.option(strategy)
    if option is None:
        return [
            ValidationIssue(
                "UNKNOWN_STRATEGY",
                f"{strategy.value} was not among the strategies offered for hypothesis "
                f"{selection_input.research.hypothesis.value} "
                f"({', '.join(sorted(s.value for s in selection_input.strategy_ids))}); the "
                f"agent may choose from the list it is given, never extend it",
                strategy.value,
            )
        ]

    issues: list[ValidationIssue] = []
    research = selection_input.research

    if research.hypothesis not in option.applicable_hypotheses:
        # Unreachable through the input contract, which refuses to carry such a
        # strategy. Checked anyway: this is the milestone's central rule, and a
        # rule worth having is worth asserting where it is used.
        issues.append(
            ValidationIssue(
                "STRATEGY_NOT_ELIGIBLE",
                f"{strategy.value} does not answer hypothesis {research.hypothesis.value}",
                strategy.value,
            )
        )

    issues.extend(_check_direction(option, research))

    if not _covers_horizon(option, research.horizon_days):
        issues.append(
            ValidationIssue(
                "HORIZON_NOT_EXPRESSIBLE",
                f"{strategy.value} trades a {option.dte_min}-{option.dte_max} day window, "
                f"which cannot express a {research.horizon_days}-day outlook",
                strategy.value,
            )
        )
    return issues


def _check_direction(option: StrategyOption, research: ResearchSummary) -> list[ValidationIssue]:
    """A directional payoff must match the direction the research states.

    Only *directional* structures are constrained. A straddle expresses no
    direction, so pairing it with any outlook is a judgement rather than a
    contradiction, and judgements are not this module's to make.
    """
    if option.directional_view not in (Direction.BULLISH, Direction.BEARISH):
        return []
    if research.direction is option.directional_view:
        return []
    return [
        ValidationIssue(
            "DIRECTION_CONTRADICTS_RESEARCH",
            f"{option.strategy_id.value} expresses a {option.directional_view.value} view but "
            f"the research states {research.direction.value}",
            option.strategy_id.value,
        )
    ]


def _covers_horizon(option: StrategyOption, horizon_days: int) -> bool:
    """Whether a contract in the strategy's window can express the horizon."""
    return option.dte_min <= horizon_days or horizon_days <= option.dte_max


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def _check_confidence(
    output: StrategyAgentOutput, selection_input: StrategySelectionInput
) -> list[ValidationIssue]:
    """A decision cannot be more confident than the outlook it rests on.

    Rejected rather than lowered: quietly rewriting the band would store a
    judgement the agent never made, and the stored record would then
    misattribute it — the same rule the research layer applies to ``HIGH``.
    """
    research = selection_input.research
    if confidence_rank(output.confidence) <= confidence_rank(research.confidence):
        return []
    return [
        ValidationIssue(
            "CONFIDENCE_EXCEEDS_RESEARCH",
            f"the decision states {output.confidence.value} confidence over research that "
            f"states {research.confidence.value}; a strategy choice cannot be more certain "
            f"than the view it expresses",
        )
    ]


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------
def _check_reasons(
    output: StrategyAgentOutput,
    selection_input: StrategySelectionInput,
    config: StrategyStageConfig,
) -> list[ValidationIssue]:
    """Every claimed reason must survive contact with the research report.

    Only checks decidable from the report are enforced. Whether a straddle
    suits this particular regime is the agent's judgement; whether the report
    actually names an event inside the horizon is not.
    """
    issues: list[ValidationIssue] = []
    seen: set[StrategySelectionReason] = set()
    for reason in output.reasons:
        if reason in seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_REASON",
                    f"reason {reason.value} is cited more than once",
                    reason.value,
                )
            )
        seen.add(reason)

        problem = _reason_contradicted(reason, output, selection_input, config)
        if problem is not None:
            issues.append(
                ValidationIssue(
                    "UNSUPPORTED_REASON",
                    f"the decision claims {reason.value} but {problem}",
                    reason.value,
                )
            )
    return issues


def _reason_contradicted(
    reason: StrategySelectionReason,
    output: StrategyAgentOutput,
    selection_input: StrategySelectionInput,
    config: StrategyStageConfig,
) -> str | None:
    """Why the research refutes ``reason``, or ``None`` if it does not.

    Deliberately one-sided, exactly as the universe layer's equivalent: enforce
    facts, never opinions.
    """
    research = selection_input.research
    quality = research.data_quality
    option = (
        selection_input.option(output.selected_strategy)
        if output.selected_strategy is not None
        else None
    )

    match reason:
        case StrategySelectionReason.HYPOTHESIS_MATCH:
            if option is not None and research.hypothesis not in option.applicable_hypotheses:
                return (
                    f"{option.strategy_id.value} does not declare hypothesis "
                    f"{research.hypothesis.value}"
                )
        case StrategySelectionReason.DIRECTIONAL_VIEW_SUPPORTED:
            if research.direction not in (Direction.BULLISH, Direction.BEARISH):
                return f"the research states direction {research.direction.value}"
        case StrategySelectionReason.DIRECTION_UNCERTAIN:
            if research.direction not in (Direction.UNCERTAIN, Direction.NEUTRAL):
                return f"the research states a definite direction, {research.direction.value}"
        case StrategySelectionReason.LARGE_MOVE_EXPECTED:
            if research.expected_magnitude not in (
                ExpectedMagnitude.LARGE,
                ExpectedMagnitude.EXTREME,
            ):
                return f"the expected magnitude is {research.expected_magnitude.value}"
        case StrategySelectionReason.MODERATE_MOVE_EXPECTED:
            if research.expected_magnitude not in (
                ExpectedMagnitude.SMALL,
                ExpectedMagnitude.MODERATE,
            ):
                return f"the expected magnitude is {research.expected_magnitude.value}"
        case StrategySelectionReason.EVENT_IN_HORIZON:
            if not research.events_in_horizon:
                return "the research names no event inside the horizon"
        case StrategySelectionReason.NO_EVENT_IN_HORIZON:
            if research.events_in_horizon:
                return (
                    f"the research names {len(research.events_in_horizon)} event(s) inside "
                    f"the horizon"
                )
        case StrategySelectionReason.HORIZON_COMPATIBLE:
            if option is not None and not _covers_horizon(option, research.horizon_days):
                return (
                    f"{option.strategy_id.value} trades a {option.dte_min}-{option.dte_max} "
                    f"day window and the outlook is {research.horizon_days} days"
                )
        case StrategySelectionReason.HORIZON_INCOMPATIBLE:
            if option is not None and _covers_horizon(option, research.horizon_days):
                return (
                    f"{option.strategy_id.value} trades a {option.dte_min}-{option.dte_max} "
                    f"day window, which covers the {research.horizon_days}-day outlook"
                )
        case StrategySelectionReason.CONFIDENCE_SUFFICIENT:
            if research.confidence is ConfidenceLevel.LOW:
                return "the research states LOW confidence"
        case StrategySelectionReason.CONFIDENCE_INSUFFICIENT:
            if research.confidence is not ConfidenceLevel.LOW:
                return f"the research states {research.confidence.value} confidence"
        case StrategySelectionReason.EVIDENCE_SUFFICIENT:
            if quality.evidence_count < config.eligibility.min_evidence_items:
                return (
                    f"the research rests on {quality.evidence_count} evidence item(s), below "
                    f"the configured minimum of {config.eligibility.min_evidence_items}"
                )
        case StrategySelectionReason.EVIDENCE_INSUFFICIENT:
            if quality.evidence_count >= config.eligibility.min_evidence_items:
                return (
                    f"the research rests on {quality.evidence_count} evidence item(s), at or "
                    f"above the configured minimum"
                )
        case StrategySelectionReason.CONTRADICTING_EVIDENCE:
            if quality.contradicting_count == 0:
                return "the research recorded no contradicting evidence"
        case StrategySelectionReason.DATA_QUALITY_INSUFFICIENT:
            if quality.research_usable:
                return "the data layer marked the underlying's record research-usable"
        case StrategySelectionReason.NO_ELIGIBLE_STRATEGY:
            return (
                f"{len(selection_input.eligible_strategies)} strategy(ies) were offered for "
                f"hypothesis {research.hypothesis.value}"
            )
        case StrategySelectionReason.RESEARCH_INCOMPATIBLE:
            # A judgement about fit, not a fact about the report. Never refuted.
            return None
    return None


# ---------------------------------------------------------------------------
# Boundary: the strategy agent does not pick contracts, sizes or money
# ---------------------------------------------------------------------------
def _check_no_contract_selection(output: StrategyAgentOutput) -> list[ValidationIssue]:
    """Catch a contract, a size or an amount smuggled into prose.

    Secondary to the structural guarantee: there is no field on
    :class:`~trading_system.strategies.models.StrategyAgentOutput` that can hold
    a strike, an expiry, a quantity or a price, so none can be *returned*. This
    only stops one being *written into a sentence*, where a later reader — or a
    later milestone — might act on it.
    """
    issues: list[ValidationIssue] = []
    fields = {"rationale": output.rationale, "notes": output.notes}
    guards = (
        (_CONTRACT_INSTRUCTION, "CONTRACT_RECOMMENDED", "names a specific option contract"),
        (_STRIKE_INSTRUCTION, "STRIKE_RECOMMENDED", "names a specific strike"),
        (_EXPIRATION_DATE, "EXPIRATION_RECOMMENDED", "names a specific expiration date"),
        (_QUANTITY, "QUANTITY_STATED", "states a position size"),
        (_MONEY, "ALLOCATION_STATED", "states an amount of money"),
    )
    for name, value in fields.items():
        if not value:
            continue
        for pattern, code, description in guards:
            if pattern.search(value):
                issues.append(
                    ValidationIssue(
                        code,
                        f"{name} {description}; the strategy agent chooses a strategy, and "
                        f"the contract, the size and the money are decided deterministically "
                        f"by later stages",
                        name,
                    )
                )
    return issues
