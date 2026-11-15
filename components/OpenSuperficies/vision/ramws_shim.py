"""CLI shim over vadapt.ramws (tool=opensuperficies, campaign=os-cu-0) for Go callers. JSON on stdout.

  sweep | sweep-kept --build B [--now S] | status | check-disk DIR [--since NS]   one-shot
  session --run-id ID [--quota-bytes N] [--root R] [--share-uid UID]              holds open_run (flock + peak) until EOF/SIGTERM
--share-uid UID (1..2^31-1) shares the run dir with the capsule uid ONLY through the engine API open_run(share_with_uid=UID)
(ve-sealed-1-law4); a pinned ramws without that keyword fails closed: rc 5 E_RAM_SHARE_UNSUPPORTED. The shim never changes
file permissions or ACLs itself. The share block is echoed in the open line and in the receipt.
Session stdin, one JSON object per line: {"cmd": "path"|"reserve"|"account"|"release"|"receipt"|"close", ...}.
--root (any command) is honoured only with env OS_CU_TEST_ROOT=1 and realpath under /dev/shm/vqa-ramws-test/, else rc 2.
rc: 0 ok, 2 usage, 5 RamError / pin / LAW breach / unforeseen error. Never 1.
"""
import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision import pin  # noqa: E402  (must precede every engine import)

pin.activate()
import vadapt.ramws as ramws  # noqa: E402

TOOL, CAMPAIGN = "opensuperficies", "os-cu-0"
EXTRA_FRAME_EXT = (".raw", ".xwd", ".rgb24")
EXTRA_FRAME_NAMES = ("frames.raw", "Xvfb_screen0", "frames.rgb24")


