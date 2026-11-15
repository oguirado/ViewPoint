"""RD6A-01 / ADC2-W6A-01: the S-LIVE v5 runner oracle_post join (act.jsonl step,point -> prereg plan x,y) extracted from run_slive5.sh and run on
WL-7 / WL-2b act fixtures built from the frozen prereg_wl_v2.json. act.jsonl step is 1-based (wlplan.go Step: i + 1; labels_v2.plan_events)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from bench.oracle import label_codec as LC

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
RUNNER = os.environ.get("OS_CU_RUNNER_PATH", "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w6/a/slive/run_slive5.sh")
PREREG = os.path.join(REPO, "bench", "wl", "prereg_wl_v2.json")
FX = os.path.join(HERE, "fixtures")


def heredoc(marker):
    """Body of the python heredoc whose opening line contains marker."""
    lines = open(RUNNER, encoding="utf-8").read().splitlines()
    i = [n for n, l in enumerate(lines) if marker in l and "<<'PY_EOF'" in l]
    if len(i) != 1:
        raise AssertionError("heredoc %r found %d times" % (marker, len(i)))
    j = lines.index("PY_EOF", i[0] + 1)
    return "\n".join(lines[i[0] + 1:j]) + "\n"


def act_rows(item, step, op, points, t0_ms, dt_ms):
    return [{"schema": "os.cu.wl-act-step/v1", "item": item, "actuator_seq": k + 1, "step": step, "point": p, "op": op,
             "t_plan_us": (t0_ms + p * dt_ms) * 1000, "t_us": (t0_ms + p * dt_ms) * 1000 + 50, "rc": 0} for k, p in enumerate(points)]


def sample(seq, t):
    return {"seq": seq, "t_us": t, "snap": "aa", "nodes": None, "sx": 0, "sy": 0, "focus": None, "shifts": [], "frac": 0, "canv": []}


OPTIONAL = os.environ.get("OS_CU_RUNNER_OPTIONAL") == "1"


@unittest.skipIf(OPTIONAL and not os.path.isfile(RUNNER), "OS_CU_RUNNER_OPTIONAL=1 and the conductor runner is absent")
class RunnerJoin(unittest.TestCase):
    def setUp(self):
        # RD6B-01: an absent runner FAILS (the only RD6A-01 regression guard never goes silent) unless OS_CU_RUNNER_OPTIONAL=1
        self.assertTrue(os.path.isfile(RUNNER), "conductor runner absent: %s (set OS_CU_RUNNER_OPTIONAL=1 to skip)" % RUNNER)
        self.td = tempfile.TemporaryDirectory()
        self.d = self.td.name
        self.join = os.path.join(self.d, "join.py")
        with open(self.join, "w") as f:
            f.write(heredoc('"$w/samples.json" "$PRF" "$id" "$w/lo_args.txt"'))

    def tearDown(self):
        self.td.cleanup()

    def run_join(self, item, acts, samples=None, prereg=PREREG):
        p = lambda n: os.path.join(self.d, n)
        with open(p("rows.jsonl"), "w") as f:
            for r in samples or [sample(1, 900000), sample(2, 5000000)]:
                f.write(json.dumps(r) + "\n")
        with open(p("act.jsonl"), "w") as f:
            for r in acts:
                f.write(json.dumps(r) + "\n")
        r = subprocess.run([sys.executable, "-B", "-I", self.join, p("rows.jsonl"), p("act.jsonl"), p("samples.json"), prereg, item, p("lo_args.txt")],
                           capture_output=True, text=True, timeout=60)
        return r

    def labels(self, args):
        p = lambda n: os.path.join(self.d, n)
        return subprocess.run([sys.executable, "-B", os.path.join(REPO, "bench", "oracle", "labels_oracle.py"), "--samples", p("samples.json"),
                               "--w", "1920", "--h", "1080", "--run-id", "t", "--out", p("labels_compact.jsonl")] + args, capture_output=True, text=True, timeout=60)

    def test_runner_join_step_is_one_based(self):
        r = self.run_join("WL-7", act_rows("WL-7", 1, "move", range(8), 1000, 500))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ops = json.load(open(os.path.join(self.d, "samples.json")))["ops"]
        self.assertEqual(len(ops), 8)
        self.assertEqual((ops[0]["x"], ops[0]["y"]), (200, 200))
        plan = [x for x in json.load(open(PREREG))["items"] if x["id"] == "WL-7"][0]["steps"][0]["path"]
        self.assertEqual([[o["x"], o["y"]] for o in ops], plan)
        self.assertEqual([o["point"] for o in ops], list(range(8)))
        self.assertEqual((ops[1]["x"], ops[1]["y"]), (400, 200))
        args = open(os.path.join(self.d, "lo_args.txt")).read().split()
        self.assertEqual(args, ["--cursor-px", "16"])
        lr = self.labels(args)
        self.assertEqual(lr.returncode, 0, lr.stderr)
        labs = LC.decode(open(os.path.join(self.d, "labels_compact.jsonl"), "rb").read())
        ptr = [l for l in labs if l["source"] == "pointer"]
        xc = [json.loads(l) for l in open(os.path.join(FX, "xc_wl7.jsonl")) if l.strip()]
        self.assertEqual([l["ideal"] for l in ptr], [3] * 7)  # ADC2-W6A-02: points 1..7 MOVED, point 0 is the start
        self.assertEqual([l["rects"] for l in ptr], [l["rects"] for l in xc])
        self.assertEqual(ptr[0]["mv"], [200, 0])

    def test_runner_join_miss_is_rc5_not_silent(self):
        for st in (0, 2):  # 0-based (the old off-by-one view) or past the plan: counted, rc 5 because WL-7 sets cursor_px
            r = self.run_join("WL-7", act_rows("WL-7", st, "move", range(2), 1000, 500))
            self.assertEqual(r.returncode, 5, (st, r.stdout, r.stderr))
            self.assertIn("JOIN_MISS", r.stdout)

    def test_runner_join_wl2b_caret_flags(self):
        r = self.run_join("WL-2b", act_rows("WL-2b", 1, "wait", [0], 6000, 0),
                          [dict(sample(i + 1, 1000000 + i * 16666), focus="f1") for i in range(4)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = open(os.path.join(self.d, "lo_args.txt")).read().split()
        self.assertEqual(args, ["--caret-rect", "50,42,2,36", "--period-ms", "500"])
        ops = json.load(open(os.path.join(self.d, "samples.json")))["ops"]
        self.assertEqual([o["op"] for o in ops], ["wait"])
        lr = self.labels(args)
        self.assertEqual(lr.returncode, 0, lr.stderr)
        labs = LC.decode(open(os.path.join(self.d, "labels_compact.jsonl"), "rb").read())
        self.assertEqual([(l["ideal"], l["period_ms"], l["rects"]) for l in labs], [(4, 500, [[50, 42, 2, 36]])])

    def test_runner_join_live_wl1s_fits(self):
        """FX3 L5: the live WL-1s inputs through the runner join + labels_oracle: rc 0 (was E_CAP rc 5), compact <= 24 KB, decode == v1 sha."""
        L5 = os.path.join(FX, "l5")
        d = json.load(open(os.path.join(L5, "wl1s_live_samples.json")))
        e = json.load(open(os.path.join(L5, "wl1s_live_expect.json")))
        acts = [json.loads(l) for l in open(os.path.join(L5, "wl1s_live_act.jsonl")) if l.strip()]
        r = self.run_join("WL-1s", acts, d["samples"], os.path.join(REPO, "bench", "wl", "prereg_wl_v3.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.load(open(os.path.join(self.d, "samples.json"))), d)
        lr = subprocess.run([sys.executable, "-B", os.path.join(REPO, "bench", "oracle", "labels_oracle.py"), "--samples", os.path.join(self.d, "samples.json"),
                             "--w", "1920", "--h", "1080", "--run-id", e["run_id"], "--out", os.path.join(self.d, "labels_compact.jsonl")],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(lr.returncode, 0, lr.stderr)
        data = open(os.path.join(self.d, "labels_compact.jsonl"), "rb").read()
        self.assertLessEqual(len(data), 24 * 1024)
        self.assertEqual(hashlib.sha256(LC.canonical_v1(LC.decode(data))).hexdigest(), e["v1_sha256"])
        self.assertEqual(data, LC.encode_v2(LC.decode(data)))  # FX4: file name unchanged, content codec v2

    def test_runner_join_live_wl4_v2(self):
        """FX4 L5: live WL-4 (div drag; compact v1 49917 B > the 48 KB oracle dir budget) through the runner join + labels_oracle -> v2 5913 B."""
        ev = "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w6/evidence/X4L5/wl4_inputs"
        if not os.path.exists(os.path.join(ev, "rows.jsonl")):
            self.skipTest("live WL-4 rows (C evidence, 52 KB) absent; test_label_codec pins the v2 bytes")
        L5B = os.path.join(FX, "l5b")
        e = json.load(open(os.path.join(L5B, "wl4_live_expect.json")))
        rows = [json.loads(l) for l in open(os.path.join(ev, "rows.jsonl")) if l.strip()]
        acts = [json.loads(l) for l in open(os.path.join(L5B, "wl4_live_act.jsonl")) if l.strip()]
        r = self.run_join("WL-4", acts, rows, os.path.join(REPO, "bench", "wl", "prereg_wl_v3.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.load(open(os.path.join(self.d, "samples.json"))), json.load(open(os.path.join(ev, "samples.json"))))
        lr = subprocess.run([sys.executable, "-B", os.path.join(REPO, "bench", "oracle", "labels_oracle.py"), "--samples", os.path.join(self.d, "samples.json"),
                             "--w", "1920", "--h", "1080", "--run-id", e["run_id"], "--out", os.path.join(self.d, "labels_compact.jsonl")],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(lr.returncode, 0, lr.stderr)
        data = open(os.path.join(self.d, "labels_compact.jsonl"), "rb").read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), e["v2_sha256"])
        self.assertEqual(hashlib.sha256(LC.canonical_v1(LC.decode(data))).hexdigest(), e["v1_sha256"])

    def test_oracle_post_compact_file_name(self):
        """FX3 L5: oracle_post writes, cross-checks, hashes and journals the compact labels file, and records the decoded v1 sha for the peer."""
        s = open(RUNNER, encoding="utf-8").read()
        i, j = s.index("oracle_post() {"), s.index("wl_item() {")
        body = s[i:j]
        self.assertNotIn("labels.jsonl", body)
        for k in ('--out "$od/labels_compact.jsonl"', '--oracle "$od/labels_compact.jsonl"', 'sha256sum "$w/rows.jsonl" "$od/labels_compact.jsonl"',
                  'sha256sum "$od/labels_compact.jsonl"', "bench/oracle/label_codec.py decode", "ORACLE_LABELS_V1_SHA"):
            self.assertIn(k, body)

    def test_sample_loop_rows_capped(self):
        body = heredoc('cat > "$T/oracle/sample_loop.py"')
        self.assertIn("sampler.RowSink()", body)  # RD6A-02: rows stop at the write_rows cap
        self.assertIn("truncated", body)
        self.assertNotRegex(body, r"\brows\.append\(")


if __name__ == "__main__":
    unittest.main()
