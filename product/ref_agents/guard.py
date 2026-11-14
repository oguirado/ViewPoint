#!/usr/bin/env python3
"""ViewPoint reference capsule, unit F4: execution budget for script-lane jobs.

A bpy job runs on Blender's MAIN thread inside the shared session (ref_broker/bridge.py
exec()s it from a timer).  A job that never returns therefore freezes the capsule for
every agent, and nothing outside the process can stop it short of killing Blender.

    instrument(src, budget_s) -> str

returns source with the same behaviour that stops itself: a tick is inserted at the top
of every loop body, function body, lambda and comprehension clause; once the budget is
spent the tick raises a BaseException whose text starts with MARK.  The bridge already
catches BaseException and answers ERROR, so no bridge change is needed: whoever owns
dispatch (the broker) instruments the script text before writing it to RT/inbox.

Scope, stated plainly:
- stops Python-level non-termination (while True, endless generators, huge ranges,
  comprehensions), including loops that swallow `Exception`;
- relies on contract.check_script, which refuses the constructs that could swallow the
  stop (bare except, except BaseException, jumps in finally, dunder methods) and the
  reserved name prefix; instrument() refuses any script the validator refuses;
- does NOT stop one long C call (a single huge operator, sorted(range(10**9)), 10**10**8).
  contract.SCRIPT_NUM_CAPS bounds the common operator arguments; the rest needs the
  broker's wall-clock timeout plus a session restart and a rewind;
- an aborted job has usually written part of its effect: the turn must be closed as
  failed and the scene restored from the last checkpoint, never treated as a no-op.

Stdlib only; ast.unparse needs Python >= 3.9 (host 3.12, Blender 4.0.2 ships 3.10).
Line numbers in a traceback refer to the instrumented text.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contract  # noqa: E402

MARK = "VP_BUDGET_EXCEEDED"
DEFAULT_BUDGET_S = 20.0
MAX_BUDGET_S = 600.0
TICK = contract.RESERVED_PREFIX + "tick__"

_PRELUDE = '''import time as __vp_time__
class __vp_Budget__(BaseException):
    pass
__vp_deadline__ = __vp_time__.monotonic() + %(budget)r
def __vp_tick__(__vp_now__=__vp_time__.monotonic, __vp_end__=__vp_deadline__, __vp_exc__=__vp_Budget__):
    if __vp_now__() > __vp_end__:
        raise __vp_exc__("%(mark)s: job ran longer than %(budget)r s and was stopped")
'''


def _tick_call():
    return ast.Call(func=ast.Name(id=TICK, ctx=ast.Load()), args=[], keywords=[])


def _tick_stmt():
    return ast.Expr(value=_tick_call())


class _Instrument(ast.NodeTransformer):
    def _loop(self, node):
        self.generic_visit(node)
        node.body.insert(0, _tick_stmt())
        return node

    visit_For = visit_AsyncFor = visit_While = _loop

    def _func(self, node):
        self.generic_visit(node)
        node.body.insert(0, _tick_stmt())
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def visit_Lambda(self, node):
        self.generic_visit(node)
        # (tick(), body)[1]
        node.body = ast.Subscript(
            value=ast.Tuple(elts=[_tick_call(), node.body], ctx=ast.Load()),
            slice=ast.Constant(value=1), ctx=ast.Load())
        return node

    def visit_comprehension(self, node):
        self.generic_visit(node)
        # tick() returns None: `if tick() is None` is always true and runs once per item
        node.ifs.insert(0, ast.Compare(left=_tick_call(), ops=[ast.Is()],
                                       comparators=[ast.Constant(value=None)]))
        return node


def instrument(src, budget_s=DEFAULT_BUDGET_S, probe=False):
    """Return `src` with a self-enforced wall-clock budget.  Deterministic: equal inputs
    give byte-equal output (the deadline is computed when the job starts, not here).
    Raises ValueError when contract.check_script refuses the script or the budget is bad."""
    if isinstance(budget_s, bool) or not isinstance(budget_s, (int, float)) \
            or not (0 < budget_s <= MAX_BUDGET_S):
        raise ValueError("budget_s must be a number in (0, %s]" % MAX_BUDGET_S)
    errs = contract.check_script(src, probe=probe)
    if errs:
        raise ValueError("script refused by the validator: " + "; ".join(errs[:5]))
    tree = _Instrument().visit(ast.parse(src))
    ast.fix_missing_locations(tree)
    body = ast.unparse(tree)
    out = _PRELUDE % {"budget": float(budget_s), "mark": MARK} + body + "\n"
    compile(out, "<instrumented>", "exec")       # fail here, not inside Blender
    return out


def prepare_intent(rec, claims=None, budget_s=DEFAULT_BUDGET_S, probe_budget_s=5.0):
    """One call for a broker: validate a decoded intent and return what may be dispatched.

    {"ok": bool, "errors": [...], "warnings": [...],
     "scripts": [instrumented script, instrumented alternative, ...]   # attempt order a1, a2, a3
     "probe": instrumented expect.probe or None}
    Nothing is returned for dispatch unless the whole record is valid."""
    out = {"ok": False, "errors": [], "warnings": [], "scripts": [], "probe": None}
    try:
        out["errors"] = contract.validate_intent(rec, claims)
    except (RecursionError, MemoryError) as e:
        out["errors"] = ["record: too deeply nested to validate (%s)" % type(e).__name__]
    if out["errors"]:
        return out
    if rec["lane"] == "script":
        srcs = [rec["script"]] + list(rec.get("alternatives") or [])
        for s in srcs:
            out["warnings"].extend(contract.determinism_warnings(s))
        out["warnings"].extend("foreign collection named in script: " + f for s in srcs
                               for f in contract.foreign_collections(s, rec["agent_id"],
                                                                     contract.DEFAULT_CLAIMS if claims is None else claims))
        out["scripts"] = [instrument(s, budget_s) for s in srcs]
    if isinstance(rec["expect"], dict):
        out["probe"] = instrument(rec["expect"]["probe"], probe_budget_s, probe=True)
    out["ok"] = True
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="print a bpy job with a self-enforced time budget")
    ap.add_argument("script", help="path of the bpy source, or - for stdin")
    ap.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S)
    a = ap.parse_args(argv)
    src = sys.stdin.read() if a.script == "-" else open(a.script, encoding="utf-8").read()
    try:
        sys.stdout.write(instrument(src, a.budget_s))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
