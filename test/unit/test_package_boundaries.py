from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "gearmeshing_ai.domain",
        "gearmeshing_ai.application",
        "gearmeshing_ai.application.ports",
        "gearmeshing_ai.adapters",
        "gearmeshing_ai.runtime",
        "gearmeshing_ai.interfaces",
    ),
)
def test_package_boundary_is_importable(module_name: str) -> None:
    assert import_module(module_name).__name__ == module_name
