"""WL capture driver: runs srf-cu-wlcap per pre-registered item (capture_order, WL-6 first), writes weak labels and the handover.

stdlib only. Frames never touch disk here: srf-cu-wlcap writes them into the ramws run dir; this script only writes
labels/<item>.jsonl, per-item receipts and wl_handover.json under --out. Exit codes: 0, 2 (usage/schema/io), 5 (gate); never 1.
Label enum stays NOT_CHANGED / CHANGED_REGION / MOVED / CHANGED (AMEND_W2 C2): recorder.labels_for is used unchanged.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labels_v2  # noqa: E402
import labels_v3  # noqa: E402
import recorder  # noqa: E402

PINNED_PREREG_SHA256 = "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58"
PINNED_PREREG_V2_SHA256 = "b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12"
SCHEMA = "os.cu.wl-handover/v1"
FRAME_EXT = (".rgb24", ".raw", ".xwd")
MAX_4K_CHUNK = 30
Q1080 = 2400 << 20  # kept 1080p run <= 2400 MiB (AW3-11)
PLAN_MAX = 10 << 30
Q_MAX = 2400 << 20
# HOST-ANALYSIS HANDSHAKE (CF-02, frozen by lane P2): wlcap writes chunk_<k>/READY, the host writes ANALYZED | ANALYZE_FAILED;
# wlcap writes <run>/START with the CLOCK_MONOTONIC t0 the actuator times its steps from (CF-01).
READY, ANALYZED, ANALYZE_FAILED, START = "READY", "ANALYZED", "ANALYZE_FAILED", "START"
READY_SCHEMA, START_SCHEMA = "os.cu.chunk-ready/v1", "os.cu.wlcap-start/v1"
ANALYZE_TIMEOUT_MAX = 590  # wlcap --analyze-wait-ms <= 600000; the host always answers before wlcap gives up
CHUNK_RE = re.compile(r"^chunk_([0-9]+)$")
ANALYZE_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "vision", "chunk_analyze.py")
MARKER_MAX = 64 << 10  # READY (carries det_sha256 per frame) and ANALYZED upper bound
START_MAX = 4096  # START is one small JSON line
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


class DriverError(Exception):
    def __init__(self, msg, rc=2):
        super().__init__(msg)
        self.rc = rc


def _qtile():
    spec = importlib.util.spec_from_file_location(
        "wl_qtile_profile", os.path.join(HERE, "qtile", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_res(s, allowed=((1920, 1080), (3840, 2160))):
    try:
        w, h = (int(x) for x in s.split("x"))
    except ValueError:
        raise DriverError("E_USAGE: --res WxH")
    if [w, h] not in [list(r) for r in allowed]:
        raise DriverError("E_SCHEMA: resolution not in prereg")
    return w, h


def assert_no_frames(out):
    for dp, _dn, fn in os.walk(out):
        for f in fn:
            if f.endswith(FRAME_EXT) or f in ("frames.raw", "Xvfb_screen0"):
                raise DriverError("E_STORAGE: frame file on disk: " + os.path.join(dp, f), 5)


RAM_FSTYPES = ("tmpfs", "ramfs")


def _fstype(path, mountinfo="/proc/self/mountinfo"):
    """Filesystem type of the longest mount point containing path (octal escapes of mountinfo decoded)."""
    best, kind = "", None
    with open(mountinfo, encoding="utf-8") as f:
        for line in f:
            head, sep, tail = line.partition(" - ")
            fields = head.split()
            if not sep or len(fields) < 5 or not tail.split():
                continue
            mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
            if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, kind = mp, tail.split()[0]
    return kind


def _out_ok(out, mountinfo="/proc/self/mountinfo"):
    try:
        kind = _fstype(os.path.realpath(out), mountinfo)
    except OSError:
        raise DriverError("E_USAGE: mount table unreadable, cannot prove --out is on disk")
    if kind is None or kind in RAM_FSTYPES:
        raise DriverError("E_USAGE: --out must be on a disk filesystem, not %s" % (kind or "an unknown mount"))


def _kept(a, w, h):
    big = w * h > 1920 * 1080
    return a.retain == "on" or (a.retain == "auto" and ((a.mode == "wl" and not big) or a.mode == "handover-1s"))


def _eff_quota(a):
    """Quota every kept run is given on the wlcap command line (and the one the plan total counts)."""
    return Q1080 if a.quota_bytes is None else a.quota_bytes


def _plan_bytes(n_kept, quota_bytes):
    return n_kept * (Q1080 if quota_bytes is None else quota_bytes)


def build_cmd(a, item, w, h, run_dir, item_out):
    big = w * h > 1920 * 1080
    if big and a.chunk > MAX_4K_CHUNK:
        raise DriverError("E_GATE: chunk_4k_max_30", 5)
    mode = a.mode
    retain = a.retain
    if retain == "auto":
        retain = "on" if (mode == "wl" and not big) or mode == "handover-1s" else "off"
    secs = 1 if mode == "handover-1s" else a.seconds
    cmd = list(a.wlcap) + ["--ramws-dir", run_dir, "--out", item_out, "--mode", mode, "--det", a.det,
                           "--seconds", str(secs), "--chunk", str(a.chunk),
                           "--min-segment-ms", str(5000 if mode == "wl" else 0)]
    if retain == "on":
        cmd.append("--keep")
        cmd += ["--ttl-seconds", "7200"]
        cmd += ["--quota-bytes", str(_eff_quota(a))]
    elif a.quota_bytes is not None:
        cmd += ["--quota-bytes", str(a.quota_bytes)]
    if a.require_continuous:
        cmd.append("--require-continuous" if a.require_continuous == "on" else "--require-continuous=false")
    if a.analyze != "off":
        cmd += ["--analyze", a.analyze]
    if a.analyze == "host":
        cmd += ["--analyze-wait-ms", str((a.analyze_timeout + 10) * 1000)]
    if a.fake_fb:
        cmd += ["--fake-fb", "--w", str(w), "--h", str(h)]
    else:
        cmd += ["--fb", a.fb, "--display", a.display, "--iso", a.iso]
    return cmd


def _runs(a, items):
    runs = {}
    for r in a.run:
        k, _, v = r.partition("=")
        if not v:
            raise DriverError("E_USAGE: --run ITEM=DIR")
        runs[k] = v
    if a.ramws_dir:
        if len(items) != 1:
            raise DriverError("E_USAGE: --ramws-dir needs exactly one --item")
        runs[items[0]] = a.ramws_dir
    reals = [os.path.realpath(runs[i]) for i in items if i in runs]
    if len(set(reals)) != len(reals):
        raise DriverError("E_USAGE: duplicate run dir")
    for it in items:
        if it not in runs:
            raise DriverError("E_USAGE: no ramws dir for " + it)
    return runs


def _schema_of(path):
    try:
        with open(path) as f:
            return json.load(f).get("schema")
    except (OSError, ValueError, AttributeError):
        return None


def _is_v2(path):
    return _schema_of(path) == labels_v2.SCHEMA


def _w640(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), "w")


def _grid_for(w, h):
    """capture.GridFor / visioncore.layout.grid_params: smallest power of two >= 16 with cells <= 4096, axes <= 255."""
    s = 16
    while s <= 1 << 16:
        gw, gh = -(-w // s), -(-h // s)
        if gw * gh <= 4096 and gw <= 255 and gh <= 255:
            return s
        s <<= 1
    return 0


def _ready_args(r, k, w, h):
    """READY of chunk k -> (w, h, frames, grid); geometry must be the run's, grid the derived one, ints only."""
    if not isinstance(r, dict) or r.get("schema") != READY_SCHEMA or \
            any(type(r.get(f)) is not int for f in ("chunk", "frames", "w", "h", "grid")):
        raise DriverError("E_READY: fields")
    if r["chunk"] != k or r["frames"] < 1 or (r["w"], r["h"]) != (w, h) or r["grid"] != _grid_for(w, h):
        raise DriverError("E_READY: values")
    if "run_nonce" in r and not (isinstance(r["run_nonce"], str) and NONCE_RE.match(r["run_nonce"])):
        raise DriverError("E_READY: run_nonce")
    return r["w"], r["h"], r["frames"], r["grid"]


