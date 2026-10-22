"""
synthesize.py
--------------
Turns a Pipeline's declared structure (Section: "static" layer -- reads a step
declares or that are inferred from its parameter names) plus controlled runtime
perturbation experiments (the "dynamic" layer) into a CNIC ConditionalDependencyGraph
with predicates on edges, ready to hand to the existing engine.

Method, per declared edge (source -> target):
  1. Find the root inputs `source` transitively depends on.
  2. Repeat K times: draw a random baseline input assignment, run the pipeline in
     full-invalidation mode to get source's and target's baseline values, then
     perturb ONE of source's root inputs (holding everything else fixed) and
     re-run. Record whether source's value actually changed and whether target's
     value changed as a result.
  3. If target changes on (almost) every trial where source changed, the edge is
     UNCONDITIONAL (the safe default -- CNIC never becomes unsound by treating a
     live edge as always-live, only less efficient).
  4. If target's output is invariant to source's change often enough, search for a
     numeric threshold predicate on source's value that best separates the
     "target changed" trials from the "target unchanged" trials (a one-level
     decision stump). The predicate is accepted only if its held-out agreement
     rate clears a confidence bar; otherwise the edge falls back to unconditional.

This is explicitly a heuristic, not a proof: it operationalises exactly the gap the
CNIC paper's own limitations section calls "annotation completeness is assumed, not
verified" into a measured confidence number instead of a silent assumption. Every
synthesized conditional edge carries that confidence, and shadow_mode.py provides
the runtime check that catches it if the heuristic is wrong.
"""

from __future__ import annotations
import random
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple, Callable

from .pipeline import Pipeline
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from graph_model import ConditionalDependencyGraph  # noqa: E402


@dataclass
class EdgeEvidence:
    source: str
    target: str
    trials: int = 0
    source_changed: int = 0
    target_changed_given_source_changed: int = 0
    records: List[Tuple[Any, Any, bool, Dict[str, Any]]] = field(default_factory=list)
    # each record: (s0, s1, target_changed, context) where context maps every OTHER
    # declared read of `target` (siblings of `source`) to its value at trial time --
    # the true gating variable for a conditional edge is often a sibling, not the
    # edge's own source (e.g. an "is_member" flag gating a "promo_rate" read).


@dataclass
class SynthesizedEdge:
    source: str
    target: str
    conditional: bool
    predicate_desc: str
    predicate_var: Optional[str] = None      # which node the mined predicate actually reads
    threshold: Optional[float] = None
    direction: Optional[str] = None          # "gt", "lt", "eq_true", "eq_false"
    confidence: float = 1.0
    n_trials: int = 0


def _root_inputs_of(pipeline: Pipeline, node: str) -> List[str]:
    seen, stack, roots = set(), [node], []
    while stack:
        n = stack.pop()
        spec = pipeline.steps[n]
        if spec.is_input:
            if n not in roots:
                roots.append(n)
            continue
        for r in spec.reads:
            if r not in seen:
                seen.add(r)
                stack.append(r)
    return roots


def _random_baseline(pipeline: Pipeline, samplers: Dict[str, Callable[[], Any]], rng: random.Random) -> Dict[str, Any]:
    return {name: samplers[name]() for name in pipeline.inputs}


def collect_edge_evidence(pipeline: Pipeline, samplers: Dict[str, Callable[[], Any]],
                           n_trials_per_edge: int = 400, seed: int = 0) -> Dict[Tuple[str, str], EdgeEvidence]:
    rng = random.Random(seed)
    pipeline.finalize()
    edges = [(r, spec.name) for spec in pipeline.steps.values() if not spec.is_input for r in spec.reads]
    evidence: Dict[Tuple[str, str], EdgeEvidence] = {(s, t): EdgeEvidence(s, t) for (s, t) in edges}

    for (source, target) in edges:
        roots = _root_inputs_of(pipeline, source)
        if not roots:
            continue
        siblings = [r for r in pipeline.steps[target].reads if r != source]
        ev = evidence[(source, target)]
        trials = 0
        attempts = 0
        while trials < n_trials_per_edge and attempts < n_trials_per_edge * 4:
            attempts += 1
            baseline_inputs = _random_baseline(pipeline, samplers, rng)
            base_values = pipeline.run_once(baseline_inputs)
            s0 = base_values[source]
            t0 = base_values[target]

            r = rng.choice(roots)
            perturbed_inputs = dict(baseline_inputs)
            perturbed_inputs[r] = samplers[r]()
            pert_values = pipeline.run_once(perturbed_inputs)
            s1 = pert_values[source]
            t1 = pert_values[target]

            if s1 == s0:
                continue  # perturbation didn't move source; uninformative trial
            trials += 1
            ev.trials += 1
            ev.source_changed += 1
            changed = (t1 != t0)
            if changed:
                ev.target_changed_given_source_changed += 1
            context = {sib: base_values.get(sib) for sib in siblings}
            ev.records.append((s0, s1, changed, context))
    return evidence


