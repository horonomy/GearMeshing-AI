import pytest

from gearmeshing_ai.adapters.jira_adf import AdfParseError, parse_adf


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
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Return diagnostics"}]}
                        ],
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
