# Local Docker Compose environment

This document describes the local Docker Compose proof-of-concept environment for GearMeshing-AI
(GMAI-35). It exists to make the current MVP 1 stack — the `gearmeshing_ai` library, the `gmai` CLI,
and a PostgreSQL database — easy to start locally without Kubernetes, Redis, Kafka, or any other
platform infrastructure.

## Prerequisites

- Docker and Docker Compose v2 (`docker compose version`)
- A local `.env` file (see [Configuration](#configuration) below)

## Start the environment

Copy the example environment file and start the stack with a single command:

```bash
cp .env.example .env
docker compose up -d
```

This starts, in order:

1. `postgres` — the PostgreSQL database. Compose waits for its `pg_isready` healthcheck to pass.
2. `gearmeshing` — the `gearmeshing_ai`/`gmai` image, built from the repo-root `Dockerfile`. It only
   starts once `postgres` reports healthy (`depends_on: condition: service_healthy` in
   `docker-compose.yml`), which is how this environment guarantees a predictable startup order.

Check status and logs:

```bash
docker compose ps
docker compose logs -f
```

Stop and remove the containers (the named `postgres_data` volume persists across restarts unless you
pass `-v`):

```bash
docker compose down
```

## What is actually running today

`gearmeshing_ai` has no long-running server/worker process on `main` today — the only runtime entry
point is the `gmai` CLI (`gmai version` is the sole subcommand as of this writing; see
`gearmeshing_ai/interfaces/cli.py`). The `gearmeshing` service's image is therefore built to run `gmai`
commands and the test suite reproducibly, not a fictional always-on daemon. Its default command is
`gmai --help`.

The image's `ENTRYPOINT` is `gmai`, so `docker compose run` arguments are passed straight to it:

```bash
docker compose run --rm gearmeshing version
```

To run the full test suite inside the container (the runtime image installs the `test` directory and
the `pytest`/`ruff`/`mypy` dev dependencies from the same `uv.lock` used in CI), override the
entrypoint:

```bash
docker compose run --rm --entrypoint "" gearmeshing pytest
docker compose run --rm --entrypoint "" gearmeshing ruff check .
docker compose run --rm --entrypoint "" gearmeshing mypy gearmeshing_ai test
```

(`uv` itself is only present in the image's build stage, not the slim runtime stage — the runtime
stage's virtualenv already has `pytest`, `ruff`, and `mypy` on `PATH`, so no `uv run` prefix is
needed inside the container.)

## Configuration

All environment variables read by `docker-compose.yml` are documented in `.env.example`, which is
committed to the repository as a template. Copy it to `.env` and fill in real values:

```bash
cp .env.example .env
```

`.env` itself is excluded from Git via `.gitignore` — never commit real secrets. Only `.env.example`
(with placeholder/blank values) is tracked.

Key variables:

| Variable | Purpose |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` | PostgreSQL credentials and exposed port |
| `DATABASE_URL` | Connection string passed to the `gearmeshing` service; defaults to the `POSTGRES_*` values above |
| `FIXTURES_DIR` | Host path mounted read-only into the `gearmeshing` container at `/app/fixtures` (see [Fixture E2E runs](#fixture-e2e-runs)) |
| `JIRA_SITE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud adapter configuration (`gearmeshing_ai/adapters/jira_work_management.py`) |
| `AGENT_ASSEMBLY_GATEWAY_URL`, `AGENT_ASSEMBLY_GATEWAY_TOKEN` | Placeholder for the Agent Assembly gateway integration — see [Known gaps](#known-gaps) |
| `OTEL_GRPC_PORT`, `OTEL_HTTP_PORT` | Ports for the optional observability profile |

## Fixture E2E runs

The `gearmeshing` service mounts a host directory (default `./fixtures`, overridable via
`FIXTURES_DIR`) read-only into the container at `/app/fixtures`. This gives the Compose environment a
stable, documented mount point for fixture repositories and golden datasets so E2E runs can consume
them once they exist.

GMAI-38 (fixture repos/golden dataset) is a separate, in-flight ticket. This environment does not
depend on its output — the mount path is provisioned structurally now, and is a no-op (an absent or
empty directory) until GMAI-38 lands content there.

## Optional observability profile

A minimal OpenTelemetry Collector is available behind an optional Compose profile and is **not**
started by the base `docker compose up -d` command:

```bash
docker compose --profile observability up -d
```

This starts `otel-collector` (config at `observability/otel-collector-config.yaml`), which accepts
OTLP gRPC/HTTP and currently just logs received telemetry to stdout via a `debug` exporter. It is a
local development convenience, not a production observability stack.

## Known gaps

**Agent Assembly runtime and gateway integration points are stubbed, not implemented.** There is no
Agent Assembly runtime/gateway service or container image in this repository (or referenced by it)
today — `gearmeshing_ai` only defines the boundary types and ports it will eventually call. Rather than
inventing a fake service definition with a made-up image, `docker-compose.yml` documents the
configuration surface the `gearmeshing` service will need once that integration exists:
`AGENT_ASSEMBLY_GATEWAY_URL` and `AGENT_ASSEMBLY_GATEWAY_TOKEN`, both currently unread by any code path
and safe to leave blank. Wiring an actual Agent Assembly runtime/gateway container into this Compose
environment is out of scope for GMAI-35 and is expected to land alongside that service's own
repository/image.

## No Redis, Kafka, or Kubernetes

Per the MVP 1 scope for this environment, this Compose setup intentionally does not include Redis,
Kafka, or any Kubernetes manifests. If a future ticket needs them, they should be added deliberately
with their own justification rather than folded into this POC.
