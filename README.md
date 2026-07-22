# GearMeshing-AI

Governed autonomous engineering teams powered by Agent Assembly.

## Development

Requirements:

- Python 3.13
- [uv](https://docs.astral.sh/uv/)

Install the locked environment and run the quality checks:

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy gearmeshing_ai test
```

The application version remains `0.0.0` throughout the MVP 1 proof of concept.
