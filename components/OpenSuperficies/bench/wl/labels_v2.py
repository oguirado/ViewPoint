"""WL prereg v2 loader + weak labels (W3 lane W).

stdlib only. Exit codes: 0, 2 (usage/schema/io), 5 (scene/font pin); never 1. v1 (recorder.py, prereg_wl.json) stays frozen.
Label enum: MOVED, CHANGED, CHANGED_REGION, NOT_CHANGED, FLICKER. Frame of an event = floor(t_us*60/1e6), windows half-open
[start(k), start(k+1)) so each event falls in exactly one frame (VG-0053 Thm 2); sampling alias <= 1 frame (VG-0061 s2.3).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recorder  # noqa: E402

WLError = recorder.WLError
SCHEMA = "os.cu.prereg-wl/v2"
PARENT_SHA256 = "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58"
FPS = 60
ALIAS_FRAMES = 1
PPM_MEDIAN = 500000
ORDER = ["WL-2b", "WL-2", "WL-1", "WL-7"]
TOP_KEYS = {"schema", "parent_prereg", "fps", "segment_min_ms", "det_sha256", "resolutions", "capture_order", "font",
            "text_synth", "items"}
FONT_KEYS = {"font_file", "font_sha256", "font_px", "advance_px", "units_per_em", "advance_units"}
ITEM_REQ = {"id", "name", "scene_file", "scene_sha256", "font_sha256", "xvfb_cursor", "steps"}
ITEM_OPT = {"text_len", "period_ms", "caret_rect", "caret_mode", "caret_w", "text_origin_px", "line_h", "cursor_px", "row_h"}
STEP_KEYS = {"t_ms", "op", "at_px", "text", "dy", "path", "dt_ms"}
OPS = {"click", "type", "scroll", "move", "wait"}
CARET_MODES = ("move", "redraw")


def _no_non_int(v, path="$"):
    if not isinstance(v, (dict, list)) and type(v) is not int:
        raise WLError("E_SCHEMA: non-integer value at " + path)
    if isinstance(v, dict):
        for k, x in v.items():
            _no_non_int(x, path + "." + k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _no_non_int(x, "%s[%d]" % (path, i))


def _closed(d, req, opt, what):
    if not isinstance(d, dict) or not req <= set(d) or not set(d) <= (req | opt):
        raise WLError("E_SCHEMA: %s keys" % what)


def _pinned(base, rel, want, what):
    if os.path.isabs(rel) or ".." in rel.split("/"):
        raise WLError("E_SCHEMA: %s path" % what)
    try:
        with open(os.path.join(base, rel), "rb") as f:
            got = recorder._sha(f.read())
    except OSError as e:
        raise WLError("E_IO: %s" % e)
    if got != want:
        raise WLError("E_PIN: %s sha drift" % what, 5)


def _check_ops(it, m):
    for s in it["steps"]:
        op = s["op"]
        if op == "scroll" and s.get("dy", 0) == 0:
            raise WLError("E_SCHEMA: scroll needs dy != 0")
        if op == "move":
            if it.get("cursor_px", 0) <= 0:
                raise WLError("E_SCHEMA: move needs item cursor_px > 0")
            p = s.get("path")
            if not (isinstance(p, list) and p and all(isinstance(q, list) and len(q) == 2 for q in p)):
                raise WLError("E_SCHEMA: move needs a non-empty path of [x,y]")
            if s.get("dt_ms", 0) <= 0:
                raise WLError("E_SCHEMA: move needs dt_ms > 0")
    if it["id"] == "WL-2b":
        if not any(s["op"] == "wait" for s in it["steps"]):
            raise WLError("E_SCHEMA: WL-2b needs a wait step")
        cr = it.get("caret_rect")
        if not (isinstance(cr, list) and len(cr) == 4 and cr[2] > 0 and cr[3] > 0):
            raise WLError("E_SCHEMA: WL-2b needs caret_rect [x,y,w,h] with w,h > 0")
    if it["id"] == "WL-2":
        text = "".join(s["text"] for s in it["steps"] if s["op"] == "type")
        if text != m["text_synth"] or len(text) != it.get("text_len"):
            raise WLError("E_SCHEMA: WL-2 typed text must equal text_synth and text_len")


def check_manifest(m, base):
    _closed(m, TOP_KEYS, set(), "manifest")
    _no_non_int({k: v for k, v in m.items() if k not in ("schema", "parent_prereg", "capture_order", "text_synth", "font", "items")})
    if m["schema"] != SCHEMA or m["parent_prereg"] != PARENT_SHA256:
        raise WLError("E_SCHEMA: schema/parent_prereg")
    if m["fps"] != FPS or m["segment_min_ms"] < 5000 or m["det_sha256"] != 1 or m["resolutions"] != [[1920, 1080]]:
        raise WLError("E_SCHEMA: fps/segment/det/resolutions")
    f = m["font"]
    _closed(f, FONT_KEYS, set(), "font")
    _no_non_int({k: v for k, v in f.items() if k not in ("font_file", "font_sha256")}, "$.font")
    _pinned(base, f["font_file"], f["font_sha256"], "font")
    ids = []
    for it in m["items"]:
        _closed(it, ITEM_REQ, ITEM_OPT, "item")
        _no_non_int({k: v for k, v in it.items() if k not in ("id", "name", "scene_file", "scene_sha256", "font_sha256", "caret_mode", "steps")}, "$.item")
        if it["xvfb_cursor"] not in (0, 1) or it["font_sha256"] != f["font_sha256"]:
            raise WLError("E_SCHEMA: xvfb_cursor/font_sha256")
        if it.get("caret_mode", "move") not in CARET_MODES:
            raise WLError("E_SCHEMA: caret_mode")
        _pinned(base, it["scene_file"], it["scene_sha256"], "scene " + str(it["id"]))
        for s in it["steps"]:
            _closed(s, {"t_ms", "op"}, STEP_KEYS, "step")
            _no_non_int({k: v for k, v in s.items() if k not in ("op", "text")}, "$.step")
            if s["op"] not in OPS or (s["op"] == "type" and not (isinstance(s.get("text"), str) and len(s["text"]) == 1)):
                raise WLError("E_SCHEMA: op/text")
        _check_ops(it, m)
        ids.append(it["id"])
    if m["capture_order"] != ORDER or sorted(ids) != sorted(ORDER):
        raise WLError("E_SCHEMA: capture_order must be %s (WL-7 last)" % ",".join(ORDER))
    if any((it["xvfb_cursor"] == 1) != (it["id"] == "WL-7") for it in m["items"]):
        raise WLError("E_SCHEMA: xvfb_cursor on only for WL-7")
    wl2b = _item(m, "WL-2b")
    if wl2b.get("period_ms", 0) <= 0:
        raise WLError("E_SCHEMA: WL-2b period_ms must be pre-registered > 0")
    return m


def load_manifest(path):
    try:
        with open(path) as fh:
            m = json.load(fh, parse_constant=lambda c: (_ for _ in ()).throw(WLError("E_SCHEMA: bad const")))
    except (OSError, ValueError) as e:
        raise WLError("E_IO: %s" % e)
    return check_manifest(m, os.path.dirname(os.path.abspath(path)))


def prereg_sha256(m):
    return recorder._sha(json.dumps(m, sort_keys=True, separators=(",", ":")).encode())


def _item(m, item_id):
    for it in m["items"]:
        if it["id"] == item_id:
            return it
    raise WLError("E_SCHEMA: unknown item " + str(item_id))


def frame_of(t_us):
    return t_us * FPS // 1000000


def frame_start_us(k):
    return (k * 1000000 + FPS - 1) // FPS  # smallest t_us with frame_of(t_us) >= k


def flicker_period_ms(toggle_t_us):
    """Median toggle interval in integer ms: x(k), k = max(1, ceil(ppm*n/1e6)) over sorted intervals (SW-0167 s2.1)."""
    iv = sorted((b - a + 500) // 1000 for a, b in zip(toggle_t_us, toggle_t_us[1:]))
    if not iv:
        return 0
    k = max(1, (PPM_MEDIAN * len(iv) + 999999) // 1000000)
    return iv[k - 1]


def exposed_strip(dy, w, h):
    """Newly exposed edge strip for a scroll of dy px: d = min(h, |dy|); dy>0 reveals the bottom, dy<0 the top."""
    d = min(h, abs(dy))
    return [0, h - d, w, d] if dy >= 0 else [0, 0, w, d]


def _box(cx, cy, bw, bh, w, h):
    return [max(0, min(w - bw, cx)), max(0, min(h - bh, cy)), bw, bh]


def plan_events(it):
    """(step, point, op, t_plan_us) per actuator event, in the order srf-cu-exec emits them (move: one event per path point)."""
    ev = []
    for i, s in enumerate(it["steps"]):
        pts = s["path"] if s["op"] == "move" else [None]
        for j in range(len(pts)):
            ev.append((i + 1, j, s["op"], (s["t_ms"] + j * s.get("dt_ms", 0)) * 1000))
    return ev


def labels_for(m, item_id, w, h, timing=None):
    if type(w) is not int or type(h) is not int or w <= 0 or h <= 0:
        raise WLError("E_SCHEMA: dims")
    it = _item(m, item_id)
    out = []
    if timing is None:
        def timing(step, point):
            s = it["steps"][step - 1]
            return step, (s["t_ms"] + point * s.get("dt_ms", 0)) * 1000

    def row(seq, t_us, op, rect, kind, **kw):
        r = {"actuator_seq": seq, "t_us": t_us, "frame": frame_of(t_us), "op": op, "rect": rect, "expected_kind": kind,
             "dy": None, "dx": None, "period_ms": None, "target": None}
        r.update(kw)
        out.append(r)

    ox, oy = (it.get("text_origin_px") or [0, 0])
    adv, lh = m["font"]["advance_px"], it.get("line_h", 0)
    typed = 0
    for i, s in enumerate(it["steps"]):
        op = s["op"]
        seq, t_us = timing(i + 1, 0)
        if item_id == "WL-2b" and op == "wait":
            row(seq, 0, "idle", list(it["caret_rect"]), "FLICKER", period_ms=it["period_ms"], target="caret", until_us=t_us)
            row(seq, 0, "idle", [0, 0, w, h], "NOT_CHANGED", target="rest", until_us=t_us)
        elif op == "type":
            cw = it.get("caret_w", 2)
            row(seq, t_us, op, [ox + adv * typed, oy, adv, lh], "CHANGED", target="glyph")
            typed += 1
            crect = [ox + adv * typed, oy, cw, lh]
            if it.get("caret_mode", "move") == "move":
                row(seq, t_us, op, crect, "MOVED", dx=adv, target="caret")
            else:
                row(seq, t_us, op, crect, "CHANGED", target="caret")
        elif op == "scroll":
            d = min(h, abs(s["dy"]))
            if h - d > 0:
                row(seq, t_us, op, [0, 0 if s["dy"] >= 0 else d, w, h - d], "MOVED", dy=s["dy"], target="content")
            row(seq, t_us, op, exposed_strip(s["dy"], w, h), "CHANGED_REGION", dy=s["dy"], target="strip")
        elif op == "move":
            cp = it["cursor_px"]
            for j, (px, py) in enumerate(s["path"]):
                if j:  # point 0 is the start position, not a move
                    row(*timing(i + 1, j), op, _box(px, py, cp, cp, w, h), "MOVED", target="pointer")
    return out


ACT_SCHEMA = "os.cu.wl-act-step/v1"
ACT_KEYS = {"schema", "item", "actuator_seq", "step", "point", "op", "t_plan_us", "t_us", "rc"}


def labels_from_act(m, item_id, w, h, act_rows):
    """CF-01: same label geometry as labels_for, but actuator_seq and t_us come from the srf-cu-exec AJSON act_steps
    (t_us = measured CLOCK_MONOTONIC offset from the wlcap START t0). Malformed row rc 2; a log that diverges from
    the plan, a failed event or non-monotonic time is rc 5 (no labels)."""
    it = _item(m, item_id)
    plan = plan_events(it)
    got = {}
    last_seq, last_t = 0, -1
    for r in act_rows:
        if not isinstance(r, dict) or set(r) != ACT_KEYS or r["schema"] != ACT_SCHEMA or not isinstance(r["op"], str) \
                or not isinstance(r["item"], str):
            raise WLError("E_SCHEMA: act_steps row keys/schema")
        _no_non_int({k: v for k, v in r.items() if k not in ("schema", "item", "op")}, "$.act")
        if r["item"] != item_id:
            raise WLError("E_DIVERGENCE: act_steps item %s != %s" % (r["item"], item_id), 5)
        if r["rc"] != 0:
            raise WLError("E_DIVERGENCE: actuator rc %d at seq %d" % (r["rc"], r["actuator_seq"]), 5)
        if r["actuator_seq"] <= last_seq or r["t_us"] < last_t or r["t_us"] < 0:
            raise WLError("E_DIVERGENCE: actuator_seq/t_us not monotonic", 5)
        last_seq, last_t = r["actuator_seq"], r["t_us"]
        got[len(got)] = r
    if [(r["step"], r["point"], r["op"], r["t_plan_us"]) for r in got.values()] != plan:
        raise WLError("E_DIVERGENCE: act_steps do not match the prereg plan events", 5)
    by = {(r["step"], r["point"]): (r["actuator_seq"], r["t_us"]) for r in got.values()}
    return labels_for(m, item_id, w, h, timing=lambda step, point: by[(step, point)])


def read_act(path):
    rows = []
    try:
        with open(path) as fh:
            for ln in fh:
                if not ln.endswith("\n"):
                    raise WLError("E_SCHEMA: torn act_steps line")
                rows.append(json.loads(ln, parse_constant=lambda c: (_ for _ in ()).throw(WLError("E_SCHEMA: bad const"))))
    except (OSError, ValueError) as e:
        raise WLError("E_IO: %s" % e)
    return rows


USAGE = ("usage: labels_v2.py validate <prereg_v2> | labels <prereg_v2> <WL-id> <w> <h>"
         " | labels-act <prereg_v2> <WL-id> <w> <h> <act_steps.jsonl>")


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
                labels_from_act(m, a[2], int(a[3]), int(a[4]), read_act(a[5]))
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