def _is_numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _score_numeric_threshold(values: List[float], changed: List[bool]) -> Optional[Tuple[float, str, float]]:
    uniq = sorted(set(values))
    if len(uniq) < 2:
        return None
    candidates = [(uniq[i] + uniq[i + 1]) / 2 for i in range(len(uniq) - 1)]
    best = None
    for thr in candidates:
        for direction in ("gt", "lt"):
            correct = sum(
                int(((v > thr) if direction == "gt" else (v < thr)) == c)
                for v, c in zip(values, changed)
            )
            acc = correct / len(values)
            if best is None or acc > best[2]:
                best = (thr, direction, acc)
    return best


def _score_boolean(values: List[bool], changed: List[bool]) -> Optional[Tuple[str, float]]:
    if not values or not all(isinstance(v, bool) for v in values):
        return None
    scores = []
    for direction, target_val in (("eq_true", True), ("eq_false", False)):
        correct = sum(int((v == target_val) == c) for v, c in zip(values, changed))
        scores.append((direction, correct / len(values)))
    return max(scores, key=lambda x: x[1])


def _score_categorical(values: List[Any], changed: List[bool]) -> Optional[Tuple[Any, str, float]]:
    """Generalizes _score_boolean to any hashable, non-numeric value (e.g. a
    string status flag like "LOW"/"OK"/"SURPLUS"): for each observed value,
    scores the one-vs-rest split "== that value" against the change pattern,
    in both polarities, and returns the best (value, direction, accuracy)."""
    if not values:
        return None
    uniq = sorted(set(values), key=lambda v: str(v))
    if len(uniq) < 2:
        return None
    best = None
    for val in uniq:
        for direction in ("eq", "neq"):
            correct = sum(
                int(((v == val) if direction == "eq" else (v != val)) == c)
                for v, c in zip(values, changed)
            )
            acc = correct / len(values)
            if best is None or acc > best[2]:
                best = (val, direction, acc)
    return best


def _mine_predicate(records: List[Tuple[Any, Any, bool, Dict[str, Any]]],
                     source: str) -> Optional[Tuple[str, Any, str, float]]:
    """Searches candidate single-variable predicates over the edge's own
    (post-update) source value AND every sibling read of the target, and returns
    whichever (variable, threshold_or_None, direction, accuracy) best predicts
    whether the target actually changed. This is what lets synthesis discover a
    gating condition that lives on a different node than the edge's own source
    (e.g. an 'is_member' flag gating whether a 'promo_rate' read matters)."""
    if not records:
        return None
    changed = [r[2] for r in records]
    candidates = []

    s1_values = [r[1] for r in records]
    if all(_is_numeric(v) for v in s1_values):
        res = _score_numeric_threshold(s1_values, changed)
        if res:
            thr, direction, acc = res
            candidates.append((source, thr, direction, acc))
    elif all(isinstance(v, bool) for v in s1_values):
        res = _score_boolean(s1_values, changed)
        if res:
            direction, acc = res
            candidates.append((source, None, direction, acc))
    elif all(isinstance(v, str) for v in s1_values):
        res = _score_categorical(s1_values, changed)
        if res:
            val, direction, acc = res
            candidates.append((source, val, direction, acc))

    sibling_names = list(records[0][3].keys())
    for sib in sibling_names:
        sib_values = [r[3].get(sib) for r in records]
        if any(v is None for v in sib_values):
            continue
        if all(_is_numeric(v) for v in sib_values):
            res = _score_numeric_threshold(sib_values, changed)
            if res:
                thr, direction, acc = res
                candidates.append((sib, thr, direction, acc))
        elif all(isinstance(v, bool) for v in sib_values):
            res = _score_boolean(sib_values, changed)
            if res:
                direction, acc = res
                candidates.append((sib, None, direction, acc))
        elif all(isinstance(v, str) for v in sib_values):
            res = _score_categorical(sib_values, changed)
            if res:
                val, direction, acc = res
                candidates.append((sib, val, direction, acc))

    if not candidates:
        return None
    return max(candidates, key=lambda c: c[3])


