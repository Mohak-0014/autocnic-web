"""Business constants for the shopfront pricing rules."""

FREE_SHIPPING_THRESHOLD = 200.0   # subtotal at/above which shipping is free
FRAUD_REVIEW_THRESHOLD = 250.0    # subtotal above which the deep fraud check runs
HOLD_SCORE = 0.6                  # fraud score above which an order is held
HOLD_FEE = 5.0
BACKORDER_FEE_PER_UNIT = 2.5
TAX_RATE = 0.08

TIER_MULTIPLIER = {"standard": 0.0, "gold": 1.0, "platinum": 1.5}
SHIPPING_BASE = {"standard": 4.5, "express": 12.0}
SHIPPING_PER_KM = {"standard": 0.05, "express": 0.15}
