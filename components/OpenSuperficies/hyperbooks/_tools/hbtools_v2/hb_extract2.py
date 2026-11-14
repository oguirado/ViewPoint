#!/usr/bin/env python3
"""HB2-01 / 3.1 / 3.2: symbols (H4 ids), anchors v2, imports (star propagation), CALLS rules 1-5, forward+reverse CSR graph.

Usage: hb_extract2.py --subject subject --out out/   (writes out/symbols_raw.jsonl, anchors.jsonl, graph.json, extract.json)
Scope = <root>/tools/*.py and <root>/tests/*.py (top level), as in the census (C-1).
"""
import argparse, ast, copy, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_lib_bundle as SB

CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ETYPES = ["CALLS", "IMPORTS", "TESTS", "TESTS_STATIC", "GOVERNS", "VERIFIES", "SCENE_BINDS"]
KINDS = ["module", "class", "function", "method", "constant", "test", "gd_func", "scene_node", "subanchor", "section", "receipt", "claim"]
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
MOD = "__module__"


def scope_files(root, dirs=("tools", "tests")):
    out = []
    if L.SRC_DIRS:                                   # HBV2_SRC_DIRS: file or directory prefixes under root
        for pre in L.SRC_DIRS:
            p = os.path.join(root, pre)
            if os.path.isfile(p):
                out.append(pre)
            elif os.path.isdir(p):
                out += [pre.rstrip("/") + "/" + f for f in sorted(os.listdir(p)) if f.endswith(".py") and os.path.isfile(os.path.join(p, f))]
        return sorted(set(out))
    for d in dirs:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            out += [d + "/" + f for f in sorted(os.listdir(p)) if f.endswith(".py") and os.path.isfile(os.path.join(p, f))]
    return out


def sig_text(node):
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
        return L.clip_bytes("%s(%s)" % (node.name, ", ".join(bases)) if bases else node.name, 160)
    s = "%s(%s)" % (node.name, ast.unparse(node.args))
    if node.returns is not None:
        s += " -> " + ast.unparse(node.returns)
    return L.clip_bytes(s.replace("\n", " "), 160)


def sig_hash(node):
    if isinstance(node, ast.ClassDef):
        parts = ["class", node.name] + [ast.dump(b) for b in node.bases] + [ast.dump(k) for k in node.keywords]
    else:
        parts = ["def", node.name, ast.dump(node.args)] + ([ast.dump(node.returns)] if node.returns is not None else [])
    parts += [ast.dump(d) for d in getattr(node, "decorator_list", [])]
    return L.sha256s("\n".join(parts))


