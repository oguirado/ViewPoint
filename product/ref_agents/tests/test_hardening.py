#!/usr/bin/env python3
"""Hardening tests for unit F4 (lane sw-agents-rewind, claude-fable-5-2, Track P).

Hostile or broken intents and bpy scripts: validator bypasses, malformed files,
scripts that never end.  Every class has a must-fail control.

Run (from product/ref_agents):
  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
      python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import contract  # noqa: E402
import guard  # noqa: E402
import stub_agent  # noqa: E402

RT_BASE = "/dev/shm/vqa-cu/ref-f4-hard-%d" % os.getpid()
LIVE_INTENT = os.path.join(ROOT, "evidence", "live_modeler", "modeler-agent",
                           "intent_modeler-agent-1.json")


def good():
    return stub_agent.build_intents("modeler-agent", 0)[0]


def legit_scripts():
    out = []
    for a in contract.AGENT_IDS:
        for rec in stub_agent.build_intents(a, 0):
            if rec["lane"] == "script":
                out.append(rec["script"])
    with open(LIVE_INTENT, encoding="utf-8") as fh:
        out.append(json.load(fh)["script"])   # written by a real gemini agent
    return out


# Each of these reaches the host, kills / persists in the shared Blender session, or
# hides such a reach from a purely syntactic check.  All must be refused.
HOSTILE = {
    "private module attr (random._os)": "import random\nrandom._os.system('true')\n",
    "dunder walk to subclasses": "import bpy\n().__class__.__base__.__subclasses__()\n",
    "getattr on ops": "import bpy\ngetattr(bpy.ops, 'w' + 'm').quit_blender()\n",
    "alias of bpy": "import bpy\nb = bpy\nb.ops.wm.quit_blender()\n",
    "alias of bpy.ops": "import bpy\no = bpy.ops\no.wm.quit_blender()\n",
    "alias of ops category": "import bpy\nw = bpy.ops.wm\nw.quit_blender()\n",
    "import alias": "import bpy as b\nb.ops.wm.quit_blender()\n",
    "from-import of ops": "from bpy import ops\nops.wm.quit_blender()\n",
    "ops.script": "import bpy\nbpy.ops.script.python_file_run(filepath='/tmp/x.py')\n",
    "ops.preferences": "import bpy\nbpy.ops.preferences.addon_install(filepath='/tmp/x.zip')\n",
    "ops.render (blocks for minutes, writes files)": "import bpy\nbpy.ops.render.render(write_still=True)\n",
    "ops.export": "import bpy\nbpy.ops.export_scene.gltf(filepath='/tmp/x')\n",
    "persistent handler": "import bpy\nbpy.app.handlers.depsgraph_update_post.append(print)\n",
    "timer": "import bpy\nbpy.app.timers.register(print)\n",
    "driver namespace": "import bpy\nbpy.app.driver_namespace['x'] = print\n",
    "bpy.utils": "import bpy\nbpy.utils.execfile('/tmp/x.py')\n",
    "library load": "import bpy\nbpy.data.libraries.load('/tmp/x.blend')\n",
    "text as module": "import bpy\nbpy.data.texts.new('x').as_module()\n",
    "image save to host": "import bpy\nbpy.data.images.new('i', 4, 4).save_render('/tmp/x.png')\n",
    "globals()": "import bpy\nglobals()['x'] = 1\n",
    "vars()": "import bpy\nvars(bpy)\n",
    "builtins name": "import bpy\n__builtins__['eval']('1')\n",
    "SystemExit": "import bpy\nraise SystemExit(0)\n",
    "exit()": "import bpy\nexit()\n",
    "bare except (swallows the budget stop)": "import bpy\ntry:\n    pass\nexcept:\n    pass\n",
    "except BaseException": "import bpy\ntry:\n    pass\nexcept BaseException:\n    pass\n",
    "except tuple with BaseException": "import bpy\ntry:\n    pass\nexcept (ValueError, BaseException):\n    pass\n",
    "finally that swallows": "import bpy\nfor i in range(3):\n    try:\n        pass\n    finally:\n        continue\n",
    "dunder method (context manager swallow)":
        "import bpy\nclass S:\n    def __exit__(self, *a):\n        return True\n",
    "subdivision bomb (kwarg)": "import bpy\nbpy.ops.object.subdivision_set(levels=12)\n",
    "segment bomb (kwarg)": "import bpy\nbpy.ops.mesh.primitive_uv_sphere_add(segments=100000, ring_count=50000)\n",
    "level bomb (attribute)": "import bpy\nm = bpy.context.object.modifiers.new('s', 'SUBSURF')\nm.levels = 11\n",
    "null byte": "import bpy\nx = 1\x00\n",
    # interpreter state: survives open_mainfile, so no rewind undoes it (see InterpreterPoison)
    "rebind bpy.data": "import bpy\nbpy.data = None\n",
    "rebind math.sin": "import math\nmath.sin = None\n",
    "del module attr": "import random\ndel random.random\n",
    "alias math then rebind": "import math\nm = math\nm.sin = None\n",
    "class attr through module": "import random\nrandom.Random.seed = None\n",
    "augmented module attr": "import math\nmath.pi += 1\n",
    "RNA class property": "import bpy\nbpy.types.Object.vp = 1\n",
    "draw handler": "import bpy\nbpy.types.SpaceView3D.draw_handler_add(print, (), 'WINDOW', 'POST_PIXEL')\n",
    "menu append": "import bpy\nbpy.types.VIEW3D_MT_object.append(print)\n",
    "msgbus": "import bpy\nbpy.msgbus.clear_by_owner(1)\n",
    "bpy.props": "import bpy\nx = bpy.props.IntProperty()\n",
    "bpy.path": "import bpy\nx = bpy.path.abspath('//')\n",
    "json outside a probe": "import json\nx = json.dumps(1)\n",
}

# Legal idioms that the hardening must NOT refuse (false refusals also break the take).
BENIGN = {
    "ops call": "import bpy\nbpy.ops.mesh.primitive_cube_add(size=1.0)\n",
    "bmesh ops": "import bpy, bmesh\nbm = bmesh.new()\nbmesh.ops.create_cube(bm, size=1.0)\nbm.free()\n",
    "except Exception": "import bpy\ntry:\n    x = 1\nexcept Exception:\n    x = 2\n",
    "try finally cleanup": "import bpy, bmesh\nbm = bmesh.new()\ntry:\n    pass\nfinally:\n    bm.free()\n",
    "private local name": "import bpy\n_tmp = 3\n",
    "main guard": "import bpy\nif __name__ == '__main__':\n    pass\n",
    "while loop": "import bpy\ni = 0\nwhile i < 3:\n    i += 1\n",
    "modest levels": "import bpy\nbpy.ops.object.subdivision_set(levels=2)\n",
    "from import": "from math import pi, sin\nimport bpy\nfrom mathutils import Vector\n",
    "isinstance on bpy.types": "import bpy\nok = isinstance(bpy.data.objects.get('a'), bpy.types.Object)\n",
    "context store": "import bpy\nbpy.context.scene.frame_current = 3\n",
    "data chain store": "import bpy\nbpy.data.scenes[0].render.resolution_x = 640\n",
    "bmesh.utils": "import bmesh\nf = bmesh.utils.face_flip\n",
    "mathutils submodule": "import mathutils\nn = mathutils.noise.random()\n",
}


class ScriptStaticTest(unittest.TestCase):
    def test_hostile_scripts_refused(self):
        for name, src in HOSTILE.items():
            rec = good()
            rec["script"] = src
            errs = contract.validate_intent(rec)
            self.assertTrue(any(e.startswith("script:") for e in errs), "ACCEPTED: " + name)

    def test_benign_scripts_accepted(self):
        for name, src in BENIGN.items():
            self.assertEqual(contract.check_script(src), [], name)

    def test_legit_corpus_still_valid(self):
        """Control in the other direction: stub jobs and the real agent's job stay valid."""
        scripts = legit_scripts()
        self.assertEqual(len(scripts), 8)
        for s in scripts:
            self.assertEqual(contract.check_script(s), [])

    def test_determinism_warning(self):
        self.assertTrue(contract.determinism_warnings("import random\nx = random.random()\n"))
        self.assertEqual(contract.determinism_warnings(
            "import random\nrandom.seed(7)\nx = random.random()\n"), [])
        for s in legit_scripts():
            self.assertEqual(contract.determinism_warnings(s), [])

    def test_pathological_source_does_not_crash_validator(self):
        for src in ("x = " + "(" * 5000 + "1" + ")" * 5000 + "\n",
                    "x = " + "+".join(["1"] * 200000) + "\n",
                    "x = " + "-" * 100000 + "1\n",
                    "x = " + "[" * 300 + "]" * 300 + "\n"):
            rec = good()
            rec["script"] = src
            errs = contract.validate_intent(rec)       # must return, never raise
            self.assertIsInstance(errs, list)


