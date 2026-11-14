"""W2 INT: reason-specific checks of the ADW3-01 rules, so each rule is killed by its own mutant (the lane K corpus
shadows rules with each other: module-attribute assignment and stored_pass both deny `math.f = bpy.data.images.load`).
Run: python3 -B -m unittest scripts.cu.tests.test_ast_w2 (from the repo root)."""
import json
import unittest

from capsule.astcheck.tests.test_astcheck import run


class AstW2(unittest.TestCase):
    def reason(self, src):
        rc, out = run(src)
        self.assertEqual(rc, 5, (src, out))
        return json.loads(out)["reason"]

    def test_stored_path_callable_to_plain_name(self):
        self.assertEqual(self.reason("import bpy\nx = bpy.data.images.load\n"), "stored_callable load")

    def test_stored_bpy_ops_to_plain_name(self):
        self.assertEqual(self.reason("import bpy\nx = bpy.ops.mesh.primitive_cube_add\n"), "stored_callable bpy.ops")

    def test_stored_path_op_denied_with_path_call_reason(self):
        # FK: save_as_mainfile is a path call, so its stored form is denied by the path-call reason (denial still asserted)
        self.assertEqual(self.reason("import bpy\nx = bpy.ops.wm.save_as_mainfile\n"), "stored_callable save_as_mainfile")

    def test_module_attribute_assignment_of_plain_value(self):
        self.assertEqual(self.reason("import bpy,math\nmath.x = 5\n"), "stored_callable module attribute assignment")

    def test_filepath_raw_assignment_outside_scratch_denied(self):
        self.reason("import bpy\nbpy.data.images[0].filepath_raw = '/etc/passwd'\n")

    def test_filepath_raw_assignment_inside_scratch_allowed(self):
        rc, out = run("import bpy\nbpy.data.images[0].filepath_raw = '/scratch/out/a.png'\n")
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
