"""Structured, versioned contract for coding-stage execution evidence.

This module defines the consistent evidence package produced by the coding
stage of a governed work run: which files changed and their diff metadata,
which commands were run and how they exited, the executor's own event
stream, and any generated artifacts - all associated with the ``WorkRun``
and Agent Assembly correlation IDs that produced them.

It is deliberately a distinct, purpose-built contract layered *on top of*
``coding_executor``'s existing types (``ExecutionEvent``, ``ExecutionArtifact``,
``ExecutionResult``) rather than a re-modeling of them: this schema references
those types where it needs to carry their data, and adds the audit,
redaction, correlation, and durability layer the executor contract itself
does not attempt. It intentionally does not persist unbounded raw process
output - see ``redact_text`` and ``CommandOutput`` for the truncation and
redaction rules applied before anything is stored.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from gearmeshing_ai.application.ports.coding_executor import (
    ExecutionArtifact,
    ExecutionEvent,
    MetadataValue,
)
from gearmeshing_ai.domain.work_run import WorkRunCorrelation

SCHEMA_VERSION: Literal[1] = 1
"""Current execution-evidence schema version.

Persisted evidence pins ``schema_version`` to a specific literal (for
example ``1``) so a future, breaking schema revision can be introduced as a
new version without silently reinterpreting evidence written under an
older shape.
"""

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_OUTPUT_CHARACTERS = 8_000
_MAX_COMMAND_LENGTH = 4_000
_MAX_CHANGED_FILES = 2_000
_MAX_COMMANDS = 500
_MAX_EVENTS = 5_000
_MAX_ARTIFACT_REFS = 500

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_FRAGMENT = (
    r"api[_-]?key|access[_-]?key|private[_-]?key|secret|passwd|password|token|authorization|credential|session[_-]?id"
)
_KEY_VALUE_PATTERN = re.compile(
    rf"(?im)(?P<key>[\w.\-]*(?:{_SENSITIVE_KEY_FRAGMENT})[\w.\-]*)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|Bearer\s+\S+|\S+)"
)
_BARE_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.=]+")
_AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")


def redact_text(value: str, *, sensitive_values: tuple[str, ...] = ()) -> str:
    """Best-effort scrub of secret-shaped substrings from ``value``.

    This is a heuristic, pattern-based scrubber, not a secret scanner: it
    catches the shapes this codebase already treats as sensitive elsewhere
    (see ``coding_executor._SENSITIVE_KEY_PARTS`` and
    ``work_management._SENSITIVE_METADATA_KEYS``) - ``key=value``/``key:
    value`` pairs whose key looks credential-shaped, bare ``Bearer`` tokens,
    AWS access key IDs, and GitHub personal-access-token prefixes - plus any
    caller-supplied ``sensitive_values`` (for example, resolved environment
    variable values the caller knows are secret), which are matched as exact
    substrings.

    It deliberately over-redacts around a match's boundaries when a pattern
    overlaps normal text, because a false positive (redacting benign text) is
    the acceptable failure mode here, not a false negative (leaking a
    secret). It does **not** detect secrets with no distinguishing shape
    (for example, a bare password with no surrounding ``key=``/``key:``
    marker), does not decode encoded payloads (base64, URL-encoding) before
    scanning, and is not a substitute for callers redacting known-sensitive
    environment values before they ever reach this function.
    """
    redacted = value
    for secret in sensitive_values:
        candidate = secret.strip()
        if candidate:
            redacted = redacted.replace(candidate, _REDACTED)
    redacted = _AWS_ACCESS_KEY_ID_PATTERN.sub(_REDACTED, redacted)
    redacted = _GITHUB_TOKEN_PATTERN.sub(_REDACTED, redacted)
    redacted = _KEY_VALUE_PATTERN.sub(lambda match: f"{match['key']}{match['sep']}{_REDACTED}", redacted)
    redacted = _BARE_BEARER_PATTERN.sub(f"Bearer {_REDACTED}", redacted)
    return redacted


def truncated(value: str, *, maximum: int = _MAX_OUTPUT_CHARACTERS) -> tuple[str, bool]:
    """Deterministically bound ``value`` to ``maximum`` characters.

    Matches the truncation convention already used by
    ``claude_code_executor._truncated``: whitespace is not collapsed here
    (unlike that helper) because command output legitimately carries
    meaningful line structure, but the same fixed-length-with-ellipsis rule
    applies. Returns the possibly-truncated text and whether truncation
    occurred, so callers can record ``output_truncated`` explicitly instead
    of inferring it from string length after the fact.
    """
    if len(value) <= maximum:
        return value, False
    return value[: maximum - 1] + "…", True


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


def _serialize_execution_artifact(artifact: ExecutionArtifact | None) -> dict[str, MetadataValue] | None:
    if artifact is None:
        return None
    return {
        "relative_path": artifact.relative_path,
        "media_type": artifact.media_type,
        "content_sha256": artifact.content_sha256,
        "size_bytes": artifact.size_bytes,
    }


def _serialize_execution_event(event: ExecutionEvent) -> dict[str, object]:
    """Serialize an ``ExecutionEvent`` to a JSON-safe mapping.

    ``ExecutionEvent`` is a frozen dataclass, not a Pydantic model, and its
    ``metadata`` is a ``MappingProxyType`` - neither of which Pydantic's
    default JSON encoder can serialize directly - so this evidence schema
    serializes it explicitly rather than relying on Pydantic's dataclass
    integration.
    """
    return {
        "sequence": event.sequence,
        "kind": event.kind.value,
        "message": event.message,
        "metadata": dict(event.metadata),
        "artifact": _serialize_execution_artifact(event.artifact),
    }


class ChangedFile(BaseModel):
    """One file touched by the coding stage, with bounded diff metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str
    change_type: Literal["added", "modified", "deleted", "renamed"]
    lines_added: int = 0
    lines_removed: int = 0
    previous_path: str | None = None
    diff_artifact_uri: str | None = None

    @field_validator("path", "previous_path")
    @classmethod
    def _validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "path", maximum=1024)

    @field_validator("lines_added", "lines_removed")
    @classmethod
    def _validate_line_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("line counts must not be negative")
        return value

    @field_validator("diff_artifact_uri")
    @classmethod
    def _validate_diff_artifact_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "diff_artifact_uri", maximum=1024)

    @model_validator(mode="after")
    def _require_previous_path_for_rename(self) -> ChangedFile:
        if self.change_type == "renamed" and self.previous_path is None:
            raise ValueError("a renamed file requires previous_path")
        if self.change_type != "renamed" and self.previous_path is not None:
            raise ValueError("previous_path is only meaningful for a renamed file")
        return self


