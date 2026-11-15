#!/usr/bin/env python3
"""D-4 gates. Every gate fails closed (missing input = FAIL); the runner exits 5 if any gate fails.

hb_gates2.py --all [--root HB] [--skip g_a,g_b] [--only g_a] [--out out/gates.json]
Each gate returns {"gate", "pass", "errors": [...], ...}. Gates that need a model run nothing: all are deterministic stdlib code
(Godot/xvfb are reached only through hb_visual.py).
"""
import argparse, ast, json, os, random, re, shutil, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_anchor2 as A  # noqa: E402
import hb_build as B  # noqa: E402
import hb_claim as C  # noqa: E402
import hb_pack as P  # noqa: E402
import schemalib  # noqa: E402

GATES = ["g_schema", "g_ids", "g_graph", "g_fresh", "g_anchor", "g_drift", "g_relocate", "g_backlink", "g_claim", "g_oracle", "g_budget", "g_subanchor",
         "g_merge", "g_visual", "g_rebuild", "g_determinism", "g_nofloat", "g_stdlib", "g_blind", "g_privacy", "g_freeze"]
SID = re.compile(r"^[A-Za-z0-9_./-]{1,160}::[A-Za-z_][A-Za-z0-9_.]{0,127}(#b[0-9]{1,3})?$")
_CACHE = {}
# modules imported only inside a copy of the subject tree (the oracle witness runs there): not part of the hyperbook core
TREE_LOCAL = {"oracle_witness.py": {"repo_gen", "build_index_v2", "route_v2"}}


def res(name, errs, **kw):
    d = {"gate": name, "pass": not errs, "errors": [str(e) for e in errs[:20]]}
    d.update(kw)
    return d


def paths(root):
    return dict(subject=L.subject_of(root), godot=os.path.join(root, "godot_fixture", "project"), book=os.path.join(root, L.BOOK_SUB),
                out=os.path.join(root, "out"), params=os.path.join(root, "params.json"))


def need(*ps):
    return [("missing input %s" % os.path.basename(p)) for p in ps if not os.path.exists(p)]


def params_of(root):
    p = os.path.join(root, "params.json")
    return L.jload(p) if os.path.isfile(p) else {}


def dev_rows(root):
    rows = []
    for p in (os.path.join(os.path.dirname(root), "bench", "hb_dev.jsonl"), os.path.join(root, "eval", "dev_aug.jsonl")):
        if os.path.isfile(p):
            rows += [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    return rows


# ---- structure gates ---------------------------------------------------------------------------------------
def g_schema(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "symbols.jsonl"), os.path.join(p["book"], "book.md"))
    if errs:
        return res("g_schema", errs)
    n = 0
    for name, sid in (("symbols.jsonl", "hb.symbol.v2"), ("anchors.jsonl", "hb.anchor.v2")):
        for r in L.read_jsonl(os.path.join(p["book"], name)):
            n += 1
            e = schemalib.validate(r, sid)
            if e:
                errs.append("%s %s: %s" % (name, r.get("id"), e[0]))
    pb = B.parse_book(open(os.path.join(p["book"], "book.md"), encoding="utf-8").read())
    for k, sid in (("sections", "hb.section.v2"), ("inv", "hb.inv.v2"), ("claims", "hb.claim.v2")):
        for o in pb[k]:
            n += 1
            e = schemalib.validate(o, sid)
            if e:
                errs.append("book %s: %s" % (sid, e[0]))
    lite = L.load_mod(p["subject"], "tools/jsonschema_lite.py")
    ns = L.jload(os.path.join(p["subject"], "standards", "schemas", "ctx.node.v2.schema.json"))
    for r in L.read_jsonl(os.path.join(p["book"], "nodes.jsonl")):
        n += 1
        e = lite.validate(r, ns)
        if e:
            errs.append("node %s: %s" % (r["id"], e[0]))
    packs = os.path.join(p["out"], "packs")
    if os.path.isdir(packs):
        for f in sorted(os.listdir(packs)):
            n += 1
            e = schemalib.validate(L.jload(os.path.join(packs, f)), "hb.impact-pack.v2")
            if e:
                errs.append("pack %s: %s" % (f, e[0]))
    mf = os.path.join(root, "fixtures", "mutants_oracle.json")
    if os.path.isfile(mf):
        for m in L.jload(mf)["mutants"]:
            n += 1
            e = schemalib.validate(m, "hb.mutant.v2")
            if e:
                errs.append("mutant %s: %s" % (m["id"], e[0]))
    for f in sorted(os.listdir(p["out"])):
        fp = os.path.join(p["out"], f)
        if f.endswith(".json") and os.path.isfile(fp):
            try:
                d = L.jload(fp)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("schema") == "hb.receipt.v2":
                n += 1
                e = schemalib.validate({k: d[k] for k in ("schema", "kind", "tool", "metrics", "inputs", "nonce") if k in d}, "hb.receipt.v2")
                if e:
                    errs.append("receipt %s: %s" % (f, e[0]))
    vp = os.path.join(p["out"], "visual_DEV.json")
    if os.path.isfile(vp):
        for r in L.jload(vp)["clean"]["receipts"]:
            n += 1
            e = schemalib.validate(r, "hb.visual-receipt.v2")
            if e:
                errs.append("visual receipt: %s" % e[0])
    return res("g_schema", errs, checked=n)


