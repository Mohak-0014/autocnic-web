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
clone`, actually imports and runs the cloned repo's Python pipeline code (in a
separate, time- and memory-limited process per session), and
actually re-derives the graph and certificates on every update. See the
honest scope note below for what it can and can't analyze.

## Setup

```bash
cd backend
pip install -r requirements.txt
python main.py
```

Then open **http://localhost:8000** in a browser.

## Deployment

The repo root has a `Dockerfile` and a Render blueprint (`render.yaml`).

```bash
docker build -t autocnic-web .
docker run -p 8000:8000 -v autocnic-data:/data -e AUTOCNIC_BASIC_AUTH=me:secret autocnic-web
```

The container runs one web server process as an unprivileged user and stores
clones and certificates in `/data`. Mount a volume there so certificates
survive redeploys. `GET /healthz` is the health check.

**Security:** analyzing a repo imports and runs its Python code on the server.

- Without `AUTOCNIC_BASIC_AUTH`, a server deployment (local paths off) only
  analyzes the **bundled demo projects**: safe to leave open, nothing
  user-submitted runs.
- With `AUTOCNIC_BASIC_AUTH` set, anyone with the password can submit their own
  public repos from GitHub, GitLab or Bitbucket.
- Each live session's project code runs in its **own worker process**
  (`backend/worker.py`) with an empty environment, a wall-clock timeout per
  request, and on Linux a memory cap, a file-size cap and a process cap. A
  project that hangs, crashes, exits or runs out of memory is killed; the user
  gets an error and the server keeps running. The server process is marked
  non-dumpable, so workers can't read its memory or environment (the password).
- Remaining trust assumption: workers run as the same user as the server, so a
  deliberately malicious project could still tamper with `/data` or make
  network requests. Only share the password with people you trust.

### Configuration

All settings are environment variables (see `backend/settings.py`):

| Variable | Default | Meaning |
|---|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` | Bind address |
| `AUTOCNIC_DATA_DIR` | `backend/` (`/data` in Docker) | Where `_clones/` and `cert_store/` live |
| `AUTOCNIC_BASIC_AUTH` | unset | `user:password` to require a login on every page |
| `AUTOCNIC_ALLOW_LOCAL_PATHS` | `1` (`0` in Docker) | Accept local folders and `file://` URLs |
| `AUTOCNIC_ALLOW_ANONYMOUS_REPOS` | `0` | `1` = accept any repo even without a password (not recommended) |
| `AUTOCNIC_GIT_HOSTS` | `github.com,gitlab.com,bitbucket.org` | Hosts repos may be cloned from on a server (`*` = any) |
| `AUTOCNIC_MAX_LIVE_SESSIONS` | `8` | Live sessions (one worker process each); least recently used become read-only |
| `AUTOCNIC_SESSION_IDLE_MINUTES` | `60` | Live sessions idle this long become read-only |
| `AUTOCNIC_MAX_STORED_SESSIONS` | `200` | Session records kept on disk; oldest are deleted |
| `AUTOCNIC_MAX_CONCURRENT_ANALYSES` | `2` | Analyses allowed to run at the same time |
| `AUTOCNIC_ANALYZE_TIMEOUT` | `120` | Seconds a project gets to import and run discovery |
| `AUTOCNIC_UPDATE_TIMEOUT` | `30` | Seconds a project gets to process one input change |
| `AUTOCNIC_WORKER_MEMORY_MB` | `768` | Memory cap per worker process (Linux) |
| `AUTOCNIC_MAX_REPO_MB` | `100` | Largest clone accepted |
| `AUTOCNIC_MAX_TRIALS` | `1000` | Cap on perturbation trials per edge |
| `AUTOCNIC_CLONE_TIMEOUT` | `60` | Seconds before `git clone` is abandoned |
| `AUTOCNIC_CORS_ORIGINS` | unset | Comma-separated origins, only needed for a separately hosted frontend |

## Pages

| Page | URL | What it does |
|---|---|---|
| Analyze | `/` | Paste a GitHub URL or `owner/repo` (or a local folder path when running locally), or click a bundled example. Lists previous sessions. |
| Guide | `/guide` | How to structure a repo for the app, with copyable code and a starter template download. |
| Inputs | `/inputs` | Edit several inputs at once and apply them as a scenario; each change is its own certified update. |
| Live tracker | `/track` | Graph coloured by what was recomputed vs. certified-skipped, a replayable timeline, quick single-input changes, values, totals. |
| Certificates | `/certificates` | Gallery of every certificate with filters (update, class, node, verdict). |
| Certificate | `/certificate?id=` | One printable certificate: plain-English statement, frontier witness, digests, JSON download. |

Sessions from before a server restart open **read-only** (graph, timeline, certificates) with a button to re-analyze.

## Getting a repo to analyze

The repo must follow the **AutoCNIC pipeline convention**: an
`autocnic_pipeline.py` exposing `PIPELINE` and `SAMPLERS` (and optionally
`INITIAL_INPUTS`). The `/guide` page walks through it, and `template/` is a
starter project (also downloadable from `/api/template.zip`).

The Analyze box accepts:

- `owner/repo` or `https://github.com/owner/repo`
- `https://github.com/owner/repo/tree/<branch>/<folder>`, for a branch or a subfolder
- any https git clone URL (on a server: from GitHub, GitLab or Bitbucket)
- a local folder path or `file://` URL (only when `AUTOCNIC_ALLOW_LOCAL_PATHS=1`)

If `autocnic_pipeline.py` isn't at the repo root, the app looks for it up to
3 folders deep. Repos must be public, and pipelines can only import the
standard library and modules in the repo itself.

Four projects are bundled in `demo_repos/` and can be analyzed straight from
the Analyze page. `autocnic-demo-shopfront` is a multi-module project
(package + tests) and the best starting point.

## What happens on Analyze

1. `git clone --depth 1 <url>` (or a copy, for a local folder) into `<data dir>/_clones/<session_id>/`.
   Clones left over from before a restart are deleted at startup.
2. Start a worker process for the session; it imports `autocnic_pipeline.py`, run `pipeline.finalize()` (this is where
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
  runner.py            clone, session + worker management, certificate persistence
  worker.py            per-session process: import + discovery + live updates
  settings.py          environment-variable configuration
  autocnic/             vendored AutoCNIC library (pipeline, static_analysis, synthesize, engine, ...)
  cert_store/           persisted certificates (<session_id>.jsonl) + session metadata
  requirements.txt
frontend/
  index.html guide.html inputs.html track.html certificates.html certificate.html
  app.css app.js        shared styles and helpers
template/               starter project served as /api/template.zip
demo_repos/             bundled example projects (analyzable via demo:<name>)
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
- One server process with optional shared-password auth. Run it behind your
  host's HTTPS. Submitted repos run in limited worker processes, but not in a
  full sandbox (see Security above), so only give the password to people you trust.
