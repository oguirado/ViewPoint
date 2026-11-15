#!/usr/bin/env python3
"""HB2-06: anchor classification cascade (1-7), aliases/tombstones, relocation proposals, asset anchors by uid.

classify(old_anchors, new_repo, aliases, old_repo) -> one result per baseline anchor. Classification never repairs by itself;
`repair_plan` lists what may be applied automatically (SHIFT, FORMAT, byte-identical MOVE).
"""
import ast, os, re, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402

TAU_REL, MARGIN, MIN_TOK = 250000, 100000, 16
AUTO = ("SHIFT", "FORMAT", "MOVE")
BAD = ("EDIT", "SIG", "DELETE", "RENAME", "MOVE+EDIT", "E_AMBIG", "E_DANGLING", "E_TOMBSTONE_REUSE", "DOC")
RELOC_KINDS = ("function", "method", "class")


class AliasError(Exception):
    """Alias cycle or malformed alias map (rc 1)."""


# ---- indel distance -----------------------------------------------------------------------------------
def lcs_len(a, b):
    """Bit-parallel LCS length (Hyyro/Crochemore) of two token sequences."""
    if not a or not b:
        return 0
    masks = {}
    for i, t in enumerate(b):
        masks[t] = masks.get(t, 0) | (1 << i)
    full = (1 << len(b)) - 1
    V = full
    for t in a:
        M = masks.get(t, 0)
        U = V & M
        V = ((V + U) | (V - U)) & full
    return len(b) - bin(V).count("1")


def indel(a, b):
    """D = |a| + |b| - 2 LCS (ED <= D <= 2 ED)."""
    return len(a) + len(b) - 2 * lcs_len(a, b)


def hist_l1(a, b):
    ha, hb = {}, {}
    for t in a:
        ha[t] = ha.get(t, 0) + 1
    for t in b:
        hb[t] = hb.get(t, 0) + 1
    return sum(abs(ha.get(t, 0) - hb.get(t, 0)) for t in set(ha) | set(hb))


def norm_tokens(node):
    """Normalised AST token sequence: preorder, node types + identifiers/constants, root name and docstring excluded."""
    n = X.strip_doc(node)
    out = []

    def walk(x, root=False):
        out.append(type(x).__name__)
        if isinstance(x, ast.Name):
            out.append("N:" + x.id)
        elif isinstance(x, ast.Attribute):
            out.append("A:" + x.attr)
        elif isinstance(x, ast.Constant):
            out.append("C:" + repr(x.value)[:40])
        elif isinstance(x, ast.arg):
            out.append("a:" + x.arg)
        elif isinstance(x, ast.keyword) and x.arg:
            out.append("k:" + x.arg)
        for c in ast.iter_child_nodes(x):
            if isinstance(c, (ast.Load, ast.Store, ast.Del)):
                continue
            walk(c)
    walk(n, True)
    return out


# ---- aliases ------------------------------------------------------------------------------------------
def load_aliases(path):
    if not path or not os.path.isfile(path):
        return {}
    a = L.jload(path).get("aliases", {})
    resolve_chains(a)           # raises AliasError on a cycle
    return a


def resolve_chains(al):
    for k in al:
        seen, cur, hops = {k}, k, 0
        while cur in al and al[cur].get("to"):
            cur = al[cur]["to"]
            hops += 1
            if cur in seen or hops > 32:
                raise AliasError("alias cycle at " + k)
            seen.add(cur)


def follow(al, sid):
    """-> (final id, status) following alias/tombstone entries (32-hop guard)."""
    cur, hops = sid, 0
    while cur in al:
        e = al[cur]
        if e.get("status") == "tombstone" or not e.get("to"):
            return cur, "tombstone"
        cur = e["to"]
        hops += 1
        if hops > 32:
            raise AliasError("alias chain too long at " + sid)
    return cur, "active"


# ---- classification -------------------------------------------------------------------------------------
def crlf(b):
    return b.replace(b"\r\n", b"\n")


def cur_index(repo):
    ix, data = {}, {}
    for m in repo.mods:
        for s in m.syms:
            ix[s["id"]] = (m, s)
    return ix


def sym_info(m, s):
    node = s["node"]
    raw = m.data[s["start_byte"]:s["end_byte"]]
    r = {"raw": raw, "sha256": L.sha256b(crlf(raw)), "sha256_ast": X.ast_hash(node), "doc_sha256": X.doc_hash(node),
         "start_byte": s["start_byte"] - m.data[:s["start_byte"]].count(b"\r"), "path": m.path}
    if s["kind"] in ("function", "method", "class", "test"):
        r["signature_sha256"] = X.sig_hash(node)
    if s["kind"] in ("class", "module"):
        r["shell"] = X.shell_hash(node)
    return r


