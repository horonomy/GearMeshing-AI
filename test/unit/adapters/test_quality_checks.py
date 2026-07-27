"""Unit tests for repository-defined quality-check discovery and outcome mapping.

No real Docker is used here: :func:`discover_checks` is pure filesystem logic,
and :func:`run_checks` is exercised against a fake, injectable
``DockerSandbox``/``SandboxSession`` pair so outcome-mapping logic can be
verified deterministically. See
``test/contract/adapters/test_quality_checks_integration.py`` for the real
Docker + real fixture-repo coverage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from gearmeshing_ai.adapters.docker_sandbox import (
    CacheMount,
    DockerSandbox,
    NetworkPolicy,
    SandboxResourceLimits,
    SubprocessHandle,
)
from gearmeshing_ai.adapters.quality_checks import (
    CheckDefinition,
    CheckOutcome,
    CheckResult,
    discover_checks,
    run_checks,
)
from gearmeshing_ai.application.ports.coding_executor import FailureCategory, RepositoryContext

WORKTREE_ROOT = "/workspace/.worktrees/work-run-run-1"
REPOSITORY_ROOT = "/workspace/GearMeshing-AI"


def make_repository() -> RepositoryContext:
    return RepositoryContext(
        repository_root=REPOSITORY_ROOT,
        worktree_root=WORKTREE_ROOT,
        base_ref="main",
        branch="work-run/run-1",
    )


def make_limits(*, wall_clock_seconds: float = 5.0) -> SandboxResourceLimits:
    return SandboxResourceLimits(
        cpu_limit=1.0,
        memory_limit_bytes=512_000_000,
        wall_clock_seconds=wall_clock_seconds,
        max_processes=32,
        network_policy=NetworkPolicy(),
    )


class FakeSubprocessHandle:
    """Deterministic, injectable stand-in for a running ``docker run`` process."""

    def __init__(self, *, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def wait(self) -> int:
        return self._exit_code

    async def read_stdout(self) -> bytes:
        return self._stdout

    async def read_stderr(self) -> bytes:
        return self._stderr

    def kill(self) -> None:
        self.killed = True


class ScriptedProcessLauncher:
    """Returns one pre-built fake handle per launch, in call order; records every argv."""

    def __init__(self, handles: Sequence[SubprocessHandle]) -> None:
        self._handles = list(handles)
        self.launched_argv: list[tuple[str, ...]] = []

    async def launch(self, argv: Sequence[str], *, env: Mapping[str, str], container_name: str) -> SubprocessHandle:
        self.launched_argv.append(tuple(argv))
        return self._handles.pop(0)


# --- discover_checks: explicit GearMeshing config ------------------------------------------


def test_discover_checks_reads_explicit_config_when_present(tmp_path: Path) -> None:
    config_dir = tmp_path / ".gearmeshing"
    config_dir.mkdir()
    (config_dir / "checks.yml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "checks": [
                    {"name": "pytest", "command": ["pytest", "-q"]},
                    {"name": "ruff", "command": ["ruff", "check", "."]},
                ],
            }
        )
    )

    checks = discover_checks(tmp_path)

    assert checks == (
        CheckDefinition(name="pytest", command=("pytest", "-q")),
        CheckDefinition(name="ruff", command=("ruff", "check", ".")),
    )


def test_discover_checks_honors_explicit_config_path_override(tmp_path: Path) -> None:
    config_path = tmp_path / "custom-checks.yml"
    config_path.write_text(yaml.safe_dump({"version": 1, "checks": [{"name": "mypy", "command": ["mypy", "."]}]}))

    checks = discover_checks(tmp_path, config_path=config_path)

    assert checks == (CheckDefinition(name="mypy", command=("mypy", ".")),)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "checks": [{"name": "pytest", "command": ["pytest"]}]},
        {"version": 1, "checks": []},
        {"version": 1, "checks": [{"command": ["pytest"]}]},
        {"version": 1, "checks": [{"name": "pytest"}]},
        {"version": 1, "checks": [{"name": "pytest", "command": "pytest -q"}]},
        {"version": 1, "checks": [{"name": "pytest", "command": []}]},
        {"version": 1, "checks": [{"name": "dup", "command": ["a"]}, {"name": "dup", "command": ["b"]}]},
        "not-a-mapping",
    ],
)
def test_discover_checks_rejects_malformed_explicit_config(tmp_path: Path, payload: object) -> None:
    config_dir = tmp_path / ".gearmeshing"
    config_dir.mkdir()
    (config_dir / "checks.yml").write_text(yaml.safe_dump(payload))

    with pytest.raises(ValueError):
        discover_checks(tmp_path)


def test_discover_checks_rejects_config_that_is_not_valid_yaml(tmp_path: Path) -> None:
    config_dir = tmp_path / ".gearmeshing"
    config_dir.mkdir()
    (config_dir / "checks.yml").write_text("checks: [unterminated")

    with pytest.raises(ValueError):
        discover_checks(tmp_path)


# --- discover_checks: fallback convention detection -----------------------------------------


def test_discover_checks_falls_back_to_pytest_and_ruff_conventions_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n[tool.ruff]\nline-length = 100\n"
    )

    checks = discover_checks(tmp_path)

    by_name = {check.name: check for check in checks}
    assert by_name["pytest"].command == ("pytest", "-q")
    assert by_name["ruff"].command == ("ruff", "check", ".")
    assert by_name["ruff-format"].command == ("ruff", "format", "--check", ".")
    assert by_name["mypy"].command is None


def test_discover_checks_falls_back_to_mypy_ini_convention(tmp_path: Path) -> None:
    (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\n")

    checks = discover_checks(tmp_path)

    by_name = {check.name: check for check in checks}
    assert by_name["mypy"].command == ("mypy", ".")
    assert by_name["pytest"].command is None
    assert by_name["ruff"].command is None


def test_discover_checks_detects_mypy_section_in_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")

    checks = discover_checks(tmp_path)

    by_name = {check.name: check for check in checks}
    assert by_name["mypy"].command == ("mypy", ".")


def test_discover_checks_against_the_real_sample_service_fixture() -> None:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures" / "sample_service"

    checks = discover_checks(fixture_root)

    by_name = {check.name: check for check in checks}
    assert by_name["pytest"].command == ("pytest", "-q")
    assert by_name["ruff"].command == ("ruff", "check", ".")
    assert by_name["ruff-format"].command == ("ruff", "format", "--check", ".")
    assert by_name["mypy"].command is None


# --- discover_checks: no config and no fallback convention -> UNAVAILABLE, never guessed ----


def test_discover_checks_marks_unavailable_when_no_config_and_no_pyproject(tmp_path: Path) -> None:
    checks = discover_checks(tmp_path)

    assert len(checks) == 4
    assert all(check.command is None for check in checks)
    assert {check.name for check in checks} == {"pytest", "ruff", "ruff-format", "mypy"}


def test_discover_checks_marks_unavailable_when_pyproject_has_no_recognized_sections(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'no-tool-sections'\n")

    checks = discover_checks(tmp_path)

    assert all(check.command is None for check in checks)


def test_discover_checks_never_produces_a_command_for_an_undetected_kind(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")

    checks = discover_checks(tmp_path)

    by_name = {check.name: check for check in checks}
    # ruff conventions are discovered, but pytest/mypy were never claimed without evidence.
    assert by_name["pytest"].command is None
    assert by_name["mypy"].command is None


# --- CheckDefinition / CheckResult validation ------------------------------------------------


def test_check_definition_rejects_unsafe_name() -> None:
    with pytest.raises(ValueError, match="check name"):
        CheckDefinition(name="not a valid name!", command=("pytest",))


def test_check_definition_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="command"):
        CheckDefinition(name="pytest", command=())


def test_check_result_rejects_command_on_unavailable_outcome() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        CheckResult(
            check_name="pytest",
            outcome=CheckOutcome.UNAVAILABLE,
            command=("pytest", "-q"),
            exit_code=None,
            stdout="",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_seconds=0.0,
        )


def test_check_result_rejects_missing_command_on_non_unavailable_outcome() -> None:
    with pytest.raises(ValueError, match="requires its resolved command"):
        CheckResult(
            check_name="pytest",
            outcome=CheckOutcome.PASSED,
            command=None,
            exit_code=0,
            stdout="",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_seconds=0.0,
        )


def test_check_result_rejects_passed_outcome_with_nonzero_exit_code() -> None:
    with pytest.raises(ValueError, match="exit_code 0"):
        CheckResult(
            check_name="pytest",
            outcome=CheckOutcome.PASSED,
            command=("pytest",),
            exit_code=1,
            stdout="",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_seconds=0.0,
        )


def test_check_result_rejects_failed_outcome_without_failure_metadata() -> None:
    with pytest.raises(ValueError, match="must include a failure"):
        CheckResult(
            check_name="pytest",
            outcome=CheckOutcome.FAILED,
            command=("pytest",),
            exit_code=1,
            stdout="",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_seconds=0.0,
        )


# --- run_checks: unavailable checks never reach the sandbox ---------------------------------


async def test_run_checks_marks_unavailable_without_starting_a_container() -> None:
    launcher = ScriptedProcessLauncher([])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (CheckDefinition(name="mypy", command=None),)

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
    )

    assert len(results) == 1
    assert results[0].outcome is CheckOutcome.UNAVAILABLE
    assert results[0].command is None
    assert launcher.launched_argv == []


# --- run_checks: outcome mapping from real sandbox results ----------------------------------


async def test_run_checks_maps_zero_exit_code_to_passed() -> None:
    handle = FakeSubprocessHandle(exit_code=0, stdout=b"9 passed\n")
    launcher = ScriptedProcessLauncher([handle])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (CheckDefinition(name="pytest", command=("pytest", "-q")),)

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
    )

    assert results[0].outcome is CheckOutcome.PASSED
    assert results[0].exit_code == 0
    assert results[0].stdout == "9 passed\n"
    assert results[0].failure is None


async def test_run_checks_maps_nonzero_exit_code_to_failed_with_output_attached() -> None:
    handle = FakeSubprocessHandle(exit_code=1, stdout=b"", stderr=b"E   AssertionError\n1 failed\n")
    launcher = ScriptedProcessLauncher([handle])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (CheckDefinition(name="pytest", command=("pytest", "-q")),)

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
    )

    assert results[0].outcome is CheckOutcome.FAILED
    assert results[0].exit_code == 1
    assert "AssertionError" in results[0].stderr
    assert results[0].failure is not None
    assert results[0].failure.category is FailureCategory.TOOL


async def test_run_checks_maps_wall_clock_timeout_to_timed_out() -> None:
    class HangingHandle(FakeSubprocessHandle):
        async def wait(self) -> int:
            if not self.killed:
                await asyncio.sleep(3600)
            return 137

    handle = HangingHandle()
    launcher = ScriptedProcessLauncher([handle])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (CheckDefinition(name="pytest", command=("pytest", "-q")),)

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(wall_clock_seconds=0.05),
        image="gmai-sandbox:latest",
    )

    assert results[0].outcome is CheckOutcome.TIMED_OUT
    assert results[0].failure is not None
    assert results[0].failure.category is FailureCategory.TIMEOUT
    assert handle.killed is True


async def test_run_checks_maps_policy_blocked_sandbox_request_to_failed() -> None:
    launcher = ScriptedProcessLauncher([])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (CheckDefinition(name="pytest", command=("pytest", "-q")),)

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
        cache_mounts=(CacheMount(host_path=REPOSITORY_ROOT, container_path="/cache/repo"),),
    )

    assert results[0].outcome is CheckOutcome.FAILED
    assert results[0].failure is not None
    assert results[0].failure.category is FailureCategory.POLICY
    assert launcher.launched_argv == []


async def test_run_checks_runs_mixed_available_and_unavailable_checks_in_order() -> None:
    handle_pass = FakeSubprocessHandle(exit_code=0, stdout=b"ok\n")
    handle_fail = FakeSubprocessHandle(exit_code=1, stderr=b"boom\n")
    launcher = ScriptedProcessLauncher([handle_pass, handle_fail])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (
        CheckDefinition(name="pytest", command=("pytest", "-q")),
        CheckDefinition(name="mypy", command=None),
        CheckDefinition(name="ruff", command=("ruff", "check", ".")),
    )

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
    )

    assert [result.check_name for result in results] == ["pytest", "mypy", "ruff"]
    assert results[0].outcome is CheckOutcome.PASSED
    assert results[1].outcome is CheckOutcome.UNAVAILABLE
    assert results[2].outcome is CheckOutcome.FAILED
    assert len(launcher.launched_argv) == 2


# --- run_checks: caller-signaled cancellation stops starting new containers -----------------


async def test_run_checks_skips_remaining_checks_once_cancellation_is_already_set() -> None:
    launcher = ScriptedProcessLauncher([])
    sandbox = DockerSandbox(launcher=launcher)
    checks = (
        CheckDefinition(name="pytest", command=("pytest", "-q")),
        CheckDefinition(name="ruff", command=("ruff", "check", ".")),
    )
    cancellation = asyncio.Event()
    cancellation.set()

    results = await run_checks(
        sandbox,
        make_repository(),
        checks,
        limits=make_limits(),
        image="gmai-sandbox:latest",
        cancellation=cancellation,
    )

    assert all(result.outcome is CheckOutcome.SKIPPED for result in results)
    assert launcher.launched_argv == []
    assert all(
        result.failure is not None and result.failure.category is FailureCategory.CANCELLED for result in results
    )
