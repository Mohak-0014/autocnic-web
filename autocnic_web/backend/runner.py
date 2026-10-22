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
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import settings

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "demo_repos")
CLONE_ROOT = os.path.join(settings.DATA_DIR, "_clones")
CERT_STORE_DIR = os.path.join(settings.DATA_DIR, "cert_store")
os.makedirs(CLONE_ROOT, exist_ok=True)
os.makedirs(CERT_STORE_DIR, exist_ok=True)

PIPELINE_FILE = "autocnic_pipeline.py"

# Loading a repo mutates sys.path / sys.modules and the session table, and
# Pipeline.run_once patches step functions' globals -- none of that is
# thread-safe, so analyze and update run one at a time.
LOCK = threading.Lock()

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


GUIDE_HINT = "See the Guide page (/guide) for the expected repo layout and a starter template."


@dataclass
class Source:
    """Where a project comes from, parsed from what the user typed."""
    kind: str                  # "remote" (git clone) or "local" (folder copy)
    location: str              # clone URL or folder path
    ref: Optional[str] = None  # branch/tag from a GitHub /tree/<ref>/ URL
    subdir: str = ""           # folder inside the repo holding autocnic_pipeline.py


_GITHUB_WEB = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?/?$"
)
_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)$")
_REMOTE = re.compile(r"^(https?://|git@[\w.-]+:)[^\s]+$")


def resolve_source(text: str) -> Source:
    """Accepts a GitHub URL (including /tree/<branch>/<folder> links copied from
    the browser), `owner/repo` shorthand, any https/ssh git URL, `demo:<name>`
    for a bundled project, or -- when enabled -- a local folder / file:// URL."""
    text = text.strip()
    if text.startswith("demo:"):
        name = text[len("demo:"):]
        path = os.path.join(DEMO_DIR, name)
        if "/" in name or "\\" in name or not os.path.isdir(path):
            raise RepoLoadError(f"unknown bundled project: {name!r}")
        return Source("local", path)

    is_file_url = text.lower().startswith("file://")
    if is_file_url or os.path.isabs(text) or os.path.isdir(text):
        if not settings.ALLOW_LOCAL_PATHS:
            raise RepoLoadError(
                "local folders and file:// URLs are disabled on this server -- push the "
                "project to GitHub (it can be private only if the server has access) and "
                "paste its URL instead."
            )
        if is_file_url:
            return Source("remote", text)
        if not os.path.isdir(text):
            raise RepoLoadError(f"folder not found: {text}")
        return Source("local", text)

    m = _GITHUB_WEB.match(text)
    if m:
        owner, repo, ref, sub = m.groups()
        return Source("remote", f"https://github.com/{owner}/{repo}.git", ref, (sub or "").strip("/"))
    m = _SHORTHAND.match(text)
    if m:
        return Source("remote", f"https://github.com/{m.group(1)}/{m.group(2)}.git")
    if _REMOTE.match(text):
        return Source("remote", text)
    raise RepoLoadError(
        "could not read that as a repository. Paste a GitHub URL "
        "(https://github.com/owner/repo), owner/repo, or a git clone URL."
    )


def _clone_repo(source: Source, session_id: str) -> str:
    dest = os.path.join(CLONE_ROOT, session_id)
    if os.path.exists(dest):
        raise RepoLoadError(f"clone target already exists: {dest}")
    if source.kind == "local":
        # Local project folder: copy it instead of `git clone` (no GitHub push needed).
        shutil.copytree(source.location, dest, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"))
        return dest
    cmd = ["git", "clone", "--depth", "1"]
    if source.ref:
        cmd += ["--branch", source.ref]
    cmd += ["--", source.location, dest]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # fail fast on private repos instead of hanging
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       timeout=settings.CLONE_TIMEOUT, env=env)
    except subprocess.CalledProcessError as e:
        err = "\n".join(l for l in e.stderr.strip().splitlines() if not l.startswith("Cloning into"))
        hint = ""
        if "could not read Username" in err or "Authentication failed" in err or "not found" in err.lower():
            hint = " -- check the URL, and note that private repos can't be cloned by this server."
        raise RepoLoadError(f"git clone failed: {err[-600:]}{hint}")
    except subprocess.TimeoutExpired:
        _rmtree(dest)
        raise RepoLoadError(f"git clone timed out after {settings.CLONE_TIMEOUT}s")
    return dest


def _rmtree(path: str):
    """rmtree that also removes read-only files (git objects on Windows)."""
    def make_writable_and_retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError:
        pass


_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".tox"}


def _find_pipeline_dir(repo_dir: str, subdir: str) -> str:
    """Folder containing autocnic_pipeline.py: the requested subfolder if one
    was given, else the repo root, else the only match within 3 levels."""
    base = os.path.normpath(os.path.join(repo_dir, subdir))
    if not base.startswith(os.path.normpath(repo_dir)) or not os.path.isdir(base):
        raise RepoLoadError(f"folder {subdir!r} does not exist in the repo")
    if os.path.isfile(os.path.join(base, PIPELINE_FILE)):
        return base
    found = []
    for root, dirs, files in os.walk(base):
        depth = os.path.relpath(root, base).count(os.sep) + (root != base)
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS] if depth < 3 else []
        if PIPELINE_FILE in files:
            found.append(root)
    if len(found) == 1:
        return found[0]
    if found:
        rels = ", ".join(os.path.relpath(f, repo_dir).replace(os.sep, "/") for f in found)
        raise RepoLoadError(
            f"found {PIPELINE_FILE} in several folders ({rels}). Paste the GitHub link to the "
            f"one you want (…/tree/<branch>/<folder>)."
        )
    raise RepoLoadError(f"no {PIPELINE_FILE} found in the repo. {GUIDE_HINT}")


