"""Provider-neutral contract for work-management integrations."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from math import isfinite
from types import MappingProxyType
from unicodedata import category
from urllib.parse import urlsplit

type MetadataScalar = str | int | float | bool | None
type MetadataValue = MetadataScalar | tuple[MetadataValue, ...] | Mapping[str, MetadataValue]

_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "accesskey",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "sessionid",
        "token",
    }
)
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def _required_text(value: str, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters")
    if any(category(character).startswith("C") for character in normalized):
        raise ValueError(f"{field} must not contain control characters")
    return normalized


def _optional_text(value: str, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters")
    if any(category(character).startswith("C") for character in normalized):
        raise ValueError(f"{field} must not contain control characters")
    return normalized


def _https_url_without_credentials(value: str, field: str) -> str:
    normalized = _required_text(value, field, max_length=2048)
    parsed = urlsplit(normalized)
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"{field} contains an invalid port") from error
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment")
    hostname = parsed.hostname
    try:
        ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(hostname) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError(f"{field} contains an invalid hostname") from None
    if any(character.isspace() for character in parsed.path):
        raise ValueError(f"{field} path must not contain whitespace")
    return normalized


def _freeze_metadata_value(value: object, path: str) -> MetadataValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return value
    if isinstance(value, Mapping):
        return _freeze_metadata(value, path)
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item, f"{path}[]") for item in value)
    raise TypeError(f"{path} contains unsupported metadata type {type(value).__name__!r}")


def _freeze_metadata(values: Mapping[str, object], path: str = "metadata") -> Mapping[str, MetadataValue]:
    frozen: dict[str, MetadataValue] = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise TypeError(f"{path} keys must be strings")
        normalized_key = _required_text(key, f"{path} key", max_length=128)
        if normalized_key in frozen:
            raise ValueError(f"{path} contains duplicate normalized key {normalized_key!r}")
        security_key = "".join(character for character in normalized_key.casefold() if character.isalnum())
        if any(marker in security_key for marker in _SENSITIVE_METADATA_KEYS):
            raise ValueError(f"{path} must not contain sensitive key {normalized_key!r}")
        frozen[normalized_key] = _freeze_metadata_value(value, f"{path}.{normalized_key}")
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class Metadata:
    """Defensively copied, recursively immutable, credential-free metadata."""

    values: Mapping[str, MetadataValue]

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        object.__setattr__(self, "values", _freeze_metadata(values or {}))


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
        frozen = frozenset(values)
        if not all(isinstance(value, WorkManagementCapability) for value in frozen):
            raise TypeError("values must contain only WorkManagementCapability members")
        object.__setattr__(self, "values", frozen)

    def supports(self, capability: WorkManagementCapability) -> bool:
        return capability in self.values

    def require(self, provider: str, capability: WorkManagementCapability) -> None:
        provider = _required_text(provider, "provider")
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


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Provider-neutral snapshot of an approved unit of work.

    ``revision`` and ``content_sha256`` identify the exact provider-side
    revision and normalized content this snapshot was read from, so
    execution can prove it ran the human-approved specification rather than
    a later edit. Providers populate ``revision`` from their own change
    marker (for example a Jira ``updated`` timestamp or version number).
    """

    key: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: str
    web_url: str
    repository: RepositoryReference | None
    revision: str
    content_sha256: str
    labels: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _required_text(self.key, "key"))
        object.__setattr__(self, "title", _required_text(self.title, "title", max_length=512))
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, "description", max_length=50_000),
        )
        criteria = tuple(
            _required_text(criterion, "acceptance criterion", max_length=2_000)
            for criterion in self.acceptance_criteria
        )
        if len(criteria) != len(set(criteria)):
            raise ValueError("acceptance_criteria must not contain duplicates")
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "status", _required_text(self.status, "status"))
        object.__setattr__(self, "web_url", _https_url_without_credentials(self.web_url, "web_url"))
        if self.repository is not None and not isinstance(self.repository, RepositoryReference):
            raise TypeError("repository must be RepositoryReference or None")
        object.__setattr__(self, "revision", _required_text(self.revision, "revision", max_length=128))
        digest = self.content_sha256.strip().lower()
        if not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("content_sha256 must be a lowercase hexadecimal SHA-256 digest")
        object.__setattr__(self, "content_sha256", digest)
        normalized_labels = tuple(_required_text(label, "label") for label in self.labels)
        if len(set(normalized_labels)) != len(normalized_labels):
            raise ValueError("labels must not contain duplicates")
        object.__setattr__(self, "labels", normalized_labels)
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class ReadinessProblem:
    """Actionable reason that prevents a work item from being executed."""

    code: str
    summary: str
    details: str
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, "code"))
        object.__setattr__(self, "summary", _required_text(self.summary, "summary", max_length=512))
        object.__setattr__(self, "details", _required_text(self.details, "details", max_length=10_000))
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """Readiness decision whose state is derived from its blocking problems."""

    work_item_key: str
    problems: tuple[ReadinessProblem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "problems", tuple(self.problems))
        if not all(isinstance(problem, ReadinessProblem) for problem in self.problems):
            raise TypeError("problems must contain only ReadinessProblem values")

    @property
    def ready(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Idempotent request to publish execution progress."""

    work_item_key: str
    idempotency_key: str
    summary: str
    percent_complete: int
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "summary", _required_text(self.summary, "summary", max_length=512))
        if (
            isinstance(self.percent_complete, bool)
            or not isinstance(self.percent_complete, int)
            or not 0 <= self.percent_complete <= 100
        ):
            raise ValueError("percent_complete must be an integer from 0 through 100")
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class BlockerUpdate:
    """Idempotent request to report an actionable execution blocker."""

    work_item_key: str
    idempotency_key: str
    summary: str
    details: str
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "summary", _required_text(self.summary, "summary", max_length=512))
        object.__setattr__(self, "details", _required_text(self.details, "details", max_length=10_000))
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class CompletionUpdate:
    """Idempotent request to mark an item complete with evidence."""

    work_item_key: str
    idempotency_key: str
    summary: str
    evidence_urls: tuple[str, ...]
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "summary", _required_text(self.summary, "summary", max_length=512))
        evidence_urls = tuple(_https_url_without_credentials(url, "evidence_url") for url in self.evidence_urls)
        if not evidence_urls:
            raise ValueError("evidence_urls must contain at least one URL")
        if len(set(evidence_urls)) != len(evidence_urls):
            raise ValueError("evidence_urls must not contain duplicates")
        object.__setattr__(self, "evidence_urls", evidence_urls)
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class ArtifactUpdate:
    """Idempotent request to attach a reviewable artifact to a work item."""

    work_item_key: str
    idempotency_key: str
    name: str
    kind: str
    web_url: str
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "kind", _required_text(self.kind, "kind"))
        object.__setattr__(self, "web_url", _https_url_without_credentials(self.web_url, "web_url"))
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    """Provider acknowledgement for an idempotent write operation."""

    provider: str
    work_item_key: str
    idempotency_key: str
    provider_reference: str
    accepted_at: datetime
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "work_item_key", _required_text(self.work_item_key, "work_item_key"))
        object.__setattr__(self, "idempotency_key", _required_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(
            self,
            "provider_reference",
            _required_text(self.provider_reference, "provider_reference"),
        )
        if self.accepted_at.tzinfo is None or self.accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        if not isinstance(self.metadata, Metadata):
            raise TypeError("metadata must be Metadata")


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

    def require_capability(self, capability: WorkManagementCapability) -> None:
        """Fail explicitly when orchestration requests an unsupported operation."""
        self.capabilities.require(self.name, capability)

    async def get_work_item(self, work_item_key: str) -> WorkItem:
        """Load the current provider snapshot for a work item."""
        self.require_capability(WorkManagementCapability.READ_WORK_ITEM)
        return await self._get_work_item(work_item_key)

    @abstractmethod
    async def _get_work_item(self, work_item_key: str) -> WorkItem:
        """Load a work item after the base capability guard succeeds."""

    async def evaluate_readiness(self, work_item: WorkItem) -> ReadinessResult:
        """Evaluate whether the supplied work item is ready for execution."""
        self.require_capability(WorkManagementCapability.EVALUATE_READINESS)
        return await self._evaluate_readiness(work_item)

    @abstractmethod
    async def _evaluate_readiness(self, work_item: WorkItem) -> ReadinessResult:
        """Evaluate readiness after the base capability guard succeeds."""

    async def update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        """Publish execution progress."""
        self.require_capability(WorkManagementCapability.UPDATE_PROGRESS)
        return await self._update_progress(update)

    @abstractmethod
    async def _update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        """Publish progress after the base capability guard succeeds."""

    async def report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        """Publish an actionable blocker."""
        self.require_capability(WorkManagementCapability.REPORT_BLOCKER)
        return await self._report_blocker(update)

    @abstractmethod
    async def _report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        """Publish a blocker after the base capability guard succeeds."""

    async def complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        """Mark work complete with reviewable evidence."""
        self.require_capability(WorkManagementCapability.COMPLETE_WORK)
        return await self._complete_work(update)

    @abstractmethod
    async def _complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        """Complete work after the base capability guard succeeds."""

    async def attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        """Attach a reviewable artifact."""
        self.require_capability(WorkManagementCapability.ATTACH_ARTIFACT)
        return await self._attach_artifact(update)

    @abstractmethod
    async def _attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        """Attach an artifact after the base capability guard succeeds."""
