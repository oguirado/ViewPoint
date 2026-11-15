#!/usr/bin/env python3
"""HB2-12: the one query tool. hb_query.py [--class FIND|ABSENT|IMPACT|CLAIM|auto] "<text>" -> one `hb.answer.v2` JSON line (<= 2,000 B).

FIND/ABSENT: symbol-first integer BM25F with a calibrated abstention rule (HB2-03); CLAIM: receipt check (HB2-07);
IMPACT: closure + edit context (HB2-04, hb_impact.py). Class `auto` is inferred: `file.json:key` => CLAIM, a change verb or the
HB2-04 word list => IMPACT, else FIND (ABSENT is the abstaining FIND answer).
"""
import argparse, json, os, re, sys
from fractions import Fraction

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_index as I  # noqa: E402
import hb_claim as C  # noqa: E402
import hb_pack as P  # noqa: E402

TOOL = "hb_query.py/2.0"
SIG_WORDS = {"signature", "parameter", "argument", "rename", "return", "type", "interface", "schema", "field", "format", "envelope"}
CHANGE_VERBS = {"change", "modify", "revise", "refactor", "replace", "update", "extend", "add", "remove", "fix", "rewrite", "alter", "adjust", "edit"}
REF_RE = C.REF_RE
SENTINEL = 10 ** 6 + 1
MAX_ROWS = 12


class Hyper:
    """A loaded hyperbook: index, symbols, params, graph."""

    def __init__(self, root=None, params_path=None, index_dir=None):
        self.root = root or L.HB
        self.book = os.path.join(self.root, L.BOOK_SUB)
        self.subject = L.subject_of(self.root)
        self.godot = os.path.join(self.root, "godot_fixture", "project")
        self.ix = I.Index(index_dir or os.path.join(self.book, "index"))
        self.symbols = {r["id"]: r for r in L.read_jsonl(os.path.join(self.book, "symbols.jsonl"))}
        pp = params_path or os.path.join(self.root, "params.json")
        raw = L.readb(pp) if os.path.isfile(pp) else b""
        self.params_raw = raw
        self.pr = L.sha16(raw) if raw else "0" * 16
        self.params = json.loads(raw.decode("utf-8")) if raw else {}
        self.find = dict(I.DEFAULT_FIND, **self.params.get("find", {}))
        self.impact = dict(I.DEFAULT_IMPACT, **self.params.get("impact", {}))
        self.abstain = self.params.get("abstain")           # None => uncalibrated
        self._graph = None
        self._text = {}

    def rule_text(self, rid):
        """Text of an INV-/AU- rule from the book blocks (cached)."""
        if not hasattr(self, "_rules"):
            import hb_build
            pb = hb_build.parse_book(open(os.path.join(self.book, "book.md"), encoding="utf-8").read())
            self._rules = {it["id"]: it["text"] for ib in pb["inv"] for it in ib["inv"] + ib["au"]}
        return "%s: %s" % (rid, self._rules.get(rid, ""))

    def graph(self):
        if self._graph is None:
            import hb_extract2 as X
            self._graph = X.Graph(L.jload(os.path.join(self.book, "graph.json")))
        return self._graph

    def source(self, sid):
        """Exact source bytes of a symbol (None when its file is not available)."""
        s = self.symbols[sid]
        p = s["path"]
        fp = os.path.join(self.godot, p.split("/", 1)[1]) if p.startswith("godot/") else os.path.join(self.subject, p)
        if fp not in self._text:
            self._text[fp] = L.readb(fp) if os.path.isfile(fp) else b""
        return self._text[fp][s["span"]["start_byte"]:s["span"]["end_byte"]]


def infer_class(text):
    if REF_RE.search(text):
        return "CLAIM"
    w = set(L.toks(text))
    if w & (SIG_WORDS | CHANGE_VERBS):
        return "IMPACT"
    return "FIND"


def triptych(h, sid):
    """<= 450 B: id | sig | purpose | INV ids | AU ids | receipt status."""
    s = h.symbols[sid]
    g = s["governs"]
    purpose = (s.get("card") or {}).get("purpose", "")
    status = "fresh" if g.get("tests") or g.get("receipts") else "none"
    t = " | ".join([sid, L.clip_bytes(s["sig"], 160), L.clip_bytes(purpose, 160), "INV " + (",".join(g["inv"]) or "-"), "AU " + (",".join(g["au"]) or "-"), status])
    return L.clip_bytes(t, 450)


def slice_text(h, sid):
    s = h.symbols[sid]
    if s.get("skeleton"):
        return s["skeleton"]
    raw = h.source(sid)
    if raw and len(raw) <= 1600:
        return raw.decode("utf-8", "replace")
    return triptych(h, sid)


def find_rows(h, qterms, cfg):
    S = h.ix.scores(qterms, cfg)
    return S, h.ix.order(S)


def decide(h, m, cov, oov, has_terms, has_rows):
    """-> (flag, reason). HB2-03: abstain iff m < m_min or cov < tau_cov or oov >= tau_oov; null tau => uncalibrated (no abstention)."""
    if not has_terms:
        return True, "no_query_term"
    if not has_rows:
        return True, "low_term_match"
    ab = h.abstain
    if not ab or ab.get("tau_cov") is None or ab.get("tau_oov") is None:
        return False, "uncalibrated"
    if m < ab["m_min"]:
        return True, "low_term_match"
    if cov < ab["tau_cov"]:
        return True, "below_tau"
    if oov >= ab["tau_oov"]:
        return True, "oov"
    return False, "none"


