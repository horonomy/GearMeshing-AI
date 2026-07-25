"""Claude Code CLI adapter implementing the governed ``CodingExecutor`` contract.

This adapter launches the Claude Code CLI (``claude``) as a non-interactive
subprocess pinned to an isolated worktree, streams its output as ordered
``ExecutionEvent`` values, and settles to a validated ``ExecutionResult``. It
contains no Jira-specific or ``WorkManagementProvider`` behavior: it depends
only on the provider-neutral ``ApprovedSpecification`` and the rest of the
``gearmeshing_ai.application.ports.coding_executor`` contract.

CLI INVOCATION ASSUMPTION (documented, unverified in this sandbox)
-------------------------------------------------------------------
This sandbox has no network access to the real ``claude`` CLI, so the exact
flag names and streamed JSON schema below are an assumption, not a verified
fact. The assumption is isolated in :func:`build_claude_cli_argv` and in the
line-translation helpers (``_translate_progress_line``, ``_parse_line``, and
``ClaudeCodeExecutionSession._finalize_result``) so a future correction only
touches those functions, not the rest of the adapter:

* Non-interactive "print" mode is invoked as ``claude -p "<prompt>"``, which
  runs one turn against the given prompt and exits rather than opening an
  interactive REPL.
* ``--output-format stream-json`` causes the CLI to write one JSON object per
  line to stdout, each with a ``"type"`` discriminator: ``"system"`` (session
  banner), ``"assistant"`` (a turn of assistant output, whose text is nested
  under ``message.content``), ``"tool_use"`` / ``"tool_result"`` (tool
  lifecycle), and a single final ``"result"`` line carrying ``is_error``
  (bool), an optional ``"result"`` string summary, and an optional
  ``"changed_files"`` list of worktree-relative paths.
* ``--allowedTools <comma-separated tool identifiers>`` restricts which
  tools the CLI may invoke, sourced directly from the granted
  ``ToolGrant.tool`` identifiers on the request (this adapter does not
  translate them into any provider-specific tool vocabulary).
* The full specification text is passed as the ``-p`` argument value. Very
  large specifications could exceed the operating system's argv length limit;
  this MVP does not yet fall back to piping the prompt over stdin.

If any of these flags or the JSON schema turn out to differ from the real
CLI, only the functions named above need to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from gearmeshing_ai.application.ports.coding_executor import (
    EventKind,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    FailureCategory,
    FailureMetadata,
    ResourceLimits,
    TerminalOutcome,
)

_DEFAULT_MEDIA_TYPE: Final = "application/octet-stream"
_MESSAGE_LIMIT: Final = 2048


def _bounded_text(value: str, name: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _truncated(value: str, *, maximum: int = _MESSAGE_LIMIT) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return "Claude Code reported no further detail"
    return normalized if len(normalized) <= maximum else normalized[: maximum - 1] + "…"


def _resource_failure(kind: str) -> FailureMetadata:
    return FailureMetadata(
        FailureCategory.RESOURCE,
        "execution_limit_exhausted",
        f"The execution plan exceeds its {kind} limit",
    )


class SubprocessHandle(Protocol):
    """A running Claude Code process, abstracted so tests never shell out."""

    async def read_line(self) -> bytes | None:
        """Return the next stdout line without its trailing newline, or ``None`` at EOF."""
        ...

    async def wait(self) -> int:
        """Wait for process exit and return its exit code."""
        ...

    def kill(self) -> None:
        """Forcefully terminate the process. Must be idempotent."""
        ...


class ProcessLauncher(Protocol):
    """Starts the Claude Code CLI subprocess for one execution; injectable for tests."""

    async def launch(self, argv: Sequence[str], *, cwd: str) -> SubprocessHandle:
        """Start ``argv`` with its working directory pinned to ``cwd``."""
        ...


class AsyncioSubprocessHandle:
    """Default :class:`SubprocessHandle` backed by ``asyncio.create_subprocess_exec``."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def read_line(self) -> bytes | None:
        if self._process.stdout is None:  # pragma: no cover - always piped by the launcher
            return None
        line = await self._process.stdout.readline()
        return line if line else None

    async def wait(self) -> int:
        return await self._process.wait()

    def kill(self) -> None:
        if self._process.returncode is None:
            self._process.kill()


class AsyncioProcessLauncher:
    """Default :class:`ProcessLauncher` that runs the real ``claude`` CLI."""

    async def launch(self, argv: Sequence[str], *, cwd: str) -> SubprocessHandle:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return AsyncioSubprocessHandle(process)


