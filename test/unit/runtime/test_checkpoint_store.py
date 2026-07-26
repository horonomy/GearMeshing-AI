"""Tests for the local JSON-file-backed workflow checkpoint store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gearmeshing_ai.domain.work_run import WorkRun, WorkRunCorrelation, WorkRunState
from gearmeshing_ai.runtime.checkpoint_store import (
    CheckpointConflictError,
    CheckpointStoreError,
    JsonFileCheckpointStore,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _correlation() -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key="GMAI-14",
        jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-14",
        jira_issue_revision="10",
        jira_issue_content_sha256="a" * 64,
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-14/cli_work_run_controls",
        agent_assembly_run_id="assembly-run-14",
    )


def _approved(run_id: str = "work-run-14") -> WorkRun:
    return WorkRun.approve(run_id=run_id, correlation=_correlation(), actor_id="human-product-owner", occurred_at=NOW)


def test_load_returns_none_when_no_checkpoint_exists(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)

    assert store.load("work-run-14") is None


def test_save_then_load_round_trips_an_equivalent_work_run(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    approved = _approved()

    store.save(expected=None, updated=approved)
    loaded = store.load(approved.run_id)

    assert loaded == approved


def test_save_persists_transitions_across_separate_store_instances(tmp_path: Path) -> None:
    approved = _approved()
    JsonFileCheckpointStore(tmp_path).save(expected=None, updated=approved)

    reopened = JsonFileCheckpointStore(tmp_path)
    current = reopened.load(approved.run_id)
    assert current is not None
    executing = current.transition_to(WorkRunState.EXECUTING, actor_id="agent-assembly", occurred_at=NOW)
    reopened.save(expected=current, updated=executing)

    assert JsonFileCheckpointStore(tmp_path).load(approved.run_id) == executing


def test_save_rejects_a_stale_expected_value(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    approved = _approved()
    store.save(expected=None, updated=approved)
    executing = approved.transition_to(WorkRunState.EXECUTING, actor_id="agent-assembly", occurred_at=NOW)
    store.save(expected=approved, updated=executing)

    stale_next = executing.transition_to(WorkRunState.VERIFYING, actor_id="agent-assembly", occurred_at=NOW)
    with pytest.raises(CheckpointConflictError):
        store.save(expected=approved, updated=stale_next)


def test_save_rejects_overwriting_an_unexpected_existing_checkpoint(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    approved = _approved()

    with pytest.raises(CheckpointConflictError):
        store.save(expected=approved, updated=approved)


def test_directory_is_created_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "checkpoints"

    JsonFileCheckpointStore(nested)

    assert nested.is_dir()


def test_list_run_ids_reports_every_persisted_run(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    store.save(expected=None, updated=_approved("work-run-a"))
    store.save(expected=None, updated=_approved("work-run-b"))

    assert store.list_run_ids() == ("work-run-a", "work-run-b")


def test_run_id_with_path_separators_is_rejected_as_a_filename(tmp_path: Path) -> None:
    """``WorkRun.run_id`` permits ``/``, but this store needs a flat filename."""
    store = JsonFileCheckpointStore(tmp_path)
    unsafe = _approved("work-run-14/nested")

    with pytest.raises(CheckpointStoreError):
        store.save(expected=None, updated=unsafe)


def test_corrupt_checkpoint_file_raises_a_typed_error(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    (tmp_path / "work-run-14.json").write_text(json.dumps({"schema_version": 1, "work_run": {}}), encoding="utf-8")

    with pytest.raises(CheckpointStoreError):
        store.load("work-run-14")


def test_checkpoint_files_do_not_contain_credential_like_material(tmp_path: Path) -> None:
    store = JsonFileCheckpointStore(tmp_path)
    approved = _approved()
    store.save(expected=None, updated=approved)

    raw = (tmp_path / f"{approved.run_id}.json").read_text(encoding="utf-8")

    assert "token" not in raw.casefold()
    assert "password" not in raw.casefold()
    assert "secret" not in raw.casefold()
