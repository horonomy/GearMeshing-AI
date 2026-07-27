"""Contract tests exercising real ``docker run`` against the Docker sandbox adapter.

Skipped entirely (via ``pytest.mark.skipif``) when the ``docker`` binary is not
resolvable on ``PATH`` or the daemon is unreachable, following the same
environment-gating convention used elsewhere in this sprint for capabilities
that depend on tooling that may not be present in every execution
environment. See the module docstring in ``docker_sandbox.py`` for the
adapter's own documented scope and assumptions.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from gearmeshing_ai.adapters.docker_sandbox import (
    DockerSandbox,
    NetworkPolicy,
    SandboxResourceLimits,
    SandboxRunRequest,
)
from gearmeshing_ai.application.ports.coding_executor import RepositoryContext, TerminalOutcome

_SANDBOX_IMAGE = "alpine:latest"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "version"], capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker binary/daemon is not available in this environment"
)


@pytest.fixture
def tmp_path(tmp_path: Path) -> Iterator[Path]:  # intentionally shadows pytest's own `tmp_path` fixture
    """A scratch directory under ``$HOME``, not under pytest's default system temp dir.

    This local Docker daemon runs inside a Colima VM that, by default, only
    bind-mounts the host's ``$HOME`` directory (via virtiofs) into the VM -
    not the system temp directory pytest's built-in ``tmp_path`` uses (which
    resolves under ``/private/tmp`` on this host). A ``docker run -v`` mount
    rooted outside ``$HOME`` therefore silently produces an empty directory
    inside the container rather than failing loudly, which would make these
    tests assert against a mount that never actually happened. Overriding the
    fixture name is deliberate so every test below keeps using the standard
    ``tmp_path`` parameter name while transparently getting a host path this
    environment's Docker daemon can actually see.
    """
    home_scratch_root = Path.home() / ".gmai-docker-sandbox-test-scratch"
    home_scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home_scratch_root) as scratch:
        yield Path(scratch)


def make_repository(worktree_root: Path) -> RepositoryContext:
    return RepositoryContext(
        repository_root=str(worktree_root.parent / "repository"),
        worktree_root=str(worktree_root),
        base_ref="main",
        branch="work-run/integration-1",
    )


def make_limits(*, wall_clock_seconds: float = 30.0) -> SandboxResourceLimits:
    return SandboxResourceLimits(
        cpu_limit=1.0,
        memory_limit_bytes=64 * 1024 * 1024,
        wall_clock_seconds=wall_clock_seconds,
        max_processes=32,
        network_policy=NetworkPolicy(),
    )


async def test_real_docker_run_executes_a_command_and_returns_its_output(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "hello.txt").write_text("from the worktree\n")
    sandbox = DockerSandbox()
    request = SandboxRunRequest(
        run_id="integration-1",
        repository=make_repository(worktree),
        limits=make_limits(),
        command=("cat", "/workspace/hello.txt"),
        image=_SANDBOX_IMAGE,
    )

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "from the worktree\n"


async def test_real_container_cannot_read_a_host_path_outside_its_mount(tmp_path: Path) -> None:
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("must not be visible inside the container\n")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sandbox = DockerSandbox()
    request = SandboxRunRequest(
        run_id="integration-2",
        repository=make_repository(worktree),
        limits=make_limits(),
        command=("cat", "/outside-secret.txt"),
        image=_SANDBOX_IMAGE,
    )

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.COMPLETED
    assert result.exit_code != 0
    assert "outside-secret" not in result.stdout


async def test_real_container_running_past_wall_clock_timeout_is_killed(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sandbox = DockerSandbox()
    request = SandboxRunRequest(
        run_id="integration-3",
        repository=make_repository(worktree),
        limits=make_limits(wall_clock_seconds=1.0),
        command=("sleep", "30"),
        image=_SANDBOX_IMAGE,
    )

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.TIMED_OUT
    assert result.duration_seconds < 15.0

    inspected = subprocess.run(
        ["docker", "inspect", "gmai-sandbox-integration-3"],
        capture_output=True,
        check=False,
    )
    assert inspected.returncode != 0


async def test_real_docker_run_denies_network_access_by_default(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    sandbox = DockerSandbox()
    request = SandboxRunRequest(
        run_id="integration-4",
        repository=make_repository(worktree),
        limits=make_limits(),
        command=("sh", "-c", "wget -T 3 -O /dev/null http://example.com || echo unreachable"),
        image=_SANDBOX_IMAGE,
    )

    session = await sandbox.start(request)
    result = await session.result()

    assert result.outcome is TerminalOutcome.COMPLETED
    assert "unreachable" in result.stdout
