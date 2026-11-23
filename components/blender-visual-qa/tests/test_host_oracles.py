import copy
import json
import os
import tempfile
import unittest

import numpy as np

from blender_visual_qa.oracles import harness, pixel, scene, vision

SD = "bvqa.oracle-sd/v1"


def row(oid, verdict, obj=None, frame=None, **m):
    return {"schema": SD, "check_id": oid, "oracle_id": oid, "object": obj, "frame": frame,
            "metrics": m or {"x": 0}, "thresholds": {}, "verdict": verdict, "reason": "r"}


def write_scene(path, rows, trailer=None):
    rows = sorted(rows, key=lambda r: (r["oracle_id"], r["object"] or "", -1 if r["frame"] is None else r["frame"]))
    t = trailer or scene.count_summary(rows)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(harness.canonical_json(r) + "\n")
        fh.write(harness.canonical_json({"schema": SD, "summary": t}) + "\n")


class SceneFold(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "scene.jsonl")

    def test_all_pass(self):
        write_scene(self.p, [row("bo.a", "PASS"), row("bo.b", "PASS")])
        rows, s = scene.load_scene(self.p)
        res = scene.evaluate(rows, s)
        self.assertEqual((res["verdict"], res["rc"]), ("PASS", 0))

    def test_fail_wins_over_cannot_see(self):
        write_scene(self.p, [row("bo.a", "FAIL"), row("bo.b", "CANNOT_SEE")])
        rows, s = scene.load_scene(self.p)
        res = scene.evaluate(rows, s)
        self.assertEqual((res["verdict"], res["rc"]), ("FAIL", 1))
        self.assertEqual(scene.failing_ids(res), ["bo.a"])
        self.assertEqual(res["summary"]["cannot_see_count"], 1)

    def test_cannot_see_is_incomplete_not_pass(self):
        write_scene(self.p, [row("bo.a", "CANNOT_SEE")])
        rows, s = scene.load_scene(self.p)
        self.assertEqual(scene.evaluate(rows, s)["rc"], 3)

    def test_strict_cannot_see_fails(self):
        write_scene(self.p, [row("bo.a", "PASS"), row("bo.b", "CANNOT_SEE")])
        rows, s = scene.load_scene(self.p)
        self.assertEqual(scene.evaluate(rows, s, strict_cannot_see=True)["verdict"], "FAIL")

    def test_advisory_downgrade(self):
        write_scene(self.p, [row("bo.poly.budget", "FAIL")])
        rows, s = scene.load_scene(self.p)
        res = scene.evaluate(rows, s, advisory={"bo.poly.budget"})
        self.assertEqual((res["verdict"], res["rc"]), ("ADVISORY", 0))

    def test_required_missing_is_cannot_see(self):
        write_scene(self.p, [row("bo.a", "PASS")])
        rows, s = scene.load_scene(self.p)
        res = scene.evaluate(rows, s, required={"bo.a", "bo.z"})
        self.assertEqual(res["oracles"]["bo.z"], "CANNOT_SEE")
        self.assertEqual(res["rc"], 3)

    def test_empty_is_not_pass(self):
        write_scene(self.p, [])
        rows, s = scene.load_scene(self.p)
        self.assertEqual(scene.evaluate(rows, s)["verdict"], "CANNOT_SEE")

    def test_negative_missing_trailer(self):
        with open(self.p, "w") as fh:
            fh.write(harness.canonical_json(row("bo.a", "PASS")) + "\n")
        with self.assertRaises(scene.SceneError):
            scene.load_scene(self.p)

    def test_negative_lying_trailer(self):
        write_scene(self.p, [row("bo.a", "FAIL")], trailer={"rows": 1, "fail": 0, "cannot_see_count": 0, "advisory": 0})
        rows, s = scene.load_scene(self.p)
        with self.assertRaises(scene.SceneError):
            scene.evaluate(rows, s)

    def test_negative_float_metric_and_bad_schema_and_unsorted(self):
        bad = row("bo.a", "PASS")
        bad["metrics"] = {"x": 1.5}
        write_scene(self.p, [bad])
        with self.assertRaises(scene.SceneError):
            scene.load_scene(self.p)
        with open(self.p, "w") as fh:
            fh.write('{"schema":"bvqa.oracle-sd.v1"}\n')
        with self.assertRaises(scene.SceneError):
            scene.load_scene(self.p)
        with open(self.p, "w") as fh:
            for r in (row("bo.b", "PASS"), row("bo.a", "PASS")):
                fh.write(harness.canonical_json(r) + "\n")
            fh.write(harness.canonical_json({"schema": SD, "summary": scene.count_summary([row("bo.a", "PASS")] * 2)}) + "\n")
        with self.assertRaises(scene.SceneError):
            scene.load_scene(self.p)

    def test_write_result_atomic_and_deterministic(self):
        write_scene(self.p, [row("bo.a", "PASS")])
        rows, s = scene.load_scene(self.p)
        out = os.path.join(self.d, "o.json")
        scene.write_result(scene.evaluate(rows, s), out)
        a = open(out, "rb").read()
        scene.write_result(scene.evaluate(rows, s), out)
        self.assertEqual(a, open(out, "rb").read())
        self.assertFalse(os.path.exists(out + ".tmp"))


