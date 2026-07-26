# Synthetic fixture test module. See ../NOTICE for licensing.
from inventory_service.api import get_item_price_cents, get_order_total_cents


def test_get_item_price_cents_returns_known_price() -> None:
    assert get_item_price_cents("sku-2") == 1200


def test_get_item_price_cents_returns_none_for_unknown_item() -> None:
    assert get_item_price_cents("does-not-exist") is None


def test_get_order_total_cents_computes_total() -> None:
    assert get_order_total_cents("sku-1", 4) == 2000


def test_get_order_total_cents_returns_none_for_unknown_item() -> None:
    assert get_order_total_cents("does-not-exist", 1) is None