def classify_one(a, ix, aliases):
    sid = a["id"]
    al = aliases.get(sid)
    present = ix.get(sid)
    if al is not None:
        if present is not None:
            return {"id": sid, "class": "E_TOMBSTONE_REUSE" if al.get("status") == "tombstone" or al.get("to") != sid else "SAME"}
        tgt, st = follow(aliases, sid)
        if st == "tombstone":
            return {"id": sid, "class": "DELETE", "reason": "tombstone"}
        if tgt not in ix:
            return {"id": sid, "class": "E_DANGLING", "to": tgt}
        return {"id": sid, "class": "ALIAS", "to": tgt}
    if present is None:
        return None
    m, s = present
    i = sym_info(m, s)
    if i["sha256"] == a["sha256"]:
        return {"id": sid, "class": "SAME" if i["start_byte"] == a["start_byte"] else "SHIFT"}
    if i["sha256_ast"] == a["sha256_ast"] and i["doc_sha256"] == a["doc_sha256"]:
        return {"id": sid, "class": "FORMAT"}
    if i["sha256_ast"] == a["sha256_ast"]:
        return {"id": sid, "class": "DOC"}
    if "sha256_shell" in a and i.get("shell") == a["sha256_shell"]:
        return {"id": sid, "class": "CHILD"}          # only nested symbols changed; they carry their own anchors
    if "signature_sha256" in a and i.get("signature_sha256") != a["signature_sha256"]:
        return {"id": sid, "class": "SIG"}
    return {"id": sid, "class": "EDIT"}


def relocate(a, old_repo, new_repo, ix_new, old_ids, tau=TAU_REL, margin=MARGIN):
    """Step 7: relocation search for a deleted anchor. Returns a result dict (proposal / E_AMBIG / DELETE)."""
    sid = a["id"]
    if a["kind"] not in RELOC_KINDS:
        return {"id": sid, "class": "DELETE"}
    old = None
    for m in old_repo.mods:
        for s in m.syms:
            if s["id"] == sid:
                old = (m, s)
    if old is None:
        return {"id": sid, "class": "DELETE", "reason": "no_old_text"}
    xt = norm_tokens(old[1]["node"])
    if len(xt) < MIN_TOK:
        return {"id": sid, "class": "DELETE", "reason": "too_short"}
    cands = []
    for m in new_repo.mods:
        for s in m.syms:
            if s["kind"] not in RELOC_KINDS or s["id"] in old_ids:
                continue
            yt = norm_tokens(s["node"])
            if len(yt) < MIN_TOK:
                continue
            mx = max(len(xt), len(yt))
            l1 = hist_l1(xt, yt)
            if (10 ** 6 * l1) // (2 * mx) > tau:
                continue
            d = (10 ** 6 * indel(xt, yt)) // mx
            if d <= tau + margin:
                cands.append((d, s["id"], m, s, yt, l1))
    cands.sort(key=lambda c: (c[0], c[1]))
    within = [c for c in cands if c[0] <= tau]
    if not within:
        return {"id": sid, "class": "DELETE"}
    best = cands[0]
    runner = cands[1][0] if len(cands) > 1 else None
    if len(within) >= 2 or (runner is not None and runner - best[0] < margin):
        return {"id": sid, "class": "E_AMBIG", "candidates": [c[1] for c in cands[:4]]}
    d, nid, m, s, yt, l1 = best
    oraw = old[0].data[old[1]["start_byte"]:old[1]["end_byte"]]
    nraw = m.data[s["start_byte"]:s["end_byte"]]
    same_path = old[0].path == m.path
    if d == 0 and oraw == nraw and not same_path:
        op = "MOVE"
    elif same_path:
        op = "RENAME"
    elif d == 0:
        op = "MOVE"
    else:
        op = "MOVE+EDIT"
    prop = {"schema": "hb.alias-proposal.v2", "from": sid, "to": nid, "op": op, "d_ppm": d, "runner_up_ppm": runner, "tau_ppm": tau,
            "margin_ppm": margin, "auto_apply": op == "MOVE" and oraw == nraw,
            "evidence": {"old_sha256": L.sha256b(oraw), "new_sha256": L.sha256b(nraw), "tokens_old": len(xt), "tokens_new": len(yt), "hist_l1": l1}}
    return {"id": sid, "class": op, "to": nid, "proposal": prop}


