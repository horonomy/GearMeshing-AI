# Reference — why static analysis is incomplete for Python

`engineering-loop`'s optional-tool-fallback contract already forbids
treating static/navigation evidence as correctness proof in general. This
reference is the Python-specific instance of that rule: Python's dynamic
surface makes "no callers found" wrong more often, and in less obvious
ways, than the same claim in a statically-dispatched language.

## Why a static call graph misses real callers

A static graph (CodeGraph or otherwise) is built from what it can see in
source text — literal `import`, literal attribute access, literal
function calls. Several ordinary Python patterns never appear as any of
those:

- **`importlib.import_module()` / `__import__()` with a computed string.**
  A plugin loader that does `importlib.import_module(f"myapp.plugins.{name}")`
  has no static edge to the module it loads — the module name only exists
  at runtime.
- **`getattr()`/`setattr()` dispatch.** `getattr(handler, f"handle_{event_type}")()`
  calls a method whose name is never written anywhere as a literal call
  site. Renaming or removing `handle_refund` shows zero static callers even
  though it's live.
- **Decorators that register into a side table.** `@app.route("/x")`,
  `@click.command()`, `@pytest.fixture`, `@dataclass`, a custom
  `@plugin_registry.register` — the decorated function is invoked later by
  a framework's internal dispatch, not by a call expression the function's
  own module contains. A static graph sees the decoration but not the
  eventual invocation path.
- **Plugin/entry-point registries.** `pyproject.toml`
  `[project.entry-points]`, `pkg_resources`/`importlib.metadata` entry
  point discovery, a `setuptools` plugin system — the loader walks a
  registry built at install time, not at parse time.
- **`conftest.py` fixtures and pytest's dependency injection.** A fixture
  is "called" by pytest matching its name against a test function's
  parameter list, not by any call expression. `def test_x(db_session):`
  has no static edge to the `db_session` fixture definition; removing or
  changing the fixture's behavior can silently affect every test that
  merely names the parameter.
- **Monkeypatching and mocking.** `monkeypatch.setattr(module, "func",
  fake)` in a test, or a `mock.patch("app.service.send_email")` decorator,
  rewrites what a call site resolves to at runtime. A static graph shows
  the original `send_email` definition as the callee; the test's actual
  behavior may depend on a completely different substituted implementation.
- **`__getattr__`/`__init_subclass__`/metaclasses.** Module-level
  `__getattr__` (PEP 562) and class-level dynamic attribute machinery mean
  an attribute access that looks like it must fail statically can succeed
  at runtime, and vice versa.
- **Reflection over `globals()`/`locals()`/`vars()`.** Code that iterates
  `globals()` looking for names matching a pattern (e.g. all `Command`
  subclasses in a module) has no static edge to the specific names it will
  find.

## The practical consequence

None of the above is exotic — pytest fixtures, Click/Flask/FastAPI
decorators, and `entry_points`-based plugin systems are mainstream, not
edge cases. That means for a typical Python repo, "CodeGraph/grep found no
callers of this function" is **evidence that helps you decide where to
look next**, never proof the function is dead or that a change to it is
safe.

What this means for the loop in practice:

1. **Don't delete or change a function's contract solely because a static
   search found no callers.** Search for the name as a *string* too
   (decorator arguments, entry-point tables, `getattr` targets,
   `conftest.py`), not only as a call expression.
2. **Run the actual test suite, not the graph, as proof.** A change that a
   static graph says is isolated must still pass the real Narrow/Full Gate
   checks from `references/uv-pytest-workflow.md` — the test suite exercises
   the dynamic dispatch paths a static tool can't see (fixtures actually
   resolving, decorators actually registering, plugins actually loading).
3. **Treat "no callers found" as a prompt to broaden the search, not a
   conclusion.** If a function is decorated, registered, or named
   dynamically, the burden is on demonstrating it's actually unused (e.g.
   grepping for its literal name as a string, checking entry-point
   manifests, running the full suite with the function's implementation
   deliberately broken to see what fails) — not on the absence of a static
   edge.
4. **This is exactly `engineering-loop`'s "treating navigation as proof"
   failure mode, applied to Python.** The fix is the same one that
   reference prescribes: use the graph/grep to decide where to look, let
   the actual test/runtime gate decide whether the change is safe.
