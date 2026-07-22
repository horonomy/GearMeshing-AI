from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import httpx
import pytest

from gearmeshing_ai.adapters.jira_errors import (
    JiraAuthenticationError,
    JiraAuthorizationError,
    JiraConfigurationError,
    JiraRateLimitError,
    JiraResponseError,
)
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


@pytest.mark.parametrize(
    ("status_code", "expected_error", "remediation"),
    [
        (401, JiraAuthenticationError, "replace the API token"),
        (403, JiraAuthorizationError, "grant the account"),
    ],
)
async def test_inaccessible_issue_fails_immediately_without_secret_disclosure(
    status_code: int,
    expected_error: type[Exception],
    remediation: str,
) -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status_code)

    with pytest.raises(expected_error, match=remediation) as caught:
        await provider(handler).get_work_item("GMAI-17")

    assert requests == 1
    assert "not-a-real-token" not in str(caught.value)


async def test_rate_limits_use_bounded_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "999999" if attempts == 1 else "nan"})
        return httpx.Response(200, json=issue_payload())

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(
        base_url="https://lightning-dust-mite.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    adapter = JiraWorkManagementProvider(configuration(max_rate_limit_retries=2), client=client, sleep=sleep)

    assert (await adapter.get_work_item("GMAI-17")).key == "GMAI-17"
    assert attempts == 3
    assert delays == [0.5, 1.0]


async def test_rate_limit_retry_budget_is_enforced() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429)

    async def no_wait(_: float) -> None:
        return None

    client = httpx.AsyncClient(
        base_url="https://lightning-dust-mite.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    adapter = JiraWorkManagementProvider(
        configuration(max_rate_limit_retries=2),
        client=client,
        sleep=no_wait,
    )

    with pytest.raises(JiraRateLimitError, match="bounded retries"):
        await adapter.get_work_item("GMAI-17")
    assert attempts == 3


async def test_streamed_response_is_rejected_at_the_byte_limit() -> None:
    adapter = provider(
        lambda _: httpx.Response(200, content=b"{" + b" " * 1_024),
        max_response_bytes=1_024,
    )

    with pytest.raises(JiraResponseError, match="byte limit"):
        await adapter.get_work_item("GMAI-17")
