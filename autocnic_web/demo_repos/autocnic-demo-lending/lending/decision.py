"""Final underwriting decision."""

from lending import config


def compute_decision(risk_score, dti):
    # A very high risk score is an outright decline; DTI is only checked otherwise.
    if risk_score > config.DECLINE_RISK:
        return "decline"
    if dti > config.DECLINE_DTI:
        return "decline"
    if risk_score > config.REFER_RISK:
        return "refer"
    return "approve"
