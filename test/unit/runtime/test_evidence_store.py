"""Tests for the local JSON-file-backed execution-evidence store."""

from __future__ import annotations

from pathlib import Path

import pytest

from gearmeshing_ai.application.ports.coding_executor import EventKind, ExecutionEvent
from gearmeshing_ai.application.ports.evidence import ExecutionEvidence
from gearmeshing_ai.domain.work_run import WorkRunCorrelation
from gearmeshing_ai.runtime.evidence_store import (
    EvidenceConflictError,
    EvidenceStoreError,
    JsonFileEvidenceStore,
)


def _correlation() -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key="GMAI-25",
        jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-25",
        jira_issue_revision="3",
        jira_issue_content_sha256="a" * 64,
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-25/execution_evidence_capture",
        agent_assembly_run_id="assembly-run-25",
    )


def _evidence(evidence_id: str = "evidence-25-001") -> ExecutionEvidence:
    return ExecutionEvidence(
        evidence_id=evidence_id,
        run_id="work-run-25",
        correlation=_correlation(),
        events=(ExecutionEvent(sequence=1, kind=EventKind.STARTED, message="started"),),
    )


def test_load_returns_none_when_no_evidence_exists(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)

    assert store.load("evidence-25-001") is None


def test_save_then_load_round_trips_an_equivalent_evidence_record(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    evidence = _evidence()

    store.save(evidence)
    loaded = store.load(evidence.evidence_id)

    assert loaded == evidence


def test_evidence_is_accessible_after_a_fresh_store_instance_is_opened(tmp_path: Path) -> None:
    """Simulates a process restart: a new store instance must read prior writes."""
    evidence = _evidence()
    JsonFileEvidenceStore(tmp_path).save(evidence)

    reopened = JsonFileEvidenceStore(tmp_path)

    assert reopened.load(evidence.evidence_id) == evidence


def test_save_is_idempotent_for_an_identical_record(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    evidence = _evidence()

    store.save(evidence)
    store.save(evidence)

    assert store.load(evidence.evidence_id) == evidence


def test_save_rejects_overwriting_an_existing_id_with_different_content(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    store.save(_evidence())
    conflicting = ExecutionEvidence(
        evidence_id="evidence-25-001",
        run_id="work-run-25-different",
        correlation=_correlation(),
    )

    with pytest.raises(EvidenceConflictError):
        store.save(conflicting)


def test_directory_is_created_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "evidence"

    JsonFileEvidenceStore(nested)

    assert nested.is_dir()


def test_list_evidence_ids_reports_every_persisted_record_sorted(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    store.save(_evidence("evidence-b"))
    store.save(_evidence("evidence-a"))

    assert store.list_evidence_ids() == ("evidence-a", "evidence-b")


def test_evidence_id_with_path_separators_is_rejected_as_a_filename(tmp_path: Path) -> None:
    """``ExecutionEvidence.evidence_id`` already forbids ``/`` at the schema
    layer, but the store enforces its own filename-safety independently -
    verified here via ``model_construct`` to bypass that schema validation,
    matching the defense-in-depth already tested for the checkpoint store.
    """
    store = JsonFileEvidenceStore(tmp_path)
    unsafe = ExecutionEvidence.model_construct(
        schema_version=1,
        evidence_id="evidence/nested",
        run_id="work-run-25",
        correlation=_correlation(),
        changed_files=(),
        commands=(),
        events=(),
        artifacts=(),
        metadata={},
    )

    with pytest.raises(EvidenceStoreError):
        store.save(unsafe)


def test_corrupt_evidence_file_raises_a_typed_error(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    (tmp_path / "evidence-25-001.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(EvidenceStoreError):
        store.load("evidence-25-001")


def test_save_writes_atomically_leaving_no_temp_file_behind(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)

    store.save(_evidence())

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_persisted_file_pins_schema_version(tmp_path: Path) -> None:
    store = JsonFileEvidenceStore(tmp_path)
    evidence = _evidence()

    store.save(evidence)
    raw = (tmp_path / f"{evidence.evidence_id}.json").read_text(encoding="utf-8")

    assert '"schema_version": 1' in raw
