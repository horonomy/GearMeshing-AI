"""Adapter that creates and safely tears down one isolated Git worktree per WorkRun.

This module is the thing that actually shells out to ``git worktree`` to turn a
:class:`~gearmeshing_ai.application.ports.coding_executor.RepositoryContext`
from an abstract, already-validated value into a real, isolated checkout on
disk - so concurrent or failed runs never contaminate the source checkout or
one another. It contains no orchestration logic: callers decide when to call
:meth:`GitWorktreeManager.ensure_worktree` and :meth:`GitWorktreeManager.cleanup`.

Deterministic naming is the safety mechanism this module relies on. Both the
branch name and the worktree path are pure functions of a caller-supplied
``run_id``, so:

* Calling :meth:`ensure_worktree` twice for the same run reconciles the
  existing worktree/branch instead of erroring or creating a duplicate.
* Two different runs always compute two different paths and branches, so they
  can never collide on disk or in Git's ref namespace.
* :meth:`cleanup` can only ever name the exact path/branch its own naming
  scheme derives from the given ``run_id`` - it is structurally incapable of
  naming, and therefore deleting, a worktree it did not create.
* Every computed branch name carries the fixed ``work-run/`` prefix, so it can
  never equal a protected branch name (``main``/``master``); a run's own
  branch is never protected by construction, not by a runtime check.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gearmeshing_ai.application.ports.coding_executor import (
    _PROTECTED_BRANCHES as _CORE_PROTECTED_BRANCHES,
)
from gearmeshing_ai.application.ports.coding_executor import RepositoryContext

_BRANCH_PREFIX = "work-run/"
_WORKTREE_DIR_PREFIX = "work-run-"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Base branches this adapter is willing to branch a worktree from. Reuses the
# protected-branch set from the coding executor contract: a branch that is
# "protected" (never checked out for modification) is exactly the set of
# default branches this adapter is willing to treat as a trusted, read-only
# starting point for a new isolated branch.
_SUPPORTED_BASE_BRANCHES = _CORE_PROTECTED_BRANCHES


class GitWorktreeError(RuntimeError):
    """Base class for failures raised by the Git worktree adapter."""


class DirtyRepositoryError(GitWorktreeError):
    """Raised when the source repository is not in a clean state."""


class UnsupportedBaseBranchError(GitWorktreeError):
    """Raised when ``base_branch`` is not one of the supported default branches."""


class WorktreeConflictError(GitWorktreeError):
    """Raised when an existing worktree at the run's path is bound to a different branch."""


class UnrecognizedWorktreeError(GitWorktreeError):
    """Raised when cleanup's target does not match this adapter's own naming scheme."""


class GitCommandError(GitWorktreeError):
    """Raised when a ``git`` subprocess invocation exits with a non-zero status."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"`{' '.join(argv)}` exited with {returncode}: {stderr.strip()}")


def _validate_run_id(run_id: str) -> str:
    candidate = run_id.strip()
    if not _RUN_ID_PATTERN.fullmatch(candidate):
        raise ValueError("run_id is not a safe identifier")
    return candidate


def _ensure_supported_base_branch(base_branch: str) -> None:
    if base_branch not in _SUPPORTED_BASE_BRANCHES:
        allowed = ", ".join(sorted(_SUPPORTED_BASE_BRANCHES))
        raise UnsupportedBaseBranchError(
            f"{base_branch!r} is not a supported default branch (expected one of {allowed})"
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured outcome of one invoked command."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs one command and returns its captured result; injectable for tests."""

    def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult:
        """Execute ``argv`` with its working directory pinned to ``cwd``."""
        ...


class SubprocessCommandRunner:
    """Default :class:`CommandRunner` backed by ``subprocess.run``."""

    def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    """One entry parsed from ``git worktree list --porcelain``."""

    path: str
    branch: str | None


