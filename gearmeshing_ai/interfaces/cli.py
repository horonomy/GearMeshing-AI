"""Typer CLI entry point for the GearMeshing-AI proof of concept.

Provides operator controls for POC work runs (``run``, ``status``,
``cancel``, ``retry``, ``doctor``) without requiring a dedicated UI or REST
application. Every command supports ``--json`` for machine-readable output
alongside its default human-readable text.

Work runs are persisted with :class:`JsonFileCheckpointStore`, a minimal
local JSON-file checkpoint backend scoped to this CLI ticket (GMAI-14).
There is currently no production ``CodingExecutor``/verifier/publisher
wiring on ``main`` (that is owned by other tickets), so this CLI only
manages the governed lifecycle up to and including the initial
``approved``/``blocked`` checkpoint plus operator-triggered ``cancel`` and
``retry`` transitions; it does not itself drive execution, verification, or
publication.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer

from gearmeshing_ai import __version__
from gearmeshing_ai.adapters.jira_errors import JiraAdapterError
from gearmeshing_ai.adapters.jira_work_management import JiraConfiguration, JiraWorkManagementProvider
from gearmeshing_ai.application.ports.work_management import RepositoryReference
from gearmeshing_ai.domain.work_run import (
    InvalidTransitionError,
    WorkRun,
    WorkRunCorrelation,
    WorkRunState,
    WorkRunValidationError,
)
from gearmeshing_ai.runtime.checkpoint_store import (
    CheckpointStoreError,
    JsonFileCheckpointStore,
    serialize_work_run,
)

app = typer.Typer(name="gmai", help="Governed autonomous engineering teams powered by Agent Assembly.")

_DEFAULT_CHECKPOINTS_DIR = Path.home() / ".gearmeshing-ai" / "checkpoints"
_DEFAULT_ACTOR_ID = "gmai-cli-operator"
_REVISION_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


class CliConfigurationError(RuntimeError):
    """Raised when required CLI configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class JiraCliCredentials:
    """Jira connection settings resolved from environment variables.

    ``api_token`` is intentionally excluded from ``repr`` output, matching
    the credential-safe pattern used by :class:`JiraConfiguration`.
    """

    site_url: str
    email: str
    api_token: str
    project_key: str
    repository: RepositoryReference

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction, not exercised by assertions
        return f"JiraCliCredentials(site_url={self.site_url!r}, project_key={self.project_key!r})"


_JIRA_ENV_VARS = (
    "GMAI_JIRA_SITE_URL",
    "GMAI_JIRA_EMAIL",
    "GMAI_JIRA_API_TOKEN",
    "GMAI_JIRA_PROJECT_KEY",
)
_REPOSITORY_ENV_VARS = (
    "GMAI_REPOSITORY_OWNER",
    "GMAI_REPOSITORY_NAME",
    "GMAI_REPOSITORY_URL",
)


def _missing_env_vars(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if not os.environ.get(name, "").strip())


def _load_jira_credentials() -> JiraCliCredentials:
    """Resolve Jira connection settings from environment variables.

    Required: ``GMAI_JIRA_SITE_URL``, ``GMAI_JIRA_EMAIL``,
    ``GMAI_JIRA_API_TOKEN``, ``GMAI_JIRA_PROJECT_KEY``, ``GMAI_REPOSITORY_OWNER``,
    ``GMAI_REPOSITORY_NAME``, ``GMAI_REPOSITORY_URL``. ``GMAI_REPOSITORY_PROVIDER``
    is optional and defaults to ``"github"``.
    """
    missing = _missing_env_vars(_JIRA_ENV_VARS) + _missing_env_vars(_REPOSITORY_ENV_VARS)
    if missing:
        raise CliConfigurationError("missing required environment variables: " + ", ".join(missing))
    repository = RepositoryReference(
        provider=os.environ.get("GMAI_REPOSITORY_PROVIDER", "github").strip() or "github",
        owner=os.environ["GMAI_REPOSITORY_OWNER"],
        name=os.environ["GMAI_REPOSITORY_NAME"],
        web_url=os.environ["GMAI_REPOSITORY_URL"],
    )
    return JiraCliCredentials(
        site_url=os.environ["GMAI_JIRA_SITE_URL"],
        email=os.environ["GMAI_JIRA_EMAIL"],
        api_token=os.environ["GMAI_JIRA_API_TOKEN"],
        project_key=os.environ["GMAI_JIRA_PROJECT_KEY"],
        repository=repository,
    )


