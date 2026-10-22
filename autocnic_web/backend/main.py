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
  Pages: / (analyze), /inputs, /track, /certificates, /certificate?s=&id=

Run:  pip install -r requirements.txt && python main.py
Then open http://localhost:8000
"""

from __future__ import annotations

import os
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import runner

app = FastAPI(title="AutoCNIC Web Demo")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


class AnalyzeRequest(BaseModel):
    repo_url: str
    n_trials_per_edge: int = 300
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
    try:
        session = runner.analyze_repo(repo_url, n_trials_per_edge=req.n_trials_per_edge, seed=req.seed)
    except runner.RepoLoadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"unexpected error analyzing repo: {e!r}")

    return {
        "session_id": session.id,
        "repo_url": session.repo_url,
        "graph": runner.graph_to_json(session.graph),
        "state": runner.state_to_json(session.state),
        "inputs": {
            n: {"current_value": session.state.values.get(n)} for n in session.pipeline.inputs
        },
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


DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_repos")

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
                demos.append({"name": name, "path": path, "blurb": DEMO_BLURBS.get(name, "")})
    return {"demos": demos}


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
            "graph": runner.graph_to_json(session.graph),
            "state": runner.state_to_json(session.state),
            "inputs": {n: {"current_value": session.state.values.get(n)} for n in session.pipeline.inputs},
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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