class CommandOutput(BaseModel):
    """Bounded, redacted capture of one stream of command output.

    ``text`` has already been redacted (via ``redact_text``) and truncated
    (via ``truncated``) by the time it reaches this model - see
    ``CapturedCommand.capture`` - so this model's own validators only enforce
    the resulting bounds, they do not themselves redact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    text: str
    truncated: bool = False
    artifact_uri: str | None = None

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("text must be a string")
        if len(value) > _MAX_OUTPUT_CHARACTERS:
            raise ValueError(f"text must not exceed {_MAX_OUTPUT_CHARACTERS} characters")
        return value

    @field_validator("artifact_uri")
    @classmethod
    def _validate_artifact_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "artifact_uri", maximum=1024)


class CapturedCommand(BaseModel):
    """One executed command with its exit code, duration, and captured output."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    command: str
    exit_code: int
    duration_seconds: float
    stdout: CommandOutput
    stderr: CommandOutput
    working_directory: str | None = None

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        return _bounded_text(value, "command", maximum=_MAX_COMMAND_LENGTH, allow_newlines=True)

    @field_validator("duration_seconds")
    @classmethod
    def _validate_duration(cls, value: float) -> float:
        if isinstance(value, bool) or value < 0:
            raise ValueError("duration_seconds must not be negative")
        return value

    @field_validator("working_directory")
    @classmethod
    def _validate_working_directory(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, "working_directory", maximum=1024)

    @classmethod
    def capture(
        cls,
        *,
        command: str,
        exit_code: int,
        duration_seconds: float,
        stdout: str,
        stderr: str,
        working_directory: str | None = None,
        sensitive_values: tuple[str, ...] = (),
        stdout_artifact_uri: str | None = None,
        stderr_artifact_uri: str | None = None,
    ) -> CapturedCommand:
        """Build a ``CapturedCommand`` applying redaction and truncation to raw output.

        This is the entry point evidence-producing callers should use instead
        of constructing ``CommandOutput`` directly from unredacted process
        output: redaction runs first (so it inspects full-length text) and
        truncation runs second, matching ``redact_text`` and ``truncated``'s
        documented order and limits.
        """
        redacted_stdout = redact_text(stdout, sensitive_values=sensitive_values)
        redacted_stderr = redact_text(stderr, sensitive_values=sensitive_values)
        bounded_stdout, stdout_was_truncated = truncated(redacted_stdout)
        bounded_stderr, stderr_was_truncated = truncated(redacted_stderr)
        return cls(
            command=command,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            stdout=CommandOutput(text=bounded_stdout, truncated=stdout_was_truncated, artifact_uri=stdout_artifact_uri),
            stderr=CommandOutput(text=bounded_stderr, truncated=stderr_was_truncated, artifact_uri=stderr_artifact_uri),
            working_directory=working_directory,
        )


