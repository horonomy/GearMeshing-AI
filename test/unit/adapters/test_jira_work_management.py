from __future__ import annotations

import asyncio
import hashlib
import json
from base64 import b64encode
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import httpx
import pytest

from gearmeshing_ai.adapters.jira_errors import (
    JiraAuthenticationError,
    JiraAuthorizationError,
    JiraConfigurationError,
    JiraIdempotencyConflictError,
    JiraRateLimitError,
    JiraResponseError,
)
from gearmeshing_ai.adapters.jira_work_management import JiraConfiguration, JiraWorkManagementProvider
from gearmeshing_ai.application.ports.work_management import (
    BlockerUpdate,
    ProgressUpdate,
    ReadinessProblem,
    ReadinessResult,
    RepositoryReference,
    UnsupportedCapabilityError,
)

type Handler = Callable[[httpx.Request], httpx.Response]


def operation_binding(capability: str, text: str, idempotency_key: str) -> dict[str, str]:
    digest = hashlib.sha256(f"{capability}\0{text}".encode()).hexdigest()
    return {"idempotencyKey": idempotency_key, "operationDigest": digest}


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
    assert "engineer@example.com" not in repr(value)
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


async def test_response_issue_key_must_match_the_requested_issue() -> None:
    payload = issue_payload()
    payload["key"] = "GMAI-18"
    adapter = provider(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(JiraResponseError, match="does not match"):
        await adapter.get_work_item("GMAI-17")


@pytest.mark.parametrize("malformation", ["adf", "model"])
async def test_malformed_jira_payload_uses_typed_response_errors(malformation: str) -> None:
    payload = issue_payload()
    fields = payload["fields"]
    assert isinstance(fields, dict)
    if malformation == "adf":
        fields["description"] = {"type": "doc", "version": 1, "content": "not-an-array"}
    else:
        fields["summary"] = "unsafe\nsummary"
    adapter = provider(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(JiraResponseError, match="cannot be normalized safely"):
        await adapter.get_work_item("GMAI-17")


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
    assert item.repository == repository()
    assert item.metadata.values["repository_context_present"] is True
    assert {problem.code for problem in readiness.problems} == {
        "spec-not-ready",
        "missing-acceptance-criteria",
    }


async def test_missing_description_remains_empty_and_blocks_readiness() -> None:
    payload = issue_payload()
    fields = payload["fields"]
    assert isinstance(fields, dict)
    fields["description"] = None
    adapter = provider(lambda _: httpx.Response(200, json=payload))

    item = await adapter.get_work_item("GMAI-17")
    readiness = await adapter.evaluate_readiness(item)

    assert item.description == ""
    assert {problem.code for problem in readiness.problems} == {
        "missing-description",
        "missing-acceptance-criteria",
    }


async def test_jira_repository_property_cannot_override_approved_configuration() -> None:
    payload = issue_payload()
    properties = payload["properties"]
    assert isinstance(properties, dict)
    repository_property = properties["gearmeshing-ai.repository"]
    assert isinstance(repository_property, dict)
    repository_property["owner"] = "untrusted-owner"
    adapter = provider(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(JiraResponseError, match="does not match"):
        await adapter.get_work_item("GMAI-17")


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


async def test_invalid_request_url_is_mapped_to_a_safe_configuration_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("invalid internal URL")

    with pytest.raises(JiraConfigurationError, match="request URL is invalid") as caught:
        await provider(handler).get_work_item("GMAI-17")

    assert "not-a-real-token" not in str(caught.value)


async def test_injected_client_cannot_override_destination_auth_or_request_policy() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(
            {
                "url": str(request.url.copy_with(query=None)),
                "authorization": request.headers["Authorization"],
                "accept": request.headers["Accept"],
                "timeout": request.extensions["timeout"],
            }
        )
        return httpx.Response(200, json=issue_payload())

    settings = configuration()
    client = httpx.AsyncClient(
        base_url="https://untrusted.example",
        auth=httpx.BasicAuth("wrong@example.com", "wrong-token"),
        headers={"Accept": "text/plain"},
        transport=httpx.MockTransport(handler),
    )
    adapter = JiraWorkManagementProvider(settings, client=client)

    await adapter.get_work_item("GMAI-17")

    expected_auth = b64encode(f"{settings.email}:{settings.api_token}".encode()).decode()
    assert observed["url"] == "https://lightning-dust-mite.atlassian.net/rest/api/3/issue/GMAI-17"
    assert observed["authorization"] == f"Basic {expected_auth}"
    assert observed["accept"] == "application/json"
    assert observed["timeout"] == {"connect": 15.0, "read": 15.0, "write": 15.0, "pool": 15.0}


async def test_injected_client_cannot_enable_cross_origin_redirects() -> None:
    destinations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        destinations.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://untrusted.example/capture"})

    client = httpx.AsyncClient(follow_redirects=True, transport=httpx.MockTransport(handler))
    adapter = JiraWorkManagementProvider(configuration(), client=client)

    with pytest.raises(JiraResponseError, match="HTTP 302"):
        await adapter.get_work_item("GMAI-17")

    assert destinations == ["lightning-dust-mite.atlassian.net"]


async def test_disabled_writes_fail_as_explicit_unsupported_capabilities() -> None:
    update = ProgressUpdate(
        work_item_key="GMAI-17",
        idempotency_key="run-1:progress:50",
        summary="Implementing adapter",
        percent_complete=50,
    )

    with pytest.raises(UnsupportedCapabilityError, match="update_progress"):
        await provider(lambda _: httpx.Response(500)).update_progress(update)


async def test_repeated_write_reuses_comment_with_matching_idempotency_property() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(
            200,
            json={
                "comments": [
                    {
                        "id": "10001",
                        "created": "2026-07-22T05:00:00Z",
                        "properties": [
                            {
                                "key": "gearmeshing-ai.idempotency-key",
                                "value": operation_binding(
                                    "update_progress",
                                    "GearMeshing-AI progress (50%): Implementing adapter",
                                    "run-1:progress:50",
                                ),
                            }
                        ],
                    }
                ],
                "total": 1,
            },
        )

    receipt = await provider(handler, allow_writes=True).update_progress(
        ProgressUpdate(
            work_item_key="GMAI-17",
            idempotency_key="run-1:progress:50",
            summary="Implementing adapter",
            percent_complete=50,
        )
    )

    assert receipt.provider_reference == "10001"
    assert requests == ["GET"]


async def test_existing_comment_rejects_timezone_naive_timestamp() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "comments": [
                    {
                        "id": "10001",
                        "created": "2026-07-22T05:00:00",
                        "properties": [
                            {
                                "key": "gearmeshing-ai.idempotency-key",
                                "value": operation_binding(
                                    "update_progress",
                                    "GearMeshing-AI progress (50%): Implementing adapter",
                                    "run-1:progress:50",
                                ),
                            }
                        ],
                    }
                ],
                "total": 1,
            },
        )

    with pytest.raises(JiraResponseError, match="comment creation timestamp"):
        await provider(handler, allow_writes=True).update_progress(
            ProgressUpdate(
                work_item_key="GMAI-17",
                idempotency_key="run-1:progress:50",
                summary="Implementing adapter",
                percent_complete=50,
            )
        )


async def test_idempotency_key_reuse_with_changed_payload_fails_explicitly() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "comments": [
                    {
                        "id": "10001",
                        "created": "2026-07-22T05:00:00Z",
                        "properties": [
                            {
                                "key": "gearmeshing-ai.idempotency-key",
                                "value": operation_binding(
                                    "update_progress",
                                    "GearMeshing-AI progress (50%): Original payload",
                                    "run-1:progress:50",
                                ),
                            }
                        ],
                    }
                ],
                "total": 1,
            },
        )

    with pytest.raises(JiraIdempotencyConflictError, match="different operation or payload"):
        await provider(handler, allow_writes=True).update_progress(
            ProgressUpdate(
                work_item_key="GMAI-17",
                idempotency_key="run-1:progress:50",
                summary="Changed payload",
                percent_complete=50,
            )
        )