class IntentFormTest(unittest.TestCase):
    def setUp(self):
        self.rt = RT_BASE + "-form"
        shutil.rmtree(self.rt, ignore_errors=True)
        os.makedirs(os.path.join(self.rt, "intents", "modeler-agent"))

    def tearDown(self):
        shutil.rmtree(self.rt, ignore_errors=True)

    def _put(self, name, data):
        p = os.path.join(self.rt, "intents", "modeler-agent", name)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def test_trailing_newline_in_ids_refused(self):
        rec = good()
        rec["event_id"] = rec["event_id"] + "\n"
        self.assertTrue(any(e.startswith("event_id:") for e in contract.validate_intent(rec)))
        rec = good()
        rec["write_set"] = ["col:CHAR_mesh\n"]
        claims = {"modeler-agent": ["col:CHAR_mesh\n"]}   # even if a claim table carried it
        self.assertTrue(any(e.startswith("write_set:") for e in contract.validate_intent(rec, claims)))

    def test_wrong_types_never_raise(self):
        bad_values = [None, 1, 1.5, True, [], {}, [[]], {"a": 1}, "", "x" * 10]
        for field in contract.REQUIRED + ("script", "keys", "x", "y"):
            for v in bad_values:
                rec = good()
                rec[field] = v
                errs = contract.validate_intent(rec, agent_id="modeler-agent",
                                                event_id="modeler-agent-1", newest=0)
                self.assertIsInstance(errs, list)
                legal = {("v", 1), ("expect", "x" * 10), ("script", "x" * 10)}
                if (field in contract.REQUIRED or field == "script") \
                        and (field, v if isinstance(v, (int, str)) and not isinstance(v, bool) else None) not in legal:
                    self.assertTrue(errs, (field, v))

    def test_unknown_field_refused(self):
        rec = good()
        rec["verdict"] = "COMMIT"        # an agent may not pre-write broker fields
        self.assertTrue(any("unknown field" in e for e in contract.validate_intent(rec)))
        rec = good()
        rec["x"] = 3                     # x/y belong to lane gui kind xy only
        self.assertTrue(contract.validate_intent(rec))

    def test_malformed_files_never_raise(self):
        cases = {
            "modeler-agent-1.json": b"[" * 200000,                        # RecursionError in json
            "modeler-agent-2.json": b"\xff\xfe\x00\x01",                  # not UTF-8
            "modeler-agent-3.json": b"",                                   # empty
            "modeler-agent-4.json": b'{"v": NaN}',
            "modeler-agent-5.json": json.dumps(good()).encode()[:-7],     # torn write
            "modeler-agent-6.json": b'{"v":1,"v":1}',
        }
        for name, data in cases.items():
            p = self._put(name, data)
            errs = contract.validate_file(p)
            self.assertTrue(errs, name)
        res = contract.validate_rt(self.rt)
        self.assertEqual(len(res), len(cases))
        self.assertTrue(all(res.values()))

    def test_duplicate_keys_refused(self):
        rec = good()
        raw = json.dumps(rec)
        # second write_set wins in json.load: a reviewer reading the first sees the wrong claim
        raw = raw[:-1] + ', "write_set": ["col:CHAR_mesh"]}'
        p = self._put("modeler-agent-1.json", raw)
        self.assertTrue(any("duplicate key" in e for e in contract.validate_file(p)))

    def test_oversized_file_refused_without_parsing(self):
        p = self._put("modeler-agent-1.json", b'{"pad": "' + b"a" * (contract.MAX_INTENT_BYTES + 10) + b'"}')
        self.assertTrue(any("larger than" in e for e in contract.validate_file(p)))

    def test_symlink_intent_refused(self):
        real = self._put("real.txt", json.dumps(good()))
        link = os.path.join(self.rt, "intents", "modeler-agent", "modeler-agent-1.json")
        os.symlink(real, link)
        self.assertTrue(any("regular file" in e for e in contract.validate_file(link)))

    def test_valid_file_still_valid(self):
        """Control: the same path checks pass a good record (the gate is not just 'refuse all')."""
        p = self._put("modeler-agent-1.json", json.dumps(good()))
        self.assertEqual(contract.validate_file(p), [])

    def test_claims_table_tampering_detected(self):
        """An agent that rewrites RT/claims.json to own a foreign collection gains nothing."""
        with open(os.path.join(self.rt, "claims.json"), "w") as fh:
            json.dump({"modeler-agent": ["col:CHAR_mesh", "col:CHAR_props"],
                       "props-agent": ["col:CHAR_props"]}, fh)
        self.assertTrue(contract.claims_errors(contract.load_claims_raw(self.rt)))
        rec = good()
        rec["write_set"] = ["col:CHAR_props"]
        p = self._put("modeler-agent-1.json", json.dumps(rec))
        res = contract.validate_rt(self.rt)
        self.assertTrue(any("CLAIM_REFUSED" in e for e in res[p]), res[p])
        # control: a disjoint non-default table is honoured
        with open(os.path.join(self.rt, "claims.json"), "w") as fh:
            json.dump({"modeler-agent": ["col:CHAR_props"], "props-agent": ["col:CHAR_mesh"]}, fh)
        self.assertEqual(contract.validate_rt(self.rt)[p], [])


