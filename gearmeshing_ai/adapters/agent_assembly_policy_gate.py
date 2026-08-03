"""Real Agent Assembly SDK-backed ``ToolPolicyGate`` implementation.

``RuntimeClient.query_policy`` (the SDK's native gRPC-backed policy check)
only exists when the ``AssemblyContext`` successfully registered with a
reachable gateway (``context.registered is True``); otherwise ``context.client``
degrades to a plain ``GatewayClient`` with no policy-check method at all.
Whether that degraded state fails open or closed mirrors the SDK's own local
failure posture (``agent_assembly.core.runtime_interceptor._local_posture_is_enforce``):
``enforcement_mode`` of ``None`` or ``"enforce"`` fails closed - there is no
authoritative check available, so an action must not silently proceed - while
the explicit dry-run postures ``"observe"``/``"disabled"`` fail open and
record that the check was unavailable. Detection of the degraded state is
structural (``hasattr(client, "query_policy")``) rather than
``isinstance(client, RuntimeClient)``, since ``RuntimeClient`` is a native
extension type that cannot be subclassed or instantiated hermetically for
tests.
"""

from __future__ import annotations

from typing import Protocol

from agent_assembly import AssemblyContext

from gearmeshing_ai.application.ports.tool_policy import PolicyDecision

_DENY_LIKE_DECISIONS = frozenset({"deny", "pending", "redact", "query_failed", "channel_closed", "unspecified"})
_ENFORCE_LOCAL_POSTURE = frozenset({None, "enforce"})


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
            # False) - query_policy only exists on the native RuntimeClient.
            enforcement_mode = getattr(client, "enforcement_mode", None)
            fail_closed = enforcement_mode in _ENFORCE_LOCAL_POSTURE
            return PolicyDecision(
                allowed=not fail_closed,
                reason=(
                    f"no registered Agent Assembly runtime client and the local posture is "
                    f"enforce ({enforcement_mode!r}); failing closed"
                    if fail_closed
                    else "no registered Agent Assembly runtime client; policy check unavailable"
                ),
                details={
                    "decision": "unavailable",
                    "registered": str(self._context.registered),
                    "enforcement_mode": str(enforcement_mode),
                },
            )
        queryable: _PolicyQueryable = client
        result = queryable.query_policy(agent_id=agent_id, action_type=action_type, tool_name=tool_name)
        decision = str(result.get("decision", "unspecified"))
        reason = str(result.get("reason") or f"Agent Assembly policy decision: {decision}")
        allowed = decision not in _DENY_LIKE_DECISIONS
        return PolicyDecision(allowed=allowed, reason=reason, details={"decision": decision})
