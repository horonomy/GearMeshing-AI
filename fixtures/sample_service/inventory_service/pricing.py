# Synthetic fixture module. See ../NOTICE for licensing.
"""Toy pricing helpers. No real business logic.

NOTE: ``apply_discount`` contains a deliberate bug used by the
``bugfix_discount_rounding`` golden dataset item: it subtracts the raw
``percent`` value as whole cents instead of a percentage of ``price_cents``,
so callers must not rely on its current behavior.
"""

from __future__ import annotations


def apply_discount(price_cents: int, percent: int) -> int:
    """Return ``price_cents`` after applying a ``percent`` percent discount.

    BUG: this should reduce ``price_cents`` by ``percent`` percent, but it
    currently subtracts ``percent`` as a flat number of cents instead.
    """
    return price_cents - percent


def total_price_cents(unit_price_cents: int, quantity: int) -> int:
    """Return the total price in cents for ``quantity`` units."""
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return unit_price_cents * quantity