class RewindExtensionTest(unittest.TestCase):
    """Optional intent fields of the rewind contract rw1 (ref_rewind/CONTRACT.md section 5)."""

    PROBE = "import bpy, json\nprint('RW_RESULT ' + json.dumps({'n': len(bpy.data.objects)}))\n"

    def test_extension_fields_accepted(self):
        rec = good()
        rec["alternatives"] = [rec["script"], rec["script"]]
        rec["touches"] = ["mat:CHARMAT_skin", "render"]
        rec["expect"] = {"probe": self.PROBE, "equals": {"n": 11}}
        self.assertEqual(contract.validate_intent(rec), [])
        out = guard.prepare_intent(rec)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["scripts"]), 3)
        self.assertIn(guard.TICK, out["scripts"][2])
        self.assertIsNotNone(out["probe"])

    def test_stub_rewind_intents_declare_their_materials(self):
        """Measured with the R1 digest (evidence/rewind_digest_probe/stub_delta.txt): 5 of the
        7 stub jobs change mat:CHARMAT_* keys; undeclared, the rw1 judge answers DELTA_OUTSIDE."""
        import ast
        n = 0
        for a in contract.AGENT_IDS:
            plain = stub_agent.build_intents(a, 0)
            for rec, old in zip(stub_agent.build_intents(a, 0, rewind=True), plain):
                self.assertEqual(contract.validate_intent(rec), [])
                if rec["lane"] != "script":
                    self.assertNotIn("touches", rec)
                    continue
                named = set("mat:" + c.value for c in ast.walk(ast.parse(rec["script"]))
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)
                            and c.value.startswith("CHARMAT_"))
                self.assertEqual(set(rec["touches"]), named)
                n += len(named)
                self.assertNotIn("touches", old)          # default output unchanged
                self.assertEqual({k: v for k, v in rec.items() if k != "touches"}, old)
        self.assertGreaterEqual(n, 9)

    def test_genesis_script(self):
        src = contract.genesis_script()
        self.assertEqual(contract.check_script(src), [])
        for name in ("CHAR_mesh", "CHAR_props", "CHAR_look", "CHAR_cam"):
            self.assertIn(repr(name), src)
        self.assertEqual(src, contract.genesis_script(dict(reversed(list(contract.DEFAULT_CLAIMS.items())))))
        with self.assertRaises(ValueError):
            contract.genesis_script({"modeler-agent": ["col:A"], "props-agent": ["col:A"]})

    def test_hostile_alternative_refused(self):
        """An alternative is a job: it gets the same static checks as `script`."""
        for name, src in list(HOSTILE.items())[:6]:
            rec = good()
            rec["alternatives"] = [src]
            errs = contract.validate_intent(rec)
            self.assertTrue(any(e.startswith("alternatives[0]:") for e in errs), name)
            self.assertEqual(guard.prepare_intent(rec)["scripts"], [])

    def test_hostile_probe_refused(self):
        for probe in ("import json\njson.codecs.open('/etc/hostname')\n",
                      "import os\n", "import bpy\nbpy.ops.wm.quit_blender()\n",
                      "import json\nfrom json import codecs\n"):
            rec = good()
            rec["expect"] = {"probe": probe, "equals": 1}
            errs = contract.validate_intent(rec)
            self.assertTrue(any(e.startswith("expect.probe:") for e in errs), probe)

    def test_extension_shape(self):
        for field, bad in (("alternatives", ["a = 1\n"] * 3), ("alternatives", "x"),
                           ("touches", ["col:CHAR_props"]),          # claim bypass through touches
                           ("touches", ["render", "render"]), ("touches", ["mat:"]),
                           ("touches", ["ui:view3d\n"]),
                           ("expect", {"probe": self.PROBE}),
                           ("expect", {"probe": self.PROBE, "equals": 1, "verdict": "x"})):
            rec = good()
            rec[field] = bad
            self.assertTrue(contract.validate_intent(rec), (field, bad))
        gui = stub_agent.build_intents("camera-agent", 0)[0]
        gui["alternatives"] = []
        self.assertTrue(contract.validate_intent(gui))        # alternatives: lane script only


