"""Cross-check of oracle labels (vqa.oracle.label/v1 rows) against the prereg v3/v2 labels (lane X, W6a).

stdlib only; bench/wl labels_v3/labels_v2 imported read-only. Exit codes: 0 all agree, 2 usage/schema/io/empty,
5 disagreement or prereg pin mismatch; never 1. Only confidence "exact" oracle labels are matched (R5); "derived"
labels are counted in the support table and never agree. Match rule: same class (MOVED<->MOVED,
CHANGED_REGION|CHANGED<->CHANGED, NOT_CHANGED<->UNCHANGED, FLICKER<->FLICKER), |f0 - frame| <= ALIAS_FRAMES (VG-0061),
rect IoU == 1, MOVED mv == pixel translation [dx or 0, dy or 0] (prereg "content" scroll deltas are negated; pointer rows skip mv), CHANGED requires oracle mv null (AW6-09).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.join(os.path.dirname(HERE), "wl")
sys.path.insert(0, WL)
import labels_v2 as L2  # noqa: E402
import labels_v3 as L3  # noqa: E402
sys.path.insert(0, HERE)
import label_codec as LC  # noqa: E402  FX3 L5: compact labels files are decoded, v1 JSONL is read as is

ALIAS_FRAMES = L2.ALIAS_FRAMES
CLASS_OF = {"NOT_CHANGED": 0, "CHANGED": 2, "CHANGED_REGION": 2, "MOVED": 3, "FLICKER": 4}
CLASSES = [0, 1, 2, 3, 4, 5, 6]
V3_ITEMS = L3.ORDER
V2_ITEMS = ["WL-7", "WL-2b"]
REQ = ("f0", "f1", "rects", "ideal", "mv", "flags", "confidence")


class XCError(Exception):
    def __init__(self, msg, rc=2):
        Exception.__init__(self, msg)
        self.rc = rc


def _int(v):
    return type(v) is int


def _check_row(r, n):
    if not isinstance(r, dict) or any(k not in r for k in REQ):
        raise XCError("oracle row %d: missing keys" % n)
    if not (_int(r["f0"]) and _int(r["f1"]) and _int(r["ideal"])) or r["ideal"] not in CLASSES:
        raise XCError("oracle row %d: f0/f1/ideal must be ints, ideal in 0..6" % n)
    if not isinstance(r["rects"], list) or not r["rects"] or not all(
            isinstance(q, list) and len(q) == 4 and all(_int(x) for x in q) for q in r["rects"]):
        raise XCError("oracle row %d: rects must be non-empty [[x,y,w,h]] of ints" % n)
    mv = r["mv"]
    if mv is not None and not (isinstance(mv, list) and len(mv) == 2 and all(_int(x) for x in mv)):
        raise XCError("oracle row %d: mv must be null or [int,int]" % n)
    if not isinstance(r["flags"], list) or r["confidence"] not in ("exact", "derived"):
        raise XCError("oracle row %d: flags list / confidence exact|derived" % n)
    if r.get("period_ms") is not None and not _int(r["period_ms"]):
        raise XCError("oracle row %d: period_ms must be int or null" % n)


def _check_rows(rows):
    if not rows:
        raise XCError("oracle input is empty (no vacuous green)")
    for n, r in enumerate(rows):
        _check_row(r, n)


def load_oracle(path, allow_empty=False):
    """v1 JSONL, or the compact form (first line a label_codec header: v1-compact or v2) decoded to the identical v1 rows."""
    rows = []
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
        head = json.loads(lines[0]) if lines else None
        if isinstance(head, dict) and "codec" in head:
            rows = LC.decode(data)  # FX4: the v2 base64 line is not JSON, so only the header line is parsed here
        else:
            rows = [json.loads(ln) for ln in lines]
    except (OSError, ValueError) as e:
        raise XCError("oracle io: %s" % e)
    if rows or not allow_empty:
        _check_rows(rows)
    return rows


def untested_report(item, prereg):
    """0 oracle labels: nothing to cross-check, reported as untested (never as agreement)."""
    pre_n = {c: 0 for c in CLASSES}
    for p in prereg:
        pre_n[CLASS_OF[p["expected_kind"]]] += 1
    return {"item": item, "result": "untested", "oracle_rows": 0, "agree": 0, "oracle_only": 0, "prereg_only": 0, "prereg": len(prereg),
            "oracle_derived": 0, "discrepancies": [],
            "support": [{"class": c, "oracle_exact": 0, "oracle_derived": 0, "prereg": pre_n[c], "agree": 0, "status": "untested"} for c in CLASSES]}


def _manifest(item):
    name, want = ("prereg_wl_v2.json", L3.PARENT_SHA256) if item in V2_ITEMS else ("prereg_wl_v3.json", L3.PINNED_PREREG_V3_SHA256)
    try:
        with open(os.path.join(WL, name)) as fh:
            m = json.load(fh)
    except (OSError, ValueError) as e:
        raise XCError("prereg io: %s" % e)
    if L2.prereg_sha256(m) != want:
        raise XCError("prereg canonical sha mismatch for %s" % name, 5)
    return m


def prereg_rows(item, w=1920, h=1080, act_rows=None):
    if item not in V3_ITEMS and item not in V2_ITEMS:
        raise XCError("unknown item %s" % item)
    m = _manifest(item)
    mod = L2 if item in V2_ITEMS else L3
    try:
        return mod.labels_for(m, item, w, h) if act_rows is None else mod.labels_from_act(m, item, w, h, act_rows)
    except Exception as e:  # WLError carries its own rc
        raise XCError("prereg labels: %s" % e, getattr(e, "rc", 5))


def grid_s(w, h):
    s = 16
    while True:
        gw, gh = -(-w // s), -(-h // s)
        if gw * gh <= 4096 and max(gw, gh) <= 255:
            return s
        s *= 2


def _iou_one(a, b):
    """IoU == 1 iff the rects coincide (inter == union > 0), integer arithmetic."""
    iw = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    ih = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    inter = iw * ih if iw > 0 and ih > 0 else 0
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter > 0 and inter == union


def _overlap(a, b):
    return min(a[0] + a[2], b[0] + b[2]) > max(a[0], b[0]) and min(a[1] + a[3], b[1] + b[3]) > max(a[1], b[1])


def _ok(p, e):
    c = CLASS_OF[p["expected_kind"]]
    if e["ideal"] != c or abs(e["f0"] - p["frame"]) > ALIAS_FRAMES:
        return False
    if not any(_iou_one(p["rect"], q) for q in e["rects"]):
        return False
    if c == 3 and p.get("target") != "pointer":
        k = -1 if p.get("target") == "content" else 1  # prereg scroll dx/dy are scroll-offset deltas; oracle mv is a translation
        return e["mv"] == [k * (p.get("dx") or 0), k * (p.get("dy") or 0)]
    if c == 2:
        return e["mv"] is None
    if c == 4 and p.get("period_ms") is not None:
        return e.get("period_ms") == p["period_ms"]
    return True


def compare(item, oracle_rows, prereg, w=1920, h=1080):
    _check_rows(oracle_rows)
    if not prereg:
        raise XCError("prereg rows are empty (no vacuous green)")
    exact = [e for e in oracle_rows if e["confidence"] == "exact"]
    used = set()
    agree = {c: 0 for c in CLASSES}
    pre_n = {c: 0 for c in CLASSES}
    prereg_only = 0
    pairs = []
    for p in prereg:
        c = CLASS_OF[p["expected_kind"]]
        pre_n[c] += 1
        if c == 0:  # surround: no exact non-zero oracle label may overlap it
            if any(e["ideal"] != 0 and any(_overlap(p["rect"], q) for q in e["rects"]) for e in exact):
                prereg_only += 1
            else:
                agree[0] += 1
            continue
        best = None
        for i, e in enumerate(exact):
            if i not in used and _ok(p, e):
                d = abs(e["f0"] - p["frame"])
                if best is None or d < best[0]:
                    best = (d, i)
        if best is None:
            prereg_only += 1
        else:
            used.add(best[1])
            agree[c] += 1
            pairs.append((p, exact[best[1]]))
    oracle_only = len(exact) - len(used)
    support = []
    for c in CLASSES:
        ex = sum(1 for e in oracle_rows if e["ideal"] == c and e["confidence"] == "exact")
        de = sum(1 for e in oracle_rows if e["ideal"] == c and e["confidence"] == "derived")
        support.append({"class": c, "oracle_exact": ex, "oracle_derived": de, "prereg": pre_n[c], "agree": agree[c],
                        "status": "untested" if ex + de + pre_n[c] == 0 else "tested"})
    s = grid_s(w, h)
    disc = []
    unc = [(p, e) for p, e in pairs if p.get("target") == "uncovered"]
    if unc:
        disc.append({"kind": "uncovered_changed_flag", "count": sum(1 for _, e in unc if "UNCOVERED" in e["flags"]),
                     "prereg_class": "CHANGED_REGION", "oracle_class": "CHANGED+UNCOVERED"})
    edges = set()
    for p in prereg:
        if p.get("target") == "pane":
            x, y, rw, rh = p["rect"]
            edges.update(v for v in (x, x + rw, y, y + rh) if v % s)
    if edges:
        disc.append({"kind": "straddle_edge", "edges": sorted(edges), "tile": s,
                     "oracle_straddle_flags": sum(1 for e in oracle_rows if "STRADDLE" in e["flags"])})
    sub = sum(1 for p in prereg if p.get("target") == "pointer" and min(p["rect"][2], p["rect"][3]) < s)
    if sub:
        disc.append({"kind": "subtile_pointer", "count": sub, "tile": s})
    return {"item": item, "agree": sum(agree.values()), "oracle_only": oracle_only, "prereg_only": prereg_only,
            "oracle_derived": sum(1 for e in oracle_rows if e["confidence"] == "derived"),
            "support": support, "discrepancies": disc}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crosscheck.py")
    ap.add_argument("--item", required=True)
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--act")
    ap.add_argument("--w", type=int, default=1920)
    ap.add_argument("--h", type=int, default=1080)
    a = ap.parse_args(argv)  # argparse usage errors exit 2
    try:
        act = L2.read_act(a.act) if a.act else None
        rows = load_oracle(a.oracle, allow_empty=True)
        pre = prereg_rows(a.item, a.w, a.h, act)  # unknown item / prereg pin mismatch still fail first
        rep = untested_report(a.item, pre) if not rows else compare(a.item, rows, pre, a.w, a.h)
    except Exception as e:  # XCError / WLError carry rc (2 or 5)
        sys.stderr.write("crosscheck: %s\n" % e)
        return getattr(e, "rc", 2) if getattr(e, "rc", 2) != 1 else 5
    sys.stdout.write(json.dumps(rep, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if rep["prereg_only"] == 0 and rep["oracle_only"] == 0 else 5


if __name__ == "__main__":
    sys.exit(main())
