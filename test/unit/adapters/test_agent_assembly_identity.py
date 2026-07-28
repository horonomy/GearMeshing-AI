"""Unit tests for the real Agent Assembly-backed identity resolver.

These tests never perform network I/O or connect to a real gateway:
``GatewayClient.__init__`` and ``AssemblyContext``'s dataclass constructor do
no I/O on their own (the real I/O only happens inside ``init_assembly()``),
so a hermetic ``AssemblyContext`` can be built directly for fast, isolated
Protocol-conformance and behavior tests. Contract tests that actually call
``init_assembly()`` live under ``test/contract/``.
"""

from __future__ import annotations

import pytest
from agent_assembly import AssemblyContext
from agent_assembly.client.gateway import GatewayClient

from gearmeshing_ai.adapters.agent_assembly_identity import AgentAssemblyIdentityResolver
from gearmeshing_ai.application.ports.agent_identity import (
    ActorIdentity,
    ActorRole,
    AgentIdentityProvider,
    AgentIdentityResolutionError,
)


def _hermetic_context(agent_id: str = "gmai-cli-operator") -> AssemblyContext:
    client = GatewayClient(gateway_url="http://127.0.0.1:59999", agent_id=agent_id)
    return AssemblyContext(
        client=client,
        adapters=[],
        network_mode="sdk-only",
        _network_shutdown=lambda: None,
        registered=False,
    )


def test_resolver_satisfies_the_agent_identity_provider_protocol() -> None:
    # AgentIdentityProvider is a structural (non-runtime-checkable) Protocol, so
    # conformance is asserted statically: this assignment only type-checks if
    # AgentAssemblyIdentityResolver implements the Protocol's shape.
    resolver: AgentIdentityProvider = AgentAssemblyIdentityResolver(_hermetic_context())
    assert resolver is not None


async def test_resolve_returns_a_distinct_namespaced_identity_per_role() -> None:
    resolver = AgentAssemblyIdentityResolver(_hermetic_context("gmai-cli-operator"))

    resolved = {role: await resolver.resolve(role) for role in ActorRole}

    assert resolved[ActorRole.CODING_EXECUTION] != resolved[ActorRole.VERIFICATION]
    assert resolved[ActorRole.CODING_EXECUTION].actor_id == "gmai-cli-operator.coding_execution"
    assert resolved[ActorRole.VERIFICATION].actor_id == "gmai-cli-operator.verification"
    for role, identity in resolved.items():
        assert isinstance(identity, ActorIdentity)
        assert identity.role is role


def test_constructor_rejects_an_already_shut_down_context() -> None:
    context = _hermetic_context()
    context.shutdown()

    with pytest.raises(AgentIdentityResolutionError, match="already shut down"):
        AgentAssemblyIdentityResolver(context)


async def test_resolve_fails_closed_once_the_context_is_shut_down_after_construction() -> None:
    context = _hermetic_context()
    resolver = AgentAssemblyIdentityResolver(context)
    context.shutdown()

    with pytest.raises(AgentIdentityResolutionError, match="already shut down"):
        await resolver.resolve(ActorRole.ORCHESTRATION)


async def test_resolve_rejects_a_role_that_is_not_an_actor_role() -> None:
    resolver = AgentAssemblyIdentityResolver(_hermetic_context())

    with pytest.raises(TypeError, match="role must be an ActorRole"):
        await resolver.resolve("orchestration")  # type: ignore[arg-type]
