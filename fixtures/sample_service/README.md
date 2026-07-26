# sample_service (fixture)

A small, synthetic, self-contained Python service used only as a golden
dataset fixture for GearMeshing-AI. See `../../fixtures/README.md` for how
this fixture is reset and consumed, and `NOTICE` for the licensing note.

## Layout

- `inventory_service/` — toy catalog and pricing helpers (no real business
  logic).
- `tests/` — unit tests for the fixture package.
- `docs/USAGE.md` — short usage notes for the fixture package.

## Running checks locally

These commands are scoped to this directory only; they are independent of
the parent repository's own `uv`-managed environment and quality gate.

```bash
cd fixtures/sample_service
python3 -m venv .venv && source .venv/bin/activate
pip install pytest ruff
pytest -q
ruff check .
ruff format --check .
```
