#!/usr/bin/env python3
"""int-demo: ONE live run of the integrated Track P stack on a private Blender (vision job vpintdemo*).

Track P (claude-fable-5-2). Integrates by import, edits no unit:
  ref_broker (F1 bridge + broker, seam applied IN MEMORY by ref_auction.broker_adapter.patched_source),
  ref_auction (AuctionScheduler fills the `_pick_agent` seam of the RewindBroker),
  ref_rewind / ref_rewind_ckpt / ref_rewind_lanes / ref_rewind_verify (rewind layer, ops, judge),
  ref_rewind_demo.rewind_live (the scenario: one deliberate wrong edit, judge reject, capsule rewind),
  ref_overlay (record.sh + compose.py), ref_projection (project.py), ref_agents (stub agents, second RT).

Usage: demo_live.py --job vpintdemo --rt /dev/shm/vqa-cu/ref-int-demo-<tag> --evidence <dir> [--no-auction]
Exit 0 = every scenario check holds and every stage of the stack ran.
"""
import argparse
import glob
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(HERE)
RT_PREFIX = "/dev/shm/vqa-cu/ref-int-demo"
JOB_PREFIX = "vpintdemo"
SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]


def install_seamed_broker():
    """Put a seam-patched copy of ref_broker/broker.py in sys.modules as `broker` (file untouched)."""
    sys.path.insert(0, os.path.join(PRODUCT, "ref_auction"))
    import broker_adapter as AD
    path = os.path.join(PRODUCT, "ref_broker", "broker.py")
    with open(path) as f:
        src = AD.patched_source(f.read())
    mod = types.ModuleType("broker")
    mod.__file__ = path
    sys.modules["broker"] = mod
    sys.path.insert(0, os.path.dirname(path))
    exec(compile(src, path, "exec"), mod.__dict__)
    return AD


