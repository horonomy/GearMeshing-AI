"""Contract tests exercising real ``git worktree`` operations against a scratch repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gearmeshing_ai.adapters.git_worktree import (
    DirtyRepositoryError,
    GitWorktreeManager,
    UnrecognizedWorktreeError,
    WorktreeConflictError,
)
from gearmeshing_ai.application.ports.coding_executor import RepositoryContext


def _run_git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def scratch_repository(tmp_path: Path) -> Path:
    """A throwaway Git repository with one commit on ``main``, isolated from any ambient config."""
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _run_git(["init", "-q", "-b", "main"], cwd=repository_root)
    _run_git(["config", "user.email", "test@example.com"], cwd=repository_root)
    _run_git(["config", "user.name", "Test"], cwd=repository_root)
    (repository_root / "README.md").write_text("scratch\n")
    _run_git(["add", "README.md"], cwd=repository_root)
    _run_git(["commit", "-q", "-m", "initial commit"], cwd=repository_root)
    return repository_root


def make_manager(repository_root: Path, tmp_path: Path) -> GitWorktreeManager:
    return GitWorktreeManager(repository_root=str(repository_root), worktrees_root=str(tmp_path / ".worktrees"))


def test_ensure_worktree_creates_a_real_isolated_checkout(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)

    repository = manager.ensure_worktree("run-1", base_branch="main")

    assert isinstance(repository, RepositoryContext)
    worktree_root = Path(repository.worktree_root)
    assert worktree_root.is_dir()
    assert (worktree_root / "README.md").is_file()
    assert repository.branch == "work-run/run-1"


def test_ensure_worktree_is_idempotent_for_a_repeated_run(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)

    first = manager.ensure_worktree("run-1")
    second = manager.ensure_worktree("run-1")

    assert first == second
    assert Path(first.worktree_root).is_dir()


def test_two_runs_get_separate_worktrees_without_file_conflicts(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)

    first = manager.ensure_worktree("run-1")
    second = manager.ensure_worktree("run-2")

    first_root, second_root = Path(first.worktree_root), Path(second.worktree_root)
    assert first_root != second_root
    (first_root / "run-1-only.txt").write_text("owned by run-1\n")
    (second_root / "run-2-only.txt").write_text("owned by run-2\n")

    assert (first_root / "run-1-only.txt").is_file()
    assert not (second_root / "run-1-only.txt").exists()
    assert (second_root / "run-2-only.txt").is_file()
    assert not (first_root / "run-2-only.txt").exists()


def test_ensure_worktree_refuses_on_a_dirty_source_repository(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)
    (scratch_repository / "README.md").write_text("uncommitted change\n")

    with pytest.raises(DirtyRepositoryError):
        manager.ensure_worktree("run-1")


def test_ensure_worktree_raises_when_the_path_is_owned_by_a_different_branch(
    scratch_repository: Path, tmp_path: Path
) -> None:
    manager = make_manager(scratch_repository, tmp_path)
    conflicting_path = manager.worktree_path("run-1")
    _run_git(["worktree", "add", "-b", "someone-elses-branch", conflicting_path, "main"], cwd=scratch_repository)

    with pytest.raises(WorktreeConflictError):
        manager.ensure_worktree("run-1")


def test_cleanup_removes_the_real_worktree_and_branch(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)
    repository = manager.ensure_worktree("run-1")
    worktree_root = Path(repository.worktree_root)
    assert worktree_root.is_dir()

    manager.cleanup("run-1")

    assert not worktree_root.exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "work-run/run-1"],
        cwd=scratch_repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""


def test_cleanup_preserves_the_worktree_when_evidence_should_survive(scratch_repository: Path, tmp_path: Path) -> None:
    manager = make_manager(scratch_repository, tmp_path)
    repository = manager.ensure_worktree("run-1")
    worktree_root = Path(repository.worktree_root)

    manager.cleanup("run-1", preserve_evidence=True)

    assert worktree_root.is_dir()


def test_cleanup_never_deletes_an_unrelated_developer_worktree(scratch_repository: Path, tmp_path: Path) -> None:
    """The core cleanup safety property, proven against a real git worktree."""
    manager = make_manager(scratch_repository, tmp_path)
    unrelated_path = manager.worktree_path("run-1")
    _run_git(["worktree", "add", "-b", "developer/exploring", unrelated_path, "main"], cwd=scratch_repository)

    with pytest.raises(UnrecognizedWorktreeError):
        manager.cleanup("run-1")

    assert Path(unrelated_path).is_dir()
    branches = subprocess.run(
        ["git", "branch", "--list", "developer/exploring"],
        cwd=scratch_repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "developer/exploring" in branches


def test_cleanup_after_a_normal_run_leaves_the_source_repository_clean(
    scratch_repository: Path, tmp_path: Path
) -> None:
    manager = make_manager(scratch_repository, tmp_path)
    manager.ensure_worktree("run-1")

    manager.cleanup("run-1")

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=scratch_repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""
