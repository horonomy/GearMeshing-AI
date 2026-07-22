"""Deterministic orchestration for a governed engineering work run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from gearmeshing_ai.domain.work_run import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    WorkRun,
    WorkRunArtifact,
    WorkRunCorrelation,
    WorkRunState,
)


class WorkflowIntegrityError(RuntimeError):
    """Raised when persisted workflow history diverges from the approved run."""


class WorkflowStage(StrEnum):
    """Stable names used to scope idempotent workflow operations."""

    EXECUTION = "execution"
    VERIFICATION = "verification"
    REMEDIATION = "remediation"
    DRAFT_PR_PUBLICATION = "draft_pr_publication"


@dataclass(frozen=True, slots=True)
class StageRequest:
    """Immutable context supplied to one idempotent workflow operation."""

    run_id: str
    correlation: WorkRunCorrelation
    artifacts: tuple[WorkRunArtifact, ...]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Evidence-backed result returned by the verification boundary."""

    passed: bool
    artifacts: tuple[WorkRunArtifact, ...] = ()


class ExecutionPort(Protocol):
    """Execute the approved engineering change."""

    def execute(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]: ...


class VerificationPort(Protocol):
    """Verify the current change and return immutable evidence."""

    def verify(self, request: StageRequest) -> VerificationResult: ...


class RemediationPort(Protocol):
    """Correct a failed verification attempt."""

    def remediate(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]: ...


class DraftPrPublisher(Protocol):
    """Publish or recover the single Draft PR for a work run."""

    def publish(self, request: StageRequest) -> str: ...


class WorkflowCheckpointStore(Protocol):
    """Persist workflow checkpoints using compare-and-swap semantics."""

    def load(self, run_id: str) -> WorkRun | None: ...

    def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None: ...


Clock = Callable[[], datetime]


