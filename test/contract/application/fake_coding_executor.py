"""Deterministic fake for consumers of the coding executor contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from gearmeshing_ai.application.ports.coding_executor import (
    CodingExecutor,
    EventKind,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSession,
    ExecutorCapabilities,
    FailureCategory,
    FailureMetadata,
    TerminalOutcome,
)


def _text(value: str, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


class FakeExecutionSession:
    """Single-consumer session with repeatable scripted behavior."""

    def __init__(
        self,
        request: ExecutionRequest,
        *,
        progress_messages: tuple[str, ...],
        outcome: TerminalOutcome,
        artifacts: tuple[ExecutionArtifact, ...],
        failure: FailureMetadata | None,
        cancellation_supported: bool,
    ) -> None:
        self._request = request
        self._progress_messages = progress_messages
        self._outcome = outcome
        self._artifacts = artifacts
        self._failure = failure
        self._cancellation_supported = cancellation_supported
        self._cancel_reason: str | None = None
        self._consumed = False
        self._result: ExecutionResult | None = None

    @property
    def execution_id(self) -> str:
        return self._request.execution_id

    def events(self) -> AsyncIterator[ExecutionEvent]:
        if self._consumed:
            raise RuntimeError("the event stream may only be consumed once")
        self._consumed = True
        return self._stream_events()

    async def _stream_events(self) -> AsyncIterator[ExecutionEvent]:
        events = self._build_events()
        for event in events:
            yield event
        self._result = self._build_result(len(events))

    async def result(self) -> ExecutionResult:
        if self._result is None:
            if self._consumed:
                raise RuntimeError("the event stream has not reached its terminal event")
            async for _ in self.events():
                pass
        assert self._result is not None
        return self._result

    async def cancel(self, reason: str) -> None:
        if not self._cancellation_supported:
            raise RuntimeError("cancellation is not supported")
        normalized = _text(reason, "cancellation reason", 512)
        if self._result is not None:
            return
        if self._cancel_reason is None:
            self._cancel_reason = normalized

    def _build_events(self) -> tuple[ExecutionEvent, ...]:
        events = [ExecutionEvent(1, EventKind.STARTED, "Execution started")]
        if self._cancel_reason is None:
            events.extend(
                ExecutionEvent(index, EventKind.PROGRESS, message)
                for index, message in enumerate(self._progress_messages, start=2)
            )
        terminal_outcome = TerminalOutcome.CANCELLED if self._cancel_reason is not None else self._outcome
        events.append(
            ExecutionEvent(
                len(events) + 1,
                EventKind.TERMINAL,
                "Execution reached a terminal outcome",
                {"outcome": terminal_outcome.value},
            )
        )
        return tuple(events)

    def _build_result(self, events_emitted: int) -> ExecutionResult:
        failure: FailureMetadata | None
        if self._cancel_reason is not None:
            failure = FailureMetadata(
                FailureCategory.CANCELLED,
                "cancelled_by_caller",
                self._cancel_reason,
            )
            outcome = TerminalOutcome.CANCELLED
            artifacts: tuple[ExecutionArtifact, ...] = ()
        else:
            failure = self._failure
            outcome = self._outcome
            artifacts = self._artifacts
        return ExecutionResult(
            execution_id=self.execution_id,
            outcome=outcome,
            limits=self._request.limits,
            events_emitted=events_emitted,
            artifacts=artifacts,
            failure=failure,
        )


class FakeCodingExecutor:
    """Provider-independent fake that makes contract tests deterministic."""

    def __init__(
        self,
        *,
        capabilities: ExecutorCapabilities,
        progress_messages: Iterable[str] = (),
        outcome: TerminalOutcome = TerminalOutcome.COMPLETED,
        artifacts: Iterable[ExecutionArtifact] = (),
        failure: FailureMetadata | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._progress_messages = tuple(_text(message, "progress message", 2048) for message in progress_messages)
        self._outcome = outcome
        self._artifacts = tuple(artifacts)
        self._failure = failure
        self._sessions: dict[str, tuple[ExecutionRequest, FakeExecutionSession]] = {}

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return self._capabilities

    async def start(self, request: ExecutionRequest) -> ExecutionSession:
        unsupported = {grant.tool for grant in request.tool_grants} - self.capabilities.tool_names
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported tool grants: {names}")
        existing = self._sessions.get(request.execution_id)
        if existing is not None:
            previous_request, session = existing
            if previous_request != request:
                raise ValueError("execution_id is already bound to a different request")
            return session
        session = FakeExecutionSession(
            request,
            progress_messages=self._progress_messages,
            outcome=self._outcome,
            artifacts=self._artifacts,
            failure=self._failure,
            cancellation_supported=self.capabilities.cancellation,
        )
        self._sessions[request.execution_id] = (request, session)
        return session


def assert_executor_contract(executor: CodingExecutor) -> CodingExecutor:
    """Give static type checkers a direct structural-protocol assertion."""
    return executor
