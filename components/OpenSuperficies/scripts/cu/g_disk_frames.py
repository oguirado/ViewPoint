#!/usr/bin/env python3
"""g_disk_frames: STORAGE LAW gate. Runs the CU smoke into build/gate_disk/<pid>/ and fails (rc 5) on any frame on disk.

  g_disk_frames.py [--plant NAME] [--plant-ramws] [--run-prefix PFX]   (plant flags exist only to prove the gate catches leaks)
RD3-03 (W4): the ramws root is host-global. Only run dirs this gate created count: names starting with --run-prefix
(default gate-<pid> and leftover-<pid>); every other run dir (kept foreign sessions) is counted as "ignored_foreign" and never fails.
Smoke: srf-cu-snap --fake-fb x8 (frames.raw in a ramws session dir) + srf-cu-exec replay of the L2 fixture.
Checks: check_disk(build/, since=stamp) empty (incl. *.raw *.xwd frames.raw Xvfb_screen0), /dev/shm/vqa-ramws/opensuperficies
empty after, ram_peak_bytes <= quota, g_disk_size on the smoke output. rc: 0 ok, 2 usage, 5 breach. Never 1.
"""
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from vision import pin  # noqa: E402  (ADV-08: pin first)

pin.activate()
from vision import ramws_shim  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import g_disk_size  # noqa: E402

RAMROOT = "/dev/shm/vqa-ramws/opensuperficies"
PLANS = os.path.join(REPO, "testdata", "cu", "plans")


def out(o):
    sys.stdout.write(json.dumps(o, sort_keys=True) + "\n")
    sys.stdout.flush()


def sh(cmd, test_env=False, **kw):
    env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin", CGO_ENABLED="0")
    if test_env:  # AW1-07: the fake actuator path of srf-cu-exec is refused without OS_CU_TEST=1
        env["OS_CU_TEST"] = "1"
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=240, **kw)


def parse_args(argv):
    """-> dict(plant, plant_ramws, smoke_out, run_prefix[list|None]); ValueError on usage errors (caller maps to rc 2)."""
    o, a = {"plant": None, "plant_ramws": False, "smoke_out": None, "run_prefix": None}, list(argv)
    while a:
        x = a.pop(0)
        if x == "--plant" and a:
            o["plant"] = a.pop(0)
        elif x == "--plant-ramws":
            o["plant_ramws"] = True
        elif x == "--smoke-out" and a:
            o["smoke_out"] = a.pop(0)
        elif x == "--run-prefix" and a:
            pfx = a.pop(0)
            if not pfx or "/" in pfx or pfx in (".", ".."):
                raise ValueError("bad --run-prefix")
            o["run_prefix"] = (o["run_prefix"] or []) + [pfx]
        else:
            raise ValueError("usage")
    return o


def default_prefixes(pid=None):
    pid = os.getpid() if pid is None else pid
    return ["gate-%d" % pid, "leftover-%d" % pid]


def _owned(name, prefixes):
    return any(name == p or name.startswith(p + "-") or name.startswith(p + ".") for p in prefixes)


def _walk_rel(top, rel):
    found = [rel]
    for dp, dn, fn in os.walk(os.path.join(top, rel)):
        base = os.path.relpath(dp, top)
        found.extend(os.path.join(base, d) for d in dn)
        found.extend(os.path.join(base, f) for f in fn)
    return sorted(set(found))


def leftovers(prefixes=None):
    """-> (own, foreign_count). own = everything under run dirs this gate created (name == prefix or prefix-*), under the
    root and under its os-cu-0/ campaign dir; foreign_count = other run dirs (kept sessions of other invocations)."""
    prefixes = list(prefixes) if prefixes else default_prefixes()
    own, foreign = [], 0
    try:
        top = sorted(os.listdir(RAMROOT))
    except OSError:
        return [], 0
    for n in top:
        full = os.path.join(RAMROOT, n)
        if n == "os-cu-0" and os.path.isdir(full) and not os.path.islink(full):
            mine = []
            for c in sorted(os.listdir(full)):
                if _owned(c, prefixes):
                    mine.extend(_walk_rel(RAMROOT, os.path.join(n, c)))
                else:
                    foreign += 1
            if mine:
                own.append(n)  # the campaign dir is reported only when it holds one of our own runs
                own.extend(mine)
        elif _owned(n, prefixes):
            own.extend(_walk_rel(RAMROOT, n))
        else:
            foreign += 1
    return sorted(own), foreign


