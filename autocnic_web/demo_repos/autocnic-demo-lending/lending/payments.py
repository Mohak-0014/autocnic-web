"""Amortised payment maths."""


def compute_monthly_payment(loan_amount, apr, term_months):
    r = apr / 12.0
    return round(loan_amount * r / (1 - (1 + r) ** (-term_months)), 2)


def compute_total_cost(monthly_payment, term_months, lock_fee):
    return round(monthly_payment * term_months + lock_fee, 2)
