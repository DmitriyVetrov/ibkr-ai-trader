"""Evidence records are immutable, content-addressed and honest about failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tests.readiness.conftest import NOW
from trading_system.domain.enums import ReadinessEvidenceKind
from trading_system.readiness.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    evidence_identifier,
)

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> EvidenceRecord:
    payload: dict[str, object] = {
        "kind": ReadinessEvidenceKind.COMMAND,
        "source": "pytest",
        "observed_at": NOW,
        "detail": {"exit_code": 0},
    }
    payload.update(overrides)
    return EvidenceRecord.of(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_identical_observations_share_an_id() -> None:
    assert _record().evidence_id == _record().evidence_id


def test_a_different_payload_is_a_different_record() -> None:
    assert _record().evidence_id != _record(detail={"exit_code": 1}).evidence_id


def test_the_same_observation_at_a_different_instant_is_a_different_record() -> None:
    """Evidence is a *measurement*, not a fact observed twice.

    Two probes of one endpoint a minute apart are two facts; collapsing them
    would make a stale reading indistinguishable from a fresh one that happened
    to agree.
    """
    later = _record(observed_at=NOW + timedelta(minutes=1))
    assert _record().evidence_id != later.evidence_id


def test_a_failed_collection_differs_from_a_successful_one() -> None:
    failed = _record(collected=False, error="refused")
    assert failed.evidence_id != _record().evidence_id


def test_the_identifier_is_stable_across_calls() -> None:
    common = {
        "kind": ReadinessEvidenceKind.SERVICE_PROBE,
        "source": "tempo",
        "observed_at": NOW,
        "detail_digest": "abc",
    }
    assert evidence_identifier(**common) == evidence_identifier(**common)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Failure is recorded, never hidden
# ---------------------------------------------------------------------------
def test_a_failed_collection_keeps_its_error() -> None:
    record = _record(collected=False, error="connection refused", detail={"connected": False})
    assert record.collected is False
    assert record.error == "connection refused"


def test_a_record_is_immutable() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.source = "something else"  # type: ignore[misc]


def test_a_naive_observation_instant_is_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        EvidenceRecord.of(
            kind=ReadinessEvidenceKind.COMMAND,
            source="pytest",
            observed_at=datetime(2026, 8, 15, 12, 0),
        )


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------
def test_age_is_measured_against_the_assessment_instant() -> None:
    record = _record(observed_at=NOW - timedelta(minutes=5))
    assert record.age_seconds(NOW) == pytest.approx(300.0)


def test_age_never_goes_negative() -> None:
    """Two clock reads microseconds apart are not a fact from the future."""
    record = _record(observed_at=NOW + timedelta(seconds=2))
    assert record.age_seconds(NOW) == 0.0


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------
def test_an_absent_slot_returns_none_rather_than_a_negative_record() -> None:
    bundle = EvidenceBundle(as_of=NOW)
    assert bundle.get("test_suite") is None
    assert bundle.skip_reason("test_suite") is None


def test_adding_a_record_returns_a_new_bundle() -> None:
    bundle = EvidenceBundle(as_of=NOW)
    updated = bundle.with_record("test_suite", _record())
    assert bundle.get("test_suite") is None
    assert updated.get("test_suite") is not None


def test_recording_a_slot_clears_any_skip_for_it() -> None:
    """A slot cannot be both "not collected" and carry evidence."""
    bundle = EvidenceBundle(as_of=NOW).with_skip("broker", "not requested")
    updated = bundle.with_record("broker", _record(source="IBKR"))
    assert updated.skip_reason("broker") is None
    assert updated.get("broker") is not None


def test_skipping_a_slot_clears_any_record_for_it() -> None:
    bundle = EvidenceBundle(as_of=NOW).with_record("broker", _record(source="IBKR"))
    updated = bundle.with_skip("broker", "not requested")
    assert updated.get("broker") is None
    assert updated.skip_reason("broker") == "not requested"


def test_evidence_ids_are_deterministically_ordered() -> None:
    bundle = (
        EvidenceBundle(as_of=NOW)
        .with_record("zebra", _record(source="z"))
        .with_record("alpha", _record(source="a"))
    )
    assert bundle.evidence_ids == tuple(
        bundle.records[slot].evidence_id for slot in sorted(bundle.records)
    )


def test_the_digest_is_stable_and_content_sensitive() -> None:
    first = EvidenceBundle(as_of=NOW, git_revision="abc").with_record("a", _record())
    same = EvidenceBundle(as_of=NOW, git_revision="abc").with_record("a", _record())
    different = EvidenceBundle(as_of=NOW, git_revision="abc").with_record(
        "a", _record(detail={"exit_code": 1})
    )
    assert first.digest() == same.digest()
    assert first.digest() != different.digest()


def test_the_digest_notices_a_skipped_slot() -> None:
    """ "We chose not to look" changes what a run concluded from."""
    plain = EvidenceBundle(as_of=NOW, git_revision="abc")
    skipped = plain.with_skip("broker", "not requested")
    assert plain.digest() != skipped.digest()


def test_a_naive_bundle_instant_is_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        EvidenceBundle(as_of=datetime(2026, 8, 15, 12, 0))


def test_the_bundle_normalises_to_utc() -> None:
    from zoneinfo import ZoneInfo

    bundle = EvidenceBundle(as_of=datetime(2026, 8, 15, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    assert bundle.as_of.tzinfo is UTC
