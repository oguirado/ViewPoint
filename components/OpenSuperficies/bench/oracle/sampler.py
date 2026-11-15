"""Oracle sampler (W6a lane O): speaks CDP only to the allow-list proxy through two FIFOs, never to Chromium.

Python 3 stdlib. Frames are NUL-terminated JSON. Clock: t_us = (CLOCK_MONOTONIC ns - START.t0_ns) // 1000 (actuator timebase).
Only read-only domains are called; the proxy refuses anything else (rc 5). Exit codes: 0, 2, 5; never 1.
"""
import json
import os
import re
import stat
import time

START_SCHEMA = "os.cu.wlcap-start/v1"
LIMIT = 100 * 1024 * 1024
STYLES = ["visibility", "opacity", "display"]
# prereg-declared elements of the W6a scenes (box: WL-4, cv: WL-3, caret/f1: WL-2b); anything else (the 400 .r rows) is not snapshotted
DEFAULT_TRACK_IDS = frozenset({"box", "cv", "caret", "f1"})
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
FNV_OFF, FNV_PRIME, M64 = 0xcbf29ce484222325, 0x100000001b3, (1 << 64) - 1


def fnv64(data):
    h = FNV_OFF
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & M64
    return h


def _no_dup_keys(pairs):
    """object_pairs_hook (ADC2-W6A-05): a repeated key in START is refused, never last-wins."""
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("START duplicate key %r" % k)
        d[k] = v
    return d


def read_start(run_dir):
    """<ramws_run_dir>/START: regular file, O_NOFOLLOW|O_NONBLOCK, nlink 1, bounded, schema + CLOCK_MONOTONIC + integer t0_ns."""
    p = os.path.join(run_dir, "START")
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ValueError("START unreadable: %s" % e)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 4096:
            raise ValueError("START unsafe (not a lone regular file <= 4 KiB)")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        d = json.loads(raw, object_pairs_hook=_no_dup_keys)
    except ValueError as e:
        raise ValueError("START not JSON or duplicate key: %s" % str(e)[:80])
    if not isinstance(d, dict) or d.get("schema") != START_SCHEMA or d.get("clock") != "CLOCK_MONOTONIC" or type(d.get("t0_ns")) is not int or d["t0_ns"] <= 0:
        raise ValueError("START schema/clock/t0_ns")
    if "run_nonce" in d and not (isinstance(d["run_nonce"], str) and NONCE_RE.match(d["run_nonce"])):
        raise ValueError("START run_nonce must match ^[0-9a-f]{32}$")
    return d


def t_us_of(now_ns, t0_ns):
    return (now_ns - t0_ns) // 1000


def frame_encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\0"


class FrameReader:
    """Chunk-cut invariant: feeding any split of a stream yields the same frames. lim = min(len, M+1) keeps oversize detectable."""

    def __init__(self, limit=LIMIT):
        self.limit, self.buf = limit, b""

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"\0", 0, min(len(self.buf), self.limit + 1))
            if i < 0:
                if len(self.buf) > self.limit:
                    raise ValueError("oversize frame")
                return out
            frame, self.buf = self.buf[:i], self.buf[i + 1:]
            out.append(json.loads(frame))


