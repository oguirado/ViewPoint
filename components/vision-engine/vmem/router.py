# @module {"id":"vm.router","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs","vm.store"],"book":"HB-VC-s7-vmem"}
"""@module vm.router - read-only query ladder V0-V4 over one pinned generation (SPEC_VMEM §8, §9). Integer-only, stdlib."""
import bisect
import re
import struct

from visioncore import canon, layout
from visioncore.errors import VqaError
from .blobs import get_blob
from .store import Store
from . import units as U

SIG_HAM_MAX = U.SIG_HAM_MAX
VM_LOOK_SHARE_PPM = U.VM_LOOK_SHARE_PPM
T0_Q4_DEFAULT = 32
SHORTLIST_B = 2000
ANSWER_SOFT_TOK = 2520
ANSWER_HARD_TOK = 4000
DIGEST_TOK = 600
QUOTE_MIN_CHARS = 12
K1_M, B_M = 1200, 750
ZONES = (("key", 3), ("subject", 2), ("predicate", 2), ("summary", 1), ("tk", 1))
ABSTAIN = {"a": "scope_unknown", "c": "build_out_of_range", "d": "tk_unknown", "e": "sig_no_hit",
           "f": "no_lexical_hit", "g": "check_failed"}
_TOK = re.compile(r"[a-z0-9]+")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def est_tokens(nbytes):
    """Token model [ASSUMED]: ceil(bytes / 4)."""
    return (nbytes + 3) // 4


def _tokens(s):
    return _TOK.findall(str(s).lower())


def parse_sig(h):
    if not isinstance(h, str) or not _HEX16.match(h):
        raise VqaError("E_USAGE", {"reason": "bad_sig", "sig": str(h)[:20]})
    return int(h, 16)


# ---------------------------------------------------------------- index builders (used by ingest/compact writers)
def build_sig_index(units):
    """-> {relpath: bytes}: index/sig/b0..b3.bin = sorted LE (band u16, row u32) pairs; ids.bin = 'unit_id\\tsig64' lines (I9)."""
    rows = []
    for u in sorted(units, key=lambda u: u["id"]):
        if u.get("kind") != "defect" and u.get("kind") != "region_event" and u.get("kind") != "baseline":
            continue
        for a in u.get("anchors", ()):
            if a.get("sig64"):
                rows.append((u["id"], a["sig64"]))
    files = {"index/sig/ids.bin": "".join("%s\t%s\n" % r for r in rows).encode("ascii")}
    for k in range(4):
        pairs = sorted(((int(s, 16) >> (16 * k)) & 0xFFFF, i) for i, (_u, s) in enumerate(rows))
        files["index/sig/b%d.bin" % k] = b"".join(struct.pack("<HI", v, i) for v, i in pairs)
    return files


def golden_chain(units, tk):
    """GOLDEN baselines of one tk ordered by (build_seq or ingest order, id)."""
    g = [u for u in units if u.get("tk") == tk and u.get("pin") == "GOLDEN" and u.get("kind") == "baseline"
         and u.get("status") != "retired"]
    return sorted(g, key=lambda u: (_bs(u), u["id"]))


def appearance_chain(units, tk):
    """GOLDEN baselines of the tk plus its FG region_events carrying a look, ordered by (build_seq, id)."""
    g = [u for u in units if u.get("tk") == tk and u.get("status") != "retired" and u.get("look")
         and ((u.get("pin") == "GOLDEN" and u.get("kind") == "baseline")
              or (u.get("kind") == "region_event" and u.get("pin") is not None))]
    return sorted(g, key=lambda u: (_bs(u), u["id"]))


def _bs(u):
    b = u.get("scope", {}).get("build_seq")
    return int(u["id"][-6:]) if b is None else b


def build_tk_index(units):
    """-> {'index/tk.json': bytes} tk -> {golden, last_good, units}."""
    out = {}
    for u in units:
        out.setdefault(u["tk"], {"golden": [], "last_good": None, "units": []})["units"].append(u["id"])
    for tk, e in out.items():
        e["units"].sort()
        ch = [u for u in golden_chain(units, tk) if u.get("verdict") in (None, "PASS")]
        e["golden"] = [u["id"] for u in golden_chain(units, tk)]
        e["last_good"] = ch[-1]["id"] if ch else None
    return {"index/tk.json": canon.dumps(out).encode("utf-8")}