def _open_marker(path, cap):
    """The one host-marker reader (W4 contract, mirrored by capture.openMarker): open O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC
    straight away (no isfile/exists probe: no check-then-open window, a FIFO never blocks), fstat S_ISREG with st_nlink == 1 and
    0 < size <= cap, one bounded read of that descriptor. ENOENT -> None (absent). Anything else unsafe -> DriverError rc 5.
    Returns (bytes, mtime_ns)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:  # ELOOP (symlink), EACCES, ENXIO ...
        raise DriverError("E_GATE: marker %s unsafe: %s" % (os.path.basename(path), e.strerror), 5)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size <= 0 or st.st_size > cap:
            raise DriverError("E_GATE: marker %s is not a single-link regular file of 1..%d bytes" % (os.path.basename(path), cap), 5)
        data = os.read(fd, cap + 1)
        if len(data) > cap:
            raise DriverError("E_GATE: marker %s oversize" % os.path.basename(path), 5)
        return data, st.st_mtime_ns
    except OSError as e:
        raise DriverError("E_GATE: marker %s unreadable: %s" % (os.path.basename(path), e.strerror), 5)
    finally:
        os.close(fd)


def _put_marker(path, data):
    """<name>.tmp (fresh, never through a symlink) + rename, 0o640: wlcap never sees a half-written marker."""
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o640)  # on the fd we created: a swapped path cannot take the mode (ADC-W4-02)
        f.write(data)
    os.rename(tmp, path)


def _failed(cdir, k, rc, reason, nonce=None):
    row = {"chunk": k, "rc": rc, "reason": reason[:40]}
    if nonce is not None:
        row["run_nonce"] = nonce
    _put_marker(os.path.join(cdir, ANALYZE_FAILED), json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _analyze_chunk(a, cdir, k, w, h, deadline, nonce=None):
    """Host side of CF-02 for one READY chunk: run chunk_analyze.py (pinned wheel) bounded, then ANALYZED | ANALYZE_FAILED.
    nonce = START run_nonce: READY must carry it (else replay, rc 5) and ANALYZED echoes it with the chunk (AW4-02/06). An unsafe
    READY (symlink, FIFO, hardlink, oversize) writes ANALYZE_FAILED rc 5 and aborts the capture."""
    try:
        got = _open_marker(os.path.join(cdir, READY), MARKER_MAX)
    except DriverError:
        _failed(cdir, k, 5, "E_MARKER_UNSAFE", nonce)
        raise
    if got is None:
        return None  # READY vanished: nothing to answer
    try:
        r = json.loads(got[0])
        cw, ch, frames, grid = _ready_args(r, k, w, h)
    except (ValueError, DriverError):
        _failed(cdir, k, 2, "E_READY", nonce)
        return None
    if r.get("run_nonce") != nonce:  # unconditional: a foreign nonce never passes, even when START had none (ADC-W4-02)
        _failed(cdir, k, 5, "E_MARKER_REPLAY", nonce)
        return None
    cmd = [sys.executable, "-B", a.analyze_script, "analyze", "--chunk-dir", cdir,
           "--w", str(cw), "--h", str(ch), "--frames", str(frames), "--grid", str(grid)]
    tmo = max(0.1, min(a.analyze_timeout, deadline - time.monotonic()))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo)
    except subprocess.TimeoutExpired:
        _failed(cdir, k, 5, "E_ANALYZE_TIMEOUT", nonce)
        return None
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except ValueError:
        out = {}
    if p.returncode != 0 or not isinstance(out, dict) or out.get("chunk") != k:
        code = out.get("code") if isinstance(out, dict) and isinstance(out.get("code"), str) else "E_ANALYZE"
        rc = p.returncode if p.returncode in (2, 3, 4, 5, 6, 7) else 5
        _failed(cdir, k, rc, code, nonce)
        return None
    if nonce is not None:
        out["run_nonce"] = nonce  # the echo that binds ANALYZED to this run's READY
    line = json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n"
    _put_marker(os.path.join(cdir, ANALYZED), line)  # exactly one chunk_analyze.py row (P22) + the echo
    return line


def _lpresent(path):
    """Presence through lstat: a dangling or foreign symlink named like a marker IS present (isfile/exists would follow it)."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _pending_chunks(run_dir, done):
    """chunk_<k> dirs (real dirs, not symlinks) with a READY entry and no host marker entry yet, in k order. Presence only:
    READY is opened (and refused if unsafe) by _analyze_chunk, so a FIFO READY cannot hang this scan."""
    ks = []
    try:
        names = os.listdir(run_dir)
    except OSError:
        return ks
    for n in names:
        mt = CHUNK_RE.match(n)
        if not mt or int(mt.group(1)) in done:
            continue
        d = os.path.join(run_dir, n)
        try:
            if not stat.S_ISDIR(os.lstat(d).st_mode):
                continue
        except OSError:
            continue
        if not _lpresent(os.path.join(d, READY)) or _lpresent(os.path.join(d, ANALYZED)) or _lpresent(os.path.join(d, ANALYZE_FAILED)):
            continue
        ks.append(int(mt.group(1)))
    return sorted(ks)