def out(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def parse(argv):
    a, opt, pos = list(argv), {}, []
    while a:
        x = a.pop(0)
        if x.startswith("--"):
            if not a:
                raise ValueError("missing value for %s" % x)
            opt[x[2:]] = a.pop(0)
        else:
            pos.append(x)
    return opt, pos


FAIL_EV_MAX_BYTES, FAIL_EV_MAX_FILES = 100 * 1024, 6


def _tree(p):
    """(bytes, files) of a directory tree, symlinks not followed."""
    nbytes = nfiles = 0
    for dp, _dn, fn in os.walk(p):
        for f in fn:
            nfiles += 1
            nbytes += os.lstat(os.path.join(dp, f)).st_size
    return nbytes, nfiles


def _keep_live(run, now):
    """kept_frames/<run> is allowed only with a readable KEEP.json (ramws.keep_frames schema), ttl_until >= now and the
    tree within cap_mb (<= ramws.KEEP_CAP_MB). Returns (live, over_cap)."""
    try:
        with open(os.path.join(run, "KEEP.json"), encoding="utf-8") as f:
            keep = json.load(f)
        ttl, cap = keep["ttl_until"], keep["cap_mb"]
    except (OSError, ValueError, KeyError, TypeError):
        return False, False
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < now:
        return False, False
    if isinstance(cap, bool) or not isinstance(cap, int) or not 0 < cap <= ramws.KEEP_CAP_MB:
        return False, False
    nbytes, _ = _tree(run)
    over = nbytes - os.lstat(os.path.join(run, "KEEP.json")).st_size > cap * (1 << 20)
    return not over, over


def check_disk(d, since=None, now=None):
    """ramws.check_disk(allow=()) plus the CU blind spots the engine law omits (ER-006, ADC2-03): .raw/.xwd, frames.raw and
    Xvfb_screen0 are never allowed; other frame kinds only inside top-level kept_frames/<run>/ with a live KEEP.json
    within cap and top-level fail_evidence/<ep>/ (<= 100 KB, <= 6 files). Nested kept_frames/fail_evidence are not allowed."""
    now = time.time() if now is None else now
    found, live = set(), {}
    for rel in ramws.check_disk(d, allow=(), since=since):
        parts = rel.split(os.sep)
        if len(parts) >= 3 and parts[0] == "fail_evidence":
            continue  # capped below
        if len(parts) >= 3 and parts[0] == "kept_frames":
            run = os.path.join(d, parts[0], parts[1])
            if run not in live:
                live[run] = _keep_live(run, now)
            if live[run][0]:
                continue
        found.add(rel)
    for run, (_ok, over) in live.items():
        if over:
            found.add(os.path.relpath(run, d))
    for dp, dn, fn in os.walk(d):  # no pruning: raw frames are forbidden on disk everywhere
        for f in fn:
            if f.lower().endswith(EXTRA_FRAME_EXT) or f in EXTRA_FRAME_NAMES:
                p = os.path.join(dp, f)
                if since is None or os.lstat(p).st_mtime_ns > since:
                    found.add(os.path.relpath(p, d))
    fe = os.path.join(d, "fail_evidence")
    if os.path.isdir(fe) and not os.path.islink(fe):
        for g in sorted(os.listdir(fe)):
            g = os.path.join(fe, g)
            if os.path.isdir(g) and not os.path.islink(g):
                nbytes, nfiles = _tree(g)
                if nbytes > FAIL_EV_MAX_BYTES or nfiles > FAIL_EV_MAX_FILES:
                    found.add(os.path.relpath(g, d))
    return sorted(found)


TEST_ROOT_PREFIX = "/dev/shm/vqa-ramws-test/"


def _root(opt):
    """--root is a test hook: honoured only with OS_CU_TEST_ROOT=1 AND realpath(root) strictly under /dev/shm/vqa-ramws-test/
    (STORAGE LAW: the run dir lives under the law root; RJ-02, ADC2-03)."""
    if "root" not in opt:
        return ramws.ROOT
    if os.environ.get("OS_CU_TEST_ROOT") != "1":
        raise ValueError("--root requires OS_CU_TEST_ROOT=1")
    if not os.path.realpath(opt["root"]).startswith(TEST_ROOT_PREFIX):
        raise ValueError("--root must resolve under %s" % TEST_ROOT_PREFIX)
    return opt["root"]


SHARE_VIA = "open_run.share_with_uid"


def _share_uid(opt):
    """None when --share-uid is absent; else the validated uid (ValueError -> rc 2)."""
    if "share-uid" not in opt:
        return None
    try:
        uid = int(opt["share-uid"])
    except ValueError:
        raise ValueError("--share-uid must be an integer") from None
    if not 0 < uid < 1 << 31:
        raise ValueError("--share-uid must be in 1..2147483647")
    return uid


def _supports_share():
    try:
        return "share_with_uid" in inspect.signature(ramws.open_run).parameters
    except (TypeError, ValueError):
        return False


def _merged_share(ws, share):
    """Shim share block with the engine receipt share (uid, method, files_read) merged in; engine keys win on common keys."""
    try:
        eng = ws.receipt().get("share")
    except Exception:  # noqa: BLE001  the open line must still go out
        eng = None
    return dict(share, **(eng if isinstance(eng, dict) else {}))


def session(opt):
    rid = opt.get("run-id")
    if not rid:
        raise ValueError("--run-id required")
    q = opt.get("quota-bytes")
    quota = int(q) if q is not None else ramws.RUN_DEFAULT
    kw = {"root": _root(opt)} if "root" in opt else {}
    uid = _share_uid(opt)
    share = None
    if uid is not None:
        if not _supports_share():
            raise ramws.RamError("E_RAM_SHARE_UNSUPPORTED", "pinned ramws.open_run has no share_with_uid (needs ve-sealed-1-law4)")
        kw["share_with_uid"] = uid
        share = {"uid": uid, "via": SHARE_VIA}
    ws = ramws.open_run(CAMPAIGN, rid, quota, tool=TOOL, **kw)  # janitor runs inside open_run
    hello = {"code": "OK", "event": "open", "root": str(ws.root), "run_id": rid, "fstype": ws._fstype, "quota": ws.quota}
    if share:
        hello["share"] = _merged_share(ws, share)
    out(hello)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                cmd = req["cmd"]
                if cmd == "path":
                    out({"code": "OK", "path": str(ws.path(req["rel"]))})
                elif cmd == "reserve":
                    ws.reserve(req["n"])
                    out({"code": "OK", "used": ws.used})
                elif cmd == "account":
                    out({"code": "OK", "used": ws.account()})
                elif cmd == "release":
                    out({"code": "OK", "freed": ws.release(req["rel"])})
                elif cmd == "receipt":
                    rec = ws.receipt()
                    if share:
                        # merge, never overwrite: the engine receipt share ({uid, method, files_read}) wins on common keys
                        rec = dict(rec, share=dict(share, **(rec.get("share") or {})))
                    out({"code": "OK", "receipt": rec})
                elif cmd == "close":
                    ws.close()
                    out({"code": "OK", "closed": True})
                    return 0
                else:
                    out({"code": "E_USAGE", "detail": "unknown cmd"})
            except ramws.RamError as e:
                out({"code": e.code, "detail": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                out({"code": "E_USAGE", "detail": str(e)})
            except Exception as e:  # noqa: BLE001  one bad request must not kill the session or exit 1
                out({"code": "E_GATE", "detail": "%s: %s" % (type(e).__name__, e)})
    finally:
        ws.close()
    return 0


def main(argv):
    cmd = argv[0] if argv else None
    try:
        opt, pos = parse(argv[1:])
        if cmd == "sweep":
            out({"code": "OK", "removed": ramws.sweep(TOOL, _root(opt))})
        elif cmd == "sweep-kept" and "build" in opt:
            out({"code": "OK", "removed": ramws.sweep_kept(opt["build"], float(opt["now"]) if "now" in opt else time.time())})
        elif cmd == "status":
            res = ramws.status(TOOL, _root(opt))
            res["code"] = "OK"
            out(res)
        elif cmd == "check-disk" and len(pos) == 1:
            if not os.path.isdir(pos[0]):
                raise ValueError("check-disk: not a directory: %s" % pos[0])
            bad = check_disk(pos[0], int(opt["since"]) if "since" in opt else None)
            if bad:
                out({"code": "E_DISK_FRAMES", "offending": bad})
                return 5
            out({"code": "OK", "offending": []})
        elif cmd == "session":
            return session(opt)
        else:
            out({"code": "E_USAGE", "detail": "sweep|sweep-kept --build B|status|check-disk DIR [--since NS]|session --run-id ID [--quota-bytes N] [--root R] [--share-uid UID]"})
            return 2
    except ramws.RamError as e:
        out({"code": e.code, "detail": str(e)})
        return 5
    except (ValueError, OSError) as e:
        out({"code": "E_USAGE", "detail": str(e)})
        return 2
    except Exception as e:  # noqa: BLE001  rc 1 is unreachable: anything unforeseen is a gate failure
        out({"code": "E_GATE", "detail": "%s: %s" % (type(e).__name__, e)})
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
