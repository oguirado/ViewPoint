import json
import unittest

from capsule.astcheck.tests.test_astcheck import run

ZERO = [
    "bpy.ops.wm.save_as_mainfile()",
    "bpy.ops.wm.save_mainfile()",
    "bpy.ops.export_scene.fbx()",
    "bpy.ops.export_scene.gltf()",
    "bpy.ops.export_scene.obj()",
    "bpy.ops.wm.alembic_export()",
    "bpy.ops.wm.usd_export()",
    "bpy.ops.wm.collada_export()",
    "bpy.ops.wm.obj_export()",
    "bpy.ops.wm.ply_export()",
    "bpy.ops.wm.stl_export()",
    "bpy.data.images[0].pack()",
    "bpy.data.images[0].unpack()",
]


class Fx1PathCallsTest(unittest.TestCase):
    def deny(self, src, token):
        rc, out = run(src)
        self.assertEqual(rc, 5, (src, out))
        v = json.loads(out)
        self.assertEqual((v["verdict"], v["code"]), ("deny", "ast_denied"))
        self.assertTrue(v["reason"].startswith(token), (src, v["reason"]))

    def test_adw3_zero_arg_new_path_calls_denied(self):
        for call in ZERO:
            self.deny("import bpy\n" + call + "\n", "path_call_without_path")

    def test_adw3_zero_arg_unpack_denied(self):
        self.deny("import bpy\nbpy.data.images[0].unpack()\n", "path_call_without_path")

    def test_adw3_stored_new_path_calls_denied(self):
        self.deny("import bpy\nf = bpy.data.images[0].unpack\n", "stored_callable")
        self.deny("import bpy, mathutils\nv = mathutils.Vector((1, 2, 3))\nv.x = bpy.data.images[0].unpack\n",
                  "stored_callable")
        self.deny("import bpy\nf = bpy.ops.export_scene.fbx\n", "stored_callable")

    def test_adw3_advocate_repro_denied(self):
        src = ("import bpy, mathutils\nv = mathutils.Vector((1, 2, 3))\nv.x = bpy.data.images[0].unpack\n"
               "bpy.ops.wm.save_as_mainfile()\nbpy.ops.export_scene.fbx()\n")
        rc, out = run(src)
        self.assertEqual(rc, 5, out)

    def test_adw3_literal_path_outside_outdir_denied(self):
        self.deny("import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/etc/x.blend')\n", "filepath must be a literal")
        self.deny("import bpy\nbpy.ops.export_scene.fbx(filepath='/tmp/x.fbx')\n", "filepath must be a literal")

    def test_adw3_wm_user_prefs_and_exporters_denied(self):
        for op in ("save_userpref", "save_homefile"):
            self.deny("import bpy\nbpy.ops.wm.%s()\n" % op, "bpy.ops.wm." + op)

    def test_adw3_in_outdir_path_still_allowed(self):
        for src in ("import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/a.blend')\n",
                    "import bpy\nbpy.ops.export_scene.fbx(filepath='/scratch/out/a.fbx')\n",
                    "import bpy\nbpy.data.images[0].save(filepath='/scratch/out/a.png')\n"):
            rc, out = run(src)
            self.assertEqual(rc, 0, (src, out))


if __name__ == "__main__":
    unittest.main()
