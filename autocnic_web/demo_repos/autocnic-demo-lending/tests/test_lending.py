from lending.rates import compute_apr
from lending.payments import compute_monthly_payment, compute_total_cost
from lending.income import compute_cosigner_boost, compute_effective_income, compute_dti
from lending.risk import compute_risk_score
from lending.fees import compute_lock_fee
from lending.decision import compute_decision


def test_apr_tiers():
    assert compute_apr(780) == 0.055
    assert compute_apr(720) == 0.065
    assert compute_apr(650) == 0.080
    assert compute_apr(500) == 0.110


def test_payment_and_total():
    p = compute_monthly_payment(100000.0, 0.06, 360)
    assert 599 < p < 600
    assert compute_total_cost(500.0, 12, 100.0) == 6100.0


def test_cosigner_only_counts_when_present():
    assert compute_cosigner_boost(False, 90000.0) == 0.0
    assert compute_cosigner_boost(True, 90000.0) == 45000.0
    assert compute_effective_income(80000.0, 45000.0) == 125000.0


def test_dti():
    assert compute_dti(1000.0, 60000.0) == 0.2


def test_risk_employment_matters_only_under_stress():
    calm_a = compute_risk_score(0.2, 700, "salaried")
    calm_b = compute_risk_score(0.2, 700, "self_employed")
    assert calm_a == calm_b
    assert compute_risk_score(0.5, 700, "self_employed") > compute_risk_score(0.5, 700, "salaried")


def test_lock_fee():
    assert compute_lock_fee(0, 200000.0) == 0.0
    assert compute_lock_fee(30, 200000.0) == 500.0


def test_decision():
    assert compute_decision(0.9, 0.1) == "decline"
    assert compute_decision(0.3, 0.6) == "decline"
    assert compute_decision(0.5, 0.2) == "refer"
    assert compute_decision(0.2, 0.2) == "approve"
