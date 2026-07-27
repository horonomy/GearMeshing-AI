"""Unit tests for the Docker sandbox adapter (pure logic, no real Docker)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from gearmeshing_ai.adapters.docker_sandbox import (
    CacheMount,
    DockerSandbox,
    NetworkPolicy,
    SandboxResourceLimits,
    SandboxRunRequest,
    SubprocessHandle,
    build_docker_argv,
)
from gearmeshing_ai.application.ports.coding_executor import FailureCategory, RepositoryContext, TerminalOutcome

WORKTREE_ROOT = "/workspace/.worktrees/work-run-run-1"
REPOSITORY_ROOT = "/workspace/GearMeshing-AI"


def make_repository(*, worktree_root: str = WORKTREE_ROOT) -> RepositoryContext:
    return RepositoryContext(
        repository_root=REPOSITORY_ROOT,
        worktree_root=worktree_root,
        base_ref="main",
        branch="work-run/run-1",
    )


def make_limits(
    *,
    cpu_limit: float = 1.0,
    memory_limit_bytes: int = 512_000_000,
    wall_clock_seconds: float = 5.0,
    max_processes: int = 32,
    network_policy: NetworkPolicy | None = None,
) -> SandboxResourceLimits:
    return SandboxResourceLimits(
        cpu_limit=cpu_limit,
        memory_limit_bytes=memory_limit_bytes,
        wall_clock_seconds=wall_clock_seconds,
        max_processes=max_processes,
        network_policy=network_policy if network_policy is not None else NetworkPolicy(),
    )


def make_request(
    *,
    run_id: str = "run-1",
    repository: RepositoryContext | None = None,
    limits: SandboxResourceLimits | None = None,
    command: tuple[str, ...] = ("pytest", "-q"),
    image: str = "gmai-sandbox:latest",
    env: Mapping[str, str] | None = None,
    cache_mounts: tuple[CacheMount, ...] = (),
) -> SandboxRunRequest:
    return SandboxRunRequest(
        run_id=run_id,
        repository=repository if repository is not None else make_repository(),
        limits=limits if limits is not None else make_limits(),
        command=command,
        image=image,
        env=env if env is not None else {},
        cache_mounts=cache_mounts,
    )


class FakeSubprocessHandle:
    """Deterministic, injectable stand-in for a running ``docker run`` process."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self._wait_event = asyncio.Event()
        if not hang:
            self._wait_event.set()

    async def wait(self) -> int:
        if self._hang and not self.killed:
            await asyncio.sleep(3600)
        await self._wait_event.wait()
        return self._exit_code

    async def read_stdout(self) -> bytes:
        return self._stdout

    async def read_stderr(self) -> bytes:
        return self._stderr

    def kill(self) -> None:
        self.killed = True
        self._wait_event.set()


class FakeProcessLauncher:
    """Records the launch invocation and returns a pre-built fake handle."""

    def __init__(self, handle: SubprocessHandle) -> None:
        self.handle = handle
        self.launched_argv: tuple[str, ...] | None = None
        self.launched_env: Mapping[str, str] | None = None
        self.launched_container_name: str | None = None

    async def launch(self, argv: Sequence[str], *, env: Mapping[str, str], container_name: str) -> SubprocessHandle:
        self.launched_argv = tuple(argv)
        self.launched_env = env
        self.launched_container_name = container_name
        return self.handle


# --- docker run argv construction -----------------------------------------------------------


def test_argv_mounts_only_the_worktree_root_read_write() -> None:
    request = make_request()

    argv = build_docker_argv(request)

    assert "-v" in argv
    mount_index = argv.index("-v")
    assert argv[mount_index + 1] == f"{WORKTREE_ROOT}:/workspace"
    assert REPOSITORY_ROOT not in argv


def test_argv_defaults_to_network_none() -> None:
    request = make_request()

    argv = build_docker_argv(request)

    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"


def test_argv_uses_explicit_allowed_network_when_configured() -> None:
    request = make_request(limits=make_limits(network_policy=NetworkPolicy(allowed_network="restricted-egress")))

    argv = build_docker_argv(request)

    assert argv[argv.index("--network") + 1] == "restricted-egress"


