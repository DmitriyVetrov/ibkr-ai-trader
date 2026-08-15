"""Readiness runs are immutable, append-only and content-addressed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_system.domain.enums import (
    ReadinessLevel,
    ReadinessRunStatus,
    SignoffStatus,
    TradingMode,
)
from trading_system.readiness.models import LiveReadinessSignoff, ReadinessRun
from trading_system.readiness.store import (
    FilesystemReadinessRepository,
    ReadinessStoreError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> FilesystemReadinessRepository:
    return FilesystemReadinessRepository(tmp_path / "readiness")


def _run(**overrides: object) -> ReadinessRun:
    payload: dict[str, object] = {
        "readiness_run_id": "readiness-run-1",
        "status": ReadinessRunStatus.NO_EVIDENCE,
        "evaluated_at": NOW,
        "as_of": NOW,
        "trading_mode": TradingMode.PAPER,
        "git_revision": "abc123",
        "working_tree_clean": True,
    }
    payload.update(overrides)
    return ReadinessRun(**payload)


def _signoff(**overrides: object) -> LiveReadinessSignoff:
    payload: dict[str, object] = {
        "signoff_id": "signoff-1",
        "status": SignoffStatus.SIGNED,
        "readiness_run_id": "readiness-run-1",
        "readiness_level": ReadinessLevel.READY_FOR_LIVE_REVIEW,
        "signed_by": "A Person",
        "signed_at": NOW,
        "git_revision": "abc123",
    }
    payload.update(overrides)
    return LiveReadinessSignoff(**payload)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
def test_a_run_survives_a_round_trip(repository: FilesystemReadinessRepository) -> None:
    identifier, is_new = repository.save_run(_run())
    assert is_new
    restored = repository.get_run(identifier)
    assert restored is not None
    assert restored.model_dump(mode="json") == _run().model_dump(mode="json")


def test_the_latest_run_is_the_most_recent(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_run(_run(readiness_run_id="old", evaluated_at=NOW - timedelta(hours=2)))
    repository.save_run(_run(readiness_run_id="new"))
    latest = repository.latest_run()
    assert latest is not None
    assert latest.readiness_run_id == "new"


def test_history_is_newest_first(repository: FilesystemReadinessRepository) -> None:
    repository.save_run(_run(readiness_run_id="old", evaluated_at=NOW - timedelta(hours=2)))
    repository.save_run(_run(readiness_run_id="new"))
    assert [entry.readiness_run_id for entry in repository.history()] == ["new", "old"]


def test_an_unknown_id_reads_back_as_none(
    repository: FilesystemReadinessRepository,
) -> None:
    assert repository.get_run("does-not-exist") is None


def test_an_empty_store_has_no_latest_run(
    repository: FilesystemReadinessRepository,
) -> None:
    assert repository.latest_run() is None
    assert repository.history() == []


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
def test_re_storing_identical_content_is_a_re_observation(
    repository: FilesystemReadinessRepository,
) -> None:
    """Brief section 25. The second write is not a second event."""
    _, first = repository.save_run(_run())
    _, second = repository.save_run(_run())
    assert first is True
    assert second is False


def test_a_re_observation_is_still_recorded_in_the_history(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_run(_run())
    repository.save_run(_run())
    entries = repository.history()
    assert len(entries) == 2
    assert any(entry.reobserved for entry in entries)


def test_rewriting_a_run_with_different_content_raises(
    repository: FilesystemReadinessRepository,
) -> None:
    """A verdict that changed under one id is what an audit gate prevents."""
    repository.save_run(_run())
    with pytest.raises(ReadinessStoreError, match="immutable"):
        repository.save_run(_run(working_tree_clean=False))


def test_a_run_is_written_atomically(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_run(_run())
    leftovers = list(repository.runs_dir.rglob("*.tmp"))
    assert not leftovers


# ---------------------------------------------------------------------------
# Sign-offs live in their own index
# ---------------------------------------------------------------------------
def test_a_signoff_survives_a_round_trip(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_signoff(_signoff())
    latest = repository.latest_signoff()
    assert latest is not None
    assert latest.signed_by == "A Person"
    assert latest.enables_trading is False


def test_signing_does_not_touch_the_run_record(
    repository: FilesystemReadinessRepository,
) -> None:
    """A sign-off is a decision *about* a run, not a property of it.

    Folding it in would mean rewriting an immutable artifact to record that
    somebody read it.
    """
    repository.save_run(_run())
    before = repository.get_run("readiness-run-1")
    repository.save_signoff(_signoff())
    after = repository.get_run("readiness-run-1")
    assert before is not None and after is not None
    assert before.model_dump(mode="json") == after.model_dump(mode="json")


def test_a_revocation_does_not_overwrite_the_signing(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_signoff(_signoff())
    repository.save_signoff(
        _signoff(
            signoff_id="signoff-2", status=SignoffStatus.REVOKED, signed_at=NOW + timedelta(hours=1)
        )
    )
    history = repository.signoff_history()
    assert {entry.status for entry in history} == {"SIGNED", "REVOKED"}


def test_signoffs_for_a_run_are_returned_in_order(
    repository: FilesystemReadinessRepository,
) -> None:
    repository.save_signoff(_signoff())
    repository.save_signoff(_signoff(signoff_id="signoff-2", signed_at=NOW + timedelta(hours=1)))
    records = repository.signoffs_for("readiness-run-1")
    assert [record.signoff_id for record in records] == ["signoff-1", "signoff-2"]


def test_an_empty_store_has_no_signoff(
    repository: FilesystemReadinessRepository,
) -> None:
    assert repository.latest_signoff() is None
    assert repository.signoff_history() == []


# ---------------------------------------------------------------------------
# Nothing leaks into the repository's own data tree
# ---------------------------------------------------------------------------
def test_everything_is_written_under_the_given_root(
    repository: FilesystemReadinessRepository, tmp_path: Path
) -> None:
    repository.save_run(_run())
    repository.save_signoff(_signoff())
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert str(path).startswith(str(tmp_path))
