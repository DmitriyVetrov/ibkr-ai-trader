"""Immutable readiness artifacts (Milestone 12).

Four records, and the boundaries between them are the milestone:

.. code-block:: text

    ReadinessCriterion    one question, one status, one piece of evidence
    ReadinessAssessment   every criterion, plus the level they add up to
    ReadinessRun          one evaluation: when, at what revision, by what policy
    LiveReadinessSignoff  a human decision about an assessment. Enables NOTHING.

The model validators are where the milestone's safety claims stop being prose.
A ``ReadinessCriterion`` that reads ``PASS`` **cannot be constructed** without
an evidence id, so "every PASS has evidence" is a property of the type rather
than a discipline. A ``ReadinessAssessment`` at ``READY_FOR_PAPER`` cannot
carry an unsatisfied blocking criterion. A ``LiveReadinessSignoff`` cannot
record ``SIGNED`` with no signer, because a sign-off whose signer the
environment guessed is not a sign-off.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    READINESS_INCONCLUSIVE_STATUSES,
    READINESS_LEVEL_ORDER,
    ReadinessCriterionId,
    ReadinessDomain,
    ReadinessLevel,
    ReadinessReasonCode,
    ReadinessRunStatus,
    ReadinessStatus,
    SignoffStatus,
    TradingMode,
)

__all__ = [
    "LiveReadinessSignoff",
    "ReadinessAssessment",
    "ReadinessCriterion",
    "ReadinessRun",
    "assessment_identifier",
    "run_identifier",
    "signoff_identifier",
]

READINESS_SCHEMA_VERSION = "2026.08.15-1"

#: What a sign-off records when the environment cannot establish who signed.
#: Never a guess from ``$USER`` or a git config anybody can set — an inferred
#: signer is worse than no signer, because it looks like accountability.
IDENTITY_NOT_AVAILABLE = "NOT_AVAILABLE"


def assessment_identifier(
    *,
    git_revision: str | None,
    as_of: datetime,
    evidence_digest: str,
    level: str,
    criteria_digest: str,
    schema_version: str = READINESS_SCHEMA_VERSION,
) -> str:
    """Derive one assessment's identity from its evidence *and its conclusion*.

    The conclusion is in the digest deliberately. This repository has learned
    the same lesson three times — allocation records it about the campaign's
    committed state, execution about the ledger's, profit and loss about
    settlement outcomes — and it is the same lesson here: two runs over
    superficially similar inputs that reach *different verdicts* are different
    facts, and an id derived from the inputs alone would collide them and the
    immutable store would correctly refuse the second.
    """
    digest = stable_hash(
        [
            "READINESS_ASSESSMENT",
            schema_version,
            git_revision,
            as_of.isoformat(),
            evidence_digest,
            level,
            criteria_digest,
        ]
    )
    return f"readiness-{digest[:20]}"


def run_identifier(
    *,
    assessment_id: str,
    as_of: datetime,
    status: str,
    schema_version: str = READINESS_SCHEMA_VERSION,
) -> str:
    """Derive one run's identity from the assessment it produced."""
    digest = stable_hash(
        ["READINESS_RUN", schema_version, assessment_id, as_of.isoformat(), status]
    )
    return f"readiness-run-{digest[:20]}"


def signoff_identifier(
    *,
    readiness_run_id: str,
    git_revision: str | None,
    signed_by: str,
    signed_at: datetime,
    status: str,
    schema_version: str = READINESS_SCHEMA_VERSION,
) -> str:
    """Derive one sign-off's identity from who signed what, and when.

    Includes the instant, unlike most identifiers here: signing the same run
    twice is two decisions by a person, not one decision observed twice, and a
    revocation followed by a re-signing must not collapse onto the record it
    reversed.
    """
    digest = stable_hash(
        [
            "LIVE_READINESS_SIGNOFF",
            schema_version,
            readiness_run_id,
            git_revision,
            signed_by,
            signed_at.isoformat(),
            status,
        ]
    )
    return f"signoff-{digest[:20]}"


