# autocnic-demo-shopfront

A multi-module demo project for the AutoCNIC web app.

```
autocnic_pipeline.py     <- entry point the app imports (PIPELINE, SAMPLERS, INITIAL_INPUTS)
shopfront/
  config.py              business constants
  pricing.py             subtotal, tier discount, tax
  shipping.py            shipping cost (free above a threshold)
  fraud.py               fraud score (deep check only for big orders)
  inventory.py           backorder fee
  checkout.py            final order total
tests/test_shopfront.py  unit tests for the business logic (pytest)
```

Run the tests: `python -m pytest`

Use it in the app: push to GitHub, or `git init` locally and paste a
`file:///C:/path/to/autocnic-demo-shopfront` URL.

Conditional dependencies the app discovers: tier_discount (customer_tier !=
'standard'), shipping_cost (subtotal < ~200), fraud_score (subtotal > ~250).
backorder_fee depends on stock_level < quantity, a two-input condition the
single-variable miner reports as unconditional (a known limitation).
