<!-- horonom:generated -->
<!-- Source: horonomy/.github agents/skills/python-development/SKILL.md. Do not hand-edit — rerun `python3 agents/common/project_skills.py`. -->

# SKILL.md — python-development

## Purpose

Supply the concrete uv/pytest/Ruff/type-checker commands for a Python repo.
This skill owns the language-specific commands referenced by
`governance/engineering/agent-skill-architecture.md` (HORO-969) §1 — it does
not own the execution loop, the L0–L3 diagnostic contract, or the optional-
tool preferred→fallback semantics. Those live in `engineering-loop`; compose
with it rather than restating it here.

## Type

Auto-used. Applies to any repo/task where Python evidence is present
(`pyproject.toml`, `uv.lock` — see the manifest's applicability stacks and
`governance/engineering/agent-skill-architecture.md` §3 for the detection
order). Composes with `engineering-loop` for every stage below.

## When to use

- Iterating on a Python change: writing/running/debugging pytest, Ruff, or
  a type checker.
- Validating a Python package before calling a change done (build/install
  smoke check, when the repo ships a wheel or an installable package).

## When NOT to use

- As a substitute for `engineering-loop`'s Explore/Narrow/Validate/
  Escalate/Full Gate model — this skill fills that model in with real
  commands, it doesn't replace it.
- On a repo with no Python evidence at all — a Python skill projected into
  a pure-Go repo is exactly the "every repo receives every skill" mistake
  HORO-969 §3 forbids.
- As the sole language skill for a native-extension repo (PyO3, Cython, a
  bundled Rust sidecar) — see Composition below.

## Routing decision

1. **Discover the repo/venv first** — don't assume a global interpreter or
   an already-active venv. See `references/uv-pytest-workflow.md` for
   `pyproject.toml`/`uv.lock` discovery and `uv run`/`uv sync` usage.
2. **Narrow stage → targeted pytest.** Run the smallest node id that proves
   or disproves the change (one test, one class, one file) — never the full
   suite while iterating. See `references/uv-pytest-workflow.md` for exact
   flag examples.
3. **Lint/format targeted, then full.** Run Ruff against only the touched
   file(s) during the loop; run the full-repo Ruff check as part of Full
   Gate.
4. **Type-check with whatever the repo actually configures.** Never assume
   mypy — read the repo's own config (`mypy.ini`, `pyproject.toml`
   `[tool.mypy]`/`[tool.pyright]`/`[tool.ty]`) and run that tool.
5. **Full Gate**: full pytest + full Ruff (lint and format check) + the
   repo's configured type checker, plus a build/install smoke check when
   the repo has a wheel or service boundary (see
   `references/uv-pytest-workflow.md`).
6. **Before trusting "no callers found" from a static tool**, read
   `references/dynamic-python-caveats.md` — Python's dynamic-dispatch
   surface (reflection, `importlib`, decorators, plugin registries,
   `conftest.py` fixtures, monkeypatching) makes static graphs
   systematically incomplete in ways a compiled language's aren't; the
   actual test run is still the proof, per `engineering-loop`'s
   optional-tool-fallback rule against treating navigation as correctness
   proof.

## Composition

- **PyO3/native-extension repos** (a Rust extension built with maturin, a
  Cython module, any repo where Python packaging wraps compiled code)
  additionally compose `rust-development` for the Cargo/nextest/clippy side
  of the same change — this skill still owns the Python-facing pytest/Ruff/
  type-check workflow, `rust-development` owns the Rust-facing one, and
  `engineering-loop` supplies the shared loop both sides run inside.
- Always compose with `engineering-loop` — see its `SKILL.md` for the
  execution model and the L0–L3 diagnostic contract.

## References and examples

- `references/uv-pytest-workflow.md` — repo/venv discovery, targeted
  pytest, targeted vs. full Ruff, respecting the repo's configured type
  checker, build/install smoke validation.
- `references/dynamic-python-caveats.md` — why Python's dynamic surface
  defeats static-only impact analysis, and what to run instead.
- `examples/targeted-pytest-then-full-gate.md` — a worked failing-test →
  targeted iteration → Full Gate example.
