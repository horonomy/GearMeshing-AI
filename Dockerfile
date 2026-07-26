# syntax=docker/dockerfile:1
#
# GearMeshing-AI local Docker image (MVP 1 proof of concept).
#
# This image packages the `gearmeshing_ai` library and the `gmai` CLI exactly as
# published today: there is no long-running worker/server process in this repo yet,
# so the default command is `gmai --help`. It exists so the Compose stack can run
# `gmai` commands and the test suite against a locked, reproducible environment.

FROM python:3.13-slim AS builder

# Pin uv by digest-free tag for the POC; matches the version used in CI (.github/workflows/quality.yml).
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

# Install locked third-party dependencies first, with --no-build so no dependency's source
# distribution can execute an arbitrary build-backend setup script; only prebuilt wheels are used.
# --no-install-project defers installing gearmeshing_ai itself to the explicit wheel build below.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-build

# Now copy the rest of the project, build it as a wheel (a source build of our own code, not a
# third-party dependency, so it is not the risk --no-build above protects against), and install
# that wheel with --no-build --no-deps so this step cannot trigger a source build either.
COPY gearmeshing_ai ./gearmeshing_ai
COPY test ./test
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel -o /tmp/dist && \
    uv pip install /tmp/dist/*.whl --no-build --no-deps && \
    rm -rf /tmp/dist

FROM python:3.13-slim AS runtime

WORKDIR /app

RUN groupadd --system gearmeshing && useradd --system --gid gearmeshing --home /app gearmeshing

COPY --from=builder --chown=gearmeshing:gearmeshing /app /app

ENV PATH="/app/.venv/bin:${PATH}"

USER gearmeshing

# No real "gmai serve" command exists on the CLI today (see gearmeshing_ai/interfaces/cli.py) — the
# only shipped subcommand is `gmai version`. Default to `--help` so the container is inspectable and
# usable for ad-hoc `gmai` invocations without fabricating a daemon that does not exist in the codebase.
ENTRYPOINT ["gmai"]
CMD ["--help"]
