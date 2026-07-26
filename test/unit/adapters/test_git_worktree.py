"""Unit tests for the deterministic Git worktree adapter (pure logic, mocked git)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from gearmeshing_ai.adapters.git_worktree import (
    CommandResult,
    DirtyRepositoryError,
    GitCommandError,
    GitWorktreeManager,
    UnrecognizedWorktreeError,
    UnsupportedBaseBranchError,
    WorktreeConflictError,
    _parse_worktree_list,
)
from gearmeshing_ai.application.ports.coding_executor import RepositoryContext

REPOSITORY_ROOT = "/workspace/GearMeshing-AI"
WORKTREES_ROOT = "/workspace/.worktrees"


@dataclass
class FakeCommandRunner:
    """Scripted, injectable stand-in that records every invoked ``git`` command."""

    status_output: str = ""
    known_branches: set[str] = field(default_factory=set)
    worktrees: dict[str, str] = field(default_factory=dict)  # path -> branch
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail_on: tuple[str, ...] | None = None

    def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        if self.fail_on is not None and call[:2] == self.fail_on:
            return CommandResult(1, "", "simulated failure")
        if call[1:3] == ("status", "--porcelain"):
            return CommandResult(0, self.status_output, "")
        if call[1:3] == ("worktree", "list"):
            return CommandResult(0, self._porcelain_listing(), "")
        if call[1] == "rev-parse":
            branch = call[-1].removeprefix("refs/heads/")
            return CommandResult(0 if branch in self.known_branches else 1, "", "")
        if call[1:3] == ("worktree", "add") and "-b" in call:
            branch = call[call.index("-b") + 1]
            path = call[call.index("-b") + 2]
            self.known_branches.add(branch)
            self.worktrees[path] = branch
            return CommandResult(0, "", "")
        if call[1:3] == ("worktree", "add"):
            path, branch = call[3], call[4]
            self.worktrees[path] = branch
            return CommandResult(0, "", "")
        if call[1:3] == ("worktree", "remove"):
            path = call[-1]
            self.worktrees.pop(path, None)
            return CommandResult(0, "", "")
        if call[1:3] == ("branch", "-D"):
            self.known_branches.discard(call[-1])
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected git invocation: {call}")

    def _porcelain_listing(self) -> str:
        blocks = []
        for path, branch in self.worktrees.items():
            blocks.append(f"worktree {path}\nHEAD 0000000000000000000000000000000000000000\nbranch refs/heads/{branch}")
        return "\n\n".join(blocks) + ("\n" if blocks else "")


def make_manager(runner: FakeCommandRunner | None = None) -> tuple[GitWorktreeManager, FakeCommandRunner]:
    fake = runner if runner is not None else FakeCommandRunner()
    manager = GitWorktreeManager(repository_root=REPOSITORY_ROOT, worktrees_root=WORKTREES_ROOT, runner=fake)
    return manager, fake


# --- deterministic naming -----------------------------------------------------------------


def test_branch_name_is_deterministic_and_run_scoped() -> None:
    manager, _ = make_manager()

    first_call = manager.branch_name("run-1")
    second_call = manager.branch_name("run-1")

    assert first_call == "work-run/run-1"
    assert first_call == second_call
    assert first_call != manager.branch_name("run-2")


def test_worktree_path_is_deterministic_and_run_scoped() -> None:
    manager, _ = make_manager()

    assert manager.worktree_path("run-1") == f"{WORKTREES_ROOT}/work-run-run-1"
    assert manager.worktree_path("run-1") != manager.worktree_path("run-2")


@pytest.mark.parametrize("unsafe_run_id", ("", " ", "../escape", "run id", "run/id", "-run"))
def test_branch_name_rejects_unsafe_run_ids(unsafe_run_id: str) -> None:
    manager, _ = make_manager()

    with pytest.raises(ValueError, match="safe identifier"):
        manager.branch_name(unsafe_run_id)


def test_branch_name_never_collides_with_a_protected_branch() -> None:
    manager, _ = make_manager()

    for run_id in ("main", "master"):
        assert manager.branch_name(run_id) not in {"main", "master"}


# --- constructor isolation ------------------------------------------------------------------


def test_manager_rejects_relative_roots() -> None:
    with pytest.raises(ValueError, match="absolute paths"):
        GitWorktreeManager(repository_root="relative/path", worktrees_root=WORKTREES_ROOT)


def test_manager_rejects_worktrees_root_nested_inside_repository_root() -> None:
    with pytest.raises(ValueError, match="isolated"):
        GitWorktreeManager(repository_root=REPOSITORY_ROOT, worktrees_root=f"{REPOSITORY_ROOT}/.worktrees")


def test_for_sibling_worktrees_places_worktrees_next_to_the_repository() -> None:
    manager = GitWorktreeManager.for_sibling_worktrees(REPOSITORY_ROOT)

    assert manager.worktrees_root == "/workspace/.worktrees"


# --- ensure_worktree: validation and creation -----------------------------------------------


def test_ensure_worktree_rejects_unsupported_base_branch() -> None:
    manager, _ = make_manager()

    with pytest.raises(UnsupportedBaseBranchError):
        manager.ensure_worktree("run-1", base_branch="develop")


def test_ensure_worktree_refuses_when_repository_is_dirty() -> None:
    manager, _ = make_manager(FakeCommandRunner(status_output=" M some/file.py\n"))

    with pytest.raises(DirtyRepositoryError):
        manager.ensure_worktree("run-1")


def test_ensure_worktree_creates_a_new_branch_and_worktree() -> None:
    manager, fake = make_manager()

    repository = manager.ensure_worktree("run-1", base_branch="main")

    assert isinstance(repository, RepositoryContext)
    assert repository.branch == "work-run/run-1"
    assert repository.worktree_root == f"{WORKTREES_ROOT}/work-run-run-1"
    assert repository.base_ref == "main"
    assert any(call[1:3] == ("worktree", "add") and "-b" in call for call in fake.calls)


def test_ensure_worktree_reuses_an_existing_branch_without_the_dash_b_flag() -> None:
    manager, fake = make_manager()
    fake.known_branches.add("work-run/run-1")

    manager.ensure_worktree("run-1")

    add_calls = [call for call in fake.calls if call[1:3] == ("worktree", "add")]
    assert len(add_calls) == 1
    assert "-b" not in add_calls[0]


def test_ensure_worktree_is_idempotent_for_the_same_run() -> None:
    manager, fake = make_manager()

    first = manager.ensure_worktree("run-1")
    second = manager.ensure_worktree("run-1")

    assert first == second
    add_calls = [call for call in fake.calls if call[1:3] == ("worktree", "add")]
    assert len(add_calls) == 1


def test_ensure_worktree_for_two_runs_computes_non_conflicting_contexts() -> None:
    manager, _ = make_manager()

    first = manager.ensure_worktree("run-1")
    second = manager.ensure_worktree("run-2")

    assert first.worktree_root != second.worktree_root
    assert first.branch != second.branch


def test_ensure_worktree_raises_on_conflicting_existing_worktree() -> None:
    manager, fake = make_manager()
    fake.worktrees[manager.worktree_path("run-1")] = "someone-elses-branch"

    with pytest.raises(WorktreeConflictError):
        manager.ensure_worktree("run-1")


def test_ensure_worktree_wraps_git_failures() -> None:
    manager, _ = make_manager(FakeCommandRunner(fail_on=("git", "worktree")))

    with pytest.raises(GitCommandError):
        manager.ensure_worktree("run-1")


# --- cleanup: safe deletion ------------------------------------------------------------------


def test_cleanup_removes_the_worktree_and_branch_it_created() -> None:
    manager, fake = make_manager()
    manager.ensure_worktree("run-1")

    manager.cleanup("run-1")

    assert manager.worktree_path("run-1") not in fake.worktrees
    assert manager.branch_name("run-1") not in fake.known_branches


def test_cleanup_is_a_no_op_when_nothing_exists_for_the_run() -> None:
    manager, fake = make_manager()

    manager.cleanup("never-created")

    assert not any(call[1:3] == ("worktree", "remove") for call in fake.calls)


def test_cleanup_preserves_evidence_when_requested() -> None:
    manager, fake = make_manager()
    manager.ensure_worktree("run-1")

    manager.cleanup("run-1", preserve_evidence=True)

    assert manager.worktree_path("run-1") in fake.worktrees
    assert not any(call[1:3] == ("worktree", "remove") for call in fake.calls)


def test_cleanup_never_deletes_a_worktree_bound_to_a_different_branch() -> None:
    """The core safety property: cleanup must refuse anything it did not name itself."""
    manager, fake = make_manager()
    unrelated_path = manager.worktree_path("run-1")
    fake.worktrees[unrelated_path] = "someone-elses-branch"

    with pytest.raises(UnrecognizedWorktreeError):
        manager.cleanup("run-1")

    assert fake.worktrees[unrelated_path] == "someone-elses-branch"


def test_cleanup_wraps_git_failures() -> None:
    manager, fake = make_manager()
    manager.ensure_worktree("run-1")
    fake.fail_on = ("git", "worktree")

    with pytest.raises(GitCommandError):
        manager.cleanup("run-1")


# --- porcelain parsing ------------------------------------------------------------------------


def test_parse_worktree_list_extracts_path_and_branch() -> None:
    output = (
        "worktree /workspace/GearMeshing-AI\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /workspace/.worktrees/work-run-run-1\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/work-run/run-1\n"
    )

    entries = _parse_worktree_list(output)

    assert entries[0].path == "/workspace/GearMeshing-AI"
    assert entries[0].branch == "main"
    assert entries[1].path == "/workspace/.worktrees/work-run-run-1"
    assert entries[1].branch == "work-run/run-1"


def test_parse_worktree_list_reports_detached_entries_as_no_branch() -> None:
    output = "worktree /workspace/GearMeshing-AI\nHEAD 1111111111111111111111111111111111111111\ndetached\n"

    entries = _parse_worktree_list(output)

    assert entries[0].branch is None


def test_parse_worktree_list_handles_empty_output() -> None:
    assert _parse_worktree_list("") == ()
