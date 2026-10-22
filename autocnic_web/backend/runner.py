"""
runner.py
----------
Bridges the AutoCNIC library (vendored in ./autocnic/) to the web app: clones
a GitHub repo, loads its AutoCNIC-compatible pipeline definition, runs
discovery (perturbation experiments + predicate mining) to build a CNIC
dependency graph, and drives live updates through the real Algorithm 1/2
engine -- issuing, verifying, and persisting certificates to disk.

Repo convention (see demo_repos/ for two working examples): a repo usable
here must expose, at its root, a module `autocnic_pipeline.py` defining:

    PIPELINE: autocnic.pipeline.Pipeline   -- built with .input()/.step(...)
    SAMPLERS: Dict[str, Callable[[], Any]] -- one random-value sampler per
                                               input node, used for perturbation
                                               experiments during discovery
    INITIAL_INPUTS: Dict[str, Any]         -- optional; starting input values
                                               shown when the graph first loads
                                               (falls back to one sampler draw
                                               per input if omitted)

This mirrors exactly what examples/pricing_pipeline.py and
examples/global_reads_pipeline.py already do in the base AutoCNIC package --
the repo just needs `from autocnic.pipeline import Pipeline`, which resolves
against this backend's own vendored copy (the cloned repo does not need to
ship or install AutoCNIC itself).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
CLONE_ROOT = os.path.join(BACKEND_DIR, "_clones")
CERT_STORE_DIR = os.path.join(BACKEND_DIR, "cert_store")
os.makedirs(CLONE_ROOT, exist_ok=True)
os.makedirs(CERT_STORE_DIR, exist_ok=True)

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from autocnic.pipeline import Pipeline  # noqa: E402
from autocnic.synthesize import collect_edge_evidence, synthesize_edges, build_cnic_graph  # noqa: E402
from autocnic.graph_model import ConditionalDependencyGraph, State, is_active  # noqa: E402
from autocnic.engine import ingest_and_certify, verify_certificate, Certificate  # noqa: E402


class RepoLoadError(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    repo_url: str
    pipeline: Pipeline
    samplers: Dict[str, Callable[[], Any]]
    graph: ConditionalDependencyGraph
    state: State
    synthesized_desc: List[dict]
    created_at: float = field(default_factory=time.time)
    cumulative_baseline_invocations: int = 0
    cumulative_cnic_invocations: int = 0
    updates_processed: int = 0
    certificates_issued: int = 0
    certificates_verified_ok: int = 0


SESSIONS: Dict[str, Session] = {}


def _clone_repo(repo_url: str, session_id: str) -> str:
    dest = os.path.join(CLONE_ROOT, session_id)
    if os.path.exists(dest):
        raise RepoLoadError(f"clone target already exists: {dest}")
    if os.path.isdir(repo_url):
        # Local project folder: copy it instead of `git clone` (no GitHub push needed).
        import shutil
        shutil.copytree(repo_url, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        return dest
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, dest],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        raise RepoLoadError(f"git clone failed: {e.stderr.strip()[-800:]}")
    except subprocess.TimeoutExpired:
        raise RepoLoadError("git clone timed out after 60s")
    return dest


def _load_pipeline_module(repo_dir: str):
    entry = os.path.join(repo_dir, "autocnic_pipeline.py")
    if not os.path.isfile(entry):
        raise RepoLoadError(
            "repo does not contain autocnic_pipeline.py at its root -- this app only "
            "works with repos that follow the AutoCNIC pipeline convention (see the "
            "bundled demo repos for the exact shape expected)."
        )
    mod_name = f"autocnic_pipeline_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, entry)
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: static_analysis.py's infer_reads_for_callable
    # uses inspect.getmodule(fn) on the step functions, which resolves via sys.modules --
    # without this, beniget-based inference can't recover the function's own source module.
    sys.modules[mod_name] = module
    old_path = list(sys.path)
    sys.path.insert(0, repo_dir)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(mod_name, None)
        raise RepoLoadError(f"error importing autocnic_pipeline.py: {e!r}")
    finally:
        sys.path[:] = old_path

    pipeline = getattr(module, "PIPELINE", None)
    samplers = getattr(module, "SAMPLERS", None)
    if not isinstance(pipeline, Pipeline):
        raise RepoLoadError("autocnic_pipeline.py must define PIPELINE = <autocnic.pipeline.Pipeline instance>")
    if not isinstance(samplers, dict):
        raise RepoLoadError("autocnic_pipeline.py must define SAMPLERS = {input_name: callable}")
    missing = [n for n in pipeline.inputs if n not in samplers]
    if missing:
        raise RepoLoadError(f"SAMPLERS is missing entries for input(s): {missing}")
    initial_inputs = getattr(module, "INITIAL_INPUTS", None)
    if initial_inputs is None:
        initial_inputs = {n: samplers[n]() for n in pipeline.inputs}
    return pipeline, samplers, initial_inputs


def analyze_repo(repo_url: str, n_trials_per_edge: int = 300, seed: int = 0) -> Session:
    """Clones repo_url, loads its pipeline, runs discovery (perturbation
    experiments + predicate mining), and returns a live Session with the
    synthesized CNIC graph and an initial engine State ready for updates."""
    session_id = uuid.uuid4().hex[:12]
    repo_dir = _clone_repo(repo_url, session_id)
    pipeline, samplers, initial_inputs = _load_pipeline_module(repo_dir)
    pipeline.finalize()

    evidence = collect_edge_evidence(pipeline, samplers, n_trials_per_edge=n_trials_per_edge, seed=seed)
    synthesized = synthesize_edges(evidence, confidence_bar=0.90)
    graph = build_cnic_graph(pipeline, synthesized)

    baseline_values = pipeline.run_once(initial_inputs)
    state = State(version=0, values=dict(baseline_values))

    synthesized_desc = [
        {
            "source": e.source, "target": e.target, "conditional": e.conditional,
            "predicate_desc": e.predicate_desc, "confidence": e.confidence, "n_trials": e.n_trials,
        }
        for e in synthesized
    ]

    session = Session(
        id=session_id, repo_url=repo_url, pipeline=pipeline, samplers=samplers,
        graph=graph, state=state, synthesized_desc=synthesized_desc,
    )
    SESSIONS[session_id] = session
    _write_meta(session)
    return session


def graph_to_json(graph: ConditionalDependencyGraph) -> dict:
    nodes = (
        [{"id": n, "kind": "input"} for n in sorted(graph.input_nodes)]
        + [{"id": n, "kind": "derived"} for n in sorted(graph.derived_nodes)]
    )
    edges = [
        {
            "src": e.src, "dst": e.dst, "conditional": e.is_conditional(),
            "predicate_desc": e.predicate_desc,
        }
        for e in graph.edges
    ]
    return {"nodes": nodes, "edges": edges}


def state_to_json(state: State) -> dict:
    return {"version": state.version, "values": state.values, "digest": state.digest()}


def _cert_store_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.jsonl")


def _meta_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.meta.json")


def _write_meta(session: Session):
    with open(_meta_path(session.id), "w") as f:
        json.dump({
            "session_id": session.id,
            "repo_url": session.repo_url,
            "created_at": session.created_at,
            "graph": graph_to_json(session.graph),
            "synthesized_edges": session.synthesized_desc,
            "input_nodes": list(session.pipeline.inputs),
            "initial_values": session.state.values,
        }, f, indent=2, default=str)


def _persist_certificate(session_id: str, record: dict):
    with open(_cert_store_path(session_id), "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_certificates(session_id: str) -> List[dict]:
    path = _cert_store_path(session_id)
    if not os.path.isfile(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _history_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.updates.jsonl")


def load_history(session_id: str) -> List[dict]:
    path = _history_path(session_id)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_meta(session_id: str) -> Optional[dict]:
    path = _meta_path(session_id)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_sessions() -> List[dict]:
    """Every session with metadata on disk (live in memory or not), newest first."""
    out = []
    for name in os.listdir(CERT_STORE_DIR):
        if not name.endswith(".meta.json"):
            continue
        meta = load_meta(name[: -len(".meta.json")])
        if meta is None:
            continue
        sid = meta["session_id"]
        out.append({
            "session_id": sid,
            "repo_url": meta.get("repo_url"),
            "created_at": meta.get("created_at"),
            "n_nodes": len(meta["graph"]["nodes"]),
            "n_edges": len(meta["graph"]["edges"]),
            "n_conditional": sum(1 for e in meta["graph"]["edges"] if e["conditional"]),
            "n_updates": len(load_history(sid)),
            "n_certificates": len(load_certificates(sid)),
            "live": sid in SESSIONS,
        })
    out.sort(key=lambda s: s["created_at"] or 0, reverse=True)
    return out


def apply_update(session: Session, input_node: str, value: Any) -> dict:
    if input_node not in session.pipeline.inputs:
        raise RepoLoadError(f"unknown input node: {input_node}")

    state_pre = session.state
    old_value = state_pre.values.get(input_node)
    result = ingest_and_certify(session.graph, state_pre, input_node, value)

    # Determine per-edge status this update, for UI highlighting.
    A = result.influenced_set
    F_set = set(result.frontier)
    edge_status = []
    for e in session.graph.edges:
        if e.src not in A:
            status = "not_involved"
        elif e.dst in A:
            status = "active"
        elif (e.src, e.dst) in F_set:
            active_now = is_active(e, state_pre, result.state_post_at_decision)
            status = "frontier_active" if active_now else "frontier_blocked"
        else:
            status = "not_involved"
        edge_status.append({"src": e.src, "dst": e.dst, "status": status})

    certs_out = []
    now = time.time()
    for target, cert in result.certificates.items():
        ok, reason = verify_certificate(cert, session.graph, state_pre, result.state_post_at_decision)
        session.certificates_issued += 1
        if ok:
            session.certificates_verified_ok += 1
        record = {
            "cert_id": uuid.uuid4().hex[:10],
            "session_id": session.id,
            "issued_at": now,
            "update_index": session.updates_processed,
            **cert.to_dict(),
            "checker_verdict": "ACCEPT" if ok else f"REJECT ({reason})",
            "verified_ok": ok,
        }
        _persist_certificate(session.id, record)
        certs_out.append(record)

    baseline_this_update = len(session.graph.derived_nodes)
    session.cumulative_baseline_invocations += baseline_this_update
    session.cumulative_cnic_invocations += result.model_invocations
    session.updates_processed += 1
    session.state = result.state_post

    reduction_pct = 0.0
    if session.cumulative_baseline_invocations:
        reduction_pct = 100.0 * (
            1 - session.cumulative_cnic_invocations / session.cumulative_baseline_invocations
        )

    update_index = session.updates_processed - 1
    with open(_history_path(session.id), "a") as f:
        f.write(json.dumps({
            "update_index": update_index,
            "at": now,
            "input_node": input_node,
            "old_value": old_value,
            "new_value": value,
            "influenced_set": sorted(A),
            "recomputed": result.recomputed,
            "frontier": [{"src": s, "dst": d} for (s, d) in result.frontier],
            "edge_status": edge_status,
            "values": session.state.values,
            "digest": session.state.digest(),
            "model_invocations": result.model_invocations,
            "baseline_invocations": baseline_this_update,
            "n_certificates": len(certs_out),
        }, default=str) + "\n")

    return {
        "input_node": input_node,
        "old_value": old_value,
        "update_index": update_index,
        "new_value": value,
        "state": state_to_json(session.state),
        "influenced_set": sorted(A),
        "recomputed": result.recomputed,
        "frontier": [{"src": s, "dst": d} for (s, d) in result.frontier],
        "edge_status": edge_status,
        "certificates_issued": certs_out,
        "model_invocations_this_update": result.model_invocations,
        "baseline_invocations_this_update": baseline_this_update,
        "cumulative": {
            "updates_processed": session.updates_processed,
            "baseline_invocations": session.cumulative_baseline_invocations,
            "cnic_invocations": session.cumulative_cnic_invocations,
            "reduction_pct": round(reduction_pct, 1),
            "certificates_issued": session.certificates_issued,
            "certificates_verified_ok": session.certificates_verified_ok,
        },
    }