def strip_doc(node):
    n = copy.deepcopy(node)
    body = getattr(n, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        n.body = body[1:]
    return n


def ast_hash(node):
    return L.sha256s(ast.dump(strip_doc(node), include_attributes=False))


def shell_hash(node):
    """AST hash of a class/module with nested function/class definitions removed (children's changes do not alter it)."""
    n = strip_doc(node)
    n.body = [b for b in n.body if not isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return L.sha256s(ast.dump(n, include_attributes=False))


def doc_hash(node):
    d = ast.get_docstring(node, clean=False) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) else None
    return L.sha256s(d or "")


class Mod:
    """One parsed module: symbols with byte spans plus its import tables."""

    def __init__(self, path, data):
        self.path, self.data = path, data
        self.src = data.decode("utf-8")
        self.tree = ast.parse(self.src)
        self.is_test = path.startswith("tests/")
        self.stem = os.path.splitext(os.path.basename(path))[0]
        offs = [0]
        for i, b in enumerate(data):
            if b == 10:
                offs.append(i + 1)
        if offs[-1] != len(data):
            offs.append(len(data))
        self.offs = offs                     # line k (1-based) occupies [offs[k-1], offs[k])
        self.nlines = len(offs) - 1
        self.syms = []                       # dicts: id, kind, node, parent_cls, qual
        self.by_qual = {}
        self.alias = {}                      # local name -> module path (in scope) or "ext:<name>"
        self.fromn = {}                      # local name -> (module path | "ext:<m>", symbol name)
        self.stars = []                      # module paths star-imported
        self.imports_mods = set()            # in-scope module paths imported
        self.import_syms = set()             # (module path, name)
        self._collect()

    def span(self, node):
        s = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        e = node.end_lineno
        return s, e, self.offs[s - 1], self.offs[min(e, self.nlines)]

    def _add(self, node, kind, qual, cls=None):
        s, e, sb, eb = self.span(node)
        self.syms.append(dict(qual=qual, kind=kind, node=node, cls=cls, start_line=s, end_line=e, start_byte=sb, end_byte=eb))
        self.by_qual[qual] = self.syms[-1]

    def _collect(self):
        mn = self.tree
        self.syms.append(dict(qual=MOD, kind="module", node=mn, cls=None, start_line=1, end_line=max(1, self.nlines),
                              start_byte=0, end_byte=len(self.data)))
        self.by_qual[MOD] = self.syms[0]
        for node in mn.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add(node, "function", node.name)
            elif isinstance(node, ast.ClassDef):
                self._add(node, "class", node.name)
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        k = "test" if (self.is_test and m.name.startswith("test_")) else "method"
                        self._add(m, k, node.name + "." + m.name, cls=node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                tg = node.targets if isinstance(node, ast.Assign) else [node.target]
                if len(tg) == 1 and isinstance(tg[0], ast.Name) and CONST_RE.match(tg[0].id):
                    self._add(node, "constant", tg[0].id)

    def import_nodes(self):
        """All import statements anywhere in the module (function-level imports are recorded in the module tables)."""
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                yield n


def build_ids(mods):
    """H4 ids: path::qual; test methods path::name, collision -> path::Class.meth."""
    for m in mods:
        names = {}
        for s in m.syms:
            if s["kind"] == "test":
                names.setdefault(s["qual"].split(".", 1)[1], []).append(s)
        for s in m.syms:
            if s["kind"] == "test" and len(names[s["qual"].split(".", 1)[1]]) == 1:
                s["id"] = "%s::%s" % (m.path, s["qual"].split(".", 1)[1])
            else:
                s["id"] = "%s::%s" % (m.path, s["qual"])
        ids = [s["id"] for s in m.syms]
        if len(ids) != len(set(ids)):
            raise ValueError("E_DUP_ID in " + m.path)


class Repo:
    def __init__(self, root, dirs=("tools", "tests")):
        self.root = root
        self.files = scope_files(root, dirs)
        self.mods = [Mod(p, L.readb(os.path.join(root, p))) for p in self.files]
        self.bypath = {m.path: m for m in self.mods}
        self.stem = {}
        for m in sorted(self.mods, key=lambda m: (m.path.startswith("tests/"), m.path)):   # tools win a stem clash
            self.stem.setdefault(m.stem, m.path)
        build_ids(self.mods)
        self.symid = {}                        # (path, qual) -> id
        for m in self.mods:
            for s in m.syms:
                self.symid[(m.path, s["qual"])] = s["id"]
        for m in self.mods:
            self._imports(m)
        self._propagate_stars()

    def _imports(self, m):
        for n in m.import_nodes():
            if isinstance(n, ast.Import):
                for a in n.names:
                    top = a.name.split(".")[0]
                    tgt = self.stem.get(a.name) if "." not in a.name else None
                    local = a.asname or top
                    if tgt:
                        m.alias[local] = tgt
                        m.imports_mods.add(tgt)
                    else:
                        m.alias[local] = "ext:" + a.name
            else:
                if n.level:
                    continue
                mod = n.module or ""
                tgt = self.stem.get(mod) if "." not in mod else None
                for a in n.names:
                    if a.name == "*":
                        if tgt:
                            m.stars.append(tgt)
                            m.imports_mods.add(tgt)
                        continue
                    local = a.asname or a.name
                    if tgt:
                        sub = self.stem.get(a.name)
                        m.imports_mods.add(tgt)
                        if (tgt, a.name) in self.symid and not sub:
                            m.fromn[local] = (tgt, a.name)
                            m.import_syms.add((tgt, a.name))
                        else:
                            m.fromn[local] = (tgt, a.name)
                    else:
                        sub = self.stem.get(a.name) if mod == "" else None
                        if sub:
                            m.alias[local] = sub
                            m.imports_mods.add(sub)
                        else:
                            m.fromn[local] = ("ext:" + mod, a.name)

    def _propagate_stars(self):
        """`from c import *`: c's own public symbols plus c's alias/from tables, one level (C-7)."""
        for m in self.mods:
            m.star_syms = {}
            for c in m.stars:
                cm = self.bypath[c]
                for s in cm.syms:
                    if "." not in s["qual"] and s["qual"] != MOD and not s["qual"].startswith("_"):
                        m.star_syms.setdefault(s["qual"], (c, s["qual"]))
                for k, v in cm.alias.items():
                    if not k.startswith("_"):
                        m.alias.setdefault(k, v)
                        if v.startswith("ext:"):
                            continue
                        m.imports_mods.add(v)
                for k, v in cm.fromn.items():
                    if not k.startswith("_"):
                        m.fromn.setdefault(k, v)

    # ---- call resolution --------------------------------------------------------------------------
    def bases_of(self, m, cls):
        """In-repo base classes of class `cls` in module m, MRO order (left to right, depth first, de-duplicated)."""
        node = m.by_qual[cls]["node"]
        out = []
        for b in node.bases:
            r = None
            if isinstance(b, ast.Name):
                r = self.resolve_name(m, b.id)
            elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                a = m.alias.get(b.value.id)
                if a and not a.startswith("ext:") and (a, b.attr) in self.symid:
                    r = (a, b.attr)
            if r and r[0] in self.bypath and r[1] in self.bypath[r[0]].by_qual and self.bypath[r[0]].by_qual[r[1]]["kind"] == "class":
                if r not in out:
                    out.append(r)
                    for rr in self.bases_of(self.bypath[r[0]], r[1]):
                        if rr not in out:
                            out.append(rr)
        return out

    def resolve_name(self, m, name):
        """(module path, qual) for a bare Name: rule 1 (same module) then rule 2 (from-import, incl. star)."""
        if name in m.by_qual and "." not in name:
            return (m.path, name)
        if name in m.fromn:
            t, x = m.fromn[name]
            if not t.startswith("ext:") and (t, x) in self.symid:
                return (t, x)
            return None
        if name in m.star_syms:
            return m.star_syms[name]
        return None

    def resolve_call(self, m, func, cls):
        """-> ('R', (path, qual)) | ('X', None) external | ('U', None) unresolved."""
        if isinstance(func, ast.Name):
            r = self.resolve_name(m, func.id)
            if r:
                return "R", r
            if func.id in m.alias or func.id in m.fromn or func.id in BUILTINS:
                return "X", None
            return "U", None
        if isinstance(func, ast.Attribute):
            v = func.value
            if isinstance(v, ast.Name):
                if v.id in ("self", "cls") and cls:
                    for (p, c) in [(m.path, cls)] + self.bases_of(m, cls):
                        q = c + "." + func.attr
                        if (p, q) in self.symid:
                            return "R", (p, q)
                    return "U", None
                a = m.alias.get(v.id)
                if a:
                    if a.startswith("ext:"):
                        return "X", None
                    if (a, func.attr) in self.symid:
                        return "R", (a, func.attr)
                    return "U", None
                if v.id in m.fromn and m.fromn[v.id][0].startswith("ext:"):
                    return "X", None
                if v.id in BUILTINS:
                    return "X", None
            root = v
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Constant):
                return "X", None
            if isinstance(root, ast.Name) and root is not v:
                a = m.alias.get(root.id)
                if (a and a.startswith("ext:")) or (root.id in m.fromn and m.fromn[root.id][0].startswith("ext:")):
                    return "X", None
            return "U", None
        return "U", None


BUILTINS = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)


