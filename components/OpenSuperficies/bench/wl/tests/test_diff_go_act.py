import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(WL))
sys.path.insert(0, WL)

import labels_v2 as L2  # noqa: E402
import labels_v3 as L3  # noqa: E402
import recorder as R  # noqa: E402

MAN2 = os.path.join(WL, "prereg_wl_v2.json")
MAN3 = os.path.join(WL, "prereg_wl_v3.json")
W, H = 1920, 1080

EXPECTED_DIVERGENCES = {}


class TestDiffGoAct(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work_dir = tempfile.mkdtemp(prefix="diff_go_act_")
        cls.exec_bin = os.path.join(cls.work_dir, "srf-cu-exec")
        env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin")
        cmd = ["go", "build", "-p", "5", "-o", cls.exec_bin, "./cmd/srf-cu-exec"]
        try:
            p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, timeout=300)
            if p.returncode != 0:
                raise RuntimeError("go build failed:\n" + p.stderr.decode())
        except Exception as e:
            raise RuntimeError("Failed to build cmd/srf-cu-exec: %s" % e)

        cls.m2 = L2.load_manifest(MAN2)
        cls.m3 = L3.load_manifest(MAN3)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "work_dir") and os.path.exists(cls.work_dir):
            shutil.rmtree(cls.work_dir)

    def _run_go_act(self, prereg_path, item_id):
        run_dir = tempfile.mkdtemp(dir=self.work_dir)
        start_file = os.path.join(run_dir, "START")
        start_data = {
            "schema": "os.cu.wlcap-start/v1",
            "clock": "CLOCK_MONOTONIC",
            "t0_ns": time.monotonic_ns(),
            "w": W,
            "h": H,
        }
        with open(start_file, "w") as f:
            json.dump(start_data, f)

        act_file = os.path.join(run_dir, "act.jsonl")
        cmd = [
            self.exec_bin,
            "run",
            "--prereg",
            prereg_path,
            "--item",
            item_id,
            "--start",
            start_file,
            "--out",
            act_file,
            "--mock",
            "--start-wait-ms",
            "2000",
        ]
        env = dict(os.environ, OS_CU_TEST="1")
        p = subprocess.run(cmd, env=env, capture_output=True, timeout=60)
        return p, act_file

    def test_positive_items_v2_and_v3(self):
        cases = [
            (2, MAN2, self.m2, ["WL-2b", "WL-2", "WL-1", "WL-7"], L2),
            (3, MAN3, self.m3, ["WL-1s", "WL-1c", "WL-1L", "WL-1h", "WL-4", "WL-3"], L3),
        ]
        strip_keys = {"actuator_seq", "t_us", "frame", "until_us"}

        for ver, prereg_path, manifest, item_ids, mod in cases:
            for item_id in item_ids:
                with self.subTest(ver=ver, item_id=item_id):
                    p, act_file = self._run_go_act(prereg_path, item_id)
                    self.assertEqual(p.returncode, 0, f"srf-cu-exec failed for {item_id}: {p.stderr.decode()}")
                    self.assertTrue(os.path.exists(act_file), f"act.jsonl missing for {item_id}")

                    rows = L2.read_act(act_file)
                    item_obj = L2._item(manifest, item_id)

                    go_events = [(r["step"], r["point"], r["op"], r["t_plan_us"]) for r in rows]
                    py_events = mod.plan_events(item_obj)
                    self.assertEqual(go_events, py_events, f"Event list mismatch for {item_id}")

                    act_labels = mod.labels_from_act(manifest, item_id, W, H, rows)
                    plan_labels = mod.labels_for(manifest, item_id, W, H)

                    act_geom = [{k: v for k, v in r.items() if k not in strip_keys} for r in act_labels]
                    plan_geom = [{k: v for k, v in r.items() if k not in strip_keys} for r in plan_labels]
                    self.assertEqual(act_geom, plan_geom, f"Label geometry mismatch for {item_id}")

    def test_negative_mutated_manifests(self):
        mutations = []

        # 1. Scroll with dy 0
        m1 = copy.deepcopy(self.m2)
        [it for it in m1["items"] if it["id"] == "WL-1"][0]["steps"][0]["dy"] = 0
        mutations.append(("scroll_dy_0", 2, "WL-1", m1, L2))

        # 2. Move without dt_ms
        m2 = copy.deepcopy(self.m2)
        del [it for it in m2["items"] if it["id"] == "WL-7"][0]["steps"][0]["dt_ms"]
        mutations.append(("move_without_dt_ms", 2, "WL-7", m2, L2))

        # 3. Type of two characters
        m3 = copy.deepcopy(self.m2)
        [it for it in m3["items"] if it["id"] == "WL-2"][0]["steps"][0]["text"] = "ab"
        mutations.append(("type_two_chars", 2, "WL-2", m3, L2))

        # 4. Decreasing t_ms
        m4 = copy.deepcopy(self.m3)
        [it for it in m4["items"] if it["id"] == "WL-1s"][0]["steps"][1]["t_ms"] = 100
        mutations.append(("decreasing_t_ms", 3, "WL-1s", m4, L3))

        # 5. Off-screen at_px
        m5 = copy.deepcopy(self.m3)
        [it for it in m5["items"] if it["id"] == "WL-1s"][0]["steps"][0]["at_px"] = [2000, 2000]
        mutations.append(("offscreen_at_px", 3, "WL-1s", m5, L3))

        # 6. Unknown op
        m6 = copy.deepcopy(self.m2)
        [it for it in m6["items"] if it["id"] == "WL-1"][0]["steps"][0]["op"] = "bad_op"
        mutations.append(("unknown_op", 2, "WL-1", m6, L2))

        for name, ver, item_id, manifest, mod in mutations:
            with self.subTest(name=name, ver=ver, item_id=item_id):
                with self.assertRaises(R.WLError, msg=f"Python check_manifest should reject {name}"):
                    mod.check_manifest(manifest, WL)

                mut_path = os.path.join(self.work_dir, f"mut_{name}.json")
                with open(mut_path, "w") as f:
                    json.dump(manifest, f)

                p, _ = self._run_go_act(mut_path, item_id)
                self.assertIn(p.returncode, (2, 5), f"Go srf-cu-exec should reject {name} with rc 2 or 5, got {p.returncode}")


if __name__ == "__main__":
    unittest.main()
