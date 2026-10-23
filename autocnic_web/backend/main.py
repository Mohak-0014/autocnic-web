"""
main.py
--------
FastAPI backend for the AutoCNIC web demo. Serves the frontend and exposes:

  POST /api/analyze          {repo_url}                 -> clone + discover + build graph
  POST /api/update           {session_id, input_node, value} -> live ingest_and_certify
  GET  /api/session/{id}                                  -> current graph + state
  GET  /api/certificates/{id}                              -> full persisted certificate history
  GET  /api/certificate/{id}/{cert_id}                     -> one certificate
  GET  /api/sessions                                       -> all sessions on disk
  GET  /api/session/{id}/history                           -> per-update timeline
  GET  /api/demos                                          -> bundled example projects
  GET  /api/template.zip                                   -> starter project to copy
  GET  /healthz                                            -> liveness probe
  Pages: / (analyze), /guide, /inputs, /track, /certificates, /certificate?s=&id=

Run:  pip install -r requirements.txt && python main.py
Then open http://localhost:8000 . Configuration: see settings.py.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import io
import os
import secrets
import sys
import traceback
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import runner
import settings


@contextlib.asynccontextmanager
async def lifespan(_app):
    if sys.platform.startswith("linux"):
        # Project workers run as this same user. Non-dumpable means they can't
        # read this process's memory or /proc/<pid>/environ (the password).
        try:
            ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
        except (OSError, AttributeError):
            pass
    if not settings.REPOS_ENABLED:
        print("AutoCNIC: no AUTOCNIC_BASIC_AUTH set -- only the bundled demo projects can be "
              "analyzed. Set it to let users submit their own repositories.", flush=True)
    yield
    runner.shutdown()


app = FastAPI(title="AutoCNIC Web Demo", lifespan=lifespan)
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
    )

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(APP_DIR, "frontend")
TEMPLATE_DIR = os.path.join(APP_DIR, "template")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional shared password (AUTOCNIC_BASIC_AUTH=user:pass). Analyze runs the
    submitted repo's code on this server, so a public deployment should set it."""
    if settings.BASIC_AUTH and request.url.path != "/healthz":
        expected = "Basic " + base64.b64encode(settings.BASIC_AUTH.encode()).decode()
        if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            return Response("authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="AutoCNIC"'})
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(max_length=500)
    n_trials_per_edge: int = Field(300, ge=10)
    seed: int = 0


class UpdateRequest(BaseModel):
    session_id: str
    input_node: str
    value: object


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    repo_url = req.repo_url.strip()
    if not repo_url:
        raise HTTPException(400, "repo_url is required")
    n_trials = min(req.n_trials_per_edge, settings.MAX_TRIALS)
    try:
        session = runner.analyze_repo(repo_url, n_trials_per_edge=n_trials, seed=req.seed)
    except runner.RepoLoadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"unexpected error analyzing repo: {e!r}")

    return {
        "session_id": session.id,
        "repo_url": session.repo_url,
        "graph": session.graph,
        "state": session.state,
        "inputs": {n: {"current_value": session.state["values"].get(n)} for n in session.input_nodes},
        "synthesized_edges": session.synthesized_desc,
    }


@app.post("/api/update")
def update(req: UpdateRequest):
    session = runner.SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(404, "unknown session_id (re-analyze the repo)")
    try:
        result = runner.apply_update(session, req.input_node, req.value)
    except runner.RepoLoadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"unexpected error applying update: {e!r}")
    return result


def _cumulative_from_history(session_id: str) -> dict:
    hist = runner.load_history(session_id)
    certs = runner.load_certificates(session_id)
    base = sum(h["baseline_invocations"] for h in hist)
    cnic = sum(h["model_invocations"] for h in hist)
    return {
        "updates_processed": len(hist),
        "baseline_invocations": base,
        "cnic_invocations": cnic,
        "reduction_pct": round(100.0 * (1 - cnic / base), 1) if base else 0.0,
        "certificates_issued": len(certs),
        "certificates_verified_ok": sum(1 for c in certs if c.get("verified_ok")),
    }


