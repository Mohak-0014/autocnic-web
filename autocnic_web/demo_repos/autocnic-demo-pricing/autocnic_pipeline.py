"""
autocnic_pipeline.py -- checkout pricing demo, AutoCNIC web-app convention.

This is the same pricing pipeline used in the base AutoCNIC package's
examples/pricing_pipeline.py, reshaped to the three names the web app's
runner.py looks for: PIPELINE, SAMPLERS, INITIAL_INPUTS.

Two dependencies are genuinely conditional in the code but never declared as
such -- this is what the app's discovery step (controlled perturbation
experiments + predicate mining) is supposed to find on its own:
  - loyalty_discount only reads market_promo_rate (and effectively subtotal)
    while the customer is a loyalty member.
  - fraud_review_score only performs the deep review when subtotal exceeds a
    threshold; below it, device_risk_signal never actually matters.

Push this directory as its own GitHub repo (root = this file, or this file at
the repo root) and paste the repo's clone URL into the app.
"""

import random
from autocnic.pipeline import Pipeline

PIPELINE = Pipeline("checkout_pricing")

PIPELINE.input("cart_subtotal_hint")   # float, a rough pre-tax subtotal signal
PIPELINE.input("is_loyalty_member")    # bool
PIPELINE.input("market_promo_rate")    # float, 0..0.3
PIPELINE.input("shipping_distance_km") # float
PIPELINE.input("device_risk_signal")   # float, 0..1 (stand-in fraud signal)


@PIPELINE.step(reads=["cart_subtotal_hint"])
def subtotal(cart_subtotal_hint):
    return round(cart_subtotal_hint, 2)


@PIPELINE.step(reads=["is_loyalty_member", "market_promo_rate", "subtotal"])
def loyalty_discount(is_loyalty_member, market_promo_rate, subtotal):
    if not is_loyalty_member:
        return 0.0
    return round(subtotal * market_promo_rate, 2)


@PIPELINE.step(reads=["shipping_distance_km"])
def shipping_cost(shipping_distance_km):
    return round(4.5 + 0.15 * shipping_distance_km, 2)


@PIPELINE.step(reads=["subtotal", "device_risk_signal"])
def fraud_review_score(subtotal, device_risk_signal):
    if subtotal > 250:
        return round(0.5 + 0.5 * device_risk_signal, 3)
    return 0.05


@PIPELINE.step(reads=["subtotal", "loyalty_discount", "shipping_cost", "fraud_review_score"])
def final_price(subtotal, loyalty_discount, shipping_cost, fraud_review_score):
    hold_fee = 5.0 if fraud_review_score > 0.6 else 0.0
    return round(subtotal - loyalty_discount + shipping_cost + hold_fee, 2)


_rng = random.Random(7)
SAMPLERS = {
    "cart_subtotal_hint": lambda: round(_rng.uniform(10, 500), 2),
    "is_loyalty_member": lambda: _rng.random() < 0.5,
    "market_promo_rate": lambda: round(_rng.uniform(0.0, 0.3), 3),
    "shipping_distance_km": lambda: round(_rng.uniform(1, 80), 1),
    "device_risk_signal": lambda: round(_rng.uniform(0.0, 1.0), 3),
}

INITIAL_INPUTS = {
    "cart_subtotal_hint": 180.0,
    "is_loyalty_member": False,
    "market_promo_rate": 0.1,
    "shipping_distance_km": 12.0,
    "device_risk_signal": 0.2,
}
