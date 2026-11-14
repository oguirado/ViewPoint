"""WL prereg v3 loader + weak labels (W4 lane V).

stdlib only. Exit codes: 0, 2 (usage/schema/io), 5 (scene/font pin, plan divergence); never 1. v1/v2 stay frozen;
labels_v2 is imported read-only. Items WL-1s/1c/1L/1h/4/3 (WL-7 stays in v2). Label enum as v2. Labels carry the TRUE dx/dy of the
scene tables and the actuator t_us (label_timebase "actuator"), never anything from engine output. Frame of an event =
floor(t_us*60/1e6), half-open windows (VG-0053 Thm 2).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labels_v2 as L2  # noqa: E402
import recorder  # noqa: E402

WLError = recorder.WLError
SCHEMA = "os.cu.prereg-wl/v3"
PARENT_SHA256 = "b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12"  # canonical prereg v2
PINNED_PREREG_V3_SHA256 = "15be6e44b486a310a429ac42c902f7f9ba0d7ae5ea98a7471e4e7c3fb4e8cb20"  # canonical sha of bench/wl/prereg_wl_v3.json; driver default want for v3
FPS = 60
ORDER = ["WL-1s", "WL-1c", "WL-1L", "WL-1h", "WL-4", "WL-3"]
TOP_KEYS = {"schema", "parent_prereg", "fps", "segment_min_ms", "det_sha256", "resolutions", "capture_order", "font",
            "items", "item_ms", "kept_max_mib", "label_timebase"}
ITEM_REQ = {"id", "name", "scene_file", "scene_sha256", "font_sha256", "xvfb_cursor", "steps"}
ITEM_OPT = {"dy_seq", "dx_seq", "dy_table", "align_frames", "viewport_h", "page_dy", "div_rect0", "pane_rect", "seed", "canvas"}
STEP_KEYS = {"t_ms", "op", "at_px", "dy", "dx", "keysym", "fps", "path"}
OPS = {"scroll", "hscroll", "key", "drag", "wait"}
FORBIDDEN_SCENE = (b"Math.random", b"Date", b"performance.now", b"http://", b"https://", b"fetch(", b"XMLHttpRequest")
SMALL_DY = [4, 8, 16, 24]
LARGE_DY = 200
HDX = [40, 120]
SETTLE_MS = 500
TAB_N_MIN, TAB_N_MAX, TAB_MIN, TAB_MAX = 18, 30, 2, 8


def dy_table(n):
    """Integer velocity envelope of Q16 smootherstep (VG-0035 s4.1): v_k = 2 + floor(96*t^2*(1-t)^2 + 1/2), t=(2k+1)/(2n)
    in Q16, exact in Python ints. Every v_k is in [2,8] (a pure S2 position step is 0/1 px at the ends, never >= 2)."""
    out = []
    for k in range(n):
        t = ((2 * k + 1) << 16) // (2 * n)
        w = t * (65536 - t)
        out.append(2 + ((96 * w * w + (1 << 63)) >> 64))
    return out


def prereg_sha256(m):
    return L2.prereg_sha256(m)


def drag_t_us(t_ms, j):
    """Drift-free 60 Hz grid: point j at t_ms*1000 + floor(j*1e6/60) (VG-0053 F_60(j))."""
    return t_ms * 1000 + (j * 1000000) // FPS


def _in_screen(p, w, h):
    return isinstance(p, list) and len(p) == 2 and 0 <= p[0] < w and 0 <= p[1] < h


def _in_rect(r, w, h):
    return len(r) == 4 and r[2] > 0 and r[3] > 0 and r[0] >= 0 and r[1] >= 0 and r[0] + r[2] <= w and r[1] + r[3] <= h


def _steps(it, op):
    return [s for s in it["steps"] if s["op"] == op]


def _check_item(it, m, w, h):
    iid, st = it["id"], it["steps"]
    for s in st:
        if "at_px" in s and not _in_screen(s["at_px"], w, h):
            raise WLError("E_SCHEMA: at_px outside the scene")
    if iid in ("WL-1s", "WL-1c", "WL-1L", "WL-1h"):
        vh = it.get("viewport_h", h)
        if vh != h and iid != "WL-1h":
            raise WLError("E_SCHEMA: viewport_h must equal the resolution height")
    if iid == "WL-1s":
        sc = _steps(it, "scroll")
        dys = [s.get("dy") for s in sc]
        if len(sc) != 12 or len(st) != 12 or it.get("dy_seq") != dys or sorted(dys) != sorted(SMALL_DY * 3):
            raise WLError("E_SCHEMA: WL-1s needs 12 scroll steps dy in {4,8,16,24} x3 equal to dy_seq")
        if any(b["t_ms"] - a["t_ms"] < 400 for a, b in zip(sc, sc[1:])):
            raise WLError("E_SCHEMA: WL-1s spacing < 400 ms")
    elif iid == "WL-1c":
        tab = it.get("dy_table")
        sc = _steps(it, "scroll")
        if not isinstance(tab, list) or not TAB_N_MIN <= len(tab) <= TAB_N_MAX or any(not TAB_MIN <= v <= TAB_MAX for v in tab):
            raise WLError("E_SCHEMA: WL-1c dy_table needs N in [18,30] steps of 2..8 px")
        if tab != dy_table(len(tab)) or it.get("align_frames") != 1:
            raise WLError("E_SCHEMA: WL-1c dy_table must be the Q16 smootherstep envelope, align_frames 1")
        if len(sc) != 3 or len(st) != 3 or any(s.get("dy") != sum(tab) for s in sc):
            raise WLError("E_SCHEMA: WL-1c needs 3 scroll triggers with dy = sum(dy_table)")
        if any((b["t_ms"] - a["t_ms"]) * FPS < (len(tab) + 6) * 1000 for a, b in zip(sc, sc[1:])):
            raise WLError("E_SCHEMA: WL-1c gestures overlap")
    elif iid == "WL-1L":
        sc, ks = _steps(it, "scroll"), _steps(it, "key")
        page = it.get("page_dy")
        if len(sc) != 3 or len(ks) != 3 or len(st) != 6 or any(s.get("dy") != LARGE_DY for s in sc):
            raise WLError("E_SCHEMA: WL-1L needs 3 scroll dy 200 + 3 key steps")
        if type(page) is not int or not 0 < page < h or any(s.get("keysym") != "PageDown" or s.get("dy") != page for s in ks):
            raise WLError("E_SCHEMA: WL-1L PageDown steps need keysym PageDown and dy = page_dy < viewport_h")
    elif iid == "WL-1h":
        hs = _steps(it, "hscroll")
        dxs = [s.get("dx") for s in hs]
        if len(hs) != 6 or len(st) != 6 or it.get("dx_seq") != dxs or sorted(dxs) != sorted(HDX * 3):
            raise WLError("E_SCHEMA: WL-1h needs 6 hscroll steps dx in {40,120} x3 equal to dx_seq")
    elif iid == "WL-4":
        ds = _steps(it, "drag")
        r0 = it.get("div_rect0")
        if len(ds) != 2 or len(st) != 2 or not isinstance(r0, list) or not _in_rect(r0, w, h):
            raise WLError("E_SCHEMA: WL-4 needs 2 drag steps and an in-screen div_rect0")
        x, y = r0[0], r0[1]
        for i, s in enumerate(ds):
            if s.get("fps") != FPS or not s.get("path") or any(not _in_screen(p, w, h) for p in s["path"]):
                raise WLError("E_SCHEMA: WL-4 drag needs fps 60 and an on-screen path")
            prev = s["at_px"]
            for p in s["path"]:
                x, y = x + p[0] - prev[0], y + p[1] - prev[1]
                prev = p
                if not _in_rect([x, y, r0[2], r0[3]], w, h):
                    raise WLError("E_SCHEMA: WL-4 div leaves the screen")
            if any(p[1] != s["at_px"][1] for p in s["path"]):
                raise WLError("E_SCHEMA: WL-4 path must be horizontal")
        n1 = len(ds[0]["path"])
        if drag_t_us(ds[0]["t_ms"], n1 + 1) + SETTLE_MS * 1000 > ds[1]["t_ms"] * 1000:
            raise WLError("E_SCHEMA: WL-4 needs >= 500 ms settle between drag steps")
        if [len(s["path"]) for s in ds] != [60, 30]:
            raise WLError("E_SCHEMA: WL-4 needs 60 moves then 30 moves")
        for s, d in zip(ds, (3, 20)):
            if any(b[0] - a[0] != d for a, b in zip([s["at_px"]] + s["path"], s["path"])):
                raise WLError("E_SCHEMA: WL-4 phase step is not constant")
    elif iid == "WL-3":
        pr, cv = it.get("pane_rect"), it.get("canvas")
        if not (isinstance(pr, list) and _in_rect(pr, w, h) and cv == pr[2:]):
            raise WLError("E_SCHEMA: WL-3 pane_rect must be in-screen and equal canvas size")
        if type(it.get("seed")) is not int or len(st) != 1 or st[0]["op"] != "wait":
            raise WLError("E_SCHEMA: WL-3 needs an integer seed and one wait step")


def check_manifest(m, base):
    L2._closed(m, TOP_KEYS, set(), "manifest")
    L2._no_non_int({k: v for k, v in m.items() if k not in ("schema", "parent_prereg", "capture_order", "font", "items", "label_timebase")})
    if m["schema"] != SCHEMA or m["parent_prereg"] != PARENT_SHA256 or m["label_timebase"] != "actuator":
        raise WLError("E_SCHEMA: schema/parent_prereg/label_timebase")
    if m["fps"] != FPS or m["segment_min_ms"] < 5000 or m["det_sha256"] != 1 or m["resolutions"] != [[1920, 1080]] \
            or m["item_ms"] != 6000 or not 0 < m["kept_max_mib"] <= 2400:
        raise WLError("E_SCHEMA: fps/segment/det/resolutions/item_ms/kept_max_mib")
    w, h = m["resolutions"][0]
    f = m["font"]
    L2._closed(f, L2.FONT_KEYS, set(), "font")
    L2._no_non_int({k: v for k, v in f.items() if k not in ("font_file", "font_sha256")}, "$.font")
    L2._pinned(base, f["font_file"], f["font_sha256"], "font")
    ids = []
    for it in m["items"]:
        L2._closed(it, ITEM_REQ, ITEM_OPT, "item")
        L2._no_non_int({k: v for k, v in it.items() if k not in ("id", "name", "scene_file", "scene_sha256", "font_sha256", "steps")}, "$.item")
        if it["xvfb_cursor"] != 0 or it["font_sha256"] != f["font_sha256"]:
            raise WLError("E_SCHEMA: xvfb_cursor must be 0 / font_sha256")
        L2._pinned(base, it["scene_file"], it["scene_sha256"], "scene " + str(it["id"]))
        with open(os.path.join(base, it["scene_file"]), "rb") as fh:
            src = fh.read()
        for tok in FORBIDDEN_SCENE:
            if tok in src:
                raise WLError("E_SCENE: forbidden token %s in %s" % (tok.decode(), it["scene_file"]), 5)
        for s in it["steps"]:
            L2._closed(s, {"t_ms", "op"}, STEP_KEYS, "step")
            L2._no_non_int({k: v for k, v in s.items() if k not in ("op", "keysym")}, "$.step")
            if s["op"] not in OPS or (s["op"] == "key" and s.get("keysym") != "PageDown"):
                raise WLError("E_SCHEMA: op/keysym")
            if s["op"] in ("scroll", "hscroll", "drag") and "at_px" not in s:
                raise WLError("E_SCHEMA: %s needs at_px" % s["op"])
            if s["op"] == "scroll" and s.get("dy", 0) <= 0 or s["op"] == "hscroll" and s.get("dx", 0) == 0:
                raise WLError("E_SCHEMA: scroll dy > 0 / hscroll dx != 0")
        if any(b["t_ms"] < a["t_ms"] for a, b in zip(it["steps"], it["steps"][1:])):
            raise WLError("E_SCHEMA: t_ms must be non-decreasing")
        _check_item(it, m, w, h)
        ids.append(it["id"])
    if m["capture_order"] != ORDER or sorted(ids) != sorted(ORDER):
        raise WLError("E_SCHEMA: capture_order must be %s" % ",".join(ORDER))
    return m


def load_manifest(path):
    try:
        with open(path) as fh:
            m = json.load(fh, parse_constant=lambda c: (_ for _ in ()).throw(WLError("E_SCHEMA: bad const")))
    except (OSError, ValueError) as e:
        raise WLError("E_IO: %s" % e)
    return check_manifest(m, os.path.dirname(os.path.abspath(path)))


def plan_events(it):
    """(step, point, op, t_plan_us) per actuator event in the order srf-cu-exec emits them. A drag is press (point 0),
    one drag_move per path point (1..n) and drag_release (n+1), on the 60 Hz grid."""
    ev = []
    for i, s in enumerate(it["steps"]):
        if s["op"] == "drag":
            n = len(s["path"])
            ev.append((i + 1, 0, "drag_press", drag_t_us(s["t_ms"], 0)))
            ev += [(i + 1, j, "drag_move", drag_t_us(s["t_ms"], j)) for j in range(1, n + 1)]
            ev.append((i + 1, n + 1, "drag_release", drag_t_us(s["t_ms"], n + 1)))
        else:
            ev.append((i + 1, 0, s["op"], s["t_ms"] * 1000))
    return ev


def _vscroll(row, op, dy, w, h, **kw):
    d = min(h, abs(dy))
    if h - d > 0:
        row(op, [0, 0 if dy >= 0 else d, w, h - d], "MOVED", dy=dy, target="content", **kw)
    row(op, L2.exposed_strip(dy, w, h), "CHANGED_REGION", dy=dy, target="strip", **kw)


def labels_for(m, item_id, w, h, timing=None):
    if type(w) is not int or type(h) is not int or [w, h] not in m["resolutions"]:
        raise WLError("E_SCHEMA: dims must equal the prereg resolution")
    it = L2._item(m, item_id)
    out = []
    if timing is None:
        def timing(step, point):
            s = it["steps"][step - 1]
            return step, (drag_t_us(s["t_ms"], point) if s["op"] == "drag" else s["t_ms"] * 1000)

    def emit(seq, t_us, frame):
        def row(op, rect, kind, **kw):
            r = {"actuator_seq": seq, "t_us": t_us, "frame": frame, "op": op, "rect": rect, "expected_kind": kind,
                 "dy": None, "dx": None, "period_ms": None, "target": None}
            r.update(kw)
            out.append(r)
        return row

    div = list(it.get("div_rect0") or [0, 0, 0, 0])
    for i, s in enumerate(it["steps"]):
        op = s["op"]
        seq, t_us = timing(i + 1, 0)
        row = emit(seq, t_us, L2.frame_of(t_us))
        if op == "scroll" and item_id == "WL-1c":
            tab = it["dy_table"]
            for k, dy in enumerate(tab):  # one rAF step per frame, anchored at the actuator event
                _vscroll(emit(seq, t_us + (k * 1000000 + FPS - 1) // FPS, L2.frame_of(t_us) + k), op, dy, w, h, anim_index=k)
        elif op in ("scroll", "key"):
            _vscroll(row, op, s["dy"], w, h)
        elif op == "hscroll":
            d = min(w, abs(s["dx"]))
            if w - d > 0:
                row(op, [0 if s["dx"] > 0 else d, 0, w - d, h], "MOVED", dx=s["dx"], target="content")
            row(op, [w - d, 0, d, h] if s["dx"] > 0 else [0, 0, d, h], "CHANGED_REGION", dx=s["dx"], target="strip")
        elif op == "drag":
            prev = s["at_px"]
            for j, p in enumerate(s["path"], 1):
                dx, dy = p[0] - prev[0], p[1] - prev[1]
                prev = p
                old = list(div)
                div[0] += dx
                div[1] += dy
                seq_j, t_j = timing(i + 1, j)
                rj = emit(seq_j, t_j, L2.frame_of(t_j))
                rj("drag_move", list(div), "MOVED", dx=dx, dy=dy, target="div")
                if dx > 0:
                    rj("drag_move", [old[0], old[1], dx, old[3]], "CHANGED_REGION", dx=dx, dy=dy, target="uncovered")
                elif dx < 0:
                    rj("drag_move", [div[0] + div[2], old[1], -dx, old[3]], "CHANGED_REGION", dx=dx, dy=dy, target="uncovered")
        elif op == "wait" and item_id == "WL-3":
            px, py, pw, ph = it["pane_rect"]
            row0 = emit(seq, 0, 0)
            row0("idle", list(it["pane_rect"]), "CHANGED_REGION", target="pane", until_us=t_us)
            for r in ([0, 0, w, py], [0, py + ph, w, h - py - ph], [0, py, px, ph], [px + pw, py, w - px - pw, ph]):
                row0("idle", r, "NOT_CHANGED", target="rest", until_us=t_us)
    return out


def labels_from_act(m, item_id, w, h, act_rows):
    """Same label geometry as labels_for, with actuator_seq/t_us from the srf-cu-exec AJSON act_steps (measured offset
    from the START t0). Malformed row rc 2; a log that diverges from the plan, a failed event or non-monotonic time is rc 5."""
    it = L2._item(m, item_id)
    plan = plan_events(it)
    got = []
    last_seq, last_t = 0, -1
    for r in act_rows:
        if not isinstance(r, dict) or set(r) != L2.ACT_KEYS or r["schema"] != L2.ACT_SCHEMA or not isinstance(r["op"], str) \
                or not isinstance(r["item"], str):
            raise WLError("E_SCHEMA: act_steps row keys/schema")
        L2._no_non_int({k: v for k, v in r.items() if k not in ("schema", "item", "op")}, "$.act")
        if r["item"] != item_id:
            raise WLError("E_DIVERGENCE: act_steps item %s != %s" % (r["item"], item_id), 5)
        if r["rc"] != 0:
            raise WLError("E_DIVERGENCE: actuator rc %d at seq %d" % (r["rc"], r["actuator_seq"]), 5)
        if r["actuator_seq"] <= last_seq or r["t_us"] < last_t or r["t_us"] < 0:
            raise WLError("E_DIVERGENCE: actuator_seq/t_us not monotonic", 5)
        last_seq, last_t = r["actuator_seq"], r["t_us"]
        got.append(r)
    if [(r["step"], r["point"], r["op"], r["t_plan_us"]) for r in got] != plan:
        raise WLError("E_DIVERGENCE: act_steps do not match the prereg plan events", 5)
    by = {(r["step"], r["point"]): (r["actuator_seq"], r["t_us"]) for r in got}
    return labels_for(m, item_id, w, h, timing=lambda step, point: by[(step, point)])


USAGE = ("usage: labels_v3.py validate <prereg_v3> | labels <prereg_v3> <WL-id> <w> <h>"
         " | labels-act <prereg_v3> <WL-id> <w> <h> <act_steps.jsonl>")


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        want = {"validate": (2,), "labels": (5,), "labels-act": (6,)}
        if not a or a[0] not in want or len(a) not in want[a[0]]:
            print(USAGE, file=sys.stderr)
            return 2
        m = load_manifest(a[1])
        if a[0] == "validate":
            print(prereg_sha256(m))
        else:
            rows = labels_for(m, a[2], int(a[3]), int(a[4])) if a[0] == "labels" else \
                labels_from_act(m, a[2], int(a[3]), int(a[4]), L2.read_act(a[5]))
            for r in rows:
                print(json.dumps(r, sort_keys=True, separators=(",", ":")))
        return 0
    except WLError as e:
        print(str(e), file=sys.stderr)
        return e.rc
    except (ValueError, OSError, KeyError, TypeError) as e:
        print("E_IO: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