def g_ids(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "symbols.jsonl"), os.path.join(p["book"], "graph.json"))
    if errs:
        return res("g_ids", errs)
    ids = [r["id"] for r in L.read_jsonl(os.path.join(p["book"], "symbols.jsonl"))]
    if len(ids) != len(set(ids)):
        errs.append("duplicate symbol ids")
    errs += ["bad id shape %s" % i for i in ids if not SID.match(i)][:5]
    g = L.jload(os.path.join(p["book"], "graph.json"))
    if g["ids"] != sorted(g["ids"], key=lambda s: s.encode("utf-8")):
        errs.append("graph ids not bytewise sorted")
    if len(g["ids"]) != len(set(g["ids"])):
        errs.append("graph ids not unique")
    missing = set(ids) - set(g["ids"])
    if missing:
        errs.append("symbols absent from graph: %d" % len(missing))
    return res("g_ids", errs, symbols=len(ids))


def g_graph(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "graph.json"), os.path.join(p["out"], "trace.json"))
    if errs:
        return res("g_graph", errs)
    g = L.jload(os.path.join(p["book"], "graph.json"))
    n = len(g["ids"])
    for d in ("fwd", "rev"):
        c = g[d]
        if len(c["row_ptr"]) != n + 1 or c["row_ptr"][-1] != len(c["col"]) or len(c["etype"]) != len(c["col"]):
            errs.append("%s CSR shape" % d)
        if any(not (0 <= x < n) for x in c["col"]):
            errs.append("%s edge endpoint out of range" % d)
        if any(not (0 <= x < len(g["etypes"])) for x in c["etype"]):
            errs.append("%s edge type out of range" % d)
    if len(g["fwd"]["col"]) != len(g["rev"]["col"]):
        errs.append("forward/reverse edge counts differ")
    if not errs:
        gr = X.Graph(g)
        fw = {(i, j, t) for i in range(n) for j, t in gr.row("fwd", i)}
        rv = {(j, i, t) for i in range(n) for j, t in gr.row("rev", i)}
        if fw != rv:
            errs.append("forward and reverse CSR disagree")
    stored = L.jload(os.path.join(p["out"], "trace.json"))
    d = tempfile.mkdtemp(prefix="gg")
    try:
        r = subprocess.run([sys.executable, os.path.join(L.HERE, "hb_trace.py"), "--subject", p["subject"], "--out", os.path.join(d, "t.json")],
                           capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode != 0:
            errs.append("trace run failed")
        elif L.jload(os.path.join(d, "t.json"))["edges"] != stored["edges"]:
            errs.append("TESTS edges differ between trace runs")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        L.clean_pycache(p["subject"])
    return res("g_graph", errs, nodes=n, edges=len(g["fwd"]["col"]))


def g_fresh(root):
    p = paths(root)
    errs = need(os.path.join(p["out"], "subject.sha256.json"), os.path.join(p["book"], "index", "ID"), os.path.join(p["book"], "manifest.json"))
    if errs:
        return res("g_fresh", errs)
    import freeze_inputs
    now = freeze_inputs.walk(p["subject"])
    old = L.jload(os.path.join(p["out"], "subject.sha256.json"))
    bad = sorted(k for k in set(now) | set(old) if now.get(k) != old.get(k))
    if bad:
        errs.append("subject changed since T00: %s" % ",".join(bad[:5]))
    ix = os.path.join(p["book"], "index")
    files = {f: L.readb(os.path.join(ix, f)) for f in ("cols.json", "post.json", "vocab.json") if os.path.isfile(os.path.join(ix, f))}
    ixid = L.sha256b(b"".join(files[k] for k in sorted(files)))
    if len(files) != 3 or ixid != open(os.path.join(ix, "ID")).read().strip():
        errs.append("index does not match its ID")
    man = L.jload(os.path.join(p["book"], "manifest.json"))["files"]
    for f, h in man.items():
        fp = os.path.join(p["book"], f)
        if not os.path.isfile(fp) or L.sha_file(fp) != h:
            errs.append("book artefact stale: " + f)
    tr = L.jload(os.path.join(p["out"], "trace.json"))
    ex = L.jload(os.path.join(p["out"], "extract.json")) if os.path.isfile(os.path.join(p["out"], "extract.json")) else None
    if ex is None:
        errs.append("missing input extract.json")
    return res("g_fresh", errs)


def g_anchor(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "anchors.jsonl"))
    if errs:
        return res("g_anchor", errs)
    anchors = [a for a in L.read_jsonl(os.path.join(p["book"], "anchors.jsonl")) if a["path"].startswith(("tools/", "tests/")) and a["kind"] != "subanchor"]
    out = A.classify(anchors, X.Repo(p["subject"]), {}, None)
    not_same = [r["id"] + ":" + r["class"] for r in out if r["class"] != "SAME"]
    errs += not_same[:10]
    gd = os.path.join(p["godot"])
    if os.path.isdir(gd):
        a1 = A.asset_anchors(gd)
        errs += [r["id"] for r in A.classify_assets(a1, A.asset_anchors(gd)) if r["class"] != "SAME"]
    return res("g_anchor", errs, anchors=len(anchors), same=len(out) - len(not_same))


