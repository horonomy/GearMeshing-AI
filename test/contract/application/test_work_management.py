from datetime import UTC, datetime

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
