"""A readiness result identifies the code it describes (brief section 29)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_system.readiness.collectors import collect_git

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository, so nothing here reads the real one."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-qm", "first")
    return root


def test_a_clean_tree_is_reported_clean(repo: Path) -> None:
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.collected
    assert record.detail["working_tree_clean"] is True
    assert record.detail["changed_files"] == 0
    assert record.detail["git_revision"]


def test_the_revision_is_recorded_on_the_evidence(repo: Path) -> None:
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.git_revision == record.detail["git_revision"]


def test_a_modified_file_makes_the_tree_dirty(repo: Path) -> None:
    (repo / "file.txt").write_text("two\n", encoding="utf-8")
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.detail["working_tree_clean"] is False
    assert record.detail["changed_files"] == 1


def test_an_untracked_file_makes_the_tree_dirty(repo: Path) -> None:
    """``git status --porcelain`` rather than ``git diff --quiet``.

    A readiness result claiming to describe a revision while sitting on top of
    an untracked module is claiming something false.
    """
    (repo / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.detail["working_tree_clean"] is False


def test_the_changed_sample_is_bounded(repo: Path) -> None:
    """A thousand changed files must not produce a thousand-line audit record."""
    for index in range(40):
        (repo / f"file{index}.txt").write_text("x\n", encoding="utf-8")
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.detail["changed_files"] == 40
    assert len(record.detail["changed_sample"]) <= 20


def test_a_directory_that_is_not_a_repository_reports_a_failure(tmp_path: Path) -> None:
    """No revision is a recorded failure, never a silent None."""
    record = collect_git(repo_root=tmp_path / "not-a-repo", observed_at=NOW)
    assert record.collected is False
    assert record.detail["git_revision"] is None
    assert record.error


def test_the_branch_is_recorded(repo: Path) -> None:
    record = collect_git(repo_root=repo, observed_at=NOW)
    assert record.detail["branch"]
