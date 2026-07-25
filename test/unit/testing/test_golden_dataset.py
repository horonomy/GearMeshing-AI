"""Validate the golden work-item dataset against its loader schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from gearmeshing_ai.application.ports.coding_executor import FailureCategory, TerminalOutcome
from gearmeshing_ai.testing.golden_dataset import (
    GoldenDatasetError,
    ScenarioCategory,
    dataset_version,
    load_golden_dataset,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATASET_ROOT = _REPO_ROOT / "fixtures" / "golden_dataset"


def test_dataset_version_is_recorded() -> None:
    assert dataset_version(_DATASET_ROOT) == "1.0.0"


def test_load_golden_dataset_loads_every_item_file() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    item_files = sorted((_DATASET_ROOT / "items").glob("*.json"))
    assert len(items) == len(item_files)


def test_every_item_declares_the_recorded_dataset_version() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    expected = dataset_version(_DATASET_ROOT)
    assert all(item.dataset_version == expected for item in items)


def test_every_item_converts_to_a_canonical_work_item() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    for item in items:
        work_item = item.work_item.to_work_item()
        assert work_item.key == item.work_item.key


def test_every_item_work_item_content_hash_matches_canonical_content() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    for item in items:
        assert item.work_item.matches_canonical_content(), item.item_id


def test_every_item_has_a_valid_terminal_outcome_and_failure_category() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    for item in items:
        outcome = item.expected_outcome
        assert isinstance(outcome.terminal_outcome, TerminalOutcome)
        if outcome.terminal_outcome is TerminalOutcome.COMPLETED:
            assert outcome.failure_category is None
        else:
            assert isinstance(outcome.failure_category, FailureCategory)


def test_dataset_covers_every_required_scenario_category() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    categories = {item.scenario_category for item in items}
    assert categories == set(ScenarioCategory)


def test_dataset_includes_at_least_five_success_items() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    success_items = [item for item in items if item.scenario_category is ScenarioCategory.SUCCESS]
    assert len(success_items) >= 5


def test_blocked_item_has_expected_readiness_problems() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    blocked_items = [item for item in items if item.scenario_category is ScenarioCategory.BLOCKED]
    assert blocked_items
    for item in blocked_items:
        assert item.expected_readiness_problems
        assert item.expected_outcome.terminal_outcome is TerminalOutcome.BLOCKED
        assert item.expected_outcome.failure_category is FailureCategory.POLICY


def test_policy_denied_item_declares_forbidden_changes_and_touched_outcome() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    policy_items = [item for item in items if item.scenario_category is ScenarioCategory.POLICY_DENIED]
    assert policy_items
    for item in policy_items:
        assert item.forbidden_changes
        touched = item.expected_outcome_if_forbidden_changes_touched
        assert touched is not None
        assert touched.terminal_outcome is TerminalOutcome.BLOCKED
        assert touched.failure_category is FailureCategory.POLICY


def test_verification_failed_item_expects_a_failing_check() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    verification_items = [item for item in items if item.scenario_category is ScenarioCategory.VERIFICATION_FAILED]
    assert verification_items
    for item in verification_items:
        assert any(check.expected_result == "fail" for check in item.expected_checks)
        assert item.expected_outcome.terminal_outcome is TerminalOutcome.FAILED
        assert item.expected_outcome.failure_category is FailureCategory.VERIFICATION


def test_no_item_lists_a_forbidden_change_among_its_expected_changed_files() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    for item in items:
        assert not set(item.expected_changed_files) & set(item.forbidden_changes)


def test_item_ids_are_unique() -> None:
    items = load_golden_dataset(_DATASET_ROOT)
    item_ids = [item.item_id for item in items]
    assert len(item_ids) == len(set(item_ids))


def test_missing_dataset_version_file_raises(tmp_path: Path) -> None:
    (tmp_path / "items").mkdir()
    with pytest.raises(GoldenDatasetError, match="dataset version file not found"):
        load_golden_dataset(tmp_path)


def test_missing_items_directory_raises(tmp_path: Path) -> None:
    (tmp_path / "DATASET_VERSION").write_text("1.0.0\n", encoding="utf-8")
    with pytest.raises(GoldenDatasetError, match="items directory not found"):
        load_golden_dataset(tmp_path)


def test_empty_items_directory_raises(tmp_path: Path) -> None:
    (tmp_path / "DATASET_VERSION").write_text("1.0.0\n", encoding="utf-8")
    (tmp_path / "items").mkdir()
    with pytest.raises(GoldenDatasetError, match="no golden dataset items found"):
        load_golden_dataset(tmp_path)


def test_item_with_mismatched_dataset_version_raises(tmp_path: Path) -> None:
    (tmp_path / "DATASET_VERSION").write_text("1.0.0\n", encoding="utf-8")
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    source = _DATASET_ROOT / "items" / "001_doc_change_update_usage_notes.json"
    stale = source.read_text(encoding="utf-8").replace('"1.0.0"', '"9.9.9"')
    (items_dir / "001_stale.json").write_text(stale, encoding="utf-8")
    with pytest.raises(GoldenDatasetError, match="does not match DATASET_VERSION"):
        load_golden_dataset(tmp_path)


def test_item_with_invalid_json_raises(tmp_path: Path) -> None:
    (tmp_path / "DATASET_VERSION").write_text("1.0.0\n", encoding="utf-8")
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    (items_dir / "001_broken.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(GoldenDatasetError, match="invalid JSON"):
        load_golden_dataset(tmp_path)


def test_item_with_duplicate_item_ids_raises(tmp_path: Path) -> None:
    (tmp_path / "DATASET_VERSION").write_text("1.0.0\n", encoding="utf-8")
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    source = _DATASET_ROOT / "items" / "001_doc_change_update_usage_notes.json"
    payload = source.read_text(encoding="utf-8")
    (items_dir / "001_copy_a.json").write_text(payload, encoding="utf-8")
    (items_dir / "001_copy_b.json").write_text(payload, encoding="utf-8")
    with pytest.raises(GoldenDatasetError, match="unique item_id"):
        load_golden_dataset(tmp_path)