def drift_run(root):
    if "drift" not in _CACHE:
        import hb_drift2 as D
        p = paths(root)
        tau = params_of(root).get("anchors", {}).get("tau_rel_ppm", A.TAU_REL)
        _CACHE["drift"] = D.run(p["subject"], p["godot"], tau)
    return _CACHE["drift"]


def g_drift(root):
    errs = need(paths(root)["subject"])
    if errs:
        return res("g_drift", errs)
    r = drift_run(root)
    s = r["summary"]
    errs += ["label mismatch %s expected %s observed %s" % (x["mutant"], x.get("expected"), x.get("observed")) for x in r["rows"] if not x.get("correct")]
    if s["false_positives"]:
        errs.append("false positives %d" % s["false_positives"])
    if s["v1_targets_correct"] < 10:
        errs.append("v1 targets %d/10" % s["v1_targets_correct"])
    return res("g_drift", errs, correct=s["all_correct"], n=s["all_n"], v1=s["v1_targets_correct"], false_positives=s["false_positives"])


def g_relocate(root):
    errs = need(paths(root)["subject"])
    if errs:
        return res("g_relocate", errs)
    r = drift_run(root)
    rows = {x["mutant"]: x for x in r["rows"]}
    if r["summary"]["false_rebinds"]:
        errs.append("false rebinds %d" % r["summary"]["false_rebinds"])
    if rows["N6"]["observed"] != "E_AMBIG":
        errs.append("duplicate helper not E_AMBIG")
    for mid in ("RN1", "RN2", "RN3", "ED1", "ED2", "ED3", "N3", "N5"):
        if rows[mid]["observed"] in ("SHIFT", "FORMAT", "SAME"):
            errs.append("%s auto-class" % mid)
    auto, manual = A.repair_plan([{"id": "x", "class": c} for c in ("RENAME", "EDIT", "MOVE+EDIT", "DOC")])
    if auto:
        errs.append("repair_plan auto-applies a manual class")
    return res("g_relocate", errs, false_rebinds=r["summary"]["false_rebinds"])


