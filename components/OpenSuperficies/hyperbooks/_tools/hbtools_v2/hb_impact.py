#!/usr/bin/env python3
"""HB2-04: impact closure, cutoff, ranking and edit context pack.

hb_impact.py --symbol path::q | --task "<text>" [--diff file] [--root HB] [--packs DIR]
Prints the `hb.answer.v2` IMPACT answer (<= 2,000 B) and writes the edit context pack `hb.impact-pack.v2` (<= 12,288 B) under
<DIR>/<task_q>.json (default out/packs). Closure = `rebuild_set` with unchanged-signature cutoff: a BODY edit stops at direct
tests/receipts/sections, a SIG edit dirties the callers to depth D_max, at most F_max = 64 nodes (never a silent cut).
"""
import argparse, os, re, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_query as Q  # noqa: E402
import hb_pack as P  # noqa: E402
import hb_index as I  # noqa: E402

F_MAX, K_T = 64, 3
SIG_WORDS = Q.SIG_WORDS


def impact_cfg(h):
    return dict(h.find, profile=h.impact["profile"], w_body=h.impact["w_body"])


def diff_changes_signature(diff_text, targets):
    """True iff a unified diff removes and adds a different `def <name>(` / `class <name>` line for one of the targets."""
    for sid in targets:
        name = sid.split("::", 1)[1].split(".")[-1]
        pat = re.compile(r"^[-+]\s*(?:async\s+)?(?:def|class)\s+%s\b(.*)$" % re.escape(name))
        old = [m.group(1) for l in diff_text.split("\n") if l.startswith("-") and (m := pat.match(l))]
        new = [m.group(1) for l in diff_text.split("\n") if l.startswith("+") and (m := pat.match(l))]
        if old and new and old != new:
            return True
    return False


def change_kind(text, diff, targets):
    if set(L.toks(text)) & SIG_WORDS:
        return "SIG"
    if diff and diff_changes_signature(diff, targets):
        return "SIG"
    return "BODY"


def sccs(nodes, succ):
    """Tarjan strongly connected components over `nodes` with successor function; returns list of components (lists)."""
    index, low, on, st, out, idx = {}, {}, set(), [], [], [0]

    def strong(v):
        index[v] = low[v] = idx[0]
        idx[0] += 1
        st.append(v)
        on.add(v)
        for w in succ(v):
            if w not in nodes:
                continue
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = st.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp))
    for v in sorted(nodes):
        if v not in index:
            strong(v)
    return out


def heights(R, g):
    """Reverse-DAG Kahn heights over the CALLS-induced subgraph of R with SCCs collapsed. Returns ({node: height}, cycles)."""
    succ = lambda v: g.neighbours("rev", v, {"CALLS"})        # dependants of v
    comps = sccs(set(R), succ)
    cid = {v: i for i, c in enumerate(comps) for v in c}
    cycles = [c for c in comps if len(c) > 1]
    adj = {i: set() for i in range(len(comps))}
    indeg = {i: 0 for i in range(len(comps))}
    for v in R:
        for w in succ(v):
            if w in cid and cid[w] != cid[v] and cid[w] not in adj[cid[v]]:
                adj[cid[v]].add(cid[w])
                indeg[cid[w]] += 1
    h, ready = {}, sorted(i for i in indeg if indeg[i] == 0)
    for i in ready:
        h[i] = 0
    while ready:
        i = ready.pop(0)
        for j in sorted(adj[i]):
            h[j] = max(h.get(j, 0), h[i] + 1)
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
    return {v: h[cid[v]] for v in R}, cycles