class CallVisitor(ast.NodeVisitor):
    """Collect (call node) under one symbol body, not descending into nested *symbols* (they own their calls)."""

    def __init__(self):
        self.calls = []

    def visit_Call(self, n):
        self.calls.append(n)
        self.generic_visit(n)


def calls_of(sym):
    node = sym["node"]
    cv = CallVisitor()
    kind = sym["kind"]
    if kind == "module":
        for st in node.body:
            if isinstance(st, DEFS):
                for d in getattr(st, "decorator_list", []):
                    cv.visit(d)
                continue
            cv.visit(st)
    elif kind == "class":
        for st in node.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in st.decorator_list:
                    cv.visit(d)
                continue
            cv.visit(st)
        for d in node.decorator_list:
            cv.visit(d)
    else:
        cv.visit(node)
    return cv.calls


def edges_calls(repo):
    """CALLS edges src -> dst (distinct), plus counters of resolved / external / unresolved call sites."""
    E, stat = set(), dict(calls=0, resolved=0, external=0, unresolved=0)
    unres = []
    for m in repo.mods:
        for s in m.syms:
            if s["kind"] in ("constant",):
                continue
            for c in calls_of(s):
                stat["calls"] += 1
                k, r = repo.resolve_call(m, c.func, s["cls"])
                if k == "R":
                    dst = repo.symid[r]
                    E.add((s["id"], dst, "CALLS"))
                    stat["resolved"] += 1
                    if r[1] in repo.bypath[r[0]].by_qual and repo.bypath[r[0]].by_qual[r[1]]["kind"] == "class":
                        init = r[1] + ".__init__"
                        if (r[0], init) in repo.symid:
                            E.add((s["id"], repo.symid[(r[0], init)], "CALLS"))
                elif k == "X":
                    stat["external"] += 1
                else:
                    stat["unresolved"] += 1
                    unres.append((s["id"], c.lineno))
    return E, stat, unres


