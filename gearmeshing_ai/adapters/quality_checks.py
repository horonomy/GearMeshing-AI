"""Discover and run a repository's own declared test/lint/type-check/build commands.

This module answers "what quality checks does this repository define, and what
happened when we ran them" without ever guessing at a command. It is layered on
top of the Docker sandbox adapter (``docker_sandbox.py``, GMAI-23): discovery is
pure and produces a fully-resolved, inspectable list of commands *before* any
execution starts; execution then runs each declared check as one
``SandboxRunRequest`` and maps the resulting ``SandboxResult`` onto a small,
check-specific outcome contract.

DISCOVERY ORDER (config-first, fallback-second, never guessed)
----------------------------------------------------------------
1. **Explicit GearMeshing configuration.** If ``<repository_root>/.gearmeshing/
   checks.yml`` (or an explicit ``config_path`` override) exists, it is the
   sole source of truth: a small, versioned YAML document declaring
   ``version: 1`` and a ``checks`` list of ``{name, command}`` pairs. YAML was
   chosen over TOML because ``pyyaml`` is already a project dependency
   (GMAI-36) and a list-of-mappings schema reads more naturally in YAML than
   in TOML's array-of-tables syntax. Every entry must resolve to a real,
   non-empty command; a malformed document raises ``ValueError`` rather than
   silently discovering nothing.
2. **Fallback convention detection**, only when no explicit config file
   exists. This is a fixed, narrow allowlist of exactly four known check
   kinds - ``pytest``, ``ruff``, ``ruff-format``, ``mypy`` - detected by
   reading the target repository's own ``pyproject.toml``:

   * ``[tool.pytest.ini_options]`` present -> ``pytest`` runs ``pytest -q``.
   * ``[tool.ruff]`` present -> ``ruff`` runs ``ruff check .`` and
     ``ruff-format`` runs ``ruff format --check .``.
   * ``[tool.mypy]`` present, or a ``mypy.ini`` file exists at the repository
     root -> ``mypy`` runs ``mypy .``.

   All four kinds are always considered and always represented in the
   returned tuple. A kind with no discoverable evidence is represented by a
   ``CheckDefinition`` whose ``command`` is ``None`` - never an arbitrary
   guess - so :func:`run_checks` can later mark it ``UNAVAILABLE`` without
   ever attempting to run it.

This module does not wire itself into ``workflow_runner.py`` or into GMAI-28's
future "verify the diff against acceptance criteria" ticket; per this
sprint's contract-first, integration-deferred pattern, that wiring is
deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml

from gearmeshing_ai.adapters.docker_sandbox import (
    CacheMount,
    DockerSandbox,
    SandboxResourceLimits,
    SandboxResult,
    SandboxRunRequest,
)
from gearmeshing_ai.application.ports.coding_executor import (
    FailureCategory,
    FailureMetadata,
    RepositoryContext,
    TerminalOutcome,
)
from gearmeshing_ai.application.ports.evidence import truncated

_CHECK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GEARMESHING_CONFIG_RELATIVE_PATH: Final = Path(".gearmeshing/checks.yml")
_SUPPORTED_CONFIG_VERSION: Final = 1

_FALLBACK_PYTEST_COMMAND: Final[tuple[str, ...]] = ("pytest", "-q")
_FALLBACK_RUFF_CHECK_COMMAND: Final[tuple[str, ...]] = ("ruff", "check", ".")
_FALLBACK_RUFF_FORMAT_COMMAND: Final[tuple[str, ...]] = ("ruff", "format", "--check", ".")
_FALLBACK_MYPY_COMMAND: Final[tuple[str, ...]] = ("mypy", ".")


def _check_name(value: str) -> str:
    normalized = value.strip()
    if not _CHECK_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(f"check name {value!r} must be a short, safe identifier")
    return normalized


def _validated_command(command: object, *, context: str) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)):
        raise ValueError(f"{context} must be a non-empty list of non-empty command strings")
    normalized = tuple(command)
    if not normalized or any(not isinstance(part, str) or not part for part in normalized):
        raise ValueError(f"{context} must be a non-empty list of non-empty command strings")
    return normalized


class CheckOutcome(StrEnum):
    """Finite terminal states reported for one repository-defined quality check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


