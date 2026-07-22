"""Tests for deterministic governed workflow orchestration."""

from datetime import UTC, datetime, timedelta

from gearmeshing_ai.application.workflow_runner import (
    StageRequest,
    VerificationResult,
    WorkflowCheckpointStore,
    WorkflowRunner,
)
from gearmeshing_ai.domain.work_run import WorkRun, WorkRunArtifact, WorkRunCorrelation

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
    def __init__(self) -> None:
        self.current = START

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
