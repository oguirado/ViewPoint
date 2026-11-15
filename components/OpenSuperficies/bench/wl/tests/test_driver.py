import hashlib, json, os, re, shutil, stat, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
sys.path.insert(0, HERE)
import driver as D
import recorder as R
from test_recorder import RamDir, MAN

FAKE = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
log = os.environ["FAKE_WLCAP_LOG"]
open(log, "a").write(json.dumps(a) + "\\n")
out = a[a.index("--out") + 1]
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "receipt.json"), "w").write("{}")
if os.environ.get("FAKE_WLCAP_FRAME"):
    open(os.path.join(out, "frames.rgb24"), "wb").write(b"x")
run = a[a.index("--ramws-dir") + 1]
if os.environ.get("FAKE_WLCAP_START"):  # CF-01: START after a delay, so an actuator launched early is caught
    import time
    time.sleep(float(os.environ["FAKE_WLCAP_START"]))
    open(os.path.join(run, "START.tmp"), "w").write(json.dumps({"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": 123456789}))
    os.rename(os.path.join(run, "START.tmp"), os.path.join(run, "START"))
for k in range(int(os.environ.get("FAKE_WLCAP_HOST", "0"))):  # CF-02 capsule side of the handshake
    import time
    d = os.path.join(run, "chunk_%d" % k)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "frames.rgb24"), "wb").write(b"\\0" * 12)
    ready = json.loads(os.environ.get("FAKE_READY", "{}")) or {"schema": "os.cu.chunk-ready/v1", "chunk": k, "first_render": 2 * k,
                                                          "frames": 2, "w": 1920, "h": 1080, "grid": 32, "det_sha256": []}
    open(os.path.join(d, "READY.tmp"), "w").write(json.dumps(ready))
    os.rename(os.path.join(d, "READY.tmp"), os.path.join(d, "READY"))
    end, got = time.monotonic() + float(os.environ.get("FAKE_WAIT", "10")), "timeout"
    while time.monotonic() < end and got == "timeout":
        for mk in ("ANALYZED", "ANALYZE_FAILED"):
            if os.path.exists(os.path.join(d, mk)):
                got = mk
        time.sleep(0.02)
    open(log, "a").write(json.dumps({"chunk": k, "marker": got}) + "\\n")
sys.exit(int(os.environ.get("FAKE_WLCAP_RC", "0")))
"""


FAKE_AN = """import json, os, sys, time
a = sys.argv[1:]
d = a[a.index("--chunk-dir") + 1]
open(os.environ["FAKE_WLCAP_LOG"], "a").write(json.dumps({"analyze": os.path.basename(d), "argv": a}) + "\\n")
mode = os.environ.get("FAKE_AN_MODE", "ok")
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    print(json.dumps({"code": "E_GATE", "detail": "x"}))
    sys.exit(5)
k = int(os.path.basename(d).split("_")[1])
print(json.dumps({"chunk": k, "frames": int(a[a.index("--frames") + 1]), "grid": int(a[a.index("--grid") + 1]),
                  "class_counts": {"UNCHANGED": 7}, "analyze_us_per_frame": 3, "engine_wheel_sha8": "5e2395d5"}))
"""

FAKE_ACT = """import json, os, sys
a = sys.argv[1:]
start = a[a.index("--start") + 1]
open(os.environ["FAKE_WLCAP_LOG"], "a").write(json.dumps({"act": a, "start_seen": os.path.exists(start)}) + "\\n")
mode = os.environ.get("FAKE_ACT_MODE", "ok")
plan = json.loads(os.environ["FAKE_ACT_PLAN"])
rows = [{"schema": "os.cu.wl-act-step/v1", "item": a[a.index("--item") + 1], "actuator_seq": i + 1, "step": i + 1, "point": 0, "op": "scroll",
         "t_plan_us": plan[i], "t_us": plan[i] + 777, "rc": 0} for i in range(3 if mode != "short" else 2)]
