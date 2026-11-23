"""In-Blender oracle tests: scene_checks.py + apply_mutant.py on tiny scratch blends (skips without /usr/bin/blender).

Serial, taskset -c 15-19, threads 2, scratch under build/room_O1_tests. No render is made; only scene inspection.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLENDER = "/usr/bin/blender"
SCRATCH = os.path.join(ROOT, "build", "room_O1_tests")
THR = os.path.join(ROOT, "catalogue", "thresholds.tsv")
MANIFEST = os.path.join(ROOT, "adapters", "example_scene", "mutant-manifest.json")
JOURNAL = {"schema": "bvqa.journal/v1", "scene": "Scene", "camera": "Cam", "view_layer": "ViewLayer",
           "frames": {"list": [1, 2, 3, 4]}, "animated": [], "fps": [24, 1], "res": [160, 90], "seed": 1,
           "seed_mode": "fixed", "ops": []}


def blender(args, timeout=150):
    env = dict(os.environ, HOME=SCRATCH + "/xdg", XDG_CONFIG_HOME=SCRATCH + "/xdg", XDG_DATA_HOME=SCRATCH + "/xdg",
               XDG_CACHE_HOME=SCRATCH + "/xdg", TZ="UTC", LC_ALL="C", PYTHONNOUSERSITE="1")
    cmd = ["taskset", "-c", "15-19", BLENDER, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9", "-t", "2"] + args
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    last = [l for l in p.stdout.splitlines() if l.startswith("{")][-1:]
    return p.returncode, json.loads(last[0]) if last else {}


def rows(path):
    with open(path) as f:
        return [json.loads(l) for l in f]


@unittest.skipUnless(os.path.exists(BLENDER), "needs /usr/bin/blender")
class BlenderOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        os.makedirs(SCRATCH + "/xdg")
        cls.base = SCRATCH + "/base.blend"
        rc, _ = blender(["--python", os.path.join(ROOT, "adapters", "example_scene", "make_base.py"), "--", "--out", cls.base])
        assert rc == 0, "make_base failed"
        cls.journal, cls.manifest = SCRATCH + "/journal.json", SCRATCH + "/manifest.json"
        with open(cls.journal, "w") as f:
            json.dump(JOURNAL, f)
        with open(cls.manifest, "w") as f:
            json.dump({"schema": "bvqa.scene/v1", "budgets": {"tris_object": 200000, "tris_scene": 1000000}}, f)
        cls.clean = cls.check(cls.base, "clean.jsonl")
        with open(MANIFEST) as f:
            cls.entries = {e["id"]: e for e in json.load(f)["mutants"]}

    @classmethod
    def check(cls, blend, name, baseline=None):
        out = os.path.join(SCRATCH, name)
        args = [blend, "--python", os.path.join(ROOT, "bpy_side", "scene_checks.py"), "--", "--manifest", cls.manifest,
                "--journal", cls.journal, "--thresholds", THR, "--out", out]
        if baseline:
            args += ["--baseline", baseline]
        rc, res = blender(args)
        assert rc == 0, res
        return out

    def apply(self, mid, fault=None, name=None):
        d = os.path.join(SCRATCH, name or mid)
        os.makedirs(d, exist_ok=True)
        fault = fault or self.entries[mid]["fault"]
        with open(d + "/spec.json", "w") as f:
            json.dump({"id": mid, "fault": fault}, f)
        rc, res = blender([self.base, "--python", os.path.join(ROOT, "bpy_side", "apply_mutant.py"), "--", "--spec", d + "/spec.json",
                           "--out", d + "/mut.blend", "--receipt", d + "/receipt.json"])
        return d, rc, res

    def test_clean_base_zero_fail_zero_cannot_see(self):
        r = rows(self.clean)
        self.assertEqual(r[-1]["summary"]["fail"], 0, [x for x in r[:-1] if x["verdict"] == "FAIL"])
        self.assertEqual(r[-1]["summary"]["cannot_see_count"], 0)
        oracles = {x["oracle_id"] for x in r[:-1]}
        with open(os.path.join(ROOT, "adapters", "example_scene", "registry.json")) as f:
            self.assertEqual(oracles, set(json.load(f)["sd"]))
        for x in r[:-1]:
            self.assertTrue(all(type(v) is int for v in x["metrics"].values()), x)

    def test_determinism_byte_identical(self):
        again = self.check(self.base, "clean2.jsonl")
        with open(again, "rb") as a, open(self.clean, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_every_scene_mutant_killed_and_equivalent_clean(self):
        for mid, e in sorted(self.entries.items()):
            if e["kind"] != "scene":
                continue
            d, rc, res = self.apply(mid)
            self.assertEqual(rc, 0, (mid, res))
            r = rows(self.check(d + "/mut.blend", mid + ".jsonl", self.clean))
            failed = {x["oracle_id"] for x in r[:-1] if x["verdict"] == "FAIL"}
            if e["equivalent"]:
                self.assertEqual(failed, set(), mid)
            else:
                self.assertTrue(set(e["expected_sd"]) <= failed, (mid, failed, e["expected_sd"]))
            with open(d + "/receipt.json") as f:
                receipt = json.load(f)
            self.assertEqual(receipt["fault_spec_sha256"], e["fault_spec_sha256"])
            self.assertNotEqual(receipt["witness_before"], receipt["witness_after"])
            self.assertNotEqual(receipt["base_blend_sha256"], receipt["mutant_blend_sha256"])

    def test_step_hold_op_applies_keys_a_b_b_c(self):  # FO3 request: journal step_hold fixture
        d, rc, res = self.apply("bm.step_hold.22x", {"operation": "step_hold", "selector": {"object": "Static"}, "parameters": {}})
        self.assertEqual(rc, 0, res)
        script = d + "/chk.py"
        with open(script, "w") as f:
            f.write("import bpy, json\nm = bpy.data.objects['Static'].material_slots[0].material\n"
                    "s = bpy.context.scene\nout = []\nfor fr in (1, 2, 3, 4):\n    s.frame_set(fr)\n"
                    "    out.append(m.node_tree.nodes['Principled BSDF'].inputs['Emission Strength'].default_value)\n"
                    "print(json.dumps({'code': 'X', 'v': out}))\n")
        rc, res = blender([d + "/mut.blend", "--python", script])
        self.assertEqual((rc, res["v"]), (0, [0.0, 5.0, 5.0, 10.0]))
        with open(d + "/receipt.json") as f:
            r = json.load(f)
        self.assertNotEqual(r["witness_before"], r["witness_after"])

    def test_noop_mutant_rc5_no_output(self):
        fault = {"operation": "flip_normals", "selector": {"object": "Subject"}, "parameters": {"permille": 0}}
        d, rc, res = self.apply("bm.noop.99", fault)
        self.assertEqual((rc, res.get("code"), res.get("reason")), (5, "E_GATE", "mutant_noop"))
        self.assertFalse(os.path.exists(d + "/mut.blend"))
        self.assertFalse(os.path.exists(d + "/receipt.json"))

    def test_bad_operation_and_missing_selector_rc2(self):
        d, rc, res = self.apply("bm.bad.98", {"operation": "nope", "selector": {}, "parameters": {}})
        self.assertEqual((rc, res.get("code")), (2, "E_SCHEMA"))
        d, rc, res = self.apply("bm.bad.97", {"operation": "hide_render", "selector": {"object": "Missing"}, "parameters": {}})
        self.assertEqual((rc, res.get("code")), (2, "E_SCHEMA"))
        self.assertFalse(os.path.exists(d + "/mut.blend"))

    def test_untagged_object_is_cannot_see_and_counted(self):
        d = os.path.join(SCRATCH, "untag")
        os.makedirs(d, exist_ok=True)
        script = d + "/untag.py"
        with open(script, "w") as f:
            f.write("import bpy\nbpy.data.objects['Static']['bvqa_tags']=''\n"
                    "bpy.ops.wm.save_as_mainfile(filepath=%r, copy=True)\n" % (d + "/u.blend"))
        rc, _ = blender([self.base, "--python", script])
        self.assertEqual(rc, 0)
        r = rows(self.check(d + "/u.blend", "untag.jsonl"))
        cs = [x for x in r[:-1] if x["verdict"] == "CANNOT_SEE"]
        self.assertTrue(cs and all(x["object"] == "Static" and x["reason"] == "untagged" for x in cs))
        self.assertEqual(r[-1]["summary"]["cannot_see_count"], len(cs))

    def test_open_plane_passes_closed_plane_fails_non_manifold(self):
        d = os.path.join(SCRATCH, "tagplane")
        os.makedirs(d, exist_ok=True)
        script = d + "/t.py"
        with open(script, "w") as f:
            f.write("import bpy\nbpy.data.objects['Textured']['bvqa_tags']='closed'\n"
                    "bpy.ops.wm.save_as_mainfile(filepath=%r, copy=True)\n" % (d + "/c.blend"))
        self.assertEqual(blender([self.base, "--python", script])[0], 0)
        base = {(x["oracle_id"], x["object"]): x for x in rows(self.clean)[:-1]}
        self.assertEqual(base[("bo.mesh.non_manifold", "Textured")]["verdict"], "PASS")
        closed = {(x["oracle_id"], x["object"]): x for x in rows(self.check(d + "/c.blend", "tagplane.jsonl"))[:-1]}
        self.assertEqual(closed[("bo.mesh.non_manifold", "Textured")]["verdict"], "FAIL")

    # ------------------------------------------------------------------ JB3-3/4/7 fixtures
    def variant(self, name, code):
        """Save a scratch blend = base + python `code` (runs in Blender); returns its path."""
        d = os.path.join(SCRATCH, name)
        os.makedirs(d, exist_ok=True)
        with open(d + "/v.py", "w") as f:
            f.write("import bpy, bmesh\n" + code + "\nbpy.ops.wm.save_as_mainfile(filepath=%r, copy=True)\n" % (d + "/v.blend"))
        self.assertEqual(blender([self.base, "--python", d + "/v.py"])[0], 0, name)
        return d + "/v.blend"

    def verdicts(self, blend, name, oracle, obj=None, check=None):
        r = rows(check(blend, name) if check else self.check(blend, name))
        return [x for x in r[:-1] if x["oracle_id"] == oracle and (obj is None or x["object"] == obj)]

    def test_no_material_slots_fails_and_assigned_control_passes(self):  # JB3-3
        blend = self.variant("noslots", "bpy.data.objects['Static'].data.materials.clear()")
        got = self.verdicts(blend, "noslots.jsonl", "bo.mat.missing")
        by = {x["object"]: x for x in got}
        self.assertEqual((by["Static"]["verdict"], by["Static"]["metrics"]["no_slots"]), ("FAIL", 1))
        self.assertEqual((by["Subject"]["verdict"], by["Subject"]["metrics"]["no_slots"]), ("PASS", 0))

    def test_partial_clipping_advisory_complete_clipping_fails(self):  # JB3-4
        part = self.variant("clip_part", "bpy.data.objects['Cam'].data.clip_end = 9.0")
        got = [x for x in self.verdicts(part, "clip_part.jsonl", "bo.cam.clipping", "Subject")]
        self.assertTrue(got and all(x["verdict"] == "ADVISORY" and 1 <= x["metrics"]["clipped_corners"] <= 7 for x in got), got)
        full = self.variant("clip_full", "bpy.data.objects['Cam'].data.clip_end = 0.1")
        got = self.verdicts(full, "clip_full.jsonl", "bo.cam.clipping", "Subject")
        self.assertTrue(got and all(x["verdict"] == "FAIL" and x["metrics"]["clipped_corners"] == 8 for x in got), got)

    GROUP_IMG = (
        "g = bpy.data.node_groups.new('G', 'ShaderNodeTree')\n"
        "tex = g.nodes.new('ShaderNodeTexImage')\n"
        "img = bpy.data.images.new('gimg', 4, 4); img.source = 'FILE'; img.filepath = '//__missing__/g.png'\n"
        "tex.image = img\n"
        "m = bpy.data.materials.new('GM'); m.use_nodes = True\n"
        "gn = m.node_tree.nodes.new('ShaderNodeGroup'); gn.node_tree = g\n"
        "bpy.data.objects['Static'].data.materials.clear(); bpy.data.objects['Static'].data.materials.append(m)\n")

    def test_missing_image_inside_node_group_fails(self):  # JB3-7 cm.no_group
        blend = self.variant("groupimg", self.GROUP_IMG)
        by = {x["object"]: x for x in self.verdicts(blend, "groupimg.jsonl", "bo.tex.missing")}
        self.assertEqual((by["Static"]["verdict"], by["Static"]["metrics"]["missing_images"]), ("FAIL", 1))
        self.assertEqual(by["Textured"]["verdict"], "PASS")  # control: existing file

    REVERSED_DUP = (
        "src = bpy.data.objects['Textured']\n"
        "ob = src.copy(); ob.data = src.data.copy(); ob.name = 'DupRev'; ob['bvqa_tags'] = 'open'\n"
        "bpy.context.scene.collection.objects.link(ob)\n"
        "bm = bmesh.new(); bm.from_mesh(ob.data); bmesh.ops.reverse_faces(bm, faces=bm.faces[:]); bm.to_mesh(ob.data); bm.free()\n")

    def test_reversed_coplanar_duplicate_fails_zfight(self):  # JB3-7 cm.abs_cos
        blend = self.variant("revdup", self.REVERSED_DUP)
        got = self.verdicts(blend, "revdup.jsonl", "bo.geom.zfight")
        self.assertTrue(any(x["verdict"] == "FAIL" and "DupRev" in (x["object"] or "") for x in got), got)
        clean = [x for x in rows(self.clean)[:-1] if x["oracle_id"] == "bo.geom.zfight"]
        self.assertTrue(clean and all(x["verdict"] == "PASS" for x in clean))

    def test_missing_threshold_row_is_rc2_not_default(self):  # JB3-8 end to end
        thr = os.path.join(SCRATCH, "thr_missing.tsv")
        with open(THR) as f:
            keep = [l for l in f if not l.startswith("BO_ZF_COS_PPM\t")]
        with open(thr, "w") as f:
            f.writelines(keep)
        out = os.path.join(SCRATCH, "thr_missing.jsonl")
        rc, res = blender([self.base, "--python", os.path.join(ROOT, "bpy_side", "scene_checks.py"), "--", "--manifest", self.manifest,
                           "--journal", self.journal, "--thresholds", thr, "--out", out])
        self.assertEqual((rc, res.get("code")), (2, "E_SCHEMA"), res)
        self.assertIn("BO_ZF_COS_PPM", res.get("reason", ""))
        self.assertFalse(os.path.exists(out))


class ReviewerMutantBlenderTests(unittest.TestCase):
    """JB3-7: source mutants of scene_checks.py run through the real Blender fixtures must fail the named assertion."""

    MUTANTS = {
        "cm.no_group": ("test_missing_image_inside_node_group_fails", "            yield from tree_nodes(bpy, n.node_tree, seen)", "            pass"),
        "cm.abs_cos": ("test_reversed_coplanar_duplicate_fails_zfight", "abs(_dot(na, nb)) * PPM_ONE", "_dot(na, nb) * PPM_ONE"),
    }

    @unittest.skipUnless(os.path.exists(BLENDER), "needs /usr/bin/blender")
    def test_each_mutant_fails_its_named_fixture_by_assertion(self):
        src_path = os.path.join(ROOT, "bpy_side", "scene_checks.py")
        with open(src_path) as f:
            src = f.read()
        for mid, (test, old, new) in sorted(self.MUTANTS.items()):
            self.assertEqual(src.count(old), 1, mid)  # a setup error is never a kill
            mroot = os.path.join(ROOT, "build", "room_FO1_mut_" + mid.replace(".", "_"))
            shutil.rmtree(mroot, ignore_errors=True)
            shutil.copytree(os.path.join(ROOT, "bpy_side"), mroot + "/bpy_side", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(os.path.join(ROOT, "adapters"), mroot + "/adapters", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(os.path.join(ROOT, "catalogue"), mroot + "/catalogue", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(os.path.join(ROOT, "tests"), mroot + "/tests", ignore=shutil.ignore_patterns("__pycache__"))
            with open(mroot + "/bpy_side/scene_checks.py", "w") as f:
                f.write(src.replace(old, new))
            p = subprocess.run(["python3", "-m", "unittest", "test_bpy_oracles_blender.BlenderOracleTests." + test],
                               cwd=mroot + "/tests", capture_output=True, text=True, timeout=400, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.assertEqual(p.returncode, 1, (mid, p.stderr[-800:]))
            self.assertIn("AssertionError", p.stderr, mid)
            self.assertIn("FAIL: " + test, p.stderr, mid)  # an assertion failure of the fixture, not an ERROR/setUpClass
            shutil.rmtree(mroot, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
