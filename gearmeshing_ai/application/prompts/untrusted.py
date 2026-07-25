"""Delimiting strategy that keeps untrusted repository text out of instructions.

Templates render trusted, repository-authored instructions alongside
untrusted text pulled from elsewhere in the world a change was made for
(a diff, a Jira description, tool output, ...). If that untrusted text is
interpolated verbatim, it can contain strings that *look* like the
delimiter tags this module adds, and a careless reader (human or model)
could be tricked into treating injected text as if it were a fresh
instruction boundary.

The strategy:

1. Every untrusted span is wrapped in a fixed, human-readable tag pair --
   ``<<<BEGIN_UNTRUSTED:LABEL>>>`` / ``<<<END_UNTRUSTED:LABEL>>>``.
2. Before wrapping, any occurrence of the delimiter tokens (``<<<`` or
   ``>>>``) *already present* inside the untrusted text is neutralized by
   substituting visually similar but distinct Unicode "single angle
   quotation mark" characters (U+2039/U+203A, tripled). This is
   irreversible and one-directional, so an attacker cannot forge a string
   that, once escaped, reproduces a real delimiter -- the only literal
   ``<<<``/``>>>`` sequences in the rendered output are the ones this
   module adds itself.

The escaping is deterministic (no randomness), so rendering the same
input twice always produces the same output -- required for the content
hash in ``PromptMetadata`` to be reproducible.
"""

from __future__ import annotations

import re

_DELIMITER_OPEN = "<<<"
_DELIMITER_CLOSE = ">>>"
# Visually similar to "<<<"/">>>" but distinct code points (U+2039/U+203A, tripled).
_ESCAPED_OPEN = "‹‹‹"  # noqa: RUF001
_ESCAPED_CLOSE = "›››"  # noqa: RUF001
_LABEL_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class UntrustedLabelError(ValueError):
    """Raised when a delimiter label does not match the required format."""


def _validate_label(label: str) -> str:
    normalized = label.strip()
    if _LABEL_PATTERN.fullmatch(normalized) is None:
        raise UntrustedLabelError(f"label {label!r} must be UPPER_SNAKE_CASE")
    return normalized


def _escape_delimiter_lookalikes(text: str) -> str:
    return text.replace(_DELIMITER_OPEN, _ESCAPED_OPEN).replace(_DELIMITER_CLOSE, _ESCAPED_CLOSE)


def wrap_untrusted(text: str, label: str = "UNTRUSTED_CONTENT") -> str:
    """Wrap ``text`` so it can never be mistaken for a trusted instruction.

    Any delimiter-shaped substrings already present in ``text`` are
    neutralized first, so the only real ``<<<...>>>`` tags in the output
    are the boundary this function adds.
    """
    normalized_label = _validate_label(label)
    escaped = _escape_delimiter_lookalikes(text)
    open_tag = f"{_DELIMITER_OPEN}BEGIN_UNTRUSTED:{normalized_label}{_DELIMITER_CLOSE}"
    close_tag = f"{_DELIMITER_OPEN}END_UNTRUSTED:{normalized_label}{_DELIMITER_CLOSE}"
    return f"{open_tag}\n{escaped}\n{close_tag}"