class OneWay(unittest.TestCase):
    def test_judge_pass_never_clears_sd_fail(self):
        self.assertEqual(harness.final_verdict(["FAIL"], ["PASS"], ["PASS"]), "FAIL")

    def test_judge_adds_fail(self):
        self.assertEqual(harness.final_verdict(["PASS"], ["PASS"], ["FAIL"]), "FAIL")

    def test_unparsed_judge_is_not_pass(self):
        self.assertEqual(harness.final_verdict(["PASS"], ["PASS"], ["garbage"]), "CANNOT_SEE")

    def test_unknown_verdict_raises(self):
        with self.assertRaises(harness.OracleError):
            harness.max_precedence(["PASS", "MAYBE"])


def entry(**kw):
    fault = {"operation": "flip_normals", "selector": "Cube", "parameters": {}}
    e = {"id": "bm.flip_normals.01", "fault": fault, "fault_spec_sha256": harness.sha256_canonical(fault),
         "interval": {"start_render_index": 0, "end_render_index": 3},
         "expected_check_ids": ["bo.mesh.inverted_normals"], "equivalent": False}
    e.update(kw)
    return e


def receipt(e):
    return {"mutant_id": e["id"], "fault_spec_sha256": e["fault_spec_sha256"], "witness_before": {"a": 1}, "witness_after": {"a": 2}}


CLEAN = {"verdict": "PASS", "rc": 0, "findings": []}


def mut(verdict="FAIL", check="bo.mesh.inverted_normals"):
    return {"verdict": "FAIL", "rc": 1, "findings": [{"check_id": check, "verdict": verdict, "render_index": None}]}


class Qualify(unittest.TestCase):
    def test_killed(self):
        e = entry()
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut(), receipt(e))["status"], "KILLED")

    def test_survived_when_check_passes(self):
        e = entry()
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut("PASS"), receipt(e))["status"], "SURVIVED")

    def test_survived_when_other_check_fails(self):
        e = entry()
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut("FAIL", "bo.mat.missing"), receipt(e))["status"], "SURVIVED")

    def test_invalid_variants(self):
        e = entry()
        bad_sha = entry(fault_spec_sha256="0" * 64)
        self.assertEqual(harness.qualify_mutant(bad_sha, CLEAN, mut(), receipt(e))["status"], "INVALID")
        self.assertEqual(harness.qualify_mutant(entry(expected_check_ids=[]), CLEAN, mut(), receipt(e))["status"], "INVALID")
        self.assertEqual(harness.qualify_mutant(entry(expected_check_ids=["a", "a"]), CLEAN, mut(), receipt(e))["status"], "INVALID")
        self.assertEqual(harness.qualify_mutant(entry(id="dm.x.01"), CLEAN, mut(), receipt(e))["status"], "INVALID")
        self.assertEqual(harness.qualify_mutant(e, {"verdict": "FAIL", "rc": 1}, mut(), receipt(e))["reason"], "clean_baseline_not_passing")
        r = receipt(e)
        r["witness_after"] = r["witness_before"]
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut(), r)["reason"], "mutant_noop")
        r = receipt(e)
        r["mutant_id"] = "bm.other.02"
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut(), r)["status"], "INVALID")
        self.assertEqual(harness.qualify_mutant(e, CLEAN, {"verdict": "ERROR", "rc": 2, "findings": []}, receipt(e))["reason"], "mutant_run_error")

    def test_equivalent_inverts(self):
        e = entry(id="bm.neg_scale.18", equivalent=True)
        e["fault_spec_sha256"] = harness.sha256_canonical(e["fault"])
        ok = {"verdict": "PASS", "rc": 0, "findings": [
            {"check_id": "bo.mesh.inverted_normals", "verdict": "PASS", "render_index": None}]}
        self.assertEqual(harness.qualify_mutant(e, CLEAN, ok, receipt(e))["status"], "EQUIVALENT_OK")
        self.assertEqual(harness.qualify_mutant(e, CLEAN, mut(), receipt(e))["status"], "EQUIVALENT_FP")

    def test_equivalent_missing_evidence_is_not_certified(self):
        e = entry(id="bm.neg_scale.18", equivalent=True)
        e["fault_spec_sha256"] = harness.sha256_canonical(e["fault"])
        absent = {"verdict": "PASS", "rc": 0, "findings": []}
        r = harness.qualify_mutant(e, CLEAN, absent, receipt(e))
        self.assertEqual((r["status"], r["reason"]), ("INVALID", "equivalent_evidence_missing"))
        blind = {"verdict": "INCOMPLETE", "rc": 3, "findings": [
            {"check_id": "bo.mesh.inverted_normals", "verdict": "CANNOT_SEE", "render_index": None}]}
        r = harness.qualify_mutant(e, CLEAN, blind, receipt(e))
        self.assertEqual((r["status"], r["reason"]), ("INVALID", "equivalent_evidence_incomplete"))
        outside = {"verdict": "PASS", "rc": 0, "findings": [
            {"check_id": "bo.mesh.inverted_normals", "verdict": "PASS", "render_index": 99}]}
        self.assertEqual(harness.qualify_mutant(e, CLEAN, outside, receipt(e))["reason"], "equivalent_evidence_missing")

    def test_interval_filters_rows(self):
        e = entry()
        m = {"verdict": "FAIL", "rc": 1, "findings": [
            {"check_id": "bo.mesh.inverted_normals", "verdict": "FAIL", "render_index": 1},
            {"check_id": "bo.mesh.inverted_normals", "verdict": "PASS", "render_index": 2}]}
        self.assertEqual(harness.qualify_mutant(e, CLEAN, m, receipt(e))["status"], "SURVIVED")
        m["findings"][1]["render_index"] = 9   # outside the interval
        self.assertEqual(harness.qualify_mutant(e, CLEAN, m, receipt(e))["status"], "KILLED")


