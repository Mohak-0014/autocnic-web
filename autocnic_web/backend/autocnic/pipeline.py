"""
pipeline.py
-----------
The registration surface a codebase uses to tell AutoCNIC what its computation
steps are. This is the honest scope boundary of the v1 product: instead of trying
to understand arbitrary imperative code (unbounded static analysis, hidden global
state, side effects), AutoCNIC targets codebases organised as named computation
steps that declare what they read -- the shape almost every feature pipeline,
pricing engine, ranking/recommendation stack, and ETL job already has, and
exactly the f_R(in-neighbours) shape CNIC's own formal model assumes.

A step can declare its reads explicitly (`reads=[...]`) or, if omitted, AutoCNIC
infers them from the function's parameter names -- any parameter whose name
matches another registered step or a declared input is treated as a read of it.
Explicit declaration is safer and is what a real integration should use; name
inference is a convenience for quick prototyping (and is exactly the kind of
completeness gap the paper's Assumption A1 warns about: get the reads wrong and
downstream certificates verify cleanly and are wrong).

A third option, `infer_reads=True`, closes part of that gap for real code: it
uses `static_analysis.py` (beniget use-def chains over the function's whole
enclosing module) to recover reads that aren't explicit parameters at all --
a step function that reads another step's value as a module-level global or a
closed-over variable, not just an argument. Since a step invoked this way may
read some upstream values as parameters and others as globals, `StepSpec`
tracks the two separately (`param_reads` vs `global_reads`); `run_once`
injects the global reads into the function's `__globals__` immediately before
calling it (and restores whatever was there afterwards), while param reads are
still passed as ordinary keyword arguments.
"""

from __future__ import annotations
import inspect
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, List, Optional


@dataclass
class StepSpec:
    name: str
    fn: Callable
    reads: List[str]
    is_input: bool = False
    infer_reads: bool = False
    param_reads: List[str] = field(default_factory=list)
    global_reads: List[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, name: str):
        self.name = name
        self.steps: Dict[str, StepSpec] = {}
        self.inputs: List[str] = []

    def input(self, name: str):
        """Declare a raw input (no reads, value supplied by the caller each run)."""
        self.steps[name] = StepSpec(name=name, fn=None, reads=[], is_input=True)
        self.inputs.append(name)
        return name

    def step(self, name: Optional[str] = None, reads: Optional[List[str]] = None,
              infer_reads: bool = False):
        """Decorator registering a derived computation step.

        infer_reads=True defers read discovery to finalize(), using
        static_analysis.py's beniget-based use-def resolution over the
        function's whole enclosing module -- this is what lets a step read
        another step's value as a global/closure rather than a parameter."""
        def decorator(fn):
            step_name = name or fn.__name__
            if infer_reads:
                declared_reads: List[str] = []  # filled in finalize()
            elif reads is not None:
                declared_reads = list(reads)
            else:
                # Name-inference fallback: every parameter name is a *candidate* read,
                # pruned in finalize() against the final set of known node names.
                declared_reads = list(inspect.signature(fn).parameters.keys())
            self.steps[step_name] = StepSpec(name=step_name, fn=fn, reads=declared_reads,
                                              infer_reads=infer_reads)
            return fn
        return decorator

    def finalize(self):
        """Resolve any name-inferred reads against the final set of registered
        step/input names, dropping parameter names that don't correspond to a
        known node (e.g. a step reading a literal default, not another step).
        Also runs static (beniget) inference for any step registered with
        infer_reads=True, and splits every derived step's reads into
        param_reads (passed as kwargs) vs global_reads (injected into the
        function's __globals__ before the call)."""
        known = set(self.steps.keys())
        for spec in self.steps.values():
            if spec.is_input:
                continue
            if spec.infer_reads:
                from .static_analysis import infer_reads_for_callable
                # Static inference only reports reads NOT already bound as
                # explicit parameters (those resolve locally, correctly, and
                # aren't "external" in beniget's sense) -- so union its
                # result (global/closure reads) back with the function's own
                # parameter names that happen to match known pipeline nodes
                # (ordinary explicit-argument reads), rather than replacing
                # them.
                param_names_now = set(inspect.signature(spec.fn).parameters.keys())
                inferred_global = infer_reads_for_callable(spec.fn, known_names=known - {spec.name})
                spec.reads = sorted((param_names_now & known) | set(inferred_global))
            spec.reads = [r for r in spec.reads if r in known and r != spec.name]
            param_names = set(inspect.signature(spec.fn).parameters.keys())
            spec.param_reads = [r for r in spec.reads if r in param_names]
            spec.global_reads = [r for r in spec.reads if r not in param_names]

    def topological_order(self) -> List[str]:
        indeg = {n: 0 for n in self.steps}
        for spec in self.steps.values():
            for r in spec.reads:
                indeg[spec.name] += 0  # reads counted via r->spec.name below
        adj: Dict[str, List[str]] = {n: [] for n in self.steps}
        indeg = {n: 0 for n in self.steps}
        for spec in self.steps.values():
            for r in spec.reads:
                adj[r].append(spec.name)
                indeg[spec.name] += 1
        queue = [n for n, d in indeg.items() if d == 0]
        order = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        return order

    def run_once(self, inputs: Dict[str, Any], trace_sink: Optional[Callable] = None) -> Dict[str, Any]:
        """Executes every step exactly once, in topological order (full invalidation).
        If trace_sink is given, calls it with (step_name, read_values, output, elapsed_s)
        for every derived step actually executed."""
        import time
        values: Dict[str, Any] = dict(inputs)
        for name in self.topological_order():
            spec = self.steps[name]
            if spec.is_input:
                continue
            read_values = {r: values.get(r) for r in spec.reads}
            param_values = {r: values.get(r) for r in spec.param_reads}
            t0 = time.perf_counter()
            if spec.global_reads:
                fn_globals = spec.fn.__globals__
                saved = {r: fn_globals.get(r, _MISSING) for r in spec.global_reads}
                try:
                    for r in spec.global_reads:
                        fn_globals[r] = values.get(r)
                    out = spec.fn(**param_values)
                finally:
                    for r, old in saved.items():
                        if old is _MISSING:
                            fn_globals.pop(r, None)
                        else:
                            fn_globals[r] = old
            else:
                out = spec.fn(**param_values)
            elapsed = time.perf_counter() - t0
            values[name] = out
            if trace_sink:
                trace_sink(name, read_values, out, elapsed)
        return values


_MISSING = object()
