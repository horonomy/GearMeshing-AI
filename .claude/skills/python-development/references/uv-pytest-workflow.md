# Reference — uv/pytest/Ruff/type-checker workflow

Concrete commands for the Narrow/Full Gate stages `engineering-loop` calls
for; this reference doesn't restate that model, only fills it in.

## 1. Repo and venv discovery

Before running anything, confirm what you're actually operating on:

- `pyproject.toml` at the repo root (or a subproject root in a monorepo) is
  the primary signal — read `[project]` for `requires-python` and
  dependencies, `[tool.uv]` for workspace/source settings.
- `uv.lock` next to it means the repo pins with `uv` — use `uv run`/
  `uv sync`, never a bare `pip install` or an assumed already-active venv.
  `uv sync` (or `uv sync --all-extras` if the repo's dev workflow needs
  optional groups) materializes the locked environment; `uv run <cmd>`
  executes inside it without a manual `source .venv/bin/activate` step.
- A monorepo may have more than one `pyproject.toml` (e.g. a `python-sdk/`
  subdirectory alongside a Rust workspace) — resolve the nearest one to the
  file you're touching, don't assume the repo root's is the only one.
- If `uv.lock` is absent but `pyproject.toml` exists, the repo may use
  Poetry, plain pip, or another manager — check `[build-system]` and any
  `poetry.lock`/`requirements*.txt` before assuming uv.

## 2. Targeted pytest during the edit loop

Never run the full suite while iterating on one fix — run only the node id
that proves or disproves the change:

```bash
# One file
uv run pytest tests/http/test_backoff.py -q

# One class
uv run pytest tests/http/test_backoff.py::TestRetryAfter -q

# One test by name (node id)
uv run pytest tests/http/test_backoff.py::TestRetryAfter::test_parses_http_date -q

# By keyword substring across the whole tree — useful when the exact node
# id isn't known yet
uv run pytest -k "retry_after and http_date" -q

# By marker, when the repo defines them (pytest.ini / pyproject.toml
# [tool.pytest.ini_options] markers = [...])
uv run pytest -m "not slow" -q
```

`-q` keeps L0 output compact (PASS/FAIL + counts); drop it, or add
`-v`/`--tb=long`, to escalate toward L1/L2 per `engineering-loop`'s
diagnostic contract. `--tb=short` is a reasonable L1/L2 middle ground for a
first escalation before reaching for `-v --tb=long` or a full traceback.

## 3. Ruff — targeted vs. full-repo

Ruff does both linting and formatting; run it narrow during the loop and
full at the gate:

```bash
# Targeted, during the loop — only the file(s) just touched
uv run ruff check app/http/backoff.py
uv run ruff format --check app/http/backoff.py

# Full repo, at Full Gate
uv run ruff check .
uv run ruff format --check .
```

Config lives in `pyproject.toml`'s `[tool.ruff]` or a standalone
`ruff.toml`/`.ruff.toml` — read it before assuming a default rule set;
repos commonly narrow or extend the selected rules (`select`, `ignore`,
per-file overrides) there.

## 4. Type checker — use whatever the repo actually configures

Do not assume mypy. Check, in order, for the repo's own configuration:

- `mypy.ini`, or `[tool.mypy]` in `pyproject.toml` → `uv run mypy <path>`.
- `pyrightconfig.json`, or `[tool.pyright]` in `pyproject.toml` → `uv run
  pyright <path>` (or the repo's own invocation, e.g. via `npx pyright` if
  it's vendored through a Node toolchain in a mixed repo).
- `[tool.ty]` in `pyproject.toml` (Astral's `ty`) → `uv run ty check
  <path>`.
- If more than one is configured, run the one CI actually gates on — check
  the repo's CI workflow file rather than guessing when configs conflict.

Targeted, during the loop, type-check only the touched module(s) when the
tool supports scoping (`mypy app/http/backoff.py`); run the full configured
path at Full Gate (`mypy app/` or whatever the repo's own invocation is).

## 5. Full Gate

Before calling a Python change done, run all of:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy app/   # or pyright/ty — see §4
```

## 6. Build/install smoke validation, when applicable

Not every Python repo ships a wheel or a service boundary — a pure
internal script doesn't need this step. When it does (a published package,
a native-extension build, a service with an entrypoint), add a smoke check
appropriate to the boundary:

- **Pure-Python package**: `uv build` then `uv run --with dist/*.whl
  python -c "import <package>"` (or the repo's documented smoke-import) to
  confirm the built artifact actually imports, not just that source does.
- **Native-extension package** (maturin/PyO3, Cython): building the wheel
  is itself a compile step — a source-only pytest run against an editable
  install does not prove the *shipped* wheel works. Build with `maturin
  build` (or the repo's documented `uv run maturin develop` for local
  iteration), then run the smoke import against the built wheel. See
  `rust-development` for the Cargo-side build/test story on the same
  change — this is a compose point, not something this skill re-implements.
- **Service with an entrypoint** (`[project.scripts]`): run the installed
  console script's `--help` or an equivalent minimal invocation after
  install, to catch an entrypoint that imports fine in source but breaks
  once packaged (missing data file, wrong `packages`/`include` glob in
  `pyproject.toml`, etc.).