def settle(look, tries=8, gap_s=0.4, first_s=0.6, sleep=time.sleep):
    """Wait until the display stops changing: two consecutive looks with the same PNG bytes.
    Found live (evidence/flake_live2, flake_live4): the frame taken right after a restore job can show
    Blender BEFORE its redraw (stale outliner, blank viewport) although the scene digest already matches,
    and the rollback witness then reports WITNESS_NOT_RESTORED. Returns the number of looks spent."""
    def sha():
        rc, out = look()
        try:
            with open(out["frame"], "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except (OSError, KeyError, TypeError):
            return None
    if tries <= 0:
        return 0
    sleep(first_s)
    prev, n = sha(), 1
    while n < tries:
        sleep(gap_s)
        cur = sha()
        n += 1
        if cur is not None and cur == prev:
            break
        prev = cur
    return n


def wire_settle(tries=8):
    """Every broker of this process waits for a settled display before the frame of a restore job."""
    import rewind_broker as RB
    base = RB.RewindBroker

    class SettledBroker(base):
        settle_looks = 0

        def _capture(self, by_event, regions):
            if str(by_event or "").startswith("ctl.restore."):
                self.settle_looks += settle(self.vision.look, tries=tries)
            return base._capture(self, by_event, regions)

    RB.RewindBroker = SettledBroker


def wire_auction(AD):
    """RewindBroker -> AuctionBroker(RewindBroker) with an AuctionScheduler over the broker's ring."""
    import scheduler as SCH
    import rewind_broker as RB
    base = AD.make_auction_broker(RB.RewindBroker)

    class DemoBroker(base):
        def __init__(self, *a, **kw):
            base.__init__(self, *a, **kw)
            self.scheduler = SCH.AuctionScheduler(self.agents)

    RB.RewindBroker = DemoBroker
    return SCH.POLICY


def display_of(rt):
    """DISPLAY of the Blender this run started (the process whose environment names our RT)."""
    for p in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(p, "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue
        if ("VP_SWARM_RT=%s" % rt).encode() in env:
            for e in env:
                if e.startswith(b"DISPLAY="):
                    return e[8:].decode()
    return None


def run(cmd, timeout=300, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    return p.returncode, (p.stdout or "")[-600:], (p.stderr or "")[-600:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default=JOB_PREFIX)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--no-auction", action="store_true", help="round-robin turn order (fallback)")
    ap.add_argument("--no-settle", action="store_true", help="negative control: restore frame without settling")
    ap.add_argument("--osb-room", default="viewpoint-capsule-test")
    ap.add_argument("--tasklight-room", default="demo")
    a = ap.parse_args()
    a.rt, a.evidence, a.mutant = os.path.abspath(a.rt), os.path.abspath(a.evidence), None
    if not a.rt.startswith(RT_PREFIX) or not a.job.startswith(JOB_PREFIX):
        print("refused: RT must be under %s* and the job must be named %s*" % (RT_PREFIX, JOB_PREFIX))
        return 2
    if os.path.exists(os.path.join(a.rt, "events.jsonl")):
        print("refused: %s is not fresh" % a.rt)
        return 2
    stages = {}
    AD = install_seamed_broker()
    sys.path.insert(0, os.path.join(PRODUCT, "ref_rewind_demo"))
    import rewind_live as L  # the scenario, unedited
    wire_settle(0 if a.no_settle else 8)
    stages["settle_before_restore_frame"] = not a.no_settle
    stages["scheduler"] = "round_robin" if a.no_auction else wire_auction(AD)
    os.makedirs(a.rt, exist_ok=True)
    os.makedirs(a.evidence, exist_ok=True)
    with open(os.path.join(a.rt, "claims.json"), "w") as f:
        json.dump(L.CLAIMS, f)
    os.environ["VP_SWARM_NONCE"] = secrets.token_hex(8)
    rc, tail = L.launch(a.job, a.rt, os.environ["VP_SWARM_NONCE"])
    print("vision start rc=%d %s" % (rc, tail), flush=True)
    if rc != 0:
        L.stop(a.job, a.rt)
        return 2
    checks, out, rec_pid, rec_dir = [], {}, None, os.path.join(a.rt, "rec")
    try:
        disp = display_of(a.rt)
        stages["display"] = disp
        if disp and disp not in (":1", ":2") and not disp.startswith((":1.", ":2.")):
            rc, o, e = run(["bash", os.path.join(PRODUCT, "ref_overlay", "record.sh"), disp, "intdemo", rec_dir], 30)
            rec_pid = int(o.strip().splitlines()[-1]) if rc == 0 and o.strip() else None
            stages["record"] = {"rc": rc, "pid": rec_pid, "err": e[-200:]}
        out = L.scenario(a, checks)
    except Exception as e:  # noqa: BLE001
        checks.append({"check": "scenario_completed", "ok": False, "detail": "%s: %s" % (type(e).__name__, e)})
        print("FAIL scenario_completed %s: %s" % (type(e).__name__, e), flush=True)
    finally:
        if rec_pid:
            time.sleep(1.0)
            try:
                os.kill(rec_pid, signal.SIGINT)
                for _ in range(100):
                    if not os.path.exists("/proc/%d" % rec_pid):
                        break
                    time.sleep(0.1)
            except OSError:
                pass
        print("vision stop rc=%d %s" % L.stop(a.job, a.rt), flush=True)
    red = [c["check"] for c in checks if not c["ok"]]
    out.update({"mutant": None, "checks": checks, "red": red, "job": a.job, "rt": a.rt})
    L.save_evidence(a, out)

    # -- overlay: cursors + queue strip over the display recording
    vids = sorted(glob.glob(os.path.join(rec_dir, "intdemo_*.mp4")))
    if vids:
        raw = vids[-1]
        composed = os.path.join(a.evidence, "demo_composed.mp4")
        rc, o, e = run(SCOPE + ["python3", os.path.join(PRODUCT, "ref_overlay", "compose.py"), "--video", raw,
                                "--out", composed, "--rt", a.rt, "--start-file", raw[:-4] + ".start_ms",
                                "--crf", "28"], 600)
        stages["compose"] = {"rc": rc, "out": composed if os.path.exists(composed) else None, "tail": (o + e)[-300:]}
    else:
        stages["compose"] = {"rc": None, "out": None, "tail": "no recording"}

    # -- projection: OpenSwitchboard room + TaskLight room, from the broker's events.jsonl only
    env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()))
    pdir = os.path.join(a.rt, "projection")
    rc, o, e = run(["python3", os.path.join(PRODUCT, "ref_projection", "project.py"), "--rt", a.rt, "--once",
                    "--osb-room", a.osb_room, "--tasklight-room", a.tasklight_room, "--out", pdir], 300, env=env)
    stages["projection"] = {"rc": rc, "tail": (o + e)[-400:]}
    if os.path.isdir(pdir):
        shutil.copytree(pdir, os.path.join(a.evidence, "projection"), dirs_exist_ok=True)

    # -- stub agents (ref_agents --stub): 4 scripted agents write contract intents into their own RT
    art = a.rt + "-agents"
    if not os.path.exists(os.path.join(art, "intents")):
        os.makedirs(os.path.join(art, "frames"), exist_ok=True)
        seed = os.path.join(a.evidence, "frame_p3.png")   # the stubs observe the last accepted live frame
        run(["python3", os.path.join(PRODUCT, "ref_agents", "tools", "make_frame.py"), "--rt", art, "--frame", "0"]
            + (["--png", seed] if os.path.exists(seed) else []), 60)
        rc, o, e = run(SCOPE + ["python3", os.path.join(PRODUCT, "ref_agents", "runner.py"), "run", "--rt", art,
                                "--evidence", os.path.join(a.evidence, "stub_agents"), "--stub", "--stub-count", "2",
                                "--init-rt", "--max-seconds", "60"], 180)
        n = len(glob.glob(os.path.join(art, "intents", "*", "*.json")))
        stages["stub_agents"] = {"rc": rc, "intents": n, "tail": (o + e)[-300:]}

    evs = []
    p = os.path.join(a.rt, "events.jsonl")
    if os.path.exists(p):
        with open(p) as f:
            evs = [json.loads(x) for x in f if x.strip()]
    recs = {}
    for r in evs:
        recs[r.get("rec")] = recs.get(r.get("rec"), 0) + 1
    stages["events_by_rec"] = recs
    out["stages"] = stages
    out["ok"] = (not red and bool(checks) and stages.get("compose", {}).get("rc") == 0
                 and stages["projection"]["rc"] == 0)
    with open(os.path.join(a.evidence, "live.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print(json.dumps({"ok": out["ok"], "red": red, "checks": len(checks), "stages": stages}, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
