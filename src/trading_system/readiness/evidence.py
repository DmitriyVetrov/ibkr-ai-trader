"""Captured readiness evidence (Milestone 12).

Everything the evaluator is allowed to look at, and nothing else. An
:class:`EvidenceBundle` is a frozen snapshot: the evaluator receives one and
reads it, and cannot run a command, open a connection or consult a clock of its
own. That is what makes ``evaluate(bundle)`` reproducible — a stored assessment
can be recomputed from its stored evidence and must reach the same verdict.

The shape follows :class:`~trading_system.operations.health.HealthInputs` and
:class:`~trading_system.risk.models.AccountSnapshot` for the same reason both
of those are frozen bundles rather than live lookups: a verdict that fetched
its own inputs cannot be reproduced, and the interesting verdicts are exactly
the ones somebody wants to reproduce.

**An absent record is not a negative record.** ``EvidenceBundle.get`` returns
``None`` when nothing was collected, and the evaluator renders that
``NOT_TESTED``. A collector that failed records an evidence record *saying so*,
which is a different thing again: "we could not look" and "we did not look" are
both distinguishable from "we looked and it was wrong".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import ReadinessEvidenceKind

__all__ = [
    "EvidenceBundle",
    "EvidenceRecord",
    "evidence_identifier",
]


def evidence_identifier(
    *,
    kind: ReadinessEvidenceKind,
    source: str,
    observed_at: datetime,
    detail_digest: str,
) -> str:
    """Derive one evidence record's identity from what it observed.

    Includes the observation instant, deliberately and unlike most identifiers
    in this system. Evidence is a *measurement*: two probes of the same
    endpoint a minute apart are two facts, not one fact observed twice, and
    collapsing them would make a stale reading indistinguishable from a fresh
    one that happened to agree.
    """
    digest = stable_hash(
        ["READINESS_EVIDENCE", kind.value, source, observed_at.isoformat(), detail_digest]
    )
    return f"evidence-{digest[:20]}"


class EvidenceRecord(BaseModel):
    """One observation, with enough detail to reproduce it.

    ``detail`` deliberately carries the raw shape of what was observed — an
    exit code, a count, a status string, an artifact id — rather than a
    pre-digested boolean. Brief section 27 asks that a criterion never claim
    ``PASS`` without evidence; recording only the conclusion would satisfy the
    letter of that and none of its purpose, because nobody could check the
    conclusion afterwards.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    kind: ReadinessEvidenceKind
    #: What produced this. A command line, a service URL, an artifact id, a
    #: store path. Never a secret — collectors mask before recording.
    source: str
    observed_at: datetime

    #: Whether the observation itself succeeded. A failed probe is still
    #: evidence, and its ``detail`` says what went wrong.
    collected: bool = True
    #: Present when ``collected`` is false.
    error: str | None = None

    #: The observation. Values are JSON-serialisable scalars, lists or dicts.
    detail: dict[str, Any] = Field(default_factory=dict)

    #: The git revision this observation belongs to, when it is revision-bound
    #: evidence. ``None`` for evidence about a running deployment.
    git_revision: str | None = None

    #: Ids of immutable domain artifacts this evidence points at, so a reader
    #: can go from the assessment to the record that justified it.
    artifact_ids: tuple[str, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "observed_at must be timezone-aware. A naive instant cannot be compared "
                "against a freshness window without assuming a timezone, and the assumption "
                "is wrong for most of the day."
            )
        return value.astimezone(UTC)

    @classmethod
    def of(
        cls,
        *,
        kind: ReadinessEvidenceKind,
        source: str,
        observed_at: datetime,
        collected: bool = True,
        error: str | None = None,
        detail: Mapping[str, Any] | None = None,
        git_revision: str | None = None,
        artifact_ids: Iterable[str] = (),
    ) -> EvidenceRecord:
        """Build a record, deriving its content-addressed id."""
        payload = dict(detail or {})
        identifier = evidence_identifier(
            kind=kind,
            source=source,
            observed_at=observed_at,
            detail_digest=stable_hash([payload, collected, error]),
        )
        return cls(
            evidence_id=identifier,
            kind=kind,
            source=source,
            observed_at=observed_at,
            collected=collected,
            error=error,
            detail=payload,
            git_revision=git_revision,
            artifact_ids=tuple(artifact_ids),
        )

    def age_seconds(self, as_of: datetime) -> float:
        """How old this observation is at ``as_of``. Never negative.

        Clamped at zero rather than reported as a negative age: evidence
        recorded microseconds ahead of the assessment instant is an ordinary
        consequence of two clock reads, not a fact from the future.
        """
        return max(0.0, (as_of - self.observed_at).total_seconds())


