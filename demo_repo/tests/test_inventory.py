from inventory import Item, average_price, apply_bulk_discount


def test_average_price_basic():
    items = [Item("widget", 10.0, 1), Item("gadget", 20.0, 1)]
    assert average_price(items) == 15.0


def test_apply_bulk_discount_triggers_at_threshold():
    items = [Item("widget", 10.0, 50)]
    result = apply_bulk_discount(items)
    assert result[0].unit_price == 9.0
