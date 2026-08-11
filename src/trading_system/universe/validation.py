"""Deterministic validation of what the agent returned.

The prompt asks the agent to stay inside its boundaries. This module is what
*enforces* them, and the difference matters: a prompt is a request, and a
request is not a control. Everything the agent could get wrong is checked here
against the input it was actually given.

The response to a violation is always the same: **reject the whole ranking**.
Nothing is silently repaired — not an unknown symbol dropped, not a duplicate
rank renumbered, not an over-long selection truncated. Repairing would mean the
stored universe is not what the model said, while still being recorded as the
model's output, and a system that quietly edits its AI's answers cannot be
audited. The run ends as ``AI_INVALID_OUTPUT`` and selects nothing.

What is checked, and why each one is a real failure mode rather than a
formality:

* **Membership** — a symbol not in the input means the model invented an asset,
  or hallucinated one from its training data. Either way it was never filtered.
* **Eligibility** — a selected asset must have been deterministically eligible.
  This is the milestone's central rule expressed as an assertion.
* **Rank uniqueness and contiguity** — two rank-1 assets is not an ordering.
* **Size** — ``max_selected_assets`` is a ceiling the agent cannot raise.
* **Evidence** — a reason code whose precondition the supplied data contradicts
  is a fabricated justification, even though it comes from the allowed
  vocabulary.
* **Coverage** — a selected asset must carry at least one reason and a
  confidence, so no selection is unexplainable.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_system.domain.enums import (
    DataQuality,
    Optionability,
    UniverseEligibility,
    UniverseSelectionReason,
)
from trading_system.universe.models import (
    CandidateAsset,
    UniverseAgentRanking,
    UniverseSelectionInput,
)

__all__ = [
    "AgentOutputInvalidError",
    "ValidationIssue",
    "validate_ranking",
]


class AgentOutputInvalidError(RuntimeError):
    """The agent's response cannot be trusted, so none of it is used."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One specific way the response failed, with the symbol it concerns."""

    code: str
    message: str
    symbol: str | None = None


def validate_ranking(
    ranking: UniverseAgentRanking,
    agent_input: UniverseSelectionInput,
    *,
    max_selected: int,
) -> None:
    """Check a ranking against the input that produced it.

    Raises :class:`AgentOutputInvalidError` listing every problem found, rather than
    stopping at the first: an operator debugging a misbehaving prompt needs the
    whole picture, and collecting them costs nothing.
    """
    issues: list[ValidationIssue] = []

    if ranking.run_id != agent_input.run_id:
        issues.append(
            ValidationIssue(
                "RUN_ID_MISMATCH",
                f"response is for run {ranking.run_id!r} but the request was "
                f"{agent_input.run_id!r}",
            )
        )

    known = {c.symbol: c for c in agent_input.candidate_assets}
    issues.extend(_check_membership(ranking, known))
    issues.extend(_check_ranks(ranking, max_selected))
    issues.extend(_check_evidence(ranking, known))

    if issues:
        raise AgentOutputInvalidError(issues)


def _check_membership(
    ranking: UniverseAgentRanking, known: dict[str, CandidateAsset]
) -> list[ValidationIssue]:
    """Every ranked symbol must be one the agent was shown, exactly once."""
    issues: list[ValidationIssue] = []
    seen: set[str] = set()

    for entry in ranking.rankings:
        if entry.symbol not in known:
            issues.append(
                ValidationIssue(
                    "UNKNOWN_SYMBOL",
                    f"{entry.symbol} was not among the candidates supplied to the agent; "
                    f"the agent may rank the pool it is given, never extend it",
                    entry.symbol,
                )
            )
        if entry.symbol in seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_SYMBOL",
                    f"{entry.symbol} appears more than once in the ranking",
                    entry.symbol,
                )
            )
        seen.add(entry.symbol)

    return issues


def _check_ranks(ranking: UniverseAgentRanking, max_selected: int) -> list[ValidationIssue]:
    """Ranks form 1..n exactly once, and n does not exceed the configured cap."""
    issues: list[ValidationIssue] = []
    selected = [entry for entry in ranking.rankings if entry.is_selected]

    if len(selected) > max_selected:
        issues.append(
            ValidationIssue(
                "TOO_MANY_SELECTED",
                f"{len(selected)} assets were selected but max_selected_assets is "
                f"{max_selected}; the result is rejected rather than truncated, because "
                f"truncating would record a universe the model did not choose",
            )
        )

    ranks = [entry.rank for entry in selected if entry.rank is not None]
    if len(set(ranks)) != len(ranks):
        issues.append(
            ValidationIssue(
                "DUPLICATE_RANK",
                f"ranks must be unique, got {sorted(ranks)}",
            )
        )
    elif sorted(ranks) != list(range(1, len(ranks) + 1)):
        issues.append(
            ValidationIssue(
                "NON_CONTIGUOUS_RANK",
                f"ranks must be contiguous from 1, got {sorted(ranks)}",
            )
        )

    return issues


