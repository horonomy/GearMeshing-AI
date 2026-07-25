"""Local JSON-file-backed workflow checkpoint store for the POC CLI.

This is intentionally minimal: one JSON file per work run, written
atomically, with compare-and-swap enforced by re-reading the file before
every write. It satisfies the ``WorkflowCheckpointStore`` protocol used by
``WorkflowRunner`` and gives the ``gmai`` CLI durable state for ``status``,
``cancel``, and ``retry`` across process invocations. A production-grade,
concurrent-safe checkpoint backend is out of scope for this ticket.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gearmeshing_ai.domain.work_run import (
    WorkRun,
    WorkRunArtifact,
    WorkRunCorrelation,
    WorkRunEvent,
    WorkRunState,
)

_SCHEMA_VERSION = 1
_FILENAME_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _require_filename_safe_run_id(run_id: str) -> str:
    """Reject run IDs that could escape the checkpoints directory as a filename.

    ``WorkRun.run_id`` already permits ``/`` (see ``domain.work_run``), which
    would otherwise let a checkpoint be written into an unexpected
    subdirectory. This store only ever needs a flat, single-segment filename.
    """
    if not _FILENAME_SAFE_RUN_ID.fullmatch(run_id):
        raise CheckpointStoreError(f"run_id {run_id!r} is not a safe checkpoint filename")
    return run_id


class CheckpointStoreError(RuntimeError):
    """Raised when a checkpoint cannot be safely loaded or persisted."""


class CheckpointConflictError(CheckpointStoreError):
    """Raised when the on-disk checkpoint no longer matches the expected value."""


def serialize_work_run(run: WorkRun) -> dict[str, Any]:
    """Return a JSON-safe representation of ``run``.

    ``Any`` is unavoidable here: the return value is a JSON document, whose
    values are heterogeneous by construction.
    """
    return {
        "run_id": run.run_id,
        "correlation": {
            "jira_issue_key": run.correlation.jira_issue_key,
            "jira_issue_url": run.correlation.jira_issue_url,
            "jira_issue_revision": run.correlation.jira_issue_revision,
            "jira_issue_content_sha256": run.correlation.jira_issue_content_sha256,
            "repository_url": run.correlation.repository_url,
            "branch_name": run.correlation.branch_name,
            "agent_assembly_run_id": run.correlation.agent_assembly_run_id,
        },
        "state": run.state.value,
        "events": [_serialize_event(event) for event in run.events],
        "artifacts": [_serialize_artifact(artifact) for artifact in run.artifacts],
        "draft_pr_url": run.draft_pr_url,
    }


def _serialize_event(event: WorkRunEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "name": event.name,
        "state": event.state.value,
        "actor_id": event.actor_id,
        "occurred_at": event.occurred_at.isoformat(),
        "details": [[key, value] for key, value in event.details],
    }


def _serialize_artifact(artifact: WorkRunArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "uri": artifact.uri,
        "sha256": artifact.sha256,
    }


def _deserialize_work_run(payload: object) -> WorkRun:
    try:
        return _deserialize_work_run_unsafe(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise CheckpointStoreError("checkpoint file is corrupt or unreadable") from error


def _deserialize_work_run_unsafe(payload: object) -> WorkRun:
    if not isinstance(payload, dict):
        raise TypeError("checkpoint payload must be a JSON object")
    body = payload.get("work_run")
    if not isinstance(body, dict):
        raise TypeError("checkpoint payload is missing its work_run body")
    correlation_body = body["correlation"]
    if not isinstance(correlation_body, dict):
        raise TypeError("checkpoint correlation must be a JSON object")
    correlation = WorkRunCorrelation(
        jira_issue_key=correlation_body["jira_issue_key"],
        jira_issue_url=correlation_body["jira_issue_url"],
        jira_issue_revision=correlation_body["jira_issue_revision"],
        jira_issue_content_sha256=correlation_body["jira_issue_content_sha256"],
        repository_url=correlation_body["repository_url"],
        branch_name=correlation_body["branch_name"],
        agent_assembly_run_id=correlation_body["agent_assembly_run_id"],
    )
    events_body = body["events"]
    if not isinstance(events_body, list):
        raise TypeError("checkpoint events must be a JSON array")
    events = tuple(_deserialize_event(entry) for entry in events_body)
    artifacts_body = body.get("artifacts", [])
    if not isinstance(artifacts_body, list):
        raise TypeError("checkpoint artifacts must be a JSON array")
    artifacts = tuple(_deserialize_artifact(entry) for entry in artifacts_body)
    draft_pr_url = body.get("draft_pr_url")
    if draft_pr_url is not None and not isinstance(draft_pr_url, str):
        raise TypeError("checkpoint draft_pr_url must be a string or null")
    return WorkRun(
        run_id=body["run_id"],
        correlation=correlation,
        state=WorkRunState(body["state"]),
        events=events,
        artifacts=artifacts,
        draft_pr_url=draft_pr_url,
    )


def _deserialize_event(entry: object) -> WorkRunEvent:
    if not isinstance(entry, dict):
        raise TypeError("checkpoint event must be a JSON object")
    details_body = entry.get("details", [])
    if not isinstance(details_body, list):
        raise TypeError("checkpoint event details must be a JSON array")
    details: tuple[tuple[str, str], ...] = tuple((str(pair[0]), str(pair[1])) for pair in details_body)
    occurred_at = datetime.fromisoformat(entry["occurred_at"])
    return WorkRunEvent(
        sequence=entry["sequence"],
        name=entry["name"],
        state=WorkRunState(entry["state"]),
        actor_id=entry["actor_id"],
        occurred_at=occurred_at,
        details=details,
    )


def _deserialize_artifact(entry: object) -> WorkRunArtifact:
    if not isinstance(entry, dict):
        raise TypeError("checkpoint artifact must be a JSON object")
    return WorkRunArtifact(
        artifact_id=entry["artifact_id"],
        kind=entry["kind"],
        uri=entry["uri"],
        sha256=entry.get("sha256"),
    )


def _read_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        raise CheckpointStoreError(f"unable to read checkpoint file {path.name!r}") from error


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    directory = path.parent
    try:
        descriptor, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, path)
        except BaseException:
            os.unlink(temp_name)
            raise
    except OSError as error:
        raise CheckpointStoreError(f"unable to write checkpoint file {path.name!r}") from error


@dataclass(frozen=True, slots=True)
class JsonFileCheckpointStore:
    """One JSON file per work run, keyed by ``run_id``, under ``directory``."""

    directory: Path

    def __post_init__(self) -> None:
        directory = Path(self.directory)
        object.__setattr__(self, "directory", directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CheckpointStoreError(f"unable to create checkpoints directory {directory!s}") from error

    def _path(self, run_id: str) -> Path:
        return self.directory / f"{_require_filename_safe_run_id(run_id)}.json"

    def list_run_ids(self) -> tuple[str, ...]:
        """Return every persisted run ID, for identifier resolution by the CLI."""
        return tuple(sorted(path.stem for path in self.directory.glob("*.json")))

    def load(self, run_id: str) -> WorkRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return _deserialize_work_run(_read_json(path))

    def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None:
        path = self._path(updated.run_id)
        current = self.load(updated.run_id)
        if current != expected:
            raise CheckpointConflictError(f"checkpoint for {updated.run_id!r} was modified since it was last read")
        _write_json_atomic(path, {"schema_version": _SCHEMA_VERSION, "work_run": serialize_work_run(updated)})