def _forget_project_modules():
    """Drop modules imported from earlier clones, so a repo whose package name
    (say `utils`) matches another project's gets its own code, not a cached copy.
    Sessions already loaded keep working: they hold direct function references."""
    roots = tuple(os.path.normcase(os.path.abspath(p)) + os.sep for p in (CLONE_ROOT, DEMO_DIR))
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f and os.path.normcase(os.path.abspath(f)).startswith(roots):
            del sys.modules[name]


def _load_pipeline_module(repo_dir: str):
    entry = os.path.join(repo_dir, PIPELINE_FILE)
    _forget_project_modules()
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
    except ModuleNotFoundError as e:
        sys.modules.pop(mod_name, None)
        raise RepoLoadError(
            f"autocnic_pipeline.py imports {e.name!r}, which is not available here. The app can "
            f"only use the Python standard library, modules inside your repo, and "
            f"`autocnic.pipeline`. If {e.name!r} is your own package, make sure its folder "
            f"(with an __init__.py) sits next to autocnic_pipeline.py."
        )
    except Exception as e:
        sys.modules.pop(mod_name, None)
        raise RepoLoadError(f"error importing autocnic_pipeline.py: {e!r}")
    finally:
        sys.path[:] = old_path

    pipeline = getattr(module, "PIPELINE", None)
    samplers = getattr(module, "SAMPLERS", None)
    if not isinstance(pipeline, Pipeline):
        raise RepoLoadError(
            "autocnic_pipeline.py must define PIPELINE = Pipeline(...) "
            "(from autocnic.pipeline import Pipeline). " + GUIDE_HINT)
    if not pipeline.inputs:
        raise RepoLoadError("PIPELINE has no inputs -- declare at least one with PIPELINE.input(\"name\").")
    if not any(not s.is_input for s in pipeline.steps.values()):
        raise RepoLoadError("PIPELINE has no steps -- register functions with PIPELINE.step(\"name\")(fn).")
    if not isinstance(samplers, dict):
        raise RepoLoadError("autocnic_pipeline.py must define SAMPLERS = {input_name: lambda: <random value>}. " + GUIDE_HINT)
    missing = [n for n in pipeline.inputs if n not in samplers]
    if missing:
        raise RepoLoadError(f"SAMPLERS is missing entries for input(s): {missing}")
    not_callable = [n for n in pipeline.inputs if not callable(samplers[n])]
    if not_callable:
        raise RepoLoadError(f"SAMPLERS entries must be functions (e.g. lambda: random.randint(1, 9)); not callable: {not_callable}")
    initial_inputs = dict(getattr(module, "INITIAL_INPUTS", None) or {})
    unknown = [n for n in initial_inputs if n not in pipeline.inputs]
    if unknown:
        raise RepoLoadError(f"INITIAL_INPUTS has keys that are not declared inputs: {unknown}")
    for n in pipeline.inputs:
        if n not in initial_inputs:
            initial_inputs[n] = samplers[n]()
    return pipeline, samplers, initial_inputs


def analyze_repo(repo_url: str, n_trials_per_edge: int = 300, seed: int = 0) -> Session:
    """Clones repo_url, loads its pipeline, runs discovery (perturbation
    experiments + predicate mining), and returns a live Session with the
    synthesized CNIC graph and an initial engine State ready for updates."""
    source = resolve_source(repo_url)
    session_id = uuid.uuid4().hex[:12]
    clone_dir = _clone_repo(source, session_id)
    try:
        repo_dir = _find_pipeline_dir(clone_dir, source.subdir)
        pipeline, samplers, initial_inputs = _load_pipeline_module(repo_dir)
        try:
            pipeline.finalize()
            pipeline.topological_order()
            baseline_values = pipeline.run_once(initial_inputs)
        except Exception as e:
            raise RepoLoadError(f"running the pipeline once with INITIAL_INPUTS failed: {e!r}")
        try:
            evidence = collect_edge_evidence(pipeline, samplers, n_trials_per_edge=n_trials_per_edge, seed=seed)
        except Exception as e:
            raise RepoLoadError(
                f"a step raised during perturbation experiments: {e!r}. Steps must accept any "
                f"value the SAMPLERS can produce.")
        synthesized = synthesize_edges(evidence, confidence_bar=0.90)
        graph = build_cnic_graph(pipeline, synthesized)
    except BaseException:
        _rmtree(clone_dir)
        raise
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
    _evict_old_sessions()
    return session


def _evict_old_sessions():
    """Keep memory bounded: past the limit, the oldest live sessions drop to
    read-only (their records stay on disk) and their clones are deleted."""
    while len(SESSIONS) > settings.MAX_LIVE_SESSIONS:
        oldest = min(SESSIONS.values(), key=lambda s: s.created_at)
        del SESSIONS[oldest.id]
        _rmtree(os.path.join(CLONE_ROOT, oldest.id))


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
