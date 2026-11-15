#!/usr/bin/env python3
"""Digest v2 coverage and stability in REAL Blender 4.0.2 (`blender -b`), no fakes.

Lane sw-agents-rewind (wave w186), claude-fable-5-2, Track P.  Complements test_ckpt.py
(fake bpy).  Why it matters: the store dedups on the digest and the judge rejects an
empty delta (NO_EFFECT), so a state change the digest does not see is either rolled back
as "no effect" or, once committed next to another change, lost by a later rewind.

Run (from product/ref_rewind_ckpt):
  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
      python3 -m unittest test_digest_live -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# One Blender process: build a small scene, then for every mutation reload the saved file,
# digest, mutate, digest, and record which keys changed.
DRIVER = r'''
import bpy, json, sys, os
OUT, BLEND, DIGEST = sys.argv[sys.argv.index("--") + 1:][:3]
ns = {}
exec(compile(open(DIGEST).read(), "digest", "exec"), ns)
dg = ns["digest_v2"]

def upd():
    bpy.context.view_layer.update()

def build():
    for o in list(bpy.data.objects): bpy.data.objects.remove(o)
    sc = bpy.data.scenes[0]
    for name in ("CHAR_mesh", "CHAR_look", "CHAR_cam"):
        c = bpy.data.collections.new(name); sc.collection.children.link(c)
    import bmesh
    me = bpy.data.meshes.new("CHAR_mesh.cube"); bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new("CHAR_mesh.cube", me); bpy.data.collections["CHAR_mesh"].objects.link(ob)
    m = bpy.data.materials.new("CHARMAT_a"); me.materials.append(m)
    ld = bpy.data.lights.new("CHAR_look.key", "AREA"); ld.energy = 900.0
    lo = bpy.data.objects.new("CHAR_look.key", ld); lo.location = (1, -2, 3)
    bpy.data.collections["CHAR_look"].objects.link(lo)
    cd = bpy.data.cameras.new("CHAR_cam.cam"); co = bpy.data.objects.new("CHAR_cam.cam", cd)
    bpy.data.collections["CHAR_cam"].objects.link(co)
    cu = bpy.data.curves.new("CHAR_mesh.curve", "CURVE"); cuo = bpy.data.objects.new("CHAR_mesh.curve", cu)
    bpy.data.collections["CHAR_mesh"].objects.link(cuo)
    upd()

def cube(): return bpy.data.objects["CHAR_mesh.cube"]
def light(): return bpy.data.objects["CHAR_look.key"]
def cam(): return bpy.data.objects["CHAR_cam.cam"]

def m_light_energy(): light().data.energy *= 3
def m_light_color(): light().data.color = (1.0, 0.0, 0.0)
def m_light_size(): light().data.size = 4.0
def m_camera_lens(): cam().data.lens = 85.0
def m_camera_ortho(): cam().data.type = "ORTHO"
def m_delete_face():
    import bmesh
    me = cube().data; bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[0]], context="FACES_ONLY"); bm.to_mesh(me); bm.free()
def m_flip_face():
    import bmesh
    me = cube().data; bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table()
    bm.faces[0].normal_flip(); bm.to_mesh(me); bm.free()
def m_shade_smooth():
    for p in cube().data.polygons: p.use_smooth = not p.use_smooth
def m_custom_prop(): cube()["vp_note"] = 42
def m_hide_set(): cube().hide_set(True)
def m_display_type(): cube().display_type = "WIRE"
def m_curve_bevel(): bpy.data.objects["CHAR_mesh.curve"].data.bevel_depth = 0.25
def m_face_material_index():
    me = cube().data; me.materials.append(bpy.data.materials.new("CHARMAT_b")); me.polygons[0].material_index = 1
# controls that the digest already saw before the coverage fix
def m_move(): cube().location.x += 1.0
def m_vertex(): cube().data.vertices[0].co.x += 0.5
def m_second_scene_object():
    s2 = bpy.data.scenes.new("S2")
    s2.collection.objects.link(bpy.data.objects.new("loose_in_s2", None))

# state a .blend does not keep: must be OUTSIDE the digest, or a correct restore mismatches
def m_orphan_material(): bpy.data.materials.new("unused_material")
def m_orphan_collection(): bpy.data.collections.new("linked_nowhere")
def m_orphan_object(): bpy.data.objects.new("in_no_collection", None)
# controls: the same kinds of datablock, when the scene uses them, are inside the digest
def m_used_material_value(): bpy.data.materials["CHARMAT_a"].roughness = 0.123
def m_linked_collection():
    c = bpy.data.collections.new("CHAR_new"); bpy.data.scenes[0].collection.children.link(c)
def m_fake_user_material(): bpy.data.materials.new("kept_material").use_fake_user = True

MUT = {"orphan_material": m_orphan_material, "orphan_collection": m_orphan_collection,
       "orphan_object": m_orphan_object, "used_material_value": m_used_material_value,
       "linked_collection": m_linked_collection, "fake_user_material": m_fake_user_material,
       "light_energy": m_light_energy, "light_color": m_light_color, "light_size": m_light_size,
       "camera_lens": m_camera_lens, "camera_ortho": m_camera_ortho, "delete_face": m_delete_face,
       "flip_face": m_flip_face, "shade_smooth": m_shade_smooth, "custom_prop": m_custom_prop,
       "hide_set": m_hide_set, "display_type": m_display_type, "curve_bevel": m_curve_bevel,
       "face_material_index": m_face_material_index, "move": m_move, "vertex": m_vertex,
       "second_scene_object": m_second_scene_object}

def reload():
    bpy.ops.wm.open_mainfile(filepath=BLEND, load_ui=False, use_scripts=False); upd()

build()
base = dg(bpy)
bpy.ops.wm.save_as_mainfile(filepath=BLEND, copy=True, check_existing=False)
out = {"base": base, "changed": {}, "roundtrip": {}}
for name in sorted(MUT):
    reload()
    b = dg(bpy)
    out.setdefault("reload_equal_base", True)
    out["reload_equal_base"] = out["reload_equal_base"] and b == base
    out.setdefault("reload_diff", sorted(k for k in set(b) | set(base) if b.get(k) != base.get(k)))
    MUT[name](); upd()
    a = dg(bpy)
    out["changed"][name] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    # the mutated state must itself survive save -> open with an equal digest
    p2 = BLEND + "." + name + ".blend"
    bpy.ops.wm.save_as_mainfile(filepath=p2, copy=True, check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=p2, load_ui=False, use_scripts=False); upd()
    a2 = dg(bpy)
    out["roundtrip"][name] = sorted(k for k in set(a) | set(a2) if a.get(k) != a2.get(k))
json.dump(out, open(OUT, "w"), sort_keys=True)
'''

# mutation -> digest key that MUST change
EXPECT = {
    "light_energy": "col:CHAR_look", "light_color": "col:CHAR_look", "light_size": "col:CHAR_look",
    "camera_lens": "col:CHAR_cam", "camera_ortho": "col:CHAR_cam",
    "delete_face": "col:CHAR_mesh", "flip_face": "col:CHAR_mesh", "shade_smooth": "col:CHAR_mesh",
    "custom_prop": "col:CHAR_mesh", "hide_set": "col:CHAR_mesh", "display_type": "col:CHAR_mesh",
    "curve_bevel": "col:CHAR_mesh", "face_material_index": "col:CHAR_mesh",
    "move": "col:CHAR_mesh", "vertex": "col:CHAR_mesh",
}


def run_blender(tmp, tag, digest_path):
    out = os.path.join(tmp, "out_%s.json" % tag)
    drv = os.path.join(tmp, "driver.py")
    with open(drv, "w") as fh:
        fh.write(DRIVER)
    r = subprocess.run(["blender", "-b", "--factory-startup", "--python", drv, "--",
                        out, os.path.join(tmp, "base_%s.blend" % tag), digest_path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
    if r.returncode != 0 or not os.path.exists(out):
        raise AssertionError("blender failed: " + r.stdout.decode("utf-8", "replace")[-1500:])
    with open(out) as fh:
        return json.load(fh)


@unittest.skipUnless(shutil.which("blender"), "blender not installed")
class DigestLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rw-digest-live-", dir="/dev/shm")
        cls.a = run_blender(cls.tmp, "a", os.path.join(HERE, "digest.py"))
        cls.b = run_blender(cls.tmp, "b", os.path.join(HERE, "digest.py"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_state_class_changes_its_collection_key(self):
        missing = sorted(n for n, key in EXPECT.items() if key not in self.a["changed"][n])
        self.assertEqual(missing, [], "digest blind to: %s" % missing)

    def test_change_stays_in_its_own_collection_key(self):
        """A light change must not also flip the mesh or camera collection (spill would turn
        into a false DELTA_OUTSIDE)."""
        for n, key in EXPECT.items():
            cols = [k for k in self.a["changed"][n] if k.startswith("col:")]
            self.assertEqual(cols, [key], n)

    def test_two_processes_agree(self):
        self.assertEqual(self.a["base"], self.b["base"])
        self.assertEqual(self.a["changed"], self.b["changed"])

    def test_reload_equals_base(self):
        self.assertEqual(self.a["reload_diff"], [])
        self.assertTrue(self.a["reload_equal_base"])

    def test_every_mutated_state_roundtrips(self):
        """Restore correctness for the new coverage: save -> open gives the same digest, for
        every mutated state (a field that does not round-trip would make every restore
        answer RESTORE_MISMATCH)."""
        bad = {n: d for n, d in self.a["roundtrip"].items() if d}
        self.assertEqual(bad, {})

    def test_unsaved_orphans_are_outside_the_digest(self):
        for n in ("orphan_material", "orphan_collection", "orphan_object"):
            self.assertEqual(self.a["changed"][n], [], n)

    def test_control_used_datablocks_are_inside(self):
        self.assertEqual(self.a["changed"]["used_material_value"], ["mat:CHARMAT_a"])
        self.assertEqual(self.a["changed"]["linked_collection"], ["col:CHAR_new", "col:Scene Collection"])
        self.assertEqual(self.a["changed"]["fake_user_material"], ["mat:kept_material"])
        self.assertNotIn("mat:Material", self.a["base"])     # factory material, unused after build()

    def test_second_scene_does_not_hide_the_first(self):
        """Every scene's master collection is named 'Scene Collection': the key must cover
        all of them, not the last one visited."""
        self.assertIn("col:Scene Collection", self.a["changed"]["second_scene_object"])


if __name__ == "__main__":
    unittest.main()
