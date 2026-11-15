#!/usr/bin/env python3
"""ViewPoint reference capsule, unit F4: shared contract constants and the intent validator.

Stdlib only. The validator checks an intent file (the "enqueue" record of
02_TURN_QUEUE_SPEC.md section 7, as narrowed by the shared reference contract) and
returns a list of error strings; an empty list means the intent is valid.

CLI:
    python3 contract.py --rt RT [--agent modeler-agent] [--json]
exit code 0 = every intent valid, 1 = at least one invalid, 2 = usage / RT problem.
"""
import argparse
import ast
import json
import os
import re
import sys

V = 1
DEFAULT_RT = "/dev/shm/vqa-cu/ref"
AGENT_IDS = ("modeler-agent", "props-agent", "look-agent", "camera-agent")
DEFAULT_CLAIMS = {
    "modeler-agent": ["col:CHAR_mesh"],
    "props-agent": ["col:CHAR_props"],
    "look-agent": ["col:CHAR_look"],
    "camera-agent": ["col:CHAR_cam"],
}
LANES = ("script", "gui")
KINDS = ("job", "key", "xy")
# lane -> kinds allowed on it
LANE_KINDS = {"script": ("job",), "gui": ("key", "xy")}
# GUI acts happen only on the camera agent's turn (04_LANZAMIENTO C2 H1).
GUI_AGENTS = ("camera-agent",)
GUI_KEYS = ("KP_1", "KP_3", "KP_4", "KP_Add", "Home")
MAX_ACTS = 8          # at most 8 acts in one camera-review event (02 section 1)
FIFO_DEPTH = 8        # bounded per-agent FIFO (02 section 2)
MAX_SCRIPT_BYTES = 64 * 1024
MAX_EXPECT_CHARS = 2000
# Modules a script-lane job may import. Everything else is refused: a job runs
# inside the shared Blender process and must not reach the host.
SCRIPT_IMPORT_ALLOW = ("bpy", "bmesh", "mathutils", "math", "random", "colorsys")
SCRIPT_NAME_DENY = ("exec", "eval", "compile", "open", "__import__", "input", "breakpoint",
                    # reflection defeats every attribute rule below
                    "getattr", "setattr", "delattr", "globals", "locals", "vars",
                    # ends the shared session / is swallowed by nobody but the bridge
                    "exit", "quit", "SystemExit", "KeyboardInterrupt", "GeneratorExit",
                    "BaseException", "BaseExceptionGroup",
                    # type(x) hands out the class object: `type(scene).frame_set = None`
                    # poisons every later job and survives open_mainfile (xr-io A1)
                    "type", "super", "object", "classmethod", "staticmethod", "property")
# bpy.ops is an ALLOW list: bpy.ops.<category>.<op>(...) with category in this tuple, and
# bpy.ops may not be stored, aliased or passed (so wm / script / preferences / render /
# export_* / import_* / image / file / text / screen ... are unreachable).
SCRIPT_OPS_ALLOW = ("mesh", "object", "transform", "curve", "surface", "armature",
                    "material", "collection", "node", "uv")
# Attribute names refused on ANY receiver (aliases cannot hide them).  wm: session quit /
# save / open; app: handlers, timers, driver_namespace (code that outlives the turn);
# utils / as_module / libraries: code or data from host files; save*: host file writes.
SCRIPT_ATTR_DENY = ("wm", "app", "handlers", "timers", "driver_namespace",
                    "libraries", "as_module", "save", "save_render", "save_as_mainfile",
                    "preferences", "msgbus", "draw_handler_add", "draw_handler_remove",
                    "register_class", "unregister_class", "f_globals", "f_builtins", "gi_frame", "cr_frame", "tb_frame",
                    # xr-io A1: other roads to a class / RNA struct object
                    "bl_rna", "rna_type", "mro",
                    # xr-io A3: keymaps and drivers outlive the turn (a key bound to
                    # wm.quit_blender kills the shared GUI session; drivers carry Python)
                    "window_manager", "window_managers", "keyconfigs", "keymaps",
                    "keymap_items", "driver_add", "driver_remove", "drivers", "expression",
                    "use_module",
                    # xr-io A2: host file readers / writers inside allowed op categories
                    "export_layout", "multires_external_save", "multires_external_pack",
                    "ocean_bake", "load", "pack", "unpack", "pack_all", "unpack_all",
                    "save_external", "filepath_from_user")