def g_backlink(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "book.md"))
    if errs:
        return res("g_backlink", errs)
    try:
        ctx, d = B.build_all(root, write=False)
    except B.BookError as e:
        return res("g_backlink", [str(e)])
    errs += ["%s %s" % (i["code"], i["detail"]) for i in d["issues"]]
    rank1 = [s for s in d["symbols"] if s["path"].startswith("tools/") and s["kind"] in ("function", "method", "class") and B.is_public(s["id"])]
    no_gov = [s["id"] for s in rank1 if not s["governs"]["sections"]]
    errs += ["no section: " + i for i in no_gov[:5]]
    return res("g_backlink", errs, public_symbols=len(rank1))


def g_claim(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "book.md"))
    if errs:
        return res("g_claim", errs)
    text = open(os.path.join(p["book"], "book.md"), encoding="utf-8").read()
    pb = B.parse_book(text)
    f = C.g_claim_book(text, p["subject"], pb["claims"])
    errs += ["%s %s" % (x["code"], x["claim"]) for x in f]
    # planted fixtures must FAIL (a dead gate would let them through)
    cl = os.path.join(root, "fixtures", "claims")
    planted = []
    if os.path.isdir(cl):
        planted.append(C.check_prose("Cold latency is 50 ms", "lat_receipt.json:cold.p50", cl))
    for q in dev_rows(root):
        if q["qid"] in ("HB-C09", "HB-C12"):
            planted.append(C.check_prose(q["text"], q["receipt"], p["subject"]))
    if len(planted) < 3 or any(x is None or x["code"] != "E_REFUTED_CLAIM" for x in planted):
        errs.append("planted contradicted claims not all caught (%d)" % len(planted))
    return res("g_claim", errs, claims=len(pb["claims"]), planted_caught=len(planted))


def g_oracle(root):
    p = paths(root)
    f = os.path.join(p["out"], "oracle_kill.json")
    errs = need(f)
    if errs:
        return res("g_oracle", errs)
    r = L.jload(f)
    s = r["summary"]
    if s["k"] != s["p"] or s["p"] < 10:
        errs.append("kill %d/%d" % (s["k"], s["p"]))
    if s["unexercised"] or s["weak"] or s["clean_failures"]:
        errs.append("unexercised %d weak %d clean_failures %d" % (s["unexercised"], s["weak"], s["clean_failures"]))
    man = os.path.join(root, "fixtures", "mutants_oracle.json")
    if r["inputs"].get("manifest_sha256") != L.sha_file(man):
        errs.append("oracle receipt is stale (manifest)")
    if r["inputs"].get("subject_sha256") != L.sha_file(os.path.join(p["out"], "subject.sha256.json")):
        errs.append("oracle receipt is stale (subject)")
    for x in r["rows"]:
        if not x["clean"]["nonce"] or x["clean"]["rc"] != 0:
            errs.append("clean run without nonce: " + x["id"])
    return res("g_oracle", errs, k=s["k"], p=s["p"], baseline_k=(r.get("baseline_subject_suite") or {}).get("summary", {}).get("k"))


