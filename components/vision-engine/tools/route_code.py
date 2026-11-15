#!/usr/bin/env python3
"""Code router: map a task text to catalogue anchors (stdlib, integer BM25, no index file).
Usage: route_code.py --q "<text>" [--k 3] [--json]
Stdout: one canonical JSON line <= 2000 B:
  {"abstain":{"flag":bool,"reason":str|null},"code":..,"overflow":int,"r":[[anchor,path,line,score_ppm]..],"rc":int}
rc 0 answered, 3 abstained, 2 usage.
"""
# @module {"book":"HB-VC-a0-arch","stage":"TOOLS"}
import json
import os
import re
import sys
MAX_B = 2000
K1_MILLI, B_MILLI = 1200, 750
ZONE_W = {"anchor": 3, "symbol": 2, "purpose": 2, "text": 1}
STOP = frozenset(("a add an and are between can do does for from how i in instead is it new not of on or the to "
                  "what when where which why with").split())
SUFFIX = ("ations", "ation", "ion", "ings", "ing", "ies", "es", "ed", "s")
FIELD_RE = re.compile(r"f(0|[1-9][0-9]{0,9}):t[0-9a-f]{4}|f(0|[1-9][0-9]{0,9}):all|r(0|[1-9][0-9]{0,9}):[0-9a-f]{4}")
ANCHOR_RE = re.compile(r"(vc|vm|va|tools)(\.[A-Za-z0-9_]+){2,}")
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,40}")
# document-side synonyms: code says fg/screen, tasks say foreground/screenshot (query side and abstention rule untouched)
DOC_ALIAS = {"fg": "foreground", "screen": "screenshot", "png": "screenshot"}
# @anchor:va.route_code.canon [HB-VC-a0-arch]
def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
# @anchor:va.route_code.stem [HB-VC-a0-arch]
def stem(t):
    """Deterministic suffix strip (no dictionary): engines->engin, engine->engin, matching->match."""
    for x in SUFFIX:
        if len(t) > len(x) + 3 and t.endswith(x):
            return t[:-len(x)]
    return t[:-1] if len(t) > 4 and t.endswith("e") else t
# @anchor:va.route_code.toks [HB-VC-a0-arch]
def toks(s):
    return [stem(t) for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP]
# @anchor:va.route_code.lg_milli [HB-VC-a0-arch]
def lg_milli(x_milli):
    """Monotone integer log2 of a milli-scaled value >= 1000 (piecewise-linear between powers of two)."""
    k = x_milli.bit_length() - 1
    return 1000 * k + (x_milli - (1 << k)) * 1000 // (1 << k)
def _catalogue(root):
    cat = os.path.join(root, "catalogue")
    docs = {}
    def doc(a):
        return docs.setdefault(a, {"path": "", "line": 0, "anchor": [], "symbol": [], "purpose": [], "text": []})

    def rows(name):
        p = os.path.join(cat, name)
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    p = os.path.join(cat, "symbols.jsonl")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    r = json.loads(ln)
                    d = doc(r["anchor"])
                    d["path"], d["line"] = r["path"], r["line"]
                    d["symbol"] += toks(r["symbol"])
                    d["purpose"] += toks(r.get("doc", ""))
    for r in rows("routes.tsv"):
        doc(r[1])["purpose"] += toks(r[0])
    thr, fail = {}, {}
    for r in rows("thresholds.tsv"):
        thr[r[0]] = r[3]
        doc(r[3])["text"] += toks(" ".join([r[0], r[2], r[4], r[6]]))
    for r in rows("failures.tsv"):
        fail[r[0]] = r[3]
        doc(r[3])["text"] += toks(" ".join([r[0], r[4]]))
    for r in rows("book_sections.tsv"):
        b = doc("hb:" + r[0])
        b["path"], b["line"] = r[1], int(r[2])
        b["purpose"] += toks(r[3] + " " + r[4])
        b["text"] += toks(r[6].replace("/", " ")) if len(r) > 6 else []
        for a in (r[5].split(",") if r[5] else []):
            if a in docs:
                docs[a]["text"] += toks(r[3])
                b["text"] += toks(a)
    for a, d in docs.items():
        d["anchor"] = toks(a) if not a.startswith("hb:") else []
        for z in ZONE_W:
            d[z] += [DOC_ALIAS[x] for x in d[z] if x in DOC_ALIAS]
    return docs, thr, fail


# @anchor:va.route_code.exact [HB-VC-a0-arch]
def exact(q, docs, thr, fail):
    s = q.strip()
    if FIELD_RE.fullmatch(s):
        a = "vc.ids.parse"
    elif s in docs:
        a = s
    elif NAME_RE.fullmatch(s) and (s in thr or s in fail):
        a = thr.get(s) or fail.get(s)
    else:
        return None
    d = docs.get(a, {"path": "", "line": 0})
    return [[a, d["path"], d["line"], 1000000]]


