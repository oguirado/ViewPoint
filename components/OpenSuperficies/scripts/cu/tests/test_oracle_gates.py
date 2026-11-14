"""Lane G tests (W6a): oracle mutant catalogue, canonical-projection determinism x2, storage law, gate script contract.
Run: python3 -B -m unittest scripts.cu.tests.test_oracle_gates (repo root). Mutants run on a tempdir copy of only the files they touch (AW6-11)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GATES = os.path.join(REPO, "scripts", "cu", "oracle_gates.sh")
MUTS = os.path.join(REPO, "scripts", "cu", "oracle_mutants.json")
REQUIRED = {
    "orc.proxy.prefix_match", "orc.proxy.case_fold", "orc.proxy.stop_at_nul", "orc.proxy.drop_input_deny", "orc.proxy.allow_runtime_evaluate",
    "orc.proxy.navigate_any_file", "orc.proxy.lim_m_not_m1", "orc.proxy.oversize_need_more", "orc.proxy.forward_denied_events",
    "orc.proxy.refusal_rc1", "orc.nolisten.skip_unix_acceptcon", "orc.nolisten.ignore_tcp6", "orc.label.mv_sign_flip",
    "orc.label.exact_for_canvas", "orc.label.emit_jitter", "orc.label.straddle_aligned", "orc.xc.tolerance_2", "orc.xc.empty_passes",
    # FX1 (INT2): F1 proxy/launch and F2 oracle python mutants
    "orc.shim.fd_table_off",
    "orc.nolisten.debug_name_off",
    "orc.shim.argv_case_fold_off",
    "orc.shim.argv_underscore_off",
    "orc.proxy.info_changed_any_url",
    "orc.proxy.info_changed_any_type",
    "orc.proxy.info_changed_unknown_target",
    "orc.proxy.candidates_unbounded",
    "orc.proxy.leave_scene_stays_bound",
    "orc.proxy.synth_leaks_blank_attach",
    "orc.allowlist.drop_info_changed",
    "lab.smooth_ignore_span",
    "lab.scroll_mv_not_negated",
    "xc.content_not_negated",
    "lab.clip_off",
    "lab.autofocus_needs_change",
    "smp.t0_zero_ok",
    "smp.nonce_unchecked",
    "smp.track_ids_all",
    "xc.v2_wl2b_missing",
    "xc.empty_rc2",
}
KEYS = {"id", "file", "find", "replace", "precheck", "oracle"}
FRAME_NAMES = ("a.png", "b.rgb", "c.ppm", "d.raw", "e.jpg")


def sh(args, **kw):
    r = subprocess.run(["timeout", "60", "bash", GATES] + args, capture_output=True, text=True, cwd=REPO, **kw)
    return r.returncode


def wr(d, name, data):
    with open(os.path.join(d, name), "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def rows(run_id, t0, extra=0):
    return "".join(json.dumps({"run_id": run_id, "seq": i, "t_us": t0 + 7 * i, "f0": i, "f1": i + 1, "mv": [0, 4 + extra], "ideal": 3}, sort_keys=True) + "\n"
                   for i in range(3))


class Catalogue(unittest.TestCase):
    def load(self):
        with open(MUTS, encoding="utf-8") as fh:
            return json.load(fh)

    def test_schema_and_ids(self):
        d = self.load()
        self.assertEqual(d["schema"], "os.cu.mutants/v1")
        ms = d["mutants"]
        ids = [m["id"] for m in ms]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), REQUIRED)
        for m in ms:
            self.assertTrue(KEYS <= set(m), m["id"])
            self.assertNotEqual(m["find"], m["replace"], m["id"])
            self.assertTrue(m["find"] and m["oracle"] and m["precheck"], m["id"])
            self.assertFalse(m["file"].startswith("/") or ".." in m["file"], m["id"])
            self.assertTrue(m["file"].startswith(("docker/cu/oracle/", "bench/oracle/")), m["id"])
            self.assertNotIn("go test ./...", m["oracle"])

    def check_one(self, m):
        src = os.path.join(REPO, m["file"])
        self.assertTrue(os.path.isfile(src), "target missing: " + m["file"])
        with open(src, encoding="utf-8") as fh:
            orig = fh.read()
        self.assertEqual(orig.count(m["find"]), 1, m["id"] + " find must match once")
        tmp = tempfile.mkdtemp(prefix="orcmut_", dir=os.environ.get("TMPDIR", "/tmp"))
        try:
            for rel in ("docker/cu", "bench", "vision", "scripts/cu"):
                s = os.path.join(REPO, rel)
                if os.path.isdir(s):
                    shutil.copytree(s, os.path.join(tmp, rel), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

            def run(c):
                return subprocess.run(["taskset", "-c", "10-14", "timeout", "120", "bash", "-c", c], cwd=tmp, capture_output=True, text=True).returncode
            self.assertEqual(run(m["precheck"]), 0, m["id"] + " precheck")
            self.assertEqual(run(m["oracle"]), 0, m["id"] + " dead: oracle fails on the original")
            with open(os.path.join(tmp, m["file"]), "w", encoding="utf-8") as fh:
                fh.write(orig.replace(m["find"], m["replace"]))
            self.assertEqual(run(m["precheck"]), 0, m["id"] + " mutant must compile")
            self.assertNotEqual(run(m["oracle"]), 0, m["id"] + " SURVIVED")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_every_mutant_killed_by_execution(self):
        """Contrast pair (SW-0065): original oracle rc 0, mutant oracle rc != 0, find matches exactly once (SW-0067 MS = 1.0)."""
        ms = self.load()["mutants"]
        self.assertEqual(len(ms), len(REQUIRED))
        for m in ms:
            with self.subTest(m["id"]):
                self.check_one(m)


class Canon(unittest.TestCase):
    def pair(self, a, b):
        with tempfile.TemporaryDirectory() as d:
            wr(d, "a.jsonl", a)
            wr(d, "b.jsonl", b)
            return sh(["--canon", os.path.join(d, "a.jsonl"), os.path.join(d, "b.jsonl")])

    def test_equal_modulo_run_id_and_t_us(self):
        self.assertEqual(self.pair(rows("r1", 1000), rows("r2", 99999)), 0)

    def test_differs_on_label_content(self):
        self.assertEqual(self.pair(rows("r1", 1000), rows("r2", 1000, extra=1)), 5)

    def test_differs_on_actuator_frame(self):
        b = rows("r2", 1000).replace('"f1": 1,', '"f1": 2,', 1)
        self.assertEqual(self.pair(rows("r1", 1000), b), 5)

    def test_empty_is_not_green(self):
        self.assertEqual(self.pair("", ""), 5)

    def test_bad_json_and_usage(self):
        self.assertEqual(self.pair("{x\n", "{x\n"), 5)
        self.assertEqual(sh(["--canon", "/nonexistent/a"]), 2)


class Storage(unittest.TestCase):
    def d(self, files):
        tmp = tempfile.mkdtemp(prefix="orcstor_")
        self.addCleanup(shutil.rmtree, tmp, True)
        for ep, name, data in files:
            os.makedirs(os.path.join(tmp, ep), exist_ok=True)
            wr(os.path.join(tmp, ep), name, data)
        return tmp

    def test_ok(self):
        self.assertEqual(sh(["--storage", self.d([("o_wl1s", "rows.jsonl", rows("r", 1))])]), 0)

    def test_over_100kb(self):
        self.assertEqual(sh(["--storage", self.d([("o_wl1s", "rows.jsonl", b"x" * (100 * 1024 + 1))])]), 5)

    def test_frame_file_names(self):
        for n in FRAME_NAMES:
            self.assertEqual(sh(["--storage", self.d([("o_wl1s", "rows.jsonl", "{}\n"), ("o_wl1s", n, b"x")])]), 5, n)

    def test_png_magic_under_innocent_name(self):
        self.assertEqual(sh(["--storage", self.d([("o_wl1s", "rows.jsonl", b"\x89PNG\r\n\x1a\n" + b"0" * 20)])]), 5)

    def test_empty_dir_not_green(self):
        self.assertEqual(sh(["--storage", self.d([])]), 5)

    def test_usage(self):
        self.assertEqual(sh(["--storage", "/nonexistent/zz"]), 2)
        self.assertEqual(sh(["--bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
