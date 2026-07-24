# GearMeshing-AI

Governed autonomous engineering teams powered by Agent Assembly.

The importable package and CLI use the underscored name `gearmeshing_ai` (`import gearmeshing_ai`, `gmai` on the
command line); this is the settled naming convention for MVP 1 and applies to every module.

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

## CLI

The `gmai` command is installed with the project:

```bash
uv run gmai version
```