# @anchor:va.route_code.score [HB-VC-a0-arch]
def score(qt, docs):
    n = len(docs)
    lens = {}
    tfw = {}
    for a, d in docs.items():
        w = {}
        dl = 0
        for z, wz in ZONE_W.items():
            for t in d[z]:
                w[t] = w.get(t, 0) + wz
                dl += wz
        tfw[a], lens[a] = w, dl
    avg = max(1, sum(lens.values()) // max(1, n))
    out = {}
    for t in sorted(set(qt)):
        hit = [a for a in docs if t in tfw[a]]
        if not hit:
            continue
        df = len(hit)
        idf = lg_milli((2 * (n - df) + 1) * 1000 // (2 * df + 1) + 1000)
        for a in hit:
            tf = tfw[a][t]
            norm = 1000 - B_MILLI + B_MILLI * (lens[a] * 1000 // avg) // 1000
            den = tf * 1000 + K1_MILLI * norm // 1000
            frac = tf * (K1_MILLI + 1000) * 1000 // den
            out[a] = out.get(a, 0) + idf * frac
    return out


# @anchor:va.route_code.route [HB-VC-a0-arch]
def route(q, k=3, root=None, index_dir=None):  # noqa: C901
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs, thr, fail = load(root, index_dir)
    ex = exact(q, docs, thr, fail)
    if ex is not None and ex[0][1]:
        return {"abstain": {"flag": False, "reason": None}, "r": [_row(ex[0][0], ex[0][3], docs, [q.strip()])], "overflow": 0}
    qt = toks(q)
    legacy = {a: d["_legacy"] for a, d in docs.items() if d.get("_legacy")}
    sc = score(qt, legacy) if legacy else {}
    head = sorted(sc, key=lambda a: (-sc[a], a))[:k]
    need = (len(set(qt)) + 1) // 2 if len(set(qt)) <= 3 else (len(set(qt)) + 2) // 3
    covered = sum(any(t in legacy[a][z] for a in head for z in ZONE_W) for t in set(qt))
    if not head or covered < need:
        sc = score(qt, docs)
    # only candidates that resolve to code (path:line) may be answered; the rest are future/unbuilt anchors
    ranked = sorted(((a, s_) for a, s_ in sc.items() if docs[a]["path"]), key=lambda kv: (-kv[1], kv[0]))
    if not ranked:
        why = "no query term hits a catalogue zone" if not sc else "no matching anchor has a code path"
        return {"abstain": {"flag": True, "reason": why}, "r": [], "overflow": 0}
    nq = len(set(qt))
    need = (nq + 1) // 2 if nq <= 3 else (nq + 2) // 3  # short queries: half the terms; longer: a third
    head = [a for a, _ in ranked[:k]]  # coverage is judged on the answer the caller receives (top-k), not top-1
    got = sum(1 for t in set(qt) if any(t in docs[a][z] for a in head for z in ZONE_W))
    if got < need:
        return {"abstain": {"flag": True, "reason": "best anchor matches %d of %d query terms" % (got, len(set(qt)))},
                "r": [], "overflow": 0}
    res = [_row(a, s_, docs, _why(qt, a, docs)) for a, s_ in ranked[:k]]
    return _pack({"abstain": {"flag": False, "reason": None}, "r": res[:k], "overflow": max(0, len(ranked) - k)})
# @anchor:va.route_code.main [HB-VC-a0-arch]
# @anchor:tools.routecode.main
def main(argv=None):
    try:
        bci = _bci()
        parser = bci._parser()
        parser.add_argument("--q")
        parser.add_argument("--anchor")
        parser.add_argument("--stage")
        parser.add_argument("--scope", action="append")
        parser.add_argument("--k", type=int, default=3)
        parser.add_argument("--index")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        if not 1 <= args.k <= 20:
            raise bci.BuildError("E_USAGE", 2, "invalid k")
        if args.check:
            missing = bci.check_public_anchors(args.root, (args.paths or []) + (args.scope or []) or None)
            res = dict(code="E_MISSING_ANCHORS" if missing else "OK", rc=5 if missing else 0,
                       missing=missing, missing_count=len(missing))
        elif args.anchor:
            res = lookup_anchor(args.anchor, args.root, args.index)
        elif args.stage:
            res = list_stages(args.stage, args.root, args.index)
        elif args.q and args.q.strip():
            res = route(args.q, args.k, args.root, args.index)
            rc = 3 if res["abstain"]["flag"] else 0
            res.update(code="E_NAV_ABSTAIN" if rc else "OK", rc=rc)
        else:
            raise bci.BuildError("E_USAGE", 2, "query or navigation mode required")
        res = _pack(res)
    except Exception as e:
        if isinstance(e, _bci().BuildError):
            res = dict(code=e.code, rc=e.rc, detail=e.detail)
        elif isinstance(e, (OSError, ValueError, SyntaxError, KeyError, TypeError, AttributeError, IndexError)):
            res = dict(code="E_SCHEMA", rc=2, detail="invalid corpus or index input")
        else:
            raise
    res = _pack(res)
    sys.stdout.write(canon(res))
    return res["rc"]


def _bci():
    # Script execution under python -I excludes the repository from sys.path.
    import importlib.util
    name = "tools.build_code_index"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(os.path.dirname(__file__), "build_code_index.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _read_index(root, index_dir):
    from pathlib import Path
    bci = _bci()
    root, directory = Path(root).resolve(), Path(index_dir).resolve()
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["schema"] != "vqa.code-index-manifest/v1":
            raise ValueError()
        for rel, digest in manifest["sources"].items():
            full = bci._safe(root, rel)
            if not full.is_file() or bci.sha(full.read_bytes()) != digest:
                raise bci.BuildError("E_STALE", 4, "index sources have changed")
        known_catalogue = ("symbols.jsonl", "routes.tsv", "thresholds.tsv", "failures.tsv", "book_sections.tsv", "layers.json", "subjects.json")
        if {"catalogue/" + n for n in known_catalogue if (root / "catalogue" / n).is_file()} != {r for r in manifest["sources"] if r.startswith("catalogue/")}:
            raise bci.BuildError("E_STALE", 4, "catalogue inventory has changed")
        # Detect additions and removals as well as changed source bytes.
        expected_py = {r for r in manifest["sources"] if r.endswith(".py")}
        if set(bci._pyfiles(root)) != expected_py:
            raise bci.BuildError("E_STALE", 4, "index source inventory has changed")
        expected_companions = {r for r in manifest["sources"] if r.startswith("hyperbooks/")}
        observed_companions = set()
        hb = root / "hyperbooks"
        if (hb / "COMPANION_FORMAT.json").is_file():
            observed_companions.add("hyperbooks/COMPANION_FORMAT.json")
        for book in hb.glob("HB-*"):
            for p in book.rglob("*"):
                if p.is_file() and (p.parent == book or p.parent.name in ("receipts", "index")):
                    observed_companions.add(p.relative_to(root).as_posix())
        if expected_companions != observed_companions:
            raise bci.BuildError("E_STALE", 4, "companion inventory has changed")
        blobs = {}
        for rel, digest in manifest["files"].items():
            raw = bci._safe(directory, rel).read_bytes()
            if bci.sha(raw) != digest:
                raise bci.BuildError("E_INDEX", 5, "index content hash mismatch")
            blobs[rel] = raw
        identity = bci.sha(canon(dict(files=manifest["files"], sources=manifest["sources"])).encode())
        if identity != manifest["index_sha256"] or (directory / "ID").read_text() != identity + "\n":
            raise bci.BuildError("E_INDEX", 5, "index identity mismatch")
        shards = json.loads(blobs["shards.json"])
        def read_map(rel):
            if rel in shards:
                result = {}
                for child in shards[rel]:
                    result.update(read_map(child))
                return result
            return json.loads(blobs[rel])
        records = {}
        for rel in sorted(blobs):
            if rel.startswith("docs/"):
                for line in blobs[rel].splitlines():
                    r = json.loads(line)
                    bci._safe(root, r["path"])
                    records[r["anchor"]] = r
        cols = read_map("cols.json")
        ordered = sorted(records.values(), key=lambda r: r["i"])
        if cols["N"] != len(ordered) or cols["zones"] != bci.ZONES:
            raise ValueError("invalid columns")
        if any(shards.get(rel) != children for rel, children in cols.get("shards", {}).items()):
            raise ValueError("inconsistent shard map")
        for key in ("anchor", "path", "line", "symbol", "book", "stage"):
            if cols[key] != [r[key] for r in ordered]:
                raise ValueError("column/document mismatch")
        for i, r in enumerate(ordered):
            if r["i"] != i:
                raise ValueError("invalid document ordinal")
            r["zones"] = {z: [] for z in bci.ZONES}
        prefixes = sorted({rel.split("/")[1].split(".")[0].split("_")[0] for rel in blobs if rel.startswith("post/")})
        for prefix in prefixes:
            for token, posting in read_map("post/" + prefix + ".json").items():
                if token[:2] != prefix:
                    raise ValueError("invalid posting prefix")
                for zone, pairs in posting["z"].items():
                    if zone not in bci.ZONES or len(pairs) % 2:
                        raise ValueError("invalid posting zone")
                    for i, count in zip(pairs[::2], pairs[1::2]):
                        if type(i) is not int or not 0 <= i < len(ordered) or type(count) is not int or count <= 0:
                            raise ValueError("invalid posting position")
                        ordered[i]["zones"][zone].extend([token] * count)
        aliases = read_map("aliases.json")
        return dict(records=records, companions=read_map("companions.json"), stages=read_map("stages.json"),
                    sources=manifest["sources"], thresholds=aliases["thresholds"], failures=aliases["failures"])
    except bci.BuildError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise bci.BuildError("E_INDEX", 5, "invalid index") from None


def _index(root, index_dir=None):
    bci = _bci()
    if index_dir is not None:
        return _read_index(root, index_dir)
    for candidate in (os.path.join(root, "build/code_index"), os.path.join(root, "out/code_index"),
                      "/dev/shm/vqa-code-index-" + bci.sha(os.path.realpath(root).encode())[:16]):
        if os.path.isdir(candidate):
            try:
                return _read_index(root, candidate)
            except bci.BuildError:
                pass
    return bci.build_in_memory(root)


# @anchor:va.route_code.load [HB-VC-a0-arch]
def load(root, index_dir=None):
    index = _index(root, index_dir)
    docs, thr, fail = _catalogue(root)
    for d in docs.values():
        d["_legacy"] = {k: list(v) if isinstance(v, list) else v for k, v in d.items()}
    for a, r in index["records"].items():
        if a not in docs:
            docs[a] = dict(path=r["path"], line=r["line"], anchor=r["zones"]["anchor"],
                           symbol=r["zones"]["symbol"], purpose=r["zones"]["doc"], text=r["zones"]["hb"])
        else:
            docs[a]["purpose"] += r["zones"]["doc"]
            docs[a]["text"] += r["zones"]["hb"]
        docs[a]["_meta"] = r
    return docs, thr, fail


def _why(qt, anchor, docs):
    matched = [t for t in set(qt) if any(t in docs[anchor][z] for z in ZONE_W)]
    return sorted(matched, key=lambda t: (-_bci().idf_ppm(len(docs), sum(any(t in d[z] for z in ZONE_W) for d in docs.values())), t))[:3]


def _row(anchor, score_ppm, docs, why):
    d = docs[anchor]
    return [anchor, d["path"], d["line"], score_ppm, d.get("_meta", {}).get("stage", "AUSENTE"), [t[:24] for t in why]]


def _pack(res):
    while len(canon(res).encode("utf-8")) > MAX_B and res.get("r"):
        res["r"].pop()
        res["overflow"] = res.get("overflow", 0) + 1
    while len(canon(res).encode("utf-8")) > MAX_B and res.get("missing"):
        res["missing"].pop()
        res["overflow"] = res.get("overflow", 0) + 1
    if len(canon(res).encode("utf-8")) > MAX_B:
        return dict(code="E_BUDGET", rc=6, detail="response exceeds byte budget")
    return res


def _abstain(reason):
    return dict(abstain=dict(flag=True, reason=reason), code="E_NAV_ABSTAIN", r=[], rc=3, overflow=0)


# @anchor:va.route_code.lookup_anchor [HB-VC-a0-arch]
def lookup_anchor(anchor_id, root=None, index_dir=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index = _index(root, index_dir)
    a = index["thresholds"].get(anchor_id) or index["failures"].get(anchor_id) or anchor_id
    r = index["records"].get(a)
    if r is None:
        return _abstain("anchor not found: " + anchor_id[:120])
    data = {k: r[k] for k in ("anchor", "book", "callees", "callers", "doc", "line", "path", "stage", "symbol")}
    return _pack(dict(abstain=dict(flag=False, reason=None), anchor=data, code="OK", rc=0, overflow=0,
                      r=[[a, r["path"], r["line"], 1000000, r["stage"], [a]]]))


# @anchor:va.route_code.list_stages [HB-VC-a0-arch]
def list_stages(stage_filter, root=None, index_dir=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index = _index(root, index_dir)
    name = _bci().BOOK_STAGE.get(stage_filter, stage_filter)
    stages = index["stages"]
    stage = next((s for s in stages if s.lower() == name.lower()), None)
    all_stages = name.lower() in ("*", "all")
    if not all_stages and not stage:
        return _abstain("stage not found: " + stage_filter[:120])
    anchors = sorted(index["records"]) if all_stages else stages[stage]
    rows = [[a, index["records"][a]["path"], index["records"][a]["line"], 1000000,
             index["records"][a]["stage"], [index["records"][a]["stage"].lower()]] for a in anchors]
    result = dict(abstain=dict(flag=False, reason=None), code="OK", rc=0, stage=stage or "ALL", r=rows, overflow=0)
    if all_stages:
        result["stages"] = {s: len(a) for s, a in sorted(stages.items())}
    return _pack(result)


if __name__ == "__main__":
    sys.exit(main())
