"""
Tiny inventory-discount module used as the demo target for the
Adversarial Code Review agent pair (Patcher vs Reviewer).

The ticket handed to the Patcher agent:

    "average_price() should return the average unit price of the
    given items. Also, apply_bulk_discount() should give a 10%
    discount when total quantity across items is >= 50."

The starting implementation below is deliberately buggy in a way
that a naive fix (or an off-the-shelf 'looks good to me' reviewer)
would miss: it crashes on an empty list, and the discount function
mutates the caller's list in place, which is a subtle correctness
bug when the caller re-uses the original list elsewhere.
"""

from dataclasses import dataclass


@dataclass
class Item:
    name: str
    unit_price: float
    quantity: int


def average_price(items: list[Item]) -> float:
    total = sum(i.unit_price for i in items)
    return total / len(items)  # BUG: ZeroDivisionError on empty list


def apply_bulk_discount(items: list[Item]) -> list[Item]:
    total_qty = sum(i.quantity for i in items)
    if total_qty >= 50:
        for i in items:
            i.unit_price = i.unit_price * 0.9  # BUG: mutates caller's objects
    return items
