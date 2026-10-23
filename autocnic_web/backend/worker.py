"""
worker.py
----------
Runs one project in its own process. The web server (runner.py) starts one
worker per live session and talks to it over stdin/stdout, one JSON object per
line, so a project's code never runs inside the server: if it hangs, crashes,
calls sys.exit() or eats memory, the server kills this process and carries on.

Requests:
    {"op": "analyze", "repo_dir": ..., "n_trials_per_edge": ..., "seed": ...}
    {"op": "update", "input_node": ..., "value": ...}
Replies:
    {"ok": true, "result": {...}}
    {"ok": false, "error": "...", "user_error": true|false}

The worker only computes; the server persists certificates and history.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
import uuid

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from autocnic.pipeline import Pipeline  # noqa: E402
from autocnic.synthesize import collect_edge_evidence, synthesize_edges, build_cnic_graph  # noqa: E402
from autocnic.graph_model import ConditionalDependencyGraph, State, is_active  # noqa: E402
from autocnic.engine import ingest_and_certify, verify_certificate  # noqa: E402

PIPELINE_FILE = "autocnic_pipeline.py"
GUIDE_HINT = "See the Guide page (/guide) for the expected repo layout and a starter template."


class RepoLoadError(RuntimeError):
    pass


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


def _load_pipeline_module(repo_dir: str):
    entry = os.path.join(repo_dir, PIPELINE_FILE)
    mod_name = f"autocnic_pipeline_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, entry)
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: static_analysis.py's infer_reads_for_callable
    # uses inspect.getmodule(fn) on the step functions, which resolves via sys.modules --
    # without this, beniget-based inference can't recover the function's own source module.
    sys.modules[mod_name] = module
    # The project's own packages stay importable for the life of this process.
    sys.path.insert(0, repo_dir)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as e:
        raise RepoLoadError(
            f"autocnic_pipeline.py imports {e.name!r}, which is not available here. The app can "
            f"only use the Python standard library, modules inside your repo, and "
            f"`autocnic.pipeline`. If {e.name!r} is your own package, make sure its folder "
            f"(with an __init__.py) sits next to autocnic_pipeline.py."
        )
    except Exception as e:
        raise RepoLoadError(f"error importing autocnic_pipeline.py: {e!r}")

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


class Project:
    """Everything the worker keeps between requests for its one session."""

    def __init__(self):
        self.pipeline = None
        self.graph = None
        self.state = None
        self.updates_processed = 0

    def analyze(self, repo_dir: str, n_trials_per_edge: int, seed: int) -> dict:
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
        self.graph = build_cnic_graph(pipeline, synthesized)
        self.pipeline = pipeline
        self.state = State(version=0, values=dict(baseline_values))
        return {
            "graph": graph_to_json(self.graph),
            "state": state_to_json(self.state),
            "input_nodes": list(pipeline.inputs),
            "derived_count": len(self.graph.derived_nodes),
            "synthesized_edges": [
                {
                    "source": e.source, "target": e.target, "conditional": e.conditional,
                    "predicate_desc": e.predicate_desc, "confidence": e.confidence, "n_trials": e.n_trials,
                }
                for e in synthesized
            ],
        }

    def update(self, input_node: str, value) -> dict:
        if self.pipeline is None:
            raise RuntimeError("update before analyze")
        if input_node not in self.pipeline.inputs:
            raise RepoLoadError(f"unknown input node: {input_node}")

        state_pre = self.state
        old_value = state_pre.values.get(input_node)
        try:
            result = ingest_and_certify(self.graph, state_pre, input_node, value)
        except Exception as e:
            raise RepoLoadError(f"a step raised while recomputing: {e!r}")

        # Determine per-edge status this update, for UI highlighting.
        A = result.influenced_set
        F_set = set(result.frontier)
        edge_status = []
        for e in self.graph.edges:
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

        certs = []
        for target, cert in result.certificates.items():
            ok, reason = verify_certificate(cert, self.graph, state_pre, result.state_post_at_decision)
            certs.append({
                "cert_id": uuid.uuid4().hex[:10],
                "update_index": self.updates_processed,
                **cert.to_dict(),
                "checker_verdict": "ACCEPT" if ok else f"REJECT ({reason})",
                "verified_ok": ok,
            })

        self.updates_processed += 1
        self.state = result.state_post
        return {
            "input_node": input_node,
            "old_value": old_value,
            "new_value": value,
            "state": state_to_json(self.state),
            "influenced_set": sorted(A),
            "recomputed": result.recomputed,
            "frontier": [{"src": s, "dst": d} for (s, d) in result.frontier],
            "edge_status": edge_status,
            "certificates": certs,
            "model_invocations": result.model_invocations,
        }


def _limit_resources(memory_mb: int, file_mb: int):
    """Hard limits (Linux), set before any project code runs; an unprivileged
    process can't raise them again."""
    try:
        import resource
    except ImportError:  # Windows: local runs only, where the user trusts their own code
        return
    for res, value in ((resource.RLIMIT_AS, memory_mb * 1024 * 1024),
                       (resource.RLIMIT_FSIZE, file_mb * 1024 * 1024),
                       (resource.RLIMIT_NPROC, 256),
                       (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def main():
    if len(sys.argv) >= 3:
        _limit_resources(int(sys.argv[1]), int(sys.argv[2]))

    # Keep the protocol channel to ourselves: whatever the project prints goes
    # to stderr (the server's per-session log) instead of corrupting replies.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    project = Project()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            if req["op"] == "analyze":
                result = project.analyze(req["repo_dir"], req["n_trials_per_edge"], req["seed"])
            elif req["op"] == "update":
                result = project.update(req["input_node"], req["value"])
            else:
                raise RuntimeError(f"unknown op {req['op']!r}")
            reply = {"ok": True, "result": result}
        except RepoLoadError as e:
            reply = {"ok": False, "error": str(e), "user_error": True}
        except Exception as e:
            traceback.print_exc()
            reply = {"ok": False, "error": repr(e), "user_error": False}
        proto.write(json.dumps(reply, default=str) + "\n")
        proto.flush()


if __name__ == "__main__":
    main()
