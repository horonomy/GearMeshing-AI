"""Provider-neutral contract for a governed coding executor."""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Protocol

type MetadataValue = str | int | float | None
type FrozenMetadata = Mapping[str, MetadataValue]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]*$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SENSITIVE_KEY_PARTS = frozenset(
    {"authorization", "bearer", "cookie", "credential", "passwd", "password", "secret", "session", "token"}
)
_PROTECTED_BRANCHES = frozenset({"main", "master"})


def _required_text(value: str, name: str, *, maximum: int = 512) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def _required_exact_text(value: str, name: str, *, maximum: int) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError(f"{name} must not contain unsupported control characters")
    return value


def _identifier(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=128)
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} has an invalid format")
    return normalized


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _relative_path(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=1024)
    path = PurePosixPath(normalized)
    if normalized == "." or path.is_absolute() or normalized != path.as_posix() or ".." in path.parts:
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    return normalized


def _absolute_path(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=1024)
    path = PurePosixPath(normalized)
    if not path.is_absolute() or normalized != path.as_posix() or ".." in path.parts or path == PurePosixPath("/"):
        raise ValueError(f"{name} must be a normalized absolute POSIX path")
    return normalized


def _git_ref(value: str, name: str) -> str:
    normalized = _required_text(value, name, maximum=256)
    invalid_fragments = ("..", "//", "@{", "\\")
    invalid_characters = frozenset(" ~^:?*[")
    components = normalized.split("/")
    if (
        normalized.startswith(("/", "-"))
        or normalized.endswith(("/", "."))
        or any(fragment in normalized for fragment in invalid_fragments)
        or any(character in invalid_characters for character in normalized)
        or any(component.startswith(".") or component.endswith(".lock") for component in components)
    ):
        raise ValueError(f"{name} must be a normalized Git ref")
    return normalized


def _local_branch(value: str) -> str:
    normalized = _git_ref(value, "branch")
    if normalized.startswith("refs/heads/"):
        normalized = normalized.removeprefix("refs/heads/")
    elif normalized.startswith("refs/"):
        raise ValueError("branch must identify a local branch")
    if normalized in {"@", "HEAD"}:
        raise ValueError("branch must identify a local branch")
    return _git_ref(normalized, "branch")


def _frozen_metadata(metadata: Mapping[str, MetadataValue]) -> FrozenMetadata:
    """Canonicalize safe keys and snapshot scalar metadata.

    Values are structurally validated but are not secret-scanned. Callers must
    redact credential material upstream.
    """
    snapshot: dict[str, MetadataValue] = {}
    for raw_key, value in metadata.items():
        key_text = _required_text(raw_key, "metadata key", maximum=64)
        separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_text)
        key = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
        if not key:
            raise ValueError("metadata key must contain letters or digits")
        parts = frozenset(key.split("_"))
        base_parts = frozenset(re.sub(r"\d+$", "", part) for part in parts)
        key_bearing_secret = "key" in base_parts and bool(base_parts & {"access", "api", "private"})
        collapsed_key = re.sub(r"\d+$", "", key.replace("_", ""))
        if (
            base_parts & _SENSITIVE_KEY_PARTS
            or key_bearing_secret
            or collapsed_key in {"accesskey", "apikey", "privatekey"}
        ):
            raise ValueError(f"metadata key {key_text!r} may contain credentials")
        if key in snapshot:
            raise ValueError(f"metadata keys normalize to duplicate {key!r}")
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

    COMPLETED = "completed"
    BLOCKED = "blocked"
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


