"""Rate-lock fees."""

from lending import config


def compute_lock_fee(rate_lock_days, loan_amount):
    # No lock requested means no fee, whatever the loan size.
    if rate_lock_days <= 0:
        return 0.0
    return round(loan_amount * config.LOCK_FEE_RATE_PER_30D * rate_lock_days / 30.0, 2)
