"""Risk scoring."""

from lending import config


def compute_risk_score(dti, credit_score, employment_type):
    score = 0.6 * (850 - credit_score) / 550.0
    if dti <= config.DTI_STRESS_THRESHOLD:
        return round(score + 0.3 * dti, 3)
    # Under stress, job stability starts to matter.
    penalty = config.EMPLOYMENT_PENALTY.get(employment_type, 0.10)
    return round(score + 0.3 + penalty, 3)