class ReadinessCriterion(BaseModel):
    """One readiness question and what the evidence established.

    ``evidence_id`` is required for ``PASS`` and for ``FAIL`` alike. A failure
    without evidence is as unaccountable as a pass without one: it names a
    defect nobody can go and look at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: ReadinessCriterionId
    domain: ReadinessDomain
    title: str
    status: ReadinessStatus
    reason_code: ReadinessReasonCode
    #: A sentence a reader can act on. Never "not ready".
    detail: str

    #: Which levels this criterion holds shut when it is not satisfied.
    blocking_for: tuple[ReadinessLevel, ...] = ()

    #: The evidence behind the verdict. ``None`` only for ``NOT_TESTED``.
    evidence_id: str | None = None
    evidence_kind: str | None = None
    evidence_source: str | None = None
    observed_at: datetime | None = None
    #: How old the evidence was when this was judged, in seconds. ``None`` for
    #: revision-bound evidence, which does not age with a clock.
    evidence_age_seconds: float | None = None
    #: Ids of immutable domain artifacts supporting this criterion.
    artifact_ids: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """Whether this criterion is met. Exactly ``PASS``, and nothing else."""
        return self.status is ReadinessStatus.PASS

    @property
    def inconclusive(self) -> bool:
        """Evidence was inadequate rather than contradictory."""
        return self.status in READINESS_INCONCLUSIVE_STATUSES

    def blocks(self, level: ReadinessLevel) -> bool:
        """Whether this criterion holds ``level`` shut as it currently stands."""
        return not self.satisfied and level in self.blocking_for

    @field_validator("observed_at")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def _a_verdict_needs_evidence(self) -> ReadinessCriterion:
        """No ``PASS`` and no ``FAIL`` without a piece of evidence behind it.

        Brief section 27, made structural. A readiness framework whose
        conclusions could be asserted without evidence would produce a document
        that looks exactly like a real assessment and certifies nothing.
        """
        if self.status is ReadinessStatus.NOT_TESTED:
            if self.evidence_id is not None:
                raise ValueError(
                    f"{self.criterion_id.value} is NOT_TESTED but carries evidence "
                    f"{self.evidence_id}. NOT_TESTED means nothing was collected; a "
                    f"criterion with evidence has a verdict, even if that verdict is UNKNOWN."
                )
            return self
        if self.evidence_id is None:
            raise ValueError(
                f"{self.criterion_id.value} is {self.status.value} with no evidence. Every "
                f"verdict must name the observation that produced it — a PASS nobody can "
                f"check is indistinguishable from an unexamined system, and a FAIL nobody "
                f"can check is indistinguishable from a bug in this assessor."
            )
        return self

    @model_validator(mode="after")
    def _reason_matches_status(self) -> ReadinessCriterion:
        if self.status is ReadinessStatus.PASS:
            if self.reason_code is not ReadinessReasonCode.SATISFIED:
                raise ValueError(
                    f"{self.criterion_id.value} passes but is reasoned "
                    f"{self.reason_code.value}. A passing criterion is SATISFIED; any other "
                    f"code would record a caveat the status does not carry."
                )
        elif self.reason_code is ReadinessReasonCode.SATISFIED:
            raise ValueError(
                f"{self.criterion_id.value} is {self.status.value} but reasoned SATISFIED. "
                f"Only a PASS is satisfied."
            )
        return self


class ReadinessAssessment(BaseModel):
    """Every criterion, and the level they add up to.

    The level is **derived**, never asserted: :meth:`derive_level` computes it
    from the criteria and a model validator re-checks the stored value against
    the criteria it was stored with. An assessment claiming ``READY_FOR_PAPER``
    while carrying an unsatisfied paper-blocking criterion cannot be
    constructed, so it cannot be written, so it cannot be read back and
    believed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str
    schema_version: str = READINESS_SCHEMA_VERSION
    as_of: datetime
    evaluated_at: datetime

    #: What the system is, at the moment of assessment.
    trading_mode: TradingMode
    git_revision: str | None = None
    working_tree_clean: bool | None = None
    system_version: str | None = None
    config_version: str | None = None

    level: ReadinessLevel
    criteria: tuple[ReadinessCriterion, ...]

    #: The evidence bundle this was computed from, by content hash, so a stored
    #: assessment can be re-derived and checked rather than merely trusted.
    evidence_digest: str
    evidence_ids: tuple[str, ...] = ()

    @field_validator("as_of", "evaluated_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("readiness instants must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("criteria")
    @classmethod
    def _no_duplicates(
        cls, value: tuple[ReadinessCriterion, ...]
    ) -> tuple[ReadinessCriterion, ...]:
        seen = [criterion.criterion_id for criterion in value]
        if len(set(seen)) != len(seen):
            duplicated = sorted({c.value for c in seen if seen.count(c) > 1})
            raise ValueError(
                f"criteria appear more than once: {duplicated}. One question, one verdict — "
                f"two entries for one criterion means two answers, and nothing decides which."
            )
        return value

    @staticmethod
    def derive_level(criteria: Iterable[ReadinessCriterion]) -> ReadinessLevel:
        """The highest level no unsatisfied blocking criterion holds shut.

        Walked from the top down, so the answer is the *strongest* claim the
        evidence supports. ``UNKNOWN``, ``STALE`` and ``NOT_TESTED`` are all
        unsatisfied here — brief section 7, and the reason
        :data:`~trading_system.domain.enums.READINESS_SATISFYING_STATUSES` has
        exactly one member.
        """
        materialised = tuple(criteria)
        for level in reversed(READINESS_LEVEL_ORDER):
            if level is ReadinessLevel.NOT_READY:
                return ReadinessLevel.NOT_READY
            if not any(criterion.blocks(level) for criterion in materialised):
                return level
        return ReadinessLevel.NOT_READY

    @model_validator(mode="after")
    def _level_follows_from_the_criteria(self) -> ReadinessAssessment:
        derived = self.derive_level(self.criteria)
        if derived is not self.level:
            blockers = sorted(
                criterion.criterion_id.value
                for criterion in self.criteria
                if criterion.blocks(self.level)
            )
            raise ValueError(
                f"assessment claims {self.level.value} but its criteria derive "
                f"{derived.value}. Unsatisfied blocking criteria: {blockers}. The level is "
                f"computed from the evidence; it is not a field anybody sets."
            )
        return self

    # --- reading ------------------------------------------------------------
    def blocking(self, level: ReadinessLevel) -> tuple[ReadinessCriterion, ...]:
        """Criteria currently holding ``level`` shut, in catalogue order."""
        return tuple(criterion for criterion in self.criteria if criterion.blocks(level))

    def by_status(self, status: ReadinessStatus) -> tuple[ReadinessCriterion, ...]:
        return tuple(criterion for criterion in self.criteria if criterion.status is status)

    @property
    def counts(self) -> dict[str, int]:
        """How many criteria reached each status."""
        return {status.value: len(self.by_status(status)) for status in ReadinessStatus}

    @property
    def is_paper_ready(self) -> bool:
        return self.level in (
            ReadinessLevel.READY_FOR_PAPER,
            ReadinessLevel.READY_FOR_LIVE_REVIEW,
        )

    @property
    def is_live_review_ready(self) -> bool:
        return self.level is ReadinessLevel.READY_FOR_LIVE_REVIEW


class ReadinessRun(BaseModel):
    """One readiness evaluation, as an immutable record.

    Separate from the assessment because a run can fail to produce one. A run
    whose configuration would not load reached no verdict at all, and recording
    that as ``NOT_READY`` would blame the system for a broken assessor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    readiness_run_id: str
    schema_version: str = READINESS_SCHEMA_VERSION
    status: ReadinessRunStatus
    evaluated_at: datetime
    as_of: datetime

    trading_mode: TradingMode
    git_revision: str | None = None
    working_tree_clean: bool | None = None
    system_version: str | None = None
    config_version: str | None = None
    readiness_config_version: str | None = None

    assessment: ReadinessAssessment | None = None

    #: Slots the operator deliberately did not collect, and why. This is what
    #: makes "the observability stack was not started" visible as a *choice*
    #: rather than surfacing only as a set of NOT_TESTED criteria.
    not_collected: dict[str, str] = Field(default_factory=dict)

    #: What went wrong, when the run itself did.
    error: str | None = None

    #: Always zero, and asserted rather than assumed. Readiness has no order
    #: path; printing the count next to every run is how that stays true.
    orders_submitted: int = 0

    @field_validator("evaluated_at", "as_of")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("readiness instants must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _readiness_never_trades(self) -> ReadinessRun:
        if self.orders_submitted != 0:
            raise ValueError(
                f"a readiness run recorded {self.orders_submitted} submitted order(s). "
                f"Readiness observes a system; it never trades with it. This record cannot "
                f"exist, and if this validator ever fires the correct response is to find "
                f"the order, not to relax the check."
            )
        return self

    @model_validator(mode="after")
    def _a_conclusion_needs_an_assessment(self) -> ReadinessRun:
        if self.status in (ReadinessRunStatus.COMPLETE, ReadinessRunStatus.PARTIAL):
            if self.assessment is None:
                raise ValueError(
                    f"a {self.status.value} readiness run carries no assessment. A run that "
                    f"reached a conclusion must record it; one that did not has a status "
                    f"saying so."
                )
        elif self.assessment is not None and self.status is not ReadinessRunStatus.DRY_RUN:
            raise ValueError(
                f"a {self.status.value} readiness run carries an assessment. Only a run that "
                f"completed — or a dry run, which computes one and stores nothing — has a "
                f"verdict to record."
            )
        return self

    @property
    def level(self) -> ReadinessLevel:
        """The level reached, or ``NOT_READY`` when no assessment was made.

        A run that produced nothing is not ready by construction: absence of a
        verdict is never a favourable verdict.
        """
        return self.assessment.level if self.assessment else ReadinessLevel.NOT_READY


class LiveReadinessSignoff(BaseModel):
    """A human decision about a readiness run (brief section 22).

    **It enables nothing.** Constructing one changes no environment variable,
    no configuration file and no guard; ``tests/readiness/test_signoff.py``
    asserts that the module cannot even import something that could, and
    ``readiness.signoff.enables_trading`` fails to load if set.

    The signer is required and is never inferred. ``$USER`` is whoever ran the
    process and a git ``user.name`` is a string anybody can set; recording
    either as an accountable human decision would be a fiction that looks
    exactly like accountability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    signoff_id: str
    schema_version: str = READINESS_SCHEMA_VERSION
    status: SignoffStatus

    #: The run being signed. A sign-off always refers to specific evidence.
    readiness_run_id: str
    assessment_id: str | None = None
    readiness_level: ReadinessLevel

    signed_by: str
    signed_at: datetime
    git_revision: str | None = None
    working_tree_clean: bool | None = None

    #: Free text from the signer. Preserved verbatim.
    note: str | None = None

    #: Restated on the record itself, so a printed sign-off carries its own
    #: disclaimer and cannot be quoted out of context as an authorisation.
    enables_trading: bool = False

    @field_validator("signed_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("signed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _signing_records_a_person(self) -> LiveReadinessSignoff:
        if self.status is SignoffStatus.SIGNED and (
            not self.signed_by.strip() or self.signed_by == IDENTITY_NOT_AVAILABLE
        ):
            raise ValueError(
                "a SIGNED live-readiness record must name who signed it. Where the "
                "environment cannot establish an identity the answer is NOT_AVAILABLE and "
                "the sign-off does not happen — an inferred signer is worse than none, "
                "because it looks like accountability."
            )
        return self

    @model_validator(mode="after")
    def _a_signoff_enables_nothing(self) -> LiveReadinessSignoff:
        if self.enables_trading:
            raise ValueError(
                "a live-readiness sign-off cannot enable trading. TRADING_MODE, "
                "LIVE_TRADING_CONFIRMED, LIVE_READINESS_CHECKLIST_SIGNED_OFF, "
                "execution.enabled and IBKR_READ_ONLY are separate controls a human sets "
                "deliberately; there is no automatic transition from READY_FOR_LIVE_REVIEW "
                "to LIVE."
            )
        return self
