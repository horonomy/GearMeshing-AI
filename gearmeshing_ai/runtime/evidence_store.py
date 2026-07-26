"""Local JSON-file-backed durable store for execution evidence.

Mirrors ``checkpoint_store.JsonFileCheckpointStore``'s pattern - one JSON
file per record, written atomically via a temp-file-then-``os.replace`` -
because that is this repository's established, tested approach to durable,
restart-safe local persistence (see GMAI-14). It is a distinct sibling
rather than a shared/extended store because the two records have different
identity and mutability semantics: a checkpoint is a single mutable
``WorkRun`` snapshot per ``run_id`` guarded by compare-and-swap, while
evidence is an immutable, write-once record per ``evidence_id`` - reusing
``JsonFileCheckpointStore`` directly would mean bending its compare-and-swap
``save(expected=..., updated=...)`` contract onto a shape that has no
"expected previous version" concept.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gearmeshing_ai.application.ports.evidence import ExecutionEvidence

_FILENAME_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class EvidenceStoreError(RuntimeError):
    """Raised when evidence cannot be safely loaded or persisted."""


class EvidenceConflictError(EvidenceStoreError):
    """Raised when saving would silently overwrite a different evidence record.

    Evidence is treated as write-once: once ``evidence_id`` has been
    persisted, only re-saving the identical record is permitted. This
    catches an ``evidence_id`` collision between two distinct records
    instead of letting the second save silently clobber the first.
    """


def _require_filename_safe_evidence_id(evidence_id: str) -> str:
    if not _FILENAME_SAFE_EVIDENCE_ID.fullmatch(evidence_id):
        raise EvidenceStoreError(f"evidence_id {evidence_id!r} is not a safe checkpoint filename")
    return evidence_id


def _read_json_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvidenceStoreError(f"unable to read evidence file {path.name!r}") from error


def _write_text_atomic(path: Path, payload: str) -> None:
    directory = path.parent
    try:
        descriptor, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, path)
        except BaseException:
            os.unlink(temp_name)
            raise
    except OSError as error:
        raise EvidenceStoreError(f"unable to write evidence file {path.name!r}") from error


@dataclass(frozen=True, slots=True)
class JsonFileEvidenceStore:
    """One JSON file per ``ExecutionEvidence`` record, keyed by ``evidence_id``."""

    directory: Path

    def __post_init__(self) -> None:
        directory = Path(self.directory)
        object.__setattr__(self, "directory", directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise EvidenceStoreError(f"unable to create evidence directory {directory!s}") from error

    def _path(self, evidence_id: str) -> Path:
        return self.directory / f"{_require_filename_safe_evidence_id(evidence_id)}.json"

    def list_evidence_ids(self) -> tuple[str, ...]:
        """Return every persisted evidence ID, sorted for deterministic iteration."""
        return tuple(sorted(path.stem for path in self.directory.glob("*.json")))

    def load(self, evidence_id: str) -> ExecutionEvidence | None:
        path = self._path(evidence_id)
        if not path.exists():
            return None
        try:
            return ExecutionEvidence.model_validate_json(_read_json_text(path))
        except ValueError as error:
            raise EvidenceStoreError(f"evidence file {path.name!r} is corrupt or unreadable") from error

    def save(self, evidence: ExecutionEvidence) -> None:
        """Persist ``evidence`` durably, atomically, exactly once per ``evidence_id``.

        Re-saving an identical record is a no-op success (idempotent retry
        after a crash before the caller observed the previous write); saving
        a different record under an already-used ``evidence_id`` raises
        ``EvidenceConflictError`` rather than silently overwriting evidence
        that verification, audit, or Jira updates may already have consumed.
        """
        path = self._path(evidence.evidence_id)
        existing = self.load(evidence.evidence_id)
        if existing is not None and existing != evidence:
            raise EvidenceConflictError(
                f"evidence {evidence.evidence_id!r} is already persisted with different content"
            )
        _write_text_atomic(path, json.dumps(json.loads(evidence.model_dump_json()), indent=2, sort_keys=True) + "\n")
