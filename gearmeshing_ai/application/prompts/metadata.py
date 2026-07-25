"""Validated metadata describing one versioned prompt asset."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PROMPT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_SCHEMA_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class PromptValidationError(ValueError):
    """Raised when prompt metadata or a prompt source file fails validation."""


def _validate_prompt_id(value: str) -> str:
    normalized = value.strip()
    if _PROMPT_ID_PATTERN.fullmatch(normalized) is None:
        raise PromptValidationError(f"prompt_id {value!r} must be lowercase snake_case")
    return normalized


def _validate_version(value: str) -> str:
    normalized = value.strip()
    if _VERSION_PATTERN.fullmatch(normalized) is None:
        raise PromptValidationError(f"version {value!r} must be MAJOR.MINOR.PATCH")
    return normalized


def _validate_schema_version(value: str, name: str) -> str:
    normalized = value.strip()
    if _SCHEMA_VERSION_PATTERN.fullmatch(normalized) is None:
        raise PromptValidationError(f"{name} {value!r} has an invalid format")
    return normalized


def _validate_model(value: str) -> str:
    normalized = value.strip()
    if _MODEL_PATTERN.fullmatch(normalized) is None:
        raise PromptValidationError(f"model {value!r} has an invalid format")
    return normalized


def _validate_toolset(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    if any(_TOOL_PATTERN.fullmatch(value) is None for value in normalized):
        raise PromptValidationError("toolset entries must be lowercase snake_case tool names")
    if len(set(normalized)) != len(normalized):
        raise PromptValidationError("toolset must not contain duplicates")
    return normalized


def _validate_rendered_hash(value: str) -> str:
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise PromptValidationError("rendered_hash must be a lowercase SHA-256 digest")
    return normalized


@dataclass(frozen=True, slots=True)
class PromptDescriptor:
    """Static, repository-authored identity of one prompt template version.

    Loaded verbatim from a prompt's ``metadata.yaml`` sidecar. It never
    carries a ``rendered_hash`` because no template has been rendered yet --
    that field only exists on :class:`PromptMetadata`, computed by
    ``PromptRegistry.render`` from the actual rendered text.
    """

    prompt_id: str
    version: str
    input_schema_version: str
    output_schema_version: str
    model: str
    toolset: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt_id", _validate_prompt_id(self.prompt_id))
        object.__setattr__(self, "version", _validate_version(self.version))
        object.__setattr__(
            self, "input_schema_version", _validate_schema_version(self.input_schema_version, "input_schema_version")
        )
        object.__setattr__(
            self,
            "output_schema_version",
            _validate_schema_version(self.output_schema_version, "output_schema_version"),
        )
        object.__setattr__(self, "model", _validate_model(self.model))
        object.__setattr__(self, "toolset", _validate_toolset(self.toolset))


@dataclass(frozen=True, slots=True)
class PromptMetadata:
    """Everything a caller must record about one rendered prompt invocation.

    ``rendered_hash`` is always computed from the rendered text by
    ``PromptRegistry.render`` -- it is never accepted from a prompt's YAML
    sidecar -- so it faithfully identifies exactly what was sent to a model
    for this specific invocation.
    """

    prompt_id: str
    version: str
    rendered_hash: str
    input_schema_version: str
    output_schema_version: str
    model: str
    toolset: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt_id", _validate_prompt_id(self.prompt_id))
        object.__setattr__(self, "version", _validate_version(self.version))
        object.__setattr__(self, "rendered_hash", _validate_rendered_hash(self.rendered_hash))
        object.__setattr__(
            self, "input_schema_version", _validate_schema_version(self.input_schema_version, "input_schema_version")
        )
        object.__setattr__(
            self,
            "output_schema_version",
            _validate_schema_version(self.output_schema_version, "output_schema_version"),
        )
        object.__setattr__(self, "model", _validate_model(self.model))
        object.__setattr__(self, "toolset", _validate_toolset(self.toolset))

    @classmethod
    def from_descriptor(cls, descriptor: PromptDescriptor, *, rendered_hash: str) -> PromptMetadata:
        """Bind a static descriptor to the hash of one concrete rendering."""
        return cls(
            prompt_id=descriptor.prompt_id,
            version=descriptor.version,
            rendered_hash=rendered_hash,
            input_schema_version=descriptor.input_schema_version,
            output_schema_version=descriptor.output_schema_version,
            model=descriptor.model,
            toolset=descriptor.toolset,
        )
