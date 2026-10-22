"""
engine.py
---------
Implements Algorithm 1 (IngestAndCertify) and Algorithm 2 (VerifyCertificate)
from the CNIC paper, exactly as specified in Section IV-C and IV-D.
"""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Any, List, Set, Tuple, Optional

from graph_model import ConditionalDependencyGraph, State, Edge, is_active


@dataclass
class Certificate:
    """A witness of non-influence for a single (N, R) pair (Section IV-D)."""
    input_node: str
    target: str
    retained_value: Any
    retained_at_version: int
    graph_ver: str
    pre_dig: str
    post_dig: str
    A: Set[str]
    F: List[Tuple[str, str]]
    frontier_operands: Dict[Tuple[str, str], Dict[str, Any]]      # (u,v) -> operand snapshot values
    frontier_outcomes: Dict[Tuple[str, str], Tuple[bool, bool]]    # (u,v) -> (c[pre], c[post])
    cls: str  # "VACUOUS" or "CONDITIONAL"

    def to_dict(self):
        return {
            "input_node": self.input_node,
            "target": self.target,
            "retained_value": self.retained_value,
            "retained_at_version": self.retained_at_version,
            "graph_ver": self.graph_ver,
            "pre_dig": self.pre_dig,
            "post_dig": self.post_dig,
            "A": sorted(self.A),
            "F": sorted(self.F),
            "cls": self.cls,
        }


@dataclass
class IngestResult:
    state_post: State
    certificates: Dict[str, Certificate]      # target -> certificate, for every R not in A
    influenced_set: Set[str]                  # A(N)
    frontier: List[Tuple[str, str]]           # F
    recomputed: List[str]                     # nodes actually recomputed (in A, value differs from before OR forced)
    changed: Set[str]                         # C, the "provably changed" set from the pruning rule
    model_invocations: int                    # count of fR calls actually made (Phase 3), for cost accounting
    state_post_at_decision: State              # S_post per Definition 2: after the write,
                                                # before any derived node is recomputed --
                                                # this, not the fully-committed state, is what
                                                # certificates are bound to and must be verified against.


