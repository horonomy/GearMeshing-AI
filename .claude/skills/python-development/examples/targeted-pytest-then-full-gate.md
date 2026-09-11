# Example — targeted pytest iteration, then Full Gate

Scenario: a bug in a retry-budget helper. `allocate_retry_budget()` should
split a total timeout across a bounded number of attempts, but returns a
budget of `0` for the last attempt when the timeout doesn't divide evenly,
causing that attempt to fail instantly instead of getting its fair share.

Module under fix, `app/http/retry_budget.py`:

```python
def allocate_retry_budget(total_timeout: float, max_attempts: int) -> list[float]:
    """Split total_timeout across max_attempts, each attempt's slice in seconds."""
    per_attempt = total_timeout // max_attempts  # bug: integer division drops the remainder
    return [per_attempt] * max_attempts
```

## 1. Explore

```
$ codegraph explore "allocate_retry_budget"
```

Returns the function (`app/http/retry_budget.py:1-5`) and its one caller,
`app/http/client.py:88`, plus the existing test module
`tests/http/test_retry_budget.py`. (If CodeGraph isn't available in this
repo, `grep -rn "allocate_retry_budget" app tests` finds the same two
sites — per `engineering-loop`'s optional-tool-fallback contract, the
missing accelerator changes the tool, not whether the search happens.)

## 2. Narrow — write the failing test, run it alone

```python
# tests/http/test_retry_budget.py
class TestAllocateRetryBudget:
    def test_splits_remainder_across_attempts(self):
        # 10.0s / 3 attempts = 3.33... each, not 3.0 with a dropped 1.0s
        budget = allocate_retry_budget(10.0, 3)
        assert sum(budget) == 10.0
        assert all(b > 0 for b in budget)
```

Run only this test class while iterating:

```bash
$ uv run pytest tests/http/test_retry_budget.py::TestAllocateRetryBudget -q
F                                                                       [100%]
FAILED tests/http/test_retry_budget.py::TestAllocateRetryBudget::test_splits_remainder_across_attempts
1 failed in 0.09s
```

L0: one failure, as expected — the new test exists specifically to prove
the bug.

## 3. Validate — escalate to L1/L2 to confirm the real cause

```bash
$ uv run pytest tests/http/test_retry_budget.py::TestAllocateRetryBudget -q --tb=short
========================= FAILURES =========================
______ TestAllocateRetryBudget.test_splits_remainder_across_attempts ______

    def test_splits_remainder_across_attempts(self):
        budget = allocate_retry_budget(10.0, 3)
>       assert sum(budget) == 10.0
E       assert 9.0 == 10.0
E        +  where 9.0 = sum([3.0, 3.0, 3.0])

tests/http/test_retry_budget.py:5: AssertionError
```

L1 already gave file/line; this L2 `--tb=short` view is enough to settle
the question without escalating further — the actual list, `[3.0, 3.0,
3.0]` summing to `9.0` instead of `10.0`, makes the dropped-remainder bug
from `//` visible directly.

## 4. Fix and re-run the same narrow check

```python
def allocate_retry_budget(total_timeout: float, max_attempts: int) -> list[float]:
    """Split total_timeout across max_attempts, each attempt's slice in seconds."""
    per_attempt = total_timeout / max_attempts
    return [per_attempt] * max_attempts
```

```bash
$ uv run pytest tests/http/test_retry_budget.py::TestAllocateRetryBudget -q
.                                                                        [100%]
1 passed in 0.08s
```

L0 PASS is sufficient here — the fix targets exactly the case the new test
exercises, and the result matches expectation, per `engineering-loop`'s
Validate stage.

## 5. Full Gate before calling it done

The narrow pass proves this fix works; it doesn't prove the caller in
`app/http/client.py:88` (which may round the per-attempt budget to an
`int` deadline elsewhere) still behaves correctly, and it isn't the repo's
release signal on its own:

```bash
$ uv run pytest -q
....................................................................  [100%]
187 passed in 18.4s
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
187 files already formatted
$ uv run mypy app/
Success: no issues found in 41 source files
```

Only after the full suite, Ruff (lint + format), and the repo's configured
type checker all pass is the change considered validated — stopping at the
single targeted pytest pass would have missed a regression in
`app/http/client.py`'s caller if it depended on the old truncating
behavior (e.g. relying on the last attempt getting a `0.0` budget to mean
"skip it").
