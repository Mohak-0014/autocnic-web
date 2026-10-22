"""
graph_model.py
--------------
Implements Definitions 1-5 of the CNIC paper:
  - Conditional dependency graph  G = (V, E, c)
  - State snapshots (S_pre, S_post)
  - The two-state edge activation rule (Eq. 1)
  - Active reachable set A(N) and frontier F  (Definition 4)

This is the data model shared by engine.py (Algorithms 1 & 2).
"""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, Optional, Set, Tuple, List

TOP = "TOP"  # the constant top (unconditional edge), written c(e) = ⊤ in the paper


@dataclass
class Edge:
    src: str
    dst: str
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None  # None == TOP (unconditional)
    predicate_desc: str = "TOP"  # human-readable description, recorded on certificates
    operand_names: Optional[List[str]] = None  # variables the predicate reads, for certificates

    def is_conditional(self) -> bool:
        return self.predicate is not None

    def evaluate(self, state_values: Dict[str, Any]) -> bool:
        """c(e)[S] — evaluate this edge's predicate against a single snapshot's values."""
        if self.predicate is None:
            return True
        return bool(self.predicate(state_values))


@dataclass
class State:
    """A total assignment of values to V, carrying a monotonically increasing version counter
    (Definition 2)."""
    version: int
    values: Dict[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        """A stable digest of this snapshot, used to bind certificates to a specific state
        version (BLAKE2, as specified in Section VIII of the paper)."""
        payload = json.dumps(
            {"version": self.version, "values": self.values}, sort_keys=True, default=str
        ).encode()
        return hashlib.blake2b(payload, digest_size=16).hexdigest()

    def clone(self) -> "State":
        return State(version=self.version, values=dict(self.values))


class ConditionalDependencyGraph:
    """G = (V, E, c) — a directed acyclic graph over input nodes and derived nodes
    (Definition 1)."""

    def __init__(self):
        self.input_nodes: Set[str] = set()
        self.derived_nodes: Set[str] = set()
        self.derived_fns: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self.edges: List[Edge] = []
        self._out_edges: Dict[str, List[Edge]] = {}
        self._in_edges: Dict[str, List[Edge]] = {}

    def add_input(self, name: str):
        self.input_nodes.add(name)
        self._out_edges.setdefault(name, [])
        self._in_edges.setdefault(name, [])

    def add_derived(self, name: str, fn: Callable[[Dict[str, Any]], Any]):
        """fn receives a dict of {in_neighbour_name: value} and returns the computed value."""
        self.derived_nodes.add(name)
        self.derived_fns[name] = fn
        self._out_edges.setdefault(name, [])
        self._in_edges.setdefault(name, [])

    def add_edge(self, src: str, dst: str, predicate=None, predicate_desc: str = "TOP",
                 operand_names: Optional[List[str]] = None):
        e = Edge(src, dst, predicate, predicate_desc, operand_names)
        self.edges.append(e)
        self._out_edges.setdefault(src, []).append(e)
        self._in_edges.setdefault(dst, []).append(e)
        return e

    def out_edges(self, node: str) -> List[Edge]:
        return self._out_edges.get(node, [])

    def in_edges(self, node: str) -> List[Edge]:
        return self._in_edges.get(node, [])

    def all_nodes(self) -> List[str]:
        return list(self.input_nodes) + list(self.derived_nodes)

    def topological_order(self) -> List[str]:
        """Kahn's algorithm restricted to derived nodes, respecting edges among all nodes."""
        indeg = {n: 0 for n in self.all_nodes()}
        for e in self.edges:
            indeg[e.dst] += 1
        queue = [n for n in self.all_nodes() if indeg[n] == 0]
        order = []
        indeg = dict(indeg)
        while queue:
            n = queue.pop(0)
            order.append(n)
            for e in self.out_edges(n):
                indeg[e.dst] -= 1
                if indeg[e.dst] == 0:
                    queue.append(e.dst)
        return [n for n in order if n in self.derived_nodes]

    def digest(self) -> str:
        """digest(G) — a structural digest of the graph, invalidated by any edge/predicate
        change so certificates cannot silently outlive a graph edit."""
        struct = sorted(
            (e.src, e.dst, e.predicate_desc) for e in self.edges
        )
        payload = json.dumps(struct, sort_keys=True).encode()
        return hashlib.blake2b(payload, digest_size=16).hexdigest()

    def has_path(self, src: str, dst: str) -> bool:
        """Ignores predicates entirely — used only to classify VACUOUS vs CONDITIONAL
        non-influence (paper, Section IV-C)."""
        if src == dst:
            return True
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            for e in self.out_edges(u):
                if e.dst == dst:
                    return True
                if e.dst not in seen:
                    seen.add(e.dst)
                    stack.append(e.dst)
        return False


def is_active(edge: Edge, pre: State, post: State) -> bool:
    """Definition 3 / Eq. (1): the two-state activation rule.
    act(e) = true                                   if c(e) = TOP
           = c(e)[S_pre] OR c(e)[S_post]             otherwise
    """
    if not edge.is_conditional():
        return True
    return edge.evaluate(pre.values) or edge.evaluate(post.values)
