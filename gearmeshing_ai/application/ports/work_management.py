"""Provider-neutral contract for work-management integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _https_url_without_credentials(value: str, field: str) -> str:
    normalized = _required_text(value, field)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment")
    return normalized


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


@dataclass(frozen=True, slots=True)
class RepositoryReference:
    """Credential-free repository identity associated with a work item."""

    provider: str
    owner: str
    name: str
    web_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "owner", _required_text(self.owner, "owner"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "web_url", _https_url_without_credentials(self.web_url, "web_url"))


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