def _read_start_info(run_dir):
    """START as {t0_ns, run_nonce}, or None while wlcap has not written it. Same reader contract as the markers; unsafe -> rc 5."""
    got = _open_marker(os.path.join(run_dir, START), START_MAX)
    if got is None:
        return None
    try:
        r = json.loads(got[0])
    except ValueError:
        raise DriverError("E_GATE: START unreadable", 5)
    if not isinstance(r, dict) or r.get("schema") != START_SCHEMA or type(r.get("t0_ns")) is not int or r["t0_ns"] < 0:
        raise DriverError("E_GATE: START fields", 5)
    n = r.get("run_nonce")
    if n is not None and not (isinstance(n, str) and NONCE_RE.match(n)):
        raise DriverError("E_GATE: START run_nonce", 5)
    return {"t0_ns": r["t0_ns"], "run_nonce": n}


def _read_start(run_dir):
    """t0_ns of START (CLOCK_MONOTONIC of render 0), or None while wlcap has not written it."""
    info = _read_start_info(run_dir)
    return None if info is None else info["t0_ns"]


def _kill(p):
    if p is not None and p.poll() is None:
        p.kill()
        p.wait(timeout=10)


def _capture(a, cmd, it, run_dir, item_out):
    """Run wlcap bounded; meanwhile analyse READY chunks on the host (--analyze host) and launch the actuator once START
    exists (--exec). Returns the path of the actuator's act.jsonl (or None without --exec)."""
    deadline = time.monotonic() + a.timeout
    rows_path = os.path.join(item_out, "chunk_rows.jsonl")
    act_out = os.path.join(item_out, "act.jsonl") if a.exec else None
    done, act, act_err = set(), None, tempfile.TemporaryFile()
    err = tempfile.TemporaryFile()
    if act_out and os.path.lexists(act_out):
        os.unlink(act_out)  # never relabel from a previous run's act steps
    start_info = None
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    try:
        if a.analyze == "host":
            _w640(rows_path).close()
        while True:
            rc = p.poll()
            if start_info is None and (a.exec or a.analyze == "host"):
                start_info = _read_start_info(run_dir)
            if a.exec and act is None and start_info is not None:
                act = subprocess.Popen(list(a.exec) + ["run", "--prereg", a.prereg, "--item", it,
                                                       "--start", os.path.join(run_dir, START), "--out", act_out,
                                                       "--start-wait-ms", "1000"],
                                       stdout=subprocess.DEVNULL, stderr=act_err)
            if rc is not None:
                break
            if a.analyze == "host":
                for k in _pending_chunks(run_dir, done):
                    done.add(k)
                    line = _analyze_chunk(a, os.path.join(run_dir, "chunk_%d" % k), k, a.w, a.h, deadline,
                                          start_info["run_nonce"] if start_info else None)
                    if line:
                        with open(rows_path, "a") as f:
                            f.write(line)
            if time.monotonic() > deadline:
                raise DriverError("E_GATE: wlcap timeout", 5)
            time.sleep(a.poll_ms / 1000.0)
        if rc != 0:
            err.seek(0)
            msg = err.read()[-400:].decode("utf-8", "replace").strip()[:200]
            raise DriverError("E_GATE: wlcap rc=%d %s" % (rc, msg), rc if rc in (2, 3, 4, 5, 6, 7) else 5)
        if not a.exec:
            return None
        if act is None:
            raise DriverError("E_GATE: no START from wlcap, actuator not run", 5)
        arc = act.wait(timeout=max(0.1, deadline - time.monotonic()))
        if arc != 0:
            raise DriverError("E_GATE: actuator rc=%d" % arc, arc if arc in (2, 3, 4, 5, 6, 7) else 5)
        return act_out
    except subprocess.TimeoutExpired:
        raise DriverError("E_GATE: actuator timeout", 5)
    finally:
        _kill(p)
        _kill(act)
        err.close()
        act_err.close()


