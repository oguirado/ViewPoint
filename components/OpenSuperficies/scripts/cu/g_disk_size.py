#!/usr/bin/env python3
"""g_disk_size: smoke output <= 64 MB; every episode dir under --out (any top-level dir except kept_frames, bin/ included) and every top-level file
and each fail_evidence/<ep> <= 100 KB (fail_evidence/<ep> also <= 6 files); kept_frames/* need a live KEEP.json.

  g_disk_size.py --out DIR [--no-pin]   rc 0 ok, 2 usage, 5 breach. Never 1.
  --no-pin: size rules only, no vision.pin activation (ADC-W6A-05: a fresh clone has no gitignored wheel; the size check imports nothing from vision).
"""
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
if "--no-pin" not in sys.argv[1:]:
    from vision import pin  # noqa: E402  (ADV-08: pin first)

    pin.activate()

MAX_TOTAL = 64 * 1024 * 1024
MAX_EP_BYTES = 100 * 1024
MAX_EP_FILES = 6


def files(d):
    for dp, _, fn in os.walk(d):
        for f in fn:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                yield p


def check(out_dir, now=None):
    now = time.time() if now is None else now
    bad = []
    total = sum(os.path.getsize(p) for p in files(out_dir))
    if total > MAX_TOTAL:
        bad.append({"code": "total_bytes", "bytes": total})
    fe = os.path.join(out_dir, "fail_evidence")
    if os.path.isdir(fe):
        for ep in sorted(os.listdir(fe)):
            fl = list(files(os.path.join(fe, ep)))
            size = sum(os.path.getsize(p) for p in fl)
            if size > MAX_EP_BYTES:
                bad.append({"code": "fail_evidence_bytes", "ep": ep, "bytes": size})
            if len(fl) > MAX_EP_FILES:
                bad.append({"code": "fail_evidence_files", "ep": ep, "files": len(fl)})
    for ep in sorted(os.listdir(out_dir)):  # ADC-W3-10: the 100 KB/episode cap holds for every out dir episode, not only fail_evidence
        d = os.path.join(out_dir, ep)
        if os.path.islink(d):
            continue
        if os.path.isfile(d):  # ADC2-W3-05: a top-level file is an episode file too
            if os.path.getsize(d) > MAX_EP_BYTES:
                bad.append({"code": "top_file_bytes", "file": ep, "bytes": os.path.getsize(d)})
            continue
        if ep in ("fail_evidence", "kept_frames") or not os.path.isdir(d):
            continue
        size = sum(os.path.getsize(p) for p in files(d))
        if size > MAX_EP_BYTES:
            bad.append({"code": "episode_bytes", "ep": ep, "bytes": size})
    kf = os.path.join(out_dir, "kept_frames")
    if os.path.isdir(kf):
        for run in sorted(os.listdir(kf)):
            try:
                ttl = json.load(open(os.path.join(kf, run, "KEEP.json")))["ttl_until"]
                if not isinstance(ttl, int) or ttl < now:
                    raise ValueError("expired")
            except (OSError, ValueError, KeyError, TypeError):
                bad.append({"code": "keep_missing_or_expired", "run": run})
    return bad


def main(argv):
    argv = [a for a in argv if a != "--no-pin"]  # ADC2-W6A-04: --no-pin anywhere in argv (the import guard above already honours it anywhere)
    if len(argv) != 2 or argv[0] != "--out" or not os.path.isdir(argv[1]):
        sys.stderr.write("usage: g_disk_size.py --out DIR [--no-pin]\n")
        return 2
    try:
        bad = check(argv[1])
    except Exception as e:
        bad = [{"code": "error", "detail": repr(e)}]
    print(json.dumps({"code": "E_DISK_SIZE" if bad else "OK", "offending": bad}, sort_keys=True))
    return 5 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
