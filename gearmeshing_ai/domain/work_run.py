"""Domain model for a governed engineering work run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit


class WorkRunValidationError(ValueError):
    """Raised when work-run data violates a domain invariant."""


class InvalidTransitionError(WorkRunValidationError):
    """Raised when a lifecycle transition is not permitted."""


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_JIRA_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]*$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def _require_identifier(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(candidate) or ".." in candidate:
        raise WorkRunValidationError(f"{field_name} is not a safe identifier")
    return candidate


def _require_safe_url(value: str, field_name: str, *, schemes: frozenset[str]) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in schemes or not parsed.netloc:
        raise WorkRunValidationError(f"{field_name} must use an allowed absolute URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise WorkRunValidationError(f"{field_name} must not contain credentials")
    if parsed.fragment:
        raise WorkRunValidationError(f"{field_name} must not contain a fragment")
    return candidate


@dataclass(frozen=True, slots=True)
class WorkRunCorrelation:
    """Stable external references used throughout a work run."""

    jira_issue_key: str
    jira_issue_url: str
    repository_url: str
    branch_name: str
    agent_assembly_run_id: str

    def __post_init__(self) -> None:
        issue_key = self.jira_issue_key.strip().upper()
        if not _JIRA_ISSUE_KEY_PATTERN.fullmatch(issue_key):
            raise WorkRunValidationError("jira_issue_key must be a valid Jira issue key")
        object.__setattr__(self, "jira_issue_key", issue_key)
        object.__setattr__(
            self,
            "jira_issue_url",
            _require_safe_url(self.jira_issue_url, "jira_issue_url", schemes=frozenset({"https"})),
        )
        object.__setattr__(
            self,
            "repository_url",
            _require_safe_url(self.repository_url, "repository_url", schemes=frozenset({"https"})),
        )
        object.__setattr__(self, "branch_name", _require_identifier(self.branch_name, "branch_name"))
        object.__setattr__(
            self,
            "agent_assembly_run_id",
            _require_identifier(self.agent_assembly_run_id, "agent_assembly_run_id"),
        )


@dataclass(frozen=True, slots=True)
class WorkRunArtifact:
    """An immutable reference to evidence produced by the run."""

    artifact_id: str
    kind: str
    uri: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _require_identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "kind", _require_identifier(self.kind, "kind"))
        object.__setattr__(
            self,
            "uri",
            _require_safe_url(self.uri, "uri", schemes=frozenset({"artifact", "https"})),
        )
        if self.sha256 is not None and not _SHA256_PATTERN.fullmatch(self.sha256):
            raise WorkRunValidationError("sha256 must be a lowercase hexadecimal SHA-256 digest")


@dataclass(frozen=True, slots=True)
class WorkRunEvent:
    """An ordered, immutable audit event emitted by a work run."""

    sequence: int
    name: str
    state: WorkRunState
    actor_id: str
    occurred_at: datetime
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise WorkRunValidationError("event sequence must be positive")
        object.__setattr__(self, "name", _require_identifier(self.name, "event name"))
        object.__setattr__(self, "actor_id", _require_identifier(self.actor_id, "actor_id"))
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise WorkRunValidationError("occurred_at must be timezone-aware")
        keys = [key for key, _ in self.details]
        if len(keys) != len(set(keys)):
            raise WorkRunValidationError("event detail keys must be unique")
        for key, value in self.details:
            _require_identifier(key, "event detail key")
            if not value.strip():
                raise WorkRunValidationError("event detail values must not be blank")


class WorkRunState(StrEnum):
    """A stable state in the governed work-run lifecycle."""

    APPROVED = "approved"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REMEDIATING = "remediating"
    PUBLISHING_DRAFT_PR = "publishing_draft_pr"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {
        WorkRunState.COMPLETED,
        WorkRunState.FAILED,
        WorkRunState.BLOCKED,
        WorkRunState.CANCELLED,
    }
)
