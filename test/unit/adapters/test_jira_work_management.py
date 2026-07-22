from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import httpx
import pytest

from gearmeshing_ai.adapters.jira_errors import JiraConfigurationError
from gearmeshing_ai.adapters.jira_work_management import JiraConfiguration, JiraWorkManagementProvider
from gearmeshing_ai.application.ports.work_management import RepositoryReference

type Handler = Callable[[httpx.Request], httpx.Response]


def repository() -> RepositoryReference:
    return RepositoryReference(
        provider="github",
        owner="horonomy",
        name="GearMeshing-AI",
        web_url="https://github.com/horonomy/GearMeshing-AI",
    )


def configuration(**overrides: object) -> JiraConfiguration:
    values: dict[str, object] = {
        "site_url": "https://lightning-dust-mite.atlassian.net",
        "email": "engineer@example.com",
        "api_token": "not-a-real-token",
        "project_key": "GMAI",
        "repository": repository(),
    }
    values.update(overrides)
    return JiraConfiguration(**values)  # type: ignore[arg-type]


def provider(handler: Handler, **overrides: object) -> JiraWorkManagementProvider:
    client = httpx.AsyncClient(
        base_url="https://lightning-dust-mite.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    return JiraWorkManagementProvider(configuration(**overrides), client=client)


def adf(*blocks: dict[str, object]) -> dict[str, object]:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def paragraph(text: str) -> dict[str, object]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def heading(text: str) -> dict[str, object]:
    return {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": text}]}


def issue_payload(
    *,
    issue_type: str = "Story",
    labels: list[str] | None = None,
    description: object | None = None,
    include_repository: bool = True,
) -> dict[str, object]:
    properties: dict[str, object] = {}
    if include_repository:
        properties["gearmeshing-ai.repository"] = {
            "provider": "github",
            "owner": "horonomy",
            "name": "GearMeshing-AI",
            "webUrl": "https://github.com/horonomy/GearMeshing-AI",
        }
    return {
        "key": "GMAI-17",
        "fields": {
            "summary": "Load and validate an approved Jira work item",
            "description": description
            if description is not None
            else adf(paragraph("Approved specification"), heading("Acceptance Criteria"), paragraph("It works")),
            "status": {"name": "In Progress"},
            "labels": labels if labels is not None else ["spec-ready", "mvp-1"],
            "issuetype": {"name": issue_type},
        },
        "properties": properties,
    }


def test_configuration_is_immutable_bounded_and_credential_safe() -> None:
    value = configuration()

    assert "not-a-real-token" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.site_url = "https://example.com"  # type: ignore[misc]
    with pytest.raises(JiraConfigurationError):
        configuration(site_url="https://example.com:not-a-port")
    with pytest.raises(JiraConfigurationError):
        configuration(retry_base_seconds=float("nan"))


async def test_ready_issue_is_normalized_without_inventing_requirements() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/GMAI-17"
        assert request.url.params["properties"] == "gearmeshing-ai.repository"
        return httpx.Response(200, json=issue_payload())

    adapter = provider(handler)
    item = await adapter.get_work_item("GMAI-17")
    readiness = await adapter.evaluate_readiness(item)

    assert item.acceptance_criteria == ("It works",)
    assert item.repository == repository()
    assert item.metadata.values["repository_context_present"] is True
    assert readiness.ready is True


async def test_incomplete_issue_returns_actionable_blocking_diagnostics() -> None:
    adapter = provider(
        lambda _: httpx.Response(
            200,
            json=issue_payload(
                labels=[],
                description=adf(paragraph("Product intent only")),
                include_repository=False,
            ),
        )
    )

    item = await adapter.get_work_item("GMAI-17")
    readiness = await adapter.evaluate_readiness(item)

    assert item.acceptance_criteria == ()
    assert {problem.code for problem in readiness.problems} == {
        "spec-not-ready",
        "missing-acceptance-criteria",
        "missing-repository-context",
    }


async def test_unsupported_issue_type_is_blocked() -> None:
    adapter = provider(lambda _: httpx.Response(200, json=issue_payload(issue_type="Epic")))

    item = await adapter.get_work_item("GMAI-17")
    readiness = await adapter.evaluate_readiness(item)

    assert [problem.code for problem in readiness.problems] == ["unsupported-issue-type"]
    assert readiness.problems[0].details == "MVP 1 accepts Jira Story and Task issues only."
