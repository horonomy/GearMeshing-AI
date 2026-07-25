"""Deterministic orchestration for a governed engineering work run."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from gearmeshing_ai.application.ports.coding_executor import (
    ApprovedSpecification,
    CodingExecutor,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    RepositoryContext,
    ResourceLimits,
    TerminalOutcome,
    ToolGrant,
)
from gearmeshing_ai.application.ports.work_management import (
    BlockerUpdate,
    CompletionUpdate,
    ReadinessResult,
    WorkItem,
    WorkManagementProvider,
    canonical_work_item_content,
)
from gearmeshing_ai.domain.work_run import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    WorkRun,
    WorkRunArtifact,
    WorkRunEvent,
    WorkRunState,
)


class WorkflowIntegrityError(RuntimeError):
    """Raised when persisted workflow history diverges from the approved run."""


class WorkflowStage(StrEnum):
    """Stable names used to scope idempotent workflow operations."""

    INGEST = "ingest"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    REMEDIATION = "remediation"
    DRAFT_PR_PUBLICATION = "draft_pr_publication"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Evidence-backed result returned by the verification boundary."""

    passed: bool
    artifacts: tuple[WorkRunArtifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a boolean")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, WorkRunArtifact) for artifact in artifacts):
            raise TypeError("artifacts must contain only WorkRunArtifact values")
        object.__setattr__(self, "artifacts", artifacts)


class VerificationPort(Protocol):
    """Verify the current change and return immutable evidence."""

    async def verify(self, run: WorkRun) -> VerificationResult: ...


class RemediationPort(Protocol):
    """Correct a failed verification attempt."""

    async def remediate(self, run: WorkRun) -> tuple[WorkRunArtifact, ...]: ...


class DraftPrPublisher(Protocol):
    """Publish or recover the single Draft PR for a work run."""

    async def publish(self, run: WorkRun) -> str: ...


class WorkflowCheckpointStore(Protocol):
    """Persist workflow checkpoints using compare-and-swap semantics."""

    def load(self, run_id: str) -> WorkRun | None: ...

    def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None: ...


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ExecutionEnvironment:
    """Repository isolation and least-privilege bounds for one work item.

    Supplied by the caller per work item (not persisted on the domain
    aggregate) because these are execution-infrastructure concerns - where
    the isolated worktree lives, what the executor may touch, and how much
    it may consume - rather than governance state.
    """

    repository: RepositoryContext
    limits: ResourceLimits
    tool_grants: tuple[ToolGrant, ...] = ()


class WorkflowRunner:
    """Resume a work run from trusted checkpoints until it reaches a terminal state."""

    def __init__(
        self,
        *,
        checkpoints: WorkflowCheckpointStore,
        work_management: WorkManagementProvider,
        executor: CodingExecutor,
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
        self._work_management = work_management
        self._executor = executor
        self._verifier = verifier
        self._remediator = remediator
        self._publisher = publisher
        self._clock = clock
        self._actor_id = actor_id
        self._max_remediation_cycles = max_remediation_cycles

    async def run(self, approved: WorkRun, environment: ExecutionEnvironment) -> WorkRun:
        """Run or idempotently resume one pristine, human-approved work run."""

        self._validate_approved_input(approved)
        work_item = await self._work_management.get_work_item(approved.correlation.jira_issue_key)
        self._require_matching_approved_specification(approved, work_item)
        readiness = await self._work_management.evaluate_readiness(work_item)
        if not readiness.ready:
            return await self._block_on_unready_work_item(approved, readiness)
        current = self._ingest(approved)
        already_terminal = current.state in TERMINAL_STATES
        while current.state not in TERMINAL_STATES:
            if current.state is WorkRunState.APPROVED:
                current = self._transition(current, WorkRunState.EXECUTING)
            elif current.state is WorkRunState.EXECUTING:
                current = await self._execute(current, work_item, environment)
            elif current.state is WorkRunState.VERIFYING:
                current = await self._verify(current)
            elif current.state is WorkRunState.REMEDIATING:
                current = await self._remediate(current)
            elif current.state is WorkRunState.PUBLISHING_DRAFT_PR:
                current = await self._publish(current)
            else:  # pragma: no cover - WorkRunState is exhaustively handled above
                raise WorkflowIntegrityError(f"unsupported checkpoint state: {current.state.value}")
        if not already_terminal:
            await self._report_outcome(current)
        return current

    @staticmethod
    def _require_matching_approved_specification(run: WorkRun, work_item: WorkItem) -> None:
        """Fail closed if the Jira description changed after human approval."""
        correlation = run.correlation
        if (
            work_item.revision != correlation.jira_issue_revision
            or work_item.content_sha256 != correlation.jira_issue_content_sha256
        ):
            raise WorkflowIntegrityError(
                "the work item's current revision no longer matches the run's approved specification"
            )

    async def _block_on_unready_work_item(self, approved: WorkRun, readiness: ReadinessResult) -> WorkRun:
        checkpoint = self._checkpoints.load(approved.run_id)
        if checkpoint is not None:
            return checkpoint
        summary = "; ".join(problem.summary for problem in readiness.problems)
        blocked = approved.transition_to(
            WorkRunState.BLOCKED,
            actor_id=self._actor_id,
            occurred_at=self._next_time(approved),
            details=(("failure_code", "work_item_not_ready"), ("summary", summary)),
        )
        self._checkpoints.save(expected=None, updated=blocked)
        await self._report_outcome(blocked)
        return blocked

    def _ingest(self, approved: WorkRun) -> WorkRun:
        """Persist the approved input once, or resume its trusted checkpoint."""
        checkpoint = self._checkpoints.load(approved.run_id)
        if checkpoint is None:
            self._checkpoints.save(expected=None, updated=approved)
            return approved
        self._validate_checkpoint(checkpoint)
        if (
            checkpoint.run_id != approved.run_id
            or checkpoint.correlation != approved.correlation
            or checkpoint.events[:1] != approved.events
        ):
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
        artifact_events, draft_pr_events = WorkflowRunner._walk_checkpoint_history(run)
        WorkflowRunner._validate_checkpoint_artifacts(run, artifact_events)
        WorkflowRunner._validate_checkpoint_draft_pr(run, draft_pr_events)

    @staticmethod
    def _walk_checkpoint_history(run: WorkRun) -> tuple[list[WorkRunEvent], list[WorkRunEvent]]:
        previous_state = WorkRunState.APPROVED
        artifact_events: list[WorkRunEvent] = []
        draft_pr_events: list[WorkRunEvent] = []
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
            elif event.name != f"entered_{event.state.value}":
                raise WorkflowIntegrityError("checkpoint transition is missing its canonical audit event")
            previous_state = event.state
        return artifact_events, draft_pr_events

    @staticmethod
    def _validate_checkpoint_artifacts(run: WorkRun, artifact_events: list[WorkRunEvent]) -> None:
        recorded_artifacts = tuple(event.details for event in artifact_events)
        actual_artifacts = tuple(
            (
                ("artifact_id", artifact.artifact_id),
                ("kind", artifact.kind),
                ("uri", artifact.uri),
                *((("sha256", artifact.sha256),) if artifact.sha256 is not None else ()),
            )
            for artifact in run.artifacts
        )
        if recorded_artifacts != actual_artifacts:
            raise WorkflowIntegrityError("checkpoint evidence does not match its audit events")

    @staticmethod
    def _validate_checkpoint_draft_pr(run: WorkRun, draft_pr_events: list[WorkRunEvent]) -> None:
        if run.draft_pr_url is None and draft_pr_events:
            raise WorkflowIntegrityError("checkpoint lost its recorded Draft PR")
        if run.draft_pr_url is not None and (
            len(draft_pr_events) != 1 or dict(draft_pr_events[0].details).get("draft_pr_url") != run.draft_pr_url
        ):
            raise WorkflowIntegrityError("checkpoint Draft PR does not match its audit event")

    async def _execute(self, run: WorkRun, work_item: WorkItem, environment: ExecutionEnvironment) -> WorkRun:
        # EXECUTING is never re-entered once left (see ALLOWED_TRANSITIONS), so this always runs once.
        request = self._execution_request(run, work_item, environment, WorkflowStage.EXECUTION, 1)
        try:
            artifacts, outcome = await self._run_executor(request)
            updated = self._attach_all(run, artifacts)
        except Exception:
            return self._fail(run, WorkflowStage.EXECUTION)
        if outcome is not TerminalOutcome.COMPLETED:
            return self._fail_from(run, updated, WorkflowStage.EXECUTION, f"execution_{outcome.value}")
        return self._transition_from(run, updated, WorkRunState.VERIFYING)

    async def _run_executor(self, request: ExecutionRequest) -> tuple[tuple[WorkRunArtifact, ...], TerminalOutcome]:
        session = await self._executor.start(request)
        await self._drain(session.events())
        result: ExecutionResult = await session.result()
        artifacts = tuple(
            WorkRunArtifact(
                artifact_id=artifact.relative_path,
                kind="execution",
                uri=f"artifact://{request.execution_id}/{artifact.relative_path}",
                sha256=artifact.content_sha256,
            )
            for artifact in result.artifacts
        )
        return artifacts, result.outcome

    @staticmethod
    async def _drain(events: AsyncIterator[ExecutionEvent]) -> None:
        """Consume every streamed event so ``session.result()`` observes the terminal outcome."""
        async for _ in events:
            continue

    async def _verify(self, run: WorkRun) -> WorkRun:
        attempt = 1 + self._event_count(run, "entered_remediating")
        try:
            result = await self._verifier.verify(run)
            if not isinstance(result, VerificationResult):
                raise TypeError("verifier must return VerificationResult")
            updated = self._attach_all(run, result.artifacts)
            passed = result.passed
        except Exception:
            return self._fail(run, WorkflowStage.VERIFICATION)
        if passed:
            return self._transition_from(run, updated, WorkRunState.PUBLISHING_DRAFT_PR)
        if attempt > self._max_remediation_cycles:
            return self._fail_from(run, updated, WorkflowStage.VERIFICATION, "remediation_limit_reached")
        return self._transition_from(run, updated, WorkRunState.REMEDIATING)

    async def _remediate(self, run: WorkRun) -> WorkRun:
        try:
            artifacts = await self._remediator.remediate(run)
            updated = self._attach_all(run, artifacts)
        except Exception:
            return self._fail(run, WorkflowStage.REMEDIATION)
        return self._transition_from(run, updated, WorkRunState.VERIFYING)

    async def _publish(self, run: WorkRun) -> WorkRun:
        try:
            url = await self._publisher.publish(run)
            updated = run.record_draft_pr(url, actor_id=self._actor_id, occurred_at=self._next_time(run))
        except Exception:
            return self._fail(run, WorkflowStage.DRAFT_PR_PUBLICATION)
        return self._transition_from(run, updated, WorkRunState.COMPLETED)

    async def _report_outcome(self, run: WorkRun) -> None:
        idempotency_key = f"{run.run_id}:report:{run.state.value}"
        if run.state is WorkRunState.COMPLETED and run.draft_pr_url is not None:
            await self._work_management.complete_work(
                CompletionUpdate(
                    work_item_key=run.correlation.jira_issue_key,
                    idempotency_key=idempotency_key,
                    summary="GearMeshing-AI completed the governed work run.",
                    evidence_urls=(run.draft_pr_url,),
                )
            )
        elif run.state in {WorkRunState.FAILED, WorkRunState.BLOCKED}:
            details = dict(run.events[-1].details)
            await self._work_management.report_blocker(
                BlockerUpdate(
                    work_item_key=run.correlation.jira_issue_key,
                    idempotency_key=idempotency_key,
                    summary=details.get("failure_code", run.state.value),
                    details=details.get("summary", f"The work run entered {run.state.value}."),
                )
            )

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
        if occurred_at < run.events[-1].occurred_at:
            raise WorkflowIntegrityError("workflow timestamps must not regress")
        return occurred_at

    @staticmethod
    def _event_count(run: WorkRun, name: str) -> int:
        return sum(event.name == name for event in run.events)

    def _execution_request(
        self,
        run: WorkRun,
        work_item: WorkItem,
        environment: ExecutionEnvironment,
        stage: WorkflowStage,
        attempt: int,
    ) -> ExecutionRequest:
        content = canonical_work_item_content(work_item.title, work_item.description, work_item.acceptance_criteria)
        specification = ApprovedSpecification(
            issue_key=run.correlation.jira_issue_key,
            revision=run.correlation.jira_issue_revision,
            content=content,
            content_sha256=run.correlation.jira_issue_content_sha256,
            approved_by=run.events[0].actor_id,
        )
        return ExecutionRequest(
            execution_id=f"{run.run_id}:{stage.value}:{attempt}",
            specification=specification,
            repository=environment.repository,
            limits=environment.limits,
            tool_grants=environment.tool_grants,
        )
