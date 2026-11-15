"""Oracle rows -> vqa.oracle.label/v1 labels (W6a lane O). Python 3 stdlib, integers only, no floats.

Exit codes: 0, 2 (usage/schema/io), 5 (episode cap); never 1.
The CLI writes the compact form (label_codec, W6a FX3 L5; codec v2 deflate text since FX4): same information, exact round-trip to canonical v1 JSONL;
the cap applies to the bytes written.
mv is ONE convention everywhere (CLASS_CONTRACT): the previous content translated by (dx, dy) px; scroll mv = [-dScrollX, -dScrollY]. Only classes 2/3/4 are emitted (JITTER, FROZEN, DRIFT, UNCHANGED never).
"""
import argparse
import functools
import json
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
from bench.wl import labels_v2 as L2  # noqa: E402  frame_of / ALIAS_FRAMES / exposed_strip are reused, not copied
from bench.oracle import label_codec as LC  # noqa: E402

SCHEMA = "vqa.oracle.label/v1"
KEYS = ("schema", "run_id", "event_id", "f0", "f1", "rects", "tiles", "ideal", "mv", "period_ms", "flags", "source", "source_ref", "confidence")
IDEALS = (2, 3, 4)
FLAGS = ("POINTER", "STRADDLE", "UNCOVERED", "UNOBSERVABLE")
SOURCES = ("scroll", "layoutshift", "mutation", "focus", "pointer", "window", "animation")
CAP = 100 * 1024
TILE_MIN, CELL_MAX, AXIS_MAX = 16, 4096, 255


def _isint(v):
    return type(v) is int


