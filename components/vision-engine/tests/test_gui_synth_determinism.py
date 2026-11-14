"""P1 determinism: the DEV sha table is identical across fresh processes (different PYTHONHASHSEED) and equals the in-process table."""
import hashlib
import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = """
import hashlib, json
from eval.gui_synth import gen, scenes
t = {}
for kind in scenes.KINDS:
    for k in (0, 5):
        sc = scenes.build(kind, k, w=160, h=96, fps=60, n=12)
        t["%s/%d" % (kind, k)] = [hashlib.sha256(a.tobytes()).hexdigest() for _, a in gen.render(sc)]
print(json.dumps(t, sort_keys=True))
"""


def table(hashseed):
    env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", CODE], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-400:]
    return json.loads(r.stdout)


class TestDeterminism(unittest.TestCase):
    def test_two_processes_identical(self):
        a, b = table("1"), table("987")
        self.assertEqual(a, b)
        sha = lambda t: hashlib.sha256(json.dumps(t, sort_keys=True).encode()).hexdigest()
        self.assertEqual(sha(a), sha(b))

    def test_seeds_do_not_alias(self):
        t = table("0")
        from eval.gui_synth import scenes
        for kind in scenes.KINDS:
            self.assertNotEqual(t[kind + "/0"], t[kind + "/5"], kind)

    def test_scene_graph_is_pure_function_of_kind_k(self):
        from eval.gui_synth import scenes
        for kind in scenes.KINDS:
            a = json.dumps(scenes.build(kind, 9, w=160, h=96, fps=30, n=8), sort_keys=True)
            b = json.dumps(scenes.build(kind, 9, w=160, h=96, fps=30, n=8), sort_keys=True)
            self.assertEqual(a, b, kind)


if __name__ == "__main__":
    unittest.main()
