"""O3: bm.* mutant manifest checks (host only). Blender kill tests live in test_mutants_blender.py."""
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest

from blender_visual_qa.oracles import harness

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AD = os.path.join(ROOT, "adapters", "example_scene")
sys.path.insert(0, AD)
import build_manifest  # noqa: E402

ID_RE = re.compile(r"^bm\.[a-z][a-z0-9_]*\.[0-9]{2}$")
TOP = {"schema", "baseline_sha256", "adapter_sha256", "checklist_sha256", "capture_plan_sha256", "mutants",
       "clean_schedule", "blend_base_sha256", "blender_version"}
ENTRY = {"id", "kind", "fault", "fault_spec_sha256", "applicability", "interval", "expected_sd", "expected_px",
         "expected_judge", "expected_check_ids", "equivalent", "expected_tl"}


def load():
    with open(os.path.join(AD, "mutant-manifest.json")) as fh:
        return json.load(fh)


def registry():
    with open(os.path.join(AD, "registry.json")) as fh:
        return json.load(fh)


def clean_pass():
    return {"verdict": "PASS", "rc": 0, "findings": []}


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_file_is_regenerated_byte_identical(self):
        with open(os.path.join(AD, "mutant-manifest.json")) as fh:
            self.assertEqual(fh.read(), build_manifest.build())

    def test_top_level_shape(self):
        self.assertEqual(set(self.m), TOP)
        self.assertEqual(self.m["schema"], "bvqa.mutant-manifest/v1")
        for k in ("baseline_sha256", "adapter_sha256", "checklist_sha256", "capture_plan_sha256"):
            self.assertRegex(self.m[k], r"^[0-9a-f]{64}$")

    def test_ids_unique_grammar_and_count(self):
        ids = [e["id"] for e in self.m["mutants"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 22)
        for i in ids:
            self.assertRegex(i, ID_RE)
        self.assertEqual(sorted(int(i[-2:]) for i in ids), list(range(1, 23)))
        self.assertEqual(sorted(e["id"] for e in self.m["mutants"] if e["equivalent"]),
                         ["bm.neg_scale.18", "bm.step_hold.22"])

    def test_entries_shape_and_fault_sha_rehash(self):
        for e in self.m["mutants"]:
            with self.subTest(e["id"]):
                self.assertEqual(set(e), ENTRY)
                self.assertEqual(hashlib.sha256(harness.canonical_json(e["fault"]).encode()).hexdigest(),
                                 e["fault_spec_sha256"])
                self.assertEqual(set(e["fault"]), {"operation", "selector", "parameters"})
                self.assertEqual(e["expected_check_ids"], sorted(set(e["expected_sd"]).union(e["expected_px"], e["expected_judge"])))
                self.assertTrue(e["expected_check_ids"])
                iv = e["interval"]
                self.assertTrue(0 <= iv["start_render_index"] <= iv["end_render_index"] <= 7)
                for v in e["fault"]["parameters"].values():
                    self.assertIn(type(v), (int, str))

    def test_expected_ids_exist_in_registry(self):
        r = registry()
        for e in self.m["mutants"]:
            for key in ("sd", "px", "judge"):
                for c in e["expected_" + key]:
                    self.assertIn(c, r[key], (e["id"], c))
        with open(os.path.join(AD, "checklist.json")) as fh:
            cl = json.load(fh)
        self.assertEqual(sorted(c["id"] for c in cl["checks"]), registry()["judge"])

    def test_every_sd_oracle_has_a_killer_except_equivalents(self):
        killed = set()
        for e in self.m["mutants"]:
            if not e["equivalent"]:
                killed.update(e["expected_sd"])
        # BSPEC_BORACLES section 4 plants no mutant for bo.mesh.duplicate_faces (unit-tested by O1 only).
        self.assertEqual(killed | {"bo.mesh.duplicate_faces"}, set(registry()["sd"]))

    def test_expected_tl_only_on_truth_class_seeds(self):
        tl = {e["id"]: e["expected_tl"] for e in self.m["mutants"] if e["expected_tl"] is not None}
        self.assertEqual(tl, {"bm.delete_face.03": 4, "bm.camera_yaw.10": 5, "bm.freeze_anim.14": 10,
                              "bm.drift.15": 11, "bm.flicker.16": 9})

    def test_every_non_scene_check_is_covered_by_px_or_judge_row(self):
        px = {c for e in self.m["mutants"] for c in e["expected_px"]}
        for c in ("frame.magenta", "frame.black", "frame.frozen", "frame.uniform", "vc_flicker",
                  "vc_frozen_subregion", "vc_slow_drift", "bp.magenta.attrib", "bp.subject.visible", "bp.zfight.pixel"):
            self.assertIn(c, px)

    def test_build_manifest_check_cli(self):
        rc = subprocess.run([sys.executable, os.path.join(AD, "build_manifest.py"), "--check"]).returncode
        self.assertEqual(rc, 0)


class QualifyAgainstManifestTests(unittest.TestCase):
    """The manifest entries feed harness.qualify_mutant exactly as the mutant runner will."""

    def setUp(self):
        self.m = {e["id"]: e for e in load()["mutants"]}

    def receipt(self, e, after="b"):
        return {"mutant_id": e["id"], "fault_spec_sha256": e["fault_spec_sha256"], "witness_before": "a", "witness_after": after}

    def report(self, e, failing, passing=()):
        rows = [{"check_id": c, "verdict": "FAIL", "render_index": 0} for c in failing]
        rows += [{"check_id": c, "verdict": "PASS", "render_index": 0} for c in passing]
        return {"verdict": "FAIL" if failing else "PASS", "rc": 1 if failing else 0, "findings": rows}

    def test_killed_when_all_expected_fail(self):
        for e in self.m.values():
            if e["equivalent"]:
                continue
            with self.subTest(e["id"]):
                r = harness.qualify_mutant(e, clean_pass(), self.report(e, e["expected_check_ids"]), self.receipt(e))
                self.assertEqual(r["status"], "KILLED")

    def test_survives_when_one_expected_check_does_not_fail(self):
        e = self.m["bm.scale_1000.08"]
        r = harness.qualify_mutant(e, clean_pass(), self.report(e, e["expected_check_ids"][:1]), self.receipt(e))
        self.assertEqual(r["status"], "SURVIVED")

    def test_missing_declared_judge_check_survives(self):
        # JB3-2: expected_check_ids is the full sd|px|judge union, so SD/PX FAILs alone cannot kill.
        judged = [e for e in self.m.values() if e["expected_judge"] and not e["equivalent"]]
        self.assertEqual(len(judged), 14)
        for e in judged:
            with self.subTest(e["id"]):
                no_judge = [c for c in e["expected_check_ids"] if c not in e["expected_judge"]]
                if not no_judge:
                    continue
                r = harness.qualify_mutant(e, clean_pass(), self.report(e, no_judge), self.receipt(e))
                self.assertEqual(r["status"], "SURVIVED", e["id"])
                full = harness.qualify_mutant(e, clean_pass(), self.report(e, e["expected_check_ids"]), self.receipt(e))
                self.assertEqual(full["status"], "KILLED")

    def test_equivalents_flag_false_positive(self):
        for mid in ("bm.neg_scale.18", "bm.step_hold.22"):
            e = self.m[mid]
            ok = harness.qualify_mutant(e, clean_pass(), self.report(e, [], e["expected_check_ids"]), self.receipt(e))
            missing = harness.qualify_mutant(e, clean_pass(), self.report(e, []), self.receipt(e))
            self.assertEqual(missing["status"], "INVALID", mid)  # JB3-5: absent evidence certifies nothing
            fp = harness.qualify_mutant(e, clean_pass(), self.report(e, e["expected_check_ids"]), self.receipt(e))
            self.assertEqual((ok["status"], fp["status"]), ("EQUIVALENT_OK", "EQUIVALENT_FP"))

    def test_noop_is_invalid(self):
        e = self.m["bm.hide_render.17"]
        r = harness.qualify_mutant(e, clean_pass(), self.report(e, e["expected_check_ids"]), self.receipt(e, after="a"))
        self.assertEqual((r["status"], r["reason"]), ("INVALID", "mutant_noop"))

    def test_tampered_fault_or_empty_expected_is_invalid(self):
        e = json.loads(json.dumps(self.m["bm.bowtie.20"]))
        e["fault"]["parameters"]["x"] = 1
        self.assertEqual(harness.qualify_mutant(e, clean_pass(), self.report(e, []), self.receipt(e))["status"], "INVALID")
        e2 = json.loads(json.dumps(self.m["bm.bowtie.20"]))
        e2["expected_check_ids"] = []
        self.assertEqual(harness.qualify_mutant(e2, clean_pass(), self.report(e2, []), self.receipt(e2))["status"], "INVALID")
        e3 = json.loads(json.dumps(self.m["bm.bowtie.20"]))
        e3["expected_check_ids"] = ["bo.mesh.non_manifold"] * 2
        self.assertEqual(harness.qualify_mutant(e3, clean_pass(), self.report(e3, []), self.receipt(e3))["status"], "INVALID")

    def test_dirty_clean_baseline_is_invalid(self):
        e = self.m["bm.bowtie.20"]
        dirty = {"verdict": "FAIL", "rc": 1, "findings": []}
        self.assertEqual(harness.qualify_mutant(e, dirty, self.report(e, e["expected_check_ids"]), self.receipt(e))["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
