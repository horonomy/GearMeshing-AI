# Synthetic fixture test module. See ../NOTICE for licensing.
import pytest

from inventory_service.pricing import total_price_cents


def test_total_price_cents_multiplies_unit_price_by_quantity() -> None:
    assert total_price_cents(500, 3) == 1500


def test_total_price_cents_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="quantity must not be negative"):
        total_price_cents(500, -1)
