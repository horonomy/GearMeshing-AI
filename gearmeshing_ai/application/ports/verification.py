"""Structured, versioned contract for verifier output.

``workflow_runner.VerificationResult`` remains the workflow runner's minimal
internal summary (``passed`` plus attached artifacts). This module defines
the richer report a real verifier implementation produces: a typed decision
with confidence, per-acceptance-criterion and per-repository-check results,
evidence-backed findings distinguished by severity, unresolved risks, and an
explicit PR-readiness recommendation. It intentionally does not persist raw
hidden model reasoning (no chain-of-thought or scratch fields) — only the
verifier's concluded, reviewable output.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

SCHEMA_VERSION: Literal[1] = 1
"""Current structured-verification-result schema version.

Fixtures and adapters pin ``schema_version`` to a specific literal (for
example ``1``) so a future, breaking schema revision can be introduced as a
new version without silently reinterpreting old fixtures.
"""

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_EVIDENCE_ITEMS = 20
_MAX_EVIDENCE_ITEM_LENGTH = 4_000
_MAX_RISKS = 50


def _bounded_text(value: str, name: str, *, maximum: int, allow_newlines: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    allowed_control = "\t\n\r" if allow_newlines else ""
    if any(ord(character) < 32 and character not in allowed_control for character in normalized):
        raise ValueError(f"{name} must not contain unsupported control characters")
    return normalized


def _identifier(value: str, name: str) -> str:
    normalized = _bounded_text(value, name, maximum=128)
    if _IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} has an invalid format")
    return normalized


def _bounded_evidence(value: tuple[str, ...], name: str = "evidence") -> tuple[str, ...]:
    """Bound and sanitize free-form evidence snippets.

    Evidence entries are structurally validated (bounded length, no control
    characters other than newline/tab/carriage-return, no duplicates) but are
    not secret-scanned, matching this codebase's existing metadata convention
    (see ``coding_executor._frozen_metadata``): callers must redact
    credential material before it reaches this schema.
    """
    if len(value) > _MAX_EVIDENCE_ITEMS:
        raise ValueError(f"{name} must not exceed {_MAX_EVIDENCE_ITEMS} items")
    normalized = tuple(
        _bounded_text(item, f"{name} item", maximum=_MAX_EVIDENCE_ITEM_LENGTH, allow_newlines=True) for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must not contain duplicate entries")
    return normalized


class VerificationStatus(StrEnum):
    """Finite terminal conclusions a verifier may reach for one attempt."""

    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class CheckStatus(StrEnum):
    """Outcome of one discrete acceptance-criterion or repository check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FindingSeverity(StrEnum):
    """Whether a finding must block PR readiness or is merely advisory.

    This is the explicit mechanism the contract uses to distinguish blocking
    from advisory concerns, rather than leaving the distinction to free-text
    convention.
    """

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class PrReadiness(StrEnum):
    """The verifier's recommendation on whether a PR is ready for review."""

    READY = "ready"
    NOT_READY = "not_ready"
    READY_WITH_ADVISORIES = "ready_with_advisories"


class FindingLocation(BaseModel):
    """Bounded pointer to where a finding was observed."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str
    line: int | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _bounded_text(value, "path", maximum=1024)

    @field_validator("line")
    @classmethod
    def _validate_line(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("line must be a positive integer")
        return value


class Finding(BaseModel):
    """One evidence-backed observation with severity and remediation guidance."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    finding_id: str
    severity: FindingSeverity
    summary: str
    evidence: tuple[str, ...] = ()
    location: FindingLocation | None = None
    remediation: str | None = None

    @field_validator("finding_id")
    @classmethod
    def _validate_finding_id(cls, value: str) -> str:
        return _identifier(value, "finding_id")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        return _bounded_text(value, "summary", maximum=512)

    @field_validator("evidence")
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_evidence(value, "finding evidence")

    @field_validator("remediation")
    @classmethod
    def _validate_remediation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "remediation", maximum=2_000, allow_newlines=True)