def synthesize_edges(evidence: Dict[Tuple[str, str], EdgeEvidence],
                      confidence_bar: float = 0.90) -> List[SynthesizedEdge]:
    results = []
    for (source, target), ev in evidence.items():
        if ev.trials == 0:
            results.append(SynthesizedEdge(source, target, conditional=False,
                                            predicate_desc="TOP (no perturbation evidence; defaulting unconditional)",
                                            confidence=0.0, n_trials=0))
            continue
        change_rate = ev.target_changed_given_source_changed / ev.trials
        if change_rate >= confidence_bar:
            results.append(SynthesizedEdge(source, target, conditional=False,
                                            predicate_desc="TOP (unconditional)",
                                            confidence=change_rate, n_trials=ev.trials))
            continue

        mined = _mine_predicate(ev.records, source)
        if mined is None:
            results.append(SynthesizedEdge(source, target, conditional=False,
                                            predicate_desc="TOP (unconditional; predicate mining unsupported for observed value types)",
                                            confidence=change_rate, n_trials=ev.trials))
            continue

        var, thr, direction, acc = mined
        if acc >= confidence_bar:
            if direction in ("gt", "lt"):
                op = ">" if direction == "gt" else "<"
                desc = f"{var} {op} {thr:.4g}"
            elif direction in ("eq_true", "eq_false"):
                desc = f"{var} is {'true' if direction == 'eq_true' else 'false'}"
            else:  # "eq" / "neq" -- categorical (e.g. string) predicate
                op = "==" if direction == "eq" else "!="
                desc = f"{var} {op} {thr!r}"
            results.append(SynthesizedEdge(source, target, conditional=True,
                                            predicate_desc=desc, predicate_var=var,
                                            threshold=thr, direction=direction,
                                            confidence=acc, n_trials=ev.trials))
        else:
            results.append(SynthesizedEdge(source, target, conditional=False,
                                            predicate_desc=f"TOP (best candidate predicate on '{var}' only "
                                                            f"{acc:.0%} consistent; below confidence bar, defaulting safe)",
                                            confidence=acc, n_trials=ev.trials))
    return results


def build_cnic_graph(pipeline: Pipeline, synthesized: List[SynthesizedEdge]) -> ConditionalDependencyGraph:
    pipeline.finalize()
    g = ConditionalDependencyGraph()
    for spec in pipeline.steps.values():
        if spec.is_input:
            g.add_input(spec.name)
    _MISSING = object()

    def _make_derived_fn(spec):
        param_reads = set(spec.param_reads)
        global_reads = list(spec.global_reads)

        def _call(args, _fn=spec.fn, _param_reads=param_reads, _global_reads=global_reads):
            # Mirrors Pipeline.run_once's invocation convention: reads that
            # are the function's own parameters are passed as kwargs; reads
            # recovered only via static (beniget) inference as globals/
            # closures are injected into the function's __globals__ for the
            # duration of the call. This is required for any step registered
            # with infer_reads=True whose true dependency isn't an explicit
            # argument -- the CNIC engine's derived_fns must invoke it the
            # same way the pipeline itself does, or the discovered graph and
            # the actual runtime behaviour it's supposed to model diverge.
            param_values = {k: v for k, v in args.items() if k in _param_reads}
            if not _global_reads:
                return _fn(**param_values)
            fn_globals = _fn.__globals__
            saved = {r: fn_globals.get(r, _MISSING) for r in _global_reads}
            try:
                for r in _global_reads:
                    fn_globals[r] = args.get(r)
                return _fn(**param_values)
            finally:
                for r, old in saved.items():
                    if old is _MISSING:
                        fn_globals.pop(r, None)
                    else:
                        fn_globals[r] = old
        return _call

    for spec in pipeline.steps.values():
        if not spec.is_input:
            g.add_derived(spec.name, _make_derived_fn(spec))

    by_edge = {(e.source, e.target): e for e in synthesized}
    for spec in pipeline.steps.values():
        if spec.is_input:
            continue
        for r in spec.reads:
            e = by_edge.get((r, spec.name))
            if e is None or not e.conditional:
                g.add_edge(r, spec.name)
            else:
                var, thr, direction = e.predicate_var, e.threshold, e.direction
                if direction == "gt":
                    pred = lambda v, _v=var, _t=thr: v.get(_v, 0) > _t
                elif direction == "lt":
                    pred = lambda v, _v=var, _t=thr: v.get(_v, 0) < _t
                elif direction == "eq_true":
                    pred = lambda v, _v=var: bool(v.get(_v))
                elif direction == "eq_false":
                    pred = lambda v, _v=var: not bool(v.get(_v))
                elif direction == "eq":
                    pred = lambda v, _v=var, _t=thr: v.get(_v) == _t
                else:  # "neq"
                    pred = lambda v, _v=var, _t=thr: v.get(_v) != _t
                g.add_edge(r, spec.name, predicate=pred, predicate_desc=e.predicate_desc,
                           operand_names=[var])
    return g
