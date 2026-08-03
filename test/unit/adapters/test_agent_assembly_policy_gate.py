"""Unit tests for the real Agent Assembly-backed tool policy gate.

These tests never perform network I/O or connect to a real gateway. A
hermetic ``AssemblyContext`` is built directly, with either a real (but
unconnected) ``GatewayClient`` to exercise the "no registered runtime"
degraded path, or a duck-typed fake standing in for the native
``RuntimeClient`` (which cannot be subclassed or instantiated hermetically)
to exercise the policy-decision path. Contract tests that actually call
``init_assembly()`` live under ``test/contract/``.
"""

from __future__ import annotations

from typing import Any

from agent_assembly import AssemblyContext
from agent_assembly.client.gateway import GatewayClient

from gearmeshing_ai.adapters.agent_assembly_policy_gate import AgentAssemblyPolicyGate


class _FakeRuntimeClient:
    """Duck-typed stand-in for ``agent_assembly.RuntimeClient.query_policy``."""

    def __init__(self, decision: str, reason: str = "test decision") -> None:
        self.decision = decision
        self.reason = reason
        self.calls: list[dict[str, Any]] = []

    def query_policy(
        self, /, agent_id: str, action_type: str, tool_name: str | None = None, tool_args_json: str | None = None
    ) -> dict[str, str]:
        self.calls.append(
            {"agent_id": agent_id, "action_type": action_type, "tool_name": tool_name, "tool_args_json": tool_args_json}
        )
        return {"decision": self.decision, "reason": self.reason}


def _unregistered_context(
    agent_id: str = "gmai-cli-operator", *, enforcement_mode: str | None = "observe"
) -> AssemblyContext:
    client = GatewayClient(gateway_url="http://127.0.0.1:59999", agent_id=agent_id, enforcement_mode=enforcement_mode)
    return AssemblyContext(
        client=client,
        adapters=[],
        network_mode="sdk-only",
        _network_shutdown=lambda: None,
        registered=False,
    )


def _registered_context(decision: str, reason: str = "test decision") -> tuple[AssemblyContext, _FakeRuntimeClient]:
    fake_client = _FakeRuntimeClient(decision, reason)
    context = AssemblyContext(
        client=fake_client,  # type: ignore[arg-type]
        adapters=[],
        network_mode="sdk-only",
        _network_shutdown=lambda: None,
        registered=True,
    )
    return context, fake_client


async def test_check_allows_and_discloses_unavailability_when_no_runtime_is_registered_under_observe() -> None:
    gate = AgentAssemblyPolicyGate(_unregistered_context(enforcement_mode="observe"))

    decision = await gate.check(agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk")

    assert decision.allowed is True
    assert "unavailable" in decision.reason
    assert decision.details["decision"] == "unavailable"
    assert decision.details["registered"] == "False"
    assert decision.details["enforcement_mode"] == "observe"


async def test_check_fails_closed_when_no_runtime_is_registered_under_the_default_enforce_posture() -> None:
    """``enforcement_mode=None`` mirrors the gateway's live-``enforce`` default (AAASM-4130).

    Matches the real SDK's own local failure posture
    (``agent_assembly.core.runtime_interceptor._local_posture_is_enforce``): with
    no authoritative check available, an action must not silently proceed
    under enforce.
    """
    gate = AgentAssemblyPolicyGate(_unregistered_context(enforcement_mode=None))

    decision = await gate.check(agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk")

    assert decision.allowed is False
    assert "failing closed" in decision.reason
    assert decision.details["decision"] == "unavailable"


async def test_check_fails_closed_when_no_runtime_is_registered_under_explicit_enforce() -> None:
    gate = AgentAssemblyPolicyGate(_unregistered_context(enforcement_mode="enforce"))

    decision = await gate.check(agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk")

    assert decision.allowed is False
    assert decision.details["enforcement_mode"] == "enforce"


async def test_check_allows_when_the_registered_runtime_returns_allow() -> None:
    context, fake_client = _registered_context("allow", reason="within budget")

    decision = await AgentAssemblyPolicyGate(context).check(
        agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk"
    )

    assert decision.allowed is True
    assert decision.reason == "within budget"
    assert decision.details["decision"] == "allow"
    assert fake_client.calls == [
        {
            "agent_id": "gmai-cli-operator",
            "action_type": "tool_call",
            "tool_name": "write_to_disk",
            "tool_args_json": None,
        }
    ]


async def test_check_denies_when_the_registered_runtime_returns_deny() -> None:
    context, _ = _registered_context("deny", reason="egress not allow-listed")

    decision = await AgentAssemblyPolicyGate(context).check(
        agent_id="gmai-cli-operator", action_type="tool_call", tool_name="send_http_request"
    )

    assert decision.allowed is False
    assert decision.reason == "egress not allow-listed"
    assert decision.details["decision"] == "deny"


async def test_check_fails_closed_on_a_query_failed_sentinel() -> None:
    context, _ = _registered_context("query_failed", reason="runtime unreachable")

    decision = await AgentAssemblyPolicyGate(context).check(
        agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk"
    )

    assert decision.allowed is False
    assert decision.details["decision"] == "query_failed"


async def test_check_fails_closed_once_the_context_is_shut_down() -> None:
    context = _unregistered_context()
    context.shutdown()

    decision = await AgentAssemblyPolicyGate(context).check(
        agent_id="gmai-cli-operator", action_type="tool_call", tool_name="write_to_disk"
    )

    assert decision.allowed is False
    assert "shut down" in decision.reason