with open(a[a.index("--out") + 1], "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\\n")
sys.exit(5 if mode == "fail" else 0)
"""


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drvtest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = os.path.join(self.tmp, "fake_wlcap.py")
        open(self.fake, "w").write(FAKE)
        os.chmod(self.fake, 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        os.environ["FAKE_WLCAP_LOG"] = self.log
        for k in ("FAKE_WLCAP_RC", "FAKE_WLCAP_FRAME", "FAKE_WLCAP_START", "FAKE_WLCAP_HOST", "FAKE_READY", "FAKE_WAIT",
                  "FAKE_AN_MODE", "FAKE_ACT_MODE", "FAKE_ACT_PLAN"):
            os.environ.pop(k, None)
        self.out = os.path.join(self.tmp, "out")

    def args(self, *extra):
        return ["--out", self.out, "--wlcap", sys.executable, self.fake, "--fake-fb"] + list(extra)

    def calls(self):
        return [c for c in (json.loads(l) for l in open(self.log)) if isinstance(c, list)]

    def markers(self):
        return [c for c in (json.loads(l) for l in open(self.log)) if isinstance(c, dict)]

    def fake_tool(self, name, src):
        p = os.path.join(self.tmp, name)
        open(p, "w").write(src)
        return p

    def test_driver_wl6_first(self):
        with RamDir() as d6, RamDir() as d1:
            rc = D.main(self.args("--item", "WL-1", "--item", "WL-6", "--run", "WL-6=" + d6, "--run", "WL-1=" + d1))
        self.assertEqual(rc, 0)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("--ramws-dir") + 1], d6)
        ho = json.load(open(os.path.join(self.out, "wl_handover.json")))
        self.assertEqual(ho["items"], ["WL-6", "WL-1"])

    def test_driver_labels_expected_kind_blink_not_change(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        rows = [json.loads(l) for l in open(os.path.join(self.out, "labels", "WL-6.jsonl"))]
        self.assertTrue(rows)
        self.assertTrue(all(r["expected_kind"] == "NOT_CHANGED" for r in rows))  # enum value, AMEND C2
        args = self.calls()[0]
        self.assertIn("wl", args)
        self.assertEqual(args[args.index("--det") + 1], "on")
        self.assertEqual(args[args.index("--min-segment-ms") + 1], "5000")

    def test_driver_prereg_sha_mismatch_rc5(self):
        with RamDir() as d:
            rc = D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--expect-prereg-sha256", "0" * 64))
        self.assertEqual(rc, 5)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(D.PINNED_PREREG_SHA256, "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58")
        self.assertEqual(R.prereg_sha256(R.load_manifest(MAN)), D.PINNED_PREREG_SHA256)

    def test_driver_handover_lists_run_ids_labels(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        ho = json.load(open(os.path.join(self.out, "wl_handover.json")))
        self.assertEqual(set(ho), {"schema", "run_ids", "label_files", "prereg_sha256", "items", "wm_profile"})
        self.assertEqual(ho["run_ids"], [os.path.basename(d)])
        self.assertEqual(ho["label_files"], ["labels/WL-6.jsonl"])
        self.assertEqual(ho["prereg_sha256"], D.PINNED_PREREG_SHA256)

    def test_driver_no_frame_on_disk(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
            for dp, _, fn in os.walk(self.out):
                for f in fn:
                    self.assertFalse(f.endswith((".rgb24", ".raw", ".xwd")), f)
            os.environ["FAKE_WLCAP_FRAME"] = "1"
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 5)
        self.assertEqual(D.main(["--out", "/dev/shm/drv-out", "--item", "WL-6", "--ramws-dir", "/x", "--fake-fb"]), 2)

    def test_driver_out_refused_on_any_ram_mount_by_fstype(self):
        mi = os.path.join(self.tmp, "mountinfo")
        open(mi, "w").write(
            "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "30 20 0:25 / /run/lock rw - tmpfs tmpfs rw\n"
            "31 20 0:26 / /srv/ram\\040disk rw - ramfs ramfs rw\n"
            "33 32 0:27 / /data/scratch rw - tmpfs tmpfs rw\n"  # listed before its parent mount: longest prefix must still win
            "32 20 8:2 / /data rw - ext4 /dev/sda2 rw\n")
        for bad in ("/run/lock/x", "/srv/ram disk/o", "/data/scratch/o/p"):
            with self.assertRaises(D.DriverError, msg=bad) as cm:
                D._out_ok(bad, mi)
            self.assertEqual(cm.exception.rc, 2)
        for good in ("/data/out", "/home/u/out", "/run/lockx/o"):
            D._out_ok(good, mi)  # disk (longest mount point wins; /run/lockx is not under /run/lock)
        with self.assertRaises(D.DriverError):  # unreadable mount table fails closed
            D._out_ok("/data/out", os.path.join(self.tmp, "absent"))
        if os.path.isdir("/dev/shm"):
            with self.assertRaises(D.DriverError):
                D._out_ok("/dev/shm/real-check")

    def test_driver_refuses_non_ramws_dir(self):
        rc = D.main(self.args("--item", "WL-6", "--ramws-dir", tempfile.mkdtemp(dir=self.tmp)))
        self.assertEqual(rc, 5)
        self.assertFalse(os.path.exists(self.log))

    def test_driver_qtile_refused_without_image_attestation(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--wm-profile", "qtile")), 5)
            self.assertFalse(os.path.exists(self.log))
            att = os.path.join(self.tmp, "attest")
            with open(att, "w") as f:
                json.dump({"schema": "os.cu.qtile-image-attestation/v1", "image": "opensuperficies-desktop-arm64:1.0.0",
                           "digest": "sha256:" + "ab" * 32, "qtile_version": "0.33.0", "approval_id": "OPR-0001"}, f)
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--wm-profile", "qtile",
                                              "--image-has-qtile", att)), 0)
        rows = [json.loads(l) for l in open(os.path.join(self.out, "labels", "WL-6.jsonl"))]
        self.assertEqual(rows[0]["rect"], [0, 0, 960, 1080])

    def test_driver_4k_chunk_and_retention(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--res", "3840x2160")), 5)
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--res", "3840x2160", "--chunk", "30")), 0)
        args = self.calls()[0]
        self.assertNotIn("--keep", args)
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        args = self.calls()[-1]
        self.assertIn("--keep", args)
        self.assertEqual(args[args.index("--quota-bytes") + 1], str(2400 << 20))

    def test_driver_keep_passes_ttl_7200(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        args = self.calls()[-1]
        self.assertIn("--keep", args)
        self.assertEqual(args[args.index("--ttl-seconds") + 1], "7200")

    def test_driver_accepts_readonly_lock_and_rejects_shared_holder(self):
        import fcntl
        with RamDir() as d:
            os.chmod(os.path.join(d, ".lock"), 0o400)  # law4 share: read-only .lock
            self.assertEqual(R._ramws_dir(d), os.path.realpath(d))
            # the probe never holds the lock: a host LOCK_EX attempt right after must still conflict only with RamDir's holder
            fd = os.open(os.path.join(d, ".lock"), os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        os.makedirs(R.RAMWS_ROOT, exist_ok=True)
        d = tempfile.mkdtemp(prefix="wltest-sh-", dir=R.RAMWS_ROOT)
        try:
            open(os.path.join(d, "owner.json"), "w").write("{}")
            fd = os.open(os.path.join(d, ".lock"), os.O_RDONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_SH)  # a dead session with a mere reader: not live
            try:
                with self.assertRaises(R.WLError) as c:
                    R._ramws_dir(d)
                self.assertEqual(c.exception.rc, 5)
            finally:
                os.close(fd)
            # probe released at once: host EX succeeds right after a rejected probe
            fd = os.open(os.path.join(d, ".lock"), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_driver_wlcap_failure_propagates_never_1(self):
        os.environ["FAKE_WLCAP_RC"] = "1"
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 5)

    def test_driver_v2_item_with_actions_needs_exec_rc2(self):  # INT3: labels of a v2 item come from the actuator AJSON, never from the plan
        v2 = os.path.join(WL, "prereg_wl_v2.json")
        with RamDir() as d:
            for it in ("WL-1", "WL-2", "WL-2b", "WL-7"):
                self.assertEqual(D.main(self.args("--prereg", v2, "--item", it, "--ramws-dir", d)), 2, it)
            self.assertFalse(os.path.exists(self.log))
            self.assertFalse(os.path.exists(os.path.join(self.out, "labels")))

    def test_driver_v2_order_flags_and_labels(self):
        import labels_v2 as L2
        v2 = os.path.join(WL, "prereg_wl_v2.json")
        m = L2.load_manifest(v2)
        self.assertEqual(L2.prereg_sha256(m), D.PINNED_PREREG_V2_SHA256)
        self.assertLess(D.recorder.capture_order(m).index("WL-2b"), D.recorder.capture_order(m).index("WL-1"))  # prereg v2 order: WL-2b before WL-1
        os.environ["FAKE_WLCAP_START"] = "0.1"
        with RamDir() as d:
            rc = D.main(self.act_args(d, "--require-continuous", "on", "--analyze", "engine"))
        self.assertEqual(rc, 0)
        c = self.calls()[-1]
        self.assertIn("--require-continuous", c)
        self.assertEqual(c[c.index("--analyze") + 1], "engine")
        ho = json.load(open(os.path.join(self.out, "wl_handover.json")))
        self.assertEqual((ho["items"], ho["prereg_sha256"], ho["label_timebase"]), (["WL-1"], D.PINNED_PREREG_V2_SHA256, "actuator"))
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--prereg", v2, "--item", "WL-1", "--ramws-dir", d, "--expect-prereg-sha256", "0" * 64)), 5)
            self.assertEqual(D.main(self.args("--prereg", v2, "--item", "WL-1", "--ramws-dir", d, "--require-continuous", "maybe")), 2)

    def test_driver_kept_quota_2400mib_refuse_over_and_plan_total(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--quota-bytes", str((2400 << 20) + 1))), 2)
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--quota-bytes", str(2400 << 20))), 0)
        self.assertEqual(D.Q1080, 2400 << 20)
        self.assertLessEqual(4 * D.Q1080, 10 << 30)
        # v1 has 6 kept items: 6 x 2400 MiB > 10 GiB, the plan is refused before any capture
        n = len(self.calls())
        self.assertEqual(D.main(self.args("--run", "WL-6=/x")), 2)
        self.assertEqual(len(self.calls()), n)

    def test_driver_quota_nonpositive_refused_rc2_and_plan_uses_effective_quota(self):  # ADC2-W3-03
        with RamDir() as d:
            for bad in ("-1", "0", "-%d" % (11 << 30)):
                self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--quota-bytes", bad)), 2, bad)
            self.assertFalse(os.path.exists(self.log))
        # 6 kept v1 items: with -1 the old truthy check fell back to nothing; every run must be refused before capture
        runs = []
        for it in ("WL-1", "WL-2", "WL-3", "WL-4", "WL-5", "WL-6"):
            runs += ["--run", it + "=/x"]
        self.assertEqual(D.main(self.args("--quota-bytes", "-1", *runs)), 2)
        self.assertFalse(os.path.exists(self.log))
        # the plan total uses the quota passed to wlcap: 6 x 1 MiB fits, so it is not refused for the plan (refused later: /x)
        self.assertEqual(D._plan_bytes(6, 1 << 20), 6 << 20)
        self.assertEqual(D._plan_bytes(6, None), 6 * D.Q1080)
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--quota-bytes", str(1 << 20))), 0)
        c = self.calls()[-1]
        self.assertEqual(c[c.index("--quota-bytes") + 1], str(1 << 20))
        with RamDir() as d:  # handover-1s kept: the effective quota is passed explicitly, never left to the wlcap default
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d, "--mode", "handover-1s")), 0)
        c = self.calls()[-1]
        self.assertEqual(c[c.index("--quota-bytes") + 1], str(D.Q1080))

    def host_args(self, d, *extra):
        an = self.fake_tool("fake_an.py", FAKE_AN)
        return self.args("--item", "WL-6", "--ramws-dir", d, "--analyze", "host",
                         "--analyze-script", an, "--analyze-timeout", "2", *extra)

    def test_host_analysis_ready_then_analyzed_with_row_sha(self):  # CF-02 host side
        os.environ["FAKE_WLCAP_HOST"] = "2"
        with RamDir() as d:
            self.assertEqual(D.main(self.host_args(d)), 0)
            self.assertEqual([m["marker"] for m in self.markers() if "marker" in m], ["ANALYZED", "ANALYZED"])
            lines = open(os.path.join(self.out, "WL-6", "chunk_rows.jsonl"), "rb").read().splitlines(True)
            for k in (0, 1):
                raw = open(os.path.join(d, "chunk_%d" % k, "ANALYZED"), "rb").read()
                self.assertEqual(raw, lines[k])  # P22: ANALYZED is exactly one chunk_analyze.py row
                self.assertEqual(json.loads(raw)["chunk"], k)
                self.assertEqual(json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode() + b"\n", raw)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "chunk_%d" % k, "ANALYZED")).st_mode), 0o640)
                self.assertFalse(os.path.exists(os.path.join(d, "chunk_%d" % k, "ANALYZED.tmp")))
        c = self.calls()[0]
        self.assertEqual(c[c.index("--analyze") + 1], "host")
        self.assertEqual(c[c.index("--analyze-wait-ms") + 1], "12000")  # host timeout 2 s + 10 s: wlcap outlasts the host
        ho = json.load(open(os.path.join(self.out, "wl_handover.json")))
        self.assertEqual(ho["chunk_rows"]["WL-6"], {"file": "WL-6/chunk_rows.jsonl", "sha256": hashlib.sha256(b"".join(lines)).hexdigest()})
        an = [m for m in self.markers() if "analyze" in m]
        self.assertEqual([m["analyze"] for m in an], ["chunk_0", "chunk_1"])
        argv = an[0]["argv"]
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--w", "--h", "--frames", "--grid")], ["1920", "1080", "2", "32"])
        rows = [json.loads(l) for l in open(os.path.join(self.out, "WL-6", "chunk_rows.jsonl"))]
        self.assertEqual([r["chunk"] for r in rows], [0, 1])

    def test_host_analysis_failure_and_timeout_write_analyze_failed_bounded(self):
        os.environ["FAKE_WLCAP_HOST"] = "1"
        os.environ["FAKE_AN_MODE"] = "fail"
        with RamDir() as d:
            self.assertEqual(D.main(self.host_args(d)), 0)
            mk = json.load(open(os.path.join(d, "chunk_0", "ANALYZE_FAILED")))
            self.assertEqual(mk, {"chunk": 0, "rc": 5, "reason": "E_GATE"})
            self.assertFalse(os.path.exists(os.path.join(d, "chunk_0", "ANALYZED")))
        self.assertEqual(self.markers()[-1]["marker"], "ANALYZE_FAILED")
        os.environ["FAKE_AN_MODE"] = "slow"
        with RamDir() as d:
            t = time.monotonic()
            self.assertEqual(D.main(self.host_args(d)), 0)
            self.assertLess(time.monotonic() - t, 10)  # a slow analysis is killed at --analyze-timeout, never a stall
            mk = json.load(open(os.path.join(d, "chunk_0", "ANALYZE_FAILED")))
            self.assertEqual((mk["reason"], mk["rc"]), ("E_ANALYZE_TIMEOUT", 5))
        self.assertEqual(self.markers()[-1]["marker"], "ANALYZE_FAILED")

    def test_host_analysis_only_on_ready_and_ready_validated(self):
        with RamDir() as d:
            os.makedirs(os.path.join(d, "chunk_5"))
            open(os.path.join(d, "chunk_5", "frames.rgb24"), "wb").write(b"\0")  # no READY: never touched
            os.environ["FAKE_WLCAP_HOST"] = "1"
            os.environ["FAKE_READY"] = json.dumps({"schema": "os.cu.chunk-ready/v1", "chunk": 3, "frames": 2, "w": 1920, "h": 1080, "grid": 32})
            self.assertEqual(D.main(self.host_args(d)), 0)
            self.assertEqual(os.listdir(os.path.join(d, "chunk_5")), ["frames.rgb24"])
            mk = json.load(open(os.path.join(d, "chunk_0", "ANALYZE_FAILED")))
            self.assertEqual((mk["reason"], mk["rc"]), ("E_READY", 2))
            self.assertEqual([m for m in self.markers() if "analyze" in m], [])
        ok = {"schema": "os.cu.chunk-ready/v1", "chunk": 0, "frames": 2, "w": 1920, "h": 1080, "grid": 32}
        for bad in ({"frames": 0}, {"frames": "2"}, {"w": 1280, "h": 720}, {"grid": 16}, {"schema": "x"}, {"chunk": True}):
            with self.assertRaises(D.DriverError, msg=bad):
                D._ready_args(dict(ok, **bad), 0, 1920, 1080)
        self.assertEqual(D._ready_args(ok, 0, 1920, 1080), (1920, 1080, 2, 32))
        self.assertEqual(D._ready_args(dict(ok, w=3840, h=2160, grid=64), 0, 3840, 2160), (3840, 2160, 2, 64))
        self.assertEqual((D._grid_for(1920, 1080), D._grid_for(3840, 2160), D._grid_for(64, 48)), (32, 64, 16))

    def test_no_host_analysis_unless_requested(self):
        os.environ["FAKE_WLCAP_HOST"] = "1"
        os.environ["FAKE_WAIT"] = "0.3"
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
            self.assertFalse(os.path.exists(os.path.join(d, "chunk_0", "ANALYZED")))
        self.assertEqual(self.markers()[-1]["marker"], "timeout")

    def act_args(self, d, *extra):
        v2 = os.path.join(WL, "prereg_wl_v2.json")
        act = self.fake_tool("fake_act.py", FAKE_ACT)
        m = json.load(open(v2))
        os.environ["FAKE_ACT_PLAN"] = json.dumps([s["t_ms"] * 1000 for s in next(i for i in m["items"] if i["id"] == "WL-1")["steps"]])
        return self.args("--prereg", v2, "--item", "WL-1", "--ramws-dir", d, "--exec", sys.executable, act, *extra)

    def test_actuator_launched_after_start_and_labels_from_ajson(self):  # CF-01 orchestration
        import labels_v2 as L2
        os.environ["FAKE_WLCAP_START"] = "0.4"
        with RamDir() as d:
            self.assertEqual(D.main(self.act_args(d)), 0)
            acts = [m for m in self.markers() if "act" in m]
            self.assertEqual(len(acts), 1)
            self.assertTrue(acts[0]["start_seen"])  # never launched before START exists
            a = acts[0]["act"]
            self.assertEqual(a[0], "run")
            self.assertEqual(a[a.index("--start") + 1], os.path.join(os.path.realpath(d), "START"))
            self.assertEqual(a[a.index("--item") + 1], "WL-1")
        act_rows = [json.loads(l) for l in open(os.path.join(self.out, "WL-1", "act.jsonl"))]
        rows = [json.loads(l) for l in open(os.path.join(self.out, "labels", "WL-1.jsonl"))]
        m = L2.load_manifest(os.path.join(WL, "prereg_wl_v2.json"))
        self.assertEqual(rows, L2.labels_from_act(m, "WL-1", 1920, 1080, act_rows))
        self.assertEqual(sorted({r["t_us"] for r in rows}), [1000777, 3000777, 5000777])  # actuator t_us, not the plan
        ho = json.load(open(os.path.join(self.out, "wl_handover.json")))
        self.assertEqual(ho["label_timebase"], "actuator")

    def test_actuator_not_launched_without_start_and_failures_rc5(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.act_args(d)), 5)  # wlcap never wrote START
        self.assertEqual([m for m in self.markers() if "act" in m], [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "wl_handover.json")))
        os.environ["FAKE_WLCAP_START"] = "0"
        for mode in ("fail", "short"):
            os.environ["FAKE_ACT_MODE"] = mode
            with RamDir() as d:
                self.assertEqual(D.main(self.act_args(d)), 5, mode)
            self.assertFalse(os.path.exists(os.path.join(self.out, "labels", "WL-1.jsonl")), mode)

    def test_driver_duplicate_run_dir_refused(self):
        v2 = os.path.join(WL, "prereg_wl_v2.json")
        with RamDir() as d:
            rc = D.main(self.args("--prereg", v2, "--item", "WL-2b", "--item", "WL-2", "--run", "WL-2b=" + d, "--run", "WL-2=" + d, "--exec", sys.executable, "x"))  # --exec: the refusal under test is the duplicate dir
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self.log))

    def test_driver_res_must_be_in_manifest(self):
        v2 = os.path.join(WL, "prereg_wl_v2.json")
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--prereg", v2, "--item", "WL-1", "--ramws-dir", d, "--res", "3840x2160", "--chunk", "30", "--exec", sys.executable, "x")), 2)  # --exec: the refusal under test is the resolution
            self.assertFalse(os.path.exists(self.log))
            os.environ["FAKE_WLCAP_START"] = "0.1"
            self.assertEqual(D.main(self.act_args(d)), 0)

    def test_driver_files_0640_dirs_0750_umask_027(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        self.assertEqual(os.umask(0o027), 0o027)
        for p, want in ((self.out, 0o750), (os.path.join(self.out, "labels"), 0o750), (os.path.join(self.out, "WL-6"), 0o750),
                        (os.path.join(self.out, "labels", "WL-6.jsonl"), 0o640), (os.path.join(self.out, "wl_handover.json"), 0o640)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)

    def test_driver_default_has_no_new_flags(self):
        with RamDir() as d:
            self.assertEqual(D.main(self.args("--item", "WL-6", "--ramws-dir", d)), 0)
        self.assertNotIn("--analyze", self.calls()[0])
        self.assertNotIn("--require-continuous", self.calls()[0])

    def test_driver_no_rc1(self):
        src = open(os.path.join(WL, "driver.py")).read()
        self.assertIsNone(re.search(r"sys\.exit\(\s*1\s*\)|return\s+1\b", src))
        self.assertEqual(D.main([]), 2)
        self.assertEqual(D.main(["--bogus"]), 2)
        self.assertEqual(D.main(self.args("--res", "1x1", "--ramws-dir", "/x", "--item", "WL-6")), 2)


if __name__ == "__main__":
    unittest.main()
