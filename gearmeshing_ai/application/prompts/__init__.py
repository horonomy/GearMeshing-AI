"""Versioned prompt registry, metadata, and typed-invocation seam."""

from __future__ import annotations

from gearmeshing_ai.application.prompts.invocation import InvocationRecord, TypedCapabilityInvoker
from gearmeshing_ai.application.prompts.metadata import (
    PromptDescriptor,
    PromptMetadata,
    PromptValidationError,
)
from gearmeshing_ai.application.prompts.rendering import (
    DEFAULT_PROMPTS_ROOT,
    PromptNotFoundError,
    PromptRegistry,
    PromptRenderError,
    RenderedPrompt,
)
from gearmeshing_ai.application.prompts.untrusted import UntrustedLabelError, wrap_untrusted

__all__ = [
    "DEFAULT_PROMPTS_ROOT",
    "InvocationRecord",
    "PromptDescriptor",
    "PromptMetadata",
    "PromptNotFoundError",
    "PromptRegistry",
    "PromptRenderError",
    "PromptValidationError",
    "RenderedPrompt",
    "TypedCapabilityInvoker",
    "UntrustedLabelError",
    "wrap_untrusted",
]
