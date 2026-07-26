"""Tests for the Agent Assembly actor-identity port and its local resolver."""

from __future__ import annotations

import pytest

from gearmeshing_ai.application.ports.agent_identity import (
    ActorIdentity,
    ActorRole,
    AgentIdentityResolutionError,
    LocalActorCredential,
    LocalAgentIdentityResolver,
)


def _all_roles_environment() -> dict[str, str]:
    return {
        "GMAI_ACTOR_ID_ORCHESTRATION": "agent-assembly-orchestration",
        "GMAI_ACTOR_ID_CODING_EXECUTION": "agent-assembly-coding-execution",
        "GMAI_ACTOR_ID_VERIFICATION": "agent-assembly-verification",
        "GMAI_ACTOR_ID_DRAFT_PR_PUBLICATION": "agent-assembly-draft-pr-publication",
    }


async def test_resolve_returns_a_distinct_identity_per_role() -> None:
    resolver = LocalAgentIdentityResolver(
        {
            ActorRole.ORCHESTRATION: "agent-assembly-orchestration",
            ActorRole.CODING_EXECUTION: "agent-assembly-coding-execution",
            ActorRole.VERIFICATION: "agent-assembly-verification",
            ActorRole.DRAFT_PR_PUBLICATION: "agent-assembly-draft-pr-publication",
        }
    )

    resolved = {role: await resolver.resolve(role) for role in ActorRole}

    assert resolved[ActorRole.CODING_EXECUTION] != resolved[ActorRole.VERIFICATION]
    assert resolved[ActorRole.CODING_EXECUTION].actor_id == "agent-assembly-coding-execution"
    assert resolved[ActorRole.VERIFICATION].actor_id == "agent-assembly-verification"
    for role, identity in resolved.items():
        assert isinstance(identity, ActorIdentity)
        assert identity.role is role


async def test_resolve_fails_before_tool_execution_when_role_is_unregistered() -> None:
    resolver = LocalAgentIdentityResolver({ActorRole.ORCHESTRATION: "agent-assembly-orchestration"})

    with pytest.raises(AgentIdentityResolutionError, match="no actor identity is registered"):
        await resolver.resolve(ActorRole.CODING_EXECUTION)


@pytest.mark.parametrize("invalid_actor_id", ["", "   ", "has spaces", "has/../traversal", "embedded\ncontrol"])
async def test_resolve_fails_on_an_invalid_identity_format(invalid_actor_id: str) -> None:
    resolver = LocalAgentIdentityResolver({ActorRole.VERIFICATION: invalid_actor_id})

    with pytest.raises(AgentIdentityResolutionError, match="invalid"):
        await resolver.resolve(ActorRole.VERIFICATION)


async def test_resolve_rejects_a_role_that_is_not_an_actor_role() -> None:
    resolver = LocalAgentIdentityResolver({ActorRole.ORCHESTRATION: "agent-assembly-orchestration"})

    with pytest.raises(TypeError, match="ActorRole"):
        await resolver.resolve("orchestration")  # type: ignore[arg-type]


def test_actor_identity_rejects_a_non_actor_role() -> None:
    with pytest.raises(TypeError, match="ActorRole"):
        ActorIdentity(role="orchestration", actor_id="agent-assembly-orchestration")  # type: ignore[arg-type]


def test_actor_identity_rejects_an_unsafe_identifier() -> None:
    with pytest.raises(ValueError, match="not a safe identifier"):
        ActorIdentity(role=ActorRole.ORCHESTRATION, actor_id="has spaces")


def test_local_actor_credential_token_is_not_disclosed_by_repr() -> None:
    credential = LocalActorCredential(actor_id="agent-assembly-orchestration", token="super-secret-token")

    rendered = repr(credential)

    assert "super-secret-token" not in rendered
    assert "agent-assembly-orchestration" in rendered


def test_local_actor_credential_rejects_a_blank_token() -> None:
    with pytest.raises(ValueError, match="token must not be blank"):
        LocalActorCredential(actor_id="agent-assembly-orchestration", token="   ")


async def test_from_environment_registers_every_configured_role() -> None:
    resolver = LocalAgentIdentityResolver.from_environment(environ=_all_roles_environment())

    orchestration = await resolver.resolve(ActorRole.ORCHESTRATION)

    assert orchestration.actor_id == "agent-assembly-orchestration"


async def test_from_environment_leaves_unset_roles_unresolvable() -> None:
    resolver = LocalAgentIdentityResolver.from_environment(environ={})

    with pytest.raises(AgentIdentityResolutionError, match="no actor identity is registered"):
        await resolver.resolve(ActorRole.CODING_EXECUTION)


async def test_from_environment_ignores_a_blank_variable() -> None:
    resolver = LocalAgentIdentityResolver.from_environment(
        environ={"GMAI_ACTOR_ID_VERIFICATION": "   "},
    )

    with pytest.raises(AgentIdentityResolutionError, match="no actor identity is registered"):
        await resolver.resolve(ActorRole.VERIFICATION)


async def test_from_environment_never_exposes_a_configured_token_on_the_resolved_identity() -> None:
    environment = _all_roles_environment()
    environment["GMAI_ACTOR_TOKEN_CODING_EXECUTION"] = "super-secret-token"

    resolver = LocalAgentIdentityResolver.from_environment(environ=environment)
    identity = await resolver.resolve(ActorRole.CODING_EXECUTION)

    assert "super-secret-token" not in repr(identity)
    assert "super-secret-token" not in str(identity)
