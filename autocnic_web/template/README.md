# AutoCNIC starter project

```
autocnic_pipeline.py   <- the entry point the app imports (PIPELINE, SAMPLERS, INITIAL_INPUTS)
my_project/
  __init__.py
  logic.py             <- your step functions
```

1. Replace `my_project/logic.py` with your own functions and wire them up in
   `autocnic_pipeline.py`.
2. Push to a **public** GitHub repo.
3. Paste the repo URL (or `owner/repo`) into the app's Analyze box.

Rules of thumb:

- Every input needs a sampler in `SAMPLERS` that returns realistic random values.
- Step functions should be pure: no network, files, clocks or randomness inside them.
- Use only the Python standard library plus modules inside this repo. The app
  provides `autocnic.pipeline`; don't add it to your requirements.
- Keep imports at the top of `autocnic_pipeline.py`.