# Keyword arguments refused on any call, and attributes that may not be assigned: they
# point an operator / datablock at a host path (xr-io A2).
SCRIPT_KWARG_DENY = ("filepath", "directory", "files", "filename", "relative_path")
SCRIPT_ATTR_STORE_DENY = ("filepath", "filepath_raw", "directory")
# Names an `except` clause may catch.  Anything else (a local that could hold
# BaseException, xr-io A6) is refused.
SCRIPT_EXCEPT_OK = ("Exception", "ArithmeticError", "AssertionError", "AttributeError",
                    "EOFError", "FloatingPointError", "ImportError", "IndexError", "KeyError",
                    "LookupError", "MemoryError", "ModuleNotFoundError", "NameError",
                    "NotImplementedError", "OSError", "OverflowError", "RecursionError",
                    "ReferenceError", "RuntimeError", "StopIteration", "TypeError",
                    "UnboundLocalError", "UnicodeError", "ValueError", "ZeroDivisionError")
# Builtins that must keep their meaning (isinstance is the only consumer of bpy.types.X).
SCRIPT_NO_REBIND = ("isinstance", "iter") + SCRIPT_EXCEPT_OK
# Dunder names a script may read.
SCRIPT_DUNDER_OK = ("__name__",)
# Names that must keep meaning the module: no rebinding, no aliasing.  A module object is
# interpreter state shared by every later job and NOT part of a .blend checkpoint:
# `bpy.data = None` or `math.sin = None` survives open_mainfile [MEASURED, Blender 4.0.2],
# so no rewind can undo it.
SCRIPT_MODULE_NAMES = SCRIPT_IMPORT_ALLOW + ("json",)
# The only attributes reachable directly on the bpy module (bpy.app, bpy.utils, bpy.path,
# bpy.props, bpy.msgbus ... are refused).  bpy.types is read-only: no store, no call.
BPY_ATTR_ALLOW = ("data", "context", "ops", "types")
BPY_STORE_ROOTS = ("data", "context")
# A read-only probe (rewind contract rw1, expect.probe) may also `import json` to print
# its RW_RESULT line, and may use json.dumps only (json.codecs reaches builtins and sys).
PROBE_IMPORT_EXTRA = ("json",)
JSON_ATTR_ALLOW = ("dumps",)
# Rewind contract rw1 (product/ref_rewind/CONTRACT.md section 5), optional intent fields.
K_MAX = 3
TOUCH_RE = re.compile(r"^(mat:[^\x00-\x1f]{1,63}|render|ui:view3d)\Z")
# Numeric arguments that make ONE C call run for minutes (no Python-level guard can stop it).
SCRIPT_NUM_CAPS = {"levels": 4, "render_levels": 4, "subdivisions": 6, "octree_depth": 8,
                   "segments": 256, "ring_count": 256, "u_segments": 256, "v_segments": 256,
                   "cuts": 64, "number_cuts": 64, "vertices": 1024, "count": 1024,
                   "iterations": 64, "repeat": 64}
MAX_INTENT_BYTES = 256 * 1024
MAX_SCRIPT_NODES = 20000
RESERVED_PREFIX = "__vp_"     # names injected by guard.instrument

REQUIRED = ("v", "rec", "event_id", "agent_id", "lane", "kind",
            "based_on_frame", "write_set", "expect")
# \Z, not $: "$" also matches before a trailing newline.
EVENT_ID_RE = re.compile(r"^(?P<agent>[a-z]+-agent)-(?P<n>[0-9]{1,6})\Z")
WRITE_RE = re.compile(r"^col:[A-Za-z0-9_.]{1,63}\Z")
# Optional fields per (lane, kind); anything else in the record is refused.
OPTIONAL = {("script", "job"): ("script", "alternatives", "touches"),
            ("gui", "key"): ("keys", "touches"), ("gui", "xy"): ("x", "y", "touches")}


