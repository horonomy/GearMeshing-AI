# Synthetic fixture module. See ../NOTICE for licensing.
"""Plain-function toy "API" surface over the catalog and pricing helpers."""

from __future__ import annotations

from inventory_service.catalog import get_item
from inventory_service.pricing import total_price_cents


def get_item_price_cents(item_id: str) -> int | None:
    """Return the unit price in cents for ``item_id``, or ``None`` if unknown."""
    item = get_item(item_id)
    if item is None:
        return None
    return item.unit_price_cents


def get_order_total_cents(item_id: str, quantity: int) -> int | None:
    """Return the total price in cents for ``quantity`` of ``item_id``.

    Returns ``None`` when ``item_id`` is not in the catalog.
    """
    item = get_item(item_id)
    if item is None:
        return None
    return total_price_cents(item.unit_price_cents, quantity)
