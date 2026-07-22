"""Deterministic orchestration for a governed engineering work run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from gearmeshing_ai.domain.work_run import WorkRun, WorkRunArtifact, WorkRunCorrelation


class WorkflowIntegrityError(RuntimeError):
    """Raised when persisted workflow history diverges from the approved run."""


class WorkflowStage(StrEnum):
    """Stable names used to scope idempotent workflow operations."""

    EXECUTION = "execution"
    VERIFICATION = "verification"
    REMEDIATION = "remediation"
    DRAFT_PR_PUBLICATION = "draft_pr_publication"


@dataclass(frozen=True, slots=True)
class StageRequest:
    """Immutable context supplied to one idempotent workflow operation."""

    run_id: str
    correlation: WorkRunCorrelation
    artifacts: tuple[WorkRunArtifact, ...]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Evidence-backed result returned by the verification boundary."""

    passed: bool
    artifacts: tuple[WorkRunArtifact, ...] = ()


class ExecutionPort(Protocol):
    """Execute the approved engineering change."""

    def execute(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]: ...


class VerificationPort(Protocol):
    """Verify the current change and return immutable evidence."""

    def verify(self, request: StageRequest) -> VerificationResult: ...


class RemediationPort(Protocol):
    """Correct a failed verification attempt."""

    def remediate(self, request: StageRequest) -> tuple[WorkRunArtifact, ...]: ...


class DraftPrPublisher(Protocol):
    """Publish or recover the single Draft PR for a work run."""

    def publish(self, request: StageRequest) -> str: ...


class WorkflowCheckpointStore(Protocol):
    """Persist workflow checkpoints using compare-and-swap semantics."""

    def load(self, run_id: str) -> WorkRun | None: ...

    def save(self, *, expected: WorkRun | None, updated: WorkRun) -> None: ...


Clock = Callable[[], datetime]
