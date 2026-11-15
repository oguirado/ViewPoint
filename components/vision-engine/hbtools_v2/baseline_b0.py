#!/usr/bin/env python3
"""B0 (D-5, H1): the v1 hyperbook toolchain as shipped, run on the router v2 tree with NO model-authored slots.

build:  copy v1_frozen/hbtools -> out/b0/hbtools, set HB_ID = "HB-ROUTER-002" (diff saved to out/b0/patch.diff); repo/ = subject tools;
        hb_extract -> hb_scaffold -> every [SLOT:...] filled with "-" (no authored text) -> hb_emit -> v0 build_index (v1_frozen/router_v0).
query:  v0 route.py over the emitted nodes; node `HB-ROUTER-002.<stem>.<symbol>` -> `tools/<stem>.py::<symbol>`; section/file/asset nodes
        are kept as non-gold rows. IMPACT: not applicable. ABSENT: the v0 abstain flag. CLAIM: v1 g_hb_receipt semantics (receipt file exists => supported).
One deviation from "HB_ID is the only change": hb_emit.ASSET_SUBJECT is retargeted to the router v2 stems (route -> route_v2, eval_route -> eval_v2);
the shipped emitter cannot run on this tree without it (KeyError 'route'). The diff is part of out/b0/patch.diff.
Usage: baseline_b0.py --build [--out out/b0] | --query TEXT --class FIND|ABSENT|IMPACT|CLAIM
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_claim as C  # noqa: E402

HBID = "HB-ROUTER-002"
HBD = "hyperbooks/" + HBID


def sh(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if p.returncode != 0:
        raise RuntimeError("%s -> rc %d: %s" % (" ".join(cmd[-3:]), p.returncode, (p.stderr or p.stdout)[-300:]))
    return p.stdout


def build(subject, out, v1=None):
    v1 = v1 or os.path.join(L.HB, "v1_frozen")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    src_h = os.path.join(v1, "hbtools")
    dst_h = os.path.join(out, "hbtools")
    shutil.copytree(src_h, dst_h, ignore=shutil.ignore_patterns("__pycache__"))
    edits = {"hblib.py": ('HB_ID = "HB-ROUTER-001"', 'HB_ID = "%s"' % HBID),
             "hb_emit.py": ('ASSET_SUBJECT = {"vocabulary": "route", "bench": "eval_route"}', 'ASSET_SUBJECT = {"vocabulary": "route_v2", "bench": "eval_v2"}')}
    diff = []
    for fn, (a, b) in edits.items():
        p = os.path.join(dst_h, fn)
        t = open(p, encoding="utf-8").read()
        assert t.count(a) == 1, fn
        n = t.replace(a, b)
        open(p, "w", encoding="utf-8", newline="\n").write(n)
        diff += list(difflib.unified_diff(t.splitlines(True), n.splitlines(True), "v1/hbtools/" + fn, "b0/hbtools/" + fn))
    with open(os.path.join(out, "patch.diff"), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(diff))
    for d in ("repo/tools", "standards", "eval"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    for f in sorted(os.listdir(os.path.join(subject, "tools"))):
        if f.endswith(".py"):
            shutil.copy2(os.path.join(subject, "tools", f), os.path.join(out, "repo", "tools", f))
    shutil.copy2(os.path.join(v1, "standards", "vocabulary.json"), os.path.join(out, "standards", "vocabulary.json"))
    open(os.path.join(out, "eval", "hb_bench.jsonl"), "w").write("{}\n")           # stub: the v1 emitter only stats this path
    py = sys.executable
    sh([py, "hbtools/hb_extract.py", "--repo", "repo", "--hb-id", HBID, "--out", HBD], out)
    sh([py, "hbtools/hb_scaffold.py", "--hb", HBD], out)
    bp = os.path.join(out, HBD, "book.md")
    t = open(bp, encoding="utf-8").read()
    t = re.sub(r'"slots":\[(?:"[^"]*"(?:,"[^"]*")*)?\]', '"slots":[]', t)
    t = re.sub(r"\[SLOT:[A-Za-z0-9_.-]+\]", "-", t)
    open(bp, "w", encoding="utf-8", newline="\n").write(t)
    open(os.path.join(out, HBD, "claims.jsonl"), "w").close()
    sh([py, "hbtools/hb_emit.py", "--hb", HBD], out)
    ixh = sh([py, os.path.join(v1, "router_v0", "build_index.py"), "--root", ".", "--out", "index"], out).strip()
    nodes = [json.loads(l) for l in open(os.path.join(out, "nodes.jsonl"), encoding="utf-8") if l.strip()]
    return {"index_sha256": ixh, "nodes": len(nodes), "symbol_nodes": sum(1 for n in nodes if n["kind"] == "section")}


def node_to_sid(nid, sids):
    parts = nid.split(".", 2)
    if len(parts) == 3 and parts[0] == HBID:
        sid = "tools/%s.py::%s" % (parts[1], parts[2])
        if sid in sids:
            return sid
    return "hb-node:" + nid


def route(out, text, k=20, v1=None):
    v1 = v1 or os.path.join(L.HB, "v1_frozen")
    p = subprocess.run([sys.executable, os.path.join(v1, "router_v0", "route.py"), "--q", text, "--root", ".", "--index", "index", "--k", str(k), "--budget", "2000"],
                       cwd=out, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if p.returncode != 0:
        raise RuntimeError("route rc %d %s" % (p.returncode, p.stderr[-200:]))
    return json.loads(p.stdout), len(p.stdout.encode("utf-8"))


def answer(out, subject, text, cls, sids):
    """Unified result: {system, class, rows:[ids], abstain, claim, na, bytes}."""
    if cls == "IMPACT":
        return {"system": "B0", "class": cls, "rows": [], "abstain": False, "claim": None, "na": True, "bytes": 0}
    if cls == "CLAIM":
        claim, ref = C.split_ref(text)
        ok = bool(ref) and os.path.isfile(os.path.join(subject, ref.split(":", 1)[0]))
        return {"system": "B0", "class": cls, "rows": [], "abstain": False, "claim": "supported" if ok else "unverifiable", "na": False, "bytes": 0}
    r, nb = route(out, text)
    rows = [node_to_sid(x["id"], sids) for x in r["results"]]
    return {"system": "B0", "class": cls, "rows": rows, "abstain": bool(r["abstain"]), "claim": None, "na": False, "bytes": nb}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--class", dest="cls", default="FIND")
    ap.add_argument("--out", default=os.path.join(L.HB, "out", "b0"))
    ap.add_argument("--subject", default=L.SUBJECT)
    a = ap.parse_args(argv)
    if a.build:
        r = build(a.subject, a.out)
        print(L.dumps(r))
        return 0
    sids = {s["id"] for s in L.read_jsonl(os.path.join(L.HB, L.BOOK_SUB, "symbols.jsonl"))}
    sys.stdout.write(L.dumps(answer(a.out, a.subject, a.query, a.cls, sids)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
