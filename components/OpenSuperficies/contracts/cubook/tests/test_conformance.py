"""contracts/cubook conformance tests: schemas closed + in sync, fixtures valid, runner vs today's binaries, mutant kill 100%.
python3 -B -m unittest discover -s contracts/cubook/tests -t .   (needs go on PATH or /usr/local/go/bin for the one build)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


def jl(path):
    with open(path) as f:
        return json.load(f)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
sys.path.insert(0, ROOT)
import run_conformance as rcf  # noqa: E402

RUNNER = os.path.join(ROOT, "run_conformance.py")
WORK = os.environ.get("CUBOOK_WORK") or os.path.join(REPO, "build", "cubook_conf")
CLIS = rcf.CLIS
# mutant -> (cli whose fixtures must catch it, case expected among the failures)
MUTANTS = {
    "m01_usage_rc1": ("exec", "exec/usage_noargs"),
    "m02_extra_json_field": ("snap", "snap/fake_ok"),
    "m03_gate_rc0": ("exec", "exec/run_without_test_env_rc5"),
    "m04_trailing_data_accepted": ("exec", "exec/validate_trailing_rc2"),
    "m05_roi_target2_accepted": ("exec", "exec/validate_roi_target2_rc2"),
    "m06_mcp_fake_without_test_env": ("mcp", "mcp/fake_without_test_env_rc2"),
    "m07_mcp_tool_dropped": ("mcp", "mcp/tools_list"),
    "m08_snap_frames_65": ("snap", "snap/frames_65_rc2"),
    "m09_wlcap_fake_without_test_env": ("wlcap", "wlcap/fake_without_test_env_rc2"),
    "m10_replay_no_trailing_newline": ("exec", "exec/replay_ok"),
    "m11_start_timeout_rc2": ("exec", "exec/wl_start_timeout_rc5"),
    "m12_unmanaged_run_dir_accepted": ("snap", "snap/unmanaged_run_dir_rc5"),
    "m13_v2_plan_accepts_v3_op": ("exec", "exec/wl_v2_refuses_v3_op_rc2"),
    "m14_mcp_usage_rc1": ("mcp", "mcp/usage_noargs"),
}
_BIN = {}


def real_bin():
    if "d" in _BIN:
        return _BIN["d"]
    d = os.environ.get("CUBOOK_BIN_DIR")
    if not d:
        d = os.path.join(WORK, "bin")
        os.makedirs(d, exist_ok=True)
        env = dict(os.environ, PATH=os.environ["PATH"] + ":/usr/local/go/bin")
        p = subprocess.run(["taskset", "-c", "10-14", "go", "build", "-p", "5", "-o", d + "/", "./cmd/srf-cu-exec", "./cmd/srf-cu-mcp",
                            "./cmd/srf-cu-snap", "./cmd/srf-cu-wlcap"], cwd=REPO, env=env, capture_output=True, timeout=400)
        assert p.returncode == 0, p.stderr.decode()[:400]
    _BIN["d"] = d
    return d


def runner(bin_dir, *extra):
    os.makedirs(WORK, exist_ok=True)
    out = tempfile.mktemp(dir=WORK, suffix=".json")
    p = subprocess.run(["taskset", "-c", "10-14", sys.executable, "-B", RUNNER, "--bin-dir", bin_dir, "--json", out, *extra],
                       capture_output=True, timeout=300)
    res = jl(out) if os.path.exists(out) else None
    if os.path.exists(out):
        os.remove(out)
    return p.returncode, res, p.stdout.decode()


def closed(s, path="$"):
    bad = []
    if isinstance(s, dict):
        if s.get("type") == "object" and "properties" in s and s.get("additionalProperties") is not False:
            if "x-open" not in s:
                bad.append(path)
        for k, v in s.items():
            bad += closed(v, path + "." + k)
    elif isinstance(s, list):
        for i, v in enumerate(s):
            bad += closed(v, "%s[%d]" % (path, i))
    return bad


class Schemas(unittest.TestCase):
    def test_in_sync_with_generator(self):
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "make_schemas.py"), "--check"], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_ids_and_closure(self):
        names = sorted(os.listdir(os.path.join(ROOT, "schemas")))
        self.assertGreaterEqual(len(names), 15)
        for n in names:
            s = jl(os.path.join(ROOT, "schemas", n))
            self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(n, s["$id"].replace("/", "-") + ".schema.json")
            self.assertEqual(closed(s), [], n)
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(s)
        wanted = {"vqa.action/v1", "vqa.act-plan/v1", "vqa.act-step/v1", "vqa.frame/v1", "os.cu.capture-receipt/v1", "os.cu.prereg-wl/v2",
                  "os.cu.prereg-wl/v3", "os.cu.wl-act-step/v1", "os.cu.ast/v1", "os.cu.rc/v1", "os.cu.mcp-tools/v1", "os.cu.mcp-wire/v1",
                  "os.cu.wlcap-bench/v1", "os.cu.wlcap-receipt/v1", "os.cu.wlcap-start/v1"}
        self.assertEqual({jl(os.path.join(ROOT, "schemas", n))["$id"] for n in names}, wanted)

    def test_real_prereg_files_validate(self):
        repo = os.path.abspath(os.path.join(ROOT, "..", ".."))
        for v in (2, 3):
            rcf.validate(jl(os.path.join(repo, "bench", "wl", "prereg_wl_v%d.json" % v)), "os.cu.prereg-wl/v%d" % v)

    def test_x_open_not_forced_closed_but_steps_stay_closed(self):
        import jsonschema
        s = rcf.load_schema("os.cu.prereg-wl/v2")
        self.assertNotIn("additionalProperties", s)
        item = s["properties"]["items"]["items"]
        self.assertNotIn("additionalProperties", item)
        self.assertIs(item["properties"]["steps"]["items"]["additionalProperties"], False)
        bad = {"schema": "os.cu.prereg-wl/v2", "items": [{"id": "a", "steps": [{"t_ms": 0, "op": "click", "zzz": 1}]}]}
        with self.assertRaises(jsonschema.ValidationError):
            rcf.validate(bad, "os.cu.prereg-wl/v2")

    def test_wlcap_schemas_closed_and_reject_extra(self):
        import jsonschema
        for sid in ("os.cu.wlcap-bench/v1", "os.cu.wlcap-receipt/v1", "os.cu.wlcap-start/v1"):
            s = rcf.load_schema(sid)
            self.assertIs(s["additionalProperties"], False, sid)
            self.assertTrue(set(s["required"]) <= set(s["properties"]), sid)
        start = jl(self.fx_start())
        rcf.validate(start, "os.cu.wlcap-start/v1")
        with self.assertRaises(jsonschema.ValidationError):
            rcf.validate(dict(start, extra=1), "os.cu.wlcap-start/v1")

    def fx_start(self):
        return os.path.join(ROOT, "fixtures", "exec", "wl_run_ok", "files", "START")

    def test_wlcap_bench_ok_validates_receipt(self):
        meta = jl(os.path.join(ROOT, "fixtures", "wlcap", "bench_ok", "meta.json"))
        self.assertIn({"path": "o/receipt.json", "schema": "os.cu.wlcap-bench/v1"}, meta["expect_files"])

    def test_rc_enum_has_no_one(self):
        e = rcf.load_schema("os.cu.rc/v1")["enum"]
        self.assertEqual(e, [0, 2, 3, 4, 5, 6, 7])


class FixturesVsSchemas(unittest.TestCase):
    def fx(self, *p):
        return os.path.join(ROOT, "fixtures", *p)

    def test_plans_validate_or_fail_like_go(self):
        import jsonschema
        ok = {"validate_ok": True, "validate_roi_target_ok": True, "validate_roi_rect_ok": True, "validate_roi_target2_rc2": False,
              "validate_float_rc2": False, "validate_unknown_field_rc2": False}
        for case, good in ok.items():
            plan = jl(self.fx("exec", case, "files", "p.json"))
            if good:
                rcf.validate(plan, "vqa.act-plan/v1")
            else:
                with self.assertRaises(jsonschema.ValidationError, msg=case):
                    rcf.validate(plan, "vqa.act-plan/v1")

    def test_recorded_outputs_validate(self):
        for ln in open(self.fx("exec", "run_fake_ok", "expected_files", "s.jsonl")):
            rcf.validate(json.loads(ln), "vqa.act-step/v1")
        rcf.validate(jl(self.fx("snap", "fake_ok", "expected_files", "o", "capture_receipt.json")), "os.cu.capture-receipt/v1")
        tl = json.loads(open(self.fx("mcp", "tools_list", "expected_stdout")).read())
        rcf.validate(tl["result"], "os.cu.mcp-tools/v1")
        rcf.validate(tl, "os.cu.mcp-wire/v1")

    def test_schema_rejects_mutations(self):
        import jsonschema
        row = json.loads(open(self.fx("exec", "run_fake_ok", "expected_files", "s.jsonl")).readline())
        for mut in ({"extra": 1}, {"rc": 1}, {"verdict": "MAYBE"}, {"schema": "vqa.act-step/v2"}):
            with self.assertRaises(jsonschema.ValidationError, msg=str(mut)):
                rcf.validate(dict(row, **mut), "vqa.act-step/v1")

    def test_inventory_and_no_rc1(self):
        for cli in CLIS:
            cases = os.listdir(self.fx(cli))
            self.assertGreaterEqual(len(cases), 3, cli)
            rcs = {int(open(self.fx(cli, c, "expected_rc")).read()) for c in cases}
            self.assertIn(2, rcs, cli)
            self.assertIn(0, rcs, cli)
            self.assertNotIn(1, rcs)
            if cli != "mcp":  # srf-cu-mcp reports gate refusals in-band (tool result rc), never as a process rc 5
                self.assertIn(5, rcs, cli)


class Normalise(unittest.TestCase):
    def test_closed_allowlist(self):
        with self.assertRaises(ValueError):
            rcf.normalise(b'{"t_us":1}\n', ["t_us"])
        self.assertEqual(len(rcf.VOLATILE_ALLOW), 14)

    def test_pre_strip_presence(self):
        out, seen = rcf.normalise(b'{"a":{"pid":5},"x":1}\n', ["pid", "run_id"])
        self.assertEqual(seen, {"pid"})
        self.assertIn(b'"pid":"<V>"', out)


class Conformance(unittest.TestCase):
    def test_empty_bin_dir_rc5(self):  # failing-first state
        d = tempfile.mkdtemp(dir=WORK if os.path.isdir(WORK) else None)
        try:
            self.assertEqual(runner(d)[0], 5)
        finally:
            shutil.rmtree(d)

    def test_usage_rc2(self):
        p = subprocess.run([sys.executable, "-B", RUNNER], capture_output=True)
        self.assertEqual(p.returncode, 2)

    def test_todays_binaries_pass(self):
        rc, res, out = runner(real_bin())
        self.assertEqual(rc, 0, out)
        self.assertGreaterEqual(res["summary"]["cases"], 36)

    def test_mutants_all_killed(self):
        real = real_bin()
        self.assertGreaterEqual(len(MUTANTS), 8)
        for name, (cli, killer) in MUTANTS.items():
            d = tempfile.mkdtemp(dir=WORK)
            try:
                for c in CLIS:
                    p = os.path.join(d, "srf-cu-" + c)
                    with open(p, "w") as f:
                        f.write('#!/bin/sh\nCUBOOK_REAL_BIN=%s exec python3 -B %s %s "$@"\n' % (
                            real, os.path.join(ROOT, "mutants", name + ".py"), c))
                    os.chmod(p, 0o755)
                rc, res, out = runner(d, "--only", cli)
                self.assertEqual(rc, 5, "%s survived: %s" % (name, out))
                self.assertIn(killer, [r["case"] for r in res["results"] if not r["ok"]], name)
            finally:
                shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