def impact_core(h, text, symbol=None, diff=None):
    g = h.graph()
    qterms = L.toks(text)
    k_e, d_max = h.impact["k_e"], h.impact["d_max"]
    ranker = []
    if symbol:
        if symbol not in g.ix:
            return None
        targets = [symbol]
    else:
        cfg = impact_cfg(h)
        S = h.ix.scores(qterms, cfg)
        order = h.ix.order(S)
        targets = [h.ix.ids[d] for d in order[:k_e]]
        ranker = [(h.ix.ids[d], S[d]) for d in order[:k_e]]
        if not targets:
            return None
    change = change_kind(text, diff, targets)
    T = [g.ix[t] for t in targets]
    Sset = set(T) if change == "BODY" else set()
    # rebuild_set: u in R iff u in T or exists dependency p in R with p not in S  (BFS over reverse CALLS edges, depth <= d_max)
    dist = {t: 0 for t in T}
    frontier = sorted(T)
    for depth in range(1, d_max + 1):
        nxt = []
        for p in frontier:
            if p in Sset:
                continue
            for u in g.neighbours("rev", p, {"CALLS"}):
                if u not in dist:
                    dist[u] = depth
                    nxt.append(u)
        frontier = sorted(nxt)
        if not frontier:
            break
    visited = len(dist)
    truncated = False
    if len(dist) > F_MAX:
        keep = sorted(dist, key=lambda v: (dist[v], g.ids[v]))[:F_MAX]
        dist = {v: dist[v] for v in keep}
        truncated = True
    R = set(dist)
    hts, cycles = heights(R, g)
    # callers for chg (SIG): depth-1 callers by (depth, -shared query terms, id)
    qs = set(qterms)
    callers = []
    if change == "SIG":
        callers = sorted((v for v in R if dist[v] == 1 and g.kind[v] in ("function", "method", "class")),
                         key=lambda v: (dist[v], -len(qs & set(L.toks(L.split_ident(g.ids[v].split("::", 1)[1])))), g.ids[v]))
    chg = list(dict.fromkeys(targets + [g.ids[v] for v in callers]))[:5]
    # tests: TESTS edges into targets; order (-name overlap, coverage asc, id), top k_t
    nameq = qs | set(L.toks(" ".join(L.split_ident(t.split("::", 1)[1]) for t in targets)))
    cov = h.symbols  # symbols carry governs.tests; coverage size from the graph out-degree of the test
    tests = {}
    for t in T:
        for u in g.neighbours("rev", t, {"TESTS"}):
            tests[u] = len(g.neighbours("fwd", u, {"TESTS"}))
    tl = sorted(tests, key=lambda u: (-len(set(L.toks(L.split_ident(g.ids[u].split("::", 1)[1]))) & nameq), tests[u], g.ids[u]))[:K_T]
    # receipts / claims / sections / scene nodes touching R, in height order
    order = sorted(R, key=lambda v: (hts[v], g.ids[v]))
    receipts, claims, sections, scene = [], [], [], []
    for v in order:
        for u in g.neighbours("rev", v, {"VERIFIES"}):
            kind = g.kind[u]
            (claims if kind == "claim" else receipts).append(g.ids[u])
        for u in g.neighbours("rev", v, {"GOVERNS"}):
            sections.append(g.ids[u])
        for u in g.neighbours("fwd", v, {"SCENE_BINDS"}) + g.neighbours("rev", v, {"SCENE_BINDS"}):
            scene.append(g.ids[u])
    core = dict(targets=targets, change=change, chg=chg, tests=[g.ids[u] for u in tl], closure=dict(d_max=d_max, dirty=len(R), visited=visited, truncated=truncated),
                R=[g.ids[v] for v in order], heights={g.ids[v]: hts[v] for v in R}, cycles=[[g.ids[v] for v in c] for c in cycles],
                receipts=sorted(dict.fromkeys(receipts)), claims=sorted(dict.fromkeys(claims)), sections=sorted(dict.fromkeys(sections)),
                scene=sorted(dict.fromkeys(scene)), callers=[g.ids[v] for v in callers], ranker=ranker, text=text)
    return core


def run_cmd(sid):
    f, name = sid.split("::", 1)
    return L.clip_bytes("python3 -m unittest discover -s tests -p %s -k %s" % (os.path.basename(f), name.split(".")[-1]), 200)


def claim_ids(h, core):
    out = []
    for c in core["claims"]:
        m = re.search(r"::(C_[A-Z0-9_]+)$", c)
        if m:
            out.append(m.group(1).replace("_", "-"))
    return sorted(dict.fromkeys(out))[:16]


def receipt_ids(h, core):
    out = []
    for c in core["receipts"]:
        m = re.search(r"::(R_[A-Za-z0-9_]+)$", c)
        if m:
            out.append(m.group(1).replace("_", "-"))
    return sorted(dict.fromkeys(out))[:16]


