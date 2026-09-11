# Example — affected-service build, then full integration gate

Scenario: a two-service Compose setup — `api` (application server) and
`db` (PostgreSQL) — where the task is fixing a bug in `api`'s startup
migration logic.

```yaml
# docker-compose.yml
services:
  api:
    build:
      context: ./api
      dockerfile: Dockerfile
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgres://app:app@db:5432/app
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/healthz"]
      interval: 5s
      timeout: 3s
      retries: 5

  db:
    image: postgres:16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 3s
      retries: 5
```

The change: `api/Dockerfile`'s `CMD` runs a migration script before
starting the server; the fix corrects a migration ordering bug in
`api/entrypoint.sh`.

## 1. Explore

The edit is confined to `api/entrypoint.sh`, referenced by `api/Dockerfile`.
`db` is untouched. This is a single-stage Dockerfile, so the whole `api`
image needs rebuilding, but `db` does not.

## 2. Cheap static check first

```
$ docker compose config --quiet
```

No output, exit 0 — the Compose file itself is still valid after the edit
(no YAML/reference errors introduced).

## 3. Build gate — affected service only

```
$ docker compose build api
[+] Building 4.2s (9/9) FINISHED
 => [api 3/5] COPY entrypoint.sh /app/entrypoint.sh          0.1s
 => [api 4/5] RUN chmod +x /app/entrypoint.sh                0.3s
 => exporting to image                                        0.2s
```

Build PASS. This proves the image builds — nothing yet about whether the
service starts or the migration actually runs correctly.

## 4. Readiness gate — single service, not build success alone

```
$ docker compose up -d db
$ docker compose up -d api
$ docker compose ps
NAME      STATUS
db        Up 8s (healthy)
api       Up 3s (health: starting)
$ sleep 6 && docker compose ps
NAME      STATUS
db        Up 14s (healthy)
api       Up 9s (healthy)
```

`api` reaching `healthy` — not merely `Up` — is the readiness signal. Also
inspect its startup log to confirm the migration fix actually took the
intended path, not just that the health endpoint happens to respond:

```
$ docker compose logs api --tail 20
api  | running migrations: 001_init, 002_add_index (in order)
api  | migrations complete
api  | listening on :8080
```

This confirms the specific bug (migrations previously ran out of order)
is fixed. A build PASS alone would not have caught a regression here —
the previous image also built successfully; it just started the
migrations in the wrong order at runtime.

## 5. Full Gate — full multi-service integration before done

Bring down the isolated single-service run and bring up the complete
topology fresh, to confirm nothing about the fix depends on state left
over from the targeted check:

```
$ docker compose down -v
$ docker compose up -d
$ docker compose ps
NAME      STATUS
db        Up 12s (healthy)
api       Up 7s (healthy)
$ curl -sf http://localhost:8080/healthz
ok
```

Both services reach `healthy` from a clean state, and the API's own
health endpoint responds — the actual end-to-end path the change depends
on (API reaching a freshly-migrated database) is exercised, not just the
`api` container in isolation. Only after this full-topology check passes
is the change considered done; the single-service readiness check in step
4 was sufficient to iterate quickly, but not sufficient to merge on.

## 6. Cleanup

```
$ docker compose down -v
```

Scoped to this project's own containers/volumes — not a broad
`docker system prune`, which would also remove unrelated cached layers
and volumes belonging to other work.