def run_src(src, budget_s):
    ns = {"__name__": "__vp_job__"}
    exec(compile(guard.instrument(src, budget_s), "job", "exec"), ns)
    return ns


class GuardTest(unittest.TestCase):
    """guard.instrument: a job that does not end is stopped from inside, on the main thread."""

    NEVER_ENDING = {
        "while True": "while True:\n    pass\n",
        "while in function": "def f():\n    i = 0\n    while True:\n        i += 1\nf()\n",
        "for over endless generator": "def g():\n    while True:\n        yield 1\nfor x in g():\n    pass\n",
        "huge range": "for i in range(10**12):\n    pass\n",
        "comprehension": "x = [i for i in range(10**12) if i < 0]\n",
        "generator expression": "x = sum(1 for i in range(10**12))\n",
        "swallowing Exception in loop":
            "while True:\n    try:\n        pass\n    except Exception:\n        pass\n",
        "mutual recursion with loop":
            "def a(n):\n    while True:\n        b(n)\ndef b(n):\n    return n\na(1)\n",
    }

    def test_never_ending_jobs_are_stopped(self):
        for name, src in self.NEVER_ENDING.items():
            self.assertEqual(contract.check_script(src), [], name)   # statically legal
            t0 = time.monotonic()
            with self.assertRaises(BaseException) as cm:
                run_src(src, 0.2)
            self.assertIn(guard.MARK, str(cm.exception), name)
            self.assertLess(time.monotonic() - t0, 5.0, name)
            self.assertFalse(isinstance(cm.exception, Exception), name)

    def test_negative_control_uninstrumented_job_keeps_running(self):
        """Without the guard the same job is still alive after 10x the budget: the stop
        in the test above comes from the instrumentation and from nothing else."""
        src = self.NEVER_ENDING["while in function"]
        p = subprocess.Popen([sys.executable, "-I", "-c", src])
        try:
            time.sleep(2.0)
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            p.wait()

    def test_finite_job_unchanged_semantics(self):
        src = ("import math\n"
               "def area(r):\n    return math.pi * r * r\n"
               "acc = []\n"
               "for i in range(5):\n    acc.append(i * i)\n"
               "sq = [a + 1 for a in acc if a % 2 == 0]\n"
               "d = {k: v for k, v in zip('abc', range(3))}\n"
               "i = 0\n"
               "while i < 4:\n    i += 1\nelse:\n    done = True\n"
               "lam = sorted(acc, key=lambda v: -v)\n"
               "res = area(2)\n")
        plain = {}
        exec(compile(src, "p", "exec"), plain)
        g = run_src(src, 5.0)
        for k in ("acc", "sq", "d", "i", "done", "lam", "res"):
            self.assertEqual(plain[k], g[k], k)

    def test_job_exception_passes_through(self):
        with self.assertRaises(ZeroDivisionError):
            run_src("x = 1 / 0\n", 5.0)

    def test_instrument_is_deterministic(self):
        src = legit_scripts()[0]
        self.assertEqual(guard.instrument(src, 3.0), guard.instrument(src, 3.0))
        self.assertNotIn("time.time()", guard.instrument(src, 3.0))

    def test_instrument_refuses_invalid_script(self):
        """instrument() is not a way around the validator."""
        with self.assertRaises(ValueError):
            guard.instrument("import os\n", 1.0)
        with self.assertRaises(ValueError):
            guard.instrument("while True:\n    try:\n        pass\n    except BaseException:\n        pass\n", 1.0)

    def test_legit_corpus_instruments_and_validates(self):
        for s in legit_scripts():
            out = guard.instrument(s, 10.0)
            compile(out, "g", "exec")


