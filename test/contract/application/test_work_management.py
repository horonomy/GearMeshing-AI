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
        acceptance_criteria=("The port is covered by contract tests.",),
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

    async def _get_work_item(self, work_item_key: str) -> WorkItem:
        assert work_item_key == "GMAI-16"
        return work_item()

    async def _evaluate_readiness(self, item: WorkItem) -> ReadinessResult:
        return ReadinessResult(work_item_key=item.key)

    async def _update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def _report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def _complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)

    async def _attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        return receipt(update.idempotency_key)


class UnsupportedArtifactProvider(FakeProvider):
    def __init__(self) -> None:
        self.artifact_called = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(set(WorkManagementCapability) - {WorkManagementCapability.ATTACH_ARTIFACT})

    async def _attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        self.artifact_called = True
        return await super()._attach_artifact(update)


def test_capabilities_raise_an_explicit_error_for_unsupported_operations() -> None:
    capabilities = ProviderCapabilities({WorkManagementCapability.READ_WORK_ITEM})

    assert capabilities.supports(WorkManagementCapability.READ_WORK_ITEM)
    with pytest.raises(UnsupportedCapabilityError, match=r"jira.*attach_artifact") as caught:
        capabilities.require("jira", WorkManagementCapability.ATTACH_ARTIFACT)

    assert caught.value.provider == "jira"
    assert caught.value.capability is WorkManagementCapability.ATTACH_ARTIFACT


def test_capabilities_are_defensively_frozen() -> None:
    source = {WorkManagementCapability.READ_WORK_ITEM}
    capabilities = ProviderCapabilities(source)

    source.add(WorkManagementCapability.COMPLETE_WORK)

    assert capabilities.values == frozenset({WorkManagementCapability.READ_WORK_ITEM})


def test_capabilities_reject_unknown_runtime_values() -> None:
    with pytest.raises(TypeError, match="WorkManagementCapability"):
        ProviderCapabilities({"read_work_item"})  # type: ignore[arg-type]


async def test_provider_operations_fail_closed_before_unsupported_side_effects() -> None:
    provider = UnsupportedArtifactProvider()
    update = ArtifactUpdate(
        work_item_key="GMAI-16",
        idempotency_key="run-1:artifact:pr",
        name="Draft PR",
        kind="pull-request",
        web_url="https://github.com/horonomy/GearMeshing-AI/pull/3",
    )

    with pytest.raises(UnsupportedCapabilityError, match="attach_artifact"):
        await provider.attach_artifact(update)

    assert provider.artifact_called is False


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


@pytest.mark.parametrize("provider", ["git\nhub", "x" * 257])
def test_required_identifiers_reject_control_characters_and_unbounded_values(provider: str) -> None:
    with pytest.raises(ValueError):
        RepositoryReference(
            provider=provider,
            owner="horonomy",
            name="GearMeshing-AI",
            web_url="https://github.com/horonomy/GearMeshing-AI",
        )


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


def test_metadata_rejects_keys_that_collide_after_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate normalized key"):
        Metadata({" key": "first", "key": "second"})


@pytest.mark.parametrize(
    "key",
    ["api_token", "Authorization", "user-password", "private_key", "access_key", "bearer", "session_id"],
)
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
        acceptance_criteria=(" Remains provider neutral. ",),
        status=" In Progress ",
        web_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-16",
        repository=repository(),
        labels=labels,  # type: ignore[arg-type]
    )

    labels.append("changed")

    assert item.key == "GMAI-16"
    assert item.labels == ("mvp-1",)


def test_work_item_normalizes_and_freezes_acceptance_criteria() -> None:
    criteria = [" Contract tests pass. "]
    item = WorkItem(
        key="GMAI-16",
        title="Contract",
        description="Approved specification",
        acceptance_criteria=criteria,  # type: ignore[arg-type]
        status="In Progress",
        web_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-16",
        repository=repository(),
    )

    criteria.append("Late unapproved requirement")

    assert item.acceptance_criteria == ("Contract tests pass.",)


def test_readiness_is_derived_from_immutable_problems() -> None:
    problems = [ReadinessProblem(code="missing-approval", summary="Approval missing", details="Await owner approval")]
    blocked = ReadinessResult(work_item_key="GMAI-16", problems=problems)  # type: ignore[arg-type]

    problems.clear()

    assert blocked.ready is False
    assert blocked.problems[0].code == "missing-approval"
    assert ReadinessResult(work_item_key="GMAI-16").ready is True


@pytest.mark.parametrize("percent", [-1, 101, True, 1.5])
def test_progress_updates_reject_invalid_percentages(percent: object) -> None:
    with pytest.raises(ValueError, match="percent_complete"):
        ProgressUpdate(
            work_item_key="GMAI-16",
            idempotency_key="run-1:progress:50",
            summary="Implementing contract",
            percent_complete=percent,  # type: ignore[arg-type]
        )


def test_blocker_updates_require_actionable_details() -> None:
    with pytest.raises(ValueError, match="details"):
        BlockerUpdate(
            work_item_key="GMAI-16",
            idempotency_key="run-1:blocker",
            summary="Approval unavailable",
            details=" ",
        )


@pytest.mark.parametrize(
    "evidence_urls",
    [(), ("http://github.com/horonomy/GearMeshing-AI/pull/1",), ("https://token@github.com/pull/1",)],
)
def test_completion_updates_require_secure_evidence(evidence_urls: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        CompletionUpdate(
            work_item_key="GMAI-16",
            idempotency_key="run-1:complete",
            summary="Contract delivered",
            evidence_urls=evidence_urls,
        )


def test_artifact_updates_reject_credential_bearing_urls() -> None:
    with pytest.raises(ValueError, match="credentials"):
        ArtifactUpdate(
            work_item_key="GMAI-16",
            idempotency_key="run-1:artifact:pr",
            name="Draft PR",
            kind="pull-request",
            web_url="https://token@github.com/horonomy/GearMeshing-AI/pull/1",
        )


def test_operation_receipts_require_timezone_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OperationReceipt(
            provider="jira",
            work_item_key="GMAI-16",
            idempotency_key="run-1:progress:50",
            provider_reference="request-1",
            accepted_at=datetime(2026, 7, 22),
        )


async def test_provider_contract_exposes_typed_asynchronous_operations() -> None:
    provider = FakeProvider()
    item = await provider.get_work_item("GMAI-16")

    assert (await provider.evaluate_readiness(item)).ready
    updates = (
        await provider.update_progress(
            ProgressUpdate(
                work_item_key=item.key,
                idempotency_key="run-1:progress:50",
                summary="Implementing",
                percent_complete=50,
            )
        ),
        await provider.report_blocker(
            BlockerUpdate(
                work_item_key=item.key,
                idempotency_key="run-1:blocker",
                summary="Approval missing",
                details="Await the repository owner",
            )
        ),
        await provider.complete_work(
            CompletionUpdate(
                work_item_key=item.key,
                idempotency_key="run-1:complete",
                summary="Contract delivered",
                evidence_urls=("https://github.com/horonomy/GearMeshing-AI/pull/1",),
            )
        ),
        await provider.attach_artifact(
            ArtifactUpdate(
                work_item_key=item.key,
                idempotency_key="run-1:artifact:pr",
                name="Draft PR",
                kind="pull-request",
                web_url="https://github.com/horonomy/GearMeshing-AI/pull/1",
            )
        ),
    )

    assert [result.idempotency_key for result in updates] == [
        "run-1:progress:50",
        "run-1:blocker",
        "run-1:complete",
        "run-1:artifact:pr",
    ]
