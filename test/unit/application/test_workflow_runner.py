"""Tests for deterministic governed workflow orchestration."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gearmeshing_ai.application.ports.coding_executor import (
    EventKind,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    FailureCategory,
    FailureMetadata,
    RepositoryContext,
    ResourceLimits,
    TerminalOutcome,
)
from gearmeshing_ai.application.ports.tool_policy import PolicyDecision, ToolPolicyGate
from gearmeshing_ai.application.ports.work_management import (
    ArtifactUpdate,
    BlockerUpdate,
    CompletionUpdate,
    OperationReceipt,
    ProgressUpdate,
    ProviderCapabilities,
    ReadinessProblem,
    ReadinessResult,
    WorkItem,
    WorkManagementCapability,
    WorkManagementProvider,
    canonical_work_item_content,
)
from gearmeshing_ai.application.workflow_runner import (
    ExecutionEnvironment,
    VerificationResult,
    WorkflowIntegrityError,
    WorkflowRunner,
)
from gearmeshing_ai.domain.work_run import (
    WorkRun,
    WorkRunArtifact,
    WorkRunCorrelation,
    WorkRunState,
)
from gearmeshing_ai.domain.work_run import WorkRunEvent as DomainAuditEvent

START = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
_TITLE = "Orchestrate governed work runs"
_DESCRIPTION = "Approved specification"
_CRITERIA = ("The runner completes a mocked golden path end to end.",)
_CONTENT = canonical_work_item_content(_TITLE, _DESCRIPTION, _CRITERIA)
_CONTENT_SHA256 = hashlib.sha256(_CONTENT.encode()).hexdigest()


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


class FakeWorkManagement(WorkManagementProvider):
    def __init__(self, work_item: WorkItem, *, problems: tuple[ReadinessProblem, ...] = ()) -> None:
        self.work_item = work_item
        self.problems = problems
        self.completions: list[CompletionUpdate] = []
        self.blockers: list[BlockerUpdate] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(set(WorkManagementCapability))

    async def _get_work_item(self, work_item_key: str) -> WorkItem:
        assert work_item_key == self.work_item.key
        return self.work_item

    async def _evaluate_readiness(self, work_item: WorkItem) -> ReadinessResult:
        return ReadinessResult(work_item_key=work_item.key, problems=self.problems)

    async def _update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        return self._receipt(update.idempotency_key)

    async def _report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        self.blockers.append(update)
        return self._receipt(update.idempotency_key)

    async def _complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        self.completions.append(update)
        return self._receipt(update.idempotency_key)

    async def _attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        return self._receipt(update.idempotency_key)

    def _receipt(self, idempotency_key: str) -> OperationReceipt:
        return OperationReceipt(
            provider=self.name,
            work_item_key=self.work_item.key,
            idempotency_key=idempotency_key,
            provider_reference="operation-1",
            accepted_at=datetime(2026, 7, 22, tzinfo=UTC),
        )


class FakeVerifier:
    def __init__(self, results: list[VerificationResult] | None = None) -> None:
        self.results = results or [VerificationResult(passed=True)]
        self.calls: list[WorkRun] = []

    async def verify(self, run: WorkRun) -> VerificationResult:
        self.calls.append(run)
        return self.results[len(self.calls) - 1]


class FakeRemediator:
    def __init__(self, artifacts: tuple[WorkRunArtifact, ...] = ()) -> None:
        self.artifacts = artifacts
        self.calls: list[WorkRun] = []

    async def remediate(self, run: WorkRun) -> tuple[WorkRunArtifact, ...]:
        self.calls.append(run)
        return self.artifacts


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[WorkRun] = []

    async def publish(self, run: WorkRun) -> str:
        self.calls.append(run)
        return "https://github.com/horonomy/GearMeshing-AI/pull/3"


class FakeDenyingPolicyGate:
    """Deterministic ``ToolPolicyGate`` that always denies with a fixed reason."""

    def __init__(self, reason: str = "egress not allow-listed") -> None:
        self.reason = reason
        self.calls: list[dict[str, str]] = []

    async def check(self, *, agent_id: str, action_type: str, tool_name: str) -> PolicyDecision:
        self.calls.append({"agent_id": agent_id, "action_type": action_type, "tool_name": tool_name})
        return PolicyDecision(allowed=False, reason=self.reason, details={"decision": "deny"})


class FakeExecutionSession:
    """Minimal single-consumer session with a scripted terminal result."""

    def __init__(
        self,
        request: ExecutionRequest,
        *,
        outcome: TerminalOutcome,
        artifacts: tuple[ExecutionArtifact, ...],
        failure: FailureMetadata | None,
    ) -> None:
        self._request = request
        self._outcome = outcome
        self._artifacts = artifacts
        self._failure = failure
        self._result: ExecutionResult | None = None

    @property
    def execution_id(self) -> str:
        return self._request.execution_id

    async def events(self) -> AsyncIterator[ExecutionEvent]:
        sequence = 1
        yield ExecutionEvent(sequence, EventKind.STARTED, "Execution started")
        for artifact in self._artifacts:
            sequence += 1
            yield ExecutionEvent(sequence, EventKind.ARTIFACT, "Artifact produced", artifact=artifact)
        sequence += 1
        self._result = ExecutionResult(
            execution_id=self.execution_id,
            outcome=self._outcome,
            limits=self._request.limits,
            events_emitted=sequence,
            artifacts=self._artifacts,
            failure=self._failure,
        )
        yield ExecutionEvent(sequence, EventKind.TERMINAL, "Execution reached a terminal outcome")

    async def result(self) -> ExecutionResult:
        if self._result is None:
            async for _ in self.events():
                pass
        assert self._result is not None
        return self._result

    async def cancel(self, reason: str) -> None:
        raise RuntimeError("cancellation is not supported")


class FakeCodingExecutor:
    """Deterministic fake sufficient for workflow-runner orchestration tests."""

    def __init__(
        self,
        *,
        capabilities: ExecutorCapabilities,
        artifacts: tuple[ExecutionArtifact, ...] = (),
        outcome: TerminalOutcome = TerminalOutcome.COMPLETED,
        failure: FailureMetadata | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._artifacts = artifacts
        self._outcome = outcome
        self._failure = failure
        self.requests: list[ExecutionRequest] = []

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return self._capabilities

    async def start(self, request: ExecutionRequest) -> FakeExecutionSession:
        self.requests.append(request)
        return FakeExecutionSession(
            request,
            outcome=self._outcome,
            artifacts=self._artifacts,
            failure=self._failure,
        )


def _work_item(*, revision: str = "1", ready: bool = True) -> WorkItem:
    return WorkItem(
        key="GMAI-13",
        title=_TITLE,
        description=_DESCRIPTION,
        acceptance_criteria=_CRITERIA if ready else (),
        status="In Progress",
        web_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-13",
        repository=None,
        revision=revision,
        content_sha256=_CONTENT_SHA256,
    )


def _correlation(*, revision: str = "1") -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key="GMAI-13",
        jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-13",
        jira_issue_revision=revision,
        jira_issue_content_sha256=_CONTENT_SHA256,
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-13/workflow_runner",
        agent_assembly_run_id="assembly-run-13",
    )


def _approved(*, revision: str = "1") -> WorkRun:
    return WorkRun.approve(
        run_id="work-run-13",
        correlation=_correlation(revision=revision),
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


def _execution_artifact(relative_path: str = "report.txt") -> ExecutionArtifact:
    return ExecutionArtifact(
        relative_path=relative_path,
        media_type="text/plain",
        content_sha256="a" * 64,
        size_bytes=3,
    )


def _environment() -> ExecutionEnvironment:
    return ExecutionEnvironment(
        repository=RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root="/workspace/.worktrees/work-run-13",
            base_ref="main",
            branch="mvp1/GMAI-13/workflow_runner",
        ),
        limits=ResourceLimits(
            wall_clock_seconds=60,
            max_events=32,
            max_artifacts=8,
            max_artifact_bytes=1_000_000,
        ),
    )


def _executor(
    artifacts: tuple[ExecutionArtifact, ...] = (),
    *,
    outcome: TerminalOutcome = TerminalOutcome.COMPLETED,
    failure: FailureMetadata | None = None,
) -> FakeCodingExecutor:
    return FakeCodingExecutor(
        capabilities=ExecutorCapabilities(streaming=True, cancellation=True),
        artifacts=artifacts,
        outcome=outcome,
        failure=failure,
    )


def _runner(
    checkpoints: MemoryCheckpoints,
    *,
    work_management: FakeWorkManagement | None = None,
    executor: FakeCodingExecutor | None = None,
    verifier: FakeVerifier | None = None,
    remediator: FakeRemediator | None = None,
    publisher: FakePublisher | None = None,
    policy_gate: ToolPolicyGate | None = None,
) -> tuple[WorkflowRunner, FakeWorkManagement, FakeCodingExecutor, FakeVerifier, FakeRemediator, FakePublisher]:
    work_mgmt = work_management or FakeWorkManagement(_work_item())
    execution = executor or _executor()
    verification = verifier or FakeVerifier()
    remediation = remediator or FakeRemediator()
    publication = publisher or FakePublisher()
    runner = WorkflowRunner(
        checkpoints=checkpoints,
        work_management=work_mgmt,
        executor=execution,
        verifier=verification,
        remediator=remediation,
        publisher=publication,
        clock=TickingClock(),
        policy_gate=policy_gate,
    )
    return runner, work_mgmt, execution, verification, remediation, publication


async def test_checkpoint_must_belong_to_the_requested_run() -> None:
    approved = _approved()

    class MisroutedCheckpoints:
        def load(self, run_id: str) -> WorkRun | None:
            return replace(approved, run_id="different-run")

        def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None:
            raise AssertionError("a mismatched checkpoint must not be saved")

    runner, *_ = _runner(MisroutedCheckpoints())  # type: ignore[arg-type]

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="does not extend"):
        await runner.run(approved, environment)


async def test_ingest_persists_the_approved_input_as_the_first_checkpoint() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, *_ = _runner(checkpoints)

    await runner.run(approved, _environment())

    assert checkpoints.saved[0] is approved
    assert checkpoints.saved[0].state is WorkRunState.APPROVED


async def test_runner_completes_deterministic_governed_stages() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, work_management, _, verifier, remediator, publisher = _runner(
        checkpoints,
        executor=_executor((_execution_artifact(),)),
    )

    completed = await runner.run(approved, _environment())

    assert completed.state is WorkRunState.COMPLETED
    assert completed.draft_pr_url == "https://github.com/horonomy/GearMeshing-AI/pull/3"
    assert completed.correlation is approved.correlation
    assert tuple(a.artifact_id for a in completed.artifacts) == ("report.txt",)
    assert verifier.calls[0].artifacts == completed.artifacts[:1]
    assert remediator.calls == []
    assert len(publisher.calls) == 1
    assert checkpoints.current == completed
    assert len(work_management.completions) == 1
    assert work_management.completions[0].evidence_urls == (completed.draft_pr_url,)


def test_verification_results_require_a_strict_boolean_decision() -> None:
    with pytest.raises(TypeError, match="passed must be a boolean"):
        VerificationResult(passed="false")  # type: ignore[arg-type]


async def test_malformed_verifier_responses_become_terminal_failures() -> None:
    class MalformedVerifier:
        async def verify(self, run: WorkRun) -> object:
            return object()

    checkpoints = MemoryCheckpoints()
    runner, *_ = _runner(checkpoints, verifier=MalformedVerifier())  # type: ignore[arg-type]

    failed = await runner.run(_approved(), _environment())

    assert failed.state is WorkRunState.FAILED
    assert failed.events[-1].details == (
        ("failure_code", "operation_failed"),
        ("stage", "verification"),
    )


async def test_replaying_an_approved_run_does_not_repeat_side_effects() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, work_management, _, verifier, _, publisher = _runner(checkpoints)

    first_result = await runner.run(approved, _environment())
    save_count = len(checkpoints.saved)
    second_result = await runner.run(approved, _environment())

    assert second_result == first_result
    assert len(verifier.calls) == 1
    assert len(publisher.calls) == 1
    assert len(checkpoints.saved) == save_count
    assert len(work_management.completions) == 1


async def test_remediation_appends_evidence_without_rewriting_history() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner, *_, verifier, remediator, _ = _runner(
        checkpoints,
        executor=_executor((_execution_artifact(),)),
        verifier=FakeVerifier(
            [
                VerificationResult(passed=False, artifacts=(_artifact("failed-check"),)),
                VerificationResult(passed=True, artifacts=(_artifact("passed-check"),)),
            ]
        ),
        remediator=FakeRemediator((_artifact("remediation"),)),
    )

    completed = await runner.run(approved, _environment())

    assert approved.artifacts == ()
    assert tuple(a.artifact_id for a in completed.artifacts) == (
        "report.txt",
        "failed-check",
        "remediation",
        "passed-check",
    )
    assert len(verifier.calls) == 2
    assert len(remediator.calls) == 1


async def test_operation_failures_are_recorded_without_exception_secrets() -> None:
    checkpoints = MemoryCheckpoints()
    executor = _executor(
        outcome=TerminalOutcome.FAILED,
        failure=FailureMetadata(
            FailureCategory.PROVIDER,
            "auth_error",
            "Authorization: Bearer should-never-be-recorded",
        ),
    )
    runner, *_ = _runner(checkpoints, executor=executor)

    failed = await runner.run(_approved(), _environment())

    assert failed.state is WorkRunState.FAILED
    assert failed.events[-1].details == (
        ("failure_code", "execution_failed"),
        ("stage", "execution"),
    )
    assert "Bearer" not in repr(failed)
    assert "should-never-be-recorded" not in repr(checkpoints.saved)


async def test_invalid_stage_results_are_recorded_as_failures() -> None:
    duplicate = _execution_artifact()
    checkpoints = MemoryCheckpoints()
    runner, *_ = _runner(checkpoints, executor=_executor((duplicate, duplicate)))

    failed = await runner.run(_approved(), _environment())

    assert failed.state is WorkRunState.FAILED
    assert failed.artifacts == ()
    assert failed.events[-1].details == (
        ("failure_code", "operation_failed"),
        ("stage", "execution"),
    )


async def test_checkpoint_rejects_events_outside_the_governed_history() -> None:
    approved = _approved()
    tampered_event = DomainAuditEvent(
        sequence=2,
        name="unreviewed_mutation",
        state=WorkRunState.APPROVED,
        actor_id="unknown-actor",
        occurred_at=START + timedelta(seconds=1),
    )
    tampered = replace(approved, events=(*approved.events, tampered_event))
    runner, *_ = _runner(MemoryCheckpoints(tampered))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="invalid same-state event"):
        await runner.run(approved, environment)


async def test_draft_pr_retry_reuses_the_run_and_records_one_url() -> None:
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
        work_management=FakeWorkManagement(_work_item()),
        executor=_executor(),
        verifier=FakeVerifier(),
        remediator=FakeRemediator(),
        publisher=publisher,
        clock=TickingClock(START + timedelta(seconds=3)),
    )

    environment = _environment()

    with pytest.raises(RuntimeError, match="checkpoint outage"):
        await runner.run(approved, environment)
    completed = await runner.run(approved, _environment())

    assert len(publisher.calls) == 2
    assert sum(event.name == "draft_pr_recorded" for event in completed.events) == 1
    assert completed.draft_pr_url == "https://github.com/horonomy/GearMeshing-AI/pull/3"


async def test_checkpoint_cannot_change_external_correlation() -> None:
    approved = _approved()
    changed_correlation = replace(
        approved.correlation,
        branch_name="mvp1/GMAI-13/unapproved_branch",
        agent_assembly_run_id="different-assembly-run",
    )
    checkpoint = replace(approved, correlation=changed_correlation)
    runner, *_ = _runner(MemoryCheckpoints(checkpoint))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="does not extend"):
        await runner.run(approved, environment)


async def test_checkpoint_transitions_require_canonical_audit_events() -> None:
    approved = _approved()
    executing = approved.transition_to(
        WorkRunState.EXECUTING,
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=1),
    )
    renamed_event = replace(executing.events[-1], name="unreviewed_execution")
    tampered = replace(executing, events=(executing.events[0], renamed_event))
    runner, *_ = _runner(MemoryCheckpoints(tampered))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="canonical audit event"):
        await runner.run(approved, environment)


async def test_checkpoint_evidence_must_match_its_audit_prefix() -> None:
    approved = _approved()
    executing = approved.transition_to(
        WorkRunState.EXECUTING,
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=1),
    )
    evidenced = executing.attach_artifact(
        _artifact("implementation"),
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=2),
    )
    changed_artifact = replace(evidenced.artifacts[0], kind="unreviewed")
    tampered = replace(evidenced, artifacts=(changed_artifact,))
    runner, *_ = _runner(MemoryCheckpoints(tampered))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="evidence does not match"):
        await runner.run(approved, environment)


@pytest.mark.parametrize(
    "replacement",
    [
        {"uri": "https://attacker.invalid/replaced"},
        {"sha256": "b" * 64},
    ],
)
async def test_checkpoint_rejects_replaced_artifact_integrity(replacement: dict[str, str]) -> None:
    approved = _approved()
    executing = approved.transition_to(
        WorkRunState.EXECUTING,
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=1),
    )
    evidenced = executing.attach_artifact(
        _artifact("implementation"),
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=2),
    )
    tampered = replace(evidenced, artifacts=(replace(evidenced.artifacts[0], **replacement),))
    runner, *_ = _runner(MemoryCheckpoints(tampered))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="evidence does not match"):
        await runner.run(approved, environment)


async def test_remediation_cycles_stop_at_the_configured_limit() -> None:
    checkpoints = MemoryCheckpoints()
    verifier = FakeVerifier([VerificationResult(passed=False) for _ in range(4)])
    remediator = FakeRemediator()
    runner = WorkflowRunner(
        checkpoints=checkpoints,
        work_management=FakeWorkManagement(_work_item()),
        executor=_executor(),
        verifier=verifier,
        remediator=remediator,
        publisher=FakePublisher(),
        clock=TickingClock(),
        max_remediation_cycles=3,
    )

    failed = await runner.run(_approved(), _environment())

    assert failed.state is WorkRunState.FAILED
    assert len(verifier.calls) == 4
    assert len(remediator.calls) == 3
    assert failed.events[-1].details == (
        ("failure_code", "remediation_limit_reached"),
        ("stage", "verification"),
    )


async def test_runner_rejects_regressing_audit_timestamps() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner = WorkflowRunner(
        checkpoints=checkpoints,
        work_management=FakeWorkManagement(_work_item()),
        executor=_executor(),
        verifier=FakeVerifier(),
        remediator=FakeRemediator(),
        publisher=FakePublisher(),
        clock=lambda: START - timedelta(seconds=1),
    )

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="not regress"):
        await runner.run(approved, environment)

    assert checkpoints.saved == [approved]


async def test_runner_accepts_a_repeated_clock_reading() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    runner = WorkflowRunner(
        checkpoints=checkpoints,
        work_management=FakeWorkManagement(_work_item()),
        executor=_executor(),
        verifier=FakeVerifier(),
        remediator=FakeRemediator(),
        publisher=FakePublisher(),
        clock=lambda: START,
    )

    completed = await runner.run(approved, _environment())

    assert completed.state is WorkRunState.COMPLETED


async def test_runner_rejects_a_work_item_edited_after_approval() -> None:
    approved = _approved(revision="1")
    checkpoints = MemoryCheckpoints()
    runner, *_ = _runner(checkpoints, work_management=FakeWorkManagement(_work_item(revision="2")))

    environment = _environment()

    with pytest.raises(WorkflowIntegrityError, match="no longer matches"):
        await runner.run(approved, environment)


async def test_runner_blocks_when_the_work_item_is_not_ready() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    problems = (ReadinessProblem(code="missing-acceptance-criteria", summary="Add criteria", details="details"),)
    work_management = FakeWorkManagement(_work_item(ready=False), problems=problems)
    runner, *_ = _runner(checkpoints, work_management=work_management)

    blocked = await runner.run(approved, _environment())

    assert blocked.state is WorkRunState.BLOCKED
    assert dict(blocked.events[-1].details)["failure_code"] == "work_item_not_ready"
    assert len(work_management.blockers) == 1


async def test_runner_does_not_reblock_an_already_blocked_run() -> None:
    approved = _approved()
    blocked = approved.transition_to(
        WorkRunState.BLOCKED,
        actor_id="agent-assembly",
        occurred_at=START + timedelta(seconds=1),
        details=(("failure_code", "work_item_not_ready"), ("summary", "Add criteria")),
    )
    checkpoints = MemoryCheckpoints(blocked)
    problems = (ReadinessProblem(code="missing-acceptance-criteria", summary="Add criteria", details="details"),)
    work_management = FakeWorkManagement(_work_item(ready=False), problems=problems)
    runner, *_ = _runner(checkpoints, work_management=work_management)

    result = await runner.run(approved, _environment())

    assert result == blocked
    assert checkpoints.saved == []


async def test_runner_blocks_when_the_policy_gate_denies_execution() -> None:
    approved = _approved()
    checkpoints = MemoryCheckpoints()
    policy_gate = FakeDenyingPolicyGate(reason="egress not allow-listed")
    runner, work_management, executor, *_ = _runner(checkpoints, policy_gate=policy_gate)

    blocked = await runner.run(approved, _environment())

    assert blocked.state is WorkRunState.BLOCKED
    assert dict(blocked.events[-1].details) == {
        "failure_code": "policy_denied",
        "summary": "egress not allow-listed",
    }
    assert checkpoints.current == blocked
    assert executor.requests == []
    assert len(work_management.blockers) == 1
    assert work_management.blockers[0].summary == "policy_denied"
    assert work_management.blockers[0].details == "egress not allow-listed"
    assert policy_gate.calls == [
        {"agent_id": "agent-assembly", "action_type": "tool_call", "tool_name": "coding_executor"}
    ]