def _jira_configuration(credentials: JiraCliCredentials) -> JiraConfiguration:
    return JiraConfiguration(
        site_url=credentials.site_url,
        email=credentials.email,
        api_token=credentials.api_token,
        project_key=credentials.project_key,
        repository=credentials.repository,
    )


def _sanitize_revision(revision: str) -> str:
    """Translate a provider revision marker into a domain-safe identifier.

    ``WorkRunCorrelation.jira_issue_revision`` only accepts
    ``[A-Za-z0-9._/-]``, but provider revision markers (for example a Jira
    ISO-8601 ``updated`` timestamp) commonly contain ``:`` and ``+``. The
    substitution below is deterministic and applied only when needed, so
    revisions that already fit the safe charset (as used throughout the
    existing test suite) pass through unchanged.
    """
    if _REVISION_SAFE_PATTERN.fullmatch(revision):
        return revision
    sanitized = revision.replace(":", ".").replace("+", "-")
    sanitized = re.sub(r"[^A-Za-z0-9._/-]", "-", sanitized)
    if not sanitized or not sanitized[0].isalnum():
        sanitized = f"r-{sanitized}"
    return sanitized[:255]


_CHECKPOINTS_DIR_OPTION = typer.Option(
    _DEFAULT_CHECKPOINTS_DIR,
    "--checkpoints-dir",
    envvar="GMAI_CHECKPOINTS_DIR",
    help="Directory holding local JSON work-run checkpoints.",
)
_JSON_OPTION = typer.Option(False, "--json", help="Emit machine-readable JSON instead of human-readable text.")
_ACTOR_ID_OPTION = typer.Option(
    _DEFAULT_ACTOR_ID,
    "--actor-id",
    envvar="GMAI_ACTOR_ID",
    help="Identity recorded against the workflow event this command appends.",
)


def _open_checkpoint_store(checkpoints_dir: Path, *, json_output: bool) -> JsonFileCheckpointStore:
    try:
        return JsonFileCheckpointStore(checkpoints_dir)
    except CheckpointStoreError as error:
        _fail(str(error), json_output=json_output)


