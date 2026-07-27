"""Contract tests running real repository-defined quality checks against real Docker.

Skipped entirely (via ``pytest.mark.skipif``) when the ``docker`` binary is not
resolvable on ``PATH`` or the daemon is unreachable - the same environment
gating convention used by ``test_docker_sandbox_integration.py`` (GMAI-23).
These tests exercise the real ``fixtures/sample_service`` fixture repository
(GMAI-38) through the real fallback-convention detector and a real, disposable
Docker image containing ``pytest`` and ``ruff``, confirming that a fully
passing check set and a deliberately mixed pass/fail scenario both produce
the correct, evidence-attached outcomes end to end.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from gearmeshing_ai.adapters.docker_sandbox import DockerSandbox, NetworkPolicy, SandboxResourceLimits
from gearmeshing_ai.adapters.quality_checks import CheckOutcome, discover_checks, run_checks
from gearmeshing_ai.application.ports.coding_executor import RepositoryContext

_FIXTURE_IMAGE = "gmai-quality-checks-fixture:test"
_FIXTURE_REPOSITORY_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "sample_service"


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

    See the identically-named fixture override in ``test_docker_sandbox_integration.py``
    for the full rationale: this environment's local Docker daemon (Colima) only
    bind-mounts ``$HOME`` into its VM, so a mount rooted outside ``$HOME`` would
    silently produce an empty directory inside the container.
    """
    home_scratch_root = Path.home() / ".gmai-docker-sandbox-test-scratch"
    home_scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home_scratch_root) as scratch:
        yield Path(scratch)


@pytest.fixture(scope="module")
def fixture_image() -> Iterator[str]:
    """Build a small, disposable image with ``pytest``/``ruff`` pinned to this repo's lockfile.

    Built once per test module rather than baked into the repository's own
    ``Dockerfile``, since the sandbox image used to run a *target* repository's
    checks is necessarily specific to that target's toolchain - here, the
    synthetic fixture repository's own declared ``pytest``/``ruff`` versions,
    not `gearmeshing-ai`'s own.
    """
    with tempfile.TemporaryDirectory() as build_context:
        dockerfile = Path(build_context) / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.13-slim\n"
            "RUN pip install --no-cache-dir --root-user-action=ignore pytest==8.4.2 ruff==0.15.22\n"
        )
        subprocess.run(
            ["docker", "build", "-q", "-t", _FIXTURE_IMAGE, str(build_context)],
            capture_output=True,
            check=True,
        )
    yield _FIXTURE_IMAGE


def make_repository(worktree_root: Path) -> RepositoryContext:
    return RepositoryContext(
        repository_root=str(worktree_root.parent / "repository"),
        worktree_root=str(worktree_root),
        base_ref="main",
        branch="work-run/quality-checks-integration-1",
    )


def make_limits(*, wall_clock_seconds: float = 30.0) -> SandboxResourceLimits:
    return SandboxResourceLimits(
        cpu_limit=1.0,
        memory_limit_bytes=256 * 1024 * 1024,
        wall_clock_seconds=wall_clock_seconds,
        max_processes=32,
        network_policy=NetworkPolicy(),
    )


def _copy_fixture_repo(destination: Path) -> None:
    shutil.copytree(_FIXTURE_REPOSITORY_ROOT, destination)


async def test_discovered_fallback_checks_all_pass_against_the_real_fixture_repo(
    tmp_path: Path, fixture_image: str
) -> None:
    worktree = tmp_path / "worktree"
    _copy_fixture_repo(worktree)
    checks = discover_checks(worktree)
    available_checks = tuple(check for check in checks if check.command is not None)
    assert {check.name for check in available_checks} == {"pytest", "ruff", "ruff-format"}

    sandbox = DockerSandbox()
    results = await run_checks(
        sandbox,
        make_repository(worktree),
        checks,
        limits=make_limits(),
        image=fixture_image,
        run_id_prefix="all-pass",
    )

    by_name = {result.check_name: result for result in results}
    assert by_name["pytest"].outcome is CheckOutcome.PASSED
    assert by_name["ruff"].outcome is CheckOutcome.PASSED
    assert by_name["ruff-format"].outcome is CheckOutcome.PASSED
    assert by_name["mypy"].outcome is CheckOutcome.UNAVAILABLE
    assert by_name["pytest"].exit_code == 0
    assert "passed" in by_name["pytest"].stdout


async def test_mixed_pass_fail_check_set_captures_failure_output_on_the_failing_check(
    tmp_path: Path, fixture_image: str
) -> None:
    worktree = tmp_path / "worktree"
    _copy_fixture_repo(worktree)
    # Deliberately break one test's expectation in the throwaway copy only - the committed
    # fixture under fixtures/sample_service is never mutated - so pytest fails while ruff's
    # lint/format checks (which do not depend on test assertions) still pass.
    pricing_test = worktree / "tests" / "test_pricing.py"
    original = pricing_test.read_text()
    broken = original.replace(
        "assert total_price_cents(500, 3) == 1500",
        "assert total_price_cents(500, 3) == 999999",
    )
    assert broken != original
    pricing_test.write_text(broken)
    checks = discover_checks(worktree)

    sandbox = DockerSandbox()
    results = await run_checks(
        sandbox,
        make_repository(worktree),
        checks,
        limits=make_limits(),
        image=fixture_image,
        run_id_prefix="mixed",
    )

    by_name = {result.check_name: result for result in results}
    assert by_name["pytest"].outcome is CheckOutcome.FAILED
    assert by_name["pytest"].exit_code != 0
    assert "999999" in by_name["pytest"].stdout or "999999" in by_name["pytest"].stderr
    assert by_name["pytest"].failure is not None
    assert by_name["ruff"].outcome is CheckOutcome.PASSED
    assert by_name["ruff-format"].outcome is CheckOutcome.PASSED
