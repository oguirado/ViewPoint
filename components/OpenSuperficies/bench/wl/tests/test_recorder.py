import fcntl, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import recorder as R

MAN = os.path.join(WL, "prereg_wl.json")
ROOT = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"


class RamDir:
    def __enter__(self):
        os.makedirs(ROOT, exist_ok=True)
        self.d = tempfile.mkdtemp(prefix="wltest-", dir=ROOT)
        with open(os.path.join(self.d, "owner.json"), "w") as f:
            json.dump({"pid": os.getpid(), "tool": "opensuperficies"}, f)
        self.fd = os.open(os.path.join(self.d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self.d

    def __exit__(self, *a):
        os.close(self.fd)
        shutil.rmtree(self.d, ignore_errors=True)


def frame(w, h, v):
    return bytes([v & 255]) * (w * h * 3)


class T(unittest.TestCase):
    def setUp(self):
        self.m = R.load_manifest(MAN)

    def test_prereg_pins_scene_sha(self):
        for it in self.m["items"]:
            p = os.path.join(WL, it["scene_file"])
            self.assertEqual(hashlib.sha256(open(p, "rb").read()).hexdigest(), it["scene_sha256"])
        self.assertEqual(len(R.prereg_sha256(self.m)), 64)
        self.assertEqual(R.prereg_sha256(self.m), R.prereg_sha256(json.loads(json.dumps(self.m))))
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(WL, os.path.join(d, "wl"), ignore=shutil.ignore_patterns("__pycache__"))
            with open(os.path.join(d, "wl", "scenes", "wl6_idle.json"), "a") as f:
                f.write(" ")
            with self.assertRaises(R.WLError) as c:
                R.load_manifest(os.path.join(d, "wl", "prereg_wl.json"))
            self.assertEqual(c.exception.rc, 5)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_schema_closed_and_wm_profile_enum(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(WL, os.path.join(d, "wl"), ignore=shutil.ignore_patterns("__pycache__"))
            p = os.path.join(d, "wl", "prereg_wl.json")
            m = json.load(open(p))
            m["items"][0]["wm_profiles"] = ["default", "i3"]
            json.dump(m, open(p, "w"))
            with self.assertRaises(R.WLError) as c:
                R.load_manifest(p)
            self.assertEqual(c.exception.rc, 2)
            m["items"][0]["wm_profiles"] = ["default", "qtile"]
            m["extra"] = 1
            json.dump(m, open(p, "w"))
            with self.assertRaises(R.WLError) as c:
                R.load_manifest(p)
            self.assertEqual(c.exception.rc, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_plans_are_action_v1_integer_only(self):
        for it in self.m["items"]:
            for (w, h) in ((1920, 1080), (3840, 2160)):
                plan = R.plan_for(self.m, it["id"], w, h)
                self.assertTrue(plan)
                for i, a in enumerate(plan):
                    self.assertEqual(a["schema"], "vqa.action/v1")
                    self.assertEqual(a["step"], i + 1)
                    self.assertNotIn("text", a["args"])
        t = [a for a in R.plan_for(self.m, "WL-2", 1920, 1080) if a["op"] == "type"][0]
        self.assertEqual(t["args"]["len"], 22)
        self.assertEqual(len(t["args"]["text_sha256"]), 64)

    def test_labels_expected_kind_per_op(self):
        def kinds(i):
            return {l["op"]: l["expected_kind"] for l in R.labels_for(self.m, i, 1920, 1080)}
        self.assertEqual(kinds("WL-1")["scroll"], "MOVED")
        self.assertEqual(kinds("WL-2")["type"], "CHANGED")
        self.assertEqual(kinds("WL-3")["wait"], "CHANGED_REGION")
        self.assertEqual(kinds("WL-6")["wait"], "NOT_CHANGED")
        sc = R.labels_for(self.m, "WL-1", 1920, 1080)
        self.assertEqual([l["dy"] for l in sc], [-120, -480, -1440])
        for l in sc:
            self.assertEqual(set(l), {"actuator_seq", "t_us", "op", "rect", "expected_kind", "dy"})
            self.assertEqual(len(l["rect"]), 4)

    def test_qtile_labels_from_wm_layout(self):
        with self.assertRaises(R.WLError) as c:
            R.labels_for(self.m, "WL-4", 1920, 1080, "qtile", None)
        self.assertEqual(c.exception.rc, 2)
        lay = {"rect": [0, 0, 960, 1080]}
        ls = R.labels_for(self.m, "WL-4", 1920, 1080, "qtile", lay)
        self.assertEqual(ls[0]["rect"], [0, 0, 960, 1080])
        d = R.labels_for(self.m, "WL-4", 1920, 1080, "default", None)
        self.assertNotEqual(d[0]["rect"], [0, 0, 960, 1080])

    def test_gap_recorded_on_skip(self):
        with RamDir() as d:
            r = R.Recorder(d, 4, 2)
            for i, idx in enumerate((0, 1, 2, 5, 6)):
                r.push(idx, i * 16667, frame(4, 2, i))
            rec = r.finish()
            self.assertEqual(rec["frames"], 5)
            self.assertEqual(rec["gaps"], [{"after": 2, "missing": 2}])
            self.assertEqual(os.path.getsize(os.path.join(d, "frames.rgb24")), 5 * 4 * 2 * 3)
            self.assertEqual(len(rec["det_sha256"]), 5)

    def test_frames_only_in_ramws(self):
        with self.assertRaises(R.WLError) as c:
            R.Recorder(tempfile.gettempdir(), 4, 2)
        self.assertEqual(c.exception.rc, 5)
        with self.assertRaises(R.WLError):
            R.Recorder(ROOT + "/../../x", 4, 2)
        with RamDir() as d:
            r = R.Recorder(d, 4, 2)
            r.push(0, 0, frame(4, 2, 1))
            r.finish()
            self.assertIn("frames.rgb24", os.listdir(d))
            with self.assertRaises(R.WLError):
                R.Recorder(d, 4, 2).push(0, 0, b"short")

    def test_60fps_segment_length(self):
        with RamDir() as d:
            r = R.Recorder(d, 2, 2, det=False)
            for i in range(300):
                r.push(i, i * 16667, frame(2, 2, i))
            rec = r.finish()
            self.assertGreaterEqual(rec["segment_ms"], 5000)
            self.assertNotIn("det_sha256", rec)
        with RamDir() as d:
            r = R.Recorder(d, 2, 2)
            for i in range(299):
                r.push(i, i * 16667, frame(2, 2, i))
            self.assertLess(r.finish()["segment_ms"], 5000)
        with RamDir() as d:
            r = R.Recorder(d, 2, 2)
            for i in range(400):
                r.push(i + (i >= 150), i * 16667, frame(2, 2, i))
            self.assertLess(r.finish()["segment_ms"], 5000)

    def test_wl6_first_in_order(self):
        o = R.capture_order(self.m)
        self.assertEqual(o[0], "WL-6")
        self.assertEqual(sorted(o), sorted(i["id"] for i in self.m["items"]))

    def _copy_with(self, mutate):
        d = tempfile.mkdtemp()
        shutil.copytree(WL, os.path.join(d, "wl"), ignore=shutil.ignore_patterns("__pycache__"))
        p = os.path.join(d, "wl", "prereg_wl.json")
        m = json.load(open(p))
        mutate(m)
        json.dump(m, open(p, "w"))
        return d, p

    def _refuses(self, mutate, rc=2):
        d, p = self._copy_with(mutate)
        try:
            with self.assertRaises(R.WLError) as c:
                R.load_manifest(p)
            self.assertEqual(c.exception.rc, rc)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_validate_refuses_wl6_not_first(self):
        def rot(m):
            o = m["capture_order"]
            self.assertEqual(o[0], "WL-6")
            m["capture_order"] = o[1:2] + o[:1] + o[2:]
        self._refuses(rot)
        self._refuses(lambda m: m.__setitem__("capture_order", m["capture_order"][1:] + m["capture_order"][:1]))

    def test_push_refuses_equal_render_index(self):
        with RamDir() as d:
            r = R.Recorder(d, 2, 2)
            r.push(3, 0, frame(2, 2, 1))
            with self.assertRaises(R.WLError) as c:
                r.push(3, 16667, frame(2, 2, 2))
            self.assertEqual(c.exception.rc, 2)
            with self.assertRaises(R.WLError):
                r.push(2, 33334, frame(2, 2, 3))

    def test_recorder_refuses_unmanaged_ramws_dirs(self):
        os.makedirs(ROOT, exist_ok=True)
        # no owner.json, no lock
        d = tempfile.mkdtemp(prefix="wltest-un-", dir=ROOT)
        try:
            with self.assertRaises(R.WLError) as c:
                R.Recorder(d, 4, 2)
            self.assertEqual(c.exception.rc, 5)
            self.assertFalse(os.path.exists(os.path.join(d, "frames.rgb24")))
            # owner.json present but nobody holds the lock
            open(os.path.join(d, "owner.json"), "w").write("{}")
            open(os.path.join(d, ".lock"), "w").close()
            with self.assertRaises(R.WLError) as c:
                R.Recorder(d, 4, 2)
            self.assertEqual(c.exception.rc, 5)
            self.assertFalse(os.path.exists(os.path.join(d, "frames.rgb24")))
        finally:
            shutil.rmtree(d, ignore_errors=True)
        # nested dir inside a managed one is not a direct child of the root
        with RamDir() as d:
            sub = os.path.join(d, "sub")
            os.mkdir(sub)
            with self.assertRaises(R.WLError) as c:
                R.Recorder(sub, 4, 2)
            self.assertEqual(c.exception.rc, 5)
        with self.assertRaises(R.WLError) as c:
            R.Recorder(ROOT, 4, 2)
        self.assertEqual(c.exception.rc, 5)

    def test_recorder_refuses_managed_dir_outside_ramws_root(self):
        # owner.json + held .lock, but not a direct child of RAMWS_ROOT (RAM-backed, never disk)
        base = "/dev/shm" if os.path.isdir("/dev/shm") else None
        d = tempfile.mkdtemp(prefix="wltest-out-", dir=base)
        self.assertNotEqual(os.path.dirname(os.path.realpath(d)), ROOT)
        fd = None
        try:
            with open(os.path.join(d, "owner.json"), "w") as f:
                json.dump({"pid": os.getpid(), "tool": "opensuperficies"}, f)
            fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(R.WLError) as c:
                R.Recorder(d, 4, 2)
            self.assertEqual(c.exception.rc, 5)
            self.assertIn("E_STORAGE", str(c.exception))
            self.assertFalse(os.path.exists(os.path.join(d, "frames.rgb24")))
        finally:
            if fd is not None:
                os.close(fd)
            shutil.rmtree(d, ignore_errors=True)

    def test_recorder_refuses_bad_dims(self):
        with RamDir() as d:
            for w, h in ((-4, -2), (0, 2), (4, 0), (-4, 2), (True, 2), (4, False),
                         (4.0, 2), ("4", 2), (None, 2)):
                with self.assertRaises(R.WLError, msg=(w, h)) as c:
                    R.Recorder(d, w, h)
                self.assertEqual(c.exception.rc, 2, (w, h))
                self.assertFalse(os.path.exists(os.path.join(d, "frames.rgb24")), (w, h))
            r = R.Recorder(d, 4, 2)
            self.assertEqual(r.size, 24)
            r.f.close()

    def test_bools_are_not_ints(self):
        self._refuses(lambda m: m.__setitem__("det_sha256", True))
        self._refuses(lambda m: m["items"][1]["steps"][0].__setitem__("t_ms", True))
        self._refuses(lambda m: m["items"][1].__setitem__("text_len", False))
        self._refuses(lambda m: m["items"][0]["steps"][0].__setitem__("at_pm", [True, 5]))

    def test_monad_ratio_pm_integer_checked(self):
        self._refuses(lambda m: m["qtile"].__setitem__("monad_ratio_pm", 550.5))
        self._refuses(lambda m: m["qtile"].__setitem__("monad_ratio_pm", True))

    def _cli(self, args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", os.path.join(WL, "recorder.py")] + args,
                              capture_output=True, env=env, timeout=60).returncode

    def test_cli_arity_and_dims_rc2(self):
        for args in (["validate", MAN, "--bogus"], ["order", MAN, "x"],
                     ["plan", MAN, "WL-1", "1920", "1080", "extra"],
                     ["labels", MAN, "WL-1", "1920", "1080", "default", "extra"],
                     ["plan", MAN, "WL-1", "-100", "0"], ["plan", MAN, "WL-1", "0", "1080"],
                     ["labels", MAN, "WL-1", "1920", "-1"], ["plan", MAN, "WL-1", "x", "y"]):
            self.assertEqual(self._cli(args), 2, args)
        for args in (["validate", MAN], ["order", MAN], ["plan", MAN, "WL-1", "1920", "1080"],
                     ["labels", MAN, "WL-1", "1920", "1080"], ["labels", MAN, "WL-1", "1920", "1080", "default"]):
            self.assertEqual(self._cli(args), 0, args)

    def test_recorder_files_0640_under_umask_022(self):
        import stat
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with RamDir() as d:
            r = R.Recorder(d, 2, 2, det=False)
            r.push(0, 0, frame(2, 2, 1))
            r.finish()
            for n in ("frames.rgb24", "wl_meta.json"):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode), 0o640, n)
        R.main(["order", MAN])
        self.assertEqual(os.umask(0o027), 0o027)

    def test_no_rc1(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for args, want in (([], 2), (["--bogus"], 2), (["validate", MAN], 0), (["validate", "/nonexistent"], 2)):
            p = subprocess.run([sys.executable, "-B", os.path.join(WL, "recorder.py")] + args,
                               capture_output=True, env=env, timeout=60)
            self.assertEqual(p.returncode, want, (args, p.stderr))
            self.assertNotEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
