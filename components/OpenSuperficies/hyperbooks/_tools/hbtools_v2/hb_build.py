#!/usr/bin/env python3
"""HB2-05 / HB2-09: derived artefacts of the hyperbook. Pure function of (subject, godot fixture, book.md, cards, trace).

hb_build.py --render-derived [--root HB]   writes book/{book.md (generated fences only), symbols.jsonl, nodes.jsonl, anchors.jsonl,
graph.json, claims.jsonl, manifest.json}. Running it twice gives the same bytes. Problems (dangling links) are returned as issues
with code E_BOOK_LINK and make the CLI exit 3.
"""
import argparse, ast, json, os, re, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_lib_bundle as SB
import hb_pack as P  # noqa: E402
try:
    import hb_visual as V  # noqa: E402   (game profile only; absent in the engine copy)
except ImportError:
    V = None

BLOCK_RE = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
GEN_RE = re.compile(r"(<!-- HB:GEN BEGIN (hb-s\d+) -->\n)(.*?)(<!-- HB:GEN END \2 -->\n)", re.S)
BOOK_PATH = "book/book.md"


class BookError(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s %s" % (code, detail))
        self.code = code


def parse_book(text):
    """-> dict(sections=[...], inv=[...], claims=[...]) from the machine blocks; a malformed block raises BookError(E_BOOK_BLOCK)."""
    out = {"sections": [], "inv": [], "claims": []}
    for m in BLOCK_RE.finditer(text):
        try:
            o = json.loads(m.group(2))
        except ValueError as e:
            raise BookError("E_BOOK_BLOCK", "%s: %s" % (m.group(1), e))
        key = {"hb.section.v2": "sections", "hb.inv.v2": "inv", "hb.claim.v2": "claims"}.get(m.group(1))
        if key is None:
            raise BookError("E_BOOK_BLOCK", "unknown block " + m.group(1))
        out[key].append(o)
    return out


def is_public(sid):
    q = sid.split("::", 1)[1].split("#")[0]
    return not any(part.startswith("_") for part in q.split("."))


# ---- godot fixture symbols ---------------------------------------------------------------------------------
def godot_symbols(gdir, prefix="godot"):
    """Symbols of the Godot fixture: file modules, `.gd` functions and scene nodes; plus SCENE_BINDS edges."""
    rows, edges = [], []
    if not gdir or not os.path.isdir(gdir):
        return rows, edges

    def mk(sid, kind, path, data, sb, eb, sl, el, sig):
        raw = data[sb:eb]
        return {"schema": "hb.symbol.v2", "id": sid, "kind": kind, "path": path,
                "span": {"start_byte": sb, "end_byte": eb, "start_line": sl, "end_line": el}, "bytes": eb - sb, "sha256": L.sha256b(raw),
                "sha256_ast": L.sha256b(raw), "doc_sha256": L.EMPTY_SHA256, "signature_sha256": L.sha256s(sig), "sig": L.clip_bytes(sig, 160),
                "governs": {"sections": [], "inv": [], "au": [], "none_declared": True}}
    for fn in ("player.gd", "level.tscn"):
        p = os.path.join(gdir, fn)
        data = L.readb(p)
        text = data.decode("utf-8")
        lines = text.split("\n")
        offs = [0]
        for ln in lines[:-1]:
            offs.append(offs[-1] + len(ln.encode("utf-8")) + 1)
        offs.append(len(data))
        rel = "%s/%s" % (prefix, fn)
        rows.append(mk("%s::__module__" % rel, "module", rel, data, 0, len(data), 1, max(1, len(lines)), rel))
        if fn.endswith(".gd"):
            starts = [i for i, l in enumerate(lines) if l.startswith("func ")]
            for k, i in enumerate(starts):
                j = starts[k + 1] if k + 1 < len(starts) else len(lines)
                while j > i + 1 and not lines[j - 1].strip():
                    j -= 1
                name = re.match(r"func\s+(\w+)", lines[i]).group(1)
                rows.append(mk("%s::%s" % (rel, name), "gd_func", rel, data, offs[i], offs[j], i + 1, j, lines[i].rstrip(":").strip()))
        else:
            sc = V.parse_tscn(text)
            headers = [i for i, l in enumerate(lines) if l.startswith("[node ")]
            ends = [i for i, l in enumerate(lines) if l.startswith("[") and not l.startswith("[node ")]
            for k, n in enumerate(sc["nodes"]):
                i = headers[k]
                j = min([e for e in ends if e > i] + [len(lines)])
                nxt = headers[k + 1] if k + 1 < len(headers) else len(lines)
                j = min(j, nxt)
                while j > i + 1 and not lines[j - 1].strip():
                    j -= 1
                qual = n["name"] if n["path"] == "." else n["path"].replace("/", ".")
                n["_sid"] = "%s::%s" % (rel, qual)
                rows.append(mk(n["_sid"], "scene_node", rel, data, offs[i], offs[j], i + 1, j, "%s %s" % (n["type"], n["path"])))
            ids = {e["id"]: e["path"] for e in sc["ext"]}
            bysid = {n["path"]: n for n in sc["nodes"]}
            gd = {r["id"] for r in rows if r["kind"] == "gd_func"}
            for c in sc["conns"]:
                src, dst = bysid.get(c["from"]), bysid.get(c["to"])
                if not dst:
                    continue
                m = re.match(r'ExtResource\("([^"]+)"\)', dst["props"].get("script", ""))
                sp = ids.get(m.group(1)) if m else None
                if sp and sp.startswith("res://"):
                    f = "%s/%s::%s" % (prefix, sp[6:], c["method"])
                    if f in gd and src:
                        edges.append((src["_sid"], f, "SCENE_BINDS"))
            for n in sc["nodes"]:
                m = re.match(r'ExtResource\("([^"]+)"\)', n["props"].get("script", ""))
                sp = ids.get(m.group(1)) if m else None
                if sp and sp.startswith("res://"):
                    edges.append((n["_sid"], "%s/%s::__module__" % (prefix, sp[6:]), "SCENE_BINDS"))
    rows.sort(key=lambda r: r["id"].encode("utf-8"))
    return rows, sorted(set(edges))


# ---- context ------------------------------------------------------------------------------------------------
def load_ctx(subject, godot, cards_path, trace_path, symbols_bundle=None, out=None):
    x = SB.source(subject, out=out, symbols_bundle=symbols_bundle)
    syms = x["symbols"]
    texts = x["texts"]
    data = {}
    for m in x["repo"].mods:
        data[m.path] = m.data
    g_rows, g_edges = ([], []) if symbols_bundle or os.environ.get("HBV2_SYMBOLS_BUNDLE") else godot_symbols(godot)
    cards = {}
    if cards_path and os.path.isfile(cards_path):
        cards = {r["id"]: r["card"] for r in L.read_jsonl(cards_path)}
    trace = L.jload(trace_path) if trace_path and os.path.isfile(trace_path) else {"edges": [], "coverage": {}}
    return dict(x=x, syms=syms, g_rows=g_rows, g_edges=g_edges, cards=cards, trace=trace, texts=texts, data=data, subject=subject)


def sec_node(anchor):
    return "%s::%s" % (BOOK_PATH, anchor.replace("-", "_"))


def derive(ctx, book_text):
    """-> dict(symbols, anchors, nodes, graph, claims, issues, rendered_book)."""
    pb = parse_book(book_text)
    issues = []
    syms = [dict(s) for s in ctx["syms"]] + [dict(s) for s in ctx["g_rows"]]
    byid = {s["id"]: s for s in syms}
    # sub-anchors of tool symbols > 1,800 B
    subs = []
    for s in list(syms):
        if L.is_src(s["path"]) and s["kind"] in ("function", "method", "class") and s["bytes"] is not None and s["bytes"] > L.SYM_MAX and s.get("lang", ctx["x"]["bundle"]["lang_default"]) == "python":
            text = ctx["data"][s["path"]][s["span"]["start_byte"]:s["span"]["end_byte"]]
            r = P.subanchor(s["id"], text, s["span"]["start_byte"], s["span"]["start_line"])
            if r:
                s["children"] = [c["id"] for c in r["children"]]
                for c in r["children"]:
                    subs.append({"schema": "hb.symbol.v2", "id": c["id"], "kind": "subanchor", "path": s["path"],
                                 "span": {"start_byte": c["start_byte"], "end_byte": c["end_byte"], "start_line": c["start_line"], "end_line": max(c["start_line"], c["end_line"])},
                                 "bytes": c["bytes"], "sha256": c["sha256"], "sha256_ast": c["sha256"], "doc_sha256": L.EMPTY_SHA256,
                                 "signature_sha256": s["signature_sha256"], "sig": "#b" + c["id"].rsplit("#b", 1)[1] + " of " + s["sig"][:120],
                                 "governs": {"sections": [], "inv": [], "au": [], "none_declared": True}})
                s["skeleton"] = r["parent"]
    for sub in subs:
        parent = byid.get(sub["id"].split("#b", 1)[0])
        if parent and "lang" in parent:
            sub["lang"] = parent["lang"]
    syms += subs
    for s in subs:
        byid[s["id"]] = s
    modsyms = {}
    for s in syms:
        if s["kind"] not in ("module", "test", "subanchor"):
            modsyms.setdefault(s["path"], []).append(s["id"])
    # governs: sections
    gov = {s["id"]: {"sections": set(), "inv": set(), "au": set(), "claims": set(), "receipts": set(), "tests": []} for s in syms}
    sec_edges, claim_edges = [], []
    sec_ids = set()
    for sb in pb["sections"]:
        anchor = sb["anchor"]
        sec_ids.add(anchor)
        targets = list(sb.get("code_anchors", []))
        if sb.get("covers"):
            if sb["covers"] not in modsyms:
                issues.append({"code": "E_BOOK_LINK", "detail": "covers unknown module " + sb["covers"]})
            targets += [i for i in modsyms.get(sb["covers"], []) if is_public(i)]
        for t in targets:
            if t not in byid:
                issues.append({"code": "E_BOOK_LINK", "detail": "%s -> %s" % (anchor, t)})
                continue
            gov[t]["sections"].add(anchor)
            sec_edges.append((sec_node(anchor), t, "GOVERNS"))
    for ib in pb["inv"]:
        for kind, key in (("inv", "inv"), ("au", "au")):
            for item in ib.get(key, []):
                for t in item["symbols"]:
                    if t not in byid:
                        issues.append({"code": "E_BOOK_LINK", "detail": "%s -> %s" % (item["id"], t)})
                        continue
                    gov[t][key].add(item["id"])
    claims_rows, receipts = [], {}
    for cb in pb["claims"]:
        claims_rows.append(cb)
        for t in cb["anchors"]:
            if t not in byid:
                issues.append({"code": "E_BOOK_LINK", "detail": "%s -> %s" % (cb["id"], t)})
                continue
            gov[t]["claims"].add(cb["id"])
            claim_edges.append(("book/claims.jsonl::" + cb["id"].replace("-", "_"), t, "VERIFIES"))
            for r in cb.get("receipts", []):
                gov[t]["receipts"].add(r)
                f = cb["value"]["metric_ref"].split(":", 1)[0]
                claim_edges.append(("%s::%s" % (f, r.replace("-", "_")), t, "VERIFIES"))
                receipts[r] = f
    cov = ctx["trace"].get("coverage", {})
    tests_of = {}
    for t, s in ctx["trace"].get("edges", []):
        tests_of.setdefault(s, []).append(t)
    for sid, ts in tests_of.items():
        if sid in gov:
            gov[sid]["tests"] = sorted(ts, key=lambda t: (cov.get(t, 0), t))[:8]
    # finalise symbols
    for s in syms:
        g = gov[s["id"]]
        gr = {"sections": sorted(g["sections"]), "inv": sorted(g["inv"]), "au": sorted(g["au"])}
        if not g["inv"] and not g["au"]:
            gr["none_declared"] = True
        if g["claims"]:
            gr["claims"] = sorted(g["claims"])
        if g["receipts"]:
            gr["receipts"] = sorted(g["receipts"])
        if g["tests"]:
            gr["tests"] = g["tests"]
        s["governs"] = gr
        card = ctx["cards"].get(s["id"])
        if card:
            s["card"] = card
        fp = {"sig": s["sig"], "inv": gr["inv"], "au": gr["au"], "pragmas": []}
        s["contract_fp"] = L.fnv16(L.canon(fp))
    # backlink problems
    for s in syms:
        if L.is_src(s["path"]) and s["kind"] in ("function", "method", "class", "constant") and is_public(s["id"]):
            if not s["governs"]["sections"]:
                issues.append({"code": "E_BOOK_LINK", "detail": "no governing section: " + s["id"]})
    syms.sort(key=lambda r: r["id"].encode("utf-8"))
    anchors = X.anchors_from_symbols(syms, lang_default=ctx["x"]["bundle"]["lang_default"])
    for a, s in zip(anchors, syms):
        if s["path"].startswith("godot/"):
            a["lang"] = "gdscript" if s["path"].endswith(".gd") else "other"
            if s["kind"] == "scene_node":
                a["role"] = "scene"
                a["symbol"] = "scene:res://%s::%s" % (s["path"].split("/", 1)[1], s["id"].split("::", 1)[1].replace(".", "/"))
    # graph
    kinds = {s["id"]: s["kind"] for s in syms}
    edges = set(ctx["x"]["edges"]) | set(ctx["g_edges"]) | set(sec_edges) | set(claim_edges)
    edges |= {(t, s, "TESTS") for t, s in ctx["trace"].get("edges", [])}
    for a, b, t in list(edges):
        for n in (a, b):
            if n not in kinds:
                kinds[n] = "section" if n.startswith(BOOK_PATH) else ("claim" if "claims.jsonl" in n else "receipt")
    graph = X.build_graph(list(kinds), kinds, edges)
    nodes = [node_of(s, ctx) for s in syms if s["kind"] in ("function", "method", "class", "gd_func", "scene_node") and not s["path"].startswith("tests/")]
    return dict(symbols=syms, anchors=anchors, nodes=nodes, graph=graph, claims=claims_rows, issues=issues, pb=pb, kinds=kinds)


def node_id(s):
    stem = os.path.splitext(os.path.basename(s["path"]))[0]
    base = (stem + "." + s["id"].split("::", 1)[1]).replace("#", "_")
    base = re.sub(r"[^A-Za-z0-9._:-]", "_", base)
    if len(base) > 32:
        base = base[:23] + "_" + L.sha256s(s["id"])[:8]
    return base


def node_of(s, ctx):
    card = s.get("card") or {}
    qual = s["id"].split("::", 1)[1]
    words = L.split_ident(qual).lower()
    doc = (ctx["texts"].get(s["id"]) or {}).get("doc", "")
    purpose = card.get("purpose") or (" ".join(doc.split())[:300]) or ("%s %s" % (s["kind"], words))
    a = s["id"] if L.blen(s["id"]) <= 64 else L.clip_bytes(s["id"], 55) + "~" + L.sha256s(s["id"])[:8]
    return {"schema": "ctx.node.v2", "id": node_id(s), "corpus": "hb", "kind": {"scene_node": "scene", "gd_func": "symbol"}.get(s["kind"], "symbol"),
            "title": L.clip_bytes(s["id"].split("::", 1)[1], 120), "purpose": L.clip_bytes(purpose, 600),
            "anchors": [{"a": a, "role": "scene" if s["kind"] == "scene_node" else "symbol"}],
            "search": {"triggers": sorted({"-".join(w.split()) for w in [words]})[:12], "keywords": sorted(set(words.split()))[:24],
                       "tasks": [L.clip_bytes(t, 200) for t in card.get("tasks", [])][:8],
                       "exclusions": [L.clip_bytes(t, 200) for t in card.get("not_for", [])][:12]},
            "status": "active", "source_sha256": s["sha256"]}


# ---- generated sections -------------------------------------------------------------------------------------
def render_gen(ctx, d):
    syms = d["symbols"]
    s8 = ["### Files\n\n| path | lines | bytes | sha256 |\n|---|---|---|---|\n"]
    for m in ctx["x"]["repo"].mods:
        s8.append("| `%s` | %s | %s | `%s` |\n" % (m.path, m.nlines if getattr(m, "source_known", True) else "AUSENTE", len(m.data) if getattr(m, "source_known", True) else "AUSENTE", getattr(m, "file_sha256", L.sha256b(m.data))[:16]))
    s8.append("\n### Symbols (tools)\n\n| id | kind | lines | signature | purpose | governs |\n|---|---|---|---|---|---|\n")
    for s in syms:
        if L.is_src(s["path"]) and s["kind"] in ("function", "method", "class", "constant"):
            g = s["governs"]
            gv = ", ".join(g["sections"] + g["inv"] + g["au"]) or "-"
            pur = (s.get("card") or {}).get("purpose", "-")
            s8.append("| `%s` | %s | %d-%d | `%s` | %s | %s |\n" % (s["id"], s["kind"], s["span"]["start_line"], s["span"]["end_line"],
                                                              s["sig"].replace("|", "/"), pur.replace("|", "/"), gv))
    s8.append("\n### Fixture symbols (Godot)\n\n| id | kind | lines |\n|---|---|---|\n")
    for s in syms:
        if s["path"].startswith("godot/"):
            s8.append("| `%s` | %s | %d-%d |\n" % (s["id"], s["kind"], s["span"]["start_line"], s["span"]["end_line"]))
    # s9: edges
    g = X.Graph(d["graph"])
    s9 = ["### Imports (module to module)\n\n| module | imports |\n|---|---|\n"]
    for m in ctx["x"]["repo"].mods:
        i = g.ix.get(m.path + "::__module__")
        tg = sorted({g.ids[j].split("::")[0] for j in g.neighbours("fwd", i, {"IMPORTS"}) if g.kind[j] == "module" or True}) if i is not None else []
        s9.append("| `%s` | %s |\n" % (m.path, ", ".join("`%s`" % t for t in tg if t != m.path) or "-"))
    s9.append("\n### Calls (tool symbols, resolved static edges)\n\n| symbol | calls |\n|---|---|\n")
    for sid in g.ids:
        i = g.ix[sid]
        if L.is_src(sid) and g.kind[i] in ("function", "method"):
            cs = sorted(g.ids[j].split("::", 1)[1] if g.ids[j].split("::")[0] == sid.split("::")[0] else g.ids[j] for j in g.neighbours("fwd", i, {"CALLS"}))
            if cs:
                s9.append("| `%s` | %s |\n" % (sid, ", ".join("`%s`" % c for c in cs)))
    s9.append("\n### Tests (runtime trace, most specific first)\n\n| symbol | tests |\n|---|---|\n")
    for s in syms:
        if L.is_src(s["path"]) and s["governs"].get("tests") and s["kind"] in ("function", "method", "class"):
            s9.append("| `%s` | %s |\n" % (s["id"], ", ".join("`%s`" % t.split("::")[1] for t in s["governs"]["tests"][:4])))
    # s10: provenance
    s10 = ["Source files at extraction time (sha256 prefix, bytes):\n\n| path | sha256 | bytes |\n|---|---|---|\n"]
    for m in ctx["x"]["repo"].mods:
        s10.append("| `%s` | `%s` | %s |\n" % (m.path, getattr(m, "file_sha256", L.sha256b(m.data))[:16], len(m.data) if getattr(m, "source_known", True) else "AUSENTE"))
    nt = len(ctx["trace"].get("edges", []))
    s10.append("\nExtractor `hb_extract2.py/2.0` (Python `ast`, rules 1-5 of the call resolver); runtime trace edges: %d; symbols: %d; graph nodes: %d. "
               "Tokeniser: router v2 `toks`; sizes are UTF-8 byte counts.\n" % (nt, len(syms), len(d["graph"]["ids"])))
    if ctx["x"]["bundle"]["lang_default"] != "python":
        s10[-1] = "\nSymbol bundle `%s` (language `%s`); runtime trace edges: %d; symbols: %d; graph nodes: %d.\n" % (SB.SCHEMA, ctx["x"]["bundle"]["lang_default"], nt, len(syms), len(d["graph"]["ids"]))
    return {8: "".join(s8), 9: "".join(s9), 10: "".join(s10)}


def render_book(book_text, gen):
    def sub(m):
        n = int(m.group(2)[len("hb-s"):])
        return m.group(1) + gen[n].rstrip("\n") + "\n" + m.group(4) if n in gen else m.group(0)
    return GEN_RE.sub(sub, book_text)


def tools_loc(root):
    return dict(subject=L.subject_of(root), godot=os.path.join(root, "godot_fixture", "project"),
                cards=os.path.join(root, L.BOOK_SUB, "cards.jsonl"), trace=os.path.join(root, "out", "trace.json"),
                book=os.path.join(root, L.BOOK_SUB, "book.md"), outdir=os.path.join(root, L.BOOK_SUB))


def build_all(root, write=True, symbols_bundle=None, hb_id=None):
    p = tools_loc(root)
    ctx = load_ctx(p["subject"], p["godot"], p["cards"], p["trace"], symbols_bundle, os.path.join(root, "out"))
    with open(p["book"], encoding="utf-8") as f:
        book = f.read()
    identity = SB.book_id(root, hb_id, config=ctx["x"]["bundle"] if symbols_bundle or os.environ.get("HBV2_SYMBOLS_BUNDLE") else None)
    d = derive(ctx, book)
    gen = render_gen(ctx, d)
    rendered = render_book(book, gen)
    d["rendered_book"] = rendered
    if write:
        o = p["outdir"]
        with open(p["book"], "w", encoding="utf-8", newline="\n") as f:
            f.write(rendered)
        L.write_jsonl(os.path.join(o, "symbols.jsonl"), d["symbols"])
        L.write_jsonl(os.path.join(o, "nodes.jsonl"), d["nodes"])
        L.write_jsonl(os.path.join(o, "anchors.jsonl"), d["anchors"])
        L.write_jsonl(os.path.join(o, "claims.jsonl"), d["claims"])
        L.write_json(os.path.join(o, "graph.json"), d["graph"])
        if not os.path.isfile(os.path.join(o, "aliases.json")):
            L.write_json(os.path.join(o, "aliases.json"), {"aliases": {}})
        man = {f: L.sha_file(os.path.join(o, f)) for f in ("book.md", "symbols.jsonl", "nodes.jsonl", "anchors.jsonl", "claims.jsonl", "graph.json", "aliases.json")}
        L.write_json(os.path.join(o, "manifest.json"), {"schema": "hb.manifest.v2", "hb_id": identity, "files": man})
    return ctx, d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--symbols-bundle", default=os.environ.get("HBV2_SYMBOLS_BUNDLE"))
    ap.add_argument("--hb-id", default=os.environ.get("HBV2_HB_ID"))
    ap.add_argument("--render-derived", action="store_true")
    a = ap.parse_args(argv)
    if not a.render_derived:
        return 2
    try:
        ctx, d = build_all(a.root, symbols_bundle=a.symbols_bundle, hb_id=a.hb_id)
    except BookError as e:
        sys.stderr.write(str(e) + "\n")
        return 3
    for i in d["issues"]:
        sys.stderr.write("%s %s\n" % (i["code"], i["detail"]))
    print(L.dumps({"symbols": len(d["symbols"]), "nodes": len(d["nodes"]), "edges": len(d["graph"]["fwd"]["col"]), "issues": len(d["issues"])}))
    return 3 if d["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