def rt_root(rt=None):
    return rt or os.environ.get("VP_SWARM_RT") or DEFAULT_RT


def load_claims_raw(rt):
    """Decoded RT/claims.json, or None when missing / unreadable / not JSON."""
    p = os.path.join(rt, "claims.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError, MemoryError):
        return None


def claims_errors(c):
    """Why a claim table cannot be trusted (empty list = usable).  Claims must be
    {known agent: ["col:<Collection>", ...]} and pairwise disjoint: a table in which two
    agents own one collection makes the claim check meaningless."""
    if not isinstance(c, dict):
        return ["claims: must be a JSON object"]
    errs = []
    seen = {}
    for a in sorted(c, key=str):
        cl = c[a]
        if a not in AGENT_IDS:
            errs.append("claims: unknown agent %r" % (a,))
        if not (isinstance(cl, list) and all(isinstance(w, str) and WRITE_RE.match(w) for w in cl)):
            errs.append("claims: %r must map to a list of \"col:<Collection>\"" % (a,))
            continue
        if len(set(cl)) != len(cl):
            errs.append("claims: duplicate entries for %r" % (a,))
        for w in cl:
            if w in seen and seen[w] != a:
                errs.append("claims: %r is claimed by both %s and %s" % (w, seen[w], a))
            seen[w] = a
    return errs


def load_claims(rt):
    """Claims from RT/claims.json; falls back to the default claim table when the file
    is missing, unreadable or fails claims_errors (fail closed toward the defaults)."""
    c = load_claims_raw(rt)
    if c is not None and not claims_errors(c):
        return c
    return dict(DEFAULT_CLAIMS)


def newest_frame(rt):
    """Largest N with a readable RT/frames/frame_<N>.json, or None."""
    d = os.path.join(rt, "frames")
    best = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        m = re.match(r"^frame_([0-9]+)\.json$", n)
        if m:
            k = int(m.group(1))
            best = k if best is None or k > best else best
    return best


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _const_num(node):
    """Numeric value of a literal (also -N), else None."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def _catches_base(t):
    """True when an except clause type would catch a BaseException (bare, or names one)."""
    if t is None:
        return True
    if isinstance(t, ast.Tuple):
        return any(_catches_base(e) for e in t.elts)
    if isinstance(t, ast.Name):
        return t.id not in SCRIPT_EXCEPT_OK
    return not isinstance(t, ast.Attribute)


def _check_ops(node, parents):
    """bpy.ops only as bpy.ops.<allowed category>.<op>(...); bmesh.ops only as bmesh.ops.<op>(...)."""
    v = node.value
    p1 = parents.get(node)
    p2 = parents.get(p1)
    p3 = parents.get(p2)
    if isinstance(v, ast.Name) and v.id == "bmesh":
        if isinstance(p1, ast.Attribute) and isinstance(p2, ast.Call) and p2.func is p1:
            return None
        return "script: bmesh.ops may only be called as bmesh.ops.<op>(...)"
    if isinstance(v, ast.Name) and v.id == "bpy":
        if (isinstance(p1, ast.Attribute) and isinstance(p2, ast.Attribute)
                and isinstance(p3, ast.Call) and p3.func is p2):
            if p1.attr in SCRIPT_OPS_ALLOW:
                return None
            return "script: bpy.ops.%s.* is not allowed" % p1.attr
        return "script: bpy.ops may only be called as bpy.ops.<category>.<op>(...)"
    return "script: .ops is only reachable as bpy.ops or bmesh.ops (no alias)"


def _check_module_attr(node, parents):
    """Rules for `<module>.<attr>` (node.value is a module Name) and the pure attribute
    chain that starts there."""
    mod = node.value.id
    chain = [node.attr]
    top = node
    while isinstance(parents.get(top), ast.Attribute) and parents[top].value is top:
        top = parents[top]
        chain.append(top.attr)
    dotted = mod + "." + ".".join(chain)
    if mod == "bpy" and node.attr not in BPY_ATTR_ALLOW:
        return "script: bpy.%s is not allowed (only bpy.%s)" % (node.attr, ", bpy.".join(BPY_ATTR_ALLOW))
    if mod == "json" and node.attr not in JSON_ATTR_ALLOW:
        return "script: json.%s is not allowed (only json.dumps)" % node.attr
    if isinstance(top.ctx, (ast.Store, ast.Del)):
        if not (mod == "bpy" and node.attr in BPY_STORE_ROOTS and len(chain) >= 2):
            return ("script: assignment to %s rebinds interpreter state that no checkpoint "
                    "restores" % dotted)
    if mod == "bpy" and node.attr == "types":
        # A class object is interpreter state shared by every later job and NOT part of a
        # .blend checkpoint.  The only use left is the literal `isinstance(x, bpy.types.X)`
        # (or a literal tuple of such): no alias, no store, no call, no argument, no base
        # class, no container (xr-io A1).
        par = parents.get(top)
        if isinstance(par, ast.Call) and par.func is top:
            return "script: calls through bpy.types (%s) are not allowed" % dotted
        ok = len(chain) == 2 and isinstance(top.ctx, ast.Load)
        if ok and isinstance(par, ast.Tuple) and isinstance(par.ctx, ast.Load):
            top, par = par, parents.get(par)
        if not (ok and isinstance(par, ast.Call) and isinstance(par.func, ast.Name)
                and par.func.id == "isinstance" and len(par.args) == 2
                and par.args[1] is top and not par.keywords):
            return ("script: bpy.types may only appear as `isinstance(x, bpy.types.<Name>)` "
                    "(%s: no alias, store, argument, base class or container)" % dotted)
    return None


def check_script(src, probe=False):
    """Static checks on a script-lane job. Returns a list of error strings.
    probe=True: the source is a rewind `expect.probe` (may also import json for json.dumps).

    The check is an allow-list where that is cheap (imports, bpy.ops categories) and a
    receiver-independent deny-list elsewhere: reflection (getattr, dunder and private
    attributes) and aliasing of bpy / bpy.ops are refused, so a denied attribute cannot
    be reached through another name.  It never raises on hostile source."""
    errs = []
    if not isinstance(src, str) or not src.strip():
        return ["script: must be a non-empty string of bpy source"]
    try:
        nbytes = len(src.encode("utf-8"))
    except UnicodeError:
        return ["script: not encodable as UTF-8"]
    if nbytes > MAX_SCRIPT_BYTES:
        return ["script: larger than %d bytes" % MAX_SCRIPT_BYTES]
    if RESERVED_PREFIX in src:
        return ["script: the prefix %r is reserved" % RESERVED_PREFIX]
    try:
        tree = ast.parse(src)
        compile(tree, "<intent-script>", "exec")
    except (SyntaxError, ValueError) as e:
        return ["script: does not compile: %s" % (e,)]
    except (RecursionError, MemoryError, OverflowError) as e:
        return ["script: too deeply nested to check (%s)" % type(e).__name__]
    parents = {}
    nodes = [tree]
    i = 0
    while i < len(nodes):                      # iterative: no recursion on hostile depth
        n = nodes[i]
        i += 1
        for ch in ast.iter_child_nodes(n):
            parents[ch] = n
            nodes.append(ch)
        if len(nodes) > MAX_SCRIPT_NODES:
            return ["script: more than %d syntax nodes" % MAX_SCRIPT_NODES]

    def bound_name(name, what):
        if name is None:
            return
        if name in SCRIPT_MODULE_NAMES or name in SCRIPT_NO_REBIND:
            errs.append("script: %s may not rebind %r" % (what, name))
        elif name.startswith("__") and name not in SCRIPT_DUNDER_OK:
            errs.append("script: %s may not use dunder name %r" % (what, name))

    import_allow = SCRIPT_IMPORT_ALLOW + (PROBE_IMPORT_EXTRA if probe else ())
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in SCRIPT_MODULE_NAMES:
            e = _check_module_attr(node, parents)
            if e:
                errs.append(e)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in import_allow:
                    errs.append("script: import of %r is not allowed" % a.name)
                elif a.asname is not None or "." in a.name:
                    errs.append("script: import %r must be a plain `import <module>`" % a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if node.level or mod not in import_allow:
                errs.append("script: import from %r is not allowed" % (node.module,))
            elif mod in ("bpy", "bmesh", "json"):
                errs.append("script: `from %s import ...` is not allowed, use `import %s`" % (mod, mod))
            else:
                for a in node.names:
                    nm = a.asname or a.name
                    if a.name == "*" or a.name.startswith("_"):
                        errs.append("script: import of %r from %r is not allowed" % (a.name, node.module))
                    else:
                        bound_name(nm, "import")
        elif isinstance(node, ast.Name):
            if node.id in SCRIPT_NAME_DENY:
                errs.append("script: use of %r is not allowed" % node.id)
            elif node.id.startswith("__") and node.id not in SCRIPT_DUNDER_OK:
                errs.append("script: use of dunder name %r is not allowed" % node.id)
            elif node.id in SCRIPT_NO_REBIND and not isinstance(node.ctx, ast.Load):
                errs.append("script: %r may not be rebound" % node.id)
            elif node.id == "iter":
                # iter(callable, sentinel) is an endless C-level loop with no Python tick
                # for the budget stop (xr-io A6); an alias would hide the arity.
                par = parents.get(node)
                if not (isinstance(par, ast.Call) and par.func is node and len(par.args) == 1
                        and not par.keywords and not isinstance(par.args[0], ast.Starred)):
                    errs.append("script: iter may only be called as iter(<one iterable>)")
            elif node.id in SCRIPT_MODULE_NAMES:
                par = parents.get(node)
                if not (isinstance(node.ctx, ast.Load) and isinstance(par, ast.Attribute)
                        and par.value is node):
                    errs.append("script: %r may only be used as `%s.<attr>` (no alias, no rebinding)"
                                % (node.id, node.id))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                errs.append("script: private / dunder attribute %r is not allowed" % node.attr)
            elif node.attr in SCRIPT_ATTR_DENY:
                errs.append("script: attribute %r is not allowed" % node.attr)
            elif node.attr in SCRIPT_ATTR_STORE_DENY and not isinstance(node.ctx, ast.Load):
                errs.append("script: assignment to .%s (a host path) is not allowed" % node.attr)
            elif node.attr == "ops":
                e = _check_ops(node, parents)
                if e:
                    errs.append(e)
            elif node.attr in SCRIPT_NUM_CAPS and isinstance(node.ctx, ast.Store):
                par = parents.get(node)
                val = _const_num(getattr(par, "value", None)) if isinstance(par, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else None
                if val is not None and val > SCRIPT_NUM_CAPS[node.attr]:
                    errs.append("script: %s = %s is over the cap %d" % (node.attr, val, SCRIPT_NUM_CAPS[node.attr]))
        elif isinstance(node, ast.keyword):
            if node.arg in SCRIPT_KWARG_DENY:
                errs.append("script: keyword argument %s= (a host path) is not allowed" % node.arg)
            elif node.arg is None:
                errs.append("script: `**kwargs` in a call is not allowed (it hides keyword names)")
            val = _const_num(node.value)
            if node.arg in SCRIPT_NUM_CAPS and val is not None and val > SCRIPT_NUM_CAPS[node.arg]:
                errs.append("script: %s=%s is over the cap %d" % (node.arg, val, SCRIPT_NUM_CAPS[node.arg]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_name(node.name, "a definition")
        elif isinstance(node, ast.arg):
            bound_name(node.arg, "a parameter")
        elif isinstance(node, ast.ExceptHandler):
            bound_name(node.name, "an except clause")
            # A handler that catches BaseException would swallow the budget stop of
            # guard.instrument (and SystemExit), keeping a never-ending job alive.
            if _catches_base(node.type):
                errs.append("script: bare `except:` / catching BaseException is not allowed "
                            "(use `except Exception`)")
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            for st in node.finalbody:
                for sub in ast.walk(st):
                    if isinstance(sub, (ast.Continue, ast.Break, ast.Return)):
                        errs.append("script: continue / break / return inside `finally` is not allowed")
                        break
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for nm in node.names:
                bound_name(nm, "a global/nonlocal statement")
    return sorted(set(errs), key=errs.index)


def foreign_collections(src, agent_id, claims):
    """Collection names claimed by OTHER agents that appear as string literals in
    the script. Reading a foreign collection is legal, so this is a warning list,
    not an error list; the hard gate is write_set versus claim."""
    own = set(claims.get(agent_id, []))
    foreign = set()
    for other, cl in claims.items():
        if other != agent_id:
            foreign.update(c[4:] for c in cl if c.startswith("col:") and c not in own)
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return []
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for f in foreign:
                if f in node.value:
                    hits.add(f)
    return sorted(hits)


def genesis_script(claims=None):
    """bpy source that creates every claimed collection under the scene collection.

    Broker-owned, run once BEFORE the genesis checkpoint (p0).  Without it the first job
    of each agent links its new collection under the scene collection, which changes the
    digest key `col:Scene Collection` [MEASURED with the R1 digest]; no agent can declare
    that key (it is in nobody's claim and `touches` refuses col:*), so the rw1 judge
    answers DELTA_OUTSIDE and the first turn of every agent is rolled back."""
    claims = DEFAULT_CLAIMS if claims is None else claims
    errs = claims_errors(claims)
    if errs:
        raise ValueError("; ".join(errs))
    names = sorted(w[4:] for cl in claims.values() for w in cl)
    return ("import bpy\n"
            "scene = bpy.data.scenes[0]\n"
            "for name in %r:\n"
            "    col = bpy.data.collections.get(name) or bpy.data.collections.new(name)\n"
            "    if scene.collection.children.get(name) is None:\n"
            "        scene.collection.children.link(col)\n") % (names,)


def determinism_warnings(src):
    """Warnings (not errors): things that make a replay of the same script give another
    scene, so a rewind by re-execution would not reproduce the digest."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    uses_random = seeded = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "random":
            if node.attr == "seed":
                seeded = True
            else:
                uses_random = True
        elif isinstance(node, ast.ImportFrom) and node.module == "random":
            uses_random = True
    out = []
    if uses_random and not seeded:
        out.append("determinism: `random` used without random.seed(<constant>)")
    return out


def validate_intent(obj, claims=None, agent_id=None, event_id=None, newest=None):
    """Validate one decoded intent.

    agent_id / event_id: what the file PATH says (RT/intents/<agent_id>/<event_id>.json);
    when given they must match the record. newest: newest published frame number, when known.
    Returns a list of error strings (empty = valid).
    """
    claims = DEFAULT_CLAIMS if claims is None else claims
    errs = []
    if not isinstance(obj, dict):
        return ["record: must be a JSON object"]
    for k in REQUIRED:
        if k not in obj:
            errs.append("%s: missing required field" % k)
    if errs:
        return errs

    if not (_is_int(obj["v"]) and obj["v"] == V):
        errs.append("v: must be %d" % V)
    if obj["rec"] != "enqueue":
        errs.append("rec: must be \"enqueue\"")

    aid = obj["agent_id"]
    if aid not in AGENT_IDS:
        errs.append("agent_id: %r is not one of %s" % (aid, ", ".join(AGENT_IDS)))
    if agent_id is not None and aid != agent_id:
        errs.append("agent_id: %r does not match intents directory %r" % (aid, agent_id))

    eid = obj["event_id"]
    m = EVENT_ID_RE.match(eid) if isinstance(eid, str) else None
    if not m:
        errs.append("event_id: must look like <agent_id>-<n>")
    elif m.group("agent") != aid:
        errs.append("event_id: prefix %r is not the record's agent_id" % m.group("agent"))
    if event_id is not None and eid != event_id:
        errs.append("event_id: %r does not match file name %r" % (eid, event_id))

    lane, kind = obj["lane"], obj["kind"]
    lane = lane if isinstance(lane, str) else None
    kind = kind if isinstance(kind, str) else None
    allowed = set(REQUIRED) | set(OPTIONAL.get((lane, kind), ("script", "keys", "x", "y")))
    for k in sorted(set(obj) - allowed, key=str):
        errs.append("%s: unknown field for lane %r kind %r" % (k, lane, kind))
    if lane not in LANES:
        errs.append("lane: must be one of %s" % (LANES,))
    if kind not in KINDS:
        errs.append("kind: must be one of %s" % (KINDS,))
    if lane in LANES and kind in KINDS and kind not in LANE_KINDS[lane]:
        errs.append("kind: %r is not valid on lane %r" % (kind, lane))

    bof = obj["based_on_frame"]
    if not (_is_int(bof) and bof >= 0):
        errs.append("based_on_frame: must be an integer >= 0")
    elif newest is not None and bof > newest:
        errs.append("based_on_frame: %d is newer than the newest published frame %d" % (bof, newest))

    exp = obj["expect"]
    if isinstance(exp, dict):        # rewind contract rw1: {"probe": <read-only bpy source>, "equals": <json>}
        if set(exp) != {"probe", "equals"}:
            errs.append("expect: an object must have exactly the keys probe and equals")
        else:
            errs.extend("expect.probe: " + e for e in check_script(exp["probe"], probe=True))
    elif not (isinstance(exp, str) and exp.strip() and len(exp) <= MAX_EXPECT_CHARS):
        errs.append("expect: must be non-empty text (<= %d chars) or {probe, equals}" % MAX_EXPECT_CHARS)

    if "touches" in obj:
        t = obj["touches"]
        if not (isinstance(t, list) and all(isinstance(k, str) and TOUCH_RE.match(k) for k in t)
                and len(set(t)) == len(t)):
            errs.append("touches: must be a duplicate-free list of mat:<name>, render or ui:view3d "
                        "(col:* belongs in write_set, where the claim is checked)")
    if "alternatives" in obj and lane == "script":
        alts = obj["alternatives"]
        if not isinstance(alts, list) or 1 + len(alts) > K_MAX:
            errs.append("alternatives: must be a list, script + alternatives <= %d" % K_MAX)
        else:
            for i, a in enumerate(alts):     # every alternative is a job: same checks as `script`
                errs.extend("alternatives[%d]: %s" % (i, e) for e in check_script(a))

    # write_set: syntactically valid and inside the agent's claim.  CLAIM CHECK.
    ws = obj["write_set"]
    claim = set(claims.get(aid, [])) if isinstance(aid, str) else set()
    if not (isinstance(ws, list) and all(isinstance(w, str) for w in ws)):
        errs.append("write_set: must be a list of \"col:<Collection>\" strings")
    else:
        if len(set(ws)) != len(ws):
            errs.append("write_set: duplicate entries")
        for w in ws:
            if not WRITE_RE.match(w):
                errs.append("write_set: %r is not \"col:<Collection>\"" % w)
            elif w not in claim:
                errs.append("write_set: %r is outside the claim of %s (CLAIM_REFUSED)" % (w, aid))
        if lane == "script" and not ws:
            errs.append("write_set: a script-lane job must name the collection it writes")

    if lane == "script":
        if "keys" in obj:
            errs.append("keys: not allowed on lane script")
        errs.extend(check_script(obj.get("script")))
    elif lane == "gui":
        if "script" in obj:
            errs.append("script: not allowed on lane gui")
        if aid not in GUI_AGENTS:
            errs.append("lane: gui acts are reserved to %s" % ", ".join(GUI_AGENTS))
        keys = obj.get("keys")
        if kind == "key":
            if not (isinstance(keys, list) and keys):
                errs.append("keys: lane gui kind key needs a non-empty list of xdotool keys")
            else:
                if len(keys) > MAX_ACTS:
                    errs.append("keys: more than %d acts in one event" % MAX_ACTS)
                for k in keys:
                    if k not in GUI_KEYS:
                        errs.append("keys: %r is not one of %s" % (k, ", ".join(GUI_KEYS)))
        elif kind == "xy":
            if not (_is_int(obj.get("x")) and _is_int(obj.get("y"))
                    and obj["x"] >= 0 and obj["y"] >= 0):
                errs.append("x/y: lane gui kind xy needs integer x and y >= 0")
            if keys is not None:
                errs.append("keys: not allowed with kind xy")
    return errs


def _no_dup_pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key %r" % (k,))
        d[k] = v
    return d


def _no_constant(name):
    raise ValueError("non-finite number %s" % name)


def load_intent_file(path):
    """(obj, None) or (None, error).  Refuses symlinks / non-regular files, files over
    MAX_INTENT_BYTES, non-UTF-8 bytes, duplicate keys and NaN / Infinity; never raises."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return None, "file: not readable: %s" % (e,)
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return None, "file: must be a regular file (no symlink, fifo or directory)"
    if st.st_size > MAX_INTENT_BYTES:
        return None, "file: larger than %d bytes" % MAX_INTENT_BYTES
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            raw = os.read(fd, MAX_INTENT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as e:
        return None, "file: not readable: %s" % (e,)
    if len(raw) > MAX_INTENT_BYTES:
        return None, "file: larger than %d bytes" % MAX_INTENT_BYTES
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs,
                         parse_constant=_no_constant)
    except (ValueError, RecursionError, MemoryError) as e:   # UnicodeDecodeError is a ValueError
        return None, "file: not readable JSON: %s" % (str(e)[:200] or type(e).__name__,)
    return obj, None


def validate_file(path, claims=None, newest=None):
    """Validate one intent file by path; the path supplies agent_id and event_id."""
    agent_id = os.path.basename(os.path.dirname(path))
    name = os.path.basename(path)
    if not name.endswith(".json"):
        return ["file: name must end in .json"]
    obj, err = load_intent_file(path)
    if err:
        return [err]
    try:
        return validate_intent(obj, claims, agent_id=agent_id, event_id=name[:-5], newest=newest)
    except (RecursionError, MemoryError) as e:
        return ["file: record too deeply nested to validate (%s)" % type(e).__name__]


def intent_files(rt, agent_id=None):
    """Sorted intent files under RT/intents (optionally one agent)."""
    base = os.path.join(rt, "intents")
    out = []
    try:
        agents = sorted(os.listdir(base))
    except OSError:
        return out
    for a in agents:
        if agent_id and a != agent_id:
            continue
        d = os.path.join(base, a)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if n.endswith(".json"):
                out.append(os.path.join(d, n))
    return out


def validate_rt(rt, agent_id=None):
    """Validate every intent file in RT. Returns {path: [errors]} (all files listed)."""
    claims = load_claims(rt)
    newest = newest_frame(rt)
    res = {}
    for p in intent_files(rt, agent_id):
        errs = validate_file(p, claims, newest)
        owner = os.path.basename(os.path.dirname(p))
        if owner not in AGENT_IDS:
            errs.append("file: intents directory %r is not a known agent" % owner)
        res[p] = errs
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="validate intent files against the shared contract")
    ap.add_argument("--rt", default=None, help="runtime root (default $VP_SWARM_RT or %s)" % DEFAULT_RT)
    ap.add_argument("--agent", default=None, choices=AGENT_IDS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rt = rt_root(a.rt)
    if not os.path.isdir(rt):
        print("RT not found: %s" % rt, file=sys.stderr)
        return 2
    res = validate_rt(rt, a.agent)
    bad = {p: e for p, e in res.items() if e}
    if a.json:
        print(json.dumps({"rt": rt, "files": len(res), "invalid": len(bad), "errors": bad},
                         indent=1, sort_keys=True))
    else:
        for p in sorted(res):
            print("%s %s" % ("INVALID" if res[p] else "VALID  ", p))
            for e in res[p]:
                print("    - " + e)
        print("%d intent file(s), %d invalid" % (len(res), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
