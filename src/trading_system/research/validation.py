"""Deterministic validation of what the research agent returned.

The prompt asks the agent to stay inside its boundaries. This module is what
*enforces* them, and the difference matters: a prompt is a request, and a
request is not a control. Everything the agent could get wrong is checked here
against the input it was actually given.

The response to a violation is always the same: **reject the whole report**.
Nothing is repaired — a fabricated citation is not dropped, an over-confident
band is not quietly downgraded, an unsupported hypothesis is not swapped for
``E``. Repairing would store an outlook the model did not produce while
recording it as the model's output, which is a fiction that looks exactly like
a clean run. The symbol ends as ``SEMANTIC_VALIDATION_FAILED`` with no
hypothesis at all.

What is checked, and why each is a real failure mode rather than a formality:

* **Citations exist.** Every ``evidence_id`` and ``event_id`` must name
  something the input actually carried. An id the input does not contain means
  the model invented a source — the single most dangerous failure available to
  a research agent, and the one no amount of prompting prevents.
* **The hypothesis is earned.** ``B`` needs upward evidence, ``C`` needs
  downward evidence, ``A`` needs evidence of an elevated *move* that is not a
  dated catalyst, ``D`` needs a specific identifiable event inside the horizon
  with its announcement recorded, and ``E`` needs an explanation. An
  unsupported hypothesis is a conclusion with nothing under it.
* **A and D stay distinct.** They are different claims — a regime versus a
  catalyst — and collapsing them loses the only thing that distinguishes them
  operationally. An ``A`` justified by a material dated event inside the
  horizon is a ``D`` that was labelled wrongly.
* **Direction agrees with the hypothesis.** A "predominantly bullish" report
  whose direction says ``BEARISH`` is not a disagreement worth preserving, it
  is a contradiction in the output.
* **Confidence is licensed.** ``HIGH`` is not the model's to declare. It
  requires enough evidence, at a good enough tier, over data the quality engine
  vouched for, with any contradiction actually resolved — all configurable in
  ``config/research.yaml``.
* **The horizon is respected.** An outlook over 200 days answers a question
  nobody asked and no 14-31 day option can express.
* **No contract is recommended.** Structurally there is no field for a strike;
  a narrow textual guard additionally catches an instruction smuggled into
  prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trading_system.domain.enums import (
    ConfidenceLevel,
    Direction,
    EvidenceDirection,
    EvidenceStance,
    MarketHypothesis,
    RelevanceLevel,
    SourceTier,
)
from trading_system.infrastructure.settings import ResearchConfig
from trading_system.research.models import (
    ResearchAgentOutput,
    ResearchInput,
    tier_rank,
)

__all__ = [
    "ResearchOutputInvalidError",
    "ValidationIssue",
    "validate_agent_output",
]


class ResearchOutputInvalidError(RuntimeError):
    """The agent's outlook cannot be trusted, so none of it is used."""

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


#: Prose that would amount to selecting a contract. Deliberately narrow: the
#: agent is *encouraged* to discuss implied volatility and expiries in the
#: abstract, and a broad keyword ban would reject legitimate analysis. The
#: structural guarantee is that no output field can hold a contract; this only
#: catches an instruction smuggled into a sentence.
_CONTRACT_INSTRUCTION = re.compile(
    r"\b(?:buy|sell|purchase|write|short|go\s+long)\b[^.]{0,60}?"
    r"\b\d+(?:\.\d+)?\s*(?:strike\s+)?(?:call|put)s?\b",
    re.IGNORECASE,
)

#: Strategy names the strategy agent owns (Milestone 6). Research classifies a
#: hypothesis; choosing the instrument that expresses it is a later stage.
_STRATEGY_TOKENS = ("LONG_CALL", "LONG_PUT", "LONG_STRADDLE", "LONG_STRANGLE")

#: Directions consistent with each hypothesis. ``A`` and ``D`` both assert that
#: direction cannot be established, so claiming one contradicts the hypothesis.
_ALLOWED_DIRECTIONS: dict[MarketHypothesis, frozenset[Direction]] = {
    MarketHypothesis.A: frozenset({Direction.UNCERTAIN}),
    MarketHypothesis.B: frozenset({Direction.BULLISH}),
    MarketHypothesis.C: frozenset({Direction.BEARISH}),
    MarketHypothesis.D: frozenset({Direction.UNCERTAIN}),
    MarketHypothesis.E: frozenset(
        {Direction.NEUTRAL, Direction.UNCERTAIN, Direction.BULLISH, Direction.BEARISH}
    ),
}