def test_argv_applies_cpu_memory_and_pids_limits() -> None:
    request = make_request(limits=make_limits(cpu_limit=2.5, memory_limit_bytes=256_000_000, max_processes=64))

    argv = build_docker_argv(request)

    assert argv[argv.index("--cpus") + 1] == "2.5"
    assert argv[argv.index("--memory") + 1] == "256000000"
    assert argv[argv.index("--pids-limit") + 1] == "64"


def test_argv_uses_read_only_root_filesystem() -> None:
    request = make_request()

    argv = build_docker_argv(request)

    assert "--read-only" in argv


def test_argv_includes_declared_cache_mounts() -> None:
    request = make_request(
        cache_mounts=(CacheMount(host_path="/var/cache/gmai/uv", container_path="/cache/uv", read_only=True),)
    )

    argv = build_docker_argv(request)

    assert "/var/cache/gmai/uv:/cache/uv:ro" in argv


def test_argv_injects_env_var_names_only_never_values_in_argv() -> None:
    request = make_request(env={"API_TOKEN": "super-secret-value"})

    argv = build_docker_argv(request)

    assert "-e" in argv
    assert argv[argv.index("-e") + 1] == "API_TOKEN"
    assert not any("super-secret-value" in part for part in argv)


def test_argv_names_the_container_deterministically_from_run_id() -> None:
    request = make_request(run_id="run-42")

    argv = build_docker_argv(request)

    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "gmai-sandbox-run-42"


def test_argv_runs_the_requested_command_in_the_requested_image() -> None:
    request = make_request(command=("ruff", "check", "."), image="gmai-sandbox:latest")

    argv = build_docker_argv(request)

    assert argv[-4:] == ("gmai-sandbox:latest", "ruff", "check", ".")


# --- mount / policy rejection, checked before subprocess invocation ------------------------


async def test_cache_mount_overlapping_repository_root_is_rejected_as_policy_before_launch() -> None:
    handle = FakeSubprocessHandle()
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(cache_mounts=(CacheMount(host_path=REPOSITORY_ROOT, container_path="/cache/repo"),))

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.POLICY
    assert result.failure.code == "cache_mount_overlaps_repository_root"
    assert launcher.launched_argv is None


async def test_cache_mount_inside_repository_root_is_rejected_as_policy_before_launch() -> None:
    handle = FakeSubprocessHandle()
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(
        cache_mounts=(CacheMount(host_path=f"{REPOSITORY_ROOT}/.cache", container_path="/cache/repo"),)
    )

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.POLICY
    assert launcher.launched_argv is None


async def test_duplicate_container_mount_paths_are_rejected_as_policy_before_launch() -> None:
    handle = FakeSubprocessHandle()
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(cache_mounts=(CacheMount(host_path="/var/cache/gmai/uv", container_path="/workspace"),))

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.code == "duplicate_container_mount_path"
    assert launcher.launched_argv is None


async def test_mount_over_reserved_container_path_is_rejected_as_policy_before_launch() -> None:
    handle = FakeSubprocessHandle()
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(cache_mounts=(CacheMount(host_path="/var/cache/gmai/etc", container_path="/etc"),))

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED
    assert result.failure is not None
    assert result.failure.code == "mount_targets_reserved_container_path"
    assert launcher.launched_argv is None


async def test_valid_request_is_not_blocked_and_reaches_the_launcher() -> None:
    handle = FakeSubprocessHandle(exit_code=0, stdout=b"ok\n")
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request()

    session = await sandbox.start(request)
    result = await session.result()

    assert launcher.launched_argv is not None
    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.failure is None


# --- validation of the request/limits dataclasses themselves -------------------------------


@pytest.mark.parametrize("cpu_limit", [0, -1.0, float("inf"), float("nan")])
def test_resource_limits_rejects_non_finite_or_non_positive_cpu_limit(cpu_limit: float) -> None:
    with pytest.raises(ValueError, match="cpu_limit"):
        make_limits(cpu_limit=cpu_limit)


