"""Docker-backed adapter that runs one governed command inside an ephemeral, isolated container.

This module is the local, single-host execution boundary described by
GMAI-23: given a :class:`~gearmeshing_ai.application.ports.coding_executor.RepositoryContext`
(produced by ``GitWorktreeManager.ensure_worktree`` in ``git_worktree.py``), a command to
run, and an explicit set of resource limits, it constructs and shells out to a
``docker run`` invocation that:

* Mounts *only* ``repository.worktree_root`` (plus any explicitly declared
  :class:`CacheMount` paths) into the container. ``repository.repository_root``
  - the shared source checkout every worktree branches from - is never
  mounted, even though it is reachable on the ``RepositoryContext`` passed in.
* Applies ``--network=none`` (deny-by-default) unless the caller names a
  pre-existing, operator-configured Docker network via
  :class:`NetworkPolicy.allowed_network`. This adapter does not create,
  inspect, or otherwise manage that named network's own egress policy - it
  only attaches the container to it. Genuine destination-level (host/port)
  allowlisting is out of scope for this MVP; see the ticket's stated
  out-of-scope items (Kubernetes, multi-tenant hostile-code isolation, a
  remote sandbox fleet).
* Applies ``--cpus``, ``--memory``, and ``--pids-limit`` from
  :class:`SandboxResourceLimits`, and a read-only container root filesystem
  (``--read-only``) so nothing outside the worktree mount and declared cache
  mounts can be written to.
* Actively enforces the wall-clock limit by calling ``docker kill
  <container_name>`` - never a broad ``pkill`` - if the container is still
  running at the deadline, mirroring the timeout-kill pattern in
  ``claude_code_executor.py``. The same ``docker kill`` path backs
  :meth:`SandboxSession.cancel`.
* Injects caller-declared environment variables into the container via
  ``-e KEY`` (name only, never ``-e KEY=VALUE``) so secret *values* are never
  written into the subprocess argv (visible to any local ``ps``); the value
  is instead placed only in the environment of the local ``docker`` client
  process, which forwards it into the container on the daemon side. That
  client-launch environment is itself a small explicit allowlist
  (``_HOST_LAUNCH_ENV_ALLOWLIST``) rather than the full ambient host
  environment, so nothing beyond what ``docker`` needs to reach its daemon
  and what the caller explicitly asked to inject ever reaches this call.

Mount and image safety are checked by :func:`_mount_policy_violation` and the
``SandboxRunRequest`` constructor *before* :meth:`DockerSandbox.start` ever
calls the injected launcher, so a rejected request never results in a real
``docker run`` invocation. Violations are surfaced as a ``BLOCKED``
:class:`SandboxResult` carrying a ``FailureMetadata`` with
``FailureCategory.POLICY`` - the same category and shape
``claude_code_executor.py`` uses for its own workspace-boundary violations -
rather than as a raised exception.

SCOPE NOTE: ``exit_code`` and ``stdout``/``stderr`` on a ``COMPLETED`` result
are exactly what the underlying ``docker run`` process reported. This adapter
does not attempt to distinguish "the sandboxed command exited non-zero" from
"``docker run`` itself failed to start the container" (for example, because
the image does not exist or the daemon is unreachable) - both surface as a
``COMPLETED`` result with a non-zero ``exit_code``, since telling them apart
reliably from the outside would require parsing ``docker``'s own error
output. Wiring this adapter into ``workflow_runner.py`` or into a future
"run repository-defined quality checks" ticket is deliberately out of scope
here, per this sprint's contract-first, integration-deferred pattern.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final, Protocol

from gearmeshing_ai.application.ports.coding_executor import (
    FailureCategory,
    FailureMetadata,
    RepositoryContext,
    TerminalOutcome,
    _absolute_path,
    _finite_positive_float,
    _identifier,
    _positive_int,
)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,255}$")
_CONTAINER_NAME_PREFIX: Final = "gmai-sandbox-"
_RESERVED_CONTAINER_PATHS: Final = frozenset({"/", "/proc", "/sys", "/dev", "/etc", "/run", "/var/run/docker.sock"})
_HOST_LAUNCH_ENV_ALLOWLIST: Final = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_API_VERSION",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)

_SANDBOX_OUTCOME_FAILURE_CATEGORIES: Mapping[TerminalOutcome, frozenset[FailureCategory]] = MappingProxyType(
    {
        TerminalOutcome.COMPLETED: frozenset(),
        TerminalOutcome.BLOCKED: frozenset({FailureCategory.POLICY}),
        TerminalOutcome.TIMED_OUT: frozenset({FailureCategory.TIMEOUT}),
        TerminalOutcome.CANCELLED: frozenset({FailureCategory.CANCELLED}),
    }
)


def _bounded_text(value: str, name: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _validate_run_id(run_id: str) -> str:
    candidate = run_id.strip()
    if not _RUN_ID_PATTERN.fullmatch(candidate):
        raise ValueError("run_id is not a safe identifier")
    return candidate


def _required_image(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized.startswith("-") or _IMAGE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("image must be a non-empty docker image reference without shell metacharacters")
    return normalized


def _validated_env(env: Mapping[str, str]) -> Mapping[str, str]:
    validated: dict[str, str] = {}
    for key, value in env.items():
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(f"env var name {key!r} is not a valid identifier")
        if not isinstance(value, str) or any(ord(character) < 32 for character in value):
            raise ValueError(f"env var value for {key!r} must be a control-character-free string")
        validated[key] = value
    return MappingProxyType(validated)


def _container_name(run_id: str) -> str:
    return f"{_CONTAINER_NAME_PREFIX}{run_id}"


def _host_launch_env() -> dict[str, str]:
    """The minimal, explicitly allowlisted host environment needed to run the ``docker`` CLI itself.

    Deliberately not the full ambient host environment: only the variables
    docker's own client needs to reach its daemon are forwarded. Variables the
    caller wants injected into the *container* are merged on top of this by
    :class:`AsyncioProcessLauncher` and are only ever surfaced to the container
    via ``-e KEY`` in argv, never written into argv as ``KEY=VALUE``.
    """
    return {key: os.environ[key] for key in _HOST_LAUNCH_ENV_ALLOWLIST if key in os.environ}


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Container network policy: deny-by-default, or bound to a pre-configured restricted network.

    ``allowed_network`` is ``None`` by default, mapping to Docker's
    ``--network=none`` (no network namespace at all - deny-by-default). When
    set, it must name a Docker network that already exists and is configured
    by the operator with whatever destination allowlisting is required (for
    example, an internal bridge network with egress firewall rules); this
    adapter does not create, inspect, or otherwise configure the named
    network, it only attaches the container to it.
    """

    allowed_network: str | None = None

    def __post_init__(self) -> None:
        if self.allowed_network is None:
            return
        normalized = _identifier(self.allowed_network, "allowed_network")
        if normalized == "none":
            raise ValueError("allowed_network must not be the reserved value 'none'")
        object.__setattr__(self, "allowed_network", normalized)

    @property
    def docker_network_argument(self) -> str:
        """The exact ``--network`` value: ``none`` (deny-by-default) or the allowed network name."""
        return self.allowed_network if self.allowed_network is not None else "none"