def classify(anchors, new_repo, aliases=None, old_repo=None, tau=TAU_REL, margin=MARGIN):
    """One result per baseline anchor (status active). Cascade of HB2-06; `old_repo` is only needed for step 7."""
    aliases = aliases or {}
    resolve_chains(aliases)
    ix = cur_index(new_repo)
    old_ids = {a["id"] for a in anchors}
    out = []
    for a in anchors:
        if a.get("status", "active") != "active":
            continue
        r = classify_one(a, ix, aliases)
        if r is None:
            r = relocate(a, old_repo, new_repo, ix, old_ids, tau, margin) if old_repo is not None else {"id": a["id"], "class": "DELETE"}
        r["kind"] = a["kind"]
        out.append(r)
    return out


def summarize(results):
    c = {}
    for r in results:
        c[r["class"]] = c.get(r["class"], 0) + 1
    return dict(sorted(c.items()))


def repair_plan(results):
    """What may be applied automatically: SHIFT, FORMAT (re-baseline the anchor), byte-identical MOVE (alias write). Never RENAME/EDIT/MOVE+EDIT/DOC."""
    auto, manual = [], []
    for r in results:
        if r["class"] in ("SHIFT", "FORMAT"):
            auto.append(r)
        elif r["class"] == "MOVE" and r.get("proposal", {}).get("auto_apply"):
            auto.append(r)
        elif r["class"] in ("RENAME", "MOVE+EDIT", "MOVE", "DOC", "E_AMBIG", "EDIT", "SIG", "DELETE", "E_DANGLING", "E_TOMBSTONE_REUSE"):
            manual.append(r)
    return auto, manual


def rebaseline(anchors, new_repo, ids):
    """Fresh anchors for `ids` from the current repo (used for SHIFT/FORMAT auto repairs)."""
    x = {a["id"]: a for a in X.anchors_from_symbols(X.symbols_from_repo(new_repo))}
    return [x[a["id"]] if a["id"] in ids and a["id"] in x else a for a in anchors]


# ---- assets by uid ------------------------------------------------------------------------------------
UID_RE = re.compile(r"uid://[a-z0-9]{1,16}")


def asset_anchors(root, exts=(".gd", ".png", ".tscn", ".tres", ".cfg")):
    """Asset anchors of a Godot fixture: sidecar `<file>.uid` holds the uid; id = `<path>::asset`."""
    out = []
    for p in L.tree_files(root):
        if p.endswith(".uid"):
            continue
        uidp = os.path.join(root, p + ".uid")
        if os.path.isfile(uidp):
            u = UID_RE.search(open(uidp, encoding="utf-8").read())
            if u:
                out.append({"id": p + "::asset", "path": p, "uid": u.group(0), "sha256": L.sha_file(os.path.join(root, p)), "kind": "asset"})
    return sorted(out, key=lambda a: a["id"])


def classify_assets(old, new):
    """Identity `uid://` first, then bytes. Same uid + same bytes at a new path = MOVE (auto); same uid + new bytes = EDIT;
    no uid match: path rule (same path same bytes = SAME; else DELETE)."""
    by_uid = {a["uid"]: a for a in new}
    by_path = {a["path"]: a for a in new}
    out = []
    for a in old:
        n = by_uid.get(a["uid"])
        if n is not None:
            if n["path"] == a["path"]:
                c = "SAME" if n["sha256"] == a["sha256"] else "EDIT"
            else:
                c = "MOVE" if n["sha256"] == a["sha256"] else "EDIT"
            out.append({"id": a["id"], "class": c, "to": n["id"], "auto_apply": c == "MOVE"})
        else:
            p = by_path.get(a["path"])
            out.append({"id": a["id"], "class": "SAME" if p and p["sha256"] == a["sha256"] else "DELETE"})
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--anchors", required=True)
    ap.add_argument("--aliases")
    a = ap.parse_args(argv)
    try:
        al = load_aliases(a.aliases)
    except AliasError as e:
        sys.stderr.write("E_ALIAS_CYCLE %s\n" % e)
        return 1
    res = classify(L.read_jsonl(a.anchors), X.Repo(a.new), al, X.Repo(a.old))
    sys.stdout.write(L.dumps({"summary": summarize(res), "results": [r for r in res if r["class"] != "SAME"]}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