def check_build(build, stamp):
    """RD3-03 (build side), ADC-W4-01: ONE check_disk over build/ itself, so the top-level fail_evidence/kept_frames exemptions of
    ramws_shim stay top-level (build/gate_disk/fail_evidence/<ep>/*.png is an own frame and fails; build/fail_evidence/<ep>/*.png
    is valid), minus the results under foreign room_* scratch dirs (counted, never failed). -> (offending, ignored_rooms)."""
    rooms = {n for n in os.listdir(build) if n.startswith("room_") and os.path.isdir(os.path.join(build, n))
             and not os.path.islink(os.path.join(build, n))}
    return [r for r in ramws_shim.check_disk(build, since=stamp) if r.split(os.sep, 1)[0] not in rooms], len(rooms)


def gate(plant, plant_ramws, prefixes=None):
    stamp = time.time_ns()
    build = os.path.join(REPO, "build")
    work = os.path.join(build, "gate_disk", str(os.getpid()))
    os.makedirs(os.path.join(work, "bin"), exist_ok=True)
    problems = []
    sh([sys.executable, "-B", os.path.join(REPO, "vision", "ramws_shim.py"), "sweep"])
    r = sh(["go", "build", "-p", "2", "-o", os.path.join(work, "bin") + "/", "./cmd/srf-cu-snap", "./cmd/srf-cu-exec"])
    if r.returncode:
        return 5, {"code": "E_BUILD", "detail": r.stderr[-400:]}, work
    sess = subprocess.Popen([sys.executable, "-B", os.path.join(REPO, "vision", "ramws_shim.py"), "session",
                             "--run-id", "gate-%d" % os.getpid()], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    peak = quota = 0
    try:
        def rpc(o):
            sess.stdin.write(json.dumps(o) + "\n")
            sess.stdin.flush()
            return json.loads(sess.stdout.readline())
        first = json.loads(sess.stdout.readline())
        if first.get("code") != "OK":
            return 5, {"code": "E_SESSION", "detail": first}, work
        # the snap needs the session run dir itself (a direct child of the law root, FX1 ADC-02), not a subdir
        ramdir = os.path.dirname(os.path.dirname(rpc({"cmd": "path", "rel": "frames/f"})["path"]))
        snap = sh(["taskset", "-c", os.environ.get("CU_CPUSET", "10-14"), "timeout", "60", os.path.join(work, "bin", "srf-cu-snap"), "--fake-fb",
                   "--frames", "8", "--ramws-dir", ramdir, "--out", os.path.join(work, "snap")], test_env=True)  # ADW1-05: --fake-fb needs OS_CU_TEST=1
        if snap.returncode:
            problems.append({"code": "E_SMOKE_SNAP", "rc": snap.returncode, "detail": snap.stderr[-300:]})
        steps = os.path.join(work, "steps.jsonl")
        base = [os.path.join(work, "bin", "srf-cu-exec")]
        plan, frames = os.path.join(PLANS, "click_effect.plan.json"), os.path.join(PLANS, "effect.frames.jsonl")
        a = sh(["timeout", "30"] + base + ["run", "--plan", plan, "--frames", frames, "--out", steps, "--mock"], test_env=True)
        b = sh(["timeout", "30"] + base + ["replay", "--plan", plan, "--steps", steps, "--frames", frames,
                                           "--out", os.path.join(work, "replay.jsonl")])
        if a.returncode or b.returncode:
            problems.append({"code": "E_SMOKE_EXEC", "rc": [a.returncode, b.returncode]})
        rpc({"cmd": "account"})
        rec = rpc({"cmd": "receipt"}).get("receipt", {})
        peak, quota = rec.get("ram_peak_bytes", 0), rec.get("quota", 0)
        if not quota or peak > quota:
            problems.append({"code": "E_RAM_PEAK", "peak": peak, "quota": quota})
        rpc({"cmd": "close"})
    finally:
        try:
            sess.stdin.close()
            sess.wait(timeout=20)
        except Exception:
            sess.kill()
    if plant:
        with open(os.path.join(work, plant), "wb") as f:
            f.write(b"planted")
    if plant_ramws:
        os.makedirs(os.path.join(RAMROOT, "os-cu-0", "leftover-%d" % os.getpid()), exist_ok=True)
    bad, foreign_build = check_build(build, stamp)
    if bad:
        problems.append({"code": "E_DISK_FRAMES", "offending": bad})
    tracked = ramws_shim.check_disk(os.path.join(REPO, "testdata", "cu"))  # AW1-04: no frame file in tracked fixtures
    if tracked:
        problems.append({"code": "E_DISK_FRAMES_TESTDATA", "offending": tracked})
    left, foreign = leftovers(prefixes)
    if left:
        problems.append({"code": "E_RAMWS_LEFTOVER", "offending": left})
    shutil.rmtree(os.path.join(work, "bin"), ignore_errors=True)  # ADC2-W3-05: binaries are not evidence and bin/ is no longer exempt
    sz = g_disk_size.check(work)
    if sz:
        problems.append({"code": "E_DISK_SIZE", "offending": sz})
    if problems:
        return 5, {"code": "E_GATE", "problems": problems, "ignored_foreign": foreign, "ignored_foreign_build": foreign_build}, None
    return 0, {"code": "OK", "ram_peak_bytes": peak, "quota": quota, "ignored_foreign": foreign, "ignored_foreign_build": foreign_build}, work


def export_smoke(work, dst):
    """Copy the smoke evidence (everything but bin/) into dst so the g_disk_size gate has a real, non-empty episode set to check."""
    os.makedirs(dst, exist_ok=True)
    for n in sorted(os.listdir(work)):
        if n == "bin":
            continue
        s, d = os.path.join(work, n), os.path.join(dst, n)
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.copytree(s, d, symlinks=True, dirs_exist_ok=True)
        elif os.path.isfile(s) and not os.path.islink(s):
            shutil.copy2(s, d)


def main(argv):
    try:
        o = parse_args(argv)
    except ValueError:
        sys.stderr.write("usage: g_disk_frames.py [--plant NAME] [--plant-ramws] [--smoke-out DIR] [--run-prefix PFX]\n")
        return 2
    plant, plant_ramws, smoke_out = o["plant"], o["plant_ramws"], o["smoke_out"]
    try:
        code, rep, cleanup = gate(plant, plant_ramws, o["run_prefix"])
    except Exception as e:  # fail closed, never rc 1
        code, rep, cleanup = 5, {"code": "E_GATE", "detail": repr(e)}, None
    out(rep)
    if plant_ramws:  # remove only what this process planted
        shutil.rmtree(os.path.join(RAMROOT, "os-cu-0", "leftover-%d" % os.getpid()), ignore_errors=True)
        try:
            os.rmdir(os.path.join(RAMROOT, "os-cu-0"))
        except OSError:
            pass
    work = os.path.join(REPO, "build", "gate_disk", str(os.getpid()))
    shutil.rmtree(os.path.join(work, "bin"), ignore_errors=True)  # binaries are not evidence
    if smoke_out and code == 0 and cleanup and not plant and not plant_ramws:
        export_smoke(work, smoke_out)
    if (code == 0 and cleanup) or plant or plant_ramws:
        shutil.rmtree(work, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
