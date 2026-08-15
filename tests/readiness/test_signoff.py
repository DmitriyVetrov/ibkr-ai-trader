"""A human sign-off records a decision and enables nothing (brief section 22)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.domain.enums import (
    ReadinessLevel,
    ReadinessRunStatus,
    SignoffStatus,
    TradingMode,
)
from trading_system.infrastructure.settings import ReadinessSignoffConfig
from trading_system.readiness.models import IDENTITY_NOT_AVAILABLE, ReadinessRun
from trading_system.readiness.signoff import (
    SignoffRefusedError,
    SignoffRequest,
    build_signoff,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class _StubRun(ReadinessRun):
    """A run whose level can be set directly."""

    forced_level: ReadinessLevel = ReadinessLevel.NOT_READY

    @property
    def level(self) -> ReadinessLevel:
        return self.forced_level


def _stub(level: ReadinessLevel, *, clean: bool | None = True) -> _StubRun:
    return _StubRun(
        readiness_run_id="readiness-run-1",
        status=ReadinessRunStatus.NO_EVIDENCE,
        evaluated_at=NOW,
        as_of=NOW,
        trading_mode=TradingMode.PAPER,
        git_revision="abc123",
        working_tree_clean=clean,
        forced_level=level,
    )


def _policy(**overrides: object) -> ReadinessSignoffConfig:
    return ReadinessSignoffConfig(**overrides)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_live_review_run_can_be_signed() -> None:
    signoff = build_signoff(
        SignoffRequest(
            run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW),
            signed_by="A Person",
            signed_at=NOW,
            note="reviewed the evidence",
        ),
        _policy(),
    )
    assert signoff.status is SignoffStatus.SIGNED
    assert signoff.signed_by == "A Person"
    assert signoff.note == "reviewed the evidence"


def test_a_signoff_never_enables_trading() -> None:
    """The claim the whole module exists to make."""
    signoff = build_signoff(
        SignoffRequest(
            run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW),
            signed_by="A Person",
            signed_at=NOW,
        ),
        _policy(),
    )
    assert signoff.enables_trading is False


def test_a_signoff_records_the_revision_it_reviewed() -> None:
    signoff = build_signoff(
        SignoffRequest(
            run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW),
            signed_by="A Person",
            signed_at=NOW,
        ),
        _policy(),
    )
    assert signoff.git_revision == "abc123"
    assert signoff.working_tree_clean is True
    assert signoff.readiness_run_id == "readiness-run-1"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_signing_without_an_identity_is_refused() -> None:
    """An inferred signer is worse than none: it looks like accountability."""
    with pytest.raises(SignoffRefusedError, match="no signer identity"):
        build_signoff(
            SignoffRequest(
                run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW), signed_by="", signed_at=NOW
            ),
            _policy(),
        )


def test_signing_as_not_available_is_refused() -> None:
    with pytest.raises(SignoffRefusedError, match="no signer identity"):
        build_signoff(
            SignoffRequest(
                run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW),
                signed_by=IDENTITY_NOT_AVAILABLE,
                signed_at=NOW,
            ),
            _policy(),
        )


@pytest.mark.parametrize("level", [ReadinessLevel.NOT_READY, ReadinessLevel.READY_FOR_PAPER])
def test_signing_a_run_below_live_review_is_refused(level: ReadinessLevel) -> None:
    with pytest.raises(SignoffRefusedError, match="not READY_FOR_LIVE_REVIEW"):
        build_signoff(
            SignoffRequest(run=_stub(level), signed_by="A Person", signed_at=NOW),
            _policy(),
        )


def test_signing_a_dirty_tree_is_refused() -> None:
    """Brief section 29: the reviewed code would not be the code that runs."""
    with pytest.raises(SignoffRefusedError, match="dirty"):
        build_signoff(
            SignoffRequest(
                run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW, clean=False),
                signed_by="A Person",
                signed_at=NOW,
            ),
            _policy(),
        )


def test_signing_an_unknown_tree_state_is_refused() -> None:
    with pytest.raises(SignoffRefusedError, match="unknown"):
        build_signoff(
            SignoffRequest(
                run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW, clean=None),
                signed_by="A Person",
                signed_at=NOW,
            ),
            _policy(),
        )


def test_a_refusal_says_what_to_do_about_it() -> None:
    """A person told only "refused" reaches for a flag that turns the check off."""
    with pytest.raises(SignoffRefusedError) as raised:
        build_signoff(
            SignoffRequest(
                run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW, clean=False),
                signed_by="A Person",
                signed_at=NOW,
            ),
            _policy(),
        )
    assert "Commit or stash" in str(raised.value)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------
def test_a_revocation_may_name_any_run() -> None:
    """Withdrawing a decision must not be gated on the thing being fine."""
    signoff = build_signoff(
        SignoffRequest(
            run=_stub(ReadinessLevel.NOT_READY, clean=False),
            signed_by="A Person",
            signed_at=NOW,
            status=SignoffStatus.REVOKED,
        ),
        _policy(),
    )
    assert signoff.status is SignoffStatus.REVOKED


# ---------------------------------------------------------------------------
# Policy can be relaxed deliberately, and only deliberately
# ---------------------------------------------------------------------------
def test_a_configured_policy_may_permit_an_unclean_tree() -> None:
    signoff = build_signoff(
        SignoffRequest(
            run=_stub(ReadinessLevel.READY_FOR_LIVE_REVIEW, clean=False),
            signed_by="A Person",
            signed_at=NOW,
        ),
        _policy(require_clean_working_tree=False),
    )
    assert signoff.working_tree_clean is False
