"""Underwriting policy constants."""

# (minimum credit score, APR) checked from the top down
APR_TIERS = [(760, 0.055), (700, 0.065), (640, 0.080)]
APR_FLOOR = 0.110

COSIGNER_INCOME_SHARE = 0.5      # fraction of a cosigner's income counted
DTI_STRESS_THRESHOLD = 0.36      # above this, employment type starts to matter
LOCK_FEE_RATE_PER_30D = 0.0025   # of the loan amount, per 30 days locked

EMPLOYMENT_PENALTY = {"salaried": 0.0, "contract": 0.05, "self_employed": 0.15}

DECLINE_RISK = 0.65
REFER_RISK = 0.40
DECLINE_DTI = 0.50