def _emit(payload: Mapping[str, Any], human_lines: tuple[str, ...], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in human_lines:
            typer.echo(line)


def _fail(message: str, *, json_output: bool, command: str | None = None) -> NoReturn:
    if json_output:
        payload: dict[str, Any] = {"status": "error", "error": message}
        if command is not None:
            payload["command"] = command
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _resolve_run(store: JsonFileCheckpointStore, identifier: str, *, json_output: bool, command: str) -> WorkRun:
    normalized = identifier.strip().upper()
    if not normalized:
        _fail("run identifier must not be blank", json_output=json_output, command=command)
    try:
        run = store.load(normalized)
    except CheckpointStoreError as error:
        _fail(str(error), json_output=json_output, command=command)
    if run is None:
        _fail(f"no work run found for identifier {identifier!r}", json_output=json_output, command=command)
    return run


def _status_summary(run: WorkRun) -> dict[str, Any]:
    summary = serialize_work_run(run)
    summary["latest_event"] = summary["events"][-1]["name"] if summary["events"] else None
    return summary


def _human_status_lines(run: WorkRun) -> tuple[str, ...]:
    lines = [
        f"run_id: {run.run_id}",
        f"jira_issue_key: {run.correlation.jira_issue_key}",
        f"state: {run.state.value}",
        f"events: {len(run.events)}",
        f"artifacts: {len(run.artifacts)}",
    ]
    if run.draft_pr_url is not None:
        lines.append(f"draft_pr_url: {run.draft_pr_url}")
    latest = run.events[-1]
    lines.append(f"latest_event: {latest.name} ({latest.occurred_at.isoformat()})")
    return tuple(lines)


@app.callback()
def main() -> None:
    """Governed autonomous engineering teams powered by Agent Assembly."""


@app.command()
def version() -> None:
    """Print the installed GearMeshing-AI version."""
    typer.echo(__version__)


@app.command()
def run(
    jira_issue_key: str = typer.Argument(..., help="Jira issue key to start a governed work run for, e.g. GMAI-14."),
    checkpoints_dir: Path = _CHECKPOINTS_DIR_OPTION,
    actor_id: str = _ACTOR_ID_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Start a governed work run for a Jira issue, or report its existing checkpoint.

    Requires Jira credentials via environment variables: ``GMAI_JIRA_SITE_URL``,
    ``GMAI_JIRA_EMAIL``, ``GMAI_JIRA_API_TOKEN``, ``GMAI_JIRA_PROJECT_KEY``,
    ``GMAI_REPOSITORY_OWNER``, ``GMAI_REPOSITORY_NAME``, ``GMAI_REPOSITORY_URL``
    (``GMAI_REPOSITORY_PROVIDER`` optionally, defaults to ``github``).
    """
    normalized_key = jira_issue_key.strip().upper()
    store = _open_checkpoint_store(checkpoints_dir, json_output=json_output)

    existing = store.load(normalized_key)
    if existing is not None:
        _emit(
            {"status": "ok", "command": "run", "already_started": True, **_status_summary(existing)},
            (f"Work run {existing.run_id!r} already exists.", *_human_status_lines(existing)),
            json_output=json_output,
        )
        return

    try:
        credentials = _load_jira_credentials()
    except CliConfigurationError as error:
        _fail(str(error), json_output=json_output, command="run")

    try:
        approved_or_blocked = asyncio.run(_start_run(normalized_key, credentials, actor_id=actor_id))
    except WorkRunValidationError as error:
        _fail(
            f"work item cannot be represented as a governed work run: {error}", json_output=json_output, command="run"
        )
    except (ValueError, JiraAdapterError, httpx.HTTPError) as error:
        _fail(f"unable to start work run: {error}", json_output=json_output, command="run")

    store.save(expected=None, updated=approved_or_blocked)
    _emit(
        {"status": "ok", "command": "run", "already_started": False, **_status_summary(approved_or_blocked)},
        (f"Work run {approved_or_blocked.run_id!r} started.", *_human_status_lines(approved_or_blocked)),
        json_output=json_output,
    )


async def _start_run(run_id: str, credentials: JiraCliCredentials, *, actor_id: str) -> WorkRun:
    configuration = _jira_configuration(credentials)
    async with JiraWorkManagementProvider(configuration) as provider:
        work_item = await provider.get_work_item(run_id)
        readiness = await provider.evaluate_readiness(work_item)

    now = datetime.now(UTC)
    correlation = WorkRunCorrelation(
        jira_issue_key=work_item.key,
        jira_issue_url=work_item.web_url,
        jira_issue_revision=_sanitize_revision(work_item.revision),
        jira_issue_content_sha256=work_item.content_sha256,
        repository_url=credentials.repository.web_url,
        branch_name=f"{run_id.lower()}/cli-run",
        agent_assembly_run_id=f"cli-{run_id.lower()}-{uuid.uuid4().hex[:12]}",
    )
    approved = WorkRun.approve(run_id=run_id, correlation=correlation, actor_id=actor_id, occurred_at=now)
    if readiness.ready:
        return approved
    summary = "; ".join(problem.summary for problem in readiness.problems)
    return approved.transition_to(
        WorkRunState.BLOCKED,
        actor_id=actor_id,
        occurred_at=now,
        details=(("failure_code", "work_item_not_ready"), ("summary", summary)),
    )


@app.command()
def status(
    identifier: str = typer.Argument(..., help="Run ID or Jira issue key."),
    checkpoints_dir: Path = _CHECKPOINTS_DIR_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Report the persisted state of a work run."""
    store = _open_checkpoint_store(checkpoints_dir, json_output=json_output)
    run_value = _resolve_run(store, identifier, json_output=json_output, command="status")
    _emit(
        {"status": "ok", "command": "status", **_status_summary(run_value)},
        _human_status_lines(run_value),
        json_output=json_output,
    )


@app.command()
def cancel(
    run_id: str = typer.Argument(..., help="Run ID (or Jira issue key) to cancel."),
    checkpoints_dir: Path = _CHECKPOINTS_DIR_OPTION,
    actor_id: str = _ACTOR_ID_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Cancel a work run that has not yet reached a terminal state."""
    store = _open_checkpoint_store(checkpoints_dir, json_output=json_output)
    current = _resolve_run(store, run_id, json_output=json_output, command="cancel")
    try:
        cancelled = current.transition_to(WorkRunState.CANCELLED, actor_id=actor_id, occurred_at=datetime.now(UTC))
    except InvalidTransitionError as error:
        _fail(f"cannot cancel work run {current.run_id!r}: {error}", json_output=json_output, command="cancel")
    store.save(expected=current, updated=cancelled)
    _emit(
        {"status": "ok", "command": "cancel", **_status_summary(cancelled)},
        (f"Work run {cancelled.run_id!r} cancelled.", *_human_status_lines(cancelled)),
        json_output=json_output,
    )


@app.command()
def retry(
    run_id: str = typer.Argument(..., help="Run ID (or Jira issue key) to retry."),
    checkpoints_dir: Path = _CHECKPOINTS_DIR_OPTION,
    actor_id: str = _ACTOR_ID_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Signal that a remediated work run is ready for re-verification.

    The domain model permits no outgoing transition from any terminal state
    (``completed``, ``failed``, ``blocked``, ``cancelled``); those runs
    cannot be retried and must be re-started as a new run instead. The one
    domain-supported manual "retry" signal is the documented
    ``remediating -> verifying`` transition ("correction ready"), so that is
    the only transition this command performs.
    """
    store = _open_checkpoint_store(checkpoints_dir, json_output=json_output)
    current = _resolve_run(store, run_id, json_output=json_output, command="retry")
    try:
        retried = current.transition_to(WorkRunState.VERIFYING, actor_id=actor_id, occurred_at=datetime.now(UTC))
    except InvalidTransitionError as error:
        _fail(f"cannot retry work run {current.run_id!r}: {error}", json_output=json_output, command="retry")
    store.save(expected=current, updated=retried)
    _emit(
        {"status": "ok", "command": "retry", **_status_summary(retried)},
        (f"Work run {retried.run_id!r} queued for re-verification.", *_human_status_lines(retried)),
        json_output=json_output,
    )


@app.command()
def doctor(
    checkpoints_dir: Path = _CHECKPOINTS_DIR_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Sanity-check CLI configuration and environment without printing secrets."""
    checks: list[dict[str, Any]] = []

    missing_jira = _missing_env_vars(_JIRA_ENV_VARS)
    checks.append(
        {
            "name": "jira_credentials",
            "ok": not missing_jira,
            "detail": "all Jira environment variables are set"
            if not missing_jira
            else f"missing: {', '.join(missing_jira)}",
        }
    )

    missing_repository = _missing_env_vars(_REPOSITORY_ENV_VARS)
    checks.append(
        {
            "name": "repository_configuration",
            "ok": not missing_repository,
            "detail": "all repository environment variables are set"
            if not missing_repository
            else f"missing: {', '.join(missing_repository)}",
        }
    )

    checkpoints_ok = True
    checkpoints_detail = f"{checkpoints_dir} is writable"
    try:
        store = JsonFileCheckpointStore(checkpoints_dir)
        probe = store.directory / f".gmai-doctor-probe-{uuid.uuid4().hex}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except (CheckpointStoreError, OSError) as error:
        checkpoints_ok = False
        checkpoints_detail = f"{checkpoints_dir} is not usable: {error}"
    checks.append({"name": "checkpoints_directory", "ok": checkpoints_ok, "detail": checkpoints_detail})

    overall_ok = all(bool(check["ok"]) for check in checks)
    payload = {"status": "ok" if overall_ok else "error", "command": "doctor", "ok": overall_ok, "checks": checks}
    human_lines = tuple(f"[{'OK' if check['ok'] else 'FAIL'}] {check['name']}: {check['detail']}" for check in checks)
    _emit(payload, human_lines, json_output=json_output)
    if not overall_ok:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