POISON = "import bpy, math\nbpy.data = None\nmath.sin = None\n"


class GuardInBlender(unittest.TestCase):
    """Real Blender 4.0.2: the bridge's exec pattern, hostile jobs, session survives."""

    def test_blender_survives_hostile_jobs(self):
        rt = RT_BASE + "-blender"
        shutil.rmtree(rt, ignore_errors=True)
        os.makedirs(rt)
        try:
            legit = stub_agent.build_intents("modeler-agent", 0)[0]["script"]
            jobs = [
                ["loop", guard.instrument("import bpy\nwhile True:\n    bpy.data.meshes.new('CHAR_mesh.junk')\n", 0.5)],
                ["raise", guard.instrument("import bpy\nbpy.data.objects['does-not-exist'].location.x = 1\n", 5.0)],
                ["legit", guard.instrument(legit, 30.0)],
            ]
            with open(os.path.join(rt, "jobs.json"), "w") as fh:
                json.dump(jobs, fh)
            driver = (
                "import bpy, json, time, traceback\n"
                "for o in list(bpy.data.objects): bpy.data.objects.remove(o)\n"
                "out = {}\n"
                "for eid, src in json.load(open(%r)):\n"
                "    t0 = time.monotonic()\n"
                "    try:\n"
                "        exec(compile(src, eid, 'exec'), {'bpy': bpy, '__name__': '__bridge__'})\n"
                "        st = 'OK'\n"
                "    except BaseException:\n"       # same clause as ref_broker/bridge.py
                "        st = 'ERROR ' + traceback.format_exc()[-300:]\n"
                "    out[eid] = [st, time.monotonic() - t0, len(bpy.data.objects)]\n"
                # WHY module rebinding is refused: it survives a checkpoint restore.
                "import math\n"
                "bpy.ops.wm.save_as_mainfile(filepath=%r, copy=True, check_existing=False)\n"
                "real = bpy.data\n"
                "exec(compile(%r, 'poison', 'exec'), {'__name__': '__bridge__'})\n"
                "try:\n"
                "    bpy.ops.wm.open_mainfile(filepath=%r, load_ui=False, use_scripts=False)\n"
                "except Exception:\n"
                "    pass\n"
                "out['poison'] = [bpy.data is None, math.sin is None]\n"
                "bpy.data = real\n"
                "json.dump(out, open(%r, 'w'))\n"
            ) % (os.path.join(rt, "jobs.json"), os.path.join(rt, "p.blend"), POISON,
                 os.path.join(rt, "p.blend"), os.path.join(rt, "out.json"))
            self.assertTrue(contract.check_script(POISON))       # the validator refuses it
            r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", driver],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout[-800:])
            with open(os.path.join(rt, "out.json")) as fh:
                out = json.load(fh)
            self.assertTrue(out["loop"][0].startswith("ERROR"), out["loop"])
            self.assertIn(guard.MARK, out["loop"][0])
            self.assertLess(out["loop"][1], 5.0)
            self.assertTrue(out["raise"][0].startswith("ERROR"), out["raise"])
            self.assertNotIn(guard.MARK, out["raise"][0])
            # the session is still usable: the next agent's job runs and builds its objects
            self.assertEqual(out["legit"][0], "OK", out["legit"])
            self.assertGreater(out["legit"][2], 0)
            self.assertEqual(out["poison"], [True, True], out["poison"])
        finally:
            shutil.rmtree(rt, ignore_errors=True)


