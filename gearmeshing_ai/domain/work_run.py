"""Domain model for a governed engineering work run."""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit


class WorkRunValidationError(ValueError):
    """Raised when work-run data violates a domain invariant."""


class InvalidTransitionError(WorkRunValidationError):
    """Raised when a lifecycle transition is not permitted."""


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def _require_identifier(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(candidate) or ".." in candidate:
        raise WorkRunValidationError(f"{field_name} is not a safe identifier")
    return candidate


def _require_safe_url(value: str, field_name: str, *, schemes: frozenset[str]) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in schemes or not parsed.netloc:
        raise WorkRunValidationError(f"{field_name} must use an allowed absolute URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise WorkRunValidationError(f"{field_name} must not contain credentials")
    if parsed.fragment:
        raise WorkRunValidationError(f"{field_name} must not contain a fragment")
    return candidate


class WorkRunState(StrEnum):
    """A stable state in the governed work-run lifecycle."""

    APPROVED = "approved"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REMEDIATING = "remediating"
    PUBLISHING_DRAFT_PR = "publishing_draft_pr"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {
        WorkRunState.COMPLETED,
        WorkRunState.FAILED,
        WorkRunState.BLOCKED,
        WorkRunState.CANCELLED,
    }
)