def edges_imports(repo):
    E = set()
    for m in repo.mods:
        mid = repo.symid[(m.path, MOD)]
        for t in m.imports_mods:
            if t != m.path:
                E.add((mid, repo.symid[(t, MOD)], "IMPORTS"))
        for (t, x) in m.import_syms:
            if t != m.path:
                E.add((mid, repo.symid[(t, x)], "IMPORTS"))
    return E


def edges_tests_static(repo, calls_edges):
    """Static test -> tool symbol edges (TESTS_STATIC): CALLS edges whose source is a test and target is in tools/."""
    return {(a, b, "TESTS_STATIC") for (a, b, _t) in calls_edges if a.startswith("tests/") and L.is_src(b)}


# ---- graph (SoA CSR) --------------------------------------------------------------------------------
def build_graph(ids, kinds, edges):
    """ids sorted bytewise; edges (src,dst,etype); returns the byte-stable graph dict (forward + reverse CSR)."""
    ids = sorted(set(ids), key=lambda s: s.encode("utf-8"))
    ix = {s: i for i, s in enumerate(ids)}
    kn = sorted(set(kinds.values()))
    kcode = {k: i for i, k in enumerate(KINDS)}
    kind = [kcode[kinds[s]] for s in ids]
    ecode = {e: i for i, e in enumerate(ETYPES)}
    es = sorted({(ix[a], ix[b], ecode[t]) for (a, b, t) in edges if a in ix and b in ix})

    def csr(pairs):
        n = len(ids)
        rows = [[] for _ in range(n)]
        for a, b, t in pairs:
            rows[a].append((b, t))
        ptr, col, et = [0], [], []
        for r in rows:
            r.sort()
            for b, t in r:
                col.append(b)
                et.append(t)
            ptr.append(len(col))
        return {"row_ptr": ptr, "col": col, "etype": et}
    return {"ids": ids, "kinds": KINDS, "kind": kind, "etypes": ETYPES, "fwd": csr(es), "rev": csr([(b, a, t) for a, b, t in es])}


class Graph:
    """Read-only view of graph.json with int-bitset reachability (pilot N ~ 330)."""

    def __init__(self, g):
        self.g = g
        self.ids = g["ids"]
        self.ix = {s: i for i, s in enumerate(self.ids)}
        self.n = len(self.ids)
        self.kind = [g["kinds"][k] for k in g["kind"]]
        self.etypes = g["etypes"]

    def row(self, direction, i, types=None):
        c = self.g[direction]
        out = []
        for j in range(c["row_ptr"][i], c["row_ptr"][i + 1]):
            t = self.etypes[c["etype"][j]]
            if types is None or t in types:
                out.append((c["col"][j], t))
        return out

    def neighbours(self, direction, i, types=None):
        return sorted({j for j, _ in self.row(direction, i, types)})

    def rev_bits(self, types=None):
        """One Python int bitset per node: direct dependants (reverse row)."""
        out = []
        for i in range(self.n):
            b = 0
            for j in self.neighbours("rev", i, types):
                b |= 1 << j
            out.append(b)
        return out

    def closure(self, seeds, direction="rev", depth=3, types=None):
        """Frontier BFS: {node index: depth} for nodes reached from seeds within `depth` hops (seeds at 0)."""
        dist = {s: 0 for s in seeds}
        fr = sorted(seeds)
        for d in range(1, depth + 1):
            nxt = []
            for i in fr:
                for j in self.neighbours(direction, i, types):
                    if j not in dist:
                        dist[j] = d
                        nxt.append(j)
            fr = sorted(nxt)
            if not fr:
                break
        return dist