def g_budget(root):
    import hb_query as Q, hb_impact as H
    p = paths(root)
    errs = need(os.path.join(p["book"], "index", "ID"), p["params"])
    if errs:
        return res("g_budget", errs)
    h = Q.Hyper(root)
    d = tempfile.mkdtemp(prefix="gb")
    n = over = refused = 0
    max_pack = 0
    try:
        for r in dev_rows(root):
            txt = r["text"] + ((" " + r["receipt"]) if r.get("receipt") else "")
            cls = {"FIND": "FIND", "ABSENT": "FIND", "IMPACT": "IMPACT", "CLAIM": "CLAIM"}[r["class"]]
            a = H.answer_impact(h, txt, packs_dir=d) if cls == "IMPACT" else Q.answer(h, txt, cls)
            n += 1
            if len(L.canon(a)) > L.ANSWER_C:
                over += 1
                errs.append("answer %s %d B" % (r["qid"], len(L.canon(a))))
            if a.get("overflow") and not a["r"]:
                refused += 1
        for f in os.listdir(d):
            sz = len(L.canon(L.jload(os.path.join(d, f))))
            max_pack = max(max_pack, sz)
            if sz > L.PACK_C:
                errs.append("pack %s %d B" % (f, sz))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return res("g_budget", errs, answers=n, over=over, refused=refused, max_pack=max_pack)


def g_subanchor(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "symbols.jsonl"))
    if errs:
        return res("g_subanchor", errs)
    syms = {s["id"]: s for s in L.read_jsonl(os.path.join(p["book"], "symbols.jsonl"))}
    n = 0
    for s in syms.values():
        if s.get("children"):
            n += 1
            data = L.readb(os.path.join(p["subject"], s["path"]))
            kids = [syms[c] for c in s["children"]]
            pieces = b"".join(data[k["span"]["start_byte"]:k["span"]["end_byte"]] for k in kids)
            hdr = data[s["span"]["start_byte"]:kids[0]["span"]["start_byte"]]
            if L.sha256b(hdr + pieces) != s["sha256"]:
                errs.append("children of %s do not concatenate" % s["id"])
            if any(k["bytes"] > L.SYM_MAX for k in kids):
                errs.append("child over limit in " + s["id"])
            if L.blen(s.get("skeleton", "")) > L.SKEL_MAX:
                errs.append("skeleton over limit " + s["id"])
        elif s["path"].startswith("tools/") and s["kind"] in ("function", "method", "class") and s["bytes"] > L.SYM_MAX:
            errs.append("symbol over 1,800 B without children: " + s["id"])
    return res("g_subanchor", errs, parents=n)


def g_merge(root):
    f = os.path.join(paths(root)["out"], "merge.json")
    errs = need(f)
    if errs:
        return res("g_merge", errs)
    r = L.jload(f)
    if r.get("orders") != 24 or not r.get("all_equal"):
        errs.append("derived files differ across merge orders")
    if r.get("sources_unique") != 1:
        errs.append("source trees differ across orders")
    n = r.get("negative", {})
    if not (n.get("E_OVERLAP") and n.get("E_PIN") and n.get("merge_rc") != 0):
        errs.append("negative branch not rejected with E_OVERLAP and E_PIN")
    return res("g_merge", errs, orders=r.get("orders"))


def g_visual(root):
    f = os.path.join(paths(root)["out"], "visual_DEV.json")
    errs = need(f)
    if errs:
        return res("g_visual", errs)
    r = L.jload(f)
    m = r["metrics"]
    if not m["clean_ok"] or m["clean_false_positives"]:
        errs.append("clean reruns not identical")
    if m["tier1_detected"] * 10 < m["mutants"] * 9:
        errs.append("tier-1 detection %d/%d < 90%%" % (m["tier1_detected"], m["mutants"]))
    if m["caught_by_expected_layer"] != m["mutants"]:
        errs.append("a mutant escaped its expected layer")
    for x in r["clean"]["receipts"]:
        if x["verdict"] != "PASS":
            errs.append("clean run verdict " + x["verdict"])
    tm = os.path.join(paths(root)["out"], "visual_TEST_manifest.sha256")
    mp = os.path.join(root, "godot_fixture", "mutants", "TEST", "manifest.json")
    if not os.path.isfile(tm) or not os.path.isfile(mp) or open(tm).read().split()[0] != L.sha_file(mp):
        errs.append("sealed TEST mutant manifest missing or changed")
    return res("g_visual", errs, detected=m["tier1_detected"], mutants=m["mutants"])


