"""Real Agent Assembly SDK-backed ``ToolPolicyGate`` implementation.

``RuntimeClient.query_policy`` (the SDK's native gRPC-backed policy check)
only exists when the ``AssemblyContext`` successfully registered with a
reachable gateway (``context.registered is True``); otherwise ``context.client``
degrades to a plain ``GatewayClient`` with no policy-check method at all. This
gate treats that degraded state as "policy check unavailable" - allowing the
action and recording that fact in ``PolicyDecision.details`` - rather than
raising, since the SDK itself only ever advises here: the gateway, proxy, and
eBPF layers remain authoritative regardless of what this in-process check
observes. Detection is structural (``hasattr(client, "query_policy")``)
rather than ``isinstance(client, RuntimeClient)``, since ``RuntimeClient`` is
a native extension type that cannot be subclassed or instantiated
hermetically for tests.
"""

from __future__ import annotations

from typing import Protocol

from agent_assembly import AssemblyContext

from gearmeshing_ai.application.ports.tool_policy import PolicyDecision

_DENY_LIKE_DECISIONS = frozenset({"deny", "pending", "redact", "query_failed", "channel_closed", "unspecified"})


class _PolicyQueryable(Protocol):
    def query_policy(
        self, /, agent_id: str, action_type: str, tool_name: str | None = None, tool_args_json: str | None = None
    ) -> dict[str, str]: ...


class AgentAssemblyPolicyGate:
    """``ToolPolicyGate`` backed by a real Agent Assembly ``AssemblyContext``.

    :param context: An ``AssemblyContext`` returned by ``agent_assembly.init_assembly()``.
    """

    def __init__(self, context: AssemblyContext) -> None:
        self._context = context

    async def check(self, *, agent_id: str, action_type: str, tool_name: str) -> PolicyDecision:
        if self._context.is_shutdown:
            return PolicyDecision(
                allowed=False,
                reason="the Agent Assembly context is already shut down",
                details={"decision": "gate_shutdown"},
            )
        client = self._context.client
        if not hasattr(client, "query_policy"):
            # No reachable native runtime registered this agent (context.registered is
            # False) - query_policy only exists on the native RuntimeClient. Fail open
            # here: the SDK's in-process check is advisory only, never authoritative.
            return PolicyDecision(
                allowed=True,
                reason="no registered Agent Assembly runtime client; policy check unavailable",
                details={"decision": "unavailable", "registered": str(self._context.registered)},
            )
        queryable: _PolicyQueryable = client
        result = queryable.query_policy(agent_id=agent_id, action_type=action_type, tool_name=tool_name)
        decision = str(result.get("decision", "unspecified"))
        reason = str(result.get("reason") or f"Agent Assembly policy decision: {decision}")
        allowed = decision not in _DENY_LIKE_DECISIONS
        return PolicyDecision(allowed=allowed, reason=reason, details={"decision": decision})
