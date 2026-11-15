import glob
import json
import os
import re
import unittest

from capsule.astcheck.tests.test_astcheck import CHECK, CORPUS, rd, run


def adw3(name):
    return rd(os.path.join(CORPUS, name))


class Adw3Test(unittest.TestCase):
    def deny(self, src, token):
        rc, out = run(src)
        self.assertEqual(rc, 5, (src, out))
        v = json.loads(out)
        self.assertEqual((v["verdict"], v["code"]), ("deny", "ast_denied"))
        self.assertTrue(v["reason"].startswith(token), v["reason"])

    def test_adw3_attr_stored_images_load_denied(self):
        self.deny(adw3("adw3_1_stored_images_load.py"), "stored_callable")

    def test_adw3_attr_stored_save_as_mainfile_denied(self):
        self.deny(adw3("adw3_2_stored_save_as_mainfile.py"), "stored_callable")
        self.deny(adw3("adw3_5_list_stored.py"), "stored_callable")
        self.deny(adw3("adw3_6_cosign_printf.py"), "stored_callable")

    def test_adw3_zero_arg_save_with_filepath_raw_denied(self):
        self.deny(adw3("adw3_3_zero_arg_save_filepath_raw.py"), "path_call_without_path")
        self.deny("import bpy\nbpy.data.images[0].save()\n", "path_call_without_path")

    def test_adw3_filepath_raw_string_denied(self):
        self.deny(adw3("adw3_4_filepath_raw_string.py"), "string filepath_raw")
        self.deny("import bpy\nbpy.data.images[0].filepath_raw = '/etc/x'\n", "filepath_raw must be a literal path")

    def test_adw3_benign_corpus_still_allowed(self):
        files = sorted(glob.glob(os.path.join(CORPUS, "allow", "*.py")))
        self.assertEqual(len(files), 6)
        for f in files:
            rc, out = run(rd(f))
            self.assertEqual(rc, 0, (f, out))
        rc, out = run("import bpy\nimg = bpy.data.images[0]\nimg.filepath_raw = '/scratch/out/a.png'\n"
                      "img.save(filepath='/scratch/out/a.png')\nbpy.context.scene.camera = bpy.data.objects['Cam']\n"
                      "bpy.context.scene.frame_end = 10\n")
        self.assertEqual(rc, 0, out)

    def test_adw3_reason_codes_stable(self):
        seen = set()
        for name in sorted(glob.glob(os.path.join(CORPUS, "adw3_*.py"))):
            rc, out = run(rd(name))
            self.assertEqual(rc, 5, (name, out))
            m = re.match(r"stored_callable|path_call_without_path|string filepath_raw", json.loads(out)["reason"])
            seen.add(m.group(0))
        self.assertEqual(seen, {"stored_callable", "path_call_without_path", "string filepath_raw"})

    def test_ast_no_rc1(self):
        for name in sorted(glob.glob(os.path.join(CORPUS, "adw3_*.py"))) + [None]:
            rc, _ = run(rd(name) if name else "def (:\n")
            self.assertIn(rc, (0, 2, 5))
        rc, _ = run("", args=["--bogus"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