def img(h=10, w=10, c=(10, 20, 30)):
    a = np.zeros((h, w, 3), np.uint8)
    a[:] = c
    return a


VT = dict(pixel.STANDARD_VT)


class Pixel(unittest.TestCase):
    def test_missing_or_partial_colour_metadata_cannot_see(self):
        a = img(c=(255, 0, 255))
        ma = np.ones((10, 10), np.uint16)
        for bad in (None, {}, {"view_transform": "Standard"}, dict(VT, look="Filmic")):
            self.assertEqual(pixel.frame_magenta(a, 1, 100, bad)[0], "CANNOT_SEE")
            self.assertEqual(pixel.magenta_attrib(a, ma, ["M"], bad)[0], "CANNOT_SEE")
        self.assertEqual(pixel.frame_magenta(a, 1, 100, VT)[0], "FAIL")
        self.assertEqual(pixel.magenta_attrib(a, ma, ["M"], VT)[0], "FAIL")

    def test_magenta_pos_neg_and_equality(self):
        a = img()
        self.assertEqual(pixel.frame_magenta(a, 1, 100, VT)[0], "PASS")
        a[:1, :] = (255, 0, 255)          # 10 of 100 pixels
        self.assertEqual(pixel.frame_magenta(a, 1, 10, VT)[0], "PASS")   # equality passes
        self.assertEqual(pixel.frame_magenta(a, 1, 11, VT)[0], "FAIL")

    def test_magenta_view_transform(self):
        a = img(c=(255, 0, 255))
        agx = dict(pixel.STANDARD_VT, view_transform="AgX")
        self.assertEqual(pixel.frame_magenta(a, 1, 100, agx)[0], "CANNOT_SEE")
        self.assertEqual(pixel.frame_magenta(a, 1, 100, dict(pixel.STANDARD_VT))[0], "FAIL")

    def test_magenta_rejects_bad_dtype(self):
        with self.assertRaises(ValueError):
            pixel.frame_magenta(np.zeros((2, 2, 3), np.float32), 1, 2, VT)

    def test_black_uniform_frozen(self):
        self.assertEqual(pixel.frame_black(img(c=(0, 0, 0)), 4, 1, 2)[0], "FAIL")
        self.assertEqual(pixel.frame_black(img(), 4, 1, 2)[0], "PASS")
        self.assertEqual(pixel.frame_uniform(img(), 2)[0], "FAIL")
        a = img()
        a[0, 0] = (200, 200, 200)
        self.assertEqual(pixel.frame_uniform(a, 2)[0], "PASS")
        self.assertEqual(pixel.frame_frozen([img(), img()])[0], "FAIL")
        self.assertEqual(pixel.frame_frozen([img(), a])[0], "PASS")
        self.assertEqual(pixel.frame_frozen([img()])[0], "CANNOT_SEE")

    def test_attrib_names_exact_material(self):
        a = img()
        ma = np.zeros((10, 10), np.uint16)
        a[2:4, 2:4] = (255, 0, 255)
        ma[2:4, 2:4] = 2
        ma[8:, 8:] = 1                       # other material, not magenta
        v, _r, m, names = pixel.magenta_attrib(a, ma, ["MatA", "MatB"], VT)
        self.assertEqual((v, names, m["attributed"]), ("FAIL", ["MatB"], 4))
        a2 = img()
        self.assertEqual(pixel.magenta_attrib(a2, ma, ["MatA", "MatB"], VT)[0], "PASS")
        agx = dict(pixel.STANDARD_VT, view_transform="AgX")
        self.assertEqual(pixel.magenta_attrib(a, ma, ["MatA", "MatB"], agx)[0], "CANNOT_SEE")
        ma[2, 2] = 7
        self.assertEqual(pixel.magenta_attrib(a, ma, ["MatA", "MatB"], VT)[0], "CANNOT_SEE")

    def test_subject_visible(self):
        ob = np.zeros((100, 100), np.uint16)
        ob[:10, :] = 3                      # 100000 ppm
        self.assertEqual(pixel.subject_visible([ob], 3, 2000)[0], "PASS")
        self.assertEqual(pixel.subject_visible([ob, np.zeros_like(ob)], 3, 2000)[0], "FAIL")
        self.assertEqual(pixel.subject_visible([], 3, 2000)[0], "CANNOT_SEE")

    def test_zfight_pixel(self):
        a = np.ones((10, 10), np.uint16)
        b = a.copy()
        self.assertEqual(pixel.zfight_pixel(a, b, [(0, 0, 10, 10)], True)[0], "PASS")
        b[0, 0] = 2
        self.assertEqual(pixel.zfight_pixel(a, b, [(0, 0, 10, 10)], True)[0], "FAIL")
        self.assertEqual(pixel.zfight_pixel(a, b, [(0, 0, 10, 10)], False)[0], "ADVISORY")
        self.assertEqual(pixel.zfight_pixel(a, b, [], True)[0], "CANNOT_SEE")


