"""Shipping cost rules."""

from shopfront import config


def compute_shipping_cost(subtotal, destination_km, shipping_method):
    # Big orders ship free, so distance and method only matter below the threshold.
    if subtotal >= config.FREE_SHIPPING_THRESHOLD:
        return 0.0
    base = config.SHIPPING_BASE.get(shipping_method, config.SHIPPING_BASE["standard"])
    per_km = config.SHIPPING_PER_KM.get(shipping_method, config.SHIPPING_PER_KM["standard"])
    return round(base + per_km * destination_km, 2)
