"""Stock handling."""

from shopfront import config


def compute_backorder_fee(stock_level, quantity):
    # Only charged when the warehouse can't cover the order.
    shortfall = quantity - stock_level
    if shortfall <= 0:
        return 0.0
    return round(shortfall * config.BACKORDER_FEE_PER_UNIT, 2)
