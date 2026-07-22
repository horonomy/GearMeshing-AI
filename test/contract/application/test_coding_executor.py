"""Contract tests for provider-neutral coding execution."""

from __future__ import annotations

from hashlib import sha256
from typing import cast

import pytest
from fake_coding_executor import FakeCodingExecutor, assert_executor_contract

from gearmeshing_ai.application.ports.coding_executor import (
    ApprovedSpecification,
    CodingExecutor,
    EventKind,
    ExecutionArtifact,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    FailureCategory,
    FailureMetadata,
    RepositoryContext,
    ResourceLimits,
    TerminalOutcome,
    ToolGrant,
)

SPECIFICATION_CONTENT = "Implement the approved behavior and its verification."
SPECIFICATION_DIGEST = sha256(SPECIFICATION_CONTENT.encode()).hexdigest()
ARTIFACT_DIGEST = sha256(b"patch").hexdigest()


def make_request(
    *,
    execution_id: str = "execution-1",
    metadata: dict[str, str | int | float | None] | None = None,
    max_events: int = 10,
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        specification=ApprovedSpecification(
            issue_key="GMAI-20",
            revision="revision-1",
            content=SPECIFICATION_CONTENT,
            content_sha256=SPECIFICATION_DIGEST,
            approved_by="account-1",
        ),
        repository=RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root="/workspace/.worktrees/GMAI-20",
            base_ref="main",
            branch="mvp1/GMAI-20/coding_executor_contract",
            writable_paths=("gearmeshing_ai/application/ports/coding_executor.py",),
        ),
        limits=ResourceLimits(
            wall_clock_seconds=60.0,
            max_events=max_events,
            max_artifacts=2,
            max_artifact_bytes=1024,
        ),
        tool_grants=(ToolGrant("shell", frozenset({"execute"}), ("pytest", "ruff")),),
        metadata={} if metadata is None else metadata,
    )


def make_capabilities(*, cancellation: bool = True) -> ExecutorCapabilities:
    return ExecutorCapabilities(streaming=True, cancellation=cancellation, tool_names=frozenset({"shell"}))


def test_fake_satisfies_provider_neutral_protocol() -> None:
    executor: CodingExecutor = assert_executor_contract(FakeCodingExecutor(capabilities=make_capabilities()))

    assert executor.capabilities.streaming is True
    assert executor.capabilities.cancellation is True


def test_repository_context_accepts_an_isolated_sibling_worktree() -> None:
    repository = make_request().repository

    assert repository.repository_root == "/workspace/GearMeshing-AI"
    assert repository.worktree_root == "/workspace/.worktrees/GMAI-20"


@pytest.mark.parametrize("unsafe_path", (".", "/etc/passwd", "../secret", "src/../../secret"))
def test_repository_context_rejects_unsafe_writable_paths(unsafe_path: str) -> None:
    with pytest.raises(ValueError, match="relative POSIX path"):
        RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root="/workspace/.worktrees/GMAI-20",
            base_ref="main",
            branch="mvp1/GMAI-20/coding_executor_contract",
            writable_paths=(unsafe_path,),
        )


@pytest.mark.parametrize("unsafe_root", ("/workspace/../secret", "/workspace//repository", "/"))
def test_repository_context_rejects_ambiguous_absolute_roots(unsafe_root: str) -> None:
    with pytest.raises(ValueError, match="normalized absolute POSIX path"):
        RepositoryContext(
            repository_root=unsafe_root,
            worktree_root="/workspace/.worktrees/GMAI-20",
            base_ref="main",
            branch="mvp1/GMAI-20/coding_executor_contract",
        )


@pytest.mark.parametrize("unsafe_ref", ("feature//name", "feature/../main", "feature@{one", "-danger"))
def test_repository_context_rejects_unsafe_git_refs(unsafe_ref: str) -> None:
    with pytest.raises(ValueError, match="normalized Git ref"):
        RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root="/workspace/.worktrees/GMAI-20",
            base_ref="main",
            branch=unsafe_ref,
        )


