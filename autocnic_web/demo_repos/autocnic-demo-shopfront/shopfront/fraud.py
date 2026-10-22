"""Fraud screening."""

from shopfront import config


def compute_fraud_score(subtotal, device_risk):
    # The device signal is only consulted for large orders.
    if subtotal > config.FRAUD_REVIEW_THRESHOLD:
        return round(0.5 + 0.5 * device_risk, 3)
    return 0.05