def run(a):
    sch = _schema_of(a.prereg)
    v3 = sch == labels_v3.SCHEMA
    v2 = sch == labels_v2.SCHEMA or v3  # v3 shares the v2 action/actuator-label rules; labels_v3 lowers its own ops
    lib = labels_v3 if v3 else labels_v2 if v2 else recorder
    m = lib.load_manifest(a.prereg)
    sha = lib.prereg_sha256(m)
    want = a.expect_prereg_sha256 or (labels_v3.PINNED_PREREG_V3_SHA256 if v3 else PINNED_PREREG_V2_SHA256 if v2 else PINNED_PREREG_SHA256)
    if sha != want:
        raise DriverError("E_PIN: prereg sha %s != %s" % (sha, want), 5)
    if v2 and a.wm_profile != "default":
        raise DriverError("E_SCHEMA: prereg v2 has no wm_profile other than default")
    order = recorder.capture_order(m)
    items = a.item or order
    if any(i not in order for i in items):
        raise DriverError("E_SCHEMA: unknown item")
    items = [i for i in order if i in items]  # capture_order wins: WL-6 first
    w, h = parse_res(a.res, [tuple(r) for r in m.get("resolutions", [])])
    if a.quota_bytes is not None and not 0 < a.quota_bytes <= Q_MAX:
        raise DriverError("E_USAGE: --quota-bytes must be in 1..2400 MiB")
    kept = [i for i in items if _kept(a, w, h)]
    if _plan_bytes(len(kept), a.quota_bytes) > PLAN_MAX:
        raise DriverError("E_USAGE: kept plan exceeds 10 GiB")
    layout = None
    if a.wm_profile == "qtile":
        q = _qtile()
        rc, code = q.profile_gate("qtile", a.image_has_qtile or "")
        if rc:
            raise DriverError("E_GATE: " + code, 5)
        layout = q.load_layout(w, h)
    if a.exec and not v2:
        raise DriverError("E_USAGE: --exec needs a prereg v2 plan")
    if v2 and not a.exec:  # labels of an item with actions are timed by the actuator AJSON, never by the plan
        acting = [i["id"] for i in m["items"] if i["id"] in items and i.get("steps")]
        if acting:
            raise DriverError("E_USAGE: prereg v2 items with actions need --exec (actuator labels): " + ",".join(acting))
    if a.analyze == "host" and not 1 <= a.analyze_timeout <= ANALYZE_TIMEOUT_MAX:
        raise DriverError("E_USAGE: --analyze-timeout")
    a.w, a.h = w, h
    if not a.fake_fb and not (a.fb and a.display and a.iso):
        raise DriverError("E_USAGE: real mode needs --fb --display --iso (or --fake-fb)")
    _out_ok(a.out)
    runs = {i: recorder._ramws_dir(d) for i, d in _runs(a, items).items()}
    os.makedirs(os.path.join(a.out, "labels"), mode=0o750, exist_ok=True)
    run_ids, label_files, chunk_rows = [], [], {}
    for it in items:
        item_out = os.path.join(a.out, it)
        os.makedirs(item_out, mode=0o750, exist_ok=True)
        cmd = build_cmd(a, it, w, h, runs[it], item_out)
        act = _capture(a, cmd, it, runs[it], item_out)
        if act:  # CF-01: label times are the actuator's t_us from its act_steps, never the plan's
            try:
                rows = lib.labels_from_act(m, it, w, h, labels_v2.read_act(act))
            except labels_v2.WLError as e:
                raise DriverError("E_GATE: act steps refused: %s" % e, e.rc if e.rc in (2, 3, 4, 5, 6, 7) else 5)
            if not rows:
                raise DriverError("E_GATE: no label row from the act steps", 5)
        else:
            rows = lib.labels_for(m, it, w, h) if v2 else recorder.labels_for(m, it, w, h, a.wm_profile, layout)
        lf = os.path.join("labels", it + ".jsonl")
        with _w640(os.path.join(a.out, lf)) as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
        run_ids.append(os.path.basename(runs[it]))
        if a.analyze == "host":
            rf = os.path.join(it, "chunk_rows.jsonl")
            with open(os.path.join(a.out, rf), "rb") as f:
                chunk_rows[it] = {"file": rf, "sha256": hashlib.sha256(f.read()).hexdigest()}
        label_files.append(lf)
    assert_no_frames(a.out)
    ho = {"schema": SCHEMA, "run_ids": run_ids, "label_files": label_files, "prereg_sha256": sha,
          "items": items, "wm_profile": a.wm_profile}
    if a.exec:
        ho["label_timebase"] = "actuator"
    if chunk_rows:
        ho["chunk_rows"] = chunk_rows
    with _w640(os.path.join(a.out, "wl_handover.json")) as f:
        json.dump(ho, f, sort_keys=True)
    print(json.dumps(ho, sort_keys=True))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="driver.py")
    p.add_argument("--prereg", default=os.path.join(HERE, "prereg_wl.json"))
    p.add_argument("--expect-prereg-sha256", default=None)
    p.add_argument("--require-continuous", choices=("on", "off"), default=None)
    p.add_argument("--analyze", choices=("off", "engine", "host"), default="off")
    p.add_argument("--analyze-script", default=ANALYZE_SCRIPT)
    p.add_argument("--analyze-timeout", type=int, default=120)
    p.add_argument("--poll-ms", type=int, default=50)
    p.add_argument("--exec", nargs="+", default=None)
    p.add_argument("--item", action="append")
    p.add_argument("--wm-profile", choices=("default", "qtile"), default="default")
    p.add_argument("--image-has-qtile", default="")
    p.add_argument("--det", choices=("on", "off"), default="on")
    p.add_argument("--mode", choices=("wl", "live", "handover-1s"), default="wl")
    p.add_argument("--retain", choices=("auto", "on", "off"), default="auto")
    p.add_argument("--res", default="1920x1080")
    p.add_argument("--chunk", type=int, default=60)
    p.add_argument("--seconds", type=int, default=6)
    p.add_argument("--quota-bytes", type=int, default=None)
    p.add_argument("--ramws-dir")
    p.add_argument("--run", action="append", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--wlcap", nargs="+", default=["srf-cu-wlcap"])
    p.add_argument("--fake-fb", action="store_true")
    p.add_argument("--fb", default="")
    p.add_argument("--display", default="")
    p.add_argument("--iso", default="")
    p.add_argument("--timeout", type=int, default=300)
    return p


def main(argv=None):
    os.umask(0o027)
    try:
        a = parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        if a.chunk < 1 or a.seconds < 1 or a.poll_ms < 1:
            raise DriverError("E_USAGE: chunk/seconds/poll-ms")
        return run(a)
    except DriverError as e:
        print(str(e), file=sys.stderr)
        return e.rc
    except recorder.WLError as e:
        print(str(e), file=sys.stderr)
        return e.rc
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print("E_IO: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