async def test_idempotency_key_reuse_across_capabilities_fails_explicitly() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "comments": [
                    {
                        "id": "10001",
                        "created": "2026-07-22T05:00:00Z",
                        "properties": [
                            {
                                "key": "gearmeshing-ai.idempotency-key",
                                "value": operation_binding(
                                    "update_progress",
                                    "GearMeshing-AI progress (50%): Shared wording",
                                    "run-1:shared-key",
                                ),
                            }
                        ],
                    }
                ],
                "total": 1,
            },
        )

    with pytest.raises(JiraIdempotencyConflictError, match="different operation or payload"):
        await provider(handler, allow_writes=True).report_blocker(
            BlockerUpdate(
                work_item_key="GMAI-17",
                idempotency_key="run-1:shared-key",
                summary="Shared wording",
                details="Await approval",
            )
        )


async def test_parallel_first_writes_expose_documented_jira_check_post_boundary() -> None:
    get_count = 0
    post_count = 0
    both_checked = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.method == "GET":
            get_count += 1
            if get_count == 2:
                both_checked.set()
            await both_checked.wait()
            return httpx.Response(200, json={"comments": [], "total": 0})
        post_count += 1
        return httpx.Response(201, json={"id": str(post_count), "created": "2026-07-22T05:01:00Z"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = JiraWorkManagementProvider(configuration(allow_writes=True), client=client)
    update = ProgressUpdate(
        work_item_key="GMAI-17",
        idempotency_key="run-1:parallel",
        summary="Concurrent first write",
        percent_complete=50,
    )

    await asyncio.gather(adapter.update_progress(update), adapter.update_progress(update))

    # Jira v3 has no conditional transaction spanning the comment lookup and create calls.
    assert get_count == 2
    assert post_count == 2


async def test_new_progress_write_posts_adf_with_an_idempotency_property() -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"comments": [], "total": 0})
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "10002", "created": "2026-07-22T05:01:00Z"})

    receipt = await provider(handler, allow_writes=True).update_progress(
        ProgressUpdate(
            work_item_key="GMAI-17",
            idempotency_key="run-1:progress:75",
            summary="Verifying adapter",
            percent_complete=75,
        )
    )

    assert receipt.provider_reference == "10002"
    assert posted["body"] == adf(paragraph("GearMeshing-AI progress (75%): Verifying adapter"))
    assert posted["properties"] == [
        {
            "key": "gearmeshing-ai.idempotency-key",
            "value": operation_binding(
                "update_progress",
                "GearMeshing-AI progress (75%): Verifying adapter",
                "run-1:progress:75",
            ),
        }
    ]
    assert "not-a-real-token" not in json.dumps(posted)


async def test_blocked_readiness_result_can_be_posted_to_jira() -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"comments": [], "total": 0})
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "10003", "created": "2026-07-22T05:02:00Z"})

    readiness = ReadinessResult(
        work_item_key="GMAI-17",
        problems=(
            ReadinessProblem(
                code="missing-acceptance-criteria",
                summary="Add acceptance criteria",
                details="Add a non-empty Acceptance Criteria section to the Jira description.",
            ),
        ),
    )

    await provider(handler, allow_writes=True).publish_readiness(readiness, "run-1:readiness")

    serialized = json.dumps(posted)
    assert "missing-acceptance-criteria" in serialized
    assert "Add acceptance criteria" in serialized


async def test_non_finite_json_response_is_rejected() -> None:
    adapter = provider(lambda _: httpx.Response(200, content=b'{"unsafe": NaN}'))

    with pytest.raises(JiraResponseError, match="invalid JSON"):
        await adapter.get_work_item("GMAI-17")
