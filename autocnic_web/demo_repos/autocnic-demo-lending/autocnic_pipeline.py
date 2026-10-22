"""
autocnic_pipeline.py -- AutoCNIC entry point for the lending demo project.

A loan-underwriting pipeline with a deliberately different shape from the
shopfront demo: two output nodes (decision, total_cost), a four-level chain
(cosigner_boost -> effective_income -> dti -> risk_score -> decision), a
boolean gate, a numeric gate on a derived value and a categorical input.

Nothing here declares which dependencies are conditional; discovery finds them.
Expected conditional edges:
  - cosigner_income -> cosigner_boost   only when has_cosigner is true
  - employment_type -> risk_score       only when dti is above ~0.36
  - loan_amount     -> lock_fee         only when rate_lock_days > 0
(dti -> decision is also gated in the code, on risk_score, but the miner reports it
as unconditional: the gate is not a clean threshold on the sampled range. Kept as
a realistic case.)

NOTE: imports must be top-level; the app restores sys.path after import.
"""

import random

from autocnic.pipeline import Pipeline
from lending.rates import compute_apr
from lending.payments import compute_monthly_payment, compute_total_cost
from lending.income import compute_cosigner_boost, compute_effective_income, compute_dti
from lending.risk import compute_risk_score
from lending.fees import compute_lock_fee
from lending.decision import compute_decision

PIPELINE = Pipeline("lending_underwriting")

PIPELINE.input("credit_score")      # int 300..850
PIPELINE.input("annual_income")     # float
PIPELINE.input("cosigner_income")   # float
PIPELINE.input("has_cosigner")      # bool
PIPELINE.input("loan_amount")       # float
PIPELINE.input("term_months")       # int
PIPELINE.input("employment_type")   # "salaried" | "contract" | "self_employed"
PIPELINE.input("rate_lock_days")    # int, 0 = no lock

PIPELINE.step("apr")(compute_apr)
PIPELINE.step("monthly_payment")(compute_monthly_payment)
PIPELINE.step("cosigner_boost")(compute_cosigner_boost)
PIPELINE.step("effective_income")(compute_effective_income)
PIPELINE.step("dti")(compute_dti)
PIPELINE.step("risk_score")(compute_risk_score)
PIPELINE.step("lock_fee")(compute_lock_fee)
PIPELINE.step("decision")(compute_decision)
PIPELINE.step("total_cost")(compute_total_cost)

_rng = random.Random(23)
SAMPLERS = {
    "credit_score": lambda: _rng.randint(450, 830),
    "annual_income": lambda: round(_rng.uniform(30000, 220000), 2),
    "cosigner_income": lambda: round(_rng.uniform(0, 150000), 2),
    "has_cosigner": lambda: _rng.random() < 0.5,
    "loan_amount": lambda: round(_rng.uniform(5000, 350000), 2),
    "term_months": lambda: _rng.choice([12, 24, 36, 60, 120, 240, 360]),
    "employment_type": lambda: _rng.choice(["salaried", "contract", "self_employed"]),
    "rate_lock_days": lambda: _rng.choice([0, 15, 30, 45, 60]),
}

INITIAL_INPUTS = {
    "credit_score": 720,
    "annual_income": 85000.0,
    "cosigner_income": 40000.0,
    "has_cosigner": False,
    "loan_amount": 180000.0,
    "term_months": 240,
    "employment_type": "salaried",
    "rate_lock_days": 0,
}