DEMO_DIR = runner.DEMO_DIR

DEMO_BLURBS = {
    "autocnic-demo-shopfront": "Multi-module order pricing project (package + tests). Best starting point.",
    "autocnic-demo-lending": "Loan underwriting: different domain and graph shape (two outputs, deeper chain).",
    "autocnic-demo-pricing": "Single-file checkout pricing pipeline.",
    "autocnic-demo-global-reads": "Inventory pricing that reads inputs as globals (static inference).",
}


@app.get("/api/demos")
def list_demos():
    demos = []
    if os.path.isdir(DEMO_DIR):
        for name in sorted(os.listdir(DEMO_DIR)):
            path = os.path.join(DEMO_DIR, name)
            if os.path.isfile(os.path.join(path, "autocnic_pipeline.py")):
                demos.append({"name": name, "source": f"demo:{name}", "blurb": DEMO_BLURBS.get(name, "")})
    return {"demos": demos, "allow_local_paths": settings.ALLOW_LOCAL_PATHS,
            "repos_enabled": settings.REPOS_ENABLED}


@app.get("/api/template.zip")
def template_zip():
    """The starter project in ../template, zipped, for users to copy into their repo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(TEMPLATE_DIR):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")]
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.join("autocnic-starter", os.path.relpath(full, TEMPLATE_DIR)))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="autocnic-starter.zip"'})


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": runner.list_sessions()}


@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    session = runner.SESSIONS.get(session_id)
    if session is not None:
        cumulative = _cumulative_from_history(session_id)
        meta = runner.load_meta(session_id) or {}
        return {
            "session_id": session.id,
            "live": True,
            "initial_values": meta.get("initial_values", {}),
            "repo_url": session.repo_url,
            "created_at": session.created_at,
            "graph": session.graph,
            "state": session.state,
            "inputs": {n: {"current_value": session.state["values"].get(n)} for n in session.input_nodes},
            "synthesized_edges": session.synthesized_desc,
            "cumulative": cumulative,
        }
    meta = runner.load_meta(session_id)
    if meta is None:
        raise HTTPException(404, "unknown session_id")
    # Not live (server restarted): serve the persisted record read-only.
    hist = runner.load_history(session_id)
    values = hist[-1]["values"] if hist else meta.get("initial_values", {})
    return {
        "session_id": session_id,
        "live": False,
        "initial_values": meta.get("initial_values", {}),
        "repo_url": meta["repo_url"],
        "created_at": meta["created_at"],
        "graph": meta["graph"],
        "state": {"version": len(hist), "values": values, "digest": hist[-1]["digest"] if hist else None},
        "inputs": {n: {"current_value": values.get(n)} for n in meta.get("input_nodes", [])},
        "synthesized_edges": meta["synthesized_edges"],
        "cumulative": _cumulative_from_history(session_id),
    }


@app.get("/api/session/{session_id}/history")
def get_history(session_id: str):
    return {"session_id": session_id, "updates": runner.load_history(session_id)}


@app.get("/api/certificates/{session_id}")
def get_certificates(session_id: str):
    return {"session_id": session_id, "certificates": runner.load_certificates(session_id)}


@app.get("/api/certificate/{session_id}/{cert_id}")
def get_certificate(session_id: str, cert_id: str):
    for c in runner.load_certificates(session_id):
        if c["cert_id"] == cert_id:
            return c
    raise HTTPException(404, "unknown certificate")


# ---- static frontend ------------------------------------------------------
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _page(name: str):
    return FileResponse(os.path.join(FRONTEND_DIR, name))


@app.get("/")
def index():
    return _page("index.html")


@app.get("/guide")
def guide_page():
    return _page("guide.html")


@app.get("/inputs")
def inputs_page():
    return _page("inputs.html")


@app.get("/track")
def track_page():
    return _page("track.html")


@app.get("/certificates")
def certificates_page():
    return _page("certificates.html")


@app.get("/certificate")
def certificate_page():
    return _page("certificate.html")


if __name__ == "__main__":
    import uvicorn
    # One process only: live sessions are held in memory.
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=False)