def ingest_and_certify(
    graph: ConditionalDependencyGraph,
    state_pre: State,
    input_node: str,
    payload: Any,
) -> IngestResult:
    """Algorithm 1: IngestAndCertify.

    Require: input node N with payload p; graph G; state S_pre
    Ensure: updated state S_post; certificate set K
    """
    state_post = state_pre.clone()
    state_post.version += 1
    state_post.values[input_node] = payload  # Preprocess(N, p) is identity here

    # ---- Modality-processor preprocessing (Fig. 1) ----
    # Nodes whose in-neighbours are *only* input nodes represent the "modality processors"
    # of Fig. 1 (feature/confidence extraction: Camera -> CarCount, Microphone -> HornConf).
    # These sit outside the engine's dependency-graph traversal and are always evaluated
    # eagerly into the candidate snapshot, which is what lets a two-hop conditional edge
    # (e.g. HornConf -> TrafficRisk) be evaluated against a genuine post-update HornConf
    # value during Phase 1, rather than a stale one awaiting Phase 3.
    eager_nodes: Set[str] = set()
    eager_recomputed: Set[str] = set()
    for R in graph.derived_nodes:
        parents = graph.in_edges(R)
        if parents and all(e.src in graph.input_nodes for e in parents):
            eager_nodes.add(R)
            # Only re-run the extractor whose input actually changed this update.
            if any(e.src == input_node for e in parents):
                args = {e.src: state_post.values.get(e.src) for e in parents}
                state_post.values[R] = graph.derived_fns[R](args)
                eager_recomputed.add(R)

    # ---- Phase 1: influence analysis (BFS over active edges only) ----
    A: Set[str] = {input_node}
    queue = [input_node]
    while queue:
        u = queue.pop(0)
        for e in graph.out_edges(u):
            if e.dst not in A and is_active(e, state_pre, state_post):
                A.add(e.dst)
                queue.append(e.dst)

    F: List[Tuple[str, str]] = [
        (e.src, e.dst)
        for e in graph.edges
        if e.src in A and e.dst not in A
    ]

    # S_post as Definition 2 defines it: after the write, before any derived node is
    # recomputed. Frozen here because Phase 3 below will go on to mutate state_post's
    # values for nodes inside A -- certificates for nodes outside A must be checked
    # against this frozen snapshot, not the fully-recomputed one.
    state_post_at_decision = state_post.clone()

    # ---- Phase 2: certification — issue a certificate for every derived node outside A ----
    graph_ver = graph.digest()
    K: Dict[str, Certificate] = {}
    for R in graph.derived_nodes:
        if R in A:
            continue
        cls = "VACUOUS" if not graph.has_path(input_node, R) else "CONDITIONAL"
        frontier_operands: Dict[Tuple[str, str], Dict[str, Any]] = {}
        frontier_outcomes: Dict[Tuple[str, str], Tuple[bool, bool]] = {}
        for (u, v) in F:
            edge = next(e for e in graph.out_edges(u) if e.dst == v)
            pre_val = edge.evaluate(state_pre.values)
            post_val = edge.evaluate(state_post_at_decision.values)
            frontier_operands[(u, v)] = {
                "pre": {k: state_pre.values.get(k) for k in _predicate_operands(edge)},
                "post": {k: state_post_at_decision.values.get(k) for k in _predicate_operands(edge)},
            }
            frontier_outcomes[(u, v)] = (pre_val, post_val)
        K[R] = Certificate(
            input_node=input_node,
            target=R,
            retained_value=state_pre.values.get(R),
            retained_at_version=state_pre.version,
            graph_ver=graph_ver,
            pre_dig=state_pre.digest(),
            post_dig=state_post_at_decision.digest(),
            A=set(A),
            F=list(F),
            frontier_operands=frontier_operands,
            frontier_outcomes=frontier_outcomes,
            cls=cls,
        )
        # Retain(R): the value simply is not touched in state_post
        state_post.values[R] = state_pre.values.get(R)

    # ---- Phase 3: selective recomputation (topological order, early cutoff) ----
    C: Set[str] = {input_node}
    for R in eager_recomputed:
        if state_post.values.get(R) != state_pre.values.get(R):
            C.add(R)
    recomputed: List[str] = list(eager_recomputed)
    model_invocations = len(eager_recomputed)
    for R in graph.topological_order():
        if R not in A or R in eager_nodes:
            continue
        in_edges = graph.in_edges(R)
        # "if no in-neighbour u of R has (u, R) active and u in C: continue"  (prune branch)
        any_active_changed_parent = any(
            is_active(e, state_pre, state_post) and e.src in C for e in in_edges
        )
        if in_edges and not any_active_changed_parent:
            continue
        old = state_post.values.get(R)
        args = {e.src: state_post.values.get(e.src) for e in in_edges}
        new_val = graph.derived_fns[R](args)
        model_invocations += 1
        state_post.values[R] = new_val
        recomputed.append(R)
        if new_val != old:
            C.add(R)

    return IngestResult(
        state_post=state_post,
        certificates=K,
        influenced_set=A,
        frontier=F,
        recomputed=recomputed,
        changed=C,
        model_invocations=model_invocations,
        state_post_at_decision=state_post_at_decision,
    )


def _predicate_operands(edge: Edge) -> List[str]:
    """Best-effort extraction of the variable names a predicate closure reads, so the
    certificate can record concrete operand values (paper: 'the operand values that made
    each blocking predicate false'). Falls back to the edge's own endpoints."""
    names = getattr(edge, "operand_names", None)
    if names:
        return names
    return [edge.src]


def verify_certificate(
    cert: Certificate,
    graph: ConditionalDependencyGraph,
    state_pre: State,
    state_post: State,
) -> Tuple[bool, str]:
    """Algorithm 2: VerifyCertificate.

    Returns (accepted, reason). Never repeats the influence analysis and never calls any fR.
    """
    if graph.digest() != cert.graph_ver:
        return False, "graph digest mismatch"
    if state_pre.digest() != cert.pre_dig or state_post.digest() != cert.post_dig:
        return False, "state digest mismatch"
    if cert.input_node not in cert.A or cert.target in cert.A:
        return False, "N not in A, or R in A"

    F_set = set(cert.F)
    for u in cert.A:
        for e in graph.out_edges(u):
            if e.dst not in cert.A:
                if (e.src, e.dst) not in F_set:
                    return False, f"witness incomplete: edge {e.src}->{e.dst} missing from F"
                if is_active(e, state_pre, state_post):
                    return False, f"A not closed: edge {e.src}->{e.dst} is active"
                recorded_pre, recorded_post = cert.frontier_outcomes[(e.src, e.dst)]
                actual_pre = e.evaluate(state_pre.values)
                actual_post = e.evaluate(state_post.values)
                if recorded_pre != actual_pre or recorded_post != actual_post:
                    return False, f"recorded outcomes disagree with snapshots on {e.src}->{e.dst}"

    if cert.cls == "VACUOUS" and graph.has_path(cert.input_node, cert.target):
        return False, "classified VACUOUS but a path exists"

    return True, "ACCEPT"