def validate_agent_output(
    output: ResearchAgentOutput,
    research_input: ResearchInput,
    *,
    config: ResearchConfig,
) -> None:
    """Check an outlook against the input that produced it and the policy.

    Raises :class:`ResearchOutputInvalidError` listing every problem found,
    rather than stopping at the first: an operator debugging a misbehaving
    prompt needs the whole picture, and collecting them costs nothing.
    """
    issues: list[ValidationIssue] = []

    issues.extend(_check_identity(output, research_input))
    issues.extend(_check_citations(output, research_input))
    issues.extend(_check_horizon(output, research_input))
    issues.extend(_check_hypothesis(output, research_input))
    issues.extend(_check_direction(output))
    issues.extend(_check_confidence(output, research_input, config))
    issues.extend(_check_sources(output, research_input, config))
    issues.extend(_check_no_contract_selection(output))

    if issues:
        raise ResearchOutputInvalidError(issues)


# ---------------------------------------------------------------------------
# Identity and citations
# ---------------------------------------------------------------------------
def _check_identity(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> list[ValidationIssue]:
    """A stale response for another run or symbol is not this run's answer."""
    issues: list[ValidationIssue] = []
    if output.run_id != research_input.run_id:
        issues.append(
            ValidationIssue(
                "RUN_ID_MISMATCH",
                f"response is for run {output.run_id!r} but the request was "
                f"{research_input.run_id!r}",
            )
        )
    if output.symbol != research_input.symbol:
        issues.append(
            ValidationIssue(
                "SYMBOL_MISMATCH",
                f"response is about {output.symbol!r} but the request was about "
                f"{research_input.symbol!r}; research contexts are isolated per underlying",
            )
        )
    return issues


def _check_citations(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> list[ValidationIssue]:
    """Every citation must name a fact the input actually carried.

    This is the anti-fabrication check. An evidence id that is not in the input
    means the model produced a source from its own memory — and a source it did
    not receive is a source it did not consult, whatever it says.
    """
    issues: list[ValidationIssue] = []
    known_evidence = research_input.evidence_ids
    known_events = research_input.event_ids

    for evidence_id in sorted(set(output.cited_evidence_ids)):
        if evidence_id not in known_evidence:
            issues.append(
                ValidationIssue(
                    "FABRICATED_EVIDENCE",
                    f"evidence id {evidence_id!r} was not among the facts supplied for "
                    f"{research_input.symbol}; an agent may cite the evidence it is given, "
                    f"never invent one",
                    evidence_id,
                )
            )

    seen: set[str] = set()
    for item in output.evidence:
        if item.evidence_id in seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_EVIDENCE",
                    f"evidence id {item.evidence_id!r} is assessed more than once; "
                    f"one fact weighed twice is two votes for one observation",
                    item.evidence_id,
                )
            )
        seen.add(item.evidence_id)

    event_seen: set[str] = set()
    for event in output.key_events:
        if event.event_id not in known_events:
            issues.append(
                ValidationIssue(
                    "FABRICATED_EVENT",
                    f"event id {event.event_id!r} was not among the events supplied for "
                    f"{research_input.symbol}; claiming an event exists without evidence "
                    f"is inventing a catalyst",
                    event.event_id,
                )
            )
        if event.event_id in event_seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_EVENT",
                    f"event id {event.event_id!r} is assessed more than once",
                    event.event_id,
                )
            )
        event_seen.add(event.event_id)

    if research_input.usable_evidence_count > 0 and not output.evidence:
        issues.append(
            ValidationIssue(
                "NO_EVIDENCE_ASSESSED",
                f"{len(research_input.all_evidence)} facts were supplied and none was "
                f"assessed; a conclusion that references nothing cannot be audited",
            )
        )

    return issues


