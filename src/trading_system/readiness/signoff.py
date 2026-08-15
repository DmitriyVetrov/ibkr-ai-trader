"""The human live-readiness sign-off (Milestone 12, brief section 22).

**Signing enables nothing.** That is the entire point of the module, and it is
enforced three ways rather than asserted once:

* ``readiness.signoff.enables_trading`` fails to load if set;
* :class:`~trading_system.readiness.models.LiveReadinessSignoff` refuses to
  construct with ``enables_trading=True``;
* ``tests/readiness/test_boundaries.py`` walks this module's transitive import
  graph and fails if it can reach a broker, an execution service, ``os.environ``
  mutation or anything that writes ``config/``.

A sign-off is a record that a named human looked at specific evidence at a
specific revision and said yes. ``TRADING_MODE``, ``LIVE_TRADING_CONFIRMED``,
``LIVE_READINESS_CHECKLIST_SIGNED_OFF``, ``execution.enabled`` and
``IBKR_READ_ONLY`` stay exactly where they are — in the environment and in
committed configuration, where somebody sets them deliberately and a reviewer
can see the diff.

**The signer is never inferred.** ``$USER`` is whoever happens to be running
the process; a git ``user.name`` is a string anybody can set. Recording either
as an accountable human decision would be a fiction that looks precisely like
accountability, which is worse than an honest ``NOT_AVAILABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_system.domain.enums import ReadinessLevel, SignoffStatus
from trading_system.infrastructure.settings import ReadinessSignoffConfig
from trading_system.readiness.models import (
    IDENTITY_NOT_AVAILABLE,
    LiveReadinessSignoff,
    ReadinessRun,
    signoff_identifier,
)

__all__ = [
    "SignoffRefusedError",
    "SignoffRequest",
    "build_signoff",
]


class SignoffRefusedError(RuntimeError):
    """A sign-off was requested and the preconditions were not met.

    Raised rather than returned as a status, deliberately. Every other refusal
    in this system is a recorded outcome because the *system* declined; this
    one is a person being told they cannot sign yet, and there is no artifact
    worth writing about an attempt that never happened.
    """


@dataclass(frozen=True, slots=True)
class SignoffRequest:
    """What a person is asking to sign, and who they say they are."""

    run: ReadinessRun
    signed_by: str
    signed_at: datetime
    note: str | None = None
    status: SignoffStatus = SignoffStatus.SIGNED


def build_signoff(request: SignoffRequest, policy: ReadinessSignoffConfig) -> LiveReadinessSignoff:
    """Validate the preconditions and mint the record. Enables nothing.

    Every refusal below names what to do about it, because a person at a
    terminal being told "refused" and nothing else will reach for a flag that
    turns the check off.
    """
    run = request.run

    if policy.require_explicit_identity:
        identity = (request.signed_by or "").strip()
        if not identity or identity == IDENTITY_NOT_AVAILABLE:
            raise SignoffRefusedError(
                "no signer identity was supplied. A sign-off records an accountable human "
                "decision, and this environment cannot establish one on its own — $USER is "
                "whoever ran the process and a git user.name is a string anybody can set. "
                "Pass the identity explicitly with --signed-by."
            )

    if request.status is SignoffStatus.SIGNED:
        if policy.require_live_review and run.level is not ReadinessLevel.READY_FOR_LIVE_REVIEW:
            blockers = _blocking_summary(run)
            raise SignoffRefusedError(
                f"this readiness run reached {run.level.value}, not READY_FOR_LIVE_REVIEW. "
                f"Signing a run that has not met the machine-checkable prerequisites would "
                f"record a review of evidence that does not exist.{blockers}"
            )

        if policy.require_clean_working_tree and run.working_tree_clean is not True:
            state = "unknown" if run.working_tree_clean is None else "dirty"
            raise SignoffRefusedError(
                f"the working tree was {state} when this readiness run was evaluated. The "
                f"code that would run is not the code the evidence describes, so a sign-off "
                f"would name a revision nobody assessed. Commit or stash, then re-run "
                f"`readiness check`."
            )

    return LiveReadinessSignoff(
        signoff_id=signoff_identifier(
            readiness_run_id=run.readiness_run_id,
            git_revision=run.git_revision,
            signed_by=request.signed_by,
            signed_at=request.signed_at,
            status=request.status.value,
        ),
        status=request.status,
        readiness_run_id=run.readiness_run_id,
        assessment_id=(run.assessment.assessment_id if run.assessment else None),
        readiness_level=run.level,
        signed_by=request.signed_by,
        signed_at=request.signed_at,
        git_revision=run.git_revision,
        working_tree_clean=run.working_tree_clean,
        note=request.note,
        # Restated on the record so a printed or exported sign-off carries its
        # own disclaimer and cannot be quoted as an authorisation.
        enables_trading=False,
    )


def _blocking_summary(run: ReadinessRun) -> str:
    """The first few criteria holding live review shut, for a refusal message."""
    if run.assessment is None:
        return ""
    blockers = run.assessment.blocking(ReadinessLevel.READY_FOR_LIVE_REVIEW)
    if not blockers:
        return ""
    named = ", ".join(criterion.criterion_id.value for criterion in blockers[:5])
    remainder = len(blockers) - 5
    suffix = f" and {remainder} more" if remainder > 0 else ""
    return f" Blocking: {named}{suffix}."