def symbols_from_repo(repo):
    rows = []
    for m in repo.mods:
        for s in m.syms:
            node, kind = s["node"], s["kind"]
            raw = m.data[s["start_byte"]:s["end_byte"]]
            r = {"schema": "hb.symbol.v2", "id": s["id"], "kind": kind, "path": m.path,
                 "span": {"start_byte": s["start_byte"], "end_byte": s["end_byte"], "start_line": s["start_line"], "end_line": s["end_line"]},
                 "bytes": s["end_byte"] - s["start_byte"], "sha256": L.sha256b(raw),
                 "sha256_ast": ast_hash(node), "doc_sha256": doc_hash(node)}
            if kind in ("class", "module"):
                r["sha256_shell"] = shell_hash(node)
            if kind in ("function", "method", "class", "test"):
                r["sig"] = sig_text(node)
                r["signature_sha256"] = sig_hash(node)
            elif kind == "module":
                r["sig"] = L.clip_bytes(m.path, 160)
                r["signature_sha256"] = L.sha256s(m.path)
            else:
                r["sig"] = L.clip_bytes(s["qual"], 160)
                r["signature_sha256"] = L.sha256s(s["qual"])
            r["governs"] = {"sections": [], "inv": [], "au": [], "none_declared": True}
            rows.append(r)
    rows.sort(key=lambda r: r["id"].encode("utf-8"))
    return rows


def anchors_from_symbols(rows, lang_default="python"):
    """hb.anchor.v2 = v1 fields (path, symbol, role, kind, span, sha256, lang, signature_sha256) + id, bytes, ast/doc hashes, status."""
    out = []
    for r in rows:
        a = {"schema": "hb.anchor.v2", "id": r["id"], "path": r["path"], "symbol": r["id"].split("::", 1)[1], "role": "symbol",
             "kind": r["kind"] if r["kind"] != "test" else "test", "lang": r.get("lang", lang_default),
             "span": {"start_line": r["span"]["start_line"], "end_line": r["span"]["end_line"]},
             "start_byte": r["span"]["start_byte"], "end_byte": r["span"]["end_byte"],
             "sha256": r["sha256"], "sha256_ast": r["sha256_ast"], "doc_sha256": r["doc_sha256"],
             "signature_sha256": r["signature_sha256"], "status": "active", "redirect_to": None}
        if "sha256_shell" in r:
            a["sha256_shell"] = r["sha256_shell"]
        out.append(a)
    return out


def extract(root, dirs=("tools", "tests")):
    repo = Repo(root, dirs)
    syms = symbols_from_repo(repo)
    kinds = {r["id"]: r["kind"] for r in syms}
    ce, stat, unres = edges_calls(repo)
    ie = edges_imports(repo)
    st = edges_tests_static(repo, ce)
    edges = ce | ie | st
    return dict(repo=repo, symbols=syms, anchors=anchors_from_symbols(syms), edges=edges, call_edges=ce, import_edges=ie,
                static_test_edges=st, stats=stat, unresolved=unres, kinds=kinds)