@pytest.mark.parametrize("memory_limit_bytes", [0, -1])
def test_resource_limits_rejects_non_positive_memory_limit(memory_limit_bytes: int) -> None:
    with pytest.raises(ValueError, match="memory_limit_bytes"):
        make_limits(memory_limit_bytes=memory_limit_bytes)


@pytest.mark.parametrize("max_processes", [0, -5])
def test_resource_limits_rejects_non_positive_max_processes(max_processes: int) -> None:
    with pytest.raises(ValueError, match="max_processes"):
        make_limits(max_processes=max_processes)


def test_network_policy_rejects_reserved_none_as_an_explicit_allowed_network() -> None:
    with pytest.raises(ValueError, match="reserved value"):
        NetworkPolicy(allowed_network="none")


def test_run_request_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="command"):
        make_request(command=())


def test_run_request_rejects_unsafe_image_reference() -> None:
    with pytest.raises(ValueError, match="image"):
        make_request(image="--privileged")


def test_run_request_rejects_invalid_env_var_name() -> None:
    with pytest.raises(ValueError, match="env var name"):
        make_request(env={"not a valid name": "value"})


def test_run_request_rejects_relative_cache_host_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        make_request(cache_mounts=(CacheMount(host_path="relative/cache", container_path="/cache"),))


def test_run_request_rejects_unsafe_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        make_request(run_id="../escape")


# --- wall-clock timeout enforcement (actively killed, not left to docker) -------------------


async def test_wall_clock_timeout_kills_the_container_and_settles_timed_out() -> None:
    handle = FakeSubprocessHandle(hang=True)
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(limits=make_limits(wall_clock_seconds=0.05))

    session = await sandbox.start(request)
    result = await session.result()

    assert handle.killed is True
    assert result.outcome is TerminalOutcome.TIMED_OUT
    assert result.failure is not None
    assert result.failure.category is FailureCategory.TIMEOUT


# --- cancellation kills the specific container, not a broad pkill ---------------------------


async def test_cancel_kills_the_running_container_and_settles_cancelled() -> None:
    handle = FakeSubprocessHandle(hang=True)
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request()

    session = await sandbox.start(request)
    await session.cancel("Human authority checkpoint")
    result = await session.result()

    assert handle.killed is True
    assert result.outcome is TerminalOutcome.CANCELLED
    assert result.failure is not None
    assert result.failure.category is FailureCategory.CANCELLED
    assert result.failure.message == "Human authority checkpoint"


async def test_cancel_is_idempotent_and_preserves_the_first_reason() -> None:
    handle = FakeSubprocessHandle(hang=True)
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request()

    session = await sandbox.start(request)
    await session.cancel("First reason")
    await session.cancel("A later reason must not replace the first")
    result = await session.result()

    assert result.failure is not None
    assert result.failure.message == "First reason"


async def test_cancel_after_terminal_result_is_a_no_op() -> None:
    handle = FakeSubprocessHandle(exit_code=0)
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request()

    session = await sandbox.start(request)
    before_cancel = await session.result()
    await session.cancel("Too late to alter the result")

    assert await session.result() is before_cancel


async def test_cancel_on_a_blocked_session_is_a_no_op() -> None:
    handle = FakeSubprocessHandle()
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request(cache_mounts=(CacheMount(host_path=REPOSITORY_ROOT, container_path="/cache/repo"),))

    session = await sandbox.start(request)
    await session.cancel("Attempting to cancel an already-blocked session")
    result = await session.result()

    assert result.outcome is TerminalOutcome.BLOCKED


# --- captured timing --------------------------------------------------------------------------


async def test_result_captures_non_negative_duration() -> None:
    handle = FakeSubprocessHandle(exit_code=0)
    launcher = FakeProcessLauncher(handle)
    sandbox = DockerSandbox(launcher=launcher)
    request = make_request()

    session = await sandbox.start(request)
    result = await session.result()

    assert result.duration_seconds >= 0
    assert result.finished_at >= result.started_at
