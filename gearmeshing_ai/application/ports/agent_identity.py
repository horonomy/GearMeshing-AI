"""Provider-neutral port for registering and resolving Agent Assembly actor identities.

Today the workflow runs every stage under one constant ``actor_id`` (see
``WorkflowRunner.__init__``). This port lets each governed capability -
orchestration, coding execution, verification, and Draft PR publication -
carry a distinct, auditable identity instead, so a ``WorkRunEvent.actor_id``
can name the specific actor responsible for that event rather than a single
broad service identity.

There is no Agent Assembly SDK in this repository yet, so
``AgentIdentityProvider`` is a swap-in seam: a local/POC resolver satisfies
it today, and a real Agent Assembly identity-provider client can satisfy it
later without changing any caller.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_identifier(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(candidate) or ".." in candidate:
        raise ValueError(f"{field_name} is not a safe identifier")
    return candidate


class ActorRole(StrEnum):
    """A distinct governed capability that acts within a work run."""

    ORCHESTRATION = "orchestration"
    CODING_EXECUTION = "coding_execution"
    VERIFICATION = "verification"
    DRAFT_PR_PUBLICATION = "draft_pr_publication"


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """A resolved, credential-free identity for one actor role.

    ``actor_id`` is what gets attached to governed actions - for example
    ``WorkRunEvent.actor_id`` - so it must never carry credential material.
    It is safe to log, persist, and place in a prompt.
    """

    role: ActorRole
    actor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, ActorRole):
            raise TypeError("role must be an ActorRole")
        object.__setattr__(self, "actor_id", _require_identifier(self.actor_id, "actor_id"))


class AgentIdentityResolutionError(RuntimeError):
    """Raised when a role has no resolvable, validly formatted actor identity.

    Consumers must resolve every ``ActorRole`` they need before invoking a
    ``CodingExecutor`` or ``WorkManagementProvider`` operation. Raising this
    error from ``resolve`` - rather than surfacing a failure partway through
    a governed operation - is what makes missing or invalid identities fail
    before any tool executes.
    """


class AgentIdentityProvider(Protocol):
    """Provider-neutral port for registering or resolving actor identities.

    Declared ``async`` to match this codebase's convention for ports that
    may perform I/O (see ``CodingExecutor.start``,
    ``WorkManagementProvider.get_work_item``), so a real Agent Assembly SDK
    client can implement this port without changing its shape. A local/POC
    resolver may do no I/O at all underneath.
    """

    async def resolve(self, role: ActorRole) -> ActorIdentity:
        """Return the governed identity for ``role`` or raise if unresolvable."""
        ...


@dataclass(frozen=True, slots=True)
class LocalActorCredential:
    """Local POC credential material backing one resolved actor identity.

    Lifecycle: local/POC credentials are provisioned out-of-band per
    environment - exported as process environment variables (see
    ``LocalAgentIdentityResolver.from_environment``) or otherwise supplied
    by the operator - and are never checked into source control or embedded
    in a prompt. They carry no long-term authority and are expected to be
    rotated or discarded per environment. ``token`` is the swap-in point for
    a credential later issued by a real Agent Assembly identity-provider
    client; it is never rendered by ``repr()``/``str()`` and is never copied
    onto an ``ActorIdentity`` or a ``WorkRun`` audit event.
    """

    actor_id: str
    token: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", _require_identifier(self.actor_id, "actor_id"))
        if self.token is not None and not self.token.strip():
            raise ValueError("token must not be blank when provided")


_ACTOR_ID_ENVIRONMENT_PREFIX = "GMAI_ACTOR_ID_"
_ACTOR_TOKEN_ENVIRONMENT_PREFIX = "GMAI_ACTOR_TOKEN_"


class LocalAgentIdentityResolver:
    """POC-safe ``AgentIdentityProvider`` backed by local process state.

    This resolver never contacts Agent Assembly; it exists so the workflow
    can be wired against the ``AgentIdentityProvider`` port today and later
    swapped for a real Agent Assembly SDK client without changing callers.
    Identity format is validated lazily inside ``resolve`` so that a missing
    registration and a malformed identifier fail the same way: a single
    ``AgentIdentityResolutionError`` raised before any tool executes.
    """

    def __init__(
        self,
        actor_ids: Mapping[ActorRole, str],
        *,
        tokens: Mapping[ActorRole, str] | None = None,
    ) -> None:
        self._actor_ids = dict(actor_ids)
        self._tokens = dict(tokens or {})

    @classmethod
    def from_environment(cls, *, environ: Mapping[str, str] | None = None) -> LocalAgentIdentityResolver:
        """Build a resolver from ``GMAI_ACTOR_ID_<ROLE>``/``GMAI_ACTOR_TOKEN_<ROLE>`` variables.

        Only non-blank variables are registered; a role with no variable set
        remains unresolvable and fails closed via ``resolve``. No credential
        is ever hardcoded here or embedded in source - operators supply them
        through the process environment.
        """
        source = environ if environ is not None else os.environ
        actor_ids: dict[ActorRole, str] = {}
        tokens: dict[ActorRole, str] = {}
        for role in ActorRole:
            actor_id = source.get(f"{_ACTOR_ID_ENVIRONMENT_PREFIX}{role.name}")
            if actor_id is not None and actor_id.strip():
                actor_ids[role] = actor_id
            token = source.get(f"{_ACTOR_TOKEN_ENVIRONMENT_PREFIX}{role.name}")
            if token is not None and token.strip():
                tokens[role] = token
        return cls(actor_ids, tokens=tokens)

    # This body never awaits: it reads from an in-memory mapping populated at construction
    # time, per the class docstring's "the resolver may do no I/O at all underneath" contract.
    # It stays async to satisfy the AgentIdentityProvider.resolve Protocol signature.
    async def resolve(self, role: ActorRole) -> ActorIdentity:  # NOSONAR
        if not isinstance(role, ActorRole):
            raise TypeError("role must be an ActorRole")
        raw_actor_id = self._actor_ids.get(role)
        if raw_actor_id is None:
            raise AgentIdentityResolutionError(f"no actor identity is registered for role {role.value!r}")
        try:
            credential = LocalActorCredential(actor_id=raw_actor_id, token=self._tokens.get(role))
            return ActorIdentity(role=role, actor_id=credential.actor_id)
        except ValueError as error:
            raise AgentIdentityResolutionError(
                f"the actor identity registered for role {role.value!r} is invalid"
            ) from error