_REBUILD_PY = """
import os, random, sys
seed = int(sys.argv[3])
_l = os.listdir
os.listdir = lambda p='.': random.Random(seed).sample(_l(p), len(_l(p)))
sys.path.insert(0, sys.argv[1])
import hb_build, hb_index
root = sys.argv[2]
hb_build.build_all(root)
hb_index.build_index(root)
"""
REBUILT = ["book.md", "symbols.jsonl", "nodes.jsonl", "anchors.jsonl", "claims.jsonl", "graph.json", "manifest.json", "index/cols.json", "index/post.json",
           "index/vocab.json", "index/ID"]


def stage_root(root, dst):
    for sub in ("tools", "tests"):
        os.makedirs(os.path.join(dst, "subject", sub))
        for f in sorted(os.listdir(os.path.join(root, "subject", sub))):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(root, "subject", sub, f), os.path.join(dst, "subject", sub, f))
    shutil.copytree(os.path.join(root, "godot_fixture", "project"), os.path.join(dst, "godot_fixture", "project"))
    os.makedirs(os.path.join(dst, "out"))
    shutil.copy2(os.path.join(root, "out", "trace.json"), os.path.join(dst, "out", "trace.json"))
    shutil.copytree(os.path.join(root, L.BOOK_SUB), os.path.join(dst, L.BOOK_SUB), ignore=shutil.ignore_patterns("seed", "index"))
    for f in ("params.json",):
        if os.path.isfile(os.path.join(root, f)):
            shutil.copy2(os.path.join(root, f), os.path.join(dst, f))


def g_rebuild(root):
    """Build twice from scratch under different cwd, TZ, PYTHONHASHSEED and shuffled listdir; compare with the committed artefacts."""
    p = paths(root)
    errs = need(os.path.join(p["book"], "book.md"), os.path.join(p["book"], "index", "ID"), os.path.join(p["out"], "trace.json"))
    if errs:
        return res("g_rebuild", errs)
    base = tempfile.mkdtemp(prefix="g_rb")
    try:
        for k, (tz, seed) in enumerate((("Pacific/Kiritimati", 11), ("America/Los_Angeles", 12))):
            dst = os.path.join(base, "r%d" % k)
            stage_root(root, dst)
            cwd = os.path.join(base, "cwd%d" % k)
            os.makedirs(cwd)
            env = dict(os.environ, TZ=tz, PYTHONHASHSEED=str(seed), PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, "-c", _REBUILD_PY, L.HERE, dst, str(seed)], cwd=cwd, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                errs.append("build rc %d %s" % (r.returncode, r.stderr[-200:]))
                continue
            for f in REBUILT:
                a, b = os.path.join(p["book"], f), os.path.join(dst, L.BOOK_SUB, f)
                if not os.path.isfile(a) or not os.path.isfile(b) or L.readb(a) != L.readb(b):
                    errs.append("run %d: %s differs from the committed artefact" % (k, f))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return res("g_rebuild", errs)


def g_determinism(root):
    p = paths(root)
    errs = need(os.path.join(p["book"], "index", "ID"))
    if errs:
        return res("g_determinism", errs)
    n = 0
    for r in dev_rows(root)[:17]:
        txt = r["text"] + ((" " + r["receipt"]) if r.get("receipt") else "")
        cls = {"FIND": "FIND", "ABSENT": "FIND", "IMPACT": "IMPACT", "CLAIM": "CLAIM"}[r["class"]]
        outs = []
        for _ in range(2):
            x = subprocess.run([sys.executable, os.path.join(L.HERE, "hb_query.py"), "--class", cls, "--root", root, txt], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED=str(random.randint(0, 999))))
            outs.append(x.stdout)
        n += 1
        if outs[0] != outs[1] or not outs[0]:
            errs.append("non-deterministic answer for " + r["qid"])
    return res("g_determinism", errs, queries=n)


