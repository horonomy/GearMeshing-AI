"""Provider-neutral contract for a governed coding executor."""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Protocol, TypeAlias

MetadataValue: TypeAlias = str | int | float | None
FrozenMetadata: TypeAlias = Mapping[str, MetadataValue]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]*$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SENSITIVE_KEY_PARTS = ("authorization", "credential", "password", "secret", "token")


def _required_text(value: str, name: str, *, maximum: int = 512) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def _identifier(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=128)
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} has an invalid format")
    return normalized


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _relative_path(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=1024)
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized != path.as_posix() or ".." in path.parts:
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    return normalized


def _frozen_metadata(metadata: Mapping[str, MetadataValue]) -> FrozenMetadata:
    snapshot: dict[str, MetadataValue] = {}
    for raw_key, value in metadata.items():
        key = _required_text(raw_key, "metadata key", maximum=64)
        lowered = key.casefold()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            raise ValueError(f"metadata key {key!r} may contain credentials")
        if isinstance(value, bool):
            raise ValueError(f"metadata value for {key!r} must not be boolean")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"metadata value for {key!r} must be finite")
        if isinstance(value, str):
            value = _required_text(value, f"metadata value for {key!r}", maximum=1024)
        elif value is not None and not isinstance(value, (int, float)):
            raise ValueError(f"metadata value for {key!r} has an unsupported type")
        snapshot[key] = value
    return MappingProxyType(snapshot)


class TerminalOutcome(StrEnum):
    """Finite terminal states reported by every execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RESOURCE_EXHAUSTED = "resource_exhausted"


class FailureCategory(StrEnum):
    """Portable failure categories independent of a coding provider."""

    POLICY = "policy"
    INVALID_REQUEST = "invalid_request"
    PROVIDER = "provider"
    TOOL = "tool"
    VERIFICATION = "verification"
    RESOURCE = "resource"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class EventKind(StrEnum):
    """Observable lifecycle events emitted in sequence order."""

    STARTED = "started"
    PROGRESS = "progress"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    ARTIFACT = "artifact"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    """Repository and isolated worktree locations for one execution."""

    repository_root: str
    worktree_root: str
    base_ref: str
    branch: str
    writable_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        repository_root = PurePosixPath(_required_text(self.repository_root, "repository_root", maximum=1024))
        worktree_root = PurePosixPath(_required_text(self.worktree_root, "worktree_root", maximum=1024))
        if not repository_root.is_absolute() or not worktree_root.is_absolute():
            raise ValueError("repository and worktree roots must be absolute POSIX paths")
        if repository_root == worktree_root:
            raise ValueError("worktree_root must be isolated from repository_root")
        if repository_root in worktree_root.parents or worktree_root in repository_root.parents:
            raise ValueError("repository and worktree roots must not contain one another")

        paths = tuple(_relative_path(path, "writable path") for path in self.writable_paths)
        if len(paths) != len(set(paths)):
            raise ValueError("writable_paths must not contain duplicates")
        object.__setattr__(self, "repository_root", repository_root.as_posix())
        object.__setattr__(self, "worktree_root", worktree_root.as_posix())
        object.__setattr__(self, "base_ref", _identifier(self.base_ref, "base_ref"))
        object.__setattr__(self, "branch", _required_text(self.branch, "branch", maximum=256))
        object.__setattr__(self, "writable_paths", paths)
