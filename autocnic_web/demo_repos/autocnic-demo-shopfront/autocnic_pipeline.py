"""
autocnic_pipeline.py -- AutoCNIC entry point for the shopfront demo project.

The business logic lives in the `shopfront` package; this file only wires those
functions into a Pipeline and supplies the three names the web app looks for:
PIPELINE, SAMPLERS, INITIAL_INPUTS.

Every step's function parameters match the names of the nodes it reads, so
reads are inferred from the signatures. Nothing here declares which
dependencies are conditional -- discovery finds them:
  - tier_discount   reads promo_rate/subtotal only for gold/platinum tiers
  - shipping_cost   reads destination_km/shipping_method only below the free-shipping threshold
  - fraud_score     reads device_risk only when subtotal > 250
  - backorder_fee   is conditional on stock_level < quantity, but that compares two
                    inputs, so the single-variable predicate miner reports it as
                    unconditional (a known limit, kept here as a realistic case)

NOTE: imports must be top-level; the app restores sys.path after import.
"""

import random

from autocnic.pipeline import Pipeline
from shopfront.pricing import compute_subtotal, compute_tier_discount, compute_tax
from shopfront.shipping import compute_shipping_cost
from shopfront.fraud import compute_fraud_score
from shopfront.inventory import compute_backorder_fee
from shopfront.checkout import compute_order_total

PIPELINE = Pipeline("shopfront_checkout")

PIPELINE.input("unit_price")        # float
PIPELINE.input("quantity")          # int
PIPELINE.input("customer_tier")     # "standard" | "gold" | "platinum"
PIPELINE.input("promo_rate")        # float 0..0.3
PIPELINE.input("destination_km")    # float
PIPELINE.input("shipping_method")   # "standard" | "express"
PIPELINE.input("device_risk")       # float 0..1
PIPELINE.input("stock_level")       # int

PIPELINE.step("subtotal")(compute_subtotal)
PIPELINE.step("tier_discount")(compute_tier_discount)
PIPELINE.step("tax")(compute_tax)
PIPELINE.step("shipping_cost")(compute_shipping_cost)
PIPELINE.step("fraud_score")(compute_fraud_score)
PIPELINE.step("backorder_fee")(compute_backorder_fee)
PIPELINE.step("order_total")(compute_order_total)

_rng = random.Random(11)
SAMPLERS = {
    "unit_price": lambda: round(_rng.uniform(5, 120), 2),
    "quantity": lambda: _rng.randint(1, 8),
    "customer_tier": lambda: _rng.choice(["standard", "gold", "platinum"]),
    "promo_rate": lambda: round(_rng.uniform(0.0, 0.3), 3),
    "destination_km": lambda: round(_rng.uniform(1, 400), 1),
    "shipping_method": lambda: _rng.choice(["standard", "express"]),
    "device_risk": lambda: round(_rng.uniform(0.0, 1.0), 3),
    "stock_level": lambda: _rng.randint(0, 10),
}

INITIAL_INPUTS = {
    "unit_price": 40.0,
    "quantity": 3,
    "customer_tier": "standard",
    "promo_rate": 0.1,
    "destination_km": 50.0,
    "shipping_method": "standard",
    "device_risk": 0.2,
    "stock_level": 5,
}
