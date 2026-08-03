"""Real Agent Assembly SDK-backed ``AgentIdentityProvider`` implementation.

Unlike :class:`~gearmeshing_ai.application.ports.agent_identity.LocalAgentIdentityResolver`
(a POC stub that never contacts Agent Assembly), this resolver is backed by a
real ``agent_assembly.AssemblyContext`` obtained from ``init_assembly()``. It
implements the exact same ``AgentIdentityProvider`` Protocol, so it is a
drop-in replacement wherever an ``AgentIdentityProvider`` is required.

Identity resolution here is deliberately simple: each :class:`ActorRole` maps
to a stable, per-role actor id derived from the process-wide agent id the
``AssemblyContext`` was registered under (e.g. ``gmai-cli-operator.verification``).
This repository does not yet register a *separate* Agent Assembly agent per
role - ``init_assembly()`` registers one agent per process - so per-role
identity is expressed as a namespaced actor id rather than a distinct
registered agent. ``resolve`` performs no I/O of its own (the real I/O already
happened in ``init_assembly()``); it stays ``async`` to satisfy the
``AgentIdentityProvider`` Protocol.
"""

from __future__ import annotations

from agent_assembly import AssemblyContext

from gearmeshing_ai.application.ports.agent_identity import (
    ActorIdentity,
    ActorRole,
    AgentIdentityResolutionError,
)


class AgentAssemblyIdentityResolver:
    """``AgentIdentityProvider`` backed by a real Agent Assembly ``AssemblyContext``.

    :param context: An ``AssemblyContext`` returned by ``agent_assembly.init_assembly()``.
        Must not already be shut down.
    """

    def __init__(self, context: AssemblyContext) -> None:
        if context.is_shutdown:
            raise AgentIdentityResolutionError("the Agent Assembly context is already shut down")
        self._context = context

    async def resolve(self, role: ActorRole) -> ActorIdentity:
        if not isinstance(role, ActorRole):
            raise TypeError("role must be an ActorRole")
        if self._context.is_shutdown:
            raise AgentIdentityResolutionError("the Agent Assembly context is already shut down")
        base_agent_id = self._context.client.agent_id
        try:
            return ActorIdentity(role=role, actor_id=f"{base_agent_id}.{role.value}")
        except ValueError as error:
            raise AgentIdentityResolutionError(
                f"the Agent Assembly agent id {base_agent_id!r} cannot form a valid actor identity for role "
                f"{role.value!r}"
            ) from error
