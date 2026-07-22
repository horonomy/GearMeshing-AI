from __future__ import annotations

from collections.abc import Callable

import httpx

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