@dataclass(frozen=True, slots=True)
class SandboxResourceLimits:
    """Finite upper bounds this sandbox must enforce on the container it launches."""

    cpu_limit: float
    memory_limit_bytes: int
    wall_clock_seconds: float
    max_processes: int
    network_policy: NetworkPolicy = field(default_factory=NetworkPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpu_limit", _finite_positive_float(self.cpu_limit, "cpu_limit"))
        object.__setattr__(self, "memory_limit_bytes", _positive_int(self.memory_limit_bytes, "memory_limit_bytes"))
        object.__setattr__(
            self, "wall_clock_seconds", _finite_positive_float(self.wall_clock_seconds, "wall_clock_seconds")
        )
        object.__setattr__(self, "max_processes", _positive_int(self.max_processes, "max_processes"))
        if not isinstance(self.network_policy, NetworkPolicy):
            raise ValueError("network_policy must be a NetworkPolicy")


@dataclass(frozen=True, slots=True)
class CacheMount:
    """One explicit host path bind-mounted into the container alongside the worktree."""

    host_path: str
    container_path: str
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "host_path", _absolute_path(self.host_path, "cache host_path"))
        object.__setattr__(self, "container_path", _absolute_path(self.container_path, "cache container_path"))
        if not isinstance(self.read_only, bool):
            raise ValueError("read_only must be a boolean")


