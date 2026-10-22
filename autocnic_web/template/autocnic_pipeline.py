"""
autocnic_pipeline.py -- the one file the AutoCNIC app looks for.

Keep it at the root of your repo (or in a subfolder and paste the GitHub link to
that folder). It must define:

  PIPELINE        a Pipeline: inputs + named steps
  SAMPLERS        {input_name: function returning a random valid value}
  INITIAL_INPUTS  (optional) the starting value of each input

Rename `my_project` and replace the example steps with your own logic.
"""

import random

from autocnic.pipeline import Pipeline  # provided by the app; nothing to install
from my_project.logic import ticket_price, booking_fee, total_due

PIPELINE = Pipeline("ticket_checkout")

# 1. Inputs: the raw values a user can change.
PIPELINE.input("base_fare")      # float
PIPELINE.input("seats")          # int
PIPELINE.input("member")         # bool
PIPELINE.input("card_fee_rate")  # float 0..0.05

# 2. Steps: each step's name is the value it produces. A function parameter
#    whose name matches an input or another step is read automatically.
PIPELINE.step("ticket_price")(ticket_price)
PIPELINE.step("booking_fee")(booking_fee)
PIPELINE.step("total_due")(total_due)

# 3. One random-value generator per input, used to discover which
#    dependencies only matter under some condition.
_rng = random.Random(0)
SAMPLERS = {
    "base_fare": lambda: round(_rng.uniform(10, 200), 2),
    "seats": lambda: _rng.randint(1, 6),
    "member": lambda: _rng.choice([True, False]),
    "card_fee_rate": lambda: round(_rng.uniform(0.0, 0.05), 3),
}

# 4. Optional: what the inputs start at when the graph first loads.
INITIAL_INPUTS = {
    "base_fare": 80.0,
    "seats": 2,
    "member": False,
    "card_fee_rate": 0.02,
}
