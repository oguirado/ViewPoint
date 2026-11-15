"""WL-1..6 workload recorder: pre-registered manifest, vqa.action/v1 plans, weak labels, RAM-only frame sink.

stdlib only. Exit codes: 0, 2 (usage/schema/io), 5 (pin or storage gate); never 1.
"""
import fcntl
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAMWS_ROOT = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"
WM_PROFILES = ("default", "qtile")
TOP_KEYS = {"schema", "fps", "segment_min_ms", "det_sha256", "w1_wm_profile", "resolutions",
            "capture_order", "text_synth", "qtile", "items"}
ITEM_KEYS = {"id", "name", "scene_file", "scene_sha256", "steps", "text_len", "wm_profiles"}
STEP_KEYS = {"op", "at_pm", "t_ms", "dy", "to_pm"}
OPS = {"scroll", "click", "type", "drag", "wait"}
FRAME_US = 16667
FRAME_TOL_US = 20000


class WLError(Exception):
    def __init__(self, msg, rc=2):
        super().__init__(msg)
        self.rc = rc


class Manifest(dict):
    base = HERE


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _ints_only(v, path="$"):
    if isinstance(v, (float, bool)) or v is None:
        raise WLError("non-integer value at " + path)
    if isinstance(v, dict):
        for k, x in v.items():
            _ints_only(x, path + "." + k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _ints_only(x, "%s[%d]" % (path, i))


def _closed(d, keys, what, required=None):
    if not isinstance(d, dict) or not set(d) <= keys or not (required or keys) <= set(d):
        raise WLError("E_SCHEMA: %s keys" % what)


def _pinned(base, rel, want, what):
    if os.path.isabs(rel) or ".." in rel.split("/"):
        raise WLError("E_SCHEMA: %s path" % what)
    try:
        got = _sha(open(os.path.join(base, rel), "rb").read())
    except OSError as e:
        raise WLError("E_IO: %s" % e)
    if got != want:
        raise WLError("E_PIN: %s sha drift" % what, 5)


def load_manifest(path):
    try:
        m = json.load(open(path), parse_constant=lambda c: (_ for _ in ()).throw(WLError("bad const")))
    except (OSError, ValueError) as e:
        raise WLError("E_IO: %s" % e)
    base = os.path.dirname(os.path.abspath(path))
    _closed(m, TOP_KEYS, "manifest")
    _ints_only({k: v for k, v in m.items() if k not in ("schema", "w1_wm_profile", "text_synth", "capture_order", "qtile", "items")})
    if m["schema"] != "os.cu.wl-prereg/v1" or m["w1_wm_profile"] != "default":
        raise WLError("E_SCHEMA: schema/w1_wm_profile")
    if m["fps"] != 60 or m["segment_min_ms"] < 5000 or m["det_sha256"] != 1:
        raise WLError("E_SCHEMA: fps/segment/det")
    if m["resolutions"] != [[1920, 1080], [3840, 2160]]:
        raise WLError("E_SCHEMA: resolutions")
    _closed(m["qtile"], {"config_file", "config_sha256", "monad_ratio_pm"}, "qtile")
    _ints_only({"monad_ratio_pm": m["qtile"]["monad_ratio_pm"]}, "$.qtile")
    _pinned(base, m["qtile"]["config_file"], m["qtile"]["config_sha256"], "qtile config")
    ids = []
    for it in m["items"]:
        _closed(it, ITEM_KEYS, "item")
        _ints_only({"text_len": it["text_len"]})
        if sorted(it["wm_profiles"]) != sorted(WM_PROFILES):
            raise WLError("E_SCHEMA: wm_profiles must be exactly default+qtile")
        _pinned(base, it["scene_file"], it["scene_sha256"], "scene " + it["id"])
        for s in it["steps"]:
            _closed(s, STEP_KEYS, "step", {"op", "at_pm", "t_ms"})
            _ints_only(s)
            if s["op"] not in OPS:
                raise WLError("E_SCHEMA: op")
        ids.append(it["id"])
    order = m["capture_order"]
    if sorted(order) != sorted(ids) or len(set(ids)) != len(ids) or order[:1] != ["WL-6"]:
        raise WLError("E_SCHEMA: capture_order must permute items with WL-6 first")
    out = Manifest(m)
    out.base = base
    return out


def prereg_sha256(m):
    return _sha(json.dumps(m, sort_keys=True, separators=(",", ":")).encode())


def capture_order(m):
    return list(m["capture_order"])


def _item(m, item_id):
    for it in m["items"]:
        if it["id"] == item_id:
            return it
    raise WLError("E_SCHEMA: unknown item " + item_id)


def _px(pm, w, h):
    return [pm[0] * w // 1000, pm[1] * h // 1000]


def plan_for(m, item_id, w, h):
    it = _item(m, item_id)
    tsha = _sha(m["text_synth"].encode())
    plan = []
    for i, s in enumerate(it["steps"]):
        op = s["op"]
        a = {"schema": "vqa.action/v1", "step": i + 1, "op": op, "args": {}, "pre": {"match_min": "L1"},
             "expect": [], "risk": "read", "allow": None, "fallback": "none", "on_fail": "stop",
             "policy": "script", "reason_sha8": _sha(("%s:%d" % (item_id, i + 1)).encode())[:8]}
        if op != "wait":
            a["target"] = {"by": "xy", "xy": _px(s["at_pm"], w, h)}
        if op == "click":
            a["args"] = {"button": 1, "count": 1}
        elif op == "scroll":
            a["args"] = {"dy": s["dy"]}
        elif op == "type":
            a["args"] = {"text_sha256": tsha, "len": it["text_len"]}
        elif op == "drag":
            a["args"] = {"to": {"by": "xy", "xy": _px(s["to_pm"], w, h)}, "steps": 16}
        plan.append(a)
    return plan


def _scene(m, it):
    return json.load(open(os.path.join(m.base if hasattr(m, "base") else HERE, it["scene_file"]))) \
        if it["scene_file"].endswith(".json") else {}


def _box(cx, cy, bw, bh, w, h):
    x = max(0, min(w - bw, cx - bw // 2))
    y = max(0, min(h - bh, cy - bh // 2))
    return [x, y, bw, bh]


def labels_for(m, item_id, w, h, wm_profile="default", wm_layout=None):
    if wm_profile not in WM_PROFILES:
        raise WLError("E_SCHEMA: wm_profile")
    layout = None
    if wm_profile == "qtile":
        r = (wm_layout or {}).get("rect")
        if not (isinstance(r, list) and len(r) == 4 and all(type(v) is int for v in r)):
            raise WLError("E_SCHEMA: qtile labels need wm_layout.rect read from the WM")
        layout = list(r)
    it = _item(m, item_id)
    sc = _scene(m, it)
    out = []
    for i, s in enumerate(it["steps"]):
        op = s["op"]
        px = _px(s["at_pm"], w, h)
        dy = None
        if op == "scroll":
            kind, rect, dy = "MOVED", [0, 0, w, h], s["dy"]
        elif op in ("click", "type"):
            kind, rect = "CHANGED", _box(px[0], px[1], w // 10, h // 20, w, h)
        elif op == "drag":
            x, y, bw, bh = (sc["window_pm"][0] * w // 1000, sc["window_pm"][1] * h // 1000,
                            sc["window_pm"][2] * w // 1000, sc["window_pm"][3] * h // 1000)
            to = _px(s["to_pm"], w, h)
            nx, ny = x + to[0] - px[0], y + to[1] - px[1]
            x0, y0 = min(x, nx), min(y, ny)
            rect, kind = [x0, y0, max(x, nx) + bw - x0, max(y, ny) + bh - y0], "CHANGED_REGION"
        elif item_id == "WL-3":
            p = sc["pane_pm"]
            kind, rect = "CHANGED_REGION", [p[0] * w // 1000, p[1] * h // 1000, p[2] * w // 1000, p[3] * h // 1000]
        else:
            kind, rect = "NOT_CHANGED", _box(px[0], px[1], 32, 32, w, h)
        if layout is not None:
            rect = layout
        out.append({"actuator_seq": i + 1, "t_us": s["t_ms"] * 1000, "op": op, "rect": rect,
                    "expected_kind": kind, "dy": dy})
    return out


def _ramws_dir(d):
    real = os.path.realpath(d)
    if os.path.dirname(real) != RAMWS_ROOT:
        raise WLError("E_STORAGE: frames only in a session dir directly under " + RAMWS_ROOT, 5)
    if not os.path.isfile(os.path.join(real, "owner.json")):
        raise WLError("E_STORAGE: unmanaged ramws dir (no owner.json)", 5)
    try:
        fd = os.open(os.path.join(real, ".lock"), os.O_RDONLY)  # law4 share: .lock is u:10001:r--
    except OSError:
        raise WLError("E_STORAGE: unmanaged ramws dir (no .lock)", 5)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # host session holds EX; never hold or take EX (AMEND C5)
        except BlockingIOError:
            return real
        fcntl.flock(fd, fcntl.LOCK_UN)
        raise WLError("E_STORAGE: unmanaged ramws dir (session lock not held)", 5)
    finally:
        os.close(fd)


class Recorder:
    """Appends packed rgb24 (stride w*3) to <ramws>/frames.rgb24; keeps only metadata in memory."""

    def __init__(self, ramws_dir, w, h, det=True):
        if any(type(x) is not int or x <= 0 for x in (w, h)):
            raise WLError("E_SCHEMA: dims")
        self.dir = _ramws_dir(ramws_dir)
        self.w, self.h, self.det = w, h, det
        self.size = w * h * 3
        self.f = os.fdopen(os.open(os.path.join(self.dir, "frames.rgb24"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), "wb")
        self.recs = []
        self.gaps = []
        self.dets = []

    def push(self, render_index, t_us, rgb):
        if len(rgb) != self.size:
            raise WLError("E_SCHEMA: frame size %d != %d" % (len(rgb), self.size))
        if self.recs:
            prev = self.recs[-1][0]
            if render_index <= prev:
                raise WLError("E_SCHEMA: render_index not increasing")
            if render_index > prev + 1:
                self.gaps.append({"after": prev, "missing": render_index - prev - 1})
        self.f.write(rgb)
        self.recs.append((render_index, t_us))
        if self.det:
            self.dets.append(_sha(rgb))

    def segment_ms(self):
        best = run_start = 0
        for i, (idx, t) in enumerate(self.recs):
            if i and (idx != self.recs[i - 1][0] + 1 or t - self.recs[i - 1][1] > FRAME_TOL_US):
                run_start = i
            best = max(best, (t - self.recs[run_start][1] + FRAME_US) // 1000)
        return best

    def finish(self):
        self.f.close()
        rec = {"frames": len(self.recs), "gaps": self.gaps, "segment_ms": self.segment_ms(),
               "width": self.w, "height": self.h, "stride": self.w * 3}
        if self.det:
            rec["det_sha256"] = self.dets
        with os.fdopen(os.open(os.path.join(self.dir, "wl_meta.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), "w") as f:
            json.dump(rec, f, sort_keys=True)
        return rec


USAGE = "usage: recorder.py validate|order <manifest> | plan|labels <manifest> <WL-N> <w> <h> [default|qtile]"


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o027)
    try:
        want = {"validate": (2,), "order": (2,), "plan": (5,), "labels": (5, 6)}
        if not a or a[0] not in want or len(a) not in want[a[0]]:
            print(USAGE, file=sys.stderr)
            return 2
        m = load_manifest(a[1])
        if a[0] == "validate":
            print(prereg_sha256(m))
        elif a[0] == "order":
            print(" ".join(capture_order(m)))
        else:
            w, h = int(a[3]), int(a[4])
            if w <= 0 or h <= 0:
                print("E_SCHEMA: dims must be positive", file=sys.stderr)
                return 2
            if a[0] == "plan":
                rows = plan_for(m, a[2], w, h)
            else:
                rows = labels_for(m, a[2], w, h, a[5] if len(a) > 5 else "default")
            for r in rows:
                print(json.dumps(r, sort_keys=True, separators=(",", ":")))
        return 0
    except WLError as e:
        print(str(e), file=sys.stderr)
        return e.rc
    except (ValueError, OSError) as e:
        print("E_IO: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
