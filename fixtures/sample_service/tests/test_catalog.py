# Synthetic fixture test module. See ../NOTICE for licensing.
from inventory_service.catalog import get_item, list_items


def test_list_items_returns_every_catalog_entry() -> None:
    items = list_items()
    assert {item.item_id for item in items} == {"sku-1", "sku-2", "sku-3"}


def test_get_item_returns_known_item() -> None:
    item = get_item("sku-1")
    assert item is not None
    assert item.name == "widget"


def test_get_item_returns_none_for_unknown_id() -> None:
    assert get_item("does-not-exist") is None
