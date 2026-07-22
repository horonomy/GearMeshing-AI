"""Contract tests for provider-neutral coding execution."""

from __future__ import annotations

from hashlib import sha256

import pytest

from gearmeshing_ai.application.ports.coding_executor import (
    ApprovedSpecification,
    CodingExecutor,
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
    ToolGrant,
)
from test.contract.application.fake_coding_executor import FakeCodingExecutor, assert_executor_contract

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
