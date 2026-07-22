"""Typed, credential-safe Jira adapter failures."""

from __future__ import annotations


class JiraAdapterError(RuntimeError):
    """Base class for failures exposed by the Jira adapter."""


class JiraConfigurationError(JiraAdapterError):
    """Raised when local Jira adapter configuration is invalid."""


class JiraAuthenticationError(JiraAdapterError):
    """Raised when Jira rejects the configured identity."""


class JiraAuthorizationError(JiraAdapterError):
    """Raised when the authenticated identity cannot perform an operation."""


class JiraNotFoundError(JiraAdapterError):
    """Raised when Jira cannot find or disclose a requested resource."""


class JiraRateLimitError(JiraAdapterError):
    """Raised after the bounded rate-limit retry budget is exhausted."""


class JiraResponseError(JiraAdapterError):
    """Raised for malformed, oversized, or unsuccessful Jira responses."""


class JiraTransportError(JiraAdapterError):
    """Raised when a request cannot safely reach Jira."""
