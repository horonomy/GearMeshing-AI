"""Provider-neutral contract for work-management integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class WorkManagementCapability(StrEnum):
    """Operations that a work-management provider may support."""

    READ_WORK_ITEM = "read_work_item"
    EVALUATE_READINESS = "evaluate_readiness"
    UPDATE_PROGRESS = "update_progress"
    REPORT_BLOCKER = "report_blocker"
    COMPLETE_WORK = "complete_work"
    ATTACH_ARTIFACT = "attach_artifact"


class UnsupportedCapabilityError(RuntimeError):
    """Raised before invoking an operation unsupported by a provider."""

    def __init__(self, provider: str, capability: WorkManagementCapability) -> None:
        self.provider = provider
        self.capability = capability
        super().__init__(f"{provider!r} does not support {capability.value!r}")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Immutable set of operations advertised by a provider."""

    values: frozenset[WorkManagementCapability]

    def __init__(self, values: frozenset[WorkManagementCapability] | set[WorkManagementCapability]) -> None:
        object.__setattr__(self, "values", frozenset(values))

    def supports(self, capability: WorkManagementCapability) -> bool:
        return capability in self.values

    def require(self, provider: str, capability: WorkManagementCapability) -> None:
        if not self.supports(capability):
            raise UnsupportedCapabilityError(provider, capability)


class WorkManagementProvider(ABC):
    """Boundary implemented by external work-management adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable provider name used in diagnostics."""

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return the operations implemented by this provider."""
