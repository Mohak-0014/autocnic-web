# AutoCNIC web demo

A small live web app around the AutoCNIC prototype: paste a GitHub repo URL,
it clones the repo, discovers the repo's CNIC conditional dependency graph
(perturbation experiments + predicate mining, same as the base package), and
renders it. Change an input value in the browser and it runs a real update
through Algorithm 1 (`ingest_and_certify`) on the backend — only the
genuinely influenced nodes are recomputed, a certificate is issued and
independently verified (Algorithm 2) for every node that was *not*
recomputed, and every certificate is persisted to disk (`backend/cert_store/`)
so the store survives restarts.

This is a real backend, not a static mockup: it actually shells out to `git
clone`, actually imports and runs the cloned repo's Python pipeline code, and
actually re-derives the graph and certificates on every update. See the
honest scope note below for what it can and can't analyze.

## Setup

```bash
cd backend
pip install -r requirements.txt
python main.py
```

Then open **http://localhost:8000** in a browser.

## Pages

| Page | URL | What it does |
|---|---|---|
| Analyze | `/` | Paste a GitHub URL, a `file://` URL or a **local folder path**, or click a bundled project. Lists previous sessions. |
| Inputs | `/inputs` | Edit several inputs at once and apply them as a scenario; each change is its own certified update. |
| Live tracker | `/track` | Graph coloured by what was recomputed vs. certified-skipped, a replayable timeline, quick single-input changes, values, totals. |
| Certificates | `/certificates` | Gallery of every certificate with filters (update, class, node, verdict). |
| Certificate | `/certificate?id=` | One printable certificate: plain-English statement, frontier witness, digests, JSON download. |

Sessions from before a server restart open **read-only** (graph, timeline, certificates) with a button to re-analyze.

## Getting a repo to analyze

The repo must follow the **AutoCNIC pipeline convention**: an
`autocnic_pipeline.py` at the root exposing `PIPELINE` and `SAMPLERS`. Three
projects are bundled in `demo_repos/` and can be analyzed straight from the
Analyze page, with no GitHub push needed. `autocnic-demo-shopfront` is a
multi-module project (package + tests) and the best starting point.

## What happens on Analyze

1. `git clone --depth 1 <url>` (or a copy, for a local folder) into `backend/_clones/<session_id>/`.
2. Import `autocnic_pipeline.py`, run `pipeline.finalize()` (this is where
   beniget-based static inference resolves any `infer_reads=True` steps).
3. Run controlled perturbation experiments (`autocnic/synthesize.py`) —
   perturb one root input at a time, observe whether each declared edge's
   target actually moves, and mine a predicate (numeric threshold, boolean,
   or categorical) when it doesn't always move.
4. Build the CNIC `ConditionalDependencyGraph` from the synthesized
   structure and render it.

## What happens on an input change

The browser sends the new value to `/api/update`. The backend runs the real
`ingest_and_certify` (Algorithm 1) against the current engine state:

- Nodes actually reachable via *active* edges get recomputed (highlighted
  green in the graph).
- Every other derived node gets a certificate instead — classified VACUOUS
  (no path from the changed input at all) or CONDITIONAL (a path exists, but
  every edge on the frontier is inactive in both the pre- and post-update
  state) — verified independently by Algorithm 2 (`verify_certificate`,
  which never re-runs the influence analysis and never calls the underlying
  function), and appended to `backend/cert_store/<session_id>.jsonl`.
- The session stats panel tracks cumulative model invocations vs. what full
  recomputation would have cost, and the certificate table (bottom right)
  shows the full persisted history for the session, reloadable via
  `GET /api/certificates/<session_id>` even after a restart.

## Project layout

```
backend/
  main.py             FastAPI app: /api/analyze, /api/update, /api/session, /api/certificates
  runner.py            clone + import + discovery + live update logic
  autocnic/             vendored AutoCNIC library (pipeline, static_analysis, synthesize, engine, ...)
  cert_store/           persisted certificates (<session_id>.jsonl) + session metadata
  requirements.txt
frontend/
  index.html inputs.html track.html certificates.html certificate.html   the five pages
  app.css app.js        shared styles and helpers
demo_repos/
  autocnic-demo-pricing/           push this to your own GitHub to use it
  autocnic-demo-global-reads/      push this to your own GitHub to use it
```

## Honest limitations (for the faculty demo)

- Repos must follow the `autocnic_pipeline.py` convention above — this is
  not "paste any GitHub repo and get a graph of arbitrary code." That
  general problem is explicitly out of scope (see the base AutoCNIC
  README); this app demonstrates the part that *is* solved: automatic
  conditional-graph discovery plus a certifying, independently-verified
  update engine, once a codebase is organised as named pipeline steps.
- Sessions (the live `Pipeline`/`ConditionalDependencyGraph`/engine `State`
  objects) are in-memory and reset on backend restart; only the certificate
  JSONL files and session metadata on disk survive a restart. Re-running
  `/api/analyze` on the same repo rebuilds a session from scratch quickly.
- Single-process, no auth, no HTTPS — this is a local demo tool, not a
  deployable multi-tenant service.
