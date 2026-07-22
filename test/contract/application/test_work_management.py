from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from gearmeshing_ai.application.ports.work_management import (
    ArtifactUpdate,
    BlockerUpdate,
    CompletionUpdate,
    Metadata,
    OperationReceipt,
    ProgressUpdate,
    ProviderCapabilities,
    ReadinessProblem,
    ReadinessResult,
    RepositoryReference,
    UnsupportedCapabilityError,
    WorkItem,
    WorkManagementCapability,
    WorkManagementProvider,
)


def repository() -> RepositoryReference:
    return RepositoryReference(
        provider="github",
        owner="horonomy",
        name="GearMeshing-AI",
        web_url="https://github.com/horonomy/GearMeshing-AI",
    )


def work_item() -> WorkItem:
    return WorkItem(
        key="GMAI-16",
        title="Define a provider-neutral contract",
        description="Expose typed operations without Jira-specific concepts.",
        status="In Progress",
        web_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-16",
        repository=repository(),
        labels=("mvp-1",),
    )


def receipt(idempotency_key: str) -> OperationReceipt:
    return OperationReceipt(
        provider="fake",
        work_item_key="GMAI-16",
        idempotency_key=idempotency_key,
        provider_reference="operation-1",
        accepted_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


class FakeProvider(WorkManagementProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(set(WorkManagementCapability))

    async def get_work_item(self, work_item_key: str) -> WorkItem:
        assert work_item_key == "GMAI-16"
        return work_item()

    async def evaluate_readiness(self, item: WorkItem) -> ReadinessResult:
        return ReadinessResult(work_item_key=item.key)

    async def update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)


def test_capabilities_raise_an_explicit_error_for_unsupported_operations() -> None:
    capabilities = ProviderCapabilities({WorkManagementCapability.READ_WORK_ITEM})

    assert capabilities.supports(WorkManagementCapability.READ_WORK_ITEM)
    with pytest.raises(UnsupportedCapabilityError, match="jira.*attach_artifact") as caught:
        capabilities.require("jira", WorkManagementCapability.ATTACH_ARTIFACT)

    assert caught.value.provider == "jira"
    assert caught.value.capability is WorkManagementCapability.ATTACH_ARTIFACT


def test_capabilities_are_defensively_frozen() -> None:
    source = {WorkManagementCapability.READ_WORK_ITEM}
    capabilities = ProviderCapabilities(source)

    source.add(WorkManagementCapability.COMPLETE_WORK)

    assert capabilities.values == frozenset({WorkManagementCapability.READ_WORK_ITEM})


def test_repository_reference_is_an_immutable_horonomy_identity() -> None:
    value = repository()

    assert value.web_url == "https://github.com/horonomy/GearMeshing-AI"
    with pytest.raises(FrozenInstanceError):
        value.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "web_url",
    [
        "http://github.com/horonomy/GearMeshing-AI",
        "https://token@github.com/horonomy/GearMeshing-AI",
        "https://github.com/horonomy/GearMeshing-AI?token=value",
        "https://github.com/horonomy/GearMeshing-AI#secret",
        "/horonomy/GearMeshing-AI",
    ],
)
def test_repository_reference_rejects_unsafe_urls(web_url: str) -> None:
    with pytest.raises(ValueError):
        RepositoryReference(provider="github", owner="horonomy", name="GearMeshing-AI", web_url=web_url)


def test_metadata_is_recursively_copied_and_frozen() -> None:
    nested = {"labels": ["mvp-1"], "context": {"attempt": 1}}
    metadata = Metadata(nested)

    nested["labels"] = ["changed"]
    nested_context = nested["context"]
    assert isinstance(nested_context, dict)
    nested_context["attempt"] = 2

    assert metadata.values["labels"] == ("mvp-1",)
    assert metadata.values["context"] == {"attempt": 1}
    with pytest.raises(TypeError):
        metadata.values["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("key", ["api_token", "Authorization", "user-password", "private_key"])
def test_metadata_rejects_sensitive_keys_at_any_depth(key: str) -> None:
    with pytest.raises(ValueError, match="sensitive key"):
        Metadata({"context": {key: "must-not-leak"}})


@pytest.mark.parametrize("value", [float("inf"), float("nan"), object()])
def test_metadata_rejects_non_serializable_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Metadata({"value": value})


def test_work_item_defensively_freezes_labels() -> None:
    labels = ["mvp-1"]
    item = WorkItem(
        key=" GMAI-16 ",
        title=" Contract ",
        description=" Approved specification ",
        status=" In Progress ",
        web_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-16",
        repository=repository(),
        labels=labels,  # type: ignore[arg-type]
    )

    labels.append("changed")

    assert item.key == "GMAI-16"
    assert item.labels == ("mvp-1",)