class EvidenceBundle(BaseModel):
    """Everything one readiness evaluation is allowed to see.

    Keyed by an opaque evidence *slot* name rather than by criterion id, so
    several criteria can be judged from one observation. A single
    ``reconciliation`` run answers "does reconciliation run", "are there
    critical findings" and "are there unresolved unknown executions"; making
    each criterion collect its own would be three broker reads to answer one
    question three ways.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The instant the evidence was assembled and the assessment is made *as
    #: of*. Every freshness comparison is against this, never against a fresh
    #: clock read — so an assessment recomputed tomorrow reaches the same
    #: verdict as it did today.
    as_of: datetime

    #: The revision the assessment describes.
    git_revision: str | None = None
    working_tree_clean: bool | None = None

    records: dict[str, EvidenceRecord] = Field(default_factory=dict)

    #: Slots a collector was deliberately asked not to fill, with the reason.
    #: Distinct from an absent slot: "the operator did not request the broker
    #: probe" and "the broker probe was requested and produced nothing" are
    #: different facts about a run.
    not_collected: dict[str, str] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(UTC)

    def get(self, slot: str) -> EvidenceRecord | None:
        """The record in ``slot``, or ``None`` if nothing was collected."""
        return self.records.get(slot)

    def skip_reason(self, slot: str) -> str | None:
        """Why ``slot`` was deliberately not collected, if it was not."""
        return self.not_collected.get(slot)

    def with_record(self, slot: str, record: EvidenceRecord) -> EvidenceBundle:
        """A copy carrying one more record.

        Reconstructs rather than ``model_copy``-ing so the validators run
        again — the same reasoning ``Reservation.with_event`` records, applied
        to a bundle that a stored assessment will be reproduced from.
        """
        records = dict(self.records)
        records[slot] = record
        return EvidenceBundle(
            as_of=self.as_of,
            git_revision=self.git_revision,
            working_tree_clean=self.working_tree_clean,
            records=records,
            not_collected={k: v for k, v in self.not_collected.items() if k != slot},
        )

    def with_skip(self, slot: str, reason: str) -> EvidenceBundle:
        """A copy recording that ``slot`` was deliberately not collected."""
        skipped = dict(self.not_collected)
        skipped[slot] = reason
        return EvidenceBundle(
            as_of=self.as_of,
            git_revision=self.git_revision,
            working_tree_clean=self.working_tree_clean,
            records={k: v for k, v in self.records.items() if k != slot},
            not_collected=skipped,
        )

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """Every evidence id in the bundle, in a deterministic order."""
        return tuple(self.records[slot].evidence_id for slot in sorted(self.records))

    def digest(self) -> str:
        """A content hash of the whole bundle, for run identity.

        Deliberately includes the evidence ids rather than the raw payloads:
        the ids are already content-derived, and hashing them keeps the digest
        stable against a reordering of dictionary keys.
        """
        return stable_hash(
            [
                "READINESS_EVIDENCE_BUNDLE",
                self.as_of.isoformat(),
                self.git_revision,
                self.working_tree_clean,
                [(slot, self.records[slot].evidence_id) for slot in sorted(self.records)],
                sorted(self.not_collected.items()),
            ]
        )