def grid_params(W, H):
    """-> (s, gw, gh), CLASS_CONTRACT grid rule (visioncore/layout.py:19-27)."""
    s = TILE_MIN
    while s <= 1 << 16:
        gw, gh = -(-W // s), -(-H // s)
        if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
            return s, gw, gh
        s <<= 1
    raise ValueError("grid")


def _spread(v):
    v &= 0xFF
    v = (v | (v << 4)) & 0x0F0F
    v = (v | (v << 2)) & 0x3333
    v = (v | (v << 1)) & 0x5555
    return v


def morton(tx, ty):
    return _spread(tx) | (_spread(ty) << 1)


@functools.lru_cache(maxsize=8)
def _order(W, H):
    s, gw, gh = grid_params(W, H)
    cells = sorted(((morton(tx, ty), tx, ty) for ty in range(gh) for tx in range(gw)))
    return {(tx, ty): r for r, (_, tx, ty) in enumerate(cells)}


def order_table(W, H):
    """Dense Morton rank order[(tx,ty)] in 0..gw*gh-1 (visioncore/grid.py:18-27)."""
    return dict(_order(W, H))


def rect_tiles(W, H, rect):
    """EXT rule: tile (tx,ty) intersects [x,y,w,h] iff tx*s < x+w and (tx+1)*s > x and ty*s < y+h and (ty+1)*s > y."""
    s, gw, gh = grid_params(W, H)
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return []
    t = _order(W, H)
    x0, x1 = max(0, x // s), min(gw - 1, (x + w - 1) // s)
    y0, y1 = max(0, y // s), min(gh - 1, (y + h - 1) // s)
    return sorted(t[(tx, ty)] for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1))


def straddle(rect, W, H):
    """True iff an interior rect edge (not on the frame border) is not a multiple of s: a tile there holds two regions."""
    s = grid_params(W, H)[0]
    x, y, w, h = rect
    for e, dim in ((x, W), (x + w, W), (y, H), (y + h, H)):
        if 0 < e < dim and e % s:
            return True
    return False


def clip_rect(r, W, H):
    """Clip [x,y,w,h] to the viewport [0,0,W,H]; None when nothing of it is inside (not paintable, nothing to label)."""
    x0, y0, x1, y1 = max(0, r[0]), max(0, r[1]), min(W, r[0] + r[2]), min(H, r[1] + r[3])
    return [x0, y0, x1 - x0, y1 - y0] if x1 > x0 and y1 > y0 else None


def _rect_ok(r):
    return isinstance(r, list) and len(r) == 4 and all(_isint(v) for v in r) and r[0] >= 0 and r[1] >= 0 and r[2] > 0 and r[3] > 0


def validate_label(l):
    if not isinstance(l, dict) or set(l) != set(KEYS):
        raise ValueError("label keys must be exactly %s" % (KEYS,))
    if l["schema"] != SCHEMA or not isinstance(l["run_id"], str):
        raise ValueError("schema/run_id")
    for k in ("event_id", "f0", "f1"):
        if not _isint(l[k]) or l[k] < 0:
            raise ValueError("%s must be a non-negative integer" % k)
    if l["f1"] <= l["f0"]:
        raise ValueError("f1 must exceed f0")
    if not isinstance(l["rects"], list) or not l["rects"] or not all(_rect_ok(r) for r in l["rects"]):
        raise ValueError("rects must be non-empty [x,y,w,h] integer lists")
    if not isinstance(l["tiles"], list) or not all(_isint(t) and t >= 0 for t in l["tiles"]):
        raise ValueError("tiles")
    if not _isint(l["ideal"]) or l["ideal"] not in IDEALS:
        raise ValueError("ideal %r not emittable (only 2,3,4)" % (l["ideal"],))
    mv = l["mv"]
    if mv is not None and not (isinstance(mv, list) and len(mv) == 2 and all(_isint(v) for v in mv)):
        raise ValueError("mv")
    if (l["ideal"] == 3) != (mv is not None):
        raise ValueError("mv is required iff ideal == 3 (MOVED)")
    if l["period_ms"] is not None and not _isint(l["period_ms"]):
        raise ValueError("period_ms")
    fl = l["flags"]
    if not isinstance(fl, list) or fl != sorted(set(fl)) or not set(fl) <= set(FLAGS):
        raise ValueError("flags")
    if l["source"] not in SOURCES or not (l["source_ref"] is None or isinstance(l["source_ref"], str)):
        raise ValueError("source/source_ref")
    if l["confidence"] not in ("exact", "derived"):
        raise ValueError("confidence")


def _row_ok(r):
    if not isinstance(r, dict):
        raise ValueError("sample row")
    for k in ("seq", "t_us", "sx", "sy", "frac"):
        if not _isint(r.get(k)):
            raise ValueError("sample %s must be an integer" % k)
    r.setdefault("shifts", [])
    r.setdefault("canv", [])
    r.setdefault("focus", None)
    if r.get("nodes") is not None:
        # ADC2-W6A-03: node row = [backendNodeId, x, y, w, h, visible, paint_sha]; a short or non-integer row is a schema error (rc 2), never an IndexError (rc 1)
        if not isinstance(r["nodes"], list) or not all(isinstance(n, list) and len(n) >= 7 and all(_isint(v) for v in n[1:5]) for n in r["nodes"]):
            raise ValueError("sample nodes must be [id,x,y,w,h,visible,sha] rows")


class _Emit:
    def __init__(self, W, H, run_id):
        self.W, self.H, self.run_id, self.out = W, H, run_id, []

    def add(self, f0, f1, rects, ideal, mv, flags, source, ref, conf, period_ms=None):
        rects = [c for c in (clip_rect(r, self.W, self.H) for r in rects) if c]
        if not rects:
            return
        fl = set(flags)
        if any(straddle(r, self.W, self.H) for r in rects):
            fl.add("STRADDLE")
        tiles = sorted({t for r in rects for t in rect_tiles(self.W, self.H, r)})
        lab = {"schema": SCHEMA, "run_id": self.run_id, "event_id": len(self.out), "f0": f0, "f1": f1, "rects": [list(r) for r in rects],
               "tiles": tiles, "ideal": ideal, "mv": mv, "period_ms": period_ms, "flags": sorted(fl), "source": source,
               "source_ref": ref, "confidence": conf}
        validate_label(lab)
        self.out.append(lab)


def derive(samples, ops, W, H, run_id, opts=None):
    """Consecutive sample pairs -> labels. exact iff every value is a CDP integer (frac == 0) AND the pair brackets exactly one
    actuator op (actuator-driven classes) or, for events with no actuator cause, adjacent samples (seq gap 0) within ALIAS_FRAMES."""
    o = opts or {}
    em = _Emit(W, H, run_id)
    ops = sorted(ops or [], key=lambda p: p["t_us"])
    for r in samples:
        _row_ok(r)
    nodes = (samples[0]["nodes"] or []) if samples else []
    for a, b in zip(samples, samples[1:]):
        prev, nodes = nodes, (b["nodes"] if b["nodes"] is not None else nodes)
        inb = [p for p in ops if a["t_us"] < p["t_us"] <= b["t_us"] and p.get("op") != "move"]
        fa, fb = L2.frame_of(a["t_us"]), L2.frame_of(b["t_us"])
        clean = not (a["frac"] or b["frac"])
        adjacent = b["seq"] == a["seq"] + 1 and fb - fa <= L2.ALIAS_FRAMES
        if inb:
            f0 = L2.frame_of(inb[0]["t_us"])
            f1 = f0 + 1
        else:
            f0, f1 = fa, fb + 1
        act_exact = clean and len(inb) == 1
        free_exact = clean and (len(inb) == 1 or (not inb and adjacent))
        dx, dy = b["sx"] - a["sx"], b["sy"] - a["sy"]
        if dx or dy:
            if o.get("smooth"):
                ex = clean and len(inb) <= 1 and adjacent and (bool(inb) or any(p["t_us"] <= b["t_us"] and p.get("op") != "move" for p in ops))
            else:
                ex = act_exact
            conf = "exact" if ex else "derived"
            if abs(dx) < W and abs(dy) < H:
                em.add(f0, f1, [[max(0, -dx), max(0, -dy), W - abs(dx), H - abs(dy)]], 3, [-dx, -dy], [], "scroll", "document", conf)
            strips = []
            if dy:
                strips.append(L2.exposed_strip(dy, W, H))
            if dx:
                d = min(W, abs(dx))
                strips.append([W - d, 0, d, H] if dx > 0 else [0, 0, d, H])
            em.add(f0, f1, strips, 2, None, [], "scroll", "document", conf)
        pm, cm = {n[0]: n for n in prev}, {n[0]: n for n in nodes}
        for bid in sorted(set(pm) & set(cm)):
            p, c = pm[bid], cm[bid]
            fl = [] if c[5] else ["UNOBSERVABLE"]
            conf = "exact" if free_exact else "derived"
            if p[1:5] == c[1:5]:
                if p[6] != c[6]:
                    canvas = bid in b["canv"]
                    em.add(f0, f1, [c[1:5]], 2, None, fl, "mutation", str(bid), "derived" if canvas else conf)
                continue
            mx, my = c[1] - p[1], c[2] - p[2]
            if p[3:5] == c[3:5]:
                em.add(f0, f1, [c[1:5]], 3, [mx, my], fl, "window", str(bid), conf)
                un = []
                if mx:
                    d = min(c[3], abs(mx))
                    un.append([p[1], p[2], d, p[4]] if mx > 0 else [c[1] + c[3], p[2], d, p[4]])
                if my:
                    d = min(c[4], abs(my))
                    un.append([p[1], p[2], p[3], d] if my > 0 else [p[1], c[2] + c[4], p[3], d])
                em.add(f0, f1, un, 2, None, ["UNCOVERED"] + fl, "window", str(bid), conf)
            else:
                x0, y0 = min(p[1], c[1]), min(p[2], c[2])
                x1, y1 = max(p[1] + p[3], c[1] + c[3]), max(p[2] + p[4], c[2] + c[4])
                em.add(f0, f1, [[x0, y0, x1 - x0, y1 - y0]], 2, None, fl, "mutation", str(bid), conf)
        for s in b["shifts"]:
            if s.get("ri"):
                continue  # Chromium suppresses layout-shift within 500 ms of input: never relied on there
            p, c = s["p"], s["c"]
            mv = [c[0] - p[0], c[1] - p[1]]
            if p[2:4] == c[2:4] and mv != [0, 0]:
                em.add(f0, f1, [c], 3, mv, [], "layoutshift", str(s["n"]), "exact" if free_exact else "derived")
            elif p != c:
                x0, y0 = min(p[0], c[0]), min(p[1], c[1])
                em.add(f0, f1, [[x0, y0, max(p[0] + p[2], c[0] + c[2]) - x0, max(p[1] + p[3], c[1] + c[3]) - y0]], 2, None, [],
                       "layoutshift", str(s["n"]), "exact" if free_exact else "derived")
    if o.get("caret_rect"):
        # caret blink needs a focused editable, not a focus change: an autofocus input is focused from sample 0 and never changes
        start = None
        for i, r in enumerate(samples + [None]):
            cur = r["focus"] if r is not None else None
            if start is not None and cur != samples[start]["focus"]:
                f0 = L2.frame_of(samples[start]["t_us"])
                f1 = max(f0 + 1, L2.frame_of(samples[i - 1]["t_us"]) + 1)
                em.add(f0, f1, [list(o["caret_rect"])], 4, None, [], "focus", samples[start]["focus"], "derived", o.get("period_ms"))
                start = None
            if start is None and cur is not None:
                start = i
    cp = o.get("cursor_px")
    last = None
    for p in ops:
        if p.get("op") == "move" and cp:
            if not (_isint(p.get("x")) and _isint(p.get("y"))):
                raise ValueError("move op needs integer x/y when --cursor-px is set")
            # ADC2-W6A-02: as labels_v2 (WL-7), plan point 0 is the start position, not a move; points 1..N are MOVED (ideal 3), mv = the pointer translation
            if p.get("point") != 0 and last is not None:
                f = L2.frame_of(p["t_us"])
                em.add(f, f + 1, [L2._box(p["x"], p["y"], cp, cp, W, H)], 3, [p["x"] - last[0], p["y"] - last[1]], ["POINTER"], "pointer", None, "derived")
            last = (p["x"], p["y"])
    return em.out


def smooth_gesture_sums(labels):
    """Per-gesture scrolled distance (-mv[1], mv is a translation) over MOVED scroll labels; a gap > 2 frames starts a new gesture (WL-1c: 122 each)."""
    sums, last = [], None
    for l in labels:
        if l["ideal"] != 3 or l["source"] != "scroll":
            continue
        if last is None or l["f0"] > last + 2:
            sums.append(0)
        sums[-1] -= l["mv"][1]
        last = l["f1"]
    return sums


def write_jsonl(path, labels, cap=CAP):
    """Atomic write of canonical JSONL; refuses (ValueError) before touching the file when over the episode cap."""
    for l in labels:
        validate_label(l)
    data = "".join(json.dumps(l, sort_keys=True, separators=(",", ":")) + "\n" for l in labels).encode()
    if len(data) > cap:
        raise ValueError("labels %d bytes exceed the %d byte episode cap" % (len(data), cap))
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lab")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data)


def write_compact(path, labels, cap=CAP):
    """Atomic write of the compact form (label_codec codec v2, FX4); refuses (ValueError) before touching the file when the v2 bytes exceed the cap."""
    for l in labels:
        validate_label(l)
    data = LC.encode_v2(labels)
    if LC.decode(data) != labels:
        raise RuntimeError("compact round-trip mismatch")
    if len(data) > cap:
        raise ValueError("compact labels %d bytes exceed the %d byte episode cap" % (len(data), cap))
    LC.write_atomic(path, data)
    return len(data)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="labels_oracle")
    ap.add_argument("--samples", required=True)
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--smooth", action="store_true")
    ap.add_argument("--caret-rect")
    ap.add_argument("--period-ms", type=int)
    ap.add_argument("--cursor-px", type=int)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    try:
        with open(a.samples) as f:
            d = json.load(f)
        opts = {"smooth": a.smooth, "period_ms": a.period_ms, "cursor_px": a.cursor_px}
        if a.caret_rect:
            opts["caret_rect"] = [int(v) for v in a.caret_rect.split(",")]
            if len(opts["caret_rect"]) != 4:  # ADC2-W6A-03: x,y,w,h exactly
                raise ValueError("--caret-rect needs 4 integers x,y,w,h")
        labs = derive(d["samples"], d.get("ops", []), a.w, a.h, a.run_id, opts)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        print("E_SCHEMA: %s" % e, file=sys.stderr)
        return 2
    try:
        write_compact(a.out, labs)
    except RuntimeError as e:
        print("E_CODEC: %s" % e, file=sys.stderr)
        return 5
    except LC.CodecError as e:
        print("E_CODEC: %s" % e, file=sys.stderr)
        return 2
    except ValueError as e:
        print("E_CAP: %s" % e, file=sys.stderr)
        return 5
    except OSError as e:
        print("E_IO: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