_OUTCOME_FAILURE_CATEGORIES: Mapping[TerminalOutcome, frozenset[FailureCategory]] = MappingProxyType(
    {
        TerminalOutcome.COMPLETED: frozenset(),
        TerminalOutcome.BLOCKED: frozenset({FailureCategory.POLICY}),
        TerminalOutcome.FAILED: frozenset(
            {
                FailureCategory.INVALID_REQUEST,
                FailureCategory.PROVIDER,
                FailureCategory.TOOL,
                FailureCategory.VERIFICATION,
                FailureCategory.INTERNAL,
            }
        ),
        TerminalOutcome.CANCELLED: frozenset({FailureCategory.CANCELLED}),
        TerminalOutcome.TIMED_OUT: frozenset({FailureCategory.TIMEOUT}),
        TerminalOutcome.RESOURCE_EXHAUSTED: frozenset({FailureCategory.RESOURCE}),
    }
)


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    """Repository and isolated worktree locations for one execution."""

    repository_root: str
    worktree_root: str
    base_ref: str
    branch: str
    writable_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        repository_root = PurePosixPath(_absolute_path(self.repository_root, "repository_root"))
        worktree_root = PurePosixPath(_absolute_path(self.worktree_root, "worktree_root"))
        if repository_root == worktree_root:
            raise ValueError("worktree_root must be isolated from repository_root")
        if repository_root in worktree_root.parents or worktree_root in repository_root.parents:
            raise ValueError("repository and worktree roots must not contain one another")

        paths = tuple(_relative_path(path, "writable path") for path in self.writable_paths)
        if len(paths) != len(set(paths)):
            raise ValueError("writable_paths must not contain duplicates")
        object.__setattr__(self, "repository_root", repository_root.as_posix())
        object.__setattr__(self, "worktree_root", worktree_root.as_posix())
        base_ref = _git_ref(self.base_ref, "base_ref")
        branch = _local_branch(self.branch)
        base_branch = base_ref.removeprefix("refs/heads/")
        if branch == base_branch or branch in _PROTECTED_BRANCHES:
            raise ValueError("branch must differ from the base and must not be protected")
        object.__setattr__(self, "base_ref", base_ref)
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "writable_paths", paths)


@dataclass(frozen=True, slots=True)
class ApprovedSpecification:
    """Immutable identity of the exact human-approved input."""

    issue_key: str
    revision: str
    content: str
    content_sha256: str
    approved_by: str

    def __post_init__(self) -> None:
        issue_key = _required_text(self.issue_key, "issue_key", maximum=32)
        if _ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
            raise ValueError("issue_key must be an uppercase Jira issue key")
        digest = self.content_sha256.strip().lower()
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        content = _required_exact_text(self.content, "content", maximum=1_000_000)
        if sha256(content.encode()).hexdigest() != digest:
            raise ValueError("content_sha256 does not match content")
        object.__setattr__(self, "issue_key", issue_key)
        object.__setattr__(self, "revision", _identifier(self.revision, "revision"))
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "approved_by", _identifier(self.approved_by, "approved_by"))


@dataclass(frozen=True, slots=True)
class ToolGrant:
    """Least-privilege operations and executables granted to one tool."""

    tool: str
    operations: frozenset[str]
    commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        operations = frozenset(_identifier(operation, "operation") for operation in self.operations)
        if not operations:
            raise ValueError("operations must not be empty")
        commands = tuple(_required_text(command, "command", maximum=64) for command in self.commands)
        if any(_COMMAND_PATTERN.fullmatch(command) is None for command in commands):
            raise ValueError("commands must be executable names without paths or shell syntax")
        if len(commands) != len(set(commands)):
            raise ValueError("commands must not contain duplicates")
        object.__setattr__(self, "tool", _identifier(self.tool, "tool"))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "commands", commands)


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Finite upper bounds that every provider must enforce."""

    wall_clock_seconds: float
    max_events: int
    max_artifacts: int
    max_artifact_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wall_clock_seconds",
            _finite_positive_float(self.wall_clock_seconds, "wall_clock_seconds"),
        )
        object.__setattr__(self, "max_events", _positive_int(self.max_events, "max_events"))
        if self.max_events < 2:
            raise ValueError("max_events must reserve STARTED and TERMINAL events")
        object.__setattr__(self, "max_artifacts", _positive_int(self.max_artifacts, "max_artifacts"))
        object.__setattr__(
            self,
            "max_artifact_bytes",
            _positive_int(self.max_artifact_bytes, "max_artifact_bytes"),
        )


@dataclass(frozen=True, slots=True)
class ExecutorCapabilities:
    """Discoverable behavior supported by an executor implementation."""

    streaming: bool
    cancellation: bool
    tool_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.streaming, bool) or not isinstance(self.cancellation, bool):
            raise ValueError("streaming and cancellation must be booleans")
        object.__setattr__(
            self,
            "tool_names",
            frozenset(_identifier(tool, "tool name") for tool in self.tool_names),
        )


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Complete governed input needed to start one execution."""

    execution_id: str
    specification: ApprovedSpecification
    repository: RepositoryContext
    limits: ResourceLimits
    tool_grants: tuple[ToolGrant, ...]
    metadata: FrozenMetadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        grants = tuple(self.tool_grants)
        names = tuple(grant.tool for grant in grants)
        if len(names) != len(set(names)):
            raise ValueError("tool_grants must contain at most one grant per tool")
        object.__setattr__(self, "execution_id", _identifier(self.execution_id, "execution_id"))
        object.__setattr__(self, "tool_grants", grants)
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """Ordered, sanitized observation from an execution session."""

    sequence: int
    kind: EventKind
    message: str
    metadata: FrozenMetadata = field(default_factory=dict)
    artifact: ExecutionArtifact | None = None

    def __post_init__(self) -> None:
        if (self.kind is EventKind.ARTIFACT) is (self.artifact is None):
            raise ValueError("artifact events require exactly one typed artifact payload")
        object.__setattr__(self, "sequence", _positive_int(self.sequence, "sequence"))
        object.__setattr__(self, "message", _required_text(self.message, "message", maximum=2048))
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class FailureMetadata:
    """Sanitized machine-readable explanation for a non-success outcome."""

    category: FailureCategory
    code: str
    message: str
    retryable: bool = False
    details: FrozenMetadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        object.__setattr__(self, "code", _identifier(self.code, "failure code"))
        object.__setattr__(self, "message", _required_text(self.message, "failure message", maximum=2048))
        object.__setattr__(self, "details", _frozen_metadata(self.details))