@dataclass(frozen=True, slots=True)
class SandboxRunRequest:
    """Complete, validated input needed to start one sandboxed command execution."""

    run_id: str
    repository: RepositoryContext
    limits: SandboxResourceLimits
    command: tuple[str, ...]
    image: str
    container_mount_path: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=dict)
    cache_mounts: tuple[CacheMount, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _validate_run_id(self.run_id))
        command = tuple(self.command)
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("command must be a non-empty sequence of non-empty strings")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "image", _required_image(self.image))
        object.__setattr__(
            self, "container_mount_path", _absolute_path(self.container_mount_path, "container_mount_path")
        )
        object.__setattr__(self, "env", _validated_env(self.env))
        object.__setattr__(self, "cache_mounts", tuple(self.cache_mounts))


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Structured, captured outcome of one sandboxed command execution."""

    run_id: str
    outcome: TerminalOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: float
    finished_at: float
    duration_seconds: float
    failure: FailureMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TerminalOutcome):
            raise ValueError("outcome must be a TerminalOutcome")
        if self.outcome is TerminalOutcome.COMPLETED and self.failure is not None:
            raise ValueError("completed results must not include a failure")
        if self.outcome is not TerminalOutcome.COMPLETED and self.failure is None:
            raise ValueError("non-success results must include a failure")
        if self.outcome is TerminalOutcome.COMPLETED and self.exit_code is None:
            raise ValueError("completed results must include an exit_code")
        allowed_categories = _SANDBOX_OUTCOME_FAILURE_CATEGORIES[self.outcome]
        if self.failure is not None and self.failure.category not in allowed_categories:
            allowed = ", ".join(sorted(category.value for category in allowed_categories))
            raise ValueError(f"{self.outcome.value} results require one of these failure categories: {allowed}")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")


def _mount_policy_violation(request: SandboxRunRequest) -> FailureMetadata | None:
    """Reject any mount that would expose more than the worktree and its declared caches.

    Checked before the container is ever launched: the worktree mount target
    and every cache's container path must be distinct and outside reserved
    system paths, and no cache's host path may overlap
    ``repository.repository_root`` - the one host path this sandbox must never
    expose, even though it is reachable on the ``RepositoryContext`` passed in.
    """
    container_paths = [request.container_mount_path, *(mount.container_path for mount in request.cache_mounts)]
    if len(container_paths) != len(set(container_paths)):
        return FailureMetadata(
            FailureCategory.POLICY,
            "duplicate_container_mount_path",
            "Two or more mounts target the same path inside the container",
        )
    for container_path in container_paths:
        if container_path in _RESERVED_CONTAINER_PATHS:
            return FailureMetadata(
                FailureCategory.POLICY,
                "mount_targets_reserved_container_path",
                f"Refusing to mount over the reserved container path: {container_path}",
            )
    repository_root = PurePosixPath(request.repository.repository_root)
    for mount in request.cache_mounts:
        host_path = PurePosixPath(mount.host_path)
        if host_path == repository_root or repository_root in host_path.parents or host_path in repository_root.parents:
            return FailureMetadata(
                FailureCategory.POLICY,
                "cache_mount_overlaps_repository_root",
                f"Cache mount host path overlaps the full repository root, not just the isolated worktree: "
                f"{mount.host_path}",
            )
    return None


def build_docker_argv(request: SandboxRunRequest) -> tuple[str, ...]:
    """Construct the ``docker run`` invocation for one sandboxed command execution.

    Assumes ``request`` has already passed :func:`_mount_policy_violation`.
    Call sites (:meth:`DockerSandbox.start`) must reject policy violations
    before ever calling this function, so an unsafe invocation is never built,
    let alone executed.
    """
    limits = request.limits
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        _container_name(request.run_id),
        "--network",
        limits.network_policy.docker_network_argument,
        "--cpus",
        str(limits.cpu_limit),
        "--memory",
        str(limits.memory_limit_bytes),
        "--pids-limit",
        str(limits.max_processes),
        "--read-only",
        "-v",
        f"{request.repository.worktree_root}:{request.container_mount_path}",
    ]
    for mount in request.cache_mounts:
        suffix = ":ro" if mount.read_only else ""
        argv += ["-v", f"{mount.host_path}:{mount.container_path}{suffix}"]
    for key in sorted(request.env):
        argv += ["-e", key]
    argv += ["-w", request.container_mount_path, request.image, *request.command]
    return tuple(argv)


class SubprocessHandle(Protocol):
    """A running ``docker run`` process, abstracted so tests never shell out."""

    async def wait(self) -> int:
        """Wait for process exit and return its exit code."""
        ...

    async def read_stdout(self) -> bytes:
        """Return the full captured stdout once the process has exited."""
        ...

    async def read_stderr(self) -> bytes:
        """Return the full captured stderr once the process has exited."""
        ...

    def kill(self) -> None:
        """Forcefully terminate the container via ``docker kill``. Must be idempotent."""
        ...


class ProcessLauncher(Protocol):
    """Starts the ``docker run`` subprocess for one execution; injectable for tests."""

    async def launch(self, argv: Sequence[str], *, env: Mapping[str, str], container_name: str) -> SubprocessHandle:
        """Start ``argv``, forwarding ``env`` into the container for the named container."""
        ...


class AsyncioSubprocessHandle:
    """Default :class:`SubprocessHandle` backed by ``asyncio.create_subprocess_exec``.

    ``kill`` deliberately calls ``docker kill <container_name>`` - never a
    broad ``pkill`` - because killing the local ``docker run`` client process
    alone does not stop the container the daemon is running; only the daemon
    can stop it, and it must be told to stop exactly this run's container.
    """

    def __init__(self, process: asyncio.subprocess.Process, *, container_name: str) -> None:
        self._process = process
        self._container_name = container_name
        self._killed = False

    async def wait(self) -> int:
        return await self._process.wait()

    async def read_stdout(self) -> bytes:
        if self._process.stdout is None:  # pragma: no cover - always piped by the launcher
            return b""
        return await self._process.stdout.read()

    async def read_stderr(self) -> bytes:
        if self._process.stderr is None:  # pragma: no cover - always piped by the launcher
            return b""
        return await self._process.stderr.read()

    def kill(self) -> None:
        if self._killed:
            return
        self._killed = True
        subprocess.run(["docker", "kill", self._container_name], capture_output=True, check=False)
        if self._process.returncode is None:
            self._process.kill()


class AsyncioProcessLauncher:
    """Default :class:`ProcessLauncher` that runs the real ``docker`` CLI."""

    async def launch(self, argv: Sequence[str], *, env: Mapping[str, str], container_name: str) -> SubprocessHandle:
        launch_env = {**_host_launch_env(), **env}
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=launch_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return AsyncioSubprocessHandle(process, container_name=container_name)


class SandboxSession:
    """One sandboxed command execution: either already blocked, or backed by a running container."""

    def __init__(
        self,
        request: SandboxRunRequest,
        *,
        handle: SubprocessHandle | None,
        started_wall: float,
        started_monotonic: float,
        blocked_failure: FailureMetadata | None = None,
    ) -> None:
        self._request = request
        self._handle = handle
        self._started_wall = started_wall
        self._started_monotonic = started_monotonic
        self._cancel_reason: str | None = None
        self._result: SandboxResult | None = None
        if blocked_failure is not None:
            self._result = SandboxResult(
                run_id=request.run_id,
                outcome=TerminalOutcome.BLOCKED,
                exit_code=None,
                stdout="",
                stderr="",
                started_at=started_wall,
                finished_at=started_wall,
                duration_seconds=0.0,
                failure=blocked_failure,
            )

    @classmethod
    def blocked(cls, request: SandboxRunRequest, failure: FailureMetadata) -> SandboxSession:
        """Build a session that never launched a container because of a policy violation."""
        return cls(
            request,
            handle=None,
            started_wall=time.time(),
            started_monotonic=time.monotonic(),
            blocked_failure=failure,
        )

    @classmethod
    def running(cls, request: SandboxRunRequest, *, handle: SubprocessHandle) -> SandboxSession:
        """Build a session backed by an already-launched container process."""
        return cls(request, handle=handle, started_wall=time.time(), started_monotonic=time.monotonic())

    @property
    def run_id(self) -> str:
        """Return the run identity represented by this session."""
        return self._request.run_id

    # This body never awaits: it records intent and signals the already-running container
    # synchronously. It stays async for interface consistency with ClaudeCodeExecutionSession.cancel
    # (gearmeshing_ai/adapters/claude_code_executor.py), which callers rely on to await it alongside
    # other session operations.
    async def cancel(self, reason: str) -> None:  # NOSONAR
        """Request idempotent cancellation by killing the running container, if any."""
        if self._result is not None:
            return
        normalized = _bounded_text(reason, "cancellation reason", maximum=512)
        if self._cancel_reason is None:
            self._cancel_reason = normalized
        if self._handle is not None:
            self._handle.kill()

    async def result(self) -> SandboxResult:
        """Wait for and return the stable terminal result."""
        if self._result is not None:
            return self._result
        handle = self._handle
        if handle is None:  # pragma: no cover - unreachable: unset only alongside an already-set _result
            raise RuntimeError("session has no running process and no settled result")
        deadline = self._started_monotonic + self._request.limits.wall_clock_seconds
        outcome, exit_code = await self._await_completion(handle, deadline)
        stdout = (await handle.read_stdout()).decode("utf-8", errors="replace")
        stderr = (await handle.read_stderr()).decode("utf-8", errors="replace")
        finished_wall = time.time()
        self._result = SandboxResult(
            run_id=self._request.run_id,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=self._started_wall,
            finished_at=finished_wall,
            duration_seconds=max(finished_wall - self._started_wall, 0.0),
            failure=self._failure_for(outcome),
        )
        return self._result

    def _is_cancelled(self) -> bool:
        """Indirection so mypy does not (incorrectly) narrow ``_cancel_reason`` across awaits.

        ``cancel()`` may run concurrently with ``_await_completion`` while it
        is suspended on ``handle.wait()``, mutating ``self._cancel_reason``
        between the checks below. Reading it through a method call, rather
        than as a bare ``self._cancel_reason`` attribute access, prevents
        mypy's flow-sensitive narrowing from treating the second check as
        statically unreachable.
        """
        return self._cancel_reason is not None

    async def _await_completion(self, handle: SubprocessHandle, deadline: float) -> tuple[TerminalOutcome, int | None]:
        if self._is_cancelled():
            handle.kill()
            return TerminalOutcome.CANCELLED, await handle.wait()
        remaining = deadline - time.monotonic()
        try:
            exit_code = await asyncio.wait_for(handle.wait(), timeout=max(remaining, 0))
        except TimeoutError:
            handle.kill()
            return TerminalOutcome.TIMED_OUT, await handle.wait()
        if self._is_cancelled():
            return TerminalOutcome.CANCELLED, exit_code
        return TerminalOutcome.COMPLETED, exit_code

    def _failure_for(self, outcome: TerminalOutcome) -> FailureMetadata | None:
        if outcome is TerminalOutcome.CANCELLED:
            assert self._cancel_reason is not None
            return FailureMetadata(FailureCategory.CANCELLED, "cancelled_by_caller", self._cancel_reason)
        if outcome is TerminalOutcome.TIMED_OUT:
            return FailureMetadata(
                FailureCategory.TIMEOUT,
                "wall_clock_exceeded",
                "The sandboxed command did not finish within its wall-clock limit",
            )
        return None


class DockerSandbox:
    """Runs one governed command inside an ephemeral, resource-constrained Docker container."""

    def __init__(self, *, launcher: ProcessLauncher | None = None) -> None:
        self._launcher = launcher if launcher is not None else AsyncioProcessLauncher()

    async def start(self, request: SandboxRunRequest) -> SandboxSession:
        """Start one sandboxed command execution, or return an already-blocked session.

        Policy violations (see :func:`_mount_policy_violation`) are detected
        and returned as a ``BLOCKED`` session *before* the launcher - and
        therefore the real ``docker`` subprocess - is ever invoked.
        """
        violation = _mount_policy_violation(request)
        if violation is not None:
            return SandboxSession.blocked(request, violation)
        argv = build_docker_argv(request)
        container_name = _container_name(request.run_id)
        handle = await self._launcher.launch(argv, env=request.env, container_name=container_name)
        return SandboxSession.running(request, handle=handle)