class WorkflowRunner:
    """Resume a work run from trusted checkpoints until it reaches a terminal state."""

    def __init__(
        self,
        *,
        checkpoints: WorkflowCheckpointStore,
        executor: ExecutionPort,
        verifier: VerificationPort,
        remediator: RemediationPort,
        publisher: DraftPrPublisher,
        clock: Clock,
        actor_id: str = "agent-assembly",
        max_remediation_cycles: int = 3,
    ) -> None:
        if not actor_id.strip():
            raise ValueError("actor_id must not be blank")
        if max_remediation_cycles < 0:
            raise ValueError("max_remediation_cycles must not be negative")
        self._checkpoints = checkpoints
        self._executor = executor
        self._verifier = verifier
        self._remediator = remediator
        self._publisher = publisher
        self._clock = clock
        self._actor_id = actor_id
        self._max_remediation_cycles = max_remediation_cycles

    def run(self, approved: WorkRun) -> WorkRun:
        """Run or idempotently resume one pristine, human-approved work run."""

        self._validate_approved_input(approved)
        current = self._restore(approved)
        while current.state not in TERMINAL_STATES:
            if current.state is WorkRunState.APPROVED:
                current = self._transition(current, WorkRunState.EXECUTING)
            elif current.state is WorkRunState.EXECUTING:
                current = self._execute(current)
            elif current.state is WorkRunState.VERIFYING:
                current = self._verify(current)
            elif current.state is WorkRunState.REMEDIATING:
                current = self._remediate(current)
            elif current.state is WorkRunState.PUBLISHING_DRAFT_PR:
                current = self._publish(current)
            else:  # pragma: no cover - WorkRunState is exhaustively handled above
                raise WorkflowIntegrityError(f"unsupported checkpoint state: {current.state.value}")
        return current

    def _restore(self, approved: WorkRun) -> WorkRun:
        checkpoint = self._checkpoints.load(approved.run_id)
        if checkpoint is None:
            self._checkpoints.save(expected=None, updated=approved)
            return approved
        self._validate_checkpoint(checkpoint)
        if checkpoint.correlation != approved.correlation or checkpoint.events[:1] != approved.events:
            raise WorkflowIntegrityError("checkpoint does not extend the approved work run")
        return checkpoint

    @staticmethod
    def _validate_approved_input(run: WorkRun) -> None:
        if (
            run.state is not WorkRunState.APPROVED
            or len(run.events) != 1
            or run.artifacts
            or run.draft_pr_url is not None
        ):
            raise WorkflowIntegrityError("workflow input must be a pristine approved work run")

    @staticmethod
    def _validate_checkpoint(run: WorkRun) -> None:
        previous_state = WorkRunState.APPROVED
        artifact_events = []
        draft_pr_events = []
        for event in run.events[1:]:
            if event.state is previous_state:
                if event.name == "artifact_attached":
                    artifact_events.append(event)
                elif event.name == "draft_pr_recorded" and event.state is WorkRunState.PUBLISHING_DRAFT_PR:
                    draft_pr_events.append(event)
                else:
                    raise WorkflowIntegrityError("checkpoint contains an invalid same-state event")
            elif event.state not in ALLOWED_TRANSITIONS[previous_state]:
                raise WorkflowIntegrityError("checkpoint contains an invalid state transition")
            previous_state = event.state

        recorded_artifacts = tuple(dict(event.details)["artifact_id"] for event in artifact_events)
        if recorded_artifacts != tuple(artifact.artifact_id for artifact in run.artifacts):
            raise WorkflowIntegrityError("checkpoint evidence does not match its audit events")
        if run.draft_pr_url is None and draft_pr_events:
            raise WorkflowIntegrityError("checkpoint lost its recorded Draft PR")
        if run.draft_pr_url is not None and (
            len(draft_pr_events) != 1 or dict(draft_pr_events[0].details).get("draft_pr_url") != run.draft_pr_url
        ):
            raise WorkflowIntegrityError("checkpoint Draft PR does not match its audit event")

    def _execute(self, run: WorkRun) -> WorkRun:
        request = self._request(run, WorkflowStage.EXECUTION, 1)
        try:
            artifacts = self._executor.execute(request)
        except Exception:
            return self._fail(run, WorkflowStage.EXECUTION)
        updated = self._attach_all(run, artifacts)
        return self._transition_from(run, updated, WorkRunState.VERIFYING)

    def _verify(self, run: WorkRun) -> WorkRun:
        attempt = 1 + self._event_count(run, "entered_remediating")
        request = self._request(run, WorkflowStage.VERIFICATION, attempt)
        try:
            result = self._verifier.verify(request)
        except Exception:
            return self._fail(run, WorkflowStage.VERIFICATION)
        updated = self._attach_all(run, result.artifacts)
        if result.passed:
            return self._transition_from(run, updated, WorkRunState.PUBLISHING_DRAFT_PR)
        if attempt > self._max_remediation_cycles:
            return self._fail_from(run, updated, WorkflowStage.VERIFICATION, "remediation_limit_reached")
        return self._transition_from(run, updated, WorkRunState.REMEDIATING)

    def _remediate(self, run: WorkRun) -> WorkRun:
        attempt = self._event_count(run, "entered_remediating")
        request = self._request(run, WorkflowStage.REMEDIATION, attempt)
        try:
            artifacts = self._remediator.remediate(request)
        except Exception:
            return self._fail(run, WorkflowStage.REMEDIATION)
        updated = self._attach_all(run, artifacts)
        return self._transition_from(run, updated, WorkRunState.VERIFYING)

    def _publish(self, run: WorkRun) -> WorkRun:
        request = self._request(run, WorkflowStage.DRAFT_PR_PUBLICATION, 1)
        try:
            url = self._publisher.publish(request)
        except Exception:
            return self._fail(run, WorkflowStage.DRAFT_PR_PUBLICATION)
        updated = run.record_draft_pr(url, actor_id=self._actor_id, occurred_at=self._next_time(run))
        return self._transition_from(run, updated, WorkRunState.COMPLETED)

    def _attach_all(self, run: WorkRun, artifacts: tuple[WorkRunArtifact, ...]) -> WorkRun:
        updated = run
        for artifact in artifacts:
            updated = updated.attach_artifact(
                artifact,
                actor_id=self._actor_id,
                occurred_at=self._next_time(updated),
            )
        return updated

    def _transition(self, run: WorkRun, target: WorkRunState) -> WorkRun:
        return self._transition_from(run, run, target)

    def _transition_from(self, expected: WorkRun, updated: WorkRun, target: WorkRunState) -> WorkRun:
        transitioned = updated.transition_to(
            target,
            actor_id=self._actor_id,
            occurred_at=self._next_time(updated),
        )
        self._checkpoints.save(expected=expected, updated=transitioned)
        return transitioned

    def _fail(self, run: WorkRun, stage: WorkflowStage) -> WorkRun:
        return self._fail_from(run, run, stage, "operation_failed")

    def _fail_from(self, expected: WorkRun, updated: WorkRun, stage: WorkflowStage, code: str) -> WorkRun:
        failed = updated.transition_to(
            WorkRunState.FAILED,
            actor_id=self._actor_id,
            occurred_at=self._next_time(updated),
            details=(("failure_code", code), ("stage", stage.value)),
        )
        self._checkpoints.save(expected=expected, updated=failed)
        return failed

    def _next_time(self, run: WorkRun) -> datetime:
        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise WorkflowIntegrityError("workflow clock must return a timezone-aware timestamp")
        if occurred_at <= run.events[-1].occurred_at:
            raise WorkflowIntegrityError("workflow timestamps must increase monotonically")
        return occurred_at

    @staticmethod
    def _event_count(run: WorkRun, name: str) -> int:
        return sum(event.name == name for event in run.events)

    @staticmethod
    def _request(run: WorkRun, stage: WorkflowStage, attempt: int) -> StageRequest:
        return StageRequest(
            run_id=run.run_id,
            correlation=run.correlation,
            artifacts=run.artifacts,
            idempotency_key=f"{run.run_id}:{stage.value}:{attempt}",
        )
