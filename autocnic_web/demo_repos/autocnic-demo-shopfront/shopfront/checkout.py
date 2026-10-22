"""Final order total."""

from shopfront import config


def compute_order_total(subtotal, tier_discount, tax, shipping_cost, backorder_fee, fraud_score):
    hold_fee = config.HOLD_FEE if fraud_score > config.HOLD_SCORE else 0.0
    return round(subtotal - tier_discount + tax + shipping_cost + backorder_fee + hold_fee, 2)
