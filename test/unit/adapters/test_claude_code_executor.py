"""Unit tests for the Claude Code CLI executor adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

import pytest

from gearmeshing_ai.adapters.claude_code_executor import (
    ClaudeCodeExecutor,
    SubprocessHandle,
    build_claude_cli_argv,
)
from gearmeshing_ai.application.ports.coding_executor import (
    ApprovedSpecification,
    EventKind,
    ExecutionRequest,
    ExecutorCapabilities,
    FailureCategory,
    RepositoryContext,
    ResourceLimits,
    TerminalOutcome,
    ToolGrant,
)

SPECIFICATION_CONTENT = "Implement the fixture task for the Claude Code executor."
SPECIFICATION_DIGEST = sha256(SPECIFICATION_CONTENT.encode()).hexdigest()


def make_request(
    worktree_root: Path,
    *,
    execution_id: str = "execution-1",
    writable_paths: tuple[str, ...] = (),
    wall_clock_seconds: float = 5.0,
    max_events: int = 10,
    max_artifacts: int = 2,
    max_artifact_bytes: int = 1024,
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        specification=ApprovedSpecification(
            issue_key="GMAI-21",
            revision="revision-1",
            content=SPECIFICATION_CONTENT,
            content_sha256=SPECIFICATION_DIGEST,
            approved_by="account-1",
        ),
        repository=RepositoryContext(
            repository_root="/workspace/GearMeshing-AI",
            worktree_root=worktree_root.as_posix(),
            base_ref="main",
            branch="mvp1/GMAI-21/claude_code_executor_adapter",
            writable_paths=writable_paths,
        ),
        limits=ResourceLimits(
            wall_clock_seconds=wall_clock_seconds,
            max_events=max_events,
            max_artifacts=max_artifacts,
            max_artifact_bytes=max_artifact_bytes,
        ),
        tool_grants=(ToolGrant("shell", frozenset({"execute"}), ("pytest",)),),
    )


def make_capabilities(*, cancellation: bool = True) -> ExecutorCapabilities:
    return ExecutorCapabilities(streaming=True, cancellation=cancellation, tool_names=frozenset({"shell"}))


class FakeSubprocessHandle:
    """Deterministic, injectable stand-in for a running Claude Code process."""

    def __init__(
        self,
        lines: Sequence[bytes],
        *,
        hang: bool = False,
        raise_on_read: Exception | None = None,
    ) -> None:
        self._lines = list(lines)
        self._hang = hang
        self._raise_on_read = raise_on_read
        self.killed = False
        self.waited = False

    async def read_line(self) -> bytes | None:
        if self._raise_on_read is not None:
            raise self._raise_on_read
        if self._hang and not self.killed:
            await asyncio.sleep(3600)
        if not self._lines:
            return None
        return self._lines.pop(0)

    async def wait(self) -> int:
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


class FakeProcessLauncher:
    """Records the launch invocation and returns a pre-built fake handle."""

    def __init__(self, handle: SubprocessHandle) -> None:
        self.handle = handle
        self.launched_argv: tuple[str, ...] | None = None
        self.launched_cwd: str | None = None

    async def launch(self, argv: Sequence[str], *, cwd: str) -> SubprocessHandle:
        self.launched_argv = tuple(argv)
        self.launched_cwd = cwd
        return self.handle


def result_line(*, is_error: bool = False, result: str | None = None, changed_files: list[str] | None = None) -> bytes:
    payload: dict[str, object] = {"type": "result", "is_error": is_error}
    if result is not None:
        payload["result"] = result
    if changed_files is not None:
        payload["changed_files"] = changed_files
    return json.dumps(payload).encode()


def assistant_line(text: str) -> bytes:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}).encode()


def tool_use_line(name: str) -> bytes:
    return json.dumps({"type": "tool_use", "name": name}).encode()


def tool_result_line(name: str) -> bytes:
    return json.dumps({"type": "tool_result", "name": name}).encode()


# --- CLI invocation construction ---------------------------------------------------------


def test_build_argv_pins_prompt_stream_json_and_allowed_tools(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    argv = build_claude_cli_argv(request)

    assert argv[:3] == ("claude", "-p", SPECIFICATION_CONTENT)
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "shell"


async def test_launcher_receives_worktree_root_as_cwd(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]

    assert launcher.launched_cwd == tmp_path.as_posix()
    assert events[0].kind is EventKind.STARTED


# --- Success path -------------------------------------------------------------------------


async def test_successful_run_streams_progress_and_artifact_then_completes(tmp_path: Path) -> None:
    (tmp_path / "output.txt").write_bytes(b"patch contents")
    launcher = FakeProcessLauncher(
        FakeSubprocessHandle(
            [
                assistant_line("Inspecting the specification"),
                tool_use_line("shell"),
                tool_result_line("shell"),
                result_line(changed_files=["output.txt"]),
            ]
        )
    )
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [
        EventKind.STARTED,
        EventKind.PROGRESS,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FINISHED,
        EventKind.ARTIFACT,
        EventKind.TERMINAL,
    ]
    assert events[-2].artifact is not None
    assert events[-2].artifact.relative_path == "output.txt"
    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.failure is None
    assert result.artifacts[0].relative_path == "output.txt"
    assert result.artifacts[0].size_bytes == len(b"patch contents")


async def test_successful_run_with_no_changed_files_completes_without_artifacts(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.artifacts == ()


async def test_malformed_and_unparsable_lines_are_skipped_without_raising(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(
        FakeSubprocessHandle(
            [
                b"not json at all {{{",
                b"",
                json.dumps({"type": "unknown_event"}).encode(),
                result_line(),
            ]
        )
    )
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.COMPLETED


# --- Failure path --------------------------------------------------------------------------


async def test_result_line_with_is_error_true_fails_with_provider_category(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(is_error=True, result="boom")]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.FAILED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.PROVIDER
    assert result.failure.message == "boom"


async def test_process_exit_without_result_line_fails_tolerantly(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([assistant_line("partial output")]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    result = await session.result()

    assert events[-1].kind is EventKind.TERMINAL
    assert result.outcome is TerminalOutcome.FAILED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.PROVIDER
    assert result.failure.code == "process_exited_without_result"


async def test_missing_is_error_field_is_treated_as_malformed_provider_output(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([json.dumps({"type": "result"}).encode()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    result = await session.result()

    assert result.outcome is TerminalOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "malformed_result_payload"


async def test_changed_file_reported_but_missing_on_disk_fails_tolerantly(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["ghost.txt"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    result = await session.result()

    assert result.outcome is TerminalOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "changed_file_not_found"


async def test_process_io_error_fails_tolerantly(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([], raise_on_read=OSError("pipe broke")))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    result = await session.result()

    assert result.outcome is TerminalOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "process_io_error"


# --- Timeout --------------------------------------------------------------------------------


async def test_wall_clock_timeout_kills_process_and_settles_timed_out(tmp_path: Path) -> None:
    handle = FakeSubprocessHandle([], hang=True)
    launcher = FakeProcessLauncher(handle)
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path, wall_clock_seconds=0.05))

    events = [event async for event in session.events()]
    result = await session.result()

    assert handle.killed is True
    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.TIMED_OUT
    assert result.failure is not None
    assert result.failure.category is FailureCategory.TIMEOUT


# --- Cancellation -----------------------------------------------------------------------------


async def test_cancel_before_consuming_events_kills_process_and_settles_cancelled(tmp_path: Path) -> None:
    handle = FakeSubprocessHandle([], hang=True)
    launcher = FakeProcessLauncher(handle)
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))
    stream = session.events()

    started = await anext(stream)
    await session.cancel("Human authority checkpoint")
    remaining = [event async for event in stream]
    result = await session.result()

    assert started.kind is EventKind.STARTED
    assert handle.killed is True
    assert [event.kind for event in remaining] == [EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.CANCELLED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.CANCELLED
    assert result.failure.message == "Human authority checkpoint"


async def test_cancel_is_idempotent_and_preserves_the_first_reason(tmp_path: Path) -> None:
    handle = FakeSubprocessHandle([], hang=True)
    launcher = FakeProcessLauncher(handle)
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    await session.cancel("First reason")
    await session.cancel("A later reason must not replace the first")
    events = [event async for event in session.events()]
    result = await session.result()

    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TERMINAL]
    assert result.failure is not None
    assert result.failure.message == "First reason"


async def test_cancel_raises_when_capability_disabled(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(cancellation=False), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    with pytest.raises(RuntimeError, match="cancellation is not supported"):
        await session.cancel("Attempted cancellation")


async def test_cancel_after_terminal_result_is_a_no_op(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    events = [event async for event in session.events()]
    before_cancel = await session.result()
    await session.cancel("Too late to alter the result")

    assert events[-1].metadata["outcome"] == TerminalOutcome.COMPLETED.value
    assert await session.result() is before_cancel


# --- Workspace / governance boundary -----------------------------------------------------------


async def test_absolute_changed_file_path_is_blocked_as_a_policy_failure(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["/etc/passwd"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.POLICY
    assert result.failure.code == "changed_file_outside_governed_boundary"


async def test_parent_traversal_changed_file_path_is_blocked_as_a_policy_failure(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["../secret.txt"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.POLICY


async def test_symlink_escaping_worktree_is_blocked_as_a_policy_failure(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "linked.txt").symlink_to(outside)
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["linked.txt"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(worktree))

    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.code == "changed_file_escapes_worktree"


async def test_change_outside_configured_writable_paths_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "unrelated.txt").write_bytes(b"data")
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["unrelated.txt"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path, writable_paths=("allowed/output.txt",)))

    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.code == "changed_file_outside_writable_paths"


async def test_change_within_configured_writable_directory_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "output.txt").write_bytes(b"ok")
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["allowed/output.txt"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path, writable_paths=("allowed",)))

    result = await session.result()

    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.artifacts[0].relative_path == "allowed/output.txt"


# --- Resource preflight (mirrors the fake executor's discipline) ------------------------------


async def test_artifact_over_byte_limit_is_reported_as_resource_exhausted(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 2000)
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line(changed_files=["big.bin"])]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path, max_artifact_bytes=1024))

    result = await session.result()

    assert result.outcome is TerminalOutcome.RESOURCE_EXHAUSTED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.RESOURCE
    assert result.artifacts == ()


async def test_progress_events_exceeding_max_events_are_reported_as_resource_exhausted(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(
        FakeSubprocessHandle(
            [assistant_line("first"), assistant_line("second"), assistant_line("third"), result_line()]
        )
    )
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path, max_events=3))

    events = [event async for event in session.events()]
    result = await session.result()

    assert len(events) <= result.limits.max_events
    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.PROGRESS, EventKind.TERMINAL]
    assert result.outcome is TerminalOutcome.RESOURCE_EXHAUSTED


# --- Executor-level contract behavior ----------------------------------------------------------


async def test_executor_rejects_grants_outside_its_capabilities(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(
        capabilities=ExecutorCapabilities(streaming=True, cancellation=True, tool_names=frozenset()),
        launcher=launcher,
    )

    with pytest.raises(ValueError, match="unsupported tool grants: shell"):
        await executor.start(make_request(tmp_path))


async def test_executor_start_is_idempotent_for_the_same_request(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    request = make_request(tmp_path)

    first = await executor.start(request)
    second = await executor.start(request)

    assert first is second


async def test_events_may_only_be_consumed_once(tmp_path: Path) -> None:
    launcher = FakeProcessLauncher(FakeSubprocessHandle([result_line()]))
    executor = ClaudeCodeExecutor(capabilities=make_capabilities(), launcher=launcher)
    session = await executor.start(make_request(tmp_path))

    session.events()

    with pytest.raises(RuntimeError, match="only be consumed once"):
        session.events()
