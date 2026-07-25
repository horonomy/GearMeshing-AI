"""Load repository prompt files and render them into governed invocations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template
from jinja2.exceptions import TemplateSyntaxError, UndefinedError

from gearmeshing_ai.application.prompts.metadata import (
    PromptDescriptor,
    PromptMetadata,
    PromptValidationError,
)
from gearmeshing_ai.application.prompts.untrusted import wrap_untrusted

_FRONTMATTER_DELIMITER = "---"
_FRAGMENTS_DIRECTORY_NAME = "fragments"
_FRONTMATTER_FIELDS = frozenset(
    {"prompt_id", "version", "input_schema_version", "output_schema_version", "model", "toolset"}
)

DEFAULT_PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"


class PromptNotFoundError(PromptValidationError):
    """Raised when a requested prompt_id or version is not registered."""


class PromptRenderError(PromptValidationError):
    """Raised when a template fails to render, e.g. a missing variable."""


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """Rendered prompt text bound to the exact metadata that produced it."""

    text: str
    metadata: PromptMetadata


def _split_frontmatter(raw_source: str, *, source_name: str) -> tuple[Mapping[str, Any], str]:
    lines = raw_source.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise PromptValidationError(f"{source_name} must begin with a '---' frontmatter block")
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            frontmatter_text = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            loaded = yaml.safe_load(frontmatter_text) if frontmatter_text.strip() else None
            if not isinstance(loaded, dict):
                raise PromptValidationError(f"{source_name} frontmatter must be a YAML mapping")
            return loaded, body
    raise PromptValidationError(f"{source_name} frontmatter block is not terminated with '---'")


def _build_descriptor(frontmatter: Mapping[str, Any], *, source_name: str) -> PromptDescriptor:
    missing = _FRONTMATTER_FIELDS - frontmatter.keys()
    if missing:
        raise PromptValidationError(f"{source_name} frontmatter is missing fields: {sorted(missing)}")
    extra = frontmatter.keys() - _FRONTMATTER_FIELDS
    if extra:
        raise PromptValidationError(f"{source_name} frontmatter has unsupported fields: {sorted(extra)}")
    toolset = frontmatter["toolset"]
    if not isinstance(toolset, list) or not all(isinstance(entry, str) for entry in toolset):
        raise PromptValidationError(f"{source_name} frontmatter toolset must be a list of strings")
    try:
        return PromptDescriptor(
            prompt_id=str(frontmatter["prompt_id"]),
            version=str(frontmatter["version"]),
            input_schema_version=str(frontmatter["input_schema_version"]),
            output_schema_version=str(frontmatter["output_schema_version"]),
            model=str(frontmatter["model"]),
            toolset=tuple(toolset),
        )
    except PromptValidationError as error:
        raise PromptValidationError(f"{source_name} frontmatter is invalid: {error}") from error


def _version_sort_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch)


class PromptRegistry:
    """Loads, validates, and renders every prompt file under one root directory.

    All prompt files are parsed and validated eagerly on construction --
    a malformed template or an invalid metadata sidecar fails fast at
    startup rather than at first use.
    """

    def __init__(self, prompts_root: Path = DEFAULT_PROMPTS_ROOT) -> None:
        self._prompts_root = prompts_root
        self._environment = Environment(
            loader=FileSystemLoader(str(prompts_root)),
            undefined=StrictUndefined,
            # These templates render plain text for an LLM prompt, never HTML for a browser, so
            # there is no XSS surface here; HTML entity escaping would instead corrupt prompt
            # content (e.g. quotes, ampersands in a diff or Jira description).
            autoescape=False,  # NOSONAR
            keep_trailing_newline=True,
        )
        self._environment.filters["untrusted"] = wrap_untrusted
        self._descriptors: dict[tuple[str, str], PromptDescriptor] = {}
        self._templates: dict[tuple[str, str], Template] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self._prompts_root.is_dir():
            raise PromptValidationError(f"prompts root {self._prompts_root} is not a directory")
        for prompt_directory in sorted(self._prompts_root.iterdir()):
            if not prompt_directory.is_dir() or prompt_directory.name == _FRAGMENTS_DIRECTORY_NAME:
                continue
            self._load_prompt_directory(prompt_directory)

    def _load_prompt_directory(self, prompt_directory: Path) -> None:
        expected_prompt_id = prompt_directory.name
        for prompt_file in sorted(prompt_directory.glob("*.md")):
            relative_path = prompt_file.relative_to(self._prompts_root).as_posix()
            raw_source = prompt_file.read_text(encoding="utf-8")
            frontmatter, body = _split_frontmatter(raw_source, source_name=relative_path)
            descriptor = _build_descriptor(frontmatter, source_name=relative_path)
            if descriptor.prompt_id != expected_prompt_id:
                raise PromptValidationError(
                    f"{relative_path} declares prompt_id {descriptor.prompt_id!r} "
                    f"but lives under directory {expected_prompt_id!r}"
                )
            expected_filename = f"v{descriptor.version}.md"
            if prompt_file.name != expected_filename:
                raise PromptValidationError(
                    f"{relative_path} declares version {descriptor.version!r} "
                    f"but its filename must be {expected_filename!r}"
                )
            key = (descriptor.prompt_id, descriptor.version)
            if key in self._descriptors:
                raise PromptValidationError(f"duplicate prompt registration for {key}")
            try:
                # `body` is repository-authored prompt source (a tracked .md file under
                # DEFAULT_PROMPTS_ROOT), not attacker-controlled HTML: this environment's
                # autoescape=False above is a plain-text-prompt choice, not an HTML-injection risk.
                template = self._environment.from_string(body)  # NOSONAR
            except TemplateSyntaxError as error:
                raise PromptValidationError(f"{relative_path} has an invalid Jinja2 template: {error}") from error
            self._descriptors[key] = descriptor
            self._templates[key] = template

    def known_prompt_ids(self) -> tuple[str, ...]:
        return tuple(sorted({prompt_id for prompt_id, _ in self._descriptors}))

    def known_versions(self, prompt_id: str) -> tuple[str, ...]:
        versions = [version for known_prompt_id, version in self._descriptors if known_prompt_id == prompt_id]
        if not versions:
            raise PromptNotFoundError(f"no versions registered for prompt_id {prompt_id!r}")
        return tuple(sorted(versions, key=_version_sort_key))

    def _resolve_version(self, prompt_id: str, version: str | None) -> str:
        if version is not None:
            return version
        return self.known_versions(prompt_id)[-1]

    def render(self, prompt_id: str, *, version: str | None = None, **variables: object) -> RenderedPrompt:
        """Render one prompt's Jinja2 template and bind the result to its metadata.

        If ``version`` is omitted, the highest registered semantic version
        for ``prompt_id`` is used. Missing template variables raise
        ``PromptRenderError`` instead of silently rendering blank, because
        the environment is configured with ``StrictUndefined``.
        """
        resolved_version = self._resolve_version(prompt_id, version)
        key = (prompt_id, resolved_version)
        if key not in self._descriptors:
            raise PromptNotFoundError(f"no prompt registered for prompt_id={prompt_id!r} version={resolved_version!r}")
        descriptor = self._descriptors[key]
        template = self._templates[key]
        try:
            text = template.render(**variables)
        except UndefinedError as error:
            raise PromptRenderError(
                f"{prompt_id} v{resolved_version} is missing a template variable: {error}"
            ) from error
        rendered_hash = sha256(text.encode("utf-8")).hexdigest()
        metadata = PromptMetadata.from_descriptor(descriptor, rendered_hash=rendered_hash)
        return RenderedPrompt(text=text, metadata=metadata)
