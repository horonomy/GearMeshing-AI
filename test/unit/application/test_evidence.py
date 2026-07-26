"""Tests for the versioned execution-evidence contract."""

from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from gearmeshing_ai.application.ports.coding_executor import EventKind, ExecutionArtifact, ExecutionEvent
from gearmeshing_ai.application.ports.evidence import (
    SCHEMA_VERSION,
    ArtifactReference,
    CapturedCommand,
    ChangedFile,
    CommandOutput,
    ExecutionEvidence,
    redact_text,
    truncated,
)
from gearmeshing_ai.domain.work_run import WorkRunCorrelation

ARTIFACT_DIGEST = sha256(b"artifact-bytes").hexdigest()


def correlation() -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key="GMAI-25",
        jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-25",
        jira_issue_revision="3",
        jira_issue_content_sha256="a" * 64,
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-25/execution_evidence_capture",
        agent_assembly_run_id="assembly-run-25",
    )


def minimal_evidence(**overrides: object) -> ExecutionEvidence:
    fields: dict[str, object] = {
        "evidence_id": "evidence-1",
        "run_id": "work-run-25",
        "correlation": correlation(),
    }
    fields.update(overrides)
    return ExecutionEvidence(**fields)  # type: ignore[arg-type]


# --- Schema validation ------------------------------------------------------


def test_schema_version_defaults_to_the_current_constant() -> None:
    evidence = minimal_evidence()

    assert evidence.schema_version == SCHEMA_VERSION == 1


def test_schema_version_rejects_any_value_other_than_the_pinned_literal() -> None:
    with pytest.raises(ValidationError):
        minimal_evidence(schema_version=2)


def test_missing_required_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionEvidence(run_id="work-run-25", correlation=correlation())  # type: ignore[call-arg]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionEvidence(
            evidence_id="evidence-1",
            run_id="work-run-25",
            correlation=correlation(),
            unexpected_field="not part of the contract",  # type: ignore[call-arg]
        )


def test_model_is_frozen() -> None:
    evidence = minimal_evidence()

    with pytest.raises(ValidationError):
        evidence.evidence_id = "evidence-2"  # type: ignore[misc]


def test_evidence_id_rejects_an_invalid_identifier_format() -> None:
    with pytest.raises(ValidationError):
        minimal_evidence(evidence_id="not a safe id!")


def test_changed_files_reject_a_duplicate_path() -> None:
    duplicate = ChangedFile(path="a.py", change_type="modified")
    with pytest.raises(ValidationError):
        minimal_evidence(changed_files=(duplicate, duplicate))


def test_changed_file_rename_requires_previous_path() -> None:
    with pytest.raises(ValidationError):
        ChangedFile(path="b.py", change_type="renamed")


def test_changed_file_non_rename_rejects_a_previous_path() -> None:
    with pytest.raises(ValidationError):
        ChangedFile(path="b.py", change_type="modified", previous_path="a.py")


def test_events_reject_a_duplicate_sequence_number() -> None:
    first = ExecutionEvent(sequence=1, kind=EventKind.STARTED, message="started")
    duplicate = ExecutionEvent(sequence=1, kind=EventKind.PROGRESS, message="progress")
    with pytest.raises(ValidationError):
        minimal_evidence(events=(first, duplicate))


def test_events_reject_an_out_of_order_sequence() -> None:
    first = ExecutionEvent(sequence=2, kind=EventKind.STARTED, message="started")
    second = ExecutionEvent(sequence=1, kind=EventKind.PROGRESS, message="progress")
    with pytest.raises(ValidationError):
        minimal_evidence(events=(first, second))


def test_artifacts_reject_a_duplicate_relative_path() -> None:
    artifact = ExecutionArtifact(
        relative_path="out.txt", media_type="text/plain", content_sha256=ARTIFACT_DIGEST, size_bytes=4
    )
    reference = ArtifactReference(artifact=artifact)
    with pytest.raises(ValidationError):
        minimal_evidence(artifacts=(reference, reference))


# --- Redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "password: hunter2",
        "API_KEY=abc123XYZ",
        "Authorization: Bearer abcdef123456.xyz",
        "aws_secret_access_key = 'xyz123'",
        "AKIAABCDEFGHIJKLMNOP is my key id",
        "token=ghp_1234567890abcdefghijklmnopqrstuvwx",
        'export DB_PASSWORD="p@ssw0rd!"',
        "some_session_token: 9f8e7d6c5b4a",
    ],
)
def test_redact_text_removes_common_secret_shapes(raw: str) -> None:
    redacted = redact_text(raw)

    assert "[REDACTED]" in redacted
    for leaked in ("hunter2", "abc123XYZ", "xyz123", "9f8e7d6c5b4a"):
        assert leaked not in redacted


