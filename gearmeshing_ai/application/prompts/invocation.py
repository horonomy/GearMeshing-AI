"""Thin seam wiring a rendered prompt to a typed pydantic-ai invocation.

This is a proof-of-concept boundary, not a production capability. It shows
how a :class:`RenderedPrompt` and a Pydantic output schema are meant to be
passed to a ``pydantic_ai.Agent`` -- and, critically, that the resulting
:class:`InvocationRecord` can never be constructed without the
:class:`PromptMetadata` that produced the prompt actually sent to a model.
Real capabilities should follow this same shape when they are built.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from gearmeshing_ai.application.prompts.metadata import PromptMetadata
from gearmeshing_ai.application.prompts.rendering import RenderedPrompt


@dataclass(frozen=True, slots=True)
class InvocationRecord[OutputT: BaseModel]:
    """A typed capability output bound to the prompt/schema versions used.

    Every invocation produces exactly one of these -- there is no code path
    that returns a bare model output without its ``PromptMetadata`` attached,
    which is what lets a caller audit, later, exactly which prompt version
    and schema versions produced a given result.
    """

    output: OutputT
    prompt_metadata: PromptMetadata


class TypedCapabilityInvoker[OutputT: BaseModel]:
    """Runs one rendered prompt through a pydantic-ai ``Agent`` for a typed output.

    ``model`` is caller-supplied so tests can pass a hermetic
    ``pydantic_ai.models.test.TestModel`` -- this module never selects or
    contacts a live model itself.
    """

    def __init__(self, *, output_type: type[OutputT], model: Model) -> None:
        self._agent: Agent[None, OutputT] = Agent(model, output_type=output_type)

    async def invoke(self, rendered_prompt: RenderedPrompt) -> InvocationRecord[OutputT]:
        """Send ``rendered_prompt.text`` to the model and bind its metadata to the result."""
        result = await self._agent.run(rendered_prompt.text)
        return InvocationRecord(output=result.output, prompt_metadata=rendered_prompt.metadata)