@pytest.mark.parametrize(
    ("base_ref", "branch"),
    (("main", "main"), ("refs/heads/main", "main"), ("develop", "master"), ("develop", "refs/heads/main")),
)
def test_repository_context_rejects_base_and_protected_branches(base_ref: str, branch: str) -> None:
    with pytest.raises(ValueError, match="must not be protected"):
        RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root="/workspace/.worktrees/GMAI-20",
            base_ref=base_ref,
            branch=branch,
        )


@pytest.mark.parametrize("unsafe_command", ("/bin/sh", "pytest;curl", "../ruff", "git status"))
def test_tool_grant_rejects_unsafe_commands(unsafe_command: str) -> None:
    with pytest.raises(ValueError, match="without paths or shell syntax"):
        ToolGrant("shell", frozenset({"execute"}), (unsafe_command,))


@pytest.mark.parametrize("unsafe_duration", (True, float("nan"), float("inf"), float("-inf")))
def test_resource_limits_reject_non_finite_or_boolean_durations(unsafe_duration: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ResourceLimits(unsafe_duration, max_events=1, max_artifacts=1, max_artifact_bytes=1)


@pytest.mark.parametrize("unsafe_count", (True, 1.5, float("nan"), float("inf")))
def test_resource_limits_reject_non_integral_counts(unsafe_count: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ResourceLimits(1.0, max_events=cast("int", unsafe_count), max_artifacts=1, max_artifact_bytes=1)


def test_resource_limits_reserve_started_and_terminal_events() -> None:
    with pytest.raises(ValueError, match="reserve STARTED and TERMINAL"):
        ResourceLimits(1.0, max_events=1, max_artifacts=1, max_artifact_bytes=1)


def test_request_defensively_snapshots_metadata() -> None:
    metadata: dict[str, str | int | float | None] = {"attempt": 1}
    request = make_request(metadata=metadata)

    metadata["attempt"] = 2

    assert request.metadata == {"attempt": 1}
    with pytest.raises(TypeError):
        request.metadata["attempt"] = 3


@pytest.mark.parametrize(
    "unsafe_metadata",
    ({"api_token": "redacted"}, {"password_hint": "redacted"}, {"valid": float("nan")}, {"valid": True}),
)
def test_request_rejects_credentials_and_unsafe_metadata_values(
    unsafe_metadata: dict[str, str | int | float | None],
) -> None:
    with pytest.raises(ValueError):
        make_request(metadata=unsafe_metadata)


def test_approved_specification_rejects_content_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        ApprovedSpecification(
            issue_key="GMAI-20",
            revision="revision-1",
            content=SPECIFICATION_CONTENT,
            content_sha256="0" * 64,
            approved_by="account-1",
        )


async def test_executor_streams_ordered_events_and_returns_success() -> None:
    executor = FakeCodingExecutor(
        capabilities=make_capabilities(),
        progress_messages=("Inspecting specification", "Running verification"),
    )

    session = await executor.start(make_request())
    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.kind for event in events] == [
        EventKind.STARTED,
        EventKind.PROGRESS,
        EventKind.PROGRESS,
        EventKind.TERMINAL,
    ]
    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.events_emitted == len(events)


async def test_executor_streams_typed_artifact_before_matching_result() -> None:
    artifact = ExecutionArtifact("reports/result.json", "application/json", ARTIFACT_DIGEST, 5)
    executor = FakeCodingExecutor(capabilities=make_capabilities(), artifacts=(artifact,))

    session = await executor.start(make_request())
    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.ARTIFACT, EventKind.TERMINAL]
    assert events[1].artifact is artifact
    assert result.artifacts == (artifact,)


async def test_executor_reports_resource_exhaustion_without_exceeding_event_limit() -> None:
    executor = FakeCodingExecutor(
        capabilities=make_capabilities(),
        progress_messages=("first", "second", "third"),
    )
    session = await executor.start(make_request(max_events=3))

    events = [event async for event in session.events()]
    result = await session.result()

    assert len(events) == 2
    assert len(events) <= result.limits.max_events
    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert events[-1].metadata["outcome"] == TerminalOutcome.RESOURCE_EXHAUSTED.value
    assert result.outcome is TerminalOutcome.RESOURCE_EXHAUSTED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.RESOURCE


