# autocnic-demo-lending

A loan-underwriting demo for the AutoCNIC web app. It exists to show the app is
driven by whatever project you give it, not by hardcoded graphs: this project
has a different domain, different node names and a different graph shape from
`autocnic-demo-shopfront`.

```
autocnic_pipeline.py     entry point (PIPELINE, SAMPLERS, INITIAL_INPUTS)
lending/
  config.py              policy constants
  rates.py               APR by credit tier
  payments.py            amortised payment, total cost
  income.py              cosigner boost, effective income, debt-to-income
  risk.py                risk score
  fees.py                rate-lock fee
  decision.py            approve / refer / decline
tests/test_lending.py    unit tests (python -m pytest)
```

Graph: 8 inputs, 9 derived nodes, two sinks (`decision`, `total_cost`).

Conditional edges the app discovers on its own (none of them are declared):
`cosigner_income -> cosigner_boost` when `has_cosigner` is true,
`employment_type -> risk_score` when `dti > ~0.357`, and
`loan_amount -> lock_fee` when `rate_lock_days > ~7.5`.

Analyze it from the app with its folder path, or `git init` it and use a
`file://` URL.
