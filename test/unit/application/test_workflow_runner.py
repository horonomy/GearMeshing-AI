"""Tests for deterministic governed workflow orchestration."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gearmeshing_ai.application.workflow_runner import (
    StageRequest,
    VerificationResult,
    WorkflowCheckpointStore,
    WorkflowIntegrityError,
    WorkflowRunner,
)
from gearmeshing_ai.domain.work_run import WorkRun, WorkRunArtifact, WorkRunCorrelation, WorkRunEvent, WorkRunState

START = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


class MemoryCheckpoints:
    def __init__(self, current: WorkRun | None = None) -> None:
        self.current = current
        self.saved: list[WorkRun] = []

    def load(self, run_id: str) -> WorkRun | None:
        if self.current is not None and self.current.run_id != run_id:
            return None
        return self.current

    def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None:
        if self.current != expected:
            raise RuntimeError("concurrent checkpoint update")
        self.current = updated
        self.saved.append(updated)


class TickingClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


class FakeExecutor:
    def __init__(self, artifacts: tuple[WorkRunArtifact, ...] = ()) -> None:
        self.artifacts = artifacts
        self.requests: list[StageRequest] = []
        self.error: Exception | None = None

    def execute(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.artifacts


class FakeVerifier:
    def __init__(self, results: list[VerificationResult] | None = None) -> None:
        self.results = results or [VerificationResult(passed=True)]
        self.requests: list[StageRequest] = []

    def verify(self, request: StageRequest) -> VerificationResult:
        self.requests.append(request)
        return self.results[len(self.requests) - 1]


class FakeRemediator:
    def __init__(self, artifacts: tuple[WorkRunArtifact, ...] = ()) -> None:
        self.artifacts = artifacts
        self.requests: list[StageRequest] = []

    def remediate(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]:
        self.requests.append(request)
        return self.artifacts


class FakePublisher:
    def __init__(self) -> None:
        self.requests: list[StageRequest] = []

    def publish(self, request: StageRequest) -> str:
        self.requests.append(request)
        return "https://github.com/horonomy/GearMeshing-AI/pull/3"


def _approved() -> WorkRun:
    return WorkRun.approve(
        run_id="work-run-13",
        correlation=WorkRunCorrelation(
            jira_issue_key="GMAI-13",
            jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-13",
            repository_url="https://github.com/horonomy/GearMeshing-AI",
            branch_name="mvp1/GMAI-13/workflow_runner",
            agent_assembly_run_id="assembly-run-13",
        ),
        actor_id="human-product-owner",
        occurred_at=START,
    )


def _artifact(artifact_id: str) -> WorkRunArtifact:
    return WorkRunArtifact(
        artifact_id=artifact_id,
        kind="verification",
        uri=f"artifact://work-run-13/{artifact_id}",
        sha256="a" * 64,
    )


def _runner(
    checkpoints: WorkflowCheckpointStore,
    *,
    executor: FakeExecutor | None = None,
    verifier: FakeVerifier | None = None,
    remediator: FakeRemediator | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[WorkflowRunner, FakeExecutor, FakeVerifier, FakeRemediator, FakePublisher]:
    execution = executor or FakeExecutor()
    verification = verifier or FakeVerifier()
    remediation = remediator or FakeRemediator()
    publication = publisher or FakePublisher()
    return (
        WorkflowRunner(
            checkpoints=checkpoints,
            executor=execution,
            verifier=verification,
            remediator=remediation,
            publisher=publication,
            clock=TickingClock(),
        ),
        execution,
        verification,
        remediation,
        publication,
    )


def test_checkpoint_must_belong_to_the_requested_run() -> None:
    approved = _approved()

    class MisroutedCheckpoints:
        def load(self, run_id: str) -> WorkRun | None:
            return replace(approved, run_id="different-run")

        def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None:
            raise AssertionError("a mismatched checkpoint must not be saved")

    runner, *_ = _runner(MisroutedCheckpoints())

    with pytest.raises(WorkflowIntegrityError, match="does not extend"):
        runner.run(approved)


def test_runner_completes_deterministic_governed_stages() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, executor, verifier, remediator, publisher = _runner(
        checkpoints,
        executor=FakeExecutor((_artifact("implementation"),)),
    )

    completed = runner.run(approved)

    assert completed.state.value == "completed"
    assert completed.draft_pr_url == "https://github.com/horonomy/GearMeshing-AI/pull/3"
    assert completed.correlation is approved.correlation
    assert executor.requests[0].idempotency_key == "work-run-13:execution:1"
    assert verifier.requests[0].artifacts == (_artifact("implementation"),)
    assert verifier.requests[0].idempotency_key == "work-run-13:verification:1"
    assert remediator.requests == []
    assert publisher.requests[0].idempotency_key == "work-run-13:draft_pr_publication:1"
    assert checkpoints.current == completed


def test_replaying_an_approved_run_does_not_repeat_side_effects() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, executor, verifier, _, publisher = _runner(checkpoints)

    first_result = runner.run(approved)
    save_count = len(checkpoints.saved)
    second_result = runner.run(approved)

    assert second_result == first_result
    assert len(executor.requests) == 1
    assert len(verifier.requests) == 1
    assert len(publisher.requests) == 1
    assert len(checkpoints.saved) == save_count


def test_remediation_appends_evidence_without_rewriting_history() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, _, verifier, remediator, _ = _runner(
        checkpoints,
        executor=FakeExecutor((_artifact("implementation"),)),
        verifier=FakeVerifier(
            [
                VerificationResult(passed=False, artifacts=(_artifact("failed-check"),)),
                VerificationResult(passed=True, artifacts=(_artifact("passed-check"),)),
            ]
        ),
        remediator=FakeRemediator((_artifact("remediation"),)),
    )

    completed = runner.run(approved)

    assert approved.artifacts == ()
    assert tuple(artifact.artifact_id for artifact in completed.artifacts) == (
        "implementation",
        "failed-check",
        "remediation",
        "passed-check",
    )
    assert [request.idempotency_key for request in verifier.requests] == [
        "work-run-13:verification:1",
        "work-run-13:verification:2",
    ]
    assert remediator.requests[0].idempotency_key == "work-run-13:remediation:1"


def test_operation_failures_are_recorded_without_exception_secrets() -> None:
    executor = FakeExecutor()
    executor.error = RuntimeError("Authorization: Bearer should-never-be-recorded")
    checkpoints = MemoryCheckpoints()
    runner, *_ = _runner(checkpoints, executor=executor)

    failed = runner.run(_approved())

    assert failed.state.value == "failed"
    assert failed.events[-1].details == (
        ("failure_code", "operation_failed"),
        ("stage", "execution"),
    )
    assert "Bearer" not in repr(failed)
    assert "should-never-be-recorded" not in repr(checkpoints.saved)


def test_checkpoint_rejects_events_outside_the_governed_history() -> None:
    approved = _approved()
    tampered_event = WorkRunEvent(
        sequence=2,
        name="unreviewed_mutation",
        state=WorkRunState.APPROVED,
        actor_id="unknown-actor",
        occurred_at=START + timedelta(seconds=1),
    )
    tampered = replace(approved, events=(*approved.events, tampered_event))
    runner, *_ = _runner(MemoryCheckpoints(tampered))

    with pytest.raises(WorkflowIntegrityError, match="invalid same-state event"):
        runner.run(approved)


def test_draft_pr_retry_reuses_its_key_and_records_one_url() -> None:
    approved = _approved()
    publishing = (
        approved.transition_to(
            WorkRunState.EXECUTING,
            actor_id="agent-assembly",
            occurred_at=START + timedelta(seconds=1),
        )
        .transition_to(
            WorkRunState.VERIFYING,
            actor_id="agent-assembly",
            occurred_at=START + timedelta(seconds=2),
        )
        .transition_to(
            WorkRunState.PUBLISHING_DRAFT_PR,
            actor_id="agent-assembly",
            occurred_at=START + timedelta(seconds=3),
        )
    )

    class FailFirstCompletionSave(MemoryCheckpoints):
        fail_next_completion = True

        def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None:
            if updated.state is WorkRunState.COMPLETED and self.fail_next_completion:
                self.fail_next_completion = False
                raise RuntimeError("simulated checkpoint outage")
            super().save(expected=expected, updated=updated)

    checkpoints = FailFirstCompletionSave(publishing)
    publisher = FakePublisher()
    runner = WorkflowRunner(
        checkpoints=checkpoints,
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        remediator=FakeRemediator(),
        publisher=publisher,
        clock=TickingClock(START + timedelta(seconds=3)),
    )

    with pytest.raises(RuntimeError, match="checkpoint outage"):
        runner.run(approved)
    completed = runner.run(approved)

    assert [request.idempotency_key for request in publisher.requests] == [
        "work-run-13:draft_pr_publication:1",
        "work-run-13:draft_pr_publication:1",
    ]
    assert sum(event.name == "draft_pr_recorded" for event in completed.events) == 1
    assert completed.draft_pr_url == "https://github.com/horonomy/GearMeshing-AI/pull/3"


def test_checkpoint_cannot_change_external_correlation() -> None:
    approved = _approved()
    changed_correlation = replace(
        approved.correlation,
        branch_name="mvp1/GMAI-13/unapproved_branch",
        agent_assembly_run_id="different-assembly-run",
    )
    checkpoint = replace(approved, correlation=changed_correlation)
    runner, *_ = _runner(MemoryCheckpoints(checkpoint))

    with pytest.raises(WorkflowIntegrityError, match="does not extend"):
        runner.run(approved)