F1_REPO = os.environ.get("VP_REPO_ROOT") or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def f1_bridge_source():
    """ref_broker/bridge.py as committed on the F1 branch (read-only; None if unavailable)."""
    try:
        r = subprocess.run(["git", "-C", F1_REPO, "show", "HEAD:product/ref_broker/bridge.py"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8") if r.returncode == 0 and r.stdout else None


class BridgeSlice(unittest.TestCase):
    """End-to-end slice: intent -> guard.prepare_intent -> RT/inbox -> the REAL reference
    bridge core (poll_once, unmodified) inside Blender 4.0.2 -> RT/outbox."""

    def test_budget_stop_through_the_real_bridge(self):
        bridge_src = f1_bridge_source()
        if bridge_src is None:
            self.skipTest("F1 bridge not readable from " + F1_REPO)
        rt = RT_BASE + "-bridge"
        shutil.rmtree(rt, ignore_errors=True)
        os.makedirs(os.path.join(rt, "inbox"))
        nonce = "n0nce%d" % os.getpid()
        try:
            with open(os.path.join(rt, "bridge.py"), "w") as fh:
                fh.write(bridge_src)
            loop = good()
            loop["event_id"] = "modeler-agent-1"
            loop["script"] = "import bpy\nn = 0\nwhile True:\n    n += 1\n"
            ok = good()
            ok["event_id"] = "modeler-agent-2"
            for rec, budget in ((loop, 0.5), (ok, 30.0)):
                prep = guard.prepare_intent(rec, budget_s=budget)
                self.assertTrue(prep["ok"], prep["errors"])
                with open(os.path.join(rt, "inbox", "%s_%s.py" % (nonce, rec["event_id"])), "w") as fh:
                    fh.write(prep["scripts"][0])
            driver = (
                "import bpy, sys, json, time\n"
                "sys.path.insert(0, %r)\n"
                "import bridge\n"
                "for o in list(bpy.data.objects): bpy.data.objects.remove(o)\n"
                "t0 = time.monotonic()\n"
                "ran = bridge.poll_once(%r, %r, {'bpy': bpy}, digest=lambda: bridge.scene_digest(bpy))\n"
                "json.dump({'ran': ran, 's': time.monotonic() - t0}, open(%r, 'w'))\n"
            ) % (rt, rt, nonce, os.path.join(rt, "driver.json"))
            env = dict(os.environ, VP_SWARM_RT=rt, VP_SWARM_NONCE=nonce)
            r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", driver],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, env=env)
            self.assertEqual(r.returncode, 0, r.stdout[-800:])
            with open(os.path.join(rt, "driver.json")) as fh:
                d = json.load(fh)
            self.assertEqual(d["ran"], ["modeler-agent-1", "modeler-agent-2"])
            self.assertLess(d["s"], 20.0)
            with open(os.path.join(rt, "outbox", "modeler-agent-1.out")) as fh:
                out1 = fh.read()
            with open(os.path.join(rt, "outbox", "modeler-agent-2.out")) as fh:
                out2 = fh.read()
            self.assertEqual(out1.split("\n", 1)[0], "ERROR")
            self.assertIn(guard.MARK, out1)
            self.assertEqual(out2.split("\n", 1)[0], "OK")
            meta = json.loads(out2.split("\n")[1][len("META "):])
            self.assertIn("col:CHAR_mesh", meta["changed"])
        finally:
            shutil.rmtree(rt, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