def _check_horizon(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> list[ValidationIssue]:
    """The outlook must answer the question that was asked."""
    horizon = research_input.horizon
    if horizon.contains(output.horizon_days):
        return []
    return [
        ValidationIssue(
            "HORIZON_OUT_OF_RANGE",
            f"horizon_days {output.horizon_days} is outside the configured "
            f"{horizon.min_days}-{horizon.max_days} day window; a long-term thesis is not "
            f"an answer to a short-term question",
        )
    ]


# ---------------------------------------------------------------------------
# Hypothesis semantics
# ---------------------------------------------------------------------------
def _check_hypothesis(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> list[ValidationIssue]:
    """Each hypothesis must be supported by the kind of evidence it claims."""
    hypothesis = output.hypothesis
    supporting = [i for i in output.evidence if i.stance is EvidenceStance.SUPPORTS]

    if hypothesis is MarketHypothesis.B:
        if not any(i.direction is EvidenceDirection.SUPPORTS_UP for i in supporting):
            return [
                ValidationIssue(
                    "UNSUPPORTED_HYPOTHESIS_B",
                    "hypothesis B (predominantly up) requires at least one supporting "
                    "evidence item pointing SUPPORTS_UP; a directional bias with no "
                    "directional evidence is an assertion, not a conclusion",
                )
            ]
        return []

    if hypothesis is MarketHypothesis.C:
        if not any(i.direction is EvidenceDirection.SUPPORTS_DOWN for i in supporting):
            return [
                ValidationIssue(
                    "UNSUPPORTED_HYPOTHESIS_C",
                    "hypothesis C (predominantly down) requires at least one supporting "
                    "evidence item pointing SUPPORTS_DOWN",
                )
            ]
        return []

    if hypothesis is MarketHypothesis.A:
        return _check_hypothesis_a(output, research_input)

    if hypothesis is MarketHypothesis.D:
        return _check_hypothesis_d(output, research_input)

    if hypothesis is MarketHypothesis.E and not (output.explanation or "").strip():
        return [
            ValidationIssue(
                "MISSING_EXPLANATION",
                "hypothesis E requires a structured explanation of why the situation "
                "fits none of A-D",
            )
        ]
    return []


def _check_hypothesis_a(
    output: ResearchAgentOutput,
    research_input: ResearchInput,
) -> list[ValidationIssue]:
    """``A``: an elevated move with no required catalyst.

    Two things have to hold. There must be evidence of an elevated *magnitude*,
    and that evidence must not itself be a dated event — because an expected
    move that hangs on a specific announcement is exactly what ``D`` is for, and
    the whole point of keeping the two apart is that they imply different
    timing.
    """
    issues: list[ValidationIssue] = []
    large_move = [
        item
        for item in output.evidence
        if item.direction is EvidenceDirection.SUPPORTS_LARGE_MOVE
        and item.stance is EvidenceStance.SUPPORTS
    ]
    if not large_move:
        issues.append(
            ValidationIssue(
                "UNSUPPORTED_HYPOTHESIS_A",
                "hypothesis A (strong move, direction unknown) requires at least one "
                "supporting evidence item pointing SUPPORTS_LARGE_MOVE; uncertainty about "
                "direction is not by itself a reason to expect a large move",
            )
        )
    else:
        non_event = [
            item
            for item in large_move
            if (found := research_input.evidence(item.evidence_id)) is not None
            and not found.kind.is_dated_event
        ]
        if not non_event:
            issues.append(
                ValidationIssue(
                    "HYPOTHESIS_A_RESTS_ON_AN_EVENT",
                    "hypothesis A rests only on dated events, which is hypothesis D. A "
                    "claims an elevated move that needs no specific catalyst, so its "
                    "evidence must include something other than a scheduled event",
                )
            )

    material = [
        assessment
        for assessment in output.key_events
        if assessment.expected_relevance is RelevanceLevel.HIGH
        and (event := research_input.event(assessment.event_id)) is not None
        and event.within_horizon
    ]
    if material:
        issues.append(
            ValidationIssue(
                "HYPOTHESIS_A_NAMES_A_MATERIAL_EVENT",
                f"hypothesis A names {len(material)} highly relevant event(s) inside the "
                f"horizon ({', '.join(sorted(e.event_id for e in material))}); an expected "
                f"move around an identified event is hypothesis D, and the distinction "
                f"decides how the position would later be timed",
            )
        )
    return issues


def _check_hypothesis_d(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> list[ValidationIssue]:
    """``D``: a sharp move around a specific, identifiable, dated event.

    Every mandatory event field must be present (brief section 57). They come
    from the input rather than from the model, so a missing ``announced_at``
    means the data never established that the event was knowable — not that the
    agent forgot to say so.
    """
    if not output.key_events:
        return [
            ValidationIssue(
                "HYPOTHESIS_D_WITHOUT_EVENT",
                "hypothesis D (sharp move after a specific event) requires the report to "
                "identify that event. Without one, this is normal volatility and belongs "
                "to hypothesis A",
            )
        ]

    issues: list[ValidationIssue] = []
    qualifying = 0
    for assessment in output.key_events:
        event = research_input.event(assessment.event_id)
        if event is None:
            continue  # already reported as FABRICATED_EVENT
        missing = [
            name
            for name, value in (
                ("announced_at", event.announced_at),
                ("expected_event_time", event.expected_event_time),
                ("source", event.source.provider),
                ("source_tier", event.source.source_tier),
            )
            if value is None
        ]
        if missing:
            issues.append(
                ValidationIssue(
                    "INCOMPLETE_EVENT_FOR_HYPOTHESIS_D",
                    f"event {event.event_id} is missing {', '.join(missing)}; an event "
                    f"with no recorded announcement was never established to be knowable "
                    f"at the research instant and cannot carry an event-driven thesis",
                    event.event_id,
                )
            )
            continue
        if not event.within_horizon:
            continue
        qualifying += 1

    if qualifying == 0 and not issues:
        issues.append(
            ValidationIssue(
                "HYPOTHESIS_D_EVENT_OUTSIDE_HORIZON",
                f"hypothesis D names no event inside the "
                f"{research_input.horizon.min_days}-{research_input.horizon.max_days} day "
                f"horizon; an event after the horizon cannot produce a move within it",
            )
        )
    return issues


def _check_direction(output: ResearchAgentOutput) -> list[ValidationIssue]:
    """The stated direction must agree with the hypothesis it accompanies."""
    allowed = _ALLOWED_DIRECTIONS[output.hypothesis]
    if output.direction in allowed:
        return []
    return [
        ValidationIssue(
            "DIRECTION_CONTRADICTS_HYPOTHESIS",
            f"hypothesis {output.hypothesis.value} allows direction "
            f"{sorted(d.value for d in allowed)} but the report states "
            f"{output.direction.value}",
        )
    ]


# ---------------------------------------------------------------------------
# Confidence and sources
# ---------------------------------------------------------------------------
def _check_confidence(
    output: ResearchAgentOutput,
    research_input: ResearchInput,
    config: ResearchConfig,
) -> list[ValidationIssue]:
    """Confidence is constrained by the evidence, not declared by the model.

    Every threshold here is configurable. The report is rejected rather than
    downgraded: quietly rewriting ``HIGH`` to ``MEDIUM`` would store a judgement
    the agent never made, and the stored record would then misattribute it.
    """
    policy = config.confidence
    issues: list[ValidationIssue] = []
    evidence_count = len(output.evidence)

    if (
        output.confidence is not ConfidenceLevel.LOW
        and evidence_count < policy.min_evidence_items_for_medium
    ):
        issues.append(
            ValidationIssue(
                "CONFIDENCE_ABOVE_LOW_WITHOUT_EVIDENCE",
                f"confidence {output.confidence.value} requires at least "
                f"{policy.min_evidence_items_for_medium} evidence item(s); "
                f"{evidence_count} were assessed",
            )
        )

    if output.confidence is not ConfidenceLevel.HIGH:
        return issues

    if evidence_count < policy.min_evidence_items_for_high:
        issues.append(
            ValidationIssue(
                "HIGH_CONFIDENCE_TOO_LITTLE_EVIDENCE",
                f"HIGH confidence requires at least {policy.min_evidence_items_for_high} "
                f"evidence items; {evidence_count} were assessed",
            )
        )

    if policy.require_research_usable_for_high and not (
        research_input.data_quality_summary.research_usable
    ):
        issues.append(
            ValidationIssue(
                "HIGH_CONFIDENCE_ON_UNUSABLE_DATA",
                "HIGH confidence requires market data the quality engine marked "
                "research-usable; this underlying's record was not",
            )
        )

    gap_count = research_input.data_quality_summary.gap_count
    if gap_count > policy.max_data_gaps_for_high:
        issues.append(
            ValidationIssue(
                "HIGH_CONFIDENCE_WITH_MISSING_DATA",
                f"HIGH confidence is refused with {gap_count} recorded data gaps "
                f"(limit {policy.max_data_gaps_for_high}): "
                f"{', '.join(g.value for g in research_input.data_quality_summary.gaps)}",
            )
        )

    best = _best_cited_tier(output, research_input)
    if best is None or tier_rank(best) > tier_rank(policy.min_source_tier_for_high):
        issues.append(
            ValidationIssue(
                "HIGH_CONFIDENCE_ON_LOW_TIER_SOURCES",
                f"HIGH confidence requires at least one cited source at "
                f"{policy.min_source_tier_for_high.value} or better; the best cited tier "
                f"is {best.value if best else 'none'}",
            )
        )

    if (
        policy.require_contradiction_resolution_for_high
        and output.contradicting
        and not (output.contradiction_resolution or "").strip()
    ):
        issues.append(
            ValidationIssue(
                "HIGH_CONFIDENCE_WITH_UNRESOLVED_CONTRADICTION",
                f"HIGH confidence is refused while {len(output.contradicting)} contradicting "
                f"item(s) stand unexplained; say why one side dominates, or classify the "
                f"conflict as E",
            )
        )

    return issues


def _best_cited_tier(
    output: ResearchAgentOutput, research_input: ResearchInput
) -> SourceTier | None:
    """The most trusted tier among the evidence the report actually cited."""
    tiers = [
        found.source.source_tier
        for item in output.evidence
        if (found := research_input.evidence(item.evidence_id)) is not None
    ]
    return min(tiers, key=tier_rank) if tiers else None


def _check_sources(
    output: ResearchAgentOutput,
    research_input: ResearchInput,
    config: ResearchConfig,
) -> list[ValidationIssue]:
    """Source attribution, per ``config/sources.yaml`` and the research policy.

    The stricter of the two floors wins: ``sources.yaml`` states the project's
    minimum for any research report, and ``research.yaml`` may only tighten it.
    """
    policy = research_input.source_policy
    required = max(policy.min_sources_per_report, config.evidence.min_distinct_sources)
    if required == 0 or not policy.require_source_attribution:
        return []

    available = len(research_input.distinct_sources())
    cited = {
        (found.source.source_name or found.source.provider)
        for item in output.evidence
        if (found := research_input.evidence(item.evidence_id)) is not None
    }
    # Never demand more distinct sources than the input actually contained:
    # that would push the agent to cite something it was not given.
    floor = min(required, available)
    if len(cited) < floor:
        return [
            ValidationIssue(
                "TOO_FEW_DISTINCT_SOURCES",
                f"the report cites {len(cited)} distinct source(s) but the policy requires "
                f"{floor} of the {available} available",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Boundary: research does not pick contracts
# ---------------------------------------------------------------------------
def _check_no_contract_selection(output: ResearchAgentOutput) -> list[ValidationIssue]:
    """Catch a contract recommendation smuggled into prose.

    Secondary to the structural guarantee: there is no field on
    :class:`~trading_system.research.models.ResearchAgentOutput` that can hold a
    strike, an expiry or a right, so a contract cannot be *returned*. This only
    stops one being *written into a sentence* — deliberately narrow, because
    discussing implied volatility and expiries in the abstract is exactly what
    the agent is for, and a broad keyword ban would reject legitimate analysis.
    """
    issues: list[ValidationIssue] = []
    fields = {
        "thesis": output.thesis,
        "expected_behavior": output.expected_behavior,
        "explanation": output.explanation,
        "contradiction_resolution": output.contradiction_resolution,
        "notes": output.notes,
    }
    for name, value in fields.items():
        if not value:
            continue
        if _CONTRACT_INSTRUCTION.search(value):
            issues.append(
                ValidationIssue(
                    "CONTRACT_RECOMMENDED",
                    f"{name} recommends a specific option contract; research produces an "
                    f"outlook, and contract selection is deterministic and belongs to a "
                    f"later stage",
                    name,
                )
            )
        upper = value.upper()
        named = [token for token in _STRATEGY_TOKENS if token in upper]
        if named:
            issues.append(
                ValidationIssue(
                    "STRATEGY_RECOMMENDED",
                    f"{name} names the strategy {', '.join(named)}; choosing the instrument "
                    f"that expresses a hypothesis is the strategy agent's job",
                    name,
                )
            )
    return issues
