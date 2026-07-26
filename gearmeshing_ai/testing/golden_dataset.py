"""Loader for the golden work-item dataset used by evaluation and tests.

The dataset lives under ``fixtures/golden_dataset`` as one JSON file per
item plus a ``DATASET_VERSION`` file. This module parses those files into
typed, validated dataclasses so malformed fixture data is caught early, and
exposes the dataset version so evaluation results can record which dataset
revision produced them.

Each golden item's ``work_item`` section is a documented subset of
``gearmeshing_ai.application.ports.work_management.WorkItem`` (see
``GoldenWorkItem.to_work_item``), and each item's ``expected_outcome`` uses
the ``TerminalOutcome``/``FailureCategory`` vocabulary from
``gearmeshing_ai.application.ports.coding_executor`` rather than inventing a
parallel one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from gearmeshing_ai.application.ports.coding_executor import FailureCategory, TerminalOutcome
from gearmeshing_ai.application.ports.work_management import (
    Metadata,
    RepositoryReference,
    WorkItem,
    canonical_work_item_content,
)

_DEFAULT_DATASET_ROOT: Final = Path(__file__).resolve().parents[2] / "fixtures" / "golden_dataset"


class GoldenDatasetError(ValueError):
    """Raised when the golden dataset directory or an item is malformed."""


class ScenarioCategory(StrEnum):
    """Coverage categories the golden dataset is required to include."""

    SUCCESS = "success"
    BLOCKED = "blocked"
    POLICY_DENIED = "policy_denied"
    VERIFICATION_FAILED = "verification_failed"


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GoldenDatasetError(f"{context} must be a JSON object")
    return value


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenDatasetError(f"{context} must be a non-empty string")
    return value


def _require_str_sequence(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise GoldenDatasetError(f"{context} must be a list of strings")
    items = tuple(value)
    if not all(isinstance(item, str) and item for item in items):
        raise GoldenDatasetError(f"{context} must contain only non-empty strings")
    return items


@dataclass(frozen=True, slots=True)
class GoldenRepositoryReference:
    """Documented subset of ``RepositoryReference`` used by golden items."""

    provider: str
    owner: str
    name: str
    web_url: str

    def to_repository_reference(self) -> RepositoryReference:
        return RepositoryReference(
            provider=self.provider,
            owner=self.owner,
            name=self.name,
            web_url=self.web_url,
        )


@dataclass(frozen=True, slots=True)
class GoldenWorkItem:
    """Documented subset of ``WorkItem`` embedded in a golden dataset item."""

    key: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: str
    web_url: str
    repository: GoldenRepositoryReference | None
    revision: str
    content_sha256: str
    labels: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_work_item(self) -> WorkItem:
        """Build the canonical, provider-neutral ``WorkItem`` for this golden item."""
        return WorkItem(
            key=self.key,
            title=self.title,
            description=self.description,
            acceptance_criteria=self.acceptance_criteria,
            status=self.status,
            web_url=self.web_url,
            repository=(self.repository.to_repository_reference() if self.repository is not None else None),
            revision=self.revision,
            content_sha256=self.content_sha256,
            labels=self.labels,
            metadata=Metadata(self.metadata),
        )

    def matches_canonical_content(self) -> bool:
        """Return whether ``content_sha256`` matches the canonical content digest.

        Uses the same ``canonical_work_item_content`` helper that a real
        ``WorkManagementProvider`` and coding-executor caller must agree on.
        """
        from hashlib import sha256

        canonical = canonical_work_item_content(self.title, self.description, self.acceptance_criteria)
        return sha256(canonical.encode()).hexdigest() == self.content_sha256.strip().lower()


@dataclass(frozen=True, slots=True)
class ExpectedCheck:
    """One machine-readable check a golden item expects to pass or fail."""

    command: str
    working_directory: str
    expected_result: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.expected_result not in {"pass", "fail"}:
            raise GoldenDatasetError("expected_checks[].expected_result must be 'pass' or 'fail'")


@dataclass(frozen=True, slots=True)
class ExpectedReadinessProblem:
    """A readiness-check code a blocked golden item must surface.

    Mirrors the problem codes reported by
    ``JiraWorkManagementProvider._evaluate_readiness``.
    """

    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    """Expected terminal outcome using the ``coding_executor`` vocabulary."""

    terminal_outcome: TerminalOutcome
    failure_category: FailureCategory | None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class GoldenDatasetItem:
    """One fully-typed, validated golden work-item scenario."""

    dataset_version: str
    item_id: str
    scenario_category: ScenarioCategory
    fixture_repository: str
    work_item: GoldenWorkItem
    expected_changed_files: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    expected_checks: tuple[ExpectedCheck, ...]
    expected_outcome: ExpectedOutcome
    expected_readiness_problems: tuple[ExpectedReadinessProblem, ...] = ()
    expected_outcome_if_forbidden_changes_touched: ExpectedOutcome | None = None

    def __post_init__(self) -> None:
        overlap = set(self.expected_changed_files) & set(self.forbidden_changes)
        if overlap:
            raise GoldenDatasetError(
                f"{self.item_id}: expected_changed_files and forbidden_changes overlap on {sorted(overlap)}"
            )


def _parse_repository(raw: object, context: str) -> GoldenRepositoryReference | None:
    if raw is None:
        return None
    payload = _require_mapping(raw, context)
    return GoldenRepositoryReference(
        provider=_require_str(payload.get("provider"), f"{context}.provider"),
        owner=_require_str(payload.get("owner"), f"{context}.owner"),
        name=_require_str(payload.get("name"), f"{context}.name"),
        web_url=_require_str(payload.get("web_url"), f"{context}.web_url"),
    )


def _parse_work_item(raw: object, context: str) -> GoldenWorkItem:
    payload = _require_mapping(raw, context)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise GoldenDatasetError(f"{context}.metadata must be a JSON object")
    return GoldenWorkItem(
        key=_require_str(payload.get("key"), f"{context}.key"),
        title=_require_str(payload.get("title"), f"{context}.title"),
        description=_require_str(payload.get("description"), f"{context}.description"),
        acceptance_criteria=tuple(
            _require_str_sequence(payload.get("acceptance_criteria", []), f"{context}.acceptance_criteria")
        ),
        status=_require_str(payload.get("status"), f"{context}.status"),
        web_url=_require_str(payload.get("web_url"), f"{context}.web_url"),
        repository=_parse_repository(payload.get("repository"), f"{context}.repository"),
        revision=_require_str(payload.get("revision"), f"{context}.revision"),
        content_sha256=_require_str(payload.get("content_sha256"), f"{context}.content_sha256"),
        labels=_require_str_sequence(payload.get("labels", []), f"{context}.labels"),
        metadata=dict(metadata),
    )


def _parse_expected_checks(raw: object, context: str) -> tuple[ExpectedCheck, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise GoldenDatasetError(f"{context} must be a list")
    checks: list[ExpectedCheck] = []
    for index, entry in enumerate(raw):
        payload = _require_mapping(entry, f"{context}[{index}]")
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise GoldenDatasetError(f"{context}[{index}].reason must be a string when present")
        checks.append(
            ExpectedCheck(
                command=_require_str(payload.get("command"), f"{context}[{index}].command"),
                working_directory=_require_str(
                    payload.get("working_directory"), f"{context}[{index}].working_directory"
                ),
                expected_result=_require_str(payload.get("expected_result"), f"{context}[{index}].expected_result"),
                reason=reason,
            )
        )
    return tuple(checks)


def _parse_expected_readiness_problems(raw: object, context: str) -> tuple[ExpectedReadinessProblem, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise GoldenDatasetError(f"{context} must be a list")
    problems: list[ExpectedReadinessProblem] = []
    for index, entry in enumerate(raw):
        payload = _require_mapping(entry, f"{context}[{index}]")
        problems.append(
            ExpectedReadinessProblem(
                code=_require_str(payload.get("code"), f"{context}[{index}].code"),
                reason=_require_str(payload.get("reason"), f"{context}[{index}].reason"),
            )
        )
    return tuple(problems)


def _parse_expected_outcome(raw: object, context: str) -> ExpectedOutcome:
    payload = _require_mapping(raw, context)
    outcome_value = _require_str(payload.get("terminal_outcome"), f"{context}.terminal_outcome")
    try:
        terminal_outcome = TerminalOutcome(outcome_value)
    except ValueError as error:
        raise GoldenDatasetError(f"{context}.terminal_outcome {outcome_value!r} is not a TerminalOutcome") from error
    failure_category_raw = payload.get("failure_category")
    failure_category: FailureCategory | None
    if failure_category_raw is None:
        failure_category = None
    else:
        if not isinstance(failure_category_raw, str):
            raise GoldenDatasetError(f"{context}.failure_category must be a string or null")
        try:
            failure_category = FailureCategory(failure_category_raw)
        except ValueError as error:
            raise GoldenDatasetError(
                f"{context}.failure_category {failure_category_raw!r} is not a FailureCategory"
            ) from error
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise GoldenDatasetError(f"{context}.notes must be a string")
    if terminal_outcome is TerminalOutcome.COMPLETED and failure_category is not None:
        raise GoldenDatasetError(f"{context}: completed outcomes must not specify a failure_category")
    if terminal_outcome is not TerminalOutcome.COMPLETED and failure_category is None:
        raise GoldenDatasetError(f"{context}: non-completed outcomes must specify a failure_category")
    return ExpectedOutcome(terminal_outcome=terminal_outcome, failure_category=failure_category, notes=notes)


def _parse_item(payload: Mapping[str, object], *, source: Path) -> GoldenDatasetItem:
    context = str(source)
    scenario_value = _require_str(payload.get("scenario_category"), f"{context}.scenario_category")
    try:
        scenario_category = ScenarioCategory(scenario_value)
    except ValueError as error:
        raise GoldenDatasetError(f"{context}.scenario_category {scenario_value!r} is not a ScenarioCategory") from error
    raw_touched = payload.get("expected_outcome_if_forbidden_changes_touched")
    return GoldenDatasetItem(
        dataset_version=_require_str(payload.get("dataset_version"), f"{context}.dataset_version"),
        item_id=_require_str(payload.get("item_id"), f"{context}.item_id"),
        scenario_category=scenario_category,
        fixture_repository=_require_str(payload.get("fixture_repository"), f"{context}.fixture_repository"),
        work_item=_parse_work_item(payload.get("work_item"), f"{context}.work_item"),
        expected_changed_files=_require_str_sequence(
            payload.get("expected_changed_files", []), f"{context}.expected_changed_files"
        ),
        forbidden_changes=_require_str_sequence(payload.get("forbidden_changes", []), f"{context}.forbidden_changes"),
        expected_checks=_parse_expected_checks(payload.get("expected_checks", []), f"{context}.expected_checks"),
        expected_outcome=_parse_expected_outcome(payload.get("expected_outcome"), f"{context}.expected_outcome"),
        expected_readiness_problems=_parse_expected_readiness_problems(
            payload.get("expected_readiness_problems", []), f"{context}.expected_readiness_problems"
        ),
        expected_outcome_if_forbidden_changes_touched=(
            _parse_expected_outcome(raw_touched, f"{context}.expected_outcome_if_forbidden_changes_touched")
            if raw_touched is not None
            else None
        ),
    )


def dataset_version(dataset_root: Path | None = None) -> str:
    """Return the recorded golden dataset version.

    Evaluation code should call this and record the result alongside
    evaluation output so results are traceable to a specific dataset
    revision.
    """
    root = dataset_root or _DEFAULT_DATASET_ROOT
    version_file = root / "DATASET_VERSION"
    if not version_file.is_file():
        raise GoldenDatasetError(f"dataset version file not found at {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise GoldenDatasetError(f"{version_file} must not be empty")
    return version


def load_golden_dataset(dataset_root: Path | None = None) -> tuple[GoldenDatasetItem, ...]:
    """Load, parse, and validate every golden dataset item.

    Raises ``GoldenDatasetError`` if the dataset directory is missing, an
    item file is malformed, or an item's ``dataset_version`` disagrees with
    the recorded ``DATASET_VERSION``.
    """
    root = dataset_root or _DEFAULT_DATASET_ROOT
    items_dir = root / "items"
    if not items_dir.is_dir():
        raise GoldenDatasetError(f"golden dataset items directory not found at {items_dir}")
    expected_version = dataset_version(root)
    item_paths = sorted(items_dir.glob("*.json"))
    if not item_paths:
        raise GoldenDatasetError(f"no golden dataset items found under {items_dir}")
    items: list[GoldenDatasetItem] = []
    for path in item_paths:
        try:
            raw_payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise GoldenDatasetError(f"{path}: invalid JSON ({error})") from error
        payload = _require_mapping(raw_payload, str(path))
        item = _parse_item(payload, source=path)
        if item.dataset_version != expected_version:
            raise GoldenDatasetError(
                f"{path}: dataset_version {item.dataset_version!r} does not match DATASET_VERSION {expected_version!r}"
            )
        items.append(item)
    item_ids = [item.item_id for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise GoldenDatasetError("golden dataset items must have unique item_id values")
    return tuple(items)
