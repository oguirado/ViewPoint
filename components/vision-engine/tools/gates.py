#!/usr/bin/env python3
"""vision-engine gate runner (stdlib only, fail-closed, pure: no clock, no randomness).

Usage: gates.py --all | --gate <id> | --mutants  [--root DIR] [--json]
stdout: one canonical JSON object {"code","rc","failed","receipt",...}. rc 0 OK, 2 usage, 5 gate failed.
Every gate is evaluated on every --all call (bitmask semantics, no short-circuit); an exception inside a gate is a failure.
"""
import ast
import importlib.util
import io
import json
import re
import sys
import tokenize
from pathlib import Path

LAYER = {
    "ids": 0, "params": 0, "layout": 0, "errors": 0, "canon": 0, "frame": 0,
    "decode": 1, "grid": 1, "desc": 1,
    "change": 2, "pyramid": 2, "regions": 2,
    "gop": 3, "pack": 3, "cite": 3,
    "pipeline": 4, "cli": 4, "__init__": 4, "schemas": 0, "parallel": 4, "topology": 0,
}
STAGE_ALIAS = {"p": "params", "s0": "grid", "s1": "desc", "s2": "change", "s3": "pyramid",
               "s4": "regions", "s5": "gop", "s6": "pack"}
VADAPT_OK = {"frame", "ids", "errors", "canon", "params", "decode"}  # amendment A-1 (orchestrator 2026-10-17): one canonical PNG decoder
VMEM_OK = {"ids", "layout", "errors", "canon", "schemas"}
FORBID_VC = {"vmem", "vadapt", "jsonschema", "bpy", "random"}
SRC_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative")
MAX_LINES = 800
TIER1 = (("llms.txt", 600), ("AGENTS.md", 6000))
ANCHOR_RE = re.compile(r"^# @anchor:(vc|vm|va|tools)\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
INLINE_ANCHOR_RE = re.compile(r"(?:^|\s)# @anchor:(\S+)\s*$")
SCHEMA_ID_RE = re.compile(r"^(vqa|vmem)\.[a-z0-9-]+/v[1-9][0-9]*$")
TID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")
IMPURE_MODS = {"time", "random", "datetime", "secrets", "uuid"}
ENGINE_NAMES = {"adapter_id", "bpy", "blender", "opensuperficies"}
ID_TEMPLATES = re.compile(r"^([frpk]\{\}:(t\{\}|all|\{\})|p\{\}:L\{\}:\{\}|g\{\})$")
ID_PCT = re.compile(r"^([frpk]%[0-9]*[dsx]:|g%[0-9]*[dsx]$|r_%0?6d|V-%s-%0?6d|T:%s)")
ID_FMT_TPL = re.compile(r"^([frpkg]\{[^}]*\}:(t\{[^}]*\}|all|\{[^}]*\})|p\{[^}]*\}:L\{[^}]*\}:\{[^}]*\}|g\{[^}]*\}|r_\{[^}]*\}_\{[^}]*\}|V-\{[^}]*\}-\{[^}]*\}|T:\{[^}]*\})")
PATH_EXTS = (".md", ".py", ".json", ".jsonl", ".tsv", ".txt", ".toml")
PATH_TOPS = ("tools/", "catalogue/", "visioncore/", "tests/", "vmem/", "vadapt/", "hyperbooks/")
EXEMPT_ANCHOR = "vc.s1.isqrt"


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def pyfiles(root, pkg):
    base = root / pkg
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def rel(root, p):
    return p.relative_to(root).as_posix()


def parse(p):
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def stem_of(root, p):
    return p.relative_to(root / "visioncore").with_suffix("").parts[0]


def imports_of(tree, level0_pkg="visioncore"):
    """yield (top, sub|None, lineno) for each import."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                parts = a.name.split(".")
                yield parts[0], (parts[1] if len(parts) > 1 else None), n.lineno
        elif isinstance(n, ast.ImportFrom):
            if n.level > 0:
                if n.module:
                    yield level0_pkg, n.module.split(".")[0], n.lineno
                else:
                    for a in n.names:
                        yield level0_pkg, a.name, n.lineno
            else:
                parts = (n.module or "").split(".")
                if len(parts) > 1:
                    yield parts[0], parts[1], n.lineno
                elif parts[0] == "visioncore":
                    for a in n.names:
                        yield "visioncore", a.name, n.lineno
                else:
                    yield parts[0], None, n.lineno


def is_stdlib(top):
    return top in sys.stdlib_module_names


# ---------------------------------------------------------------- gates
def g_tier1(root):
    out = []
    for name, cap in TIER1:
        p = root / name
        if not p.is_file():
            out.append(("E_GATE_TIER1", f"{name} missing"))
            continue
        data = p.read_bytes()
        if len(data) > cap:
            out.append(("E_GATE_TIER1", f"{name} {len(data)} B > {cap}"))
        text = data.decode("utf-8", "replace")
        cands = set(re.findall(r"\]\(([^)#\s]+)\)", text))
        for tok in re.findall(r"`([A-Za-z0-9_./-]+)`", text):
            if tok.startswith(PATH_TOPS) and tok.endswith(PATH_EXTS):
                cands.add(tok)
        for c in sorted(cands):
            if c.startswith(("http:", "https:", "mailto:")):
                continue
            if not (root / c).exists():
                out.append(("E_GATE_TIER1", f"{name} links missing path {c}"))
    return out


def anchors_in(root):
    found = {}
    out = []
    for pkg in SRC_PKGS:
        for p in pyfiles(root, pkg):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                m = INLINE_ANCHOR_RE.search(line)
                if m:
                    s = "# @anchor:" + m.group(1)
                    if not ANCHOR_RE.match(s):
                        out.append(("E_GATE_ANCHOR", f"{rel(root, p)}:{i} bad anchor grammar"))
                        continue
                    a = m.group(1)
                    if a in found:
                        out.append(("E_GATE_ANCHOR", f"duplicate anchor {a} ({found[a]} and {rel(root, p)}:{i})"))
                    else:
                        found[a] = f"{rel(root, p)}:{i}"
    return found, out


def g_anchor(root):  # @anchor:tools.gates.g_anchor
    _, out = anchors_in(root)
    sym = root / "catalogue" / "symbols.jsonl"
    if sym.is_file():
        for n, line in enumerate(sym.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            p = root / row["path"]
            lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
            ln = row["line"]
            want = "@anchor:" + row["anchor"]
            if not (1 <= ln <= len(lines)) or want not in lines[ln - 1]:
                out.append(("E_DRIFT", f"symbols.jsonl:{n} anchor {row['anchor']} not at {row['path']}:{ln}"))
            elif row.get("symbol") and not any(row["symbol"] in l for l in lines[ln - 1:ln + 3]):
                out.append(("E_GATE_ANCHOR", f"symbols.jsonl:{n} symbol {row['symbol']} not near anchor"))
    found, _ = anchors_in(root)
    pending = set()
    lj = root / "catalogue" / "layers.json"
    if lj.is_file():
        try:
            pending = set(json.loads(lj.read_text(encoding="utf-8")).get("pending_anchors", []))
        except ValueError:
            out.append(("E_GATE_ANCHOR", "layers.json invalid JSON"))
    rt = root / "catalogue" / "routes.tsv"
    if rt.is_file():
        for n, line in enumerate(rt.read_text(encoding="utf-8").splitlines(), 1):
            cols = line.split("\t")
            if len(cols) >= 2 and cols[1] not in found and cols[1] not in pending:
                out.append(("E_GATE_ANCHOR", f"routes.tsv:{n} target {cols[1]} has no code anchor (and is not in layers.json pending_anchors)"))
    for a in sorted(pending & set(found)):
        out.append(("E_GATE_ANCHOR", f"layers.json pending_anchors lists {a} but it exists in code: remove it"))
    return out


def header_of(text):
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("# @module "):
            buf = l[len("# @module "):]
            for m in lines[i + 1:]:
                if m.startswith("#   "):
                    buf += m[4:]
                else:
                    break
            return buf
    return None


def g_header(root):
    out = []
    for p in pyfiles(root, "visioncore"):
        text = p.read_text(encoding="utf-8")
        raw = header_of(text)
        if raw is None:
            continue
        r = rel(root, p)
        try:
            h = json.loads(raw)
        except ValueError:
            out.append(("E_GATE_HEADER", f"{r} header is not JSON"))
            continue
        st = stem_of(root, p)
        if h.get("layer") != LAYER.get(st):
            out.append(("E_GATE_HEADER", f"{r} layer {h.get('layer')} != {LAYER.get(st)}"))
        if not isinstance(h.get("deps"), list):
            out.append(("E_GATE_HEADER", f"{r} deps missing"))
            continue
        want = set()
        for top, sub, _ in imports_of(parse(p)):
            if top == "visioncore" and sub in LAYER and sub != st and sub != "schemas":
                want.add(sub)
        have = set()
        for d in h["deps"]:
            last = d.split(".")[-1] if d.startswith("vc.") else d
            have.add(STAGE_ALIAS.get(last, last))
        if "pack" in have and "cite" in want and "pack" not in want:
            have.add("cite")
        if "cite" in have and "pack" in want and "cite" not in want:
            have.add("pack")
        if want != have:
            out.append(("E_GATE_HEADER", f"{r} deps {sorted(have)} != imports {sorted(want)}"))
    return out


def g_layer(root):  # @anchor:tools.gates.g_layer
    out = []
    for p in pyfiles(root, "visioncore"):
        st = stem_of(root, p)
        if st not in LAYER:
            out.append(("E_GATE_LAYER_PKG", f"{rel(root, p)} unknown module {st}"))
            continue
        me = LAYER[st]
        tree = parse(p)
        seen = set()
        for top, sub, ln in imports_of(tree):
            key = (top, sub)
            if key in seen:
                continue
            seen.add(key)
            r = f"{rel(root, p)}:{ln}"
            if top == "visioncore":
                if sub in LAYER and LAYER[sub] > me:
                    out.append(("E_GATE_LAYER_UP", f"{r} imports visioncore.{sub} (L{LAYER[sub]} > L{me})"))
            elif top in FORBID_VC or top.startswith("godot"):
                out.append(("E_GATE_LAYER_PKG", f"{r} forbidden import {top}"))
            elif top == "numpy":
                if me == 0:
                    out.append(("E_GATE_LAYER_PKG", f"{r} numpy in L0"))
            elif not is_stdlib(top):
                out.append(("E_GATE_LAYER_PKG", f"{r} third-party import {top}"))
        if p.name != "cli.py":
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and n.attr == "time" and isinstance(n.value, ast.Name) and n.value.id == "time":
                    out.append(("E_GATE_LAYER_PKG", f"{rel(root, p)}:{n.lineno} time.time outside cli.py"))
    for p in pyfiles(root, "vadapt"):
        for top, sub, ln in imports_of(parse(p), "vadapt"):
            r = f"{rel(root, p)}:{ln}"
            if top == "visioncore" and sub not in VADAPT_OK:
                out.append(("E_GATE_LAYER_PKG", f"{r} vadapt imports visioncore.{sub}"))
            elif top == "vmem":
                out.append(("E_GATE_LAYER_PKG", f"{r} vadapt imports vmem"))
            elif p.name == "ramws.py" and (top == "visioncore" or not is_stdlib(top)):
                out.append(("E_GATE_LAYER_PKG", f"{r} ramws imports {top} (stdlib only)"))
    for p in pyfiles(root, "vmem"):
        for top, sub, ln in imports_of(parse(p), "vmem"):
            r = f"{rel(root, p)}:{ln}"
            if top == "visioncore" and sub not in VMEM_OK:
                out.append(("E_GATE_LAYER_PKG", f"{r} vmem imports visioncore.{sub}"))
            elif top in ("vadapt", "numpy"):
                out.append(("E_GATE_LAYER_PKG", f"{r} vmem imports {top}"))
            elif top not in ("vmem", "visioncore") and not is_stdlib(top):
                out.append(("E_GATE_LAYER_PKG", f"{r} vmem third-party import {top}"))
    for p in pyfiles(root, "tools"):
        for top, sub, ln in imports_of(parse(p), "tools"):
            if top not in ("tools",) and not is_stdlib(top):
                out.append(("E_GATE_LAYER_PKG", f"{rel(root, p)}:{ln} tools imports non-stdlib {top}"))
    return out + _vnative_layer(root)


def import_vc(root, name):
    """import visioncore.<name> from `root` (package context, so relative imports work), then forget it."""
    old = list(sys.path)
    sys.path.insert(0, str(root))
    purge = lambda: [sys.modules.pop(k) for k in [k for k in sys.modules if k == "visioncore" or k.startswith("visioncore.")]]  # noqa: E731
    purge()
    try:
        return importlib.import_module("visioncore." + name)
    finally:
        sys.path[:] = old
        purge()


def load_params(root):
    p = root / "visioncore" / "params.py"
    if not p.is_file():
        return None, "params.py missing"
    tree = parse(p)
    for n in tree.body:
        tgt = None
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in n.targets):
            tgt = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "PARAMS":
            tgt = n.value
        while tgt is not None:
            try:
                v = ast.literal_eval(tgt)
                if isinstance(v, dict):
                    return dict(v), None
            except ValueError:
                pass
            tgt = tgt.args[0] if isinstance(tgt, ast.Call) and len(tgt.args) == 1 else None
    old = list(sys.path)
    sys.path.insert(0, str(root))
    for k in [k for k in sys.modules if k == "visioncore" or k.startswith("visioncore.")]:
        del sys.modules[k]
    try:
        spec = importlib.util.spec_from_file_location("visioncore.params", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["visioncore.params"] = mod
        spec.loader.exec_module(mod)
        return dict(mod.PARAMS), None
    except Exception as e:  # noqa: BLE001 - fail-closed
        return None, f"params.py not loadable: {e}"
    finally:
        sys.path[:] = old
        for k in [k for k in sys.modules if k == "visioncore" or k.startswith("visioncore.")]:
            del sys.modules[k]


def g_threshold(root):
    out = []
    params, err = load_params(root)
    if err:
        return [("E_GATE_THRESHOLD", err)]
    tsv = root / "catalogue" / "thresholds.tsv"
    if not tsv.is_file():
        return [("E_GATE_THRESHOLD", "catalogue/thresholds.tsv missing")]
    rows = {}
    for n, line in enumerate(tsv.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#") or line.startswith("threshold_id"):
            continue
        f = line.split("\t") if "\t" in line else line.split()
        if len(f) < 3:
            out.append(("E_GATE_THRESHOLD", f"thresholds.tsv:{n} fewer than 3 fields"))
            continue
        if f[0] in rows:
            out.append(("E_GATE_THRESHOLD", f"thresholds.tsv:{n} duplicate {f[0]}"))
        if not TID_RE.match(f[0]):
            out.append(("E_GATE_THRESHOLD", f"thresholds.tsv:{n} bad id {f[0]}"))
        rows[f[0]] = f[1]
    for k in sorted(set(params) - set(rows)):
        out.append(("E_GATE_THRESHOLD", f"PARAMS key {k} has no thresholds.tsv row"))
    for k in sorted(set(rows) - set(params)):
        out.append(("E_GATE_THRESHOLD", f"thresholds.tsv row {k} not in PARAMS"))
    for k in sorted(set(rows) & set(params)):
        if str(params[k]) != rows[k]:
            out.append(("E_GATE_THRESHOLD", f"{k}: PARAMS {params[k]} != tsv {rows[k]}"))
    allow = set()
    lj = root / "catalogue" / "layers.json"
    if lj.is_file():
        try:
            allow = set(json.loads(lj.read_text(encoding="utf-8")).get("structural_constants", []))
        except ValueError:
            pass
    for p in pyfiles(root, "visioncore"):
        if LAYER.get(stem_of(root, p), 0) not in (1, 2, 3):
            continue
        for n in parse(p).body:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and type(n.value.value) is int:
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id.lstrip("_").isupper() and t.id not in params and t.id not in allow:
                        out.append(("E_GATE_THRESHOLD", f"{rel(root, p)}:{n.lineno} constant {t.id} is not a PARAMS row "
                                    "(add it to params.py + thresholds.src, or list it in layers.src structural_constants)"))
    return out


def exempt_ranges(text, tree):
    lines = text.splitlines()
    rngs = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lo = max(0, n.lineno - 4)
            if any("@anchor:" + EXEMPT_ANCHOR in l for l in lines[lo:n.lineno]):
                rngs.append((n.lineno, n.end_lineno))
    return rngs


def _strish(o):
    if isinstance(o, ast.JoinedStr):
        return True
    if isinstance(o, ast.BinOp) and isinstance(o.op, ast.Mod):
        o = o.left
    return isinstance(o, ast.Constant) and isinstance(o.value, str)


MATH_OK = {"isqrt", "gcd", "comb", "perm", "lcm", "factorial"}
BAD_MODULES = {"statistics", "fractions", "decimal", "cmath"}
NP_FLOATY = {"mean", "average", "median", "std", "var", "divide", "true_divide", "sqrt", "linspace", "power", "log", "exp"}


def _nonneg_int(o):
    return isinstance(o, ast.Constant) and type(o.value) is int and o.value >= 0


def _safe_pow(base, exp):
    """int ** nonneg-int-constant, or the structural quadtree/bit forms 2 ** k and 4 ** k (k a bounded int name)."""
    if _nonneg_int(exp):
        return True
    return isinstance(base, ast.Constant) and base.value in (2, 4) and type(base.value) is int and isinstance(exp, ast.Name)


def g_nofloat(root):
    out = []
    for p in pyfiles(root, "visioncore"):
        if LAYER.get(stem_of(root, p), 9) > 3:
            continue
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text)
        ex = exempt_ranges(text, tree)
        for n in ast.walk(tree):
            ln = getattr(n, "lineno", 0)
            if any(a <= ln <= b for a, b in ex):
                continue
            bad = None
            if isinstance(n, ast.Constant) and isinstance(n.value, float):
                bad = "float literal"
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float":
                bad = "float()"
            elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                if not any(_strish(o) for o in (n.left, n.right)):
                    bad = "true division"  # `path / "x"` is a Path join, not arithmetic
            elif isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Div):
                bad = "true division"
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "float":
                bad = "float." + n.attr
            elif isinstance(n, ast.Attribute) and n.attr.startswith("float") and isinstance(n.value, ast.Name) and n.value.id in ("np", "numpy"):
                bad = n.value.id + "." + n.attr
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "math" and n.attr not in MATH_OK:
                bad = "math." + n.attr
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "operator" and n.attr in ("truediv", "itruediv"):
                bad = "operator." + n.attr
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ("np", "numpy") and n.attr in NP_FLOATY:
                bad = n.value.id + "." + n.attr
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("mean", "std", "var", "median"):
                bad = "." + n.func.attr + "()"
            elif isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.Pow):
                if not _safe_pow(n.left if isinstance(n, ast.BinOp) else n.target, n.right if isinstance(n, ast.BinOp) else n.value):
                    bad = "power with a non-constant or negative exponent"
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "pow" and len(n.args) >= 2 and not _safe_pow(n.args[0], n.args[1]):
                bad = "pow() with a non-constant or negative exponent"
            elif isinstance(n, ast.Import) and any(a.name.split(".")[0] in BAD_MODULES for a in n.names):
                bad = "import of a float/rational module"
            elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in BAD_MODULES | {"operator"} and \
                    (n.module.split(".")[0] != "operator" or any(a.name in ("truediv", "itruediv") for a in n.names)):
                bad = "import from " + n.module
            elif isinstance(n, ast.ImportFrom) and n.module == "math" and any(a.name not in MATH_OK for a in n.names):
                bad = "from math import " + ",".join(a.name for a in n.names if a.name not in MATH_OK)
            if bad:
                out.append(("E_GATE_NOFLOAT", f"{rel(root, p)}:{ln} {bad}"))
    return out


def g_engine_free(root):
    out = []
    for p in pyfiles(root, "visioncore"):
        text = p.read_text(encoding="utf-8")
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError) as e:
            out.append(("E_GATE_ENGINE", f"{rel(root, p)} untokenizable: {e}"))
            continue
        for t in toks:
            hit = None
            if t.type == tokenize.NAME:
                low = t.string.lower()
                if low in ENGINE_NAMES or low.startswith("godot"):
                    hit = t.string
            elif t.type == tokenize.STRING:
                try:
                    v = ast.literal_eval(t.string)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(v, str) and (v.lower() in ENGINE_NAMES or v.lower().startswith("godot") and " " not in v):
                    hit = v
            if hit:
                out.append(("E_GATE_ENGINE", f"{rel(root, p)}:{t.start[0]} engine token {hit}"))
    return out


def _add_leaves(n):
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        return _add_leaves(n.left) + _add_leaves(n.right)
    return [n]


def g_ids(root):
    out = []
    for pkg in ("visioncore", "vmem"):
        for p in pyfiles(root, pkg):
            if p.name == "ids.py":
                continue
            for n in ast.walk(parse(p)):
                hit = False
                if isinstance(n, ast.JoinedStr):
                    tpl = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)
                    hit = bool(ID_TEMPLATES.match(tpl) or ID_FMT_TPL.match(tpl))
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and ID_PCT.match(n.value):
                    hit = True
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format" \
                        and isinstance(n.func.value, ast.Constant) and isinstance(n.func.value.value, str):
                    hit = bool(ID_FMT_TPL.match(n.func.value.value))
                elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                    leaves = _add_leaves(n)
                    cs = [x.value for x in leaves if isinstance(x, ast.Constant) and isinstance(x.value, str)]
                    first = leaves[0]
                    hit = (isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value in ("f", "r", "p", "k", "g")
                           and any(c.startswith((":t", ":L", ":all")) for c in cs))
                if hit:
                    out.append(("E_GATE_IDS", f"{rel(root, p)}:{n.lineno} formats an id outside ids.py"))
    return out


def g_size(root):
    out = []
    for pkg in SRC_PKGS:
        for p in pyfiles(root, pkg):
            n = len(p.read_text(encoding="utf-8").splitlines())
            if n > MAX_LINES:
                out.append(("E_GATE_SIZE", f"{rel(root, p)} {n} lines > {MAX_LINES}"))
    return out


def g_schema(root):
    out = []
    sd = root / "visioncore" / "schemas"
    if not sd.is_dir() or not list(sd.glob("*.schema.json")):
        return [("E_GATE_SCHEMA", "visioncore/schemas has no *.schema.json")]
    for p in sorted(sd.glob("*.schema.json")):
        try:
            o = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            out.append(("E_GATE_SCHEMA", f"{p.name} invalid JSON: {e}"))
            continue
        sid = o.get("$id") if isinstance(o, dict) else None
        if sid is None and isinstance(o, dict):
            sid = (o.get("properties", {}).get("schema") or {}).get("const")
        if not isinstance(sid, str) or not SCHEMA_ID_RE.match(sid):
            out.append(("E_GATE_SCHEMA", f"{p.name} id {sid!r} violates vqa.<name>/v<N>"))
            continue
        if p.name != sid.replace("/v", ".v") + ".schema.json":
            out.append(("E_GATE_SCHEMA", f"{p.name} does not match id {sid}"))
    ids = root / "visioncore" / "ids.py"
    if ids.is_file():
        try:
            mod = import_vc(root, "ids")
            for fn in ("field_id", "region_id"):
                if not callable(getattr(mod, fn, None)):
                    out.append(("E_GATE_SCHEMA", f"ids.{fn} missing"))
            if callable(getattr(mod, "field_id", None)):
                for fr, m, want in ((84, 0x0A40, "f84:t0a40"), (0, 0, "f0:t0000"), (7, 0x1A3, "f7:t01a3")):
                    if mod.field_id(fr, m) != want:
                        out.append(("E_GATE_SCHEMA", f"field_id({fr},{m}) != {want}"))
        except Exception as e:  # noqa: BLE001 - fail-closed
            out.append(("E_GATE_SCHEMA", f"ids.py not loadable: {e}"))
    else:
        out.append(("E_GATE_SCHEMA", "visioncore/ids.py missing"))
    return out


def g_canon(root):
    out = []
    files = sorted((root / "catalogue").glob("*.json")) if (root / "catalogue").is_dir() else []
    files += sorted((root / "tests" / "golden").rglob("expect/*.json")) if (root / "tests" / "golden").is_dir() else []
    for p in files:
        t = p.read_text(encoding="utf-8")
        try:
            if canon(json.loads(t)) != t:
                out.append(("E_GATE_CANON", f"{rel(root, p)} not canonical"))
        except ValueError:
            out.append(("E_GATE_CANON", f"{rel(root, p)} invalid JSON"))
    jl = sorted((root / "catalogue").glob("*.jsonl")) if (root / "catalogue").is_dir() else []
    for p in jl:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() and canon(json.loads(line)) != line + "\n":
                out.append(("E_GATE_CANON", f"{rel(root, p)}:{n} not canonical"))
    return out


def g_pure(root):
    """gates must not read the clock or randomness (SW-0238 INV-HOT-01 analogue)."""
    if not (root / "tools" / "gates.py").is_file():
        return [("E_GATE_PURE", "tools/gates.py missing")]
    out = []
    for p in pyfiles(root, "tools") + pyfiles(root, "vnative"):
        r = rel(root, p)
        tree = parse(p)
        for top, sub, ln in imports_of(tree, "tools"):
            if top in IMPURE_MODS:
                out.append(("E_GATE_PURE", f"{r}:{ln} imports {top}"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" and n.attr in ("urandom", "getpid", "getrandom", "times"):
                out.append(("E_GATE_PURE", f"{r}:{n.lineno} os.{n.attr}"))
            elif isinstance(n, ast.Name) and n.id == "__import__":
                out.append(("E_GATE_PURE", f"{r}:{n.lineno} __import__"))
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "import_module" and n.args \
                    and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).split(".")[0] in IMPURE_MODS:
                out.append(("E_GATE_PURE", f"{r}:{n.lineno} import_module of {n.args[0].value}"))
    return out


def _checks():
    return load_py(Path(__file__).resolve().parent / "gate_checks.py", "_gate_checks")


def g_catalogue(root):
    return _checks().catalogue(root)


def g_golden(root):
    return _checks().golden(root)


def g_route_budget(root):
    return _checks().route_budget(root)


def g_hint_neutral(root):
    return _checks().hint_neutral(root)


def g_sealed_untouched(root):
    return _checks().sealed_untouched(root)


def g_disk_frames(root):
    return _checks().disk_frames(root)


NA_GATES = ("g_hint_neutral",)

GATES = [("g_tier1", g_tier1), ("g_anchor", g_anchor), ("g_header", g_header), ("g_layer", g_layer),
         ("g_threshold", g_threshold), ("g_nofloat", g_nofloat), ("g_engine_free", g_engine_free),
         ("g_ids", g_ids), ("g_hint_neutral", g_hint_neutral), ("g_golden", g_golden), ("g_catalogue", g_catalogue), ("g_schema", g_schema),
         ("g_route_budget", g_route_budget), ("g_size", g_size), ("g_canon", g_canon), ("g_pure", g_pure),
         ("g_sealed_untouched", g_sealed_untouched), ("g_disk_frames", g_disk_frames)]


def run_gates(root, names=None):
    res = {}
    for name, fn in GATES:
        if names is not None and name not in names:
            continue
        try:
            res[name] = fn(root)
        except Exception as e:  # noqa: BLE001 - fail-closed
            res[name] = [("E_GATE", f"gate crashed: {type(e).__name__}: {e}")]
    return res


def receipt(res):
    failed = [k for k, v in res.items() if v]
    na = ",".join(g for g in NA_GATES if g in res and not res[g])
    return failed, ("VG:PASS" if not failed else "VG:FAIL:" + ",".join(failed)) + (" NA:" + na if na else "")


def load_py(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_mutants(root):
    import shutil
    import tempfile
    mdir = root / "tests" / "mutants" / "gate"
    files = sorted(mdir.glob("m_*.py")) if mdir.is_dir() else []
    base_p = mdir / "_base.py"
    if not files or not base_p.is_file():
        return {"error": "no mutants or _base.py"}, ["no-mutants"]
    sys.path.insert(0, str(mdir))
    sys.modules.pop("_base", None)
    base = load_py(base_p, "_base").base_tree()
    results, survived = [], []
    tmp = Path(tempfile.mkdtemp(prefix="vgm_"))
    try:
        for i, f in enumerate([None] + files):
            tree = dict(base)
            gate = None
            if f is not None:
                m = load_py(f, "_gate_mut_" + f.stem)
                gate = m.GATE
                for k, v in m.OVERLAY.items():
                    if v is None:
                        tree.pop(k, None)
                    else:
                        tree[k] = v
            d = tmp / f"t{i}"
            for k, v in tree.items():
                q = d / k
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_text(v, encoding="utf-8")
            res = run_gates(d)
            failed, _ = receipt(res)
            if f is None:
                ok = not failed
                results.append({"mutant": "<clean base>", "expect": "pass", "failed": failed, "ok": ok})
            else:
                ok = gate in failed
                results.append({"mutant": f.stem, "gate": gate, "failed": failed, "killed": ok})
            if not ok:
                survived.append(results[-1]["mutant"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        sys.path.remove(str(mdir))
    return results, survived


def emit(obj):
    sys.stdout.write(canon(obj))
    return obj["rc"]


# @anchor:tools.gates.main
def main(argv):
    root = Path(__file__).resolve().parent.parent
    mode, gate_id, detail = None, None, False
    a = list(argv)
    while a:
        x = a.pop(0)
        if x == "--root" and a:
            root = Path(a.pop(0)).resolve()
        elif x == "--all":
            mode = "all"
        elif x == "--mutants":
            mode = "mutants"
        elif x == "--gate" and a:
            mode, gate_id = "gate", a.pop(0)
        elif x == "--json":
            detail = True
        else:
            return emit({"code": "E_USAGE", "rc": 2, "detail": "gates.py --all|--gate <id>|--mutants [--root D] [--json]"})
    if mode is None:
        return emit({"code": "E_USAGE", "rc": 2, "detail": "mode required"})
    if mode == "mutants":
        results, survived = run_mutants(root)
        if survived:
            return emit({"code": "E_MUTANT_SURVIVED", "rc": 5, "survived": survived, "results": results})
        return emit({"code": "OK", "rc": 0, "killed": len(results) - 1, "results": results if detail else []})
    names = None
    if mode == "gate":
        if gate_id not in dict(GATES):
            return emit({"code": "E_USAGE", "rc": 2, "detail": "unknown gate " + gate_id})
        names = {gate_id}
    res = run_gates(root, names)
    failed, rc_str = receipt(res)
    obj = {"code": "E_GATE" if failed else "OK", "rc": 5 if failed else 0, "failed": failed, "receipt": rc_str}
    if failed or detail:
        obj["findings"] = {k: [list(x) for x in v] for k, v in res.items() if v}
    return emit(obj)


def _vnative_layer(root):  # called from g_layer: vnative imports visioncore/vnative/numpy/stdlib only
    return [("E_GATE_LAYER_PKG", f"{rel(root, p)}:{ln} vnative imports {top}") for p in pyfiles(root, "vnative")
            for top, _, ln in imports_of(parse(p), "vnative") if top not in ("vnative", "visioncore", "numpy") and not is_stdlib(top)]


NATIVE_SKIP = {".git", "build", "__pycache__", "target"}  # build/native holds the built artefact
DL_NAMES = {"CDLL", "PyDLL", "cdll", "pydll", "LoadLibrary", "WinDLL", "OleDLL", "windll", "oledll"}


def g_native_scope(root):  # RT1: sealed trees never mention the opt-in backend; no shared objects in the repo; dlopen (ctypes) only inside vnative/
    import os  # local: a module-level import would shift the catalogue anchor lines
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in NATIVE_SKIP)
        for p in sorted(Path(dp) / f for f in fn):
            top, r = p.relative_to(root).parts[0], rel(root, p)
            if top in ("visioncore", "vmem") and any(k in p.read_bytes() for k in (b"vnative", b"VISIONCORE_BACKEND")):
                out.append(("E_GATE_NATIVE_SCOPE", f"{r} mentions vnative/VISIONCORE_BACKEND"))
            if p.suffix == ".so" or ".so." in p.name:
                out.append(("E_GATE_NATIVE_SCOPE", f"{r} shared object in source tree"))
            elif p.suffix == ".py" and top != "vnative":
                for n in ast.walk(parse(p)):
                    hit = n.attr if isinstance(n, ast.Attribute) else n.id if isinstance(n, ast.Name) else getattr(n, "name", None) if isinstance(n, ast.alias) else None
                    if hit in DL_NAMES:
                        out.append(("E_GATE_NATIVE_SCOPE", f"{r}:{n.lineno} {hit} (dlopen only inside vnative/)"))
    return out


GATES.append(("g_native_scope", g_native_scope))  # appended after main() so earlier anchor line numbers (catalogue) do not move


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
