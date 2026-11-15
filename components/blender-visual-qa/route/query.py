#!/usr/bin/env python3
"""Query the blender-visual-qa route index: coordinates, not proof. Read the cited span first.

  query.py any   "<question>" [--k 5] [--type sym|doc|schema|test] [--env bpy|engine|blender|generic]
  query.py find  <name>                 symbols whose id or anchor contains <name>
  query.py where "<question>" [--k 5]  files ranked by their best rows
  query.py tests <name|path>            test files that mention a symbol name or a path
  query.py error <E_CODE|text>          symbols holding an E_* literal (or the words of a refusal)
  query.py env   [bpy|engine|blender|generic] [--prefix P]   Blender coupling per file
  query.py bench [--split S]            recall@5 / MRR on route/bench/queries.tsv

Stdout: one canonical JSON line of at most 2000 bytes. rc 0 answered, 3 abstained (the question
names terms this corpus does not contain: answer AUSENTE, do not guess), 2 usage, 4 stale index,
5 bench below target. Stdlib only; reads route/index/*, writes nothing.
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import build as B  # noqa: E402

MAX_B = 2000
K1, BB = 1.2, 0.75
W = {"name": 3.0, "anchor": 3.0, "module": 2.0, "doc": 2.0, "path": 0.5, "body": 0.6}
STOP = frozenset("""a an and are as at be by can do does for from get gets how i if in into is it its me my of on or
our should so that the their then there these this to use used uses using via was we what when where which who
why will with without would you your one two all any each every there's it's does don't don t""".split())
# Declared bridges between user words and corpus words; tuned only on the FIT/DEV splits.
SYN = {
    "image": ["frame", "png"], "picture": ["frame"], "screenshot": ["frame"], "pixels": ["pixel"],
    "reject": ["refuse", "invalid"], "rejects": ["refuse"], "refusal": ["refuse"], "deny": ["refuse"],
    "check": ["validate", "gate"], "verify": ["validate", "check"], "validate": ["check"],
    "start": ["launch"], "spawn": ["launch"], "kill": ["killpg", "terminate"], "timeout": ["timeout", "deadline"],
    "write": ["atomic", "replace"], "save": ["write"], "hash": ["sha256", "digest"], "digest": ["sha256"],
    "json": ["canonical"], "seed": ["seed"], "random": ["seed"], "number": ["integer"], "integer": ["int"],
    "quantise": ["quant", "q16"], "quantize": ["quant", "q16"], "texture": ["image", "material"],
    "material": ["material", "pbr"], "triangle": ["tri", "face"], "triangles": ["tri", "face"],
    "polygon": ["face", "poly"], "uv": ["uv"], "normal": ["normal"], "lod": ["lod", "ladder"],
    "level": ["lod"], "detail": ["lod"], "convex": ["hull"], "shadow": ["shadow", "delight"],
    "lighting": ["light", "delight"], "orthographic": ["ortho"], "turntable": ["turntable", "orbit"],
    "rotate": ["turntable"], "verdict": ["verdict", "precedence"], "worst": ["precedence", "max"],
    "camera": ["camera", "cut"], "glb": ["glb", "gltf"], "gltf": ["glb"], "export": ["glb", "write"],
    "receipt": ["receipt", "bindings"], "evidence": ["receipt", "artifact"], "bench": ["bench", "truth"],
    "test": ["test"], "tests": ["test"], "covers": ["test"], "schema": ["schema"], "section": ["doc"],
}


