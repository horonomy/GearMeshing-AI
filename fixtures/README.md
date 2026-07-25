# Fixtures

This directory holds repeatable engineering scenarios used to validate
GearMeshing-AI's workflow behavior, verification quality, and governance —
independent of any real production repository.

## Layout

- `sample_service/` — a small, self-contained, synthetic Python service
  ("fixture repository") with its own `pyproject.toml`, unit tests, and
  documented check commands (`pytest`, `ruff`). It has no dependency on the
  `gearmeshing_ai` package or the parent repository's tooling. See
  `sample_service/README.md` and `sample_service/NOTICE`.
- `golden_dataset/` — a directory of golden work-item definitions (one JSON
  file per item under `golden_dataset/items/`), each targeting a fixture
  repository above. See `golden_dataset/README.md`.

## Resetting a fixture

Fixtures are plain, version-controlled files with no external state, so
"resetting" a fixture means restoring its checked-in contents. Either
approach works:

```bash
# Re-check-out the fixture directory from git (discards local edits made
# while exercising a golden item against it).
git checkout -- fixtures/sample_service

# Or use the helper script, which does the same thing plus removes any
# stray untracked files left behind under the fixture.
./fixtures/reset_fixture.sh sample_service
```

Because `sample_service` has no database, network dependency, or shared
state, checking it back out to its committed contents is sufficient to
return it to a known-good starting point for the next evaluation run.

## Dataset versioning

`golden_dataset/DATASET_VERSION` records the current dataset revision. Every
item file under `golden_dataset/items/` embeds the same version in its
`dataset_version` field, and `gearmeshing_ai.testing.golden_dataset.load_golden_dataset`
raises if an item's version drifts from `DATASET_VERSION`. Evaluation code
should call `gearmeshing_ai.testing.golden_dataset.dataset_version()` and
record the result alongside its output so every evaluation run is
traceable to a specific dataset revision.

## Licensing and secrets

Everything under `fixtures/` is synthetic, toy code written for this
repository's own testing and evaluation purposes. `sample_service/NOTICE`
states its licensing position explicitly. No file under `fixtures/`
contains real credentials, API keys, or other secrets — golden dataset
items reference only placeholder `https://example.invalid/...` URLs and
synthetic Jira-style issue keys.