def test_redact_text_scrubs_caller_supplied_sensitive_values() -> None:
    secret = "zzz-not-shaped-like-a-secret"
    redacted = redact_text(f"connecting with value {secret}", sensitive_values=(secret,))

    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_leaves_ordinary_output_untouched() -> None:
    raw = "142 passed, 0 failed in 4.35s"

    assert redact_text(raw) == raw


def test_redact_text_does_not_catch_an_unlabeled_secret() -> None:
    """Documents a known, honest limit: no key= marker means no redaction."""
    raw = "the value is hunter2"

    assert redact_text(raw) == raw


def test_captured_command_capture_redacts_stdout_and_stderr_before_storage() -> None:
    command = CapturedCommand.capture(
        command="printenv",
        exit_code=0,
        duration_seconds=0.5,
        stdout="API_KEY=super-secret-value",
        stderr="warning: token=another-secret",
    )

    assert "super-secret-value" not in command.stdout.text
    assert "another-secret" not in command.stderr.text
    assert "[REDACTED]" in command.stdout.text
    assert "[REDACTED]" in command.stderr.text


def test_evidence_construction_never_stores_a_caller_supplied_secret() -> None:
    command = CapturedCommand.capture(
        command="deploy.sh",
        exit_code=1,
        duration_seconds=2.0,
        stdout="deploying with credential my-plain-secret-42",
        stderr="",
        sensitive_values=("my-plain-secret-42",),
    )
    evidence = minimal_evidence(commands=(command,))

    dumped = evidence.model_dump_json()
    assert "my-plain-secret-42" not in dumped


# --- Truncation ---------------------------------------------------------------


def test_truncated_returns_the_original_text_untouched_when_within_bounds() -> None:
    text, was_truncated = truncated("short output", maximum=100)

    assert text == "short output"
    assert was_truncated is False


def test_truncated_deterministically_bounds_long_text() -> None:
    long_text = "x" * 50

    text, was_truncated = truncated(long_text, maximum=10)

    assert was_truncated is True
    assert len(text) == 10
    assert text.endswith("…")


def test_truncated_is_deterministic_across_repeated_calls() -> None:
    long_text = "y" * 50

    first, _ = truncated(long_text, maximum=10)
    second, _ = truncated(long_text, maximum=10)

    assert first == second


def test_command_output_rejects_text_beyond_the_hard_maximum() -> None:
    with pytest.raises(ValidationError):
        CommandOutput(text="z" * 8_001, truncated=False)


def test_captured_command_capture_marks_truncated_flag_for_oversized_output() -> None:
    command = CapturedCommand.capture(
        command="dump-large-log",
        exit_code=0,
        duration_seconds=1.0,
        stdout="a" * 9_000,
        stderr="",
    )

    assert command.stdout.truncated is True
    assert len(command.stdout.text) == 8_000


def test_command_output_supports_an_external_artifact_reference_for_oversized_output() -> None:
    output = CommandOutput(text="see artifact", truncated=True, artifact_uri="artifact://run-1/stdout.log")

    assert output.artifact_uri == "artifact://run-1/stdout.log"


# --- Correlation association --------------------------------------------------


def test_evidence_carries_the_work_run_correlation_unchanged() -> None:
    corr = correlation()
    evidence = minimal_evidence(correlation=corr)

    assert evidence.correlation == corr
    assert evidence.correlation.agent_assembly_run_id == "assembly-run-25"


# --- Round trip ---------------------------------------------------------------


def test_evidence_round_trips_through_json() -> None:
    event = ExecutionEvent(sequence=1, kind=EventKind.STARTED, message="started", metadata={"attempt": 1})
    changed = ChangedFile(path="a.py", change_type="modified", lines_added=3, lines_removed=1)
    command = CapturedCommand.capture(
        command="pytest -q", exit_code=0, duration_seconds=4.35, stdout="388 passed", stderr=""
    )
    artifact = ArtifactReference(
        artifact=ExecutionArtifact(
            relative_path="out.txt", media_type="text/plain", content_sha256=ARTIFACT_DIGEST, size_bytes=4
        )
    )
    evidence = minimal_evidence(events=(event,), changed_files=(changed,), commands=(command,), artifacts=(artifact,))

    restored = ExecutionEvidence.model_validate_json(evidence.model_dump_json())

    assert restored == evidence
