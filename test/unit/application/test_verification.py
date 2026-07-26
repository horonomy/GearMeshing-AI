"""Tests for the structured, versioned verifier-output contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gearmeshing_ai.application.ports.verification import (
    SCHEMA_VERSION,
    AcceptanceCriterionResult,
    CheckStatus,
    Finding,
    FindingLocation,
    FindingSeverity,
    PrReadiness,
    RepositoryCheckResult,
    StructuredVerificationResult,
    UnresolvedRisk,
    VerificationStatus,
)


def passing_criterion(criterion: str = "The endpoint returns 200 for a valid request.") -> AcceptanceCriterionResult:
    return AcceptanceCriterionResult(
        criterion=criterion,
        status=CheckStatus.PASSED,
        evidence=("test/unit/test_endpoint.py::test_valid_request passed",),
    )


def passing_check(check_name: str = "pytest") -> RepositoryCheckResult:
    return RepositoryCheckResult(
        check_name=check_name,
        status=CheckStatus.PASSED,
        evidence=("142 passed, 0 failed",),
    )


def deterministic_result(
    *,
    status: VerificationStatus = VerificationStatus.PASSED,
    pr_readiness: PrReadiness = PrReadiness.READY,
    acceptance_criteria: tuple[AcceptanceCriterionResult, ...] | None = None,
    findings: tuple[Finding, ...] = (),
) -> StructuredVerificationResult:
    """Build a fully deterministic fixture-style result for reuse across tests."""
    return StructuredVerificationResult(
        run_id="run-gmai-26-001",
        status=status,
        confidence=0.97,
        acceptance_criteria=acceptance_criteria or (passing_criterion(),),
        repository_checks=(passing_check(),),
        findings=findings,
        unresolved_risks=(),
        pr_readiness=pr_readiness,
    )


def test_valid_construction_of_a_passing_result() -> None:
    result = deterministic_result()

    assert result.schema_version == SCHEMA_VERSION
    assert result.status is VerificationStatus.PASSED
    assert result.pr_readiness is PrReadiness.READY
    assert result.acceptance_criteria[0].status is CheckStatus.PASSED
    assert result.repository_checks[0].check_name == "pytest"


def test_deterministic_fixture_round_trips_through_json() -> None:
    """The same fixture builder must serialize to identical JSON every run."""
    first = deterministic_result().model_dump_json()
    second = deterministic_result().model_dump_json()

    assert first == second
    assert '"schema_version":1' in first


def test_schema_version_is_pinned_to_the_current_literal() -> None:
    assert SCHEMA_VERSION == 1

    acceptance_criteria = (passing_criterion(),)
    with pytest.raises(ValidationError):
        StructuredVerificationResult(
            schema_version=2,  # type: ignore[arg-type]
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.0,
            acceptance_criteria=acceptance_criteria,
            pr_readiness=PrReadiness.READY,
        )


def test_findings_distinguish_blocking_from_advisory_severity() -> None:
    blocking = Finding(
        finding_id="finding-1",
        severity=FindingSeverity.BLOCKING,
        summary="Auth token is logged in plaintext.",
        evidence=("gearmeshing_ai/adapters/auth.py:42 logs token",),
        location=FindingLocation(path="gearmeshing_ai/adapters/auth.py", line=42),
        remediation="Redact the token before logging.",
    )
    advisory = Finding(
        finding_id="finding-2",
        severity=FindingSeverity.ADVISORY,
        summary="Docstring is missing a return-type description.",
    )

    result = deterministic_result(
        status=VerificationStatus.FAILED,
        pr_readiness=PrReadiness.NOT_READY,
        acceptance_criteria=(
            AcceptanceCriterionResult(
                criterion="Tokens are never logged.",
                status=CheckStatus.FAILED,
                evidence=("gearmeshing_ai/adapters/auth.py:42 logs token",),
            ),
        ),
        findings=(blocking, advisory),
    )

    blocking_findings = [finding for finding in result.findings if finding.severity is FindingSeverity.BLOCKING]
    advisory_findings = [finding for finding in result.findings if finding.severity is FindingSeverity.ADVISORY]
    assert blocking_findings == [blocking]
    assert advisory_findings == [advisory]


def test_pr_readiness_cannot_be_ready_alongside_a_blocking_finding() -> None:
    blocking = Finding(
        finding_id="finding-1",
        severity=FindingSeverity.BLOCKING,
        summary="Unresolved blocking concern.",
        evidence=("evidence",),
    )

    with pytest.raises(ValidationError, match="pr_readiness cannot be ready"):
        deterministic_result(pr_readiness=PrReadiness.READY, findings=(blocking,))


def test_failed_acceptance_criterion_accepts_non_empty_evidence() -> None:
    criterion = AcceptanceCriterionResult(
        criterion="The migration is reversible.",
        status=CheckStatus.FAILED,
        evidence=("down_revision is None in migrations/0007_add_index.py",),
    )

    assert criterion.evidence
    assert criterion.evidence_gap is None


def test_failed_acceptance_criterion_accepts_an_explicit_evidence_gap() -> None:
    criterion = AcceptanceCriterionResult(
        criterion="The endpoint is idempotent under retry.",
        status=CheckStatus.FAILED,
        evidence_gap="No retry harness was available in this environment to observe the behavior.",
    )

    assert criterion.evidence == ()
    assert criterion.evidence_gap is not None


def test_failed_acceptance_criterion_without_evidence_or_gap_is_rejected() -> None:
    with pytest.raises(ValidationError, match="evidence or an explicit evidence_gap"):
        AcceptanceCriterionResult(
            criterion="The endpoint is idempotent under retry.",
            status=CheckStatus.FAILED,
        )


def test_failed_repository_check_without_evidence_or_gap_is_rejected() -> None:
    with pytest.raises(ValidationError, match="evidence or an explicit evidence_gap"):
        RepositoryCheckResult(
            check_name="mypy",
            status=CheckStatus.FAILED,
        )


def test_failed_repository_check_with_evidence_gap_is_accepted() -> None:
    check = RepositoryCheckResult(
        check_name="mypy",
        status=CheckStatus.FAILED,
        evidence_gap="mypy timed out before producing output in this run.",
    )

    assert check.status is CheckStatus.FAILED


def test_passed_or_skipped_criteria_do_not_require_evidence() -> None:
    passed = AcceptanceCriterionResult(criterion="Criterion A", status=CheckStatus.PASSED)
    skipped = AcceptanceCriterionResult(criterion="Criterion B", status=CheckStatus.SKIPPED)

    assert passed.evidence == ()
    assert skipped.evidence == ()


def test_status_passed_is_rejected_when_a_criterion_failed() -> None:
    failed_criterion = AcceptanceCriterionResult(
        criterion="Criterion A",
        status=CheckStatus.FAILED,
        evidence=("failure evidence",),
    )

    with pytest.raises(ValidationError, match="status cannot be passed"):
        deterministic_result(status=VerificationStatus.PASSED, acceptance_criteria=(failed_criterion,))


def test_status_failed_requires_a_failed_criterion_or_blocking_finding() -> None:
    with pytest.raises(ValidationError, match="requires a failed acceptance criterion"):
        deterministic_result(status=VerificationStatus.FAILED, pr_readiness=PrReadiness.NOT_READY)


def test_acceptance_criteria_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="acceptance_criteria must not be empty"):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.0,
            acceptance_criteria=(),
            pr_readiness=PrReadiness.READY,
        )


def test_confidence_must_be_within_the_unit_interval() -> None:
    acceptance_criteria = (passing_criterion(),)
    with pytest.raises(ValidationError, match="confidence must be between"):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.5,
            acceptance_criteria=acceptance_criteria,
            pr_readiness=PrReadiness.READY,
        )


def test_duplicate_finding_ids_are_rejected() -> None:
    duplicate = Finding(finding_id="dup", severity=FindingSeverity.ADVISORY, summary="First mention.")
    also_duplicate = Finding(finding_id="dup", severity=FindingSeverity.ADVISORY, summary="Second mention.")

    with pytest.raises(ValidationError, match="finding_id"):
        deterministic_result(findings=(duplicate, also_duplicate))


def test_duplicate_repository_check_names_are_rejected() -> None:
    acceptance_criteria = (passing_criterion(),)
    repository_checks = (passing_check("pytest"), passing_check("pytest"))
    with pytest.raises(ValidationError, match="check_name"):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.0,
            acceptance_criteria=acceptance_criteria,
            repository_checks=repository_checks,
            pr_readiness=PrReadiness.READY,
        )


def test_result_is_immutable() -> None:
    result = deterministic_result()

    with pytest.raises(ValidationError):
        result.status = VerificationStatus.FAILED  # type: ignore[misc]


def test_extra_fields_are_rejected() -> None:
    acceptance_criteria = (passing_criterion(),)
    with pytest.raises(ValidationError):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.0,
            acceptance_criteria=acceptance_criteria,
            pr_readiness=PrReadiness.READY,
            extra_field="not allowed",  # type: ignore[call-arg]
        )


def test_boolean_is_rejected_for_confidence_under_strict_mode() -> None:
    acceptance_criteria = (passing_criterion(),)
    with pytest.raises(ValidationError):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=True,
            acceptance_criteria=acceptance_criteria,
            pr_readiness=PrReadiness.READY,
        )


def test_unresolved_risk_requires_bounded_summary() -> None:
    risk = UnresolvedRisk(
        risk_id="risk-1",
        summary="Third-party dependency version was not pinned in this run.",
        evidence=("requirements resolved without a lockfile hash",),
    )

    result = deterministic_result()
    result_with_risk = result.model_copy(update={"unresolved_risks": (risk,)})

    assert result_with_risk.unresolved_risks == (risk,)


def test_duplicate_unresolved_risk_ids_are_rejected() -> None:
    risk = UnresolvedRisk(risk_id="risk-1", summary="A residual concern.")
    acceptance_criteria = (passing_criterion(),)

    with pytest.raises(ValidationError, match="risk_id"):
        StructuredVerificationResult(
            run_id="run-1",
            status=VerificationStatus.PASSED,
            confidence=1.0,
            acceptance_criteria=acceptance_criteria,
            unresolved_risks=(risk, risk),
            pr_readiness=PrReadiness.READY,
        )


def test_evidence_items_must_be_unique_and_bounded() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        AcceptanceCriterionResult(
            criterion="Criterion A",
            status=CheckStatus.PASSED,
            evidence=("same evidence", "same evidence"),
        )


def test_finding_location_requires_a_positive_line_number() -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        FindingLocation(path="gearmeshing_ai/domain/work_run.py", line=0)
