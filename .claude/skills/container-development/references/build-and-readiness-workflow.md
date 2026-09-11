# Reference — build-and-readiness workflow

Concrete sequence for iterating on a containerized service without wasting
time on expensive full builds, while never letting a cheap build PASS
stand in for proof the service actually runs. Written against Docker/
BuildKit/Compose as the current implementation (per SKILL.md's
vendor-neutral scope); substitute the equivalent primitive for another
builder/orchestrator.

## 1. Discover the container surface

Before changing anything, find every Dockerfile, Compose file, and
container entry point the task could affect:

- `Dockerfile`, `Dockerfile.*`, `*.dockerfile` — one or more build
  definitions, possibly multi-stage.
- `docker-compose.yml`/`compose.yml` (and override files, e.g.
  `docker-compose.override.yml`) — service topology, build contexts,
  dependency ordering (`depends_on`), health checks, volumes, networks.
- Entry-point/init scripts (`entrypoint.sh`, `docker-entrypoint.sh`,
  `CMD`/`ENTRYPOINT` targets) — where "ready" actually gets decided.

Prefer CodeGraph/`grep` to enumerate these quickly (per
`engineering-loop`'s Explore stage and its optional-tool fallback
semantics) rather than reading the whole tree by hand.

## 2. Reason about the affected stage/image/service

A multi-stage Dockerfile or a multi-service Compose file rarely needs a
full rebuild of everything for a small change:

- Identify which build **stage** the edit touches (a `FROM ... AS build`
  stage vs. a later `FROM ... AS runtime` stage) — an edit confined to one
  stage only needs that stage rebuilt if BuildKit's layer cache is being
  used correctly (see §4).
- Identify which Compose **service** owns the changed Dockerfile/context.
  A change to one service's image does not require rebuilding or
  restarting services it doesn't affect.
- If the change alters a shared base image, dependency, or a Compose
  network/volume other services attach to, treat it as affecting every
  dependent service, not just the one directly edited.

## 3. Cheap static/syntax checks before expensive builds

Run the fastest available check first — this is the Narrow stage:

- Dockerfile syntax/lint (e.g. a linter such as `hadolint`, or BuildKit's
  own `--check`/dry-run support where available) catches malformed
  instructions, missing stages, and common anti-patterns without pulling a
  single layer.
- Compose file validation (e.g. `docker compose config`) catches malformed
  YAML, unresolved variable interpolation, and invalid service references
  before attempting to build or start anything.

Only after static checks pass does building an actual image become the
next-cheapest signal.

## 4. BuildKit cache usage that preserves correctness

Use layer caching to keep iteration fast, but never let cache reuse hide a
real change:

- Order Dockerfile instructions so infrequently-changing layers (base
  image, system packages, dependency manifests) come before
  frequently-changing ones (application source) — this is what makes
  cache reuse actually save time on the common edit path.
- A cache-hit build is a valid Narrow-stage signal only for the layers
  that legitimately didn't change. If a change touches a layer that cache
  metadata (mtimes, `COPY` source hash) might not detect correctly —
  e.g. an `ARG`/`ENV` value flowing into a cached `RUN` — force a
  no-cache rebuild for that one image before trusting the result.
  Never use `--no-cache` as a routine habit; it defeats the point of the
  cache and turns Narrow into Full Gate cost every time.
- Cache reuse is a build-time convenience only — it says nothing about
  runtime readiness. Do not treat "the build hit cache and finished
  instantly" as any signal about whether the resulting container starts.

## 5. Build gate: single affected image

Build only the image(s) identified in §2 (e.g. `docker build` targeting
one Dockerfile, or `docker compose build <service>`), not the whole
Compose topology. Treat a successful build as: "the image exists and its
layers resolved" — nothing more.

## 6. Readiness gate: a separate, explicit check

Do not stop at build success. Start the container (or the single affected
Compose service) and check that the service inside it is actually ready:

- **Health check.** If the image/Compose service defines a `HEALTHCHECK`
  or Compose `healthcheck:`, wait for and inspect its reported status
  (`docker inspect --format '{{.State.Health.Status}}'` or
  `docker compose ps` showing `healthy`) rather than assuming "container
  running" means "service ready" — a process can be running and still be
  failing its own startup sequence.
- **Log inspection.** Where no health check exists, inspect startup logs
  for the service's own readiness signal (a "listening on", "ready",
  successful migration/connection message) and for crash-loop symptoms
  (repeated restarts, an early non-zero exit).
- **Direct smoke check.** Where applicable, hit the service's own
  readiness/liveness endpoint or run the smallest possible client
  interaction against it, rather than trusting log text alone.

A container that starts and then exits, or that starts and never reaches
its documented ready state, is a readiness-gate FAILURE even though the
build gate PASSED. Report and escalate that distinction explicitly — do
not describe it as "the build failed."

## 7. Full Gate: full multi-service integration

Before merge/release, bring up the complete multi-service topology (e.g.
`docker compose up` across every service the change could interact with),
not just the single service edited. Confirm every service reaches its
ready state and that inter-service communication the feature depends on
(a database connection, an internal API call) actually succeeds. A
single-service readiness PASS is never sufficient release evidence on its
own — it proves the edited service works in isolation, not that the
system as a whole still does. This is the container-specific instance of
`engineering-loop`'s Full Gate stage.

## 8. Secret-mount patterns for build-time credentials

When a build genuinely needs a credential (a private package registry
token, a private base image pull):

- Prefer BuildKit's dedicated build-time secret mount (a secret passed via
  a mount that is never written into any image layer or the build cache
  key) over `ARG`/`ENV` for anything credential-shaped.
- Confirm the secret's presence via the build succeeding or a
  non-value-revealing check, never by having a build step print it.
- For a private base image or registry pull, use the container runtime's
  own credential-helper/login mechanism rather than embedding credentials
  in a `FROM` line or a Compose `image:` reference.
- See `governance/engineering/security.md` for the non-waivable
  plaintext-exposure prohibition this pattern exists to satisfy.

## 9. Multi-arch/platform caveats

- An image built for one platform (e.g. the build host's native
  architecture) is not proof it runs correctly on another target platform
  (e.g. `linux/amd64` vs. `linux/arm64`) — a readiness PASS on one
  platform does not transfer to another without also checking it there,
  or via a proper multi-platform build.
- A multi-platform build/manifest step is slower and touches more of the
  cache than a single-platform build — reserve it for the Full Gate stage
  or for a change that specifically affects platform-sensitive code
  (native dependencies, architecture-specific binaries), not for every
  Narrow-stage iteration.
- Emulation-based cross-platform builds (e.g. via QEMU) can mask
  performance or correctness issues that only appear on real target
  hardware — treat an emulated readiness check as weaker evidence than a
  native one, and say so when reporting results.