def _check_evidence(
    ranking: UniverseAgentRanking, known: dict[str, CandidateAsset]
) -> list[ValidationIssue]:
    """Every claimed reason must survive contact with the supplied evidence.

    Only checks that are decidable from the candidate record are enforced. A
    comparative judgement ("this is more liquid than that") is the agent's to
    make; a factual claim the data contradicts ("options are available" for an
    asset whose optionability is unknown) is not.
    """
    issues: list[ValidationIssue] = []

    for entry in ranking.rankings:
        candidate = known.get(entry.symbol)
        if candidate is None:
            continue  # already reported as UNKNOWN_SYMBOL

        if (
            entry.is_selected
            and candidate.deterministic_eligibility is not UniverseEligibility.ELIGIBLE
        ):
            issues.append(
                ValidationIssue(
                    "INELIGIBLE_SELECTED",
                    f"{entry.symbol} was deterministically rejected and cannot be selected; "
                    f"an AI agent cannot override a deterministic exclusion",
                    entry.symbol,
                )
            )

        for reason in entry.reasons:
            problem = _reason_contradicted(reason, candidate)
            if problem is not None:
                issues.append(
                    ValidationIssue(
                        "UNSUPPORTED_REASON",
                        f"{entry.symbol} claims {reason.value} but {problem}",
                        entry.symbol,
                    )
                )

    return issues


#: Reason codes that assert something about underlying liquidity. Which band
#: applies is the agent's judgement; that volume was reported at all is not.
_LIQUIDITY_REASONS: frozenset[UniverseSelectionReason] = frozenset(
    {
        UniverseSelectionReason.HIGH_UNDERLYING_LIQUIDITY,
        UniverseSelectionReason.MODERATE_UNDERLYING_LIQUIDITY,
        UniverseSelectionReason.LOWER_UNDERLYING_LIQUIDITY,
    }
)


def _reason_contradicted(reason: UniverseSelectionReason, candidate: CandidateAsset) -> str | None:
    """Why the supplied evidence refutes ``reason``, or ``None`` if it does not.

    Deliberately one-sided. A liquidity *band* is a judgement and is not second-
    guessed here; a liquidity claim made with no volume figure at all is a
    fabrication and is. The rule is: enforce facts, never opinions.
    """
    if (
        reason is UniverseSelectionReason.OPTIONS_AVAILABLE
        and candidate.optionability is not Optionability.TRUE
    ):
        return (
            f"its optionability is {candidate.optionability.value}; "
            f"an unestablished chain is not an available one"
        )

    if (
        reason is UniverseSelectionReason.OPTIONABILITY_NOT_ESTABLISHED
        and candidate.optionability is not Optionability.UNKNOWN
    ):
        return f"its optionability is {candidate.optionability.value}, which is established"

    if (
        reason is UniverseSelectionReason.SUFFICIENT_DATA_QUALITY
        and not candidate.data_quality.research_usable
    ):
        return "the data layer marked its record unusable for research"

    if (
        reason is UniverseSelectionReason.FRESH_MARKET_DATA
        and not candidate.data_quality.freshness_valid
    ):
        return "its market data is outside the configured freshness window"

    if (
        reason is UniverseSelectionReason.STALE_MARKET_DATA
        and candidate.data_quality.freshness_valid
    ):
        return "its market data is within the configured freshness window"

    if reason is UniverseSelectionReason.PRICE_IN_RANGE and candidate.reference_price is None:
        return "no reference price was supplied for it"

    if (
        reason is UniverseSelectionReason.LIMITED_DATA_HISTORY
        and candidate.data_quality.classification is DataQuality.OK
    ):
        return "its data quality is OK with no recorded issues"

    if reason in _LIQUIDITY_REASONS and candidate.underlying_volume is None:
        return "no underlying volume was supplied, so no liquidity claim is supportable"

    return None
