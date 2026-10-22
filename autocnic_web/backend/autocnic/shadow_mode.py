"""
shadow_mode.py
---------------
The empirical soundness check a fully-automated graph-discovery product needs,
since nothing here can prove Assumptions A1/A2 the way a hand-built graph review
could. For a batch of fresh updates (not used during synthesis), this runs BOTH:

  (a) the real pipeline in full-invalidation mode (ground truth), and
  (b) the CNIC engine with the synthesized graph, doing selective recomputation,

on the exact same input sequence, and diffs every certified-retained value against
what full recomputation actually produced. Any mismatch is a synthesis error --
either a missed edge (A1 violated) or a wrong/overconfident predicate (A2
violated) -- surfaced as a concrete counterexample rather than a silent stale
value, which is the whole point of shipping this as a product rather than a
one-off script.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Callable
import random

from .pipeline import Pipeline
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from graph_model import ConditionalDependencyGraph, State  # noqa: E402
from engine import ingest_and_certify, verify_certificate  # noqa: E402


@dataclass
class ShadowReport:
    n_updates: int = 0
    baseline_model_invocations: int = 0
    cnic_model_invocations: int = 0
    certificates_issued: int = 0
    certificates_verified_ok: int = 0
    divergences: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def invocation_reduction_pct(self) -> float:
        if self.baseline_model_invocations == 0:
            return 0.0
        return 100.0 * (1 - self.cnic_model_invocations / self.baseline_model_invocations)


def run_shadow_mode(pipeline: Pipeline, graph: ConditionalDependencyGraph,
                     initial_inputs: Dict[str, Any],
                     input_perturbation: Callable[[Dict[str, Any], random.Random], Dict[str, Any]],
                     n_updates: int = 300, seed: int = 123) -> ShadowReport:
    rng = random.Random(seed)
    report = ShadowReport()

    pipeline.finalize()
    current_inputs = dict(initial_inputs)
    baseline_values = pipeline.run_once(current_inputs)
    report.baseline_model_invocations += len(pipeline.steps) - len(pipeline.inputs)

    state = State(version=0, values=dict(baseline_values))

    for i in range(n_updates):
        new_inputs = input_perturbation(current_inputs, rng)
        changed_inputs = [k for k in new_inputs if new_inputs[k] != current_inputs.get(k)]
        if not changed_inputs:
            continue
        input_node = changed_inputs[0]  # one input updated at a time, as CNIC assumes serialised updates
        current_inputs = new_inputs

        # ---- Ground truth: full recomputation ----
        full_values = pipeline.run_once(current_inputs)
        report.baseline_model_invocations += len(pipeline.steps) - len(pipeline.inputs)

        # ---- CNIC selective recomputation ----
        result = ingest_and_certify(graph, state, input_node, current_inputs[input_node])
        report.cnic_model_invocations += result.model_invocations
        report.n_updates += 1

        for target, cert in result.certificates.items():
            report.certificates_issued += 1
            ok, reason = verify_certificate(cert, graph, state, result.state_post_at_decision)
            if ok:
                report.certificates_verified_ok += 1
            retained_value = result.state_post.values.get(target)
            truth_value = full_values.get(target)
            if retained_value != truth_value:
                report.divergences.append({
                    "update_index": i,
                    "input_node": input_node,
                    "target": target,
                    "certificate_class": cert.cls,
                    "checker_verdict": "ACCEPT" if ok else f"REJECT ({reason})",
                    "retained_value": retained_value,
                    "ground_truth_value": truth_value,
                })

        # Also check every node CNIC recomputed against ground truth, in case of a
        # bug rather than a synthesis gap.
        for node in graph.derived_nodes:
            if result.state_post.values.get(node) != full_values.get(node) and node not in result.certificates:
                report.divergences.append({
                    "update_index": i,
                    "input_node": input_node,
                    "target": node,
                    "certificate_class": "RECOMPUTED",
                    "checker_verdict": "N/A",
                    "retained_value": result.state_post.values.get(node),
                    "ground_truth_value": full_values.get(node),
                })

        state = result.state_post

    return report
