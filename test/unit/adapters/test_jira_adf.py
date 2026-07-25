import pytest

from gearmeshing_ai.adapters.jira_adf import AdfParseError, paragraph_document, parse_adf


def test_parser_extracts_acceptance_criteria_from_adf_structure() -> None:
    value = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Approved specification"}]},
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Acceptance Criteria"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Return diagnostics"}]}],
                    }
                ],
            },
        ],
    }

    parsed = parse_adf(value)

    assert parsed.sections["acceptance criteria"] == "Return diagnostics"
    with pytest.raises(TypeError):
        parsed.sections["changed"] = "unsafe"  # type: ignore[index]
    with pytest.raises(AdfParseError, match="text limit"):
        parse_adf(value, max_characters=10)


def test_paragraph_document_renders_multiline_text_as_hard_breaks() -> None:
    document = paragraph_document("Blocker: tests failing\n\nSee CI run for details")

    content = document["content"]
    assert isinstance(content, list)
    paragraph = content[0]
    assert isinstance(paragraph, dict)
    assert paragraph["content"] == [
        {"type": "text", "text": "Blocker: tests failing"},
        {"type": "hardBreak"},
        {"type": "hardBreak"},
        {"type": "text", "text": "See CI run for details"},
    ]


def test_paragraph_document_round_trips_through_the_parser() -> None:
    document = paragraph_document("Blocker: tests failing\nSee CI run for details")

    parsed = parse_adf(document)

    assert parsed.text == "Blocker: tests failing\nSee CI run for details"


def test_paragraph_document_rejects_blank_text() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        paragraph_document("   ")


def test_paragraph_document_rejects_text_over_the_length_bound() -> None:
    with pytest.raises(ValueError, match="10000 characters"):
        paragraph_document("x" * 10_001)
