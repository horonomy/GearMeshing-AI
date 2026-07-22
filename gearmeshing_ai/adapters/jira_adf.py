"""Small, bounded parser and builder for Jira Atlassian Document Format."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class AdfDocument:
    """Text and heading-delimited sections extracted from an ADF document."""

    text: str
    sections: Mapping[str, str]


class AdfParseError(ValueError):
    """Raised when Jira returns invalid or unreasonably large ADF."""


@dataclass(slots=True)
class _Budget:
    nodes_left: int
    characters_left: int

    def consume_node(self) -> None:
        self.nodes_left -= 1
        if self.nodes_left < 0:
            raise AdfParseError("ADF exceeds the node limit")

    def consume_text(self, value: str) -> str:
        self.characters_left -= len(value)
        if self.characters_left < 0:
            raise AdfParseError("ADF exceeds the text limit")
        return value


def _content(node: Mapping[str, object]) -> Sequence[object]:
    value = node.get("content", ())
    if not isinstance(value, list | tuple):
        raise AdfParseError("ADF node content must be an array")
    return value


def _render(node: object, budget: _Budget, depth: int) -> str:
    if depth > 32:
        raise AdfParseError("ADF exceeds the nesting limit")
    if not isinstance(node, Mapping):
        raise AdfParseError("ADF nodes must be objects")
    budget.consume_node()
    node_type = node.get("type")
    if not isinstance(node_type, str):
        raise AdfParseError("ADF node type must be a string")
    if node_type == "text":
        value = node.get("text")
        if not isinstance(value, str):
            raise AdfParseError("ADF text nodes must contain text")
        return budget.consume_text(value)
    if node_type == "hardBreak":
        return "\n"

    rendered = "".join(_render(child, budget, depth + 1) for child in _content(node))
    if node_type in {"paragraph", "heading", "blockquote", "codeBlock", "listItem"}:
        return f"{rendered.strip()}\n"
    if node_type in {"bulletList", "orderedList", "tableRow"}:
        return rendered
    if node_type in {"doc", "panel", "table", "tableCell", "tableHeader"}:
        return rendered
    return rendered


def parse_adf(value: object, *, max_nodes: int = 4_096, max_characters: int = 50_000) -> AdfDocument:
    """Parse supported ADF structure without interpreting it as serialized text."""
    if not isinstance(value, Mapping) or value.get("type") != "doc" or value.get("version") != 1:
        raise AdfParseError("description must be an ADF version 1 document")
    if max_nodes < 1 or max_characters < 1:
        raise ValueError("ADF limits must be positive")

    budget = _Budget(max_nodes, max_characters)
    section_values: dict[str, list[str]] = {}
    current_heading: str | None = None
    blocks: list[str] = []
    budget.consume_node()
    for child in _content(value):
        if not isinstance(child, Mapping):
            raise AdfParseError("ADF nodes must be objects")
        rendered = _render(child, budget, 1).strip()
        if not rendered:
            continue
        blocks.append(rendered)
        if child.get("type") == "heading":
            current_heading = rendered.casefold()
            section_values.setdefault(current_heading, [])
        elif current_heading is not None:
            section_values[current_heading].append(rendered)

    sections = MappingProxyType({name: "\n".join(parts).strip() for name, parts in section_values.items()})
    return AdfDocument(text="\n\n".join(blocks).strip(), sections=sections)


def paragraph_document(text: str) -> dict[str, JsonValue]:
    """Build a minimal ADF paragraph after validating bounded caller text."""
    normalized = text.strip()
    if not normalized:
        raise ValueError("comment text must not be empty")
    if len(normalized) > 10_000:
        raise ValueError("comment text must not exceed 10000 characters")
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": normalized}]}],
    }
