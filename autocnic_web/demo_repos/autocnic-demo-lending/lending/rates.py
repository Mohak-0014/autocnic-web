"""Interest-rate pricing."""

from lending import config


def compute_apr(credit_score):
    for minimum, apr in config.APR_TIERS:
        if credit_score >= minimum:
            return apr
    return config.APR_FLOOR