def build_pack(h, core):
    syms = h.symbols
    items = []
    score = dict(core["ranker"])

    def g_line(sid):
        s = syms[sid]
        return L.clip_bytes("%s|%s|%d" % (sid, s["sha256"][:8], score.get(sid, 0)), 110)

    def add(id_, a, b, g):
        items.append({"id": id_, "a": L.clip_bytes(a, 4096), "b": L.clip_bytes(b, 450) if b else g, "g": g})
    for sid in core["targets"]:
        add(sid, Q.slice_text(h, sid), Q.triptych(h, sid), g_line(sid))
    for sid in core["callers"]:
        s = syms[sid]
        add(sid, "%s | %s" % (s["sig"], (s.get("card") or {}).get("purpose", "")), s["sig"], g_line(sid))
    seen = set()
    for sid in core["targets"] + core["callers"]:
        g = syms[sid]["governs"]
        for iid in g["inv"] + g["au"]:
            if iid not in seen:
                seen.add(iid)
                add(iid, h.rule_text(iid) if hasattr(h, "rule_text") else iid, iid, iid)
    for cid in claim_ids(h, core):
        add(cid, "claim at risk " + cid, cid, cid)
    for t in core["tests"]:
        add(t, "%s -> %s" % (t, run_cmd(t)), t, t)
    for p in core["scene"][:8]:
        add(p, "scene node " + p, p, p)
    items = items[:32]
    pk = {"schema": "hb.impact-pack.v2", "task_q": L.sha16(core["text"].encode("utf-8")), "targets": core["targets"], "change": core["change"], "chg": core["chg"],
          "tests": [{"id": t, "cmd": run_cmd(t)} for t in core["tests"]], "closure": core["closure"]}
    if core["receipts"]:
        pk["receipts"] = receipt_ids(h, core)
    if core["claims"]:
        pk["claims"] = claim_ids(h, core)
    if core["scene"]:
        pk["scene"] = [L.clip_bytes(p, 160) for p in core["scene"][:16]]

    def render(rows):
        o = dict(pk)
        o["items"] = [{"id": r[0], "level": r[1], "text": r[2]} for r in rows]
        o["bytes"] = 0
        return o
    r = P.pack(items, L.PACK_C, L.PACK_H, render)
    out = render(r["rows"])
    for _ in range(3):
        n = L.ser_len(out)
        if out["bytes"] == n:
            break
        out["bytes"] = n
    assert L.ser_len(out) <= L.PACK_C
    return out


def answer_impact(h, q, symbol=None, diff=None, packs_dir=None):
    core = impact_core(h, q, symbol, diff)
    ab = {"flag": False, "reason": "none", "m": 0, "cov": 0, "oov": 0}
    if core is None:
        ab = {"flag": True, "reason": "low_term_match", "m": 0, "cov": 0, "oov": 0}
        return Q.envelope(h, q, "IMPACT", ab, [], {"impact": {"change": "BODY", "chg": [], "tests": []}})
    pk = build_pack(h, core)
    pdir = packs_dir or os.path.join(h.root, "out", "packs")
    os.makedirs(pdir, exist_ok=True)
    L.write_json(os.path.join(pdir, pk["task_q"] + ".json"), pk)
    imp = {"change": core["change"], "chg": core["chg"], "tests": core["tests"], "receipts": len(core["receipts"]) + len(core["claims"]),
           "pack": L.sha16(L.canon(pk))}
    score = dict(core["ranker"])
    S = {}
    ids = {sid: i for i, sid in enumerate(h.ix.ids)}
    rows_cand = [ids[s] for s in core["chg"] if s in ids]
    for s, v in score.items():
        if s in ids:
            S[ids[s]] = v
    ans, _ = Q.pack_rows(h, q, "IMPACT", ab, rows_cand, S, {"impact": imp})
    return ans


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--task", default="")
    ap.add_argument("--diff")
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--packs")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if not a.symbol and not a.task:
        return 2
    h = Q.Hyper(a.root)
    diff = open(a.diff, encoding="utf-8").read() if a.diff else None
    text = a.task or ("change " + a.symbol)
    ans = answer_impact(h, text, a.symbol, diff, a.packs)
    sys.stdout.write(L.dumps(ans) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
