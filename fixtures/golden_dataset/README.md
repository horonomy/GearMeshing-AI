# Golden work-item dataset

One JSON file per golden item under `items/`, loaded and validated by
`gearmeshing_ai.testing.golden_dataset.load_golden_dataset`.

## Item shape

Each item file contains:

- `dataset_version` — must match `../DATASET_VERSION`.
- `item_id` — unique identifier for the item.
- `scenario_category` — one of `success`, `blocked`, `policy_denied`,
  `verification_failed`.
- `fixture_repository` — the fixture directory name under `../` (e.g.
  `sample_service`) the item targets.
- `work_item` — a documented subset of
  `gearmeshing_ai.application.ports.work_management.WorkItem` (see
  `GoldenWorkItem` in the loader module). `content_sha256` must be the
  SHA-256 of `canonical_work_item_content(title, description,
  acceptance_criteria)`, exactly as a real `WorkManagementProvider` would
  populate it.
- `expected_changed_files` — paths (relative to the repository root) the
  item's change is expected to touch.
- `forbidden_changes` — paths that must never be touched; this is the
  prohibited-action / policy-denied enforcement mechanism.
- `expected_checks` — check commands and their expected pass/fail result.
- `expected_outcome` — `terminal_outcome` and `failure_category` drawn
  directly from `gearmeshing_ai.application.ports.coding_executor`
  (`TerminalOutcome` / `FailureCategory`), plus free-form `notes`.
- `expected_readiness_problems` (blocked items only) — readiness problem
  codes the item is expected to surface, mirroring
  `JiraWorkManagementProvider._evaluate_readiness`.
- `expected_outcome_if_forbidden_changes_touched` (policy-denied items
  only) — the outcome evaluation must report if a hypothetical executor
  violates `forbidden_changes` despite the item otherwise looking
  compliant.

## Required coverage

The dataset must include at least:

1. A documentation-only change (`success`).
2. A bug fix (`success`).
3. A unit-test-addition task (`success`).
4. A small refactor (`success`).
5. A simple, additive API change (`success`).
6. An incomplete/underspecified item that resolves to `blocked`.
7. A prohibited-action item (`policy_denied`).
8. A verification-failure / remediation-needed scenario
   (`verification_failed`).

`test/unit/testing/test_golden_dataset.py` enforces this coverage and
validates every item against the loader's schema.