@dataclass(frozen=True, slots=True)
class ExecutionArtifact:
    """Bounded artifact produced inside the isolated worktree."""

    relative_path: str
    media_type: str
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        digest = self.content_sha256.strip().lower()
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "relative_path", _relative_path(self.relative_path, "artifact path"))
        object.__setattr__(self, "media_type", _required_text(self.media_type, "media_type", maximum=128))
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "size_bytes", _positive_int(self.size_bytes, "size_bytes"))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Validated terminal result constrained by the request's limits.

    Failure categories are explicit: blocked=policy; cancelled=cancelled;
    timed_out=timeout; resource_exhausted=resource; failed accepts only invalid
    request, provider, tool, verification, or internal failures. Completed has no
    failure.
    """

    execution_id: str
    outcome: TerminalOutcome
    limits: ResourceLimits
    events_emitted: int
    artifacts: tuple[ExecutionArtifact, ...] = ()
    failure: FailureMetadata | None = None

    def __post_init__(self) -> None:
        artifacts = tuple(self.artifacts)
        events_emitted = _positive_int(self.events_emitted, "events_emitted")
        if events_emitted > self.limits.max_events:
            raise ValueError("events_emitted exceeds max_events")
        if len(artifacts) > self.limits.max_artifacts:
            raise ValueError("artifacts exceeds max_artifacts")
        if sum(artifact.size_bytes for artifact in artifacts) > self.limits.max_artifact_bytes:
            raise ValueError("artifact bytes exceeds max_artifact_bytes")
        if self.outcome is TerminalOutcome.COMPLETED and self.failure is not None:
            raise ValueError("completed results must not include a failure")
        if self.outcome is not TerminalOutcome.COMPLETED and self.failure is None:
            raise ValueError("non-success results must include a failure")
        allowed_categories = _OUTCOME_FAILURE_CATEGORIES[self.outcome]
        if self.failure is not None and self.failure.category not in allowed_categories:
            allowed = ", ".join(category.value for category in sorted(allowed_categories, key=str))
            raise ValueError(f"{self.outcome.value} results require one of these failure categories: {allowed}")
        object.__setattr__(self, "execution_id", _identifier(self.execution_id, "execution_id"))
        object.__setattr__(self, "events_emitted", events_emitted)
        object.__setattr__(self, "artifacts", artifacts)


class ExecutionSession(Protocol):
    """One running execution with a single ordered event stream."""

    @property
    def execution_id(self) -> str:
        """Return the request identity represented by this session."""
        ...

    def events(self) -> AsyncIterator[ExecutionEvent]:
        """Stream each event once in strictly increasing sequence order."""
        ...

    async def result(self) -> ExecutionResult:
        """Wait for and return the stable terminal result."""
        ...

    async def cancel(self, reason: str) -> None:
        """Request idempotent cancellation without exposing provider details."""
        ...


class CodingExecutor(Protocol):
    """Port implemented by provider-specific governed coding adapters."""

    @property
    def capabilities(self) -> ExecutorCapabilities:
        """Return immutable capabilities before an execution is started."""
        ...

    async def start(self, request: ExecutionRequest) -> ExecutionSession:
        """Start one isolated execution or return its existing session."""
        ...
