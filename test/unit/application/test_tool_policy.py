"""Unit tests for the provider-neutral tool-policy contract and its no-op default."""

from __future__ import annotations

import pytest

from gearmeshing_ai.application.ports.tool_policy import AllowAllPolicyGate, PolicyDecision, ToolPolicyGate


def test_policy_decision_requires_a_boolean_allowed_flag() -> None:
    with pytest.raises(ValueError, match="allowed must be a boolean"):
        PolicyDecision(allowed="yes", reason="not a real boolean")  # type: ignore[arg-type]


def test_policy_decision_rejects_a_blank_reason() -> None:
    with pytest.raises(ValueError, match="reason must be non-empty"):
        PolicyDecision(allowed=True, reason="   ")


def test_policy_decision_details_default_to_an_empty_immutable_mapping() -> None:
    decision = PolicyDecision(allowed=True, reason="ok")

    assert dict(decision.details) == {}
    with pytest.raises(TypeError):
        decision.details["x"] = "y"  # type: ignore[index]


async def test_allow_all_policy_gate_satisfies_the_tool_policy_gate_protocol() -> None:
    gate: ToolPolicyGate = AllowAllPolicyGate()

    decision = await gate.check(agent_id="any-agent", action_type="tool_call", tool_name="write_to_disk")

    assert decision.allowed is True
    assert "no policy gate configured" in decision.reason
