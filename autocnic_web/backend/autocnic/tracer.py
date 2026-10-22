"""
tracer.py
---------
Runs a Pipeline repeatedly under varying inputs, in full-invalidation mode
(every step recomputed every run), and logs every step's (reads, output, elapsed
time) per run. This trace is the only thing graph synthesis has to work with --
it is deliberately just what you'd get from instrumenting a real system in
production for a while, nothing more privileged.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Callable
from .pipeline import Pipeline


@dataclass
class RunTrace:
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]                     # every node's value this run, inputs+derived
    elapsed: Dict[str, float]                    # per-step wall-clock time this run


@dataclass
class TraceLog:
    runs: List[RunTrace] = field(default_factory=list)

    def add(self, run: RunTrace):
        self.runs.append(run)


def collect_traces(pipeline: Pipeline, input_generator: Callable[[int], Dict[str, Any]],
                    n_runs: int) -> TraceLog:
    """input_generator(i) -> a dict of input values for run i. Called n_runs times,
    each run executed under full invalidation (every step runs, every time), which
    is both the training data for synthesis and the efficiency baseline."""
    log = TraceLog()
    for i in range(n_runs):
        inputs = input_generator(i)
        elapsed: Dict[str, float] = {}

        def sink(name, read_values, out, dt, _elapsed=elapsed):
            _elapsed[name] = dt

        outputs = pipeline.run_once(inputs, trace_sink=sink)
        log.add(RunTrace(inputs=dict(inputs), outputs=outputs, elapsed=elapsed))
    return log
