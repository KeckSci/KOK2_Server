from __future__ import annotations

SHOP_RATE_SCALE = 1000
SHOP_REPAIR_RATE_PERCENT = 20


def scale_shop_price(base_price: int, rate: int) -> int:
    """Apply a native shop multiplier; 1000 means 100 percent."""
    return max(0, int(base_price)) * max(0, int(rate)) // SHOP_RATE_SCALE


def calculate_purchase_price(base_price: int, purchase_rate: int) -> int:
    return scale_shop_price(base_price, purchase_rate)


def calculate_recycle_price(base_price: int, recycle_rate: int) -> int:
    return scale_shop_price(base_price, recycle_rate)


def calculate_repair_price(
    base_price: int,
    current_durability: int,
    maximum_durability: int,
    purchase_rate: int,
) -> int:
    """Return floor((missing/max) * actual NPC sale price * 20%)."""
    maximum = max(0, int(maximum_durability))
    if maximum <= 0:
        return 0
    current = min(maximum, max(0, int(current_durability)))
    missing = maximum - current
    sale_price = calculate_purchase_price(base_price, purchase_rate)
    return missing * sale_price * SHOP_REPAIR_RATE_PERCENT // (maximum * 100)