class ArtifactReference(BaseModel):
    """A generated artifact produced during the coding stage.

    Wraps ``coding_executor.ExecutionArtifact`` rather than duplicating its
    validated shape (relative path, media type, digest, size); this schema
    adds nothing to that contract beyond carrying it inside the evidence
    package.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact: ExecutionArtifact


class ExecutionEvidence(BaseModel):
    """Complete, versioned evidence package from one coding-stage execution.

    Associates changed-file/diff metadata, captured commands, the executor's
    own event stream, and generated artifacts with the ``WorkRun`` and Agent
    Assembly correlation that produced them, so verification, audit, Jira
    updates, and evaluation can consume this evidence directly rather than
    scraping free-form logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    evidence_id: str
    run_id: str
    correlation: WorkRunCorrelation
    changed_files: tuple[ChangedFile, ...] = ()
    commands: tuple[CapturedCommand, ...] = ()
    events: tuple[ExecutionEvent, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    metadata: dict[str, MetadataValue] = {}

    @field_validator("evidence_id", "run_id")
    @classmethod
    def _validate_identifier_field(cls, value: str) -> str:
        return _identifier(value, "identifier")

    @field_serializer("events")
    def _serialize_events(self, value: tuple[ExecutionEvent, ...]) -> list[dict[str, object]]:
        return [_serialize_execution_event(event) for event in value]

    @field_validator("changed_files")
    @classmethod
    def _validate_changed_files(cls, value: tuple[ChangedFile, ...]) -> tuple[ChangedFile, ...]:
        if len(value) > _MAX_CHANGED_FILES:
            raise ValueError(f"changed_files must not exceed {_MAX_CHANGED_FILES} items")
        paths = tuple(entry.path for entry in value)
        if len(paths) != len(set(paths)):
            raise ValueError("changed_files must not repeat a path")
        return value

    @field_validator("commands")
    @classmethod
    def _validate_commands(cls, value: tuple[CapturedCommand, ...]) -> tuple[CapturedCommand, ...]:
        if len(value) > _MAX_COMMANDS:
            raise ValueError(f"commands must not exceed {_MAX_COMMANDS} items")
        return value

    @field_validator("events")
    @classmethod
    def _validate_events(cls, value: tuple[ExecutionEvent, ...]) -> tuple[ExecutionEvent, ...]:
        if len(value) > _MAX_EVENTS:
            raise ValueError(f"events must not exceed {_MAX_EVENTS} items")
        sequences = tuple(event.sequence for event in value)
        if len(sequences) != len(set(sequences)):
            raise ValueError("events must not repeat a sequence number")
        if sequences != tuple(sorted(sequences)):
            raise ValueError("events must be ordered by increasing sequence")
        return value

    @field_validator("artifacts")
    @classmethod
    def _validate_artifacts(cls, value: tuple[ArtifactReference, ...]) -> tuple[ArtifactReference, ...]:
        if len(value) > _MAX_ARTIFACT_REFS:
            raise ValueError(f"artifacts must not exceed {_MAX_ARTIFACT_REFS} items")
        paths = tuple(entry.artifact.relative_path for entry in value)
        if len(paths) != len(set(paths)):
            raise ValueError("artifacts must not repeat a relative_path")
        return value
