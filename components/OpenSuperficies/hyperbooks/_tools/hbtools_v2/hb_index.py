#!/usr/bin/env python3
"""HB2-02: symbol-first index (integer BM25F columns per field) and the ranker.

Fields per candidate symbol: name, sig, doc, com, lit, card, sec. Candidates = function/method/class symbols of tools/ plus
`.gd` functions and scene nodes of the fixture (never tests, modules, constants or sub-anchors).
hb_index.py --root HB [--out book/index]  builds <out>/{cols.json, post.json, vocab.json, ID}; params are read at query time.
"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_lib_bundle as SB
import hb_build as B  # noqa: E402

FIELDS = ["name", "sig", "doc", "com", "lit", "card", "sec"]
CAND_KINDS = ("function", "method", "class", "gd_func", "scene_node")
# permille weights (name, sig, doc, sec) of D-HB2-02; card = w_card (+1000 for S1); com/lit = w_body
PROFILES = {"S0": dict(name=3000, sig=1000, doc=1500, sec=500, card_add=0), "S1": dict(name=2000, sig=800, doc=1000, sec=1000, card_add=1000),
            "S2": dict(name=2500, sig=1000, doc=1500, sec=800, card_add=0)}
DEFAULT_FIND = dict(profile="S0", w_card=0, lcp5=0, w_body=0)
DEFAULT_IMPACT = dict(profile="S0", k_e=1, d_max=1, w_body=500)
B_FIELD = L.B_PPM


def weights(cfg):
    """Field weights (permille) of one ranking configuration; `ablate` in {nocard, nosec, b1fields} zeroes fields (reported ablations)."""
    p = PROFILES[cfg["profile"]]
    w = {"name": p["name"], "sig": p["sig"], "doc": p["doc"], "sec": p["sec"], "card": cfg["w_card"] + p["card_add"] if cfg["w_card"] or p["card_add"] else 0,
         "com": cfg["w_body"], "lit": cfg["w_body"]}
    ab = cfg.get("ablate")
    if ab in ("nocard", "b1fields"):
        w["card"] = 0
    if ab in ("nosec", "b1fields"):
        w["sec"] = 0
    if ab == "b1fields":
        w["com"] = w["lit"] = 0
    return w


def is_candidate(s):
    return s["kind"] in CAND_KINDS and not s["path"].startswith("tests/")


def field_tokens(s, texts, secs):
    """Token lists per field for one symbol."""
    t = texts.get(s["id"]) or {}
    qual = s["id"].split("::", 1)[1]
    stem = os.path.splitext(os.path.basename(s["path"]))[0]
    sig = s["sig"]
    args = sig.split("(", 1)[1] if "(" in sig else sig
    card = s.get("card") or {}
    return {"name": L.toks(L.split_ident(qual) + " " + L.split_ident(stem)), "sig": L.toks(args), "doc": L.toks(t.get("doc", "")),
            "com": L.toks(t.get("com", "")), "lit": L.toks(t.get("lit", "")),
            "card": L.toks(" ".join([card.get("purpose", "")] + card.get("tasks", []))), "sec": L.toks(secs.get(s["id"], ""))}


def sec_texts(symbols, book_text):
    """id -> governing section titles + INV/AU texts."""
    pb = B.parse_book(book_text)
    title = {sb["anchor"]: sb["title"] for sb in pb["sections"]}
    inv = {}
    for ib in pb["inv"]:
        for it in ib["inv"] + ib["au"]:
            inv[it["id"]] = it["text"]
    out = {}
    for s in symbols:
        g = s["governs"]
        out[s["id"]] = " ".join([title.get(a, "") for a in g["sections"]] + [inv.get(i, "") for i in g["inv"] + g["au"]])
    return out


def build_index(root, out=None, symbols_bundle=None):
    out = out or os.path.join(root, L.BOOK_SUB, "index")
    syms = L.read_jsonl(os.path.join(root, L.BOOK_SUB, "symbols.jsonl"))
    with open(os.path.join(root, L.BOOK_SUB, "book.md"), encoding="utf-8") as f:
        book = f.read()
    x = SB.source(L.subject_of(root), os.path.join(root, "out"), symbols_bundle)
    texts = x["texts"]
    if symbols_bundle or os.environ.get("HBV2_SYMBOLS_BUNDLE"):
        # Retain authored governance/cards while taking symbols only from the bundle.
        byid = {s["id"]: s for s in syms}
        syms = [dict(s, **{k: byid.get(s["id"], {}).get(k, s.get(k)) for k in ("governs", "card")}) for s in x["symbols"]]
    cands = sorted((s for s in syms if is_candidate(s)), key=lambda s: s["id"].encode("utf-8"))
    secs = sec_texts(syms, book)
    docs = [field_tokens(s, texts, secs) for s in cands]
    ix = L.bm25f_index(docs, FIELDS)
    post = {}
    for t in sorted(ix["post"]):
        zs = {}
        dfs = set()
        for f in FIELDS:
            dm = ix["post"][t].get(f)
            if dm:
                zs[f] = [x for d in sorted(dm) for x in (d, dm[d])]
                dfs |= set(dm)
        post[t] = {"d": len(dfs), "i": L.idf_ppm(ix["N"], len(dfs)), "z": zs}
    vocab = set(ix["post"])
    for s in syms:
        vocab |= set(L.toks(L.split_ident(os.path.splitext(os.path.basename(s["path"]))[0])))
    cols = {"N": ix["N"], "fields": FIELDS, "A": ix["A"], "len": ix["lens"], "ids": [s["id"] for s in cands], "B": B_FIELD, "K": L.K_BM}
    files = {"cols.json": L.canon(cols), "post.json": L.canon(post), "vocab.json": L.canon(sorted(vocab))}
    ixid = L.sha256b(b"".join(files[k] for k in sorted(files)))
    files["ID"] = (ixid + "\n").encode()
    os.makedirs(out, exist_ok=True)
    for k, v in files.items():
        with open(os.path.join(out, k), "wb") as f:
            f.write(v)
    return ixid


class Index:
    def __init__(self, d):
        self.d = d
        self.cols = L.jload(os.path.join(d, "cols.json"))
        self.postj = L.jload(os.path.join(d, "post.json"))
        self.vocab = set(L.jload(os.path.join(d, "vocab.json")))
        self.ixid = open(os.path.join(d, "ID"), encoding="utf-8").read().strip()
        self.ix16 = self.ixid[:16]
        self.ids = self.cols["ids"]
        post = {}
        for t, p in self.postj.items():
            post[t] = {f: {flat[j]: flat[j + 1] for j in range(0, len(flat), 2)} for f, flat in p["z"].items()}
        cls = {}
        for t in sorted(post):
            if len(t) >= 5:
                cls.setdefault(t[:5], []).append(t)
        self.bm = dict(N=self.cols["N"], fields=self.cols["fields"], lens=self.cols["len"], A=self.cols["A"], post=post, cls=cls)

    def posting(self, t, lcp):
        return L.bm25f_vposting(self.bm, t, lcp)

    def scores(self, qterms, cfg):
        return L.bm25f_scores(self.bm, qterms, weights(cfg), bool(cfg["lcp5"]))

    def order(self, S):
        """Total order over the candidates with a positive score: (-S, natural id)."""
        return sorted(S, key=lambda d: (-S[d], self.ids[d].encode("utf-8")))

    def full_order(self, S):
        """Order over ALL candidates (zero scores follow in natural-id order) - used for rank positions of gold."""
        return sorted(range(len(self.ids)), key=lambda d: (-S.get(d, 0), self.ids[d].encode("utf-8")))

    def features(self, qterms, order, cfg):
        """Abstention features on the unexpanded query: (m, cov_ppm, oov_ppm). d1 = rank 1 of `order`."""
        terms = sorted(set(qterms))
        lcp = bool(cfg["lcp5"])
        if not terms:
            return 0, 0, 0
        vp = {t: self.posting(t, lcp) for t in terms}
        oov = (10 ** 6 * sum(1 for t in terms if (vp[t][1] == 0 and t not in self.vocab))) // len(terms)
        if not order:
            return 0, 0, oov
        d1 = order[0]
        m = cov_num = 0
        for t in terms:
            idf, df, z = vp[t]
            if df > 0 and any(d1 in z.get(f, ()) for f in FIELDS if f in z):
                m += 1
                cov_num += idf
        den = sum(vp[t][0] for t in terms)
        return min(m, 64), min(10 ** 6, (10 ** 6 * cov_num) // max(1, den)), oov


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--out")
    ap.add_argument("--symbols-bundle", default=os.environ.get("HBV2_SYMBOLS_BUNDLE"))
    a = ap.parse_args(argv)
    print(build_index(a.root, a.out, a.symbols_bundle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
