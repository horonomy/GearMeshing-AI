"""Jira Cloud REST v3 implementation of the work-management boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Final, cast
from unicodedata import category
from urllib.parse import quote, urlsplit

import httpx

from gearmeshing_ai.adapters.jira_adf import JsonValue, paragraph_document, parse_adf
from gearmeshing_ai.adapters.jira_errors import (
    JiraAuthenticationError,
    JiraAuthorizationError,
    JiraConfigurationError,
    JiraIdempotencyConflictError,
    JiraNotFoundError,
    JiraRateLimitError,
    JiraResponseError,
    JiraTransportError,
    JiraWriteValidationError,
)
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
    WorkItem,
    WorkManagementCapability,
    WorkManagementProvider,
)

_REPOSITORY_PROPERTY: Final = "gearmeshing-ai.repository"
_IDEMPOTENCY_PROPERTY: Final = "gearmeshing-ai.idempotency-key"
_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$")


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is not supported")


def _bounded_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise JiraConfigurationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise JiraConfigurationError(f"{field_name} must contain 1 through {maximum} characters")
    if any(category(character).startswith("C") for character in normalized):
        raise JiraConfigurationError(f"{field_name} must not contain control characters")
    return normalized


def _site_url(value: str) -> str:
    normalized = _bounded_text(value, "site_url", 2_048).rstrip("/")
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError as error:
        raise JiraConfigurationError("site_url contains an invalid port") from error
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise JiraConfigurationError("site_url must be an HTTPS origin without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise JiraConfigurationError("site_url must not contain a path, query, or fragment")
    return normalized


@dataclass(frozen=True, slots=True)
class JiraConfiguration:
    """Immutable Jira connection and bounded retry policy."""

    site_url: str
    email: str = field(repr=False)
    api_token: str = field(repr=False, compare=False)
    project_key: str
    repository: RepositoryReference
    allow_writes: bool = False
    timeout_seconds: float = 15.0
    max_response_bytes: int = 1_000_000
    max_rate_limit_retries: int = 3
    retry_base_seconds: float = 0.5
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "site_url", _site_url(self.site_url))
        object.__setattr__(self, "email", _bounded_text(self.email, "email", 320))
        object.__setattr__(self, "api_token", _bounded_text(self.api_token, "api_token", 4_096))
        project_key = _bounded_text(self.project_key, "project_key", 10)
        if not _PROJECT_KEY.fullmatch(project_key):
            raise JiraConfigurationError("project_key must be an uppercase Jira project key")
        object.__setattr__(self, "project_key", project_key)
        if not isinstance(self.repository, RepositoryReference):
            raise JiraConfigurationError("repository must be a RepositoryReference")
        if not isinstance(self.allow_writes, bool):
            raise JiraConfigurationError("allow_writes must be a boolean")
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("retry_base_seconds", self.retry_base_seconds),
            ("max_retry_after_seconds", self.max_retry_after_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value) or value <= 0:
                raise JiraConfigurationError(f"{name} must be a positive finite number")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1_024 <= self.max_response_bytes <= 10_000_000
        ):
            raise JiraConfigurationError("max_response_bytes must be from 1024 through 10000000")
        if (
            isinstance(self.max_rate_limit_retries, bool)
            or not isinstance(self.max_rate_limit_retries, int)
            or not 0 <= self.max_rate_limit_retries <= 5
        ):
            raise JiraConfigurationError("max_rate_limit_retries must be from 0 through 5")


class JiraWorkManagementProvider(WorkManagementProvider):
    """Load validated work and publish idempotent evidence using Jira REST v3."""

    def __init__(
        self,
        configuration: JiraConfiguration,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._configuration = configuration
        self._sleep = sleep
        self._owns_client = client is None
        try:
            self._client = client or httpx.AsyncClient()
        except (httpx.InvalidURL, ValueError) as error:
            raise JiraConfigurationError("Jira client URL configuration is invalid") from error

    @property
    def name(self) -> str:
        return "jira"

    @property
    def capabilities(self) -> ProviderCapabilities:
        capabilities = {
            WorkManagementCapability.READ_WORK_ITEM,
            WorkManagementCapability.EVALUATE_READINESS,
        }
        if self._configuration.allow_writes:
            capabilities.update(
                {
                    WorkManagementCapability.UPDATE_PROGRESS,
                    WorkManagementCapability.REPORT_BLOCKER,
                    WorkManagementCapability.COMPLETE_WORK,
                    WorkManagementCapability.ATTACH_ARTIFACT,
                }
            )
        return ProviderCapabilities(capabilities)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> JiraWorkManagementProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _retry_delay(self, response: httpx.Response, retry_number: int) -> float:
        maximum = self._configuration.max_retry_after_seconds
        raw_value = response.headers.get("Retry-After", "").strip()
        if raw_value:
            try:
                delay = float(raw_value)
                if isfinite(delay) and 0 <= delay <= maximum:
                    return delay
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(raw_value)
                    if deadline.tzinfo is not None:
                        delay = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
                        if isfinite(delay) and delay <= maximum:
                            return delay
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(self._configuration.retry_base_seconds * (2**retry_number), maximum))

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, JsonValue] | None = None,
    ) -> object:
        url = f"{self._configuration.site_url}{path}"
        for attempt in range(self._configuration.max_rate_limit_retries + 1):
            try:
                async with self._client.stream(
                    method,
                    url,
                    params=params,
                    json=body,
                    auth=httpx.BasicAuth(self._configuration.email, self._configuration.api_token),
                    headers={"Accept": "application/json"},
                    timeout=self._configuration.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 429:
                        if attempt == self._configuration.max_rate_limit_retries:
                            raise JiraRateLimitError("Jira rate limit persisted after bounded retries")
                        await self._sleep(self._retry_delay(response, attempt))
                        continue
                    if response.status_code == 401:
                        raise JiraAuthenticationError(
                            "Jira authentication failed; verify the account email and replace the API token"
                        )
                    if response.status_code == 403:
                        raise JiraAuthorizationError(
                            "Jira denied this operation; grant the account project and issue permissions"
                        )
                    if response.status_code == 404:
                        raise JiraNotFoundError("Jira resource was not found or is not visible to this account")
                    if not 200 <= response.status_code < 300:
                        raise JiraResponseError(f"Jira returned HTTP {response.status_code} for {method.upper()}")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > self._configuration.max_response_bytes:
                            raise JiraResponseError("Jira response exceeded the configured byte limit")
            except (
                JiraAuthenticationError,
                JiraAuthorizationError,
                JiraNotFoundError,
                JiraRateLimitError,
                JiraResponseError,
            ):
                raise
            except httpx.InvalidURL as error:
                raise JiraConfigurationError("Jira request URL is invalid") from error
            except httpx.RequestError as error:
                raise JiraTransportError("Jira request failed before a valid response was received") from error
            if not payload:
                return None
            try:
                return json.loads(payload, parse_constant=_reject_nonfinite_json)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise JiraResponseError("Jira returned invalid JSON") from error
        raise AssertionError("rate-limit loop exhausted unexpectedly")

    def _validated_issue_key(self, value: str) -> str:
        normalized = value.strip() if isinstance(value, str) else ""
        if not _ISSUE_KEY.fullmatch(normalized) or not normalized.startswith(f"{self._configuration.project_key}-"):
            raise ValueError("work_item_key must belong to the configured Jira project")
        return normalized

    @staticmethod
    def _object(value: object, context: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise JiraResponseError(f"Jira returned invalid {context}")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _string(value: object, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise JiraResponseError(f"Jira returned invalid {context}")
        return value.strip()

    @classmethod
    def _timestamp(cls, value: object, context: str) -> datetime:
        raw_value = cls._string(value, context)
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timestamp is timezone-naive")
        except ValueError as error:
            raise JiraResponseError(f"Jira returned invalid {context}") from error
        return parsed

    def _repository(self, properties: Mapping[str, object]) -> tuple[RepositoryReference, bool]:
        raw_repository = properties.get(_REPOSITORY_PROPERTY)
        if raw_repository is None:
            return self._configuration.repository, True
        repository = self._object(raw_repository, "repository property")
        try:
            parsed = RepositoryReference(
                provider=self._string(repository.get("provider"), "repository provider"),
                owner=self._string(repository.get("owner"), "repository owner"),
                name=self._string(repository.get("name"), "repository name"),
                web_url=self._string(repository.get("webUrl"), "repository web URL"),
            )
        except (TypeError, ValueError) as error:
            raise JiraResponseError("Jira repository property is invalid") from error
        if parsed != self._configuration.repository:
            raise JiraResponseError("Jira repository property does not match the configured repository")
        return parsed, True

    async def _get_work_item(self, work_item_key: str) -> WorkItem:
        key = self._validated_issue_key(work_item_key)
        payload = self._object(
            await self._request_json(
                "GET",
                f"/rest/api/3/issue/{quote(key, safe='')}",
                params={
                    "fields": "summary,description,status,labels,issuetype",
                    "properties": _REPOSITORY_PROPERTY,
                },
            ),
            "issue",
        )
        try:
            return self._normalize_work_item(key, payload)
        except JiraResponseError:
            raise
        except (TypeError, ValueError) as error:
            raise JiraResponseError("Jira issue payload cannot be normalized safely") from error

    def _normalize_work_item(self, key: str, payload: Mapping[str, object]) -> WorkItem:
        response_key = self._string(payload.get("key"), "issue key")
        if response_key != key:
            raise JiraResponseError("Jira returned an issue key that does not match the request")
        fields = self._object(payload.get("fields"), "issue fields")
        description_value = fields.get("description")
        parsed_description = parse_adf(description_value) if description_value is not None else None
        description_present = parsed_description is not None and bool(parsed_description.text)
        description = (
            " ".join(parsed_description.text.splitlines())
            if description_present and parsed_description is not None
            else ""
        )
        acceptance_criteria = (
            "" if parsed_description is None else parsed_description.sections.get("acceptance criteria", "")
        )
        normalized_criteria = tuple(line.strip() for line in acceptance_criteria.splitlines() if line.strip())
        raw_labels = fields.get("labels", [])
        if not isinstance(raw_labels, list) or not all(isinstance(label, str) for label in raw_labels):
            raise JiraResponseError("Jira returned invalid labels")
        status = self._object(fields.get("status"), "status")
        issue_type = self._object(fields.get("issuetype"), "issue type")
        properties = self._object(payload.get("properties", {}), "issue properties")
        repository, repository_context_present = self._repository(properties)
        return WorkItem(
            key=response_key,
            title=self._string(fields.get("summary"), "summary"),
            description=description,
            acceptance_criteria=normalized_criteria,
            status=self._string(status.get("name"), "status name"),
            web_url=f"{self._configuration.site_url}/browse/{quote(key, safe='')}",
            repository=repository,
            labels=tuple(cast(list[str], raw_labels)),
            metadata=Metadata(
                {
                    "description_present": description_present,
                    "issue_type": self._string(issue_type.get("name"), "issue type name"),
                    "repository_context_present": repository_context_present,
                }
            ),
        )

    async def _evaluate_readiness(self, work_item: WorkItem) -> ReadinessResult:
        metadata = work_item.metadata.values
        problems: list[ReadinessProblem] = []
        if metadata.get("issue_type") not in {"Story", "Task"}:
            problems.append(
                ReadinessProblem(
                    code="unsupported-issue-type",
                    summary="Use a supported Jira issue type",
                    details="MVP 1 accepts Jira Story and Task issues only.",
                )
            )
        if "spec-ready" not in {label.casefold() for label in work_item.labels}:
            problems.append(
                ReadinessProblem(
                    code="spec-not-ready",
                    summary="Mark the specification ready",
                    details="Add the spec-ready label after the Spec Owner approves the issue.",
                )
            )
        if metadata.get("description_present") is not True:
            problems.append(
                ReadinessProblem(
                    code="missing-description",
                    summary="Add the approved specification",
                    details="Provide a non-empty Jira description before execution.",
                )
            )
        if not work_item.acceptance_criteria:
            problems.append(
                ReadinessProblem(
                    code="missing-acceptance-criteria",
                    summary="Add acceptance criteria",
                    details="Add a non-empty Acceptance Criteria section to the Jira description.",
                )
            )
        return ReadinessResult(work_item_key=work_item.key, problems=tuple(problems))

    async def _find_comment(
        self,
        key: str,
        idempotency_key: str,
        operation_binding: Mapping[str, JsonValue],
    ) -> OperationReceipt | None:
        start_at = 0
        for _ in range(10):
            payload = self._object(
                await self._request_json(
                    "GET",
                    f"/rest/api/3/issue/{quote(key, safe='')}/comment",
                    params={"startAt": start_at, "maxResults": 100, "expand": "properties"},
                ),
                "comment page",
            )
            comments = payload.get("comments", [])
            if not isinstance(comments, list):
                raise JiraResponseError("Jira returned invalid comments")
            for raw_comment in comments:
                comment = self._object(raw_comment, "comment")
                properties = comment.get("properties", [])
                if not isinstance(properties, list):
                    raise JiraResponseError("Jira returned invalid comment properties")
                matching_property: Mapping[object, object] | None = None
                for prop in properties:
                    if not isinstance(prop, Mapping) or prop.get("key") != _IDEMPOTENCY_PROPERTY:
                        continue
                    property_value = prop.get("value")
                    if property_value == operation_binding:
                        matching_property = prop
                        break
                    if property_value == idempotency_key or (
                        isinstance(property_value, Mapping) and property_value.get("idempotencyKey") == idempotency_key
                    ):
                        raise JiraIdempotencyConflictError(
                            "Jira idempotency key is already bound to a different operation or payload"
                        )
                if matching_property is not None:
                    return OperationReceipt(
                        provider=self.name,
                        work_item_key=key,
                        idempotency_key=idempotency_key,
                        provider_reference=self._string(comment.get("id"), "comment ID"),
                        accepted_at=self._timestamp(comment.get("created"), "comment creation timestamp"),
                    )
            total = payload.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or start_at + len(comments) >= total:
                return None
            start_at += len(comments)
            if not comments:
                return None
        raise JiraResponseError("Jira comment history exceeds the idempotency scan limit")

    async def _publish_comment(
        self,
        capability: WorkManagementCapability,
        work_item_key: str,
        idempotency_key: str,
        text: str,
    ) -> OperationReceipt:
        """Deduplicate retries before Jira's non-atomic comment creation boundary.

        Jira REST v3 provides no conditional transaction spanning the comment
        lookup and create calls. Callers must serialize concurrent first writes
        that share an idempotency key; ordinary retries are deduplicated here.
        """
        self.require_capability(capability)
        key = self._validated_issue_key(work_item_key)
        try:
            comment_document = paragraph_document(text)
        except ValueError as error:
            raise JiraWriteValidationError("Jira comment exceeds the supported ADF text boundary") from error
        operation_digest = hashlib.sha256(f"{capability.value}\0{text}".encode()).hexdigest()
        operation_binding: dict[str, JsonValue] = {
            "idempotencyKey": idempotency_key,
            "operationDigest": operation_digest,
        }
        existing = await self._find_comment(key, idempotency_key, operation_binding)
        if existing is not None:
            return existing
        payload = self._object(
            await self._request_json(
                "POST",
                f"/rest/api/3/issue/{quote(key, safe='')}/comment",
                body={
                    "body": comment_document,
                    "properties": [{"key": _IDEMPOTENCY_PROPERTY, "value": operation_binding}],
                },
            ),
            "created comment",
        )
        return OperationReceipt(
            provider=self.name,
            work_item_key=key,
            idempotency_key=idempotency_key,
            provider_reference=self._string(payload.get("id"), "comment ID"),
            accepted_at=self._timestamp(payload.get("created"), "comment creation timestamp"),
            metadata=Metadata({"operation_digest": operation_digest}),
        )

    async def publish_readiness(self, result: ReadinessResult, idempotency_key: str) -> OperationReceipt:
        """Post the normalized validation decision without exposing hidden reasoning."""
        if result.ready:
            return await self._publish_comment(
                WorkManagementCapability.UPDATE_PROGRESS,
                result.work_item_key,
                idempotency_key,
                "GearMeshing-AI specification validation passed.",
            )
        diagnostics = "\n".join(
            f"- [{problem.code}] {problem.summary}: {problem.details}" for problem in result.problems
        )
        return await self._publish_comment(
            WorkManagementCapability.REPORT_BLOCKER,
            result.work_item_key,
            idempotency_key,
            f"GearMeshing-AI specification validation blocked:\n{diagnostics}",
        )

    async def _update_progress(self, update: ProgressUpdate) -> OperationReceipt:
        return await self._publish_comment(
            WorkManagementCapability.UPDATE_PROGRESS,
            update.work_item_key,
            update.idempotency_key,
            f"GearMeshing-AI progress ({update.percent_complete}%): {update.summary}",
        )

    async def _report_blocker(self, update: BlockerUpdate) -> OperationReceipt:
        return await self._publish_comment(
            WorkManagementCapability.REPORT_BLOCKER,
            update.work_item_key,
            update.idempotency_key,
            f"GearMeshing-AI blocker: {update.summary}\n\n{update.details}",
        )

    async def _complete_work(self, update: CompletionUpdate) -> OperationReceipt:
        evidence = "\n".join(f"- {url}" for url in update.evidence_urls)
        return await self._publish_comment(
            WorkManagementCapability.COMPLETE_WORK,
            update.work_item_key,
            update.idempotency_key,
            f"GearMeshing-AI completed: {update.summary}\n\nEvidence:\n{evidence}",
        )

    async def _attach_artifact(self, update: ArtifactUpdate) -> OperationReceipt:
        return await self._publish_comment(
            WorkManagementCapability.ATTACH_ARTIFACT,
            update.work_item_key,
            update.idempotency_key,
            f"GearMeshing-AI artifact ({update.kind}): {update.name}\n{update.web_url}",
        )