def envelope(h, q, cls, abstain, rows, extra=None, overflow=False):
    e = {"schema": "hb.answer.v2", "tool": TOOL, "ix": h.ix.ix16, "pr": h.pr, "q": L.sha16(q.encode("utf-8")), "class": cls,
         "abstain": abstain, "r": rows}
    if extra:
        e.update(extra)
    if overflow:
        e["overflow"] = True
    return e


def pack_rows(h, q, cls, abstain, cand, S, extra=None):
    """cand: ranked doc ids. Items a/b/g per D-HB2-11; real serialisation <= 2,000 B; refuses (E_BUDGET) rather than truncate."""
    items, score = [], {}
    for d in cand[:MAX_ROWS]:
        sid = h.ix.ids[d]
        score[sid] = S.get(d, 0)
        items.append({"id": sid, "a": slice_text(h, sid), "b": triptych(h, sid), "g": ""})

    def to_rows(rows):
        return [[sid, score[sid], lv] + ([txt] if lv != "g" else []) for sid, lv, txt in rows]
    render = lambda rows: envelope(h, q, cls, abstain, to_rows(rows), extra)
    try:
        r = P.pack(items, L.ANSWER_C, L.ANSWER_H, render)
    except P.BudgetError:
        return envelope(h, q, cls, abstain, [], extra, overflow=True), True
    overflow = len(r["rows"]) < len(items)
    return envelope(h, q, cls, abstain, to_rows(r["rows"]), extra, overflow=False), overflow


def answer(h, q, cls="auto"):
    if cls == "auto":
        cls = infer_class(q)
    if cls == "CLAIM":
        return answer_claim(h, q)
    if cls == "IMPACT":
        import hb_impact
        return hb_impact.answer_impact(h, q)
    qterms = L.toks(q)
    S, order = find_rows(h, qterms, h.find)
    m, cov, oov = h.ix.features(qterms, order, h.find)
    flag, reason = decide(h, m, cov, oov, bool(qterms), bool(order))
    ab = {"flag": flag, "reason": reason, "m": m, "cov": cov, "oov": oov}
    cand = order[:3] if flag else order
    ans, _ = pack_rows(h, q, "ABSENT" if flag else "FIND", ab, cand, S)
    return ans


def answer_claim(h, q):
    c = C.check_prose(q, None, h.subject)
    ab = {"flag": False, "reason": "none", "m": 0, "cov": 0, "oov": 0}
    if c is None:
        cl = {"verdict": "unverifiable", "value": None, "code": "E_RECEIPT_KEY"}
    else:
        cl = {"verdict": c["verdict"], "value": c["receipt"].get("value"), "code": c["code"]}
    return envelope(h, q, "CLAIM", ab, [], {"claim": cl})


# ---- X2 fit (HB2-03) -------------------------------------------------------------------------------------
def f1_frac(tp, fp, fn):
    return Fraction(2 * tp, 2 * tp + fp + fn) if (2 * tp + fp + fn) else Fraction(0)


def thresholds(vals):
    s = sorted(set(vals))
    return [0] + [(a + b) // 2 for a, b in zip(s, s[1:])] + [SENTINEL]


def fit_x2(rows, floor=Fraction(4, 5)):
    """rows: [(fixed_abstain, m, cov, oov, answerable)]. Returns {m_min, tau_cov, tau_oov, f1, abstained_answerable} or None.
    Positive class = ABSENT (answerable False). Constraint: pass-through on answerable rows >= floor. Ties: fewer answerable
    abstentions, smaller m_min, larger tau_oov, smaller tau_cov. None (tau null, reason `uncalibrated`) when a class is empty."""
    an = [r for r in rows if r[4]]
    un = [r for r in rows if not r[4]]
    if not an or not un:
        return None
    tcs = thresholds([r[2] for r in rows if not r[0]])
    tos = thresholds([r[3] for r in rows if not r[0]])
    best = None
    for mm in (1, 2):
        for tc in tcs:
            for to in tos:
                def ab(r):
                    return True if r[0] else (r[1] < mm or r[2] < tc or r[3] >= to)
                a_ab = sum(1 for r in an if ab(r))
                if Fraction(len(an) - a_ab, len(an)) < floor:
                    continue
                tp = sum(1 for r in un if ab(r))
                f1 = f1_frac(tp, a_ab, len(un) - tp)
                key = (-f1, a_ab, mm, -to, tc)
                if best is None or key < best[0]:
                    best = (key, dict(m_min=mm, tau_cov=tc, tau_oov=to, f1=f1, abstained_answerable=a_ab, tp=tp))
    return best[1] if best else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="cls", default="auto", choices=["FIND", "ABSENT", "IMPACT", "CLAIM", "auto"])
    ap.add_argument("--root", default=L.HB)
    ap.add_argument("--params")
    ap.add_argument("--index")
    ap.add_argument("--ablate", choices=["nocard", "nosec", "b1fields"])
    ap.add_argument("text")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    try:
        h = Hyper(a.root, a.params, a.index)
    except (OSError, ValueError, KeyError):
        sys.stderr.write("index or params missing/invalid\n")
        return 3
    cls = "FIND" if a.cls == "ABSENT" else a.cls
    if a.ablate:
        h.find = dict(h.find, ablate=a.ablate)
    ans = answer(h, a.text, cls)
    sys.stdout.write(L.dumps(ans) + "\n")
    if ans.get("overflow") and not ans["r"]:
        sys.stderr.write("E_BUDGET\n")
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