def make_receipt(kind, tool, metrics, inputs=None, extra=None):
    r = {"schema": "hb.receipt.v2", "kind": kind, "tool": tool, "metrics": metrics, "inputs": inputs or {}}
    if extra:
        r.update(extra)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--out", default=os.path.join(L.HB, "out"))
    ap.add_argument("--symbols-bundle", default=os.environ.get("HBV2_SYMBOLS_BUNDLE"))
    ap.add_argument("--hb-id", default=os.environ.get("HBV2_HB_ID"))
    ap.add_argument("--trace", help="trace.json whose TESTS edges are added to graph.json")
    a = ap.parse_args(argv)
    x = SB.load(a.symbols_bundle) if a.symbols_bundle else extract(a.subject)
    bundle = x["bundle"] if a.symbols_bundle else SB.emit(x, a.subject, SB.book_id(os.path.dirname(os.path.abspath(a.out)), explicit=a.hb_id))
    if a.hb_id:
        bundle = dict(bundle, hb_id=a.hb_id)
    SB.validate(bundle)
    edges = set(x["edges"])
    tmetrics = {}
    tp = a.trace or os.path.join(a.out, "trace.json")
    if os.path.isfile(tp):
        tr = L.jload(tp)
        te = {(e[0], e[1], "TESTS") for e in tr.get("edges", [])}
        edges |= te
        tmetrics["tests_edges"] = len(te)
    g = build_graph([r["id"] for r in x["symbols"]], x["kinds"], edges)
    os.makedirs(a.out, exist_ok=True)
    L.write_json(os.path.join(a.out, "symbols_bundle.json"), bundle)
    L.write_jsonl(os.path.join(a.out, "symbols_raw.jsonl"), x["symbols"])
    L.write_jsonl(os.path.join(a.out, "anchors.jsonl"), x["anchors"])
    L.write_json(os.path.join(a.out, "graph.json"), g)
    m = dict(symbols=len(x["symbols"]), edges=len(edges), calls_edges=len(x["call_edges"]), import_edges=len(x["import_edges"]),
             tests_static_edges=len(x["static_test_edges"]), call_sites=x["stats"]["calls"], calls_resolved=x["stats"]["resolved"],
             calls_external=x["stats"]["external"], calls_unresolved=x["stats"]["unresolved"], **tmetrics)
    L.write_json(os.path.join(a.out, "extract.json"), make_receipt("build", "hb_extract2.py/2.0", m,
                 {"graph_sha256": L.sha256b(L.canon(g)), "anchors_sha256": L.sha256b(open(os.path.join(a.out, "anchors.jsonl"), "rb").read())}))
    print(L.dumps(m))
    return 0




# ---- symbol text fields (HB2-02: name, sig, doc, com, lit) ---------------------------------------------
def _own_nodes(sym):
    """AST nodes owned by the symbol: its subtree minus nested symbol bodies (methods of a class, defs of a module)."""
    node, kind = sym["node"], sym["kind"]
    stack = list(node.body) if kind in ("module", "class") else [node]
    out = []
    while stack:
        n = stack.pop()
        if kind in ("module", "class") and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def symbol_texts(repo):
    """id -> {name, sig, doc, com, lit} raw strings. com = comments inside the span (innermost symbol), lit = str constants < 80 B."""
    import tokenize, io
    res = {}
    for m in repo.mods:
        cm = []
        try:
            for t in tokenize.generate_tokens(io.StringIO(m.src).readline):
                if t.type == tokenize.COMMENT:
                    cm.append((t.start[0], t.string.lstrip("#").strip()))
        except tokenize.TokenError:
            pass
        real = [s for s in m.syms if s["kind"] in ("function", "method", "class", "test", "constant")]
        for s in m.syms:
            node = s["node"]
            doc = ast.get_docstring(node) or "" if hasattr(node, "body") and not isinstance(node, (ast.Assign, ast.AnnAssign)) else ""
            lits, doc_ids = [], set()
            for n in _own_nodes(s):
                b = getattr(n, "body", None)
                if isinstance(b, list) and b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant):
                    doc_ids.add(id(b[0].value))
            for n in _own_nodes(s):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids and 0 < L.blen(n.value) < 80:
                    lits.append(n.value)
            coms = []
            if s["kind"] != "module":
                for ln, txt in cm:
                    if s["start_line"] <= ln <= s["end_line"]:
                        inner = [o for o in real if o is not s and o["start_line"] <= ln <= o["end_line"] and
                                 (o["end_line"] - o["start_line"]) < (s["end_line"] - s["start_line"])]
                        if not inner:
                            coms.append(txt)
            res[s["id"]] = {"name": s["qual"] if s["kind"] != "module" else m.stem, "stem": m.stem, "doc": doc, "com": " ".join(coms),
                            "lit": " ".join(lits)}
    return res


if __name__ == "__main__":
    sys.exit(main())