class FifoIO:
    """write -> oracle.in (the proxy reads it), read <- oracle.out; both must be FIFOs, no symlinks."""

    def __init__(self, in_path, out_path):
        self.w = self._open(in_path, os.O_WRONLY)
        self.r = self._open(out_path, os.O_RDONLY | os.O_NONBLOCK)

    @staticmethod
    def _open(p, fl):
        try:
            fd = os.open(p, fl | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            raise ValueError("fifo %s: %s" % (p, e))
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("%s is not a FIFO" % p)
        return fd

    def write(self, b):
        return os.write(self.w, b)

    def read(self, n=65536):
        try:
            return os.read(self.r, n)
        except BlockingIOError:
            return b""


def _int(v, flag):
    if type(v) is int:
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    flag[0] = 1
    return int(round(v))


def row_from_snapshot(snap, ax_tree, shifts, seq, t_us, prev_snap_fnv=None, track_ids=None):
    """DOMSnapshot.captureSnapshot + AX tree -> one integer-only row. nodes is None when the snapshot equals the previous one."""
    flag = [0]
    doc, strs = snap["documents"][0], snap["strings"]
    nd, lay = doc["nodes"], doc["layout"]

    def s(i):
        return strs[i] if isinstance(i, int) and i >= 0 else ""
    nodes, canv, rec = [], [], []
    for i, ni in enumerate(lay["nodeIndex"]):
        attrs = nd["attributes"][ni] if ni < len(nd.get("attributes", [])) else []
        pairs = [(s(attrs[j]), s(attrs[j + 1])) for j in range(0, len(attrs) - 1, 2)]
        if track_ids is not None and dict(pairs).get("id") not in track_ids:
            continue
        name = s(nd["nodeName"][ni])
        raw = "%d|%s|%s|%s" % (nd["nodeType"][ni], name, s(nd["nodeValue"][ni]), ";".join("%s=%s" % p for p in pairs))
        st = [s(x) for x in lay["styles"][i]] if lay.get("styles") else []
        vis = 0 if st and (st[0] == "hidden" or st[1] == "0" or st[2] == "none") else 1
        bx = [_int(v, flag) for v in lay["bounds"][i]]
        bid = nd["backendNodeId"][ni]
        nodes.append([bid, bx[0], bx[1], bx[2], bx[3], vis, "%016x" % fnv64(raw.encode())])
        rec.append("%d|%r|%d|%s" % (bid, lay["bounds"][i], vis, nodes[-1][6]))
        if name == "CANVAS":
            canv.append(bid)
    fp = fnv64("\n".join(rec).encode())
    focus = None
    for n in ax_tree.get("nodes", []):
        for p in n.get("properties", []):
            if p.get("name") == "focused" and p.get("value", {}).get("value") is True and focus is None:
                focus = str(n["nodeId"])
    sh = []
    for e in shifts:
        sh.append({"n": e["n"], "p": [_int(v, flag) for v in e["p"]], "c": [_int(v, flag) for v in e["c"]], "ri": e["ri"]})
    sx, sy = _int(doc.get("scrollOffsetX", 0), flag), _int(doc.get("scrollOffsetY", 0), flag)
    snap_hex = "%016x" % fp
    return {"seq": seq, "t_us": t_us, "snap": snap_hex, "nodes": None if snap_hex == prev_snap_fnv else nodes, "sx": sx, "sy": sy,
            "focus": focus, "shifts": sh, "frac": flag[0], "canv": canv}


def _rect(d):
    return [d["x"], d["y"], d["width"], d["height"]]


class Sampler:
    def __init__(self, io, now_ns, t0_ns, scene_url, sleep=time.sleep, empty_max=5000, track_ids=None):
        self.io, self.now_ns, self.t0, self.url = io, now_ns, t0_ns, scene_url
        self.sleep, self.empty_max = sleep, empty_max
        self.track_ids = DEFAULT_TRACK_IDS if track_ids is None else track_ids
        self.rd, self.next_id, self.session, self.shifts, self.prev_fp = FrameReader(), 0, None, [], None
        self.pending = []

    def _pump(self, block):
        empty = 0
        while True:
            if self.pending:
                return self.pending.pop(0)
            data = self.io.read(65536)
            if data:
                self.pending.extend(self.rd.feed(data))
                empty = 0
                continue
            if not block:
                return None
            empty += 1
            if empty > self.empty_max:
                raise TimeoutError("no frame from the proxy")
            self.sleep(0.001)

    def _event(self, m):
        meth, p = m.get("method"), m.get("params", {})
        if meth == "Target.attachedToTarget":
            ti = p.get("targetInfo", {})
            if ti.get("type") == "page" and ti.get("url") == self.url and p.get("sessionId"):
                self.session = p["sessionId"]
        elif meth == "PerformanceTimeline.timelineEventAdded":
            ev = p.get("event", {})
            if ev.get("type") == "layout-shift":
                det = ev.get("layoutShiftDetails", {})
                for src in det.get("sources", []):
                    self.shifts.append({"n": src.get("nodeId", 0) if type(src.get("nodeId", 0)) is int else 0, "p": _rect(src["previousRect"]),
                                        "c": _rect(src["currentRect"]), "ri": 1 if det.get("hadRecentInput") else 0})

    def _call(self, method, params=None, session=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session:
            msg["sessionId"] = session
        self.io.write(frame_encode(msg))
        while True:
            m = self._pump(True)
            if m.get("id") == self.next_id:
                if "error" in m:
                    raise ValueError("%s refused: %s" % (method, m["error"]))
                return m.get("result", {})
            self._event(m)

    def _drain(self):
        while True:
            m = self._pump(False)
            if m is None:
                return
            self._event(m)

    def setup(self):
        self._call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        for _ in range(self.empty_max):
            if self.session:
                break
            m = self._pump(True)
            self._event(m)
        if not self.session:
            raise ValueError("no page session for the launched scene")
        for meth, par in (("Accessibility.enable", None), ("DOMSnapshot.enable", None), ("Page.enable", None),
                          ("PerformanceTimeline.enable", {"eventTypes": ["layout-shift"]})):
            self._call(meth, par, self.session)

    def sample(self, seq):
        t = t_us_of(self.now_ns(), self.t0)
        snap = self._call("DOMSnapshot.captureSnapshot", {"computedStyles": STYLES}, self.session)
        ax = self._call("Accessibility.getFullAXTree", None, self.session)
        self._drain()
        sh, self.shifts = self.shifts, []
        row = row_from_snapshot(snap, ax, sh, seq, t, self.prev_fp, self.track_ids)
        self.prev_fp = row["snap"]
        return row


ROWS_CAP = 100 * 1024


def row_bytes(r):
    """Serialised size of one row as write_rows writes it (canonical JSON + newline)."""
    return len(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()) + 1


class RowSink:
    """RD6A-02: sample_loop keeps rows only while the serialised total stays <= cap, so write_rows can never exceed it; the first row that
    would cross the cap stops the loop and sets truncated (written to .done), instead of rc 5 after a full episode."""

    def __init__(self, cap=ROWS_CAP):
        self.rows, self.size, self.cap, self.truncated = [], 0, cap, False

    def add(self, r):
        n = row_bytes(r)
        if self.size + n > self.cap:
            self.truncated = True
            return False
        self.rows.append(r)
        self.size += n
        return True


def write_rows(path, rows, cap=ROWS_CAP):
    data = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows).encode()
    if len(data) > cap:
        raise ValueError("oracle rows %d bytes exceed the %d byte episode cap" % (len(data), cap))
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