_CHECK_OUTCOME_FAILURE_CATEGORIES: Mapping[CheckOutcome, frozenset[FailureCategory]] = MappingProxyType(
    {
        CheckOutcome.PASSED: frozenset(),
        CheckOutcome.FAILED: frozenset({FailureCategory.TOOL, FailureCategory.POLICY}),
        CheckOutcome.SKIPPED: frozenset({FailureCategory.CANCELLED}),
        CheckOutcome.TIMED_OUT: frozenset({FailureCategory.TIMEOUT}),
        CheckOutcome.UNAVAILABLE: frozenset(),
    }
)


@dataclass(frozen=True, slots=True)
class CheckDefinition:
    """One check name paired with its fully-resolved command.

    ``command`` is ``None`` only when discovery considered this check kind
    (via the fixed fallback allowlist) but found no evidence a command
    exists - never a placeholder for "not yet decided". Every ``run_checks``
    caller can rely on ``command is None`` meaning "mark UNAVAILABLE, never
    execute".
    """

    name: str
    command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_name(self.name))
        if self.command is not None:
            object.__setattr__(self, "command", _validated_command(self.command, context="command"))


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Structured, captured outcome of one repository-defined quality check."""

    check_name: str
    outcome: CheckOutcome
    command: tuple[str, ...] | None
    exit_code: int | None
    stdout: str
    stdout_truncated: bool
    stderr: str
    stderr_truncated: bool
    duration_seconds: float
    failure: FailureMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_name", _check_name(self.check_name))
        if not isinstance(self.outcome, CheckOutcome):
            raise ValueError("outcome must be a CheckOutcome")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        self._validate_command_and_exit_code()
        self._validate_failure_consistency()

    def _validate_command_and_exit_code(self) -> None:
        if self.outcome is CheckOutcome.UNAVAILABLE:
            if self.command is not None or self.exit_code is not None:
                raise ValueError("an unavailable check must not carry a command or exit_code")
        elif self.command is None:
            raise ValueError(f"a {self.outcome.value} check requires its resolved command")
        else:
            object.__setattr__(self, "command", _validated_command(self.command, context="command"))
        if self.outcome is CheckOutcome.PASSED and self.exit_code != 0:
            raise ValueError("a passed check must have exit_code 0")

    def _validate_failure_consistency(self) -> None:
        requires_failure = self.outcome in {CheckOutcome.FAILED, CheckOutcome.SKIPPED, CheckOutcome.TIMED_OUT}
        has_failure = self.failure is not None
        if requires_failure and not has_failure:
            raise ValueError(f"{self.outcome.value} results must include a failure")
        if not requires_failure and has_failure:
            raise ValueError(f"{self.outcome.value} results must not include a failure")
        if not has_failure:
            return
        failure = self.failure
        assert failure is not None
        allowed = _CHECK_OUTCOME_FAILURE_CATEGORIES[self.outcome]
        if failure.category not in allowed:
            allowed_names = ", ".join(sorted(category.value for category in allowed))
            raise ValueError(f"{self.outcome.value} results require one of these failure categories: {allowed_names}")


def _load_explicit_checks(config_path: Path) -> tuple[CheckDefinition, ...]:
    """Parse an explicit GearMeshing check config; raise ``ValueError`` on any malformed shape."""
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read GearMeshing config at {config_path}: {exc}") from exc
    try:
        payload = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"GearMeshing config at {config_path} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("GearMeshing config must be a mapping at the top level")
    version = payload.get("version")
    if version != _SUPPORTED_CONFIG_VERSION:
        raise ValueError(f"GearMeshing config version must be {_SUPPORTED_CONFIG_VERSION}, got {version!r}")
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("GearMeshing config must declare a non-empty 'checks' list")
    definitions: list[CheckDefinition] = []
    seen_names: set[str] = set()
    for entry in raw_checks:
        if not isinstance(entry, dict):
            raise ValueError("each check entry must be a mapping with 'name' and 'command'")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each check entry requires a non-empty 'name'")
        command = _validated_command(entry.get("command"), context=f"check {name!r} command")
        definition = CheckDefinition(name=name, command=command)
        if definition.name in seen_names:
            raise ValueError(f"duplicate check name {definition.name!r} in GearMeshing config")
        seen_names.add(definition.name)
        definitions.append(definition)
    return tuple(definitions)


def _load_pyproject(repository_root: Path) -> dict[str, object]:
    pyproject_path = repository_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return {}
    try:
        with pyproject_path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded


def _has_tool_section(pyproject: dict[str, object], *keys: str) -> bool:
    node: object = pyproject.get("tool")
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _detect_fallback_checks(repository_root: Path) -> tuple[CheckDefinition, ...]:
    """Fixed, narrow convention detection: exactly four known kinds, never a general guess.

    Each of the four kinds is always returned in the result; a kind with no
    discoverable evidence carries ``command=None`` rather than being omitted,
    so callers can distinguish "considered but not found" from "never
    considered at all".
    """
    pyproject = _load_pyproject(repository_root)
    has_pytest_config = _has_tool_section(pyproject, "pytest", "ini_options")
    has_ruff_config = _has_tool_section(pyproject, "ruff")
    has_mypy_config = _has_tool_section(pyproject, "mypy") or (repository_root / "mypy.ini").is_file()
    return (
        CheckDefinition(name="pytest", command=_FALLBACK_PYTEST_COMMAND if has_pytest_config else None),
        CheckDefinition(name="ruff", command=_FALLBACK_RUFF_CHECK_COMMAND if has_ruff_config else None),
        CheckDefinition(name="ruff-format", command=_FALLBACK_RUFF_FORMAT_COMMAND if has_ruff_config else None),
        CheckDefinition(name="mypy", command=_FALLBACK_MYPY_COMMAND if has_mypy_config else None),
    )


def discover_checks(
    repository_root: str | Path,
    *,
    config_path: str | Path | None = None,
) -> tuple[CheckDefinition, ...]:
    """Deterministically resolve the checks for one repository, before any execution.

    Explicit GearMeshing configuration always wins when present - see
    :func:`_load_explicit_checks`. Only when no explicit config file exists
    does this fall back to the fixed convention detector in
    :func:`_detect_fallback_checks`. The returned tuple is fully resolved and
    inspectable: a caller can log or assert on the exact commands that would
    run without starting any sandbox execution.
    """
    root = Path(repository_root)
    resolved_config_path = Path(config_path) if config_path is not None else root / _GEARMESHING_CONFIG_RELATIVE_PATH
    if resolved_config_path.is_file():
        return _load_explicit_checks(resolved_config_path)
    return _detect_fallback_checks(root)


def _unavailable_result(check: CheckDefinition) -> CheckResult:
    return CheckResult(
        check_name=check.name,
        outcome=CheckOutcome.UNAVAILABLE,
        command=None,
        exit_code=None,
        stdout="",
        stdout_truncated=False,
        stderr="",
        stderr_truncated=False,
        duration_seconds=0.0,
        failure=None,
    )


def _skipped_before_start_result(check: CheckDefinition) -> CheckResult:
    assert check.command is not None
    return CheckResult(
        check_name=check.name,
        outcome=CheckOutcome.SKIPPED,
        command=check.command,
        exit_code=None,
        stdout="",
        stdout_truncated=False,
        stderr="",
        stderr_truncated=False,
        duration_seconds=0.0,
        failure=FailureMetadata(
            FailureCategory.CANCELLED,
            "cancelled_before_start",
            "The quality-check run was cancelled before this check could start",
        ),
    )


def _check_result_from_sandbox_result(check: CheckDefinition, result: SandboxResult) -> CheckResult:
    assert check.command is not None
    stdout, stdout_truncated = truncated(result.stdout)
    stderr, stderr_truncated = truncated(result.stderr)
    outcome, failure = _outcome_and_failure(check, result)
    return CheckResult(
        check_name=check.name,
        outcome=outcome,
        command=check.command,
        exit_code=result.exit_code,
        stdout=stdout,
        stdout_truncated=stdout_truncated,
        stderr=stderr,
        stderr_truncated=stderr_truncated,
        duration_seconds=result.duration_seconds,
        failure=failure,
    )


def _outcome_and_failure(check: CheckDefinition, result: SandboxResult) -> tuple[CheckOutcome, FailureMetadata | None]:
    if result.outcome is TerminalOutcome.COMPLETED:
        if result.exit_code == 0:
            return CheckOutcome.PASSED, None
        failure = FailureMetadata(
            FailureCategory.TOOL,
            "check_command_failed",
            f"{check.name} exited with status {result.exit_code}",
        )
        return CheckOutcome.FAILED, failure
    if result.outcome is TerminalOutcome.TIMED_OUT:
        return CheckOutcome.TIMED_OUT, result.failure
    if result.outcome is TerminalOutcome.CANCELLED:
        return CheckOutcome.SKIPPED, result.failure
    if result.outcome is TerminalOutcome.BLOCKED:
        return CheckOutcome.FAILED, result.failure
    raise AssertionError(  # pragma: no cover - DockerSandbox never produces these outcomes
        f"unexpected sandbox outcome for a quality check: {result.outcome.value}"
    )


async def run_checks(
    sandbox: DockerSandbox,
    repository: RepositoryContext,
    checks: tuple[CheckDefinition, ...],
    *,
    limits: SandboxResourceLimits,
    image: str,
    env: Mapping[str, str] | None = None,
    cache_mounts: tuple[CacheMount, ...] = (),
    run_id_prefix: str = "quality-check",
    cancellation: asyncio.Event | None = None,
) -> tuple[CheckResult, ...]:
    """Run each declared check inside the sandbox, sequentially, and capture its outcome.

    A check whose ``CheckDefinition.command`` is ``None`` is reported
    ``UNAVAILABLE`` immediately, with no sandbox run ever attempted. Each
    remaining check starts its own ``SandboxRunRequest``/``SandboxSession``
    with the given ``limits`` - the wall-clock timeout and cancellation
    handling documented on ``SandboxSession.result``/``SandboxSession.cancel``
    apply unchanged, and this function only translates the resulting
    ``TerminalOutcome`` onto :class:`CheckOutcome` (``TIMED_OUT`` ->
    ``TIMED_OUT``, ``CANCELLED`` -> ``SKIPPED``). If the caller-supplied
    ``cancellation`` event is already set before a given check starts, that
    check (and every check after it) is reported ``SKIPPED`` without ever
    launching a sandbox for it, so a cancelled batch does not keep starting
    new containers.
    """
    results: list[CheckResult] = []
    already_cancelled = False
    for index, check in enumerate(checks):
        if check.command is None:
            results.append(_unavailable_result(check))
            continue
        already_cancelled = already_cancelled or (cancellation is not None and cancellation.is_set())
        if already_cancelled:
            results.append(_skipped_before_start_result(check))
            continue
        request = SandboxRunRequest(
            run_id=f"{run_id_prefix}-{index}-{check.name}",
            repository=repository,
            limits=limits,
            command=check.command,
            image=image,
            env=env if env is not None else {},
            cache_mounts=cache_mounts,
        )
        session = await sandbox.start(request)
        sandbox_result = await session.result()
        results.append(_check_result_from_sandbox_result(check, sandbox_result))
    return tuple(results)
