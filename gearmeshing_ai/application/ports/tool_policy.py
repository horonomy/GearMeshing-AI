"""Provider-neutral port for gating a tool call against governance policy.

``WorkflowRunner`` calls a ``ToolPolicyGate`` immediately before invoking a
``CodingExecutor`` (see ``WorkflowRunner._run_executor``), so a policy denial
becomes a structured ``BLOCKED``/``FailureCategory.POLICY`` outcome instead of
an uncaught exception reaching the executor. A no-op gate that always allows
is the default, so this seam is opt-in: existing callers that construct a
``WorkflowRunner`` without a policy gate are unaffected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

_MAXIMUM_REASON_LENGTH = 2048


def _required_reason(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > _MAXIMUM_REASON_LENGTH:
        raise ValueError("reason must be non-empty and bounded")
    return normalized


def _frozen_details(details: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(details))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The outcome of one pre-execution policy check.

    ``allowed=False`` carries ``reason`` (human-readable, safe to persist as
    workflow-event evidence) and ``details`` (bounded machine-readable
    context, e.g. the governance provider's raw decision string).
    """

    allowed: bool
    reason: str
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a boolean")
        object.__setattr__(self, "reason", _required_reason(self.reason))
        object.__setattr__(self, "details", _frozen_details(self.details))


class ToolPolicyGate(Protocol):
    """Provider-neutral port for a pre-execution governance policy check."""

    async def check(self, *, agent_id: str, action_type: str, tool_name: str) -> PolicyDecision:
        """Return a policy decision for one prospective tool invocation."""
        ...


class AllowAllPolicyGate:
    """No-op ``ToolPolicyGate`` that allows every action.

    The default when a caller does not configure a real governance
    provider, so ``WorkflowRunner``'s policy-gate seam is fully backward
    compatible with every existing caller.
    """

    async def check(self, *, agent_id: str, action_type: str, tool_name: str) -> PolicyDecision:
        return PolicyDecision(allowed=True, reason="no policy gate configured")