def float_hits(path):
    def bad(x):
        raise ValueError("float " + x)
    try:
        txt = open(path, encoding="utf-8").read()
        if path.endswith(".jsonl"):
            for l in txt.split("\n"):
                if l.strip():
                    json.loads(l, parse_float=bad, parse_constant=bad)
        else:
            json.loads(txt, parse_float=bad, parse_constant=bad)
    except ValueError as e:
        return str(e)
    return None


def g_nofloat(root):
    p = paths(root)
    errs = need(p["book"])
    if errs:
        return res("g_nofloat", errs)
    n = 0
    targets = []
    for d in (p["book"], os.path.join(p["book"], "index"), os.path.join(p["out"], "packs")):
        if os.path.isdir(d):
            targets += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith((".json", ".jsonl"))]
    targets += [p["params"], os.path.join(p["out"], "run_DEV.json"), os.path.join(p["out"], "oracle_kill.json"), os.path.join(p["out"], "drift_DEV.json"),
                os.path.join(p["out"], "visual_DEV.json"), os.path.join(p["out"], "merge.json"), os.path.join(p["out"], "census.json"), os.path.join(p["out"], "trace.json")]
    for t in targets:
        if os.path.isfile(t):
            n += 1
            e = float_hits(t)
            if e:
                errs.append("%s: %s" % (os.path.relpath(t, root), e))
    import hb_query as Q
    h = Q.Hyper(root)
    for r in dev_rows(root)[:20]:
        if r["class"] in ("FIND", "ABSENT"):
            a = Q.answer(h, r["text"], "FIND")
            if not L.is_float_free(a):
                errs.append("float in answer " + r["qid"])
    return res("g_nofloat", errs, files=n)


