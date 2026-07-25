# Synthetic fixture module. See ../NOTICE for licensing.
"""In-memory toy catalog of items. No real business logic."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    """A single catalog entry."""

    item_id: str
    name: str
    unit_price_cents: int


_CATALOG: dict[str, Item] = {
    "sku-1": Item(item_id="sku-1", name="widget", unit_price_cents=500),
    "sku-2": Item(item_id="sku-2", name="gadget", unit_price_cents=1200),
    "sku-3": Item(item_id="sku-3", name="gizmo", unit_price_cents=750),
}


def list_items() -> tuple[Item, ...]:
    """Return every catalog item."""
    return tuple(_CATALOG.values())


def get_item(item_id: str) -> Item | None:
    """Return the catalog item for ``item_id``, or ``None`` if absent."""
    return _CATALOG.get(item_id)


def find_item_by_name(name: str) -> Item | None:
    """Return the first catalog item whose name matches ``name`` (case-insensitive)."""
    for item in _CATALOG.values():
        if item.name.casefold() == name.casefold():
            return item
    return None