async def test_executor_cancellation_is_idempotent_and_terminal() -> None:
    executor = FakeCodingExecutor(capabilities=make_capabilities(), progress_messages=("Unused work",))
    session = await executor.start(make_request())

    await session.cancel("Human authority checkpoint")
    await session.cancel("A later reason must not replace the first")
    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.CANCELLED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.CANCELLED
    assert result.failure.message == "Human authority checkpoint"


async def test_cancellation_after_started_matches_terminal_result() -> None:
    executor = FakeCodingExecutor(capabilities=make_capabilities(), progress_messages=("Must not run",))
    session = await executor.start(make_request())
    stream = session.events()

    started = await anext(stream)
    await session.cancel("Stopped after start")
    remaining = [event async for event in stream]
    result = await session.result()

    assert started.kind is EventKind.STARTED
    assert [event.kind for event in remaining] == [EventKind.TERMINAL]
    assert remaining[0].metadata["outcome"] == TerminalOutcome.CANCELLED.value
    assert result.outcome is TerminalOutcome.CANCELLED


async def test_cancellation_after_terminal_preserves_result() -> None:
    executor = FakeCodingExecutor(capabilities=make_capabilities())
    session = await executor.start(make_request())
    events = [event async for event in session.events()]
    before_cancel = await session.result()

    await session.cancel("Too late to alter the result")

    assert events[-1].metadata["outcome"] == TerminalOutcome.COMPLETED.value
    assert await session.result() is before_cancel
    assert before_cancel.outcome is TerminalOutcome.COMPLETED


def test_execution_result_rejects_artifacts_over_the_byte_limit() -> None:
    request = make_request()
    oversized = ExecutionArtifact(
        relative_path="reports/result.json",
        media_type="application/json",
        content_sha256=ARTIFACT_DIGEST,
        size_bytes=request.limits.max_artifact_bytes + 1,
    )

    with pytest.raises(ValueError, match="artifact bytes"):
        ExecutionResult(
            execution_id=request.execution_id,
            outcome=TerminalOutcome.COMPLETED,
            limits=request.limits,
            events_emitted=2,
            artifacts=(oversized,),
        )


def test_execution_result_requires_failure_for_non_success_outcome() -> None:
    request = make_request()

    with pytest.raises(ValueError, match="must include a failure"):
        ExecutionResult(
            execution_id=request.execution_id,
            outcome=TerminalOutcome.FAILED,
            limits=request.limits,
            events_emitted=2,
        )


async def test_executor_rejects_grants_outside_its_capabilities() -> None:
    executor = FakeCodingExecutor(
        capabilities=ExecutorCapabilities(streaming=True, cancellation=True, tool_names=frozenset())
    )

    with pytest.raises(ValueError, match="unsupported tool grants: shell"):
        await executor.start(make_request())


async def test_executor_start_is_idempotent_for_the_same_request() -> None:
    executor = FakeCodingExecutor(capabilities=make_capabilities())
    request = make_request()

    first = await executor.start(request)
    second = await executor.start(request)

    assert first is second


def test_execution_result_supports_a_blocked_terminal_outcome() -> None:
    request = make_request()
    failure = FailureMetadata(
        category=FailureCategory.POLICY,
        code="approval_required",
        message="A human approval checkpoint is required",
    )

    result = ExecutionResult(
        execution_id=request.execution_id,
        outcome=TerminalOutcome.BLOCKED,
        limits=request.limits,
        events_emitted=2,
        failure=failure,
    )

    assert result.outcome is TerminalOutcome.BLOCKED


def test_terminal_outcomes_cover_the_governed_execution_contract() -> None:
    assert set(TerminalOutcome) == {
        TerminalOutcome.COMPLETED,
        TerminalOutcome.BLOCKED,
        TerminalOutcome.CANCELLED,
        TerminalOutcome.TIMED_OUT,
        TerminalOutcome.FAILED,
        TerminalOutcome.RESOURCE_EXHAUSTED,
    }