def vis(regions):
    return {"schema": "vqa.vision/v1", "frames": [], "regions": regions}


def reg(cls, frame, n=1):
    return {"id": cls + str(frame), "cls": cls, "frame": frame, "n_tiles": n}


class Vision(unittest.TestCase):
    def test_flicker_threshold(self):
        v = vis([reg("FLICKER", 1), reg("FLICKER", 3)])
        self.assertEqual(vision.vc_flicker(v)[0], "PASS")
        v = vis([reg("FLICKER", f) for f in (1, 3, 5)])
        self.assertEqual(vision.vc_flicker(v)[0], "FAIL")

    def test_frozen_needs_consecutive(self):
        self.assertEqual(vision.vc_frozen_subregion(vis([reg("FROZEN", f) for f in range(0, 16, 2)]))[0], "PASS")
        self.assertEqual(vision.vc_frozen_subregion(vis([reg("FROZEN", f) for f in range(8)]))[0], "FAIL")

    def test_profile_threshold_and_missing_vision(self):
        v = vis([reg("DRIFT", 0), reg("DRIFT", 1)])
        self.assertEqual(vision.vc_slow_drift(v, thresholds={"DRIFT": 3})[0], "PASS")
        self.assertEqual(vision.vc_slow_drift(v)[0], "FAIL")
        self.assertEqual(vision.vc_slow_drift(None)[0], "CANNOT_SEE")

    def test_shadow_is_advisory_on_is_required(self):
        v = vis([reg("DRIFT", 0)])
        self.assertEqual(vision.effective_verdict(vision.vc_slow_drift(v, mode="shadow")), "ADVISORY")
        self.assertEqual(vision.effective_verdict(vision.vc_slow_drift(v, mode="on")), "FAIL")

    def test_negative_params_and_mode(self):
        with self.assertRaises(vision.VisionError):
            vision.vc_flicker(vis([]), {"min_frames": 0})
        with self.assertRaises(vision.VisionError):
            vision.vc_flicker(vis([]), mode="loud")
        with self.assertRaises(vision.VisionError):
            vision.evaluate_vision(".", "vc_nope")

    def test_load_vision_wrong_schema(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "vision"))
        with open(os.path.join(d, "vision", "vision.json"), "w") as fh:
            json.dump({"schema": "x", "frames": [], "regions": []}, fh)
        self.assertIsNone(vision.load_vision(d))
        self.assertEqual(vision.evaluate_vision(d, "vc_flicker")[0], "CANNOT_SEE")


if __name__ == "__main__":
    unittest.main()
