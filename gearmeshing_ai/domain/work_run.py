"""Domain model for a governed engineering work run."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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


@dataclass(frozen=True, slots=True)
class WorkRun:
    """Immutable aggregate governing one approved engineering change."""

    run_id: str
    correlation: WorkRunCorrelation
    state: WorkRunState
    events: tuple[WorkRunEvent, ...]
    artifacts: tuple[WorkRunArtifact, ...] = ()
    draft_pr_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_identifier(self.run_id, "run_id"))
        if not self.events:
            raise WorkRunValidationError("a work run must contain its approval event")
        if self.events[0].sequence != 1 or self.events[0].state is not WorkRunState.APPROVED:
            raise WorkRunValidationError("the first event must record approval")
        if tuple(event.sequence for event in self.events) != tuple(range(1, len(self.events) + 1)):
            raise WorkRunValidationError("event sequences must be contiguous")
        if self.events[-1].state is not self.state:
            raise WorkRunValidationError("the latest event state must match the work run state")
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise WorkRunValidationError("artifact IDs must be unique")
        if self.draft_pr_url is not None:
            object.__setattr__(
                self,
                "draft_pr_url",
                _require_safe_url(self.draft_pr_url, "draft_pr_url", schemes=frozenset({"https"})),
            )
        if self.state is WorkRunState.COMPLETED and self.draft_pr_url is None:
            raise WorkRunValidationError("a completed work run requires a Draft PR URL")

    @classmethod
    def approve(
        cls,
        *,
        run_id: str,
        correlation: WorkRunCorrelation,
        actor_id: str,
        occurred_at: datetime,
    ) -> WorkRun:
        """Create a work run at the explicit human approval checkpoint."""

        event = WorkRunEvent(
            sequence=1,
            name="approved",
            state=WorkRunState.APPROVED,
            actor_id=actor_id,
            occurred_at=occurred_at,
        )
        return cls(
            run_id=run_id,
            correlation=correlation,
            state=WorkRunState.APPROVED,
            events=(event,),
        )

    def transition_to(
        self,
        target: WorkRunState,
        *,
        actor_id: str,
        occurred_at: datetime,
        details: tuple[tuple[str, str], ...] = (),
    ) -> WorkRun:
        """Advance to a permitted state and append its audit event."""

        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransitionError(f"cannot transition from {self.state.value} to {target.value}")
        event = WorkRunEvent(
            sequence=len(self.events) + 1,
            name=f"entered_{target.value}",
            state=target,
            actor_id=actor_id,
            occurred_at=occurred_at,
            details=details,
        )
        return replace(self, state=target, events=(*self.events, event))

    def attach_artifact(
        self,
        artifact: WorkRunArtifact,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> WorkRun:
        """Append evidence without mutating previously recorded evidence."""

        if self.state in TERMINAL_STATES:
            raise WorkRunValidationError("evidence cannot be attached to a terminal work run")
        if any(existing.artifact_id == artifact.artifact_id for existing in self.artifacts):
            raise WorkRunValidationError(f"artifact {artifact.artifact_id!r} is already attached")
        event = WorkRunEvent(
            sequence=len(self.events) + 1,
            name="artifact_attached",
            state=self.state,
            actor_id=actor_id,
            occurred_at=occurred_at,
            details=(("artifact_id", artifact.artifact_id), ("kind", artifact.kind)),
        )
        return replace(
            self,
            events=(*self.events, event),
            artifacts=(*self.artifacts, artifact),
        )

    def record_draft_pr(
        self,
        url: str,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> WorkRun:
        """Record the reviewable Draft PR before the run can complete."""

        if self.state is not WorkRunState.PUBLISHING_DRAFT_PR:
            raise WorkRunValidationError("a Draft PR can only be recorded while publishing")
        if self.draft_pr_url is not None:
            raise WorkRunValidationError("the Draft PR URL has already been recorded")
        draft_pr_url = _require_safe_url(url, "draft_pr_url", schemes=frozenset({"https"}))
        event = WorkRunEvent(
            sequence=len(self.events) + 1,
            name="draft_pr_recorded",
            state=self.state,
            actor_id=actor_id,
            occurred_at=occurred_at,
            details=(("draft_pr_url", draft_pr_url),),
        )
        return replace(
            self,
            events=(*self.events, event),
            draft_pr_url=draft_pr_url,
        )


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

ALLOWED_TRANSITIONS: dict[WorkRunState, frozenset[WorkRunState]] = {
    WorkRunState.APPROVED: frozenset(
        {WorkRunState.EXECUTING, WorkRunState.BLOCKED, WorkRunState.FAILED, WorkRunState.CANCELLED}
    ),
    WorkRunState.EXECUTING: frozenset(
        {WorkRunState.VERIFYING, WorkRunState.BLOCKED, WorkRunState.FAILED, WorkRunState.CANCELLED}
    ),
    WorkRunState.VERIFYING: frozenset(
        {
            WorkRunState.REMEDIATING,
            WorkRunState.PUBLISHING_DRAFT_PR,
            WorkRunState.BLOCKED,
            WorkRunState.FAILED,
            WorkRunState.CANCELLED,
        }
    ),
    WorkRunState.REMEDIATING: frozenset(
        {WorkRunState.VERIFYING, WorkRunState.BLOCKED, WorkRunState.FAILED, WorkRunState.CANCELLED}
    ),
    WorkRunState.PUBLISHING_DRAFT_PR: frozenset(
        {WorkRunState.COMPLETED, WorkRunState.BLOCKED, WorkRunState.FAILED, WorkRunState.CANCELLED}
    ),
    WorkRunState.COMPLETED: frozenset(),
    WorkRunState.FAILED: frozenset(),
    WorkRunState.BLOCKED: frozenset(),
    WorkRunState.CANCELLED: frozenset(),
}