# ---------------------------------------------------------------- integer BM25F
def _log2_ppm(num, den):
    """~log2(num/den) in ppm, num >= den >= 1: bit_length + linear mantissa (deterministic, monotone)."""
    sh = num.bit_length() - den.bit_length()
    n, d = (num >> sh, den) if sh >= 0 else (num, den >> -sh)
    if n < d:
        n, sh = n * 2, sh - 1
    return sh * 1000000 + (n - d) * 1000000 // d


def bm25f(units, qterms):
    """-> {unit_id: score_ppm}; zones/weights per §8.2, k1=1200/1000, b=750/1000."""
    if not units or not qterms:
        return {}
    tfs, dls = {}, {}
    for u in units:
        tf, dl = {}, 0
        for zone, w in ZONES:
            for t in _tokens(u.get(zone, "")):
                tf[t] = tf.get(t, 0) + w
                dl += w
        tfs[u["id"]], dls[u["id"]] = tf, dl
    n = len(units)
    avg = max(1, sum(dls.values()) // n)
    out = {}
    for t in set(qterms):
        df = sum(1 for tf in tfs.values() if t in tf)
        if df == 0:
            continue
        idf = _log2_ppm(2 * n - df + 1, df) + 1  # >0 always
        for uid, tf in tfs.items():
            f = tf.get(t, 0)
            if f:
                num = f * (K1_M + 1000) * 1000 * avg
                den = f * 1000000 * avg + K1_M * ((1000 - B_M) * avg + B_M * dls[uid])
                out[uid] = out.get(uid, 0) + idf * num // den
    return out


# ---------------------------------------------------------------- snapshot view
class View:
    """Reader pinned to one generation; every verb ends with snap.validate() (lock-free isolation)."""

    def __init__(self, root):
        self.root = str(root)
        self.snap = Store(root).snapshot()
        self.gen = self.snap.gen
        self._units = None

    @property
    def units(self):
        if self._units is None:
            raw = self.snap.read("units/active.jsonl").decode("utf-8")
            self._units = [canon.loads(ln) for ln in raw.splitlines() if ln.strip()]
        return self._units

    def unit(self, uid):
        for u in self.units:
            if u["id"] == uid:
                return u
        raise VqaError("E_ID", {"reason": "unit_unknown", "id": str(uid)[:20]})

    def tk_index(self):
        try:
            return canon.loads(self.snap.read("index/tk.json").decode("utf-8"))
        except VqaError:
            return canon.loads(build_tk_index(self.units)["index/tk.json"].decode("utf-8"))

    def digest(self):
        try:
            return self.snap.read("VISUAL_DIGEST.md").decode("utf-8")
        except VqaError:
            return ""

    def finish(self):
        self.snap.validate()


def _scope_match(u, scope):
    if not scope:
        return True
    app, _, scene = scope.partition("/")
    s = u.get("scope", {})
    return s.get("app") == app and (not scene or s.get("scene") == scene)


def _check_grid(view, grid):
    if grid is None:
        return
    grids = {u.get("scope", {}).get("grid") for u in view.units}
    if grids and grid not in grids:
        raise VqaError("E_STALE", {"reason": "grid_mismatch", "grid": grid})


def _abstain(letter, **extra):
    d = {"abstain": ABSTAIN[letter], "letter": letter}
    d.update(extra)
    return VqaError("NOT_IN_VISUAL_MEMORY", d)


# ---------------------------------------------------------------- sig band index
def _read_pairs(view, k):
    b = view.snap.read("index/sig/b%d.bin" % k)
    return list(struct.iter_unpack("<HI", b))


def sig_lookup(view, sig, scan=False):  # @anchor:vm.router.sig_lookup
    """-> [(dist, unit_id)] sorted. Band index exact for d <= 3; scan=True: linear popcount over ids.bin up to d 4."""
    rows = [ln.split("\t") for ln in view.snap.read("index/sig/ids.bin").decode("ascii").splitlines() if ln]
    cand = {}
    if scan:
        for i, (_u, s) in enumerate(rows):
            cand[i] = None
        limit = SIG_HAM_MAX + 1
    else:
        limit = SIG_HAM_MAX
        for k in range(4):
            pairs = _read_pairs(view, k)
            keys = [p[0] for p in pairs]
            band = (sig >> (16 * k)) & 0xFFFF
            lo = bisect.bisect_left(keys, band)
            while lo < len(keys) and keys[lo] == band:
                cand[pairs[lo][1]] = None
                lo += 1
    hits = {}
    for i in cand:
        uid, s = rows[i]
        d = (int(s, 16) ^ sig).bit_count()
        if d <= limit and (uid not in hits or d < hits[uid]):
            hits[uid] = d
    return sorted((d, uid) for uid, d in hits.items())


# ---------------------------------------------------------------- look_eq (§3.2)
def _desc(root, sha):
    data = get_blob(root, sha, "bin")
    return [dict(zip(layout.TILE_FIELDS, t)) for t in struct.iter_unpack(layout.TILE_FMT, data)]


def look_eq(root, a, b, t0_q4=T0_Q4_DEFAULT):
    """-> 'SAME_EXACT' | 'SAME_NOISY' | 'DIFFERENT'; grid mismatch -> E_STALE (rc 4). One implementation: units.look_eq."""
    return U.look_eq(a, b, load_blob=lambda sha: get_blob(root, sha, "bin"), t0_q4=t0_q4)


# ---------------------------------------------------------------- verbs
def _row(u, score):
    return [u["id"], score, u["tk"], u.get("summary", "")]


def _fit(rows, k):
    """Shortlist <= SHORTLIST_B canonical bytes; drops tail rows (overflow count); even one row too big -> rc 6."""
    rows = rows[:k]
    total = len(rows)
    while rows:
        obj = {"abstain": {"flag": False, "reason": None}, "r": rows, "overflow": total - len(rows)}
        if len(canon.dumps(obj).encode("utf-8")) <= SHORTLIST_B:
            return obj
        if len(rows) == 1:
            break
        rows = rows[:-1]
    raise VqaError("E_BUDGET", {"reason": "shortlist_bytes", "cap": SHORTLIST_B})


def route(root, q=None, scope=None, sig=None, tk=None, k=5, scan=False, grid=None, build=None, view=None):  # @anchor:vm.router.route
    """V1 shortlist. Abstention first match wins: a scope, c build, d tk, e sig, f lexical (§8.3)."""
    if not 1 <= k <= 5:
        raise VqaError("E_USAGE", {"reason": "bad_k", "k": k})
    if not q and sig is None:
        raise VqaError("E_USAGE", {"reason": "q_or_sig_required"})
    v = view or View(root)
    _check_grid(v, grid)
    pool = [u for u in v.units if u.get("status") != "retired"]
    if scope:
        pool = [u for u in pool if _scope_match(u, scope)]
        if not pool:
            raise _abstain("a", scope=scope)
    if build is not None:
        bs = [_bs(u) for u in pool]
        if not bs or not min(bs) <= build <= max(bs):
            raise _abstain("c", build=build)
    if tk:
        if tk not in v.tk_index() or not any(u["tk"] == tk for u in pool):
            raise _abstain("d", tk=tk)
        pool = [u for u in pool if u["tk"] == tk]
    scores = {}
    if sig is not None:
        hits = dict((uid, d) for d, uid in sig_lookup(v, sig, scan))
        pool = [u for u in pool if u["id"] in hits]
        if not pool:
            raise _abstain("e")
        scores = {u["id"]: 1000000 - hits[u["id"]] * 100000 for u in pool}
    if q:
        lex = bm25f(pool, _tokens(q))
        if not lex:
            raise _abstain("f")
        scores = {uid: lex[uid] + scores.get(uid, 0) for uid in lex}
        pool = [u for u in pool if u["id"] in lex]
    pool.sort(key=lambda u: (-scores[u["id"]], u["id"]))
    out = _fit([_row(u, scores[u["id"]]) for u in pool], k)
    out["gen"] = v.gen
    v.finish()
    return out


def get(root, uid, view=None):
    v = view or View(root)
    u = v.unit(uid)
    v.finish()
    return u


def evidence(root, uid, view=None):
    """Anchors + blob paths (built from sha only)."""
    from .blobs import blob_path
    v = view or View(root)
    u = v.unit(uid)
    anchors = []
    for a in u.get("anchors", ()):
        p = {}
        if a.get("crop"):
            p["crop"] = blob_path(root, a["crop"], "png")
        if a.get("desc_blob"):
            p["desc_blob"] = blob_path(root, a["desc_blob"], "bin")
        anchors.append(dict(a, paths=p))
    v.finish()
    return {"id": uid, "anchors": anchors}


def _is_good(u):
    """verdict PASS; a GOLDEN baseline carries no verdict and counts as good."""
    return u.get("verdict") == "PASS" or (u.get("verdict") is None and u.get("kind") == "baseline")


def last_good(root, tk=None, subject=None, scope=None, view=None):
    v = view or View(root)
    if (tk is None) == (subject is None):
        raise VqaError("E_USAGE", {"reason": "tk_xor_subject"})
    pool = [u for u in v.units if _scope_match(u, scope)]
    if scope and not pool:
        raise _abstain("a", scope=scope)
    tks = {tk} if tk else {u["tk"] for u in pool if u.get("subject") == subject}
    if not tks or not any(u["tk"] in tks for u in pool):
        raise _abstain("d", tk=tk or subject)
    cands = []
    for t in sorted(tks):
        cands += [u for u in golden_chain(pool, t) if _is_good(u)]
    if not cands:
        raise _abstain("d", tk=tk or subject)
    best = max(cands, key=lambda u: (_bs(u), u["id"]))
    v.finish()
    return {"unit_id": best["id"], "tk": best["tk"], "build_seq": best["scope"].get("build_seq"),
            "cite": best["anchors"][0]["cite"] if best.get("anchors") else None, "summary": best.get("summary", "")}


def diff_region(root, tk, from_build=None, to_build=None, view=None):
    """First GOLDEN after the appearance at from_build whose look is DIFFERENT; bisect over build_seq, O(log G)."""
    v = view or View(root)
    pool = [u for u in v.units if u["tk"] == tk]
    if not pool:
        raise _abstain("d", tk=tk)
    chain = appearance_chain(pool, tk)
    if not chain:
        raise _abstain("d", tk=tk)
    keys = [_bs(u) for u in chain]
    lo_b = keys[0] if from_build is None else from_build
    hi_b = keys[-1] if to_build is None else to_build
    if lo_b > hi_b or hi_b < keys[0] or lo_b > keys[-1]:
        raise _abstain("c", build=lo_b)
    base = max(0, bisect.bisect_right(keys, lo_b) - 1)
    nxt = bisect.bisect_right(keys, lo_b)  # first golden strictly after the base build (appearances are distinct)
    nsrc = chain[0]["scope"].get("build_src")
    fkey = "first_run" if nsrc == "ingest_order" or chain[0]["scope"].get("build_seq") is None else "first_change"
    res = {"tk": tk, fkey: None}
    for i in range(nxt, len(chain)):
        if keys[i] > hi_b:
            break
        rel = look_eq(root, chain[base]["look"], chain[i]["look"])
        if rel == "DIFFERENT":
            u = chain[i]
            res[fkey] = {"build_seq": u["scope"].get("build_seq"), "run_id": u["anchors"][0]["run_id"],
                         "cite": u["anchors"][0]["cite"], "ah64_old": chain[base]["look"]["ah64"],
                         "ah64_new": u["look"]["ah64"], "class": u.get("class"), "look_eq": rel,
                         "pin": u.get("pin")}
            break
    # O9: without a driver render_index the cross-run alignment is per keyframe only
    aligned = all(a.get("render_index") is not None for u in chain for a in u["anchors"])
    res["granularity"] = "frame" if aligned else "keyframe"
    v.finish()
    return res


def seen_defect(root, sig, cls=None, scope=None, view=None):
    v = view or View(root)
    hits = []
    for d, uid in sig_lookup(v, sig):
        u = v.unit(uid)
        if u.get("layer") == "FG" and u.get("kind") in ("defect", "verdict") and _scope_match(u, scope) \
                and (cls is None or u.get("class") == cls):
            hits.append((d, uid))
    if not hits:
        raise _abstain("e")
    v.finish()
    return {"hits": [{"unit_id": uid, "dist": d} for d, uid in sorted(hits)]}


def check_answer(root, ans, view=None):
    """Every cite resolves in CURRENT (unit exists + cite among its anchors); every quote >= 12 chars is a substring of
    the unit's text fields (key, subject, summary, value). Failure -> NOT_IN_VISUAL_MEMORY reason check_failed (rc 3)."""
    v = view or View(root)
    for c in ans.get("citations", ()):
        try:
            u = v.unit(c.get("unit_id"))
        except VqaError:
            raise VqaError("E_CITE", {"reason": "unit_unresolved", "unit_id": str(c.get("unit_id"))[:20]})
        cites = {a.get("cite") for a in u.get("anchors", ())}
        if c.get("cite") not in cites:
            raise VqaError("E_CITE", {"reason": "cite_unresolved", "unit_id": u["id"]})
        quote = c.get("quote", "")
        text = "\n".join(str(u.get(f, "")) for f in ("key", "subject", "summary", "value"))
        if len(quote) < QUOTE_MIN_CHARS or quote not in text:
            raise _abstain("g", unit_id=u["id"], quote_len=len(quote))
    v.finish()
    return {"checked": len(ans.get("citations", ())), "gen": v.gen}


def answer(root, q=None, scope=None, sig=None, tk=None, max_tok=ANSWER_SOFT_TOK, grid=None):  # @anchor:vm.router.answer
    """Ladder V0 digest -> V1 shortlist -> V3 top unit -> V4 <= 2 crops. Levels are dropped from the bottom to fit max_tok;
    V0+V1 alone above ANSWER_HARD_TOK -> E_BUDGET rc 6."""
    v = View(root)
    sl = route(root, q=q, scope=scope, sig=sig, tk=tk, grid=grid, view=v)
    top = v.unit(sl["r"][0][0])
    digest = v.digest()
    dtok = min(DIGEST_TOK, est_tokens(len(digest.encode("utf-8"))))
    stok = est_tokens(len(canon.dumps(sl).encode("utf-8")))
    utok = est_tokens(len(canon.dumps(top).encode("utf-8")))
    crops = [a["crop"] for a in top.get("anchors", ()) if a.get("crop")][:2]
    ctok = 260 * len(crops)
    levels = [("V0", dtok), ("V1", stok), ("V3", utok), ("V4", ctok)]
    total = sum(t for _n, t in levels)
    kept = [n for n, _t in levels]
    while total > max_tok and len(kept) > 2:
        n = kept.pop()
        total -= dict(levels)[n]
    if total > ANSWER_HARD_TOK:
        raise VqaError("E_BUDGET", {"reason": "answer_tokens", "tok": total, "cap": ANSWER_HARD_TOK})
    quote = top.get("summary", "")
    cite = top["anchors"][0]["cite"] if top.get("anchors") else None
    ans = {"schema": "vmem.answer/v1", "status": "answered", "reason": None, "gen": v.gen, "granularity": (
               "frame" if top.get("anchors") and all(a.get("render_index") is not None
                                                      for a in top["anchors"]) else "keyframe"),
           "citations": [{"unit_id": top["id"], "cite": cite, "quote": quote}], "budget_tok": total,
           "levels": kept, "code": "ANSWERED", "rc": 0, "advisory": {"abstain": False, "cov_ppm": 0}}
    try:
        check_answer(root, ans, view=v)
    except VqaError as err:
        if err.rc == 3:
            raise _abstain("g", unit_id=top["id"])
        raise
    v.finish()
    return ans