def g_stdlib(root):
    std = set(sys.stdlib_module_names)
    local = {f[:-3] for f in os.listdir(L.HERE) if f.endswith(".py")}
    errs, n = [], 0
    for f in sorted(os.listdir(L.HERE)):
        if not f.endswith(".py"):
            continue
        n += 1
        tree = ast.parse(open(os.path.join(L.HERE, f), encoding="utf-8").read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [(node.module or "").split(".")[0]]
            for m in mods:
                if m and m not in std and m not in local and not (f in TREE_LOCAL and m in TREE_LOCAL[f]):
                    errs.append("%s imports non-stdlib %s" % (f, m))
        src = open(os.path.join(L.HERE, f), encoding="utf-8").read()
        if f not in ("hb_visual.py", "hb_gates2.py") and re.search(r"xvfb-run|\.local/bin/godot|HB_GODOT", src):
            errs.append("%s reaches Godot/xvfb outside hb_visual.py" % f)
    return res("g_stdlib", errs, modules=n)


def g_blind(root):
    p = paths(root)
    errs = need(p["params"])
    if errs:
        return res("g_blind", errs)
    d = tempfile.mkdtemp(prefix="gbl")
    try:
        row = '{"qid":"X","split":"DEV","class":"FIND","gold":[],"text":"t"}\n'
        trow = '{"qid":"X","split":"TEST","class":"FIND","gold":[],"text":"t"}\n'
        cases = [("hb_test_x.jsonl", row), ("hb_bench_x.jsonl", row), (os.path.join("P1", "bench", "dev.jsonl"), row), ("dev_with_test_rows.jsonl", trow)]
        for name, content in cases:
            fp = os.path.join(d, name)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            open(fp, "w").write(content)
            r = subprocess.run([sys.executable, os.path.join(L.HERE, "hb_tune.py"), "--dev", fp, "--out", os.path.join(d, "p.json"), "--report", os.path.join(d, "r.json"),
                                "--root", root, "--skip-drift"], capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            if r.returncode != 4:
                errs.append("tuner did not refuse %s (rc %d)" % (name, r.returncode))
        r = subprocess.run([sys.executable, os.path.join(L.HERE, "hb_eval.py"), "--split", "TEST", "--systems", "B1"], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode != 4:
            errs.append("hb_eval opened TEST without --lock")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dev = os.path.join(os.path.dirname(root), "bench", "hb_dev.jsonl")
    pr = L.jload(p["params"])
    if not os.path.isfile(dev) or pr.get("dev_sha256") != L.sha_file(dev):
        errs.append("params.dev_sha256 != sha256(DEV file)")
    da = os.path.join(root, "eval", "dev_aug.jsonl")
    if os.path.isfile(da) and pr.get("dev_aug_sha256") != L.sha_file(da):
        errs.append("params.dev_aug_sha256 != sha256(DEV-AUG file)")
    if any(r.get("split") == "TEST" for r in dev_rows(root)):
        errs.append("TEST rows among DEV rows")
    return res("g_blind", errs)


def g_privacy(root):
    """The v1 scan (host paths, credentials, deny-listed tokens by digest, URLs) over the publication text: book, cards, answers, fixture."""
    p = paths(root)
    errs = need(os.path.join(p["book"], "book.md"))
    if errs:
        return res("g_privacy", errs)
    v1 = L.load_mod(os.path.join(L.HB, "v1_frozen", "hbtools"), "hb_gates.py", "v1_hb_gates")
    stage = tempfile.mkdtemp(prefix="gpv")
    try:
        for f in ("book.md", "cards.jsonl", "symbols.jsonl", "nodes.jsonl", "claims.jsonl", "anchors.jsonl"):
            os.makedirs(os.path.join(stage, L.BOOK_SUB), exist_ok=True)
            shutil.copy2(os.path.join(p["book"], f), os.path.join(stage, L.BOOK_SUB, f))
        shutil.copytree(p["godot"], os.path.join(stage, "godot_fixture"))
        import hb_query as Q
        h = Q.Hyper(root)
        os.makedirs(os.path.join(stage, "answers"))
        for i, r in enumerate(dev_rows(root)[:30]):
            if r["class"] in ("FIND", "ABSENT"):
                a = Q.answer(h, r["text"], "FIND")
                open(os.path.join(stage, "answers", "a%d.txt" % i), "w", encoding="utf-8").write("\n".join(str(x[3]) for x in a["r"] if len(x) > 3) + "\n" + r["text"])
        n, hits = v1.public_hits(stage, skip=())
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return res("g_privacy", hits, scanned=n)


def g_freeze(root):
    import hb_freeze
    ok, why = hb_freeze.verify(os.path.join(root, "out", "freeze.json"), root)
    return res("g_freeze", [] if ok else [why])


def run_all(root, skip=(), only=None):
    out = []
    for g in GATES:
        if only and g != only:
            continue
        if g in skip:
            continue
        try:
            r = globals()[g](root)
        except Exception as e:
            r = res(g, ["gate crashed: %s: %s" % (type(e).__name__, str(e)[:160])])
        out.append(r)
        sys.stderr.write("%s %s\n" % (g, "PASS" if r["pass"] else "FAIL " + "; ".join(r["errors"][:2])))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--skip", default="")
    ap.add_argument("--only")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if not (a.all or a.only):
        return 2
    rs = run_all(a.root, [x for x in a.skip.split(",") if x], a.only)
    npass = sum(1 for r in rs if r["pass"])
    doc = {"schema": "hb.receipt.v2", "kind": "gate", "tool": "hb_gates2.py/2.0", "metrics": {"gates": len(rs), "passed": npass, "failed": len(rs) - npass},
           "gates": rs, "inputs": {}}
    out = a.out or os.path.join(a.root, "out", "gates.json")
    L.write_json(out, doc)
    print(L.dumps(doc["metrics"]))
    return 0 if npass == len(rs) else 5


if __name__ == "__main__":
    sys.exit(main())
