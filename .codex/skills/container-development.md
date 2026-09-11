<!-- horonom:generated -->
<!-- Source: horonomy/.github agents/skills/container-development/SKILL.md. Provisional Codex projection shape — see agents/common/README.md. Do not hand-edit — rerun `python3 agents/common/project_skills.py`. -->

# SKILL.md — container-development

## Purpose

Give high-signal, low-waste iteration for repos that build or run
containerized services, per
`governance/engineering/agent-skill-architecture.md` (HORO-969) §1. This
skill owns container-specific build/readiness technique; it composes with
`engineering-loop`, which owns the shared Explore→Narrow→Validate→
Escalate→Full Gate execution model and the L0–L3 diagnostic contract.

## Vendor-neutral scope

The actual requirement is "build a container image and prove the service
it runs is ready" — Docker, BuildKit, and Compose are today's preferred
implementations of that requirement (per §6's optional-tool abstraction),
not permanent semantics. If a repo uses a different builder or orchestrator
(e.g. Podman, Buildah, `nerdctl`, a Kubernetes-native build), apply the same
two-gate discipline below against that tool's equivalent primitives — image
build, container start, readiness/health signal, multi-service integration
— rather than treating Docker-specific commands as the invariant.

## Type

Auto-used. Applicable only to a repo with container evidence
(`Dockerfile`, `docker-compose.yml`, or equivalent) per `manifest.yaml`.
Invoke whenever iterating on a Dockerfile, Compose file, container entry
point, or a change that affects how a service builds or starts.

## The core distinction this skill exists to enforce

**A Dockerfile/image build PASS is not proof the container starts or the
service becomes ready.** These are two different gates:

1. **Build gate** — the image builds successfully (syntax valid, stages
   resolve, dependencies install, layers produce an image).
2. **Readiness gate** — a container run from that image actually starts,
   stays up, and the service inside it reaches a ready/healthy state.

A green build with no readiness check is not a green service. Treat build
success as evidence you can proceed to the readiness check, never as a
substitute for it.

## Composition with engineering-loop

Map this skill's gates onto the shared execution model: Explore locates
the affected Dockerfile stage(s)/Compose service(s); Narrow runs the
cheapest check that can prove or disprove the specific change (a syntax
check, then a single affected image build); Validate confirms the build
succeeded for the expected reason; Escalate steps up the L0–L3 ladder when
a build or readiness failure is ambiguous; Full Gate is the full
multi-service integration check before merge/release — see
`references/build-and-readiness-workflow.md` for the concrete sequence and
`examples/affected-service-then-full-integration.md` for a worked case.

## When NOT to use

- As a substitute for `engineering-loop`'s execution model or diagnostic
  contract — this skill supplies container-specific build/readiness
  technique, not a competing loop.
- On a repo with no container evidence (no `Dockerfile`/`docker-compose.yml`
  or equivalent) — `manifest.yaml` should already exclude this case, but
  don't force container technique onto a repo that has none of its own.
- As proof of runtime correctness beyond the readiness gate — a healthy
  container is not the same claim as "the feature it hosts works"; that is
  `product-validation`'s job, not this skill's.

## Credential handling

Container builds are a common place secrets leak by accident. This skill
composes with the credential-plaintext prohibition
(`governance/engineering/security.md` — never inspect, print, log, or
otherwise expose a secret's plaintext value). A dedicated `credential-operations`
skill is planned (per `governance/engineering/agent-skill-architecture.md`
§1) to own the general procedure for safely supplying any build-time or
runtime credential; until it ships, apply the rules below directly.
Concretely:

- **Never bake a secret into an image layer.** A value set via `ENV`,
  `COPY`ed into the build context, or passed as a plain `ARG` persists in
  that layer's history even if a later stage removes the file — `docker
  history`/layer inspection can recover it.
- **Never use a `build-arg` default as a place to put a real secret.** A
  `ARG` default is visible in the Dockerfile source and in build metadata;
  it is not a secret store.
- **Never let a secret land in build logs.** Verify a credential's
  *presence* (non-empty, exit code) in build output — never echo, `cat`,
  or print its value inside a `RUN` step.
- Use the builder's dedicated secret-mount mechanism for build-time
  credentials instead (see `references/build-and-readiness-workflow.md`).

## Never run broad prune as a shortcut

Never run a broad `docker system prune` (or equivalent all-resource prune)
as a convenience shortcut to "fix" a build or free disk space during
iteration — it destroys other work's cached layers, volumes, and networks
indiscriminately, matching the standing prohibition on broad-kill/broad-
delete operations (`governance/engineering/security.md`'s filesystem/
process safety section). Prune scoped to what the current task actually
created and no longer needs, or ask before anything broader.

## References

- `references/build-and-readiness-workflow.md` — discovery, static checks,
  cache usage, the build/readiness/integration gate sequence, secret-mount
  patterns, multi-arch caveats.
- `examples/affected-service-then-full-integration.md` — worked example:
  single-service targeted iteration, then full Compose integration gate.