class AcceptanceCriterionResult(BaseModel):
    """Verdict for one acceptance criterion from the approved specification."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    criterion: str
    status: CheckStatus
    evidence: tuple[str, ...] = ()
    evidence_gap: str | None = None

    @field_validator("criterion")
    @classmethod
    def _validate_criterion(cls, value: str) -> str:
        return _bounded_text(value, "criterion", maximum=2_000)

    @field_validator("evidence")
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_evidence(value, "acceptance criterion evidence")

    @field_validator("evidence_gap")
    @classmethod
    def _validate_evidence_gap(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "evidence_gap", maximum=1_000, allow_newlines=True)

    @model_validator(mode="after")
    def _require_evidence_or_gap_when_failed(self) -> AcceptanceCriterionResult:
        """Enforce that a failed criterion always explains its evidence.

        This is the schema-level enforcement mandated by the ticket: every
        failed acceptance criterion must carry either non-empty ``evidence``
        or an explicit ``evidence_gap`` reason. A criterion that merely
        skipped or passed carries no such obligation.
        """
        if self.status is CheckStatus.FAILED and not self.evidence and not self.evidence_gap:
            raise ValueError("a failed acceptance criterion requires evidence or an explicit evidence_gap")
        return self


class RepositoryCheckResult(BaseModel):
    """Verdict for one repository-level check (tests, lint, types, build)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    check_name: str
    status: CheckStatus
    evidence: tuple[str, ...] = ()
    evidence_gap: str | None = None

    @field_validator("check_name")
    @classmethod
    def _validate_check_name(cls, value: str) -> str:
        return _identifier(value, "check_name")

    @field_validator("evidence")
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_evidence(value, "repository check evidence")

    @field_validator("evidence_gap")
    @classmethod
    def _validate_evidence_gap(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "evidence_gap", maximum=1_000, allow_newlines=True)

    @model_validator(mode="after")
    def _require_evidence_or_gap_when_failed(self) -> RepositoryCheckResult:
        """Enforce the same evidence-or-gap rule for repository checks.

        The acceptance criteria explicitly call out failed *acceptance*
        criteria, but the same rigor is applied here so a failed repository
        check (for example, a test-suite failure) can never be reported
        without either supporting evidence or an explicit acknowledgement
        that evidence could not be captured.
        """
        if self.status is CheckStatus.FAILED and not self.evidence and not self.evidence_gap:
            raise ValueError("a failed repository check requires evidence or an explicit evidence_gap")
        return self


class UnresolvedRisk(BaseModel):
    """A residual concern the verifier could not fully resolve."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    risk_id: str
    summary: str
    evidence: tuple[str, ...] = ()

    @field_validator("risk_id")
    @classmethod
    def _validate_risk_id(cls, value: str) -> str:
        return _identifier(value, "risk_id")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        return _bounded_text(value, "summary", maximum=512)

    @field_validator("evidence")
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _bounded_evidence(value, "risk evidence")


class StructuredVerificationResult(BaseModel):
    """Complete, versioned, Pydantic-validated verifier conclusion.

    This is the richer contract a real verifier implementation produces in
    place of unstructured prose: a typed status and confidence, per-item
    acceptance-criterion and repository-check verdicts, evidence-backed
    findings tagged blocking or advisory, any unresolved risks, and an
    explicit PR-readiness recommendation. It carries no raw hidden model
    reasoning (no chain-of-thought or scratch fields) — only the verifier's
    concluded, reviewable output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    status: VerificationStatus
    confidence: float
    acceptance_criteria: tuple[AcceptanceCriterionResult, ...]
    repository_checks: tuple[RepositoryCheckResult, ...] = ()
    findings: tuple[Finding, ...] = ()
    unresolved_risks: tuple[UnresolvedRisk, ...] = ()
    pr_readiness: PrReadiness

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        return _identifier(value, "run_id")

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0 inclusive")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_acceptance_criteria(
        cls, value: tuple[AcceptanceCriterionResult, ...]
    ) -> tuple[AcceptanceCriterionResult, ...]:
        if not value:
            raise ValueError("acceptance_criteria must not be empty")
        return value

    @field_validator("repository_checks")
    @classmethod
    def _validate_repository_checks(cls, value: tuple[RepositoryCheckResult, ...]) -> tuple[RepositoryCheckResult, ...]:
        names = tuple(check.check_name for check in value)
        if len(names) != len(set(names)):
            raise ValueError("repository_checks must not repeat a check_name")
        return value

    @field_validator("findings")
    @classmethod
    def _validate_findings(cls, value: tuple[Finding, ...]) -> tuple[Finding, ...]:
        ids = tuple(finding.finding_id for finding in value)
        if len(ids) != len(set(ids)):
            raise ValueError("findings must not repeat a finding_id")
        return value

    @field_validator("unresolved_risks")
    @classmethod
    def _validate_unresolved_risks(cls, value: tuple[UnresolvedRisk, ...]) -> tuple[UnresolvedRisk, ...]:
        if len(value) > _MAX_RISKS:
            raise ValueError(f"unresolved_risks must not exceed {_MAX_RISKS} items")
        ids = tuple(risk.risk_id for risk in value)
        if len(ids) != len(set(ids)):
            raise ValueError("unresolved_risks must not repeat a risk_id")
        return value

    @model_validator(mode="after")
    def _require_status_evidence_consistency(self) -> StructuredVerificationResult:
        """Cross-check the overall status against per-criterion verdicts.

        A ``passed`` overall status is inconsistent with any failed
        acceptance criterion, and a ``failed`` overall status requires at
        least one failed criterion or a blocking finding to justify it.
        """
        any_failed_criterion = any(criterion.status is CheckStatus.FAILED for criterion in self.acceptance_criteria)
        any_blocking_finding = any(finding.severity is FindingSeverity.BLOCKING for finding in self.findings)
        if self.status is VerificationStatus.PASSED and any_failed_criterion:
            raise ValueError("status cannot be passed while an acceptance criterion has failed")
        if self.status is VerificationStatus.FAILED and not any_failed_criterion and not any_blocking_finding:
            raise ValueError("status failed requires a failed acceptance criterion or a blocking finding")
        if self.pr_readiness is PrReadiness.READY and (any_blocking_finding or any_failed_criterion):
            raise ValueError("pr_readiness cannot be ready while blocking concerns remain")
        return self