def toks(text):
    """Query words: plain lowercase alphanumerics, never camelCase-split (a brand name stays one word)."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


def stem(w):
    for suf in ("ations", "ation", "ings", "ing", "ies", "ers", "ed", "es", "er", "ly", "s"):
        if len(w) - len(suf) >= 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def load(index=None):
    index = index or os.path.join(HERE, "index")
    rows = []
    for name in ("symbols.jsonl", "docs.jsonl"):
        with open(os.path.join(index, name), encoding="utf-8") as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


class Index:
    def __init__(self, rows):
        self.rows = rows
        self.fields = []
        df = {}
        core = set()
        product = 0
        total = 0
        for r in rows:
            f = {
                "name": [stem(w) for w in r["t"]["name"]],
                "anchor": [stem(w) for a in r.get("anchors", []) for w in B.words(a)],
                "doc": [stem(w) for w in r["t"]["doc"] if w not in STOP],
                "module": [stem(w) for w in B.words(os.path.splitext(os.path.basename(r["path"]))[0])],
                "path": [stem(w) for w in B.words(os.path.dirname(r["path"]))],
                "body": [stem(w) for w in r["t"]["body"]],
            }
            self.fields.append(f)
            seen = set()
            for v in f.values():
                seen.update(v)
            if r["path"].startswith(("tests/", "route/")):
                continue
            # Statistics come from product rows only, so adding a test does not move every score.
            for w in seen:
                df[w] = df.get(w, 0) + 1
            core.update(seen)
            total += sum(len(v) for v in f.values())
            product += 1
        self.df = df
        self.n = product
        self.avg = total / max(1, product)
        # Presence for abstention counts only product code, docs and schemas: a word seen only in
        # tests is not evidence that the corpus covers the topic.
        self.vocab = frozenset(core)

    def idf(self, w):
        d = self.df.get(w, 0)
        return math.log(1 + (self.n - d + 0.5) / (d + 0.5))

    def present(self, w):
        if w in self.vocab:
            return True
        return len(w) >= 6 and any(v.startswith(w[:6]) for v in self.vocab if len(v) >= 6)

    def expand(self, q):
        words = toks(q)
        base = [stem(w) for w in words]
        extra = []
        for a, b in zip(words, words[1:]):
            joined = stem(a + b)
            if joined in self.df:
                extra.append(joined)
        for w in words:
            for s in SYN.get(w, ()):
                extra.append(stem(s))
        return base, extra

    def score(self, terms, weights):
        out = []
        for i, f in enumerate(self.fields):
            length = sum(len(v) for v in f.values())
            s = 0.0
            hit = set()
            for t, wt in zip(terms, weights):
                tf = sum(W[k] * f[k].count(t) for k in f)
                if tf <= 0:
                    continue
                hit.add(t)
                s += wt * self.idf(t) * tf * (K1 + 1) / (tf + K1 * (1 - BB + BB * length / self.avg))
            if s > 0:
                out.append((s, len(hit), i))
        return out

    def any(self, q, k=5, rtype=None, env=None):
        base, extra = self.expand(q)
        if not base:
            return {"abstain": "no_query_term", "rows": []}
        absent = sorted({w for w in base if not self.present(w)})
        # Abstain when the question uses two or more words the product corpus never uses, or one
        # such word that is a proper name (MixedCase, ALLCAPS or capitalised mid-sentence) or sits
        # in a short question: the unknown word then carries the topic.
        named = set()
        for n, w in enumerate(re.findall(r"[A-Za-z0-9]+", q)):
            if any(c.isupper() for c in w[1:]) or (n > 0 and w[:1].isupper()):
                named.add(stem(w.lower()))
        if len(absent) >= 2 or (absent and (len(set(base)) <= 3 or named & set(absent))):
            return {"abstain": "terms_absent_from_corpus", "absent": absent, "rows": []}
        terms = base + [e for e in extra if e not in base]
        weights = [1.0] * len(base) + [0.5] * (len(terms) - len(base))
        test_intent = any(w in ("tests", "covers", "coverage", "unittest") for w in toks(q))
        doc_intent = any(w in ("section", "book", "hyperbook", "spec", "explain", "explains", "policy", "rules", "documented", "doc", "docs", "readme", "guide", "schema")
                         for w in toks(q))
        scored = []
        for s, hits, i in self.score(terms, weights):
            r = self.rows[i]
            if rtype and r["type"] != rtype:
                continue
            if env and r.get("env") != env:
                continue
            if r["type"] in ("doc", "schema") and not doc_intent:
                s *= 0.6
            if r["type"] == "test" and not test_intent:
                s *= 0.5
            if r["type"] == "sym" and r["path"].startswith("tests/") and not test_intent:
                s *= 0.4
            scored.append((-s, r["path"], r["line"], i))
        scored.sort()
        if not scored:
            return {"abstain": "no_match", "rows": []}
        top = -scored[0][0]
        rows = []
        for neg, _, _, i in scored[:k]:
            r = self.rows[i]
            rows.append({"id": r["id"], "at": "%s:%d" % (r["path"], r["line"]), "type": r["type"],
                         "env": r.get("env"), "s": int(-neg * 1000000 // top) if top else 0})
        return {"rows": rows, "overflow": max(0, len(scored) - k)}


def emit(obj, rc):
    obj = dict(obj, rc=rc, note="coordinates, not proof")
    text = B.canon(obj)
    while len(text.encode("utf-8")) > MAX_B and obj.get("rows"):
        obj["rows"] = obj["rows"][:-1]
        obj["overflow"] = obj.get("overflow", 0) + 1
        text = B.canon(obj)
    print(text)
    return rc


def rr(rows, first):
    for n, r in enumerate(rows[:5], 1):
        if first(r):
            return n
    return 0


def accept_fn(accept):
    acc = [a for a in accept.split(",") if a]

    def ok(r):
        ids = [r["id"]] + list(r.get("anchors", []))
        for a in acc:
            for i in ids:
                if (a.endswith("*") and i.startswith(a[:-1])) or i == a:
                    return True
        return False
    return ok


def bench(ix, split=None, path=None):
    path = path or os.path.join(HERE, "bench", "queries.tsv")
    with open(path, encoding="utf-8") as fh:
        lines = [l.rstrip("\n").split("\t") for l in fh if l.strip()]
    head, body = lines[0], lines[1:]
    col = {h: n for n, h in enumerate(head)}
    by_id = {r["id"]: r for r in ix.rows}
    res = {}
    for row in body:
        sp = row[col["split"]]
        if split and sp != split:
            continue
        kind, q = row[col["kind"]], row[col["query"]]
        acc = row[col["accept"]] if len(row) > col["accept"] else ""
        out = ix.any(q, k=5)
        m = res.setdefault(sp, {"n_pos": 0, "rr": 0.0, "hit5": 0, "n_neg": 0, "neg_answered": [], "misses": []})
        if kind == "neg":
            m["n_neg"] += 1
            if "abstain" not in out:
                m["neg_answered"].append(row[col["qid"]])
            continue
        m["n_pos"] += 1
        full = [dict(by_id.get(r["id"], {}), id=r["id"]) for r in out.get("rows", [])]
        n = rr(full, accept_fn(acc))
        if n:
            m["hit5"] += 1
            m["rr"] += 1.0 / n
        else:
            m["misses"].append(row[col["qid"]])
    out = {}
    for sp, m in sorted(res.items()):
        npos = max(1, m["n_pos"])
        out[sp] = {"recall5_ppm": m["hit5"] * 1000000 // npos, "mrr_ppm": int(m["rr"] * 1000000 // npos),
                   "n_pos": m["n_pos"], "n_neg": m["n_neg"], "neg_answered": m["neg_answered"], "misses": m["misses"]}
    return out


def run_bench(ix, split=None):
    """Frozen splits must match their sha; recall@5 floors (MEASURED) only ratchet up; MRR and TARGET are reported."""
    frozen = json.load(open(os.path.join(HERE, "bench", "FROZEN.json"), encoding="utf-8"))
    with open(os.path.join(HERE, "bench", "queries.tsv"), encoding="utf-8") as fh:
        rows = [l.rstrip("\n").split("\t") for l in fh if l.strip()][1:]
    import hashlib
    for sp, sha in frozen["splits"].items():
        got = hashlib.sha256("".join("\t".join(r) + "\n" for r in rows if r[1] == sp).encode("utf-8")).hexdigest()
        if got != sha:
            print(B.canon({"code": "E_FROZEN", "rc": 4, "split": sp}))
            return 4
    out = bench(ix, split=split)
    rc = 0
    for sp, m in out.items():
        floor = frozen["floors"].get(sp)
        tgt = frozen["targets"]
        m["target_met"] = m["recall5_ppm"] >= tgt["recall5_ppm"] and m["mrr_ppm"] >= tgt["mrr_ppm"] and not m["neg_answered"]
        if m["neg_answered"] or (floor and m["recall5_ppm"] < floor["recall5_ppm"]):
            rc = 5
    print(B.canon({"bench": out, "rc": rc}))
    return rc


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        return emit({"code": "E_USAGE", "usage": __doc__.splitlines()[2].strip()}, 2)
    verb, rest = a[0], a[1:]
    opts = {}
    pos = []
    i = 0
    while i < len(rest):
        if rest[i].startswith("--") and i + 1 < len(rest):
            opts[rest[i][2:]] = rest[i + 1]
            i += 2
        else:
            pos.append(rest[i])
            i += 1
    try:
        k = max(1, min(20, int(opts.get("k", "5"))))
    except ValueError:
        return emit({"code": "E_USAGE"}, 2)
    if verb == "check":
        stale = B.check()
        if stale:
            return emit({"code": "E_STALE", "stale": stale, "fix": "python3 -B route/build.py"}, 4)
        print("ROUTE_OK")
        return 0
    ix = Index(load())
    if verb == "any" and pos:
        out = ix.any(" ".join(pos), k=k, rtype=opts.get("type"), env=opts.get("env"))
        return emit(out, 3 if "abstain" in out else 0)
    if verb == "where" and pos:
        out = ix.any(" ".join(pos), k=60)
        if "abstain" in out:
            return emit(out, 3)
        files = {}
        for r in out["rows"]:
            p = r["at"].split(":")[0]
            files.setdefault(p, r["s"])
        rows = [{"path": p, "s": s} for p, s in sorted(files.items(), key=lambda x: (-x[1], x[0]))[:k]]
        return emit({"rows": rows}, 0)
    if verb == "find" and pos:
        needle = pos[0].lower()
        rows = [{"id": r["id"], "at": "%s:%d" % (r["path"], r["line"]), "env": r.get("env")}
                for r in ix.rows if r["type"] == "sym" and (needle in r["id"].lower()
                                                            or any(needle in x.lower() for x in r.get("anchors", [])))]
        return emit({"rows": rows[:k], "overflow": max(0, len(rows) - k)}, 0 if rows else 3)
    if verb == "tests" and pos:
        name = pos[0].rsplit(".", 1)[-1]
        rows = []
        for r in ix.rows:
            if r["type"] != "test":
                continue
            text = open(os.path.join(ROOT, r["path"]), encoding="utf-8").read()
            if name in text or pos[0] in text:
                rows.append({"id": r["id"], "at": r["path"] + ":1", "n": text.count(name)})
        rows.sort(key=lambda x: (-x["n"], x["id"]))
        return emit({"rows": rows[:k], "overflow": max(0, len(rows) - k)}, 0 if rows else 3)
    if verb == "error" and pos:
        lit = pos[0]
        rows = [{"id": r["id"], "at": "%s:%d" % (r["path"], r["line"])}
                for r in ix.rows if r["type"] == "sym" and lit in r.get("errors", [])]
        if rows:
            return emit({"rows": rows[:k], "overflow": max(0, len(rows) - k)}, 0)
        out = ix.any(" ".join(pos), k=k, rtype="sym")
        return emit(out, 3 if "abstain" in out else 0)
    if verb == "env":
        want = pos[0] if pos else None
        prefix = opts.get("prefix", "")
        files = {}
        for r in ix.rows:
            if r["type"] == "sym" and r["path"].startswith(prefix) and not r["path"].startswith("tests/"):
                files.setdefault(r["path"], {}).setdefault(r["env"], 0)
                files[r["path"]][r["env"]] += 1
        if want:
            rows = [{"path": p, want: c[want], "of": sum(c.values())} for p, c in sorted(files.items()) if c.get(want)]
            rows.sort(key=lambda x: (-x[want], x["path"]))
        else:
            tot = {}
            for c in files.values():
                for e, n in c.items():
                    tot[e] = tot.get(e, 0) + n
            return emit({"totals": tot, "files": len(files)}, 0)
        return emit({"rows": rows[:k], "overflow": max(0, len(rows) - k)}, 0)
    if verb == "bench":
        return run_bench(ix, opts.get("split"))
    return emit({"code": "E_USAGE", "verb": verb}, 2)


if __name__ == "__main__":
    sys.exit(main())