def _parse_worktree_list(output: str) -> tuple[WorktreeEntry, ...]:
    """Parse ``git worktree list --porcelain`` output into structured entries.

    Entries are blank-line delimited. Each entry starts with a ``worktree``
    line and may include a ``branch refs/heads/<name>`` line; a detached or
    bare entry has no ``branch`` line and is reported with ``branch=None``.
    """
    entries: list[WorktreeEntry] = []
    path: str | None = None
    branch: str | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                entries.append(WorktreeEntry(path, branch))
            path = line.removeprefix("worktree ").strip()
            branch = None
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").strip().removeprefix("refs/heads/")
        elif not line and path is not None:
            entries.append(WorktreeEntry(path, branch))
            path = None
            branch = None
    if path is not None:
        entries.append(WorktreeEntry(path, branch))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class GitWorktreeManager:
    """Creates, reconciles, and safely tears down one isolated Git worktree per run.

    ``repository_root`` is the existing clean checkout every worktree branches
    from. ``worktrees_root`` is a directory isolated from ``repository_root``
    (see :class:`RepositoryContext`) under which every run's worktree is
    created, named deterministically from its ``run_id``.
    """

    repository_root: str
    worktrees_root: str
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)

    def __post_init__(self) -> None:
        if not Path(self.repository_root).is_absolute() or not Path(self.worktrees_root).is_absolute():
            raise ValueError("repository_root and worktrees_root must be absolute paths")
        repository_root = Path(self.repository_root).resolve()
        worktrees_root = Path(self.worktrees_root).resolve()
        if (
            repository_root == worktrees_root
            or repository_root in worktrees_root.parents
            or worktrees_root in repository_root.parents
        ):
            raise ValueError("worktrees_root must be isolated from repository_root")
        object.__setattr__(self, "repository_root", str(repository_root))
        object.__setattr__(self, "worktrees_root", str(worktrees_root))

    @classmethod
    def for_sibling_worktrees(cls, repository_root: str) -> GitWorktreeManager:
        """Convenience constructor placing worktrees in a ``.worktrees`` sibling directory."""
        root = Path(repository_root).resolve()
        return cls(repository_root=str(root), worktrees_root=str(root.parent / ".worktrees"))

    def branch_name(self, run_id: str) -> str:
        """Compute the deterministic branch name for ``run_id``."""
        return f"{_BRANCH_PREFIX}{_validate_run_id(run_id)}"

    def worktree_path(self, run_id: str) -> str:
        """Compute the deterministic worktree path for ``run_id``."""
        return str(Path(self.worktrees_root) / f"{_WORKTREE_DIR_PREFIX}{_validate_run_id(run_id)}")

    def ensure_worktree(self, run_id: str, *, base_branch: str = "main") -> RepositoryContext:
        """Idempotently create, or reconcile, the isolated worktree for ``run_id``.

        Refuses to operate when ``repository_root`` is not clean, when
        ``base_branch`` is not a supported default branch, or when an existing
        worktree at this run's path is bound to a different branch.
        """
        _ensure_supported_base_branch(base_branch)
        branch = self.branch_name(run_id)
        path = self.worktree_path(run_id)
        self._require_clean_repository()
        existing = self._find_worktree(path)
        if existing is not None:
            if existing.branch != branch:
                raise WorktreeConflictError(
                    f"a worktree already exists at {path!r} bound to {existing.branch!r}, not {branch!r}"
                )
        elif self._branch_exists(branch):
            self._run_git(("worktree", "add", path, branch), cwd=self.repository_root)
        else:
            self._run_git(("worktree", "add", "-b", branch, path, base_branch), cwd=self.repository_root)
        return RepositoryContext(
            repository_root=self.repository_root,
            worktree_root=path,
            base_ref=base_branch,
            branch=branch,
        )

    def cleanup(self, run_id: str, *, preserve_evidence: bool = False) -> None:
        """Remove the worktree and branch this manager created for ``run_id``.

        Skips deletion entirely when ``preserve_evidence`` is set (for
        example, after a failed run whose worktree should be kept for
        inspection). Otherwise, only ever removes the path/branch this
        manager's own deterministic naming derives from ``run_id``: if a
        worktree exists at that path but is bound to a different branch, this
        raises instead of deleting it, so an unrelated developer worktree that
        happens to share the path can never be removed. Safe to call
        repeatedly - if nothing matching remains, it is a no-op.
        """
        if preserve_evidence:
            return
        branch = self.branch_name(run_id)
        path = self.worktree_path(run_id)
        existing = self._find_worktree(path)
        if existing is not None:
            if existing.branch != branch:
                raise UnrecognizedWorktreeError(
                    f"refusing to remove {path!r}: it is bound to {existing.branch!r}, not {branch!r}"
                )
            self._run_git(("worktree", "remove", "--force", path), cwd=self.repository_root)
        if self._branch_exists(branch):
            self._run_git(("branch", "-D", branch), cwd=self.repository_root)

    def _require_clean_repository(self) -> None:
        result = self._run_git(("status", "--porcelain"), cwd=self.repository_root)
        if result.stdout.strip():
            raise DirtyRepositoryError(
                f"{self.repository_root} has uncommitted changes; refusing to create an isolated worktree"
            )

    def _find_worktree(self, path: str) -> WorktreeEntry | None:
        return next((entry for entry in self._list_worktrees() if entry.path == path), None)

    def _list_worktrees(self) -> tuple[WorktreeEntry, ...]:
        result = self._run_git(("worktree", "list", "--porcelain"), cwd=self.repository_root)
        return _parse_worktree_list(result.stdout)

    def _branch_exists(self, branch: str) -> bool:
        argv = ("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return self.runner.run(argv, cwd=self.repository_root).returncode == 0

    def _run_git(self, args: Sequence[str], *, cwd: str) -> CommandResult:
        argv = ("git", *args)
        result = self.runner.run(argv, cwd=cwd)
        if result.returncode != 0:
            raise GitCommandError(argv, result.returncode, result.stderr)
        return result