def build_claude_cli_argv(request: ExecutionRequest) -> tuple[str, ...]:
    """Construct the non-interactive Claude Code CLI invocation for one execution.

    See the module docstring for the documented, unverified assumption this
    function embodies. Isolated here so the invocation can be corrected
    without touching session or event-translation logic.
    """
    argv = ["claude", "-p", request.specification.content, "--output-format", "stream-json"]
    tool_names = sorted({grant.tool for grant in request.tool_grants})
    if tool_names:
        argv += ["--allowedTools", ",".join(tool_names)]
    return tuple(argv)


def _parse_line(raw: bytes) -> Mapping[str, object] | None:
    """Decode one stdout line as a JSON object, tolerating partial or malformed output."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_assistant_text(payload: Mapping[str, object]) -> str:
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    fragments = [
        block["text"]
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return " ".join(fragments)


def _extract_tool_name(payload: Mapping[str, object]) -> str:
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "unknown_tool"


@dataclass(frozen=True, slots=True)
class _PendingEvent:
    """One translated non-terminal event awaiting a sequence number."""

    kind: EventKind
    message: str
    metadata: Mapping[str, str | int | float | None] = field(default_factory=dict)


def _translate_progress_line(payload: Mapping[str, object]) -> _PendingEvent | None:
    """Translate one non-terminal stdout line into a progress-shaped event, if recognized."""
    line_type = payload.get("type")
    if line_type == "assistant":
        text = _extract_assistant_text(payload)
        return _PendingEvent(EventKind.PROGRESS, _truncated(text) if text else "Claude Code produced a turn")
    if line_type == "system":
        return _PendingEvent(EventKind.PROGRESS, "Claude Code session initialized")
    if line_type == "tool_use":
        tool_name = _extract_tool_name(payload)
        return _PendingEvent(EventKind.TOOL_STARTED, f"Tool started: {tool_name}", {"tool": tool_name})
    if line_type == "tool_result":
        tool_name = _extract_tool_name(payload)
        return _PendingEvent(EventKind.TOOL_FINISHED, f"Tool finished: {tool_name}", {"tool": tool_name})
    return None


def _guess_media_type(relative_path: str) -> str:
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or _DEFAULT_MEDIA_TYPE


class ClaudeCodeExecutionSession:
    """Single-consumer session that streams one Claude Code CLI subprocess run."""

    def __init__(
        self,
        request: ExecutionRequest,
        *,
        argv: tuple[str, ...],
        launcher: ProcessLauncher,
        cancellation_supported: bool,
    ) -> None:
        self._request = request
        self._argv = argv
        self._launcher = launcher
        self._cancellation_supported = cancellation_supported
        self._cancel_reason: str | None = None
        self._consumed = False
        self._stream: AsyncIterator[ExecutionEvent] | None = None
        self._result: ExecutionResult | None = None
        self._handle: SubprocessHandle | None = None

    @property
    def execution_id(self) -> str:
        return self._request.execution_id

    def events(self) -> AsyncIterator[ExecutionEvent]:
        if self._consumed:
            raise RuntimeError("the event stream may only be consumed once")
        self._consumed = True
        self._stream = self._stream_events()
        return self._stream

    async def result(self) -> ExecutionResult:
        if self._result is None:
            stream = self._stream if self._stream is not None else self.events()
            async for _ in stream:
                pass
        if self._result is None:
            raise RuntimeError("the event stream closed before a terminal result")
        return self._result

    async def cancel(self, reason: str) -> None:
        if not self._cancellation_supported:
            raise RuntimeError("cancellation is not supported")
        normalized = _bounded_text(reason, "cancellation reason", maximum=512)
        if self._result is not None:
            return
        if self._cancel_reason is None:
            self._cancel_reason = normalized
        if self._handle is not None:
            self._handle.kill()

    async def _stream_events(self) -> AsyncIterator[ExecutionEvent]:
        limits = self._request.limits
        sequence = 1
        yield ExecutionEvent(sequence, EventKind.STARTED, "Claude Code execution started")
        handle = await self._launcher.launch(self._argv, cwd=self._request.repository.worktree_root)
        self._handle = handle
        outcome: TerminalOutcome | None = None
        failure: FailureMetadata | None = None
        artifacts: tuple[ExecutionArtifact, ...] = ()
        try:
            deadline = time.monotonic() + limits.wall_clock_seconds
            while True:
                if self._is_cancelled():
                    outcome, failure = self._cancelled_outcome()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome, failure = self._timed_out_outcome()
                    break
                try:
                    raw_line = await asyncio.wait_for(handle.read_line(), timeout=remaining)
                except TimeoutError:
                    outcome, failure = self._timed_out_outcome()
                    break
                except OSError:
                    outcome = TerminalOutcome.FAILED
                    failure = FailureMetadata(
                        FailureCategory.PROVIDER,
                        "process_io_error",
                        "Claude Code's output stream failed before reporting a terminal result",
                    )
                    break
                if self._is_cancelled():
                    outcome, failure = self._cancelled_outcome()
                    break
                if raw_line is None:
                    outcome = TerminalOutcome.FAILED
                    failure = FailureMetadata(
                        FailureCategory.PROVIDER,
                        "process_exited_without_result",
                        "Claude Code exited before reporting a terminal result",
                    )
                    break
                parsed = _parse_line(raw_line)
                if parsed is None:
                    continue
                if parsed.get("type") == "result":
                    outcome, failure, artifacts = self._finalize_result(parsed, sequence, limits)
                    break
                pending = _translate_progress_line(parsed)
                if pending is None:
                    continue
                if sequence + 2 > limits.max_events:
                    outcome, failure = TerminalOutcome.RESOURCE_EXHAUSTED, _resource_failure("event")
                    break
                sequence += 1
                yield ExecutionEvent(sequence, pending.kind, pending.message, pending.metadata)
        finally:
            handle.kill()
            await handle.wait()
        assert outcome is not None
        if outcome is TerminalOutcome.COMPLETED:
            for artifact in artifacts:
                sequence += 1
                yield ExecutionEvent(
                    sequence,
                    EventKind.ARTIFACT,
                    f"Produced artifact {artifact.relative_path}",
                    artifact=artifact,
                )
        else:
            artifacts = ()
        sequence += 1
        self._result = ExecutionResult(
            execution_id=self.execution_id,
            outcome=outcome,
            limits=limits,
            events_emitted=sequence,
            artifacts=artifacts,
            failure=failure,
        )
        yield ExecutionEvent(
            sequence,
            EventKind.TERMINAL,
            "Execution reached a terminal outcome",
            {"outcome": outcome.value},
        )

    def _is_cancelled(self) -> bool:
        return self._cancel_reason is not None

    def _cancelled_outcome(self) -> tuple[TerminalOutcome, FailureMetadata]:
        assert self._cancel_reason is not None
        return TerminalOutcome.CANCELLED, FailureMetadata(
            FailureCategory.CANCELLED, "cancelled_by_caller", self._cancel_reason
        )

    @staticmethod
    def _timed_out_outcome() -> tuple[TerminalOutcome, FailureMetadata]:
        return TerminalOutcome.TIMED_OUT, FailureMetadata(
            FailureCategory.TIMEOUT,
            "wall_clock_exceeded",
            "Claude Code did not report a terminal result within its wall-clock limit",
        )

    def _finalize_result(
        self,
        payload: Mapping[str, object],
        current_sequence: int,
        limits: ResourceLimits,
    ) -> tuple[TerminalOutcome, FailureMetadata | None, tuple[ExecutionArtifact, ...]]:
        is_error = payload.get("is_error")
        if not isinstance(is_error, bool):
            return (
                TerminalOutcome.FAILED,
                FailureMetadata(
                    FailureCategory.PROVIDER,
                    "malformed_result_payload",
                    "Claude Code's result line did not include a boolean is_error field",
                ),
                (),
            )
        if is_error:
            summary = payload.get("result")
            message = _truncated(summary) if isinstance(summary, str) and summary else "Claude Code reported an error"
            failure = FailureMetadata(FailureCategory.PROVIDER, "claude_code_reported_error", message)
            return TerminalOutcome.FAILED, failure, ()
        raw_changed = payload.get("changed_files")
        changed_files = (
            sorted({path for path in raw_changed if isinstance(path, str)}) if isinstance(raw_changed, list) else []
        )
        boundary_failure = self._boundary_violation(changed_files)
        if boundary_failure is not None:
            return TerminalOutcome.BLOCKED, boundary_failure, ()
        try:
            artifacts, missing = self._build_artifacts(changed_files)
        except OSError:
            return (
                TerminalOutcome.FAILED,
                FailureMetadata(
                    FailureCategory.PROVIDER,
                    "changed_file_unreadable",
                    "Claude Code reported a changed file that could not be read",
                ),
                (),
            )
        if missing is not None:
            return (
                TerminalOutcome.FAILED,
                FailureMetadata(
                    FailureCategory.PROVIDER,
                    "changed_file_not_found",
                    f"Claude Code reported a changed file that does not exist: {missing}",
                ),
                (),
            )
        if current_sequence + len(artifacts) + 1 > limits.max_events:
            return TerminalOutcome.RESOURCE_EXHAUSTED, _resource_failure("event"), ()
        if len(artifacts) > limits.max_artifacts:
            return TerminalOutcome.RESOURCE_EXHAUSTED, _resource_failure("artifact count"), ()
        if sum(artifact.size_bytes for artifact in artifacts) > limits.max_artifact_bytes:
            return TerminalOutcome.RESOURCE_EXHAUSTED, _resource_failure("artifact byte"), ()
        return TerminalOutcome.COMPLETED, None, artifacts

    def _boundary_violation(self, changed_files: list[str]) -> FailureMetadata | None:
        """Enforce that every reported change stays within the governed boundary.

        Rejects (as a POLICY failure, never silently) any reported path that is
        absolute, escapes the worktree via ``..`` segments, falls outside a
        configured ``writable_paths`` restriction, or - after resolving
        symlinks - physically escapes ``worktree_root``.
        """
        repository = self._request.repository
        worktree_root = Path(repository.worktree_root)
        writable_paths = repository.writable_paths
        for relative in changed_files:
            candidate = PurePosixPath(relative)
            if relative != candidate.as_posix() or candidate.is_absolute() or ".." in candidate.parts or not relative:
                return FailureMetadata(
                    FailureCategory.POLICY,
                    "changed_file_outside_governed_boundary",
                    f"Claude Code reported a change outside the governed workspace boundary: {relative}",
                )
            if writable_paths and not self._within_writable_paths(candidate, writable_paths):
                return FailureMetadata(
                    FailureCategory.POLICY,
                    "changed_file_outside_writable_paths",
                    f"Claude Code reported a change outside its granted writable paths: {relative}",
                )
            resolved = worktree_root / candidate
            try:
                resolved.resolve().relative_to(worktree_root.resolve())
            except ValueError:
                return FailureMetadata(
                    FailureCategory.POLICY,
                    "changed_file_escapes_worktree",
                    f"Claude Code reported a change that escapes the isolated worktree: {relative}",
                )
        return None

    @staticmethod
    def _within_writable_paths(candidate: PurePosixPath, writable_paths: tuple[str, ...]) -> bool:
        ancestors = {str(parent) for parent in candidate.parents}
        return any(candidate == PurePosixPath(path) or path in ancestors for path in writable_paths)

    def _build_artifacts(self, changed_files: list[str]) -> tuple[tuple[ExecutionArtifact, ...], str | None]:
        worktree_root = Path(self._request.repository.worktree_root)
        artifacts: list[ExecutionArtifact] = []
        for relative in changed_files:
            resolved = worktree_root / PurePosixPath(relative)
            if not resolved.is_file():
                return tuple(artifacts), relative
            size_bytes = resolved.stat().st_size
            if size_bytes == 0:
                continue
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            artifacts.append(
                ExecutionArtifact(
                    relative_path=relative,
                    media_type=_guess_media_type(relative),
                    content_sha256=digest,
                    size_bytes=size_bytes,
                )
            )
        return tuple(artifacts), None


class ClaudeCodeExecutor:
    """Governed adapter that runs the Claude Code CLI as its coding capability."""

    def __init__(
        self,
        *,
        capabilities: ExecutorCapabilities,
        launcher: ProcessLauncher | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._launcher = launcher if launcher is not None else AsyncioProcessLauncher()
        self._sessions: dict[str, tuple[ExecutionRequest, ClaudeCodeExecutionSession]] = {}

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return self._capabilities

    async def start(self, request: ExecutionRequest) -> ClaudeCodeExecutionSession:
        unsupported = {grant.tool for grant in request.tool_grants} - self._capabilities.tool_names
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported tool grants: {names}")
        existing = self._sessions.get(request.execution_id)
        if existing is not None:
            previous_request, session = existing
            if previous_request != request:
                raise ValueError("execution_id is already bound to a different request")
            return session
        session = ClaudeCodeExecutionSession(
            request,
            argv=build_claude_cli_argv(request),
            launcher=self._launcher,
            cancellation_supported=self._capabilities.cancellation,
        )
        self._sessions[request.execution_id] = (request, session)
        return session
