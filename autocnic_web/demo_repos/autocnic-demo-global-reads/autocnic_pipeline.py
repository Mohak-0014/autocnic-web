"""
autocnic_pipeline.py -- inventory pricing demo (global/closure reads),
AutoCNIC web-app convention.

Same pipeline as the base AutoCNIC package's
examples/global_reads_pipeline.py, reshaped to the three names the web app's
runner.py looks for: PIPELINE, SAMPLERS, INITIAL_INPUTS.

`inventory_multiplier` and `restock_fee` both read `warehouse_status` as a
bare module-level name -- never as a function parameter, never declared in
`reads=[...]`. They're registered with infer_reads=True, so the dependency is
recovered purely by beniget's static use-def analysis
(autocnic/static_analysis.py) when the app calls pipeline.finalize().
`restock_fee` is also conditional in substance: demand_signal only matters
when warehouse_status == "LOW".
"""

import random
from autocnic.pipeline import Pipeline

PIPELINE = Pipeline("inventory_pricing")

PIPELINE.input("base_unit_cost")     # float
PIPELINE.input("warehouse_status")   # str: "LOW" | "OK" | "SURPLUS" -- read as a GLOBAL below
PIPELINE.input("demand_signal")      # float, 0..1


@PIPELINE.step(reads=["base_unit_cost"])
def base_price(base_unit_cost):
    return round(base_unit_cost * 1.15, 2)


@PIPELINE.step(infer_reads=True)
def inventory_multiplier(base_price):
    # warehouse_status is never a parameter -- recovered via static inference.
    if warehouse_status == "LOW":
        return round(base_price * 1.25, 2)
    if warehouse_status == "SURPLUS":
        return round(base_price * 0.85, 2)
    return base_price


@PIPELINE.step(infer_reads=True)
def restock_fee(demand_signal):
    if warehouse_status == "LOW":
        return round(2.0 + 8.0 * demand_signal, 2)
    return 1.0


@PIPELINE.step(reads=["inventory_multiplier", "restock_fee"])
def final_unit_price(inventory_multiplier, restock_fee):
    return round(inventory_multiplier + restock_fee, 2)


_rng = random.Random(11)
SAMPLERS = {
    "base_unit_cost": lambda: round(_rng.uniform(10, 500), 2),
    "warehouse_status": lambda: _rng.choice(["LOW", "OK", "SURPLUS"]),
    "demand_signal": lambda: round(_rng.uniform(0.0, 1.0), 3),
}

INITIAL_INPUTS = {
    "base_unit_cost": 100.0,
    "warehouse_status": "OK",
    "demand_signal": 0.4,
}
