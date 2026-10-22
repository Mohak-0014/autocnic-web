"""
static_analysis.py
--------------------
Uses `beniget` (use-def chain analysis over the `gast` AST) to infer, for a
pipeline step function, which OTHER pipeline nodes it actually reads --
without relying on the parameter-name convention that `pipeline.py` uses by
default.

Why this matters: the original `Pipeline.step()` decorator infers `reads`
from a function's parameter names (or requires them declared explicitly).
That's correct only for step functions that receive every upstream value as
an explicit argument. Real code doesn't always look like that -- a step can
read another step's value as a module-level global, a closed-over variable,
or via a helper function defined elsewhere in the module. This module
recovers the *true* external reads by resolving every name a function's body
loads back to where it was defined, using beniget's use-def chains, and
keeping only the resolutions that land outside the function's own scope and
match a known pipeline node name.

Method:
  1. Parse the WHOLE module's source with `gast` (beniget's DefUseChains must
     see an entire module to resolve scoping correctly -- it is not meant to
     analyse an isolated function fragment).
  2. Run `beniget.DefUseChains().visit(module)` once for the module.
  3. Wrap it in `beniget.UseDefChains(duc)`, which exposes `.chains`: a dict
     mapping each USE node (a `Name` in `Load` context) to the list of `Def`
     objects that define it.
  4. Locate the target function's `FunctionDef` node in the parsed module.
  5. Walk every `Name(Load)` node inside that function's body (skipping
     names that are themselves parameters or local assignments -- those
     resolve to Defs *inside* the function and are not external reads).
  6. For each remaining name, look it up in `ud.chains`; if any resolving
     Def sits outside the function's own scope (i.e. it's a module-level
     global, or defined in a different function -- meaning it must have
     reached this function via a genuine free-variable/global reference)
     and its name matches a known pipeline node, record it as an inferred
     read.

This is deliberately conservative: it only reports reads whose name matches
a *known* pipeline node (an input or another step). Arbitrary external names
(helper functions, imported modules, constants) are ignored, since those
aren't pipeline dependencies.
"""

from __future__ import annotations
import inspect
import textwrap
from typing import Callable, Dict, List, Set

import gast as ast
import beniget


def _function_def_node(module: ast.Module, fn_name: str) -> ast.FunctionDef:
    """Finds the top-level (or nested) FunctionDef matching fn_name."""
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return node
    raise ValueError(f"could not find FunctionDef {fn_name!r} in parsed module")


def _locally_bound_names(fn_node: ast.FunctionDef) -> Set[str]:
    """Names that are parameters or are assigned inside the function body --
    these are NOT external reads even if they happen to share a name with a
    pipeline node, because within this scope they refer to the local
    binding, not the outer one. beniget's own chains already disambiguate
    this correctly (a locally-bound name's Load resolves to the local Def,
    not the outer one) but we also use this as a fast pre-filter / sanity
    check."""
    names: Set[str] = set()
    args = fn_node.args
    for a in list(args.args) + list(getattr(args, "posonlyargs", [])) + list(args.kwonlyargs):
        names.add(a.id)
    if args.vararg:
        names.add(args.vararg.id)
    if args.kwarg:
        names.add(args.kwarg.id)
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, ast.Assign):
            pass  # covered by the Name/Store walk above
    return names


def infer_reads_from_source(module_source: str, fn_name: str, known_names: Set[str]) -> List[str]:
    """Parses `module_source` (the full text of the module the function lives
    in), resolves use-def chains, and returns the sorted list of known
    pipeline node names that `fn_name`'s body reads from outside its own
    scope."""
    module = ast.parse(module_source)
    duc = beniget.DefUseChains()
    duc.visit(module)
    ud = beniget.UseDefChains(duc)

    fn_node = _function_def_node(module, fn_name)
    local_names = _locally_bound_names(fn_node)

    inferred: Set[str] = set()
    for node in ast.walk(fn_node):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        name = node.id
        if name not in known_names:
            continue
        if name in local_names:
            # Bound inside this function (a parameter, or assigned locally
            # before use) -- beniget will correctly resolve this Load to the
            # local Def, so it is not an external read even though the name
            # matches a known pipeline node.
            continue
        defs = ud.chains.get(node)
        if not defs:
            # beniget reports this as an "unbound identifier": no Def
            # anywhere in the module resolves it. That is exactly the shape
            # of a step reading another node's value as an injected runtime
            # global (Pipeline.run_once sets it in fn.__globals__ right
            # before the call, so it is never a static module-level
            # assignment) -- treat it as an inferred external read.
            inferred.add(name)
            continue
        for d in defs:
            def_node = d.node
            # Otherwise, it resolves to a real Def somewhere in the module.
            # Since we already excluded names locally bound in fn_node, any
            # such Def necessarily lies outside fn_node's own scope (e.g. a
            # genuine module-level assignment, or another function's
            # binding reached via closure) -- a real external read.
            inferred.add(name)
    return sorted(inferred)


def infer_reads_for_callable(fn: Callable, known_names: Set[str]) -> List[str]:
    """Convenience wrapper: given an actual Python function object, recovers
    its enclosing module's full source (needed for correct whole-module
    scope resolution) and its own name, then delegates to
    infer_reads_from_source."""
    module = inspect.getmodule(fn)
    if module is None:
        raise ValueError(f"cannot locate source module for {fn!r}")
    module_source = inspect.getsource(module)
    module_source = textwrap.dedent(module_source)
    return infer_reads_from_source(module_source, fn.__name__, known_names)
