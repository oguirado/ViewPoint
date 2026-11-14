import ast
import glob
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CHECK = os.path.join(ROOT, "capsule", "astcheck", "astcheck.py")
CORPUS = os.path.join(ROOT, "testdata", "cu", "ast")
OUT = "/scratch/out"


def rd(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def run(src, out=OUT, args=None):
    a = [sys.executable, "-I", "-B", CHECK] + (args if args is not None else (["--out-dir", out] if out else []))
    p = subprocess.run(a, input=src.encode("utf-8"), capture_output=True, timeout=30)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class AstCheckTest(unittest.TestCase):
    def deny(self, src, why=""):
        rc, out = run(src)
        self.assertEqual(rc, 5, (src, out))
        v = json.loads(out)
        self.assertEqual((v["verdict"], v["code"]), ("deny", "ast_denied"))
        self.assertIn(why, v["reason"])

    def test_ast_allows_translate_cube(self):
        rc, out = run("import bpy\nbpy.ops.transform.translate(value=(2,0,0))\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["verdict"], "allow")
        rc, out = run("import bpy\ncube = bpy.data.objects['Cube']\ncube.location.x += 2.0\ncube.select_set(True)\n"
                      "for o in bpy.data.objects:\n    o.hide_viewport = False\nv = bpy.context.object.location[0] * 2\n")
        self.assertEqual(rc, 0, out)

    def test_ast_allows_benign_corpus(self):
        files = sorted(glob.glob(os.path.join(CORPUS, "allow", "*.py")))
        self.assertEqual(len(files), 6)
        for f in files:
            rc, out = run(rd(f))
            self.assertEqual(rc, 0, (f, out))

    def test_ast_denies_fj01_corpus(self):
        files = sorted(glob.glob(os.path.join(CORPUS, "deny", "*.py")))
        self.assertEqual(len(files), 19)
        for f in files:
            rc, out = run(rd(f))
            self.assertEqual(rc, 5, (f, out))

    def test_ast_denies_nonascii_identifier(self):
        self.deny("ｅｘｅｃ('1')", "non-ascii")
        self.deny("import bpy\nbpy.ops.object.select_all(ａｃｔｉｏｎ='SELECT')\n", "non-ascii")
        self.deny("import bpy\nx = 1 ＋ 2\n", "non-ascii")
        rc, _ = run("import bpy\nx = 'Würfel'  # größe\n")
        self.assertEqual(rc, 0)

    def test_ast_denies_getattr_string(self):
        for s, why in (("getattr(bpy, 'app')", "call getattr"), ("getattr(1, 'real')", "call getattr"),
                       ("setattr(bpy, 'x', 1)", "call setattr"), ("globals()", "call globals"), ("vars(bpy)", "call vars"),
                       ("__import__('os')", "call __import__"), ("eval('1')", "call eval"), ("exec('1')", "call exec"),
                       ("compile('1','a','exec')", "call compile"), ("open('/scratch/out/a','w')", "call open"),
                       ("breakpoint()", "call breakpoint"), ("input()", "call input"), ("chr(47)", "call chr")):
            self.deny("import bpy\n" + s + "\n", why)

    def test_ast_denies_dunder_attr(self):
        self.deny("import bpy\nx = bpy.context.__class__\n", "underscore")
        self.deny("import bpy\n_x = 1\n", "underscore")
        self.deny("import bpy\nx = bpy.ops._x\n")

    def test_ast_syntax_error_denied(self):
        self.deny("import bpy\nx = (\n", "syntax")
        self.deny("import bpy\nx = 1\n\x00", "")
        rc, _ = run("")
        self.assertEqual(rc, 0)

    def test_ast_denies_structural_escapes(self):
        for s, why in (("f = lambda: 1", "node Lambda"), ("x = f'{1}'", "f-string"),
                       ("bpy.ops.wm.save_as_mainfile(**{'filepath': '/scratch/out/a'})", "**kwargs"),
                       ("def f():\n    pass", "node FunctionDef"), ("class C:\n    pass", "node ClassDef"),
                       ("while True:\n    pass", "node While"), ("try:\n    pass\nexcept Exception:\n    pass", "node Try"),
                       ("with open('/scratch/out/a') as f:\n    pass", "node With"), ("import os", "import os"),
                       ("import bpy as b", "import bpy"), ("from bpy import ops", "from bpy"), ("from os import path", "from os"),
                       ("from mathutils import *", "from-import *"), ("from math import *", "from-import *"),
                       ("x = bpy", "bare module bpy"), ("o = bpy.ops", "chain bpy.ops"), ("x = 2 ** 9 ** 9", "node Pow"),
                       ("x = 1 << 64", "node LShift"), ("bpy.ops.text.run_script()", "attribute run_script"),
                       ("bpy.ops.wm.addon_enable(module='x')", "bpy.ops.wm.addon_enable"),
                       ("bpy.app.handlers.save_post.append(f)", "chain bpy.app"), ("bpy.data.texts.new('t')", "attribute texts"),
                       ("bpy.ops.wm.save_as_mainfile(filepath=p)", "filepath must be a literal"),
                       ("p = '/scratch/out/a'\nbpy.ops.wm.save_mainfile(filepath=p)", "filepath must be a literal"),
                       ("bpy.ops.wm.save_as_mainfile(filepath='/scratch/out/../etc/a')", "filepath must be a literal"),
                       ("bpy.context.scene.render.filepath = '/etc/x'", "filepath must be a literal"),
                       ("bpy.context.scene.render.filepath = '/scratch/out/' + 'x'", "filepath must be a literal"),
                       ("bpy.data.images.load(p)", "load must be a literal"), ("bpy.ops.object.f(directory=d)", "directory must be a literal")):
            self.deny("import bpy\n" + s + "\n", why)

    def test_ast_deciding_rules(self):
        # each case has exactly one violated rule (mutants A2, A3, A6 and the new rules)
        for s, why in (("from math import *", "from-import *"),
                       ("p = '/scratch/out/a.png'\nbpy.data.images.load(p)", "load must be a literal"),
                       ("p = '/scratch/out/a.png'\nbpy.data.images.save(p)", "save must be a literal"),
                       ("bpy.ops.object.select_all(files=1)", "keyword files"),
                       ("bpy.ops.object.select_all(use_scripts=True)", "keyword use_scripts"),
                       ("bpy.data.images.load('rel.png')", "load must be a literal"),
                       ("bpy.ops.wm.save_as_mainfile(filepath='rel.blend')", "filepath must be a literal"),
                       ("bpy.context.scene.render.filepath = 'rel.png'", "filepath must be a literal"),
                       ("bpy.context.scene.render.filepath += 'x'", "augmented path assignment"),
                       ("x = 'a/../b'", "dotdot"), ("x = ['..']", "dotdot")):
            self.deny("import bpy\n" + s + "\n", why)

    def test_ast_denies_non_name_chain_root(self):
        for s in ("[bpy.context][0].ops.wm.addon_enable(module='evil')", "(bpy.data,)[0].ops.wm.addon_install()",
                  "(bpy.context if 1 else bpy.data).ops.screen.x()", "[bpy.context][0].ops.screen.x()",
                  "({'a': bpy.context}['a']).ops.x()", "(1,).count(1)"):
            self.deny("import bpy\n" + s + "\n", "non-name chain root")
        rc, out = run("import bpy\nbpy.data.objects['Cube'].location.x = 1.0\nbpy.context.object.select_set(True)\n")
        self.assertEqual(rc, 0, out)

    def test_ast_denies_target_and_indirect_escapes(self):
        pre = "import bpy\n"
        for s, why in (("(bpy.context.scene.render.filepath,) = ('a.blend',)", "non-name target"),
                       ("[bpy.context.scene.render.filepath] = ['a.blend']", "non-name target"),
                       ("a, (bpy.context.scene.render.filepath, b) = 1, (2, 3)", "non-name target"),
                       ("for bpy.context.scene.render.filepath in ['a.blend']:\n    pass", "non-name target"),
                       ("for (x, bpy.context.scene.render.filepath) in [(1, 'a')]:\n    pass", "non-name target"),
                       ("y = [0 for bpy.context.scene.render.filepath in ['a']]", "non-name target"),
                       ("y = (z := 1)", "node NamedExpr"),
                       ("f = bpy.data.images.load\nf('a.blend')", "call f"),
                       ("f = bpy.data.images.load\nf('/scratch/out/a.png')", "call f"),
                       ("bpy.ops.wm.save_as_mainfile('a.blend')", "bpy.ops positional args"),
                       ("bpy.ops.object.select_all('SELECT')", "bpy.ops positional args")):
            self.deny(pre + s + "\n", why)
        adv = ("s = '/scratch/out/a'\ne = '..' + s[0] + '..' + s[0] + 'etc' + s[0] + 'passwd'\n"
               "(bpy.context.scene.render.filepath,) = (s[:-1] + e,)\nf = bpy.data.images.load\nf(s[:-1] + e)\n"
               "bpy.ops.wm.save_as_mainfile('a.blend')\n")
        self.deny(pre + adv)
        rc, out = run(pre + "for o in bpy.data.objects:\n    o.hide_viewport = False\nx, y = 1, 2\n"
                      "z = [i for i in range(3)]\nbpy.data.images.load('/scratch/out/a.png')\n")
        self.assertEqual(rc, 0, out)

    def test_ast_denies_context_data_alias(self):
        # ADW2-01 / RJ1-03: bpy.context / bpy.data only as the root of a chain of length >= 2
        pre = "import bpy\n"
        for s, why in (("c = bpy.context", "chain bpy.context"), ("d = bpy.data", "chain bpy.data"),
                       ("c=bpy.context\nc.ops.wm.addon_enable(module='evil')", "chain bpy.context"),
                       ("x = [bpy.context]", "chain bpy.context"), ("x = tuple([bpy.data])", "chain bpy.data")):
            self.deny(pre + s + "\n", why)
        # a Call result is never a chain root (base_of no longer walks Call.func)
        for s in ("tuple([bpy.context])[0].ops.wm.addon_enable(module='evil')",
                  "tuple([bpy.context])[0].ops.wm.save_as_mainfile('a.blend')"):
            self.deny(pre + s + "\n", "non-name chain root")
        adv = ("c=bpy.context\nc.ops.wm.addon_enable(module=\"evil\")\ntuple([bpy.context])[0].ops.wm.addon_enable(module=\"evil\")\n"
               "tuple([bpy.context])[0].ops.wm.save_as_mainfile(\"a.blend\")\n")
        self.deny(pre + adv)
        rc, out = run(pre + "o = bpy.context.object\nv = bpy.data.objects['Cube'].location.x\nbpy.context.object.select_set(True)\n")
        self.assertEqual(rc, 0, out)

    def test_ast_denies_subscript_path_target(self):
        # ADW2-02: Subscript targets are denied in Assign/AugAssign; path/libpath keys are str_deny
        pre = "import bpy\n"
        for s, why in (("s=\"/scratch/out/a\"\nbpy.context.scene.render[\"file\"+\"path\"]=s[0]+\"etc\"+s[0]+\"passwd\"", "non-name target"),
                       ("bpy.context.scene.render[\"path\"]=\"a.blend\"", "non-name target"),
                       ("bpy.context.scene.render[\"file\"+\"path\"] += \"x\"", "non-name target"),
                       ("k = 'path'", "string path"), ("k = 'libpath'", "string libpath")):
            self.deny(pre + s + "\n", why)
        rc, out = run(pre + "v = bpy.context.object.location[0]\nbpy.data.objects['Cube'].location.x = 1.0\n")
        self.assertEqual(rc, 0, out)

    def test_ast_out_dir_prefix(self):
        src = "import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/a.blend')\n"
        self.assertEqual(run(src)[0], 0)
        self.assertEqual(run(src, out="/scratch/other")[0], 5)
        self.assertEqual(run(src, out="")[0], 5)
        self.assertEqual(run(src, out="/scratch/ou")[0], 5)

    def test_ast_usage_rc2_no_rc1(self):
        for a in (["--bogus"], ["--out-dir"], ["x"]):
            self.assertEqual(run("import bpy\n", args=a)[0], 2, a)
        # crash-like inputs fail closed with 5, never 1
        for src in ("(" * 5000, "x = " + "[" * 3000 + "]" * 3000, "\ufeffimport bpy"):
            self.assertNotIn(run("import bpy\n" + src)[0], (1,), src[:10])
        p = subprocess.run([sys.executable, "-I", "-B", CHECK], input=b"\xff\xfe", capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 5)

    def test_ast_no_rc1(self):
        rcs = {run(s)[0] for s in ("import bpy\n", "import os\n", "(\n", "import bpy\n" + "x=(" * 400)}
        self.assertTrue(rcs <= {0, 5}, rcs)

    def test_ast_stdlib_only(self):
        tree = ast.parse(rd(CHECK))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        self.assertTrue(mods, "imports found")
        self.assertTrue(mods <= set(sys.stdlib_module_names), mods)
        self.assertTrue(json.loads(rd(os.path.join(os.path.dirname(CHECK), "allowlist.json")))["modules"] == ["bpy", "math", "mathutils"])


if __name__ == "__main__":
    unittest.main()
