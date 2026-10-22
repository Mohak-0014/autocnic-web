"""Income assessment."""

from lending import config


def compute_cosigner_boost(has_cosigner, cosigner_income):
    # Without a cosigner their income is irrelevant.
    if not has_cosigner:
        return 0.0
    return round(cosigner_income * config.COSIGNER_INCOME_SHARE, 2)


def compute_effective_income(annual_income, cosigner_boost):
    return round(annual_income + cosigner_boost, 2)


def compute_dti(monthly_payment, effective_income):
    return round(monthly_payment * 12.0 / effective_income, 4)
