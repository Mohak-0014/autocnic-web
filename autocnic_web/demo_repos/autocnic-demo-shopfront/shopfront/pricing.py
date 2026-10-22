"""Line-item pricing: subtotal, loyalty discount, tax."""

from shopfront import config


def compute_subtotal(unit_price, quantity):
    return round(unit_price * quantity, 2)


def compute_tier_discount(customer_tier, promo_rate, subtotal):
    # Standard customers get nothing, so promo_rate and subtotal are irrelevant for them.
    multiplier = config.TIER_MULTIPLIER.get(customer_tier, 0.0)
    if multiplier == 0.0:
        return 0.0
    return round(subtotal * promo_rate * multiplier, 2)


def compute_tax(subtotal, tier_discount):
    return round((subtotal - tier_discount) * config.TAX_RATE, 2)
