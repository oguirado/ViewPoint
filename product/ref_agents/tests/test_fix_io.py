#!/usr/bin/env python3
"""Regression tests for the xr-io cross-review findings on unit F4 (lane fix-io,
claude-fable-5-2, Track P).  Each class was red on ff6b6b1 and is green after the fix.

Run (from product/ref_agents):
  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
      env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import contract  # noqa: E402
import make_frame  # noqa: E402
import runner  # noqa: E402
import stub_agent  # noqa: E402

RT_BASE = "/dev/shm/vqa-cu/ref-fix-io-f4-%d" % os.getpid()


def refused(src):
    return [e for e in contract.check_script(src) if e.startswith("script:")]


# A1 (BLOCKER): class objects are interpreter state that survives open_mainfile.
A1_HOSTILE = {
    "alias of bpy.types": "import bpy\nT = bpy.types\nT.Mesh.from_pydata = None\n",
    "alias of a bpy.types class": "import bpy\nM = bpy.types.Mesh\nM.from_pydata = None\n",
    "type() of a live object": "import bpy\ntype(bpy.context.scene).frame_set = None\n",
    "type() bound to a local": "import bpy\nt = type(bpy.data.objects[0])\nt.vp = 1\n",
    "header append through alias": ("import bpy\ndef f(self, context):\n    pass\n"
                                    "T = bpy.types\nT.VIEW3D_HT_header.append(f)\n"),
    "header prepend direct": ("import bpy\ndef f(self, context):\n    pass\n"
                              "bpy.types.VIEW3D_HT_header.prepend(f)\n"),
    "types passed to a helper": "import bpy\ndef g(t):\n    t.Mesh.from_pydata = None\ng(bpy.types)\n",
    "types in a container": "import bpy\nL = [bpy.types.Mesh]\nL[0].from_pydata = None\n",
    "subclass of a bpy type": "import bpy\nclass P(bpy.types.Panel):\n    pass\n",
    "bl_rna reflection": "import bpy\nr = bpy.context.scene.bl_rna\n",
    "isinstance smuggle (walrus)": ("import bpy\nisinstance(1, (T := bpy.types.Mesh))\n"
                                    "T.from_pydata = None\n"),
}
A1_BENIGN = {
    "isinstance": "import bpy\nok = isinstance(bpy.data.objects.get('a'), bpy.types.Object)\n",
    "isinstance tuple": ("import bpy\nok = isinstance(bpy.data.objects.get('a'),"
                         " (bpy.types.Object, bpy.types.Mesh))\n"),
    "local attr store": "import bpy\no = bpy.data.objects.get('a')\nif o:\n    o.location = (0, 0, 1)\n",
    "list append": "import bpy\nxs = []\nxs.append(1)\n",
}

# A3 (MAJOR): keymaps / drivers outlive the turn and can kill the shared GUI session.
A3_HOSTILE = {
    "context.window_manager": ("import bpy\nkc = bpy.context.window_manager.keyconfigs.active\n"),
    "data.window_managers": "import bpy\nw = bpy.data.window_managers[0]\n",
    "keyconfigs via local": "import bpy\ndef f(w):\n    return w.keyconfigs\n",
    "keymap_items.new": "import bpy\ndef f(km):\n    km.keymap_items.new('x.y', 'NUMPAD_1', 'PRESS')\n",
    "keymaps": "import bpy\ndef f(kc):\n    return kc.keymaps['Window']\n",
    "driver_add": "import bpy\nd = bpy.data.objects[0].driver_add('location', 0)\n",
    "driver expression": "import bpy\ndef f(d):\n    d.driver.expression = '1'\n",
    "text use_module": "import bpy\nt = bpy.data.objects[0]\nt.use_module = True\n",
}

# A2 (MAJOR): host file I/O through allowed op categories / bpy.data loaders.
A2_HOSTILE = {
    "uv.export_layout": "import bpy\nbpy.ops.uv.export_layout(filepath='/tmp/uv_pwn.svg')\n",
    "multires_external_save": "import bpy\nbpy.ops.object.multires_external_save(filepath='/tmp/x.btx')\n",
    "ocean_bake": "import bpy\nbpy.ops.object.ocean_bake(modifier='Ocean')\n",
    "texts.load": "import bpy\nt = bpy.data.texts.load('/etc/hostname')\n",
    "images.load": "import bpy\nt = bpy.data.images.load('/etc/hostname')\n",
    "filepath kwarg": "import bpy\nbpy.ops.object.bake(filepath='/tmp/x.png', save_mode='EXTERNAL')\n",
    "directory kwarg": "import bpy\nbpy.ops.object.select_all(directory='/tmp')\n",
    "files kwarg": "import bpy\nbpy.ops.object.select_all(files=[{'name': 'x'}])\n",
    "filepath store": "import bpy\nbpy.data.images[0].filepath = '/tmp/x.png'\n",
    "filepath_raw store": "import bpy\nbpy.data.images[0].filepath_raw = '/tmp/x.png'\n",
}

# A6 (MINOR): BaseException through mro(), endless C-level loop.
A6_HOSTILE = {
    "mro to BaseException": "B = Exception.mro()[1]\ntry:\n    x = 1\nexcept B:\n    x = 2\n",
    "except computed name": "E = (Exception,)\ntry:\n    x = 1\nexcept E[0]:\n    x = 2\n",
    "two-argument iter": "x = sum(iter(int, 1))\n",
    "iter alias": "it = iter\n",
}
A6_BENIGN = {
    "except Exception": "try:\n    x = 1\nexcept Exception:\n    x = 2\n",
    "except tuple": "try:\n    x = 1\nexcept (ValueError, KeyError) as e:\n    x = 2\n",
    "one-argument iter": "x = next(iter([1, 2]))\n",
}


class _Static(unittest.TestCase):
    def _all_refused(self, table):
        for name, src in table.items():
            with self.subTest(name=name):
                self.assertTrue(refused(src), "ACCEPTED: " + name)

    def _all_accepted(self, table):
        for name, src in table.items():
            with self.subTest(name=name):
                self.assertEqual(contract.check_script(src), [], name)


class A1TypesPoisoningTest(_Static):
    def test_types_poisoning_refused(self):
        self._all_refused(A1_HOSTILE)

    def test_benign_still_accepted(self):
        self._all_accepted(A1_BENIGN)

    def test_stub_and_genesis_scripts_still_valid(self):
        self.assertEqual(contract.check_script(contract.genesis_script()), [])
        for a in contract.AGENT_IDS:
            for rec in stub_agent.build_intents(a, 0):
                self.assertEqual(contract.validate_intent(rec), [], rec.get("event_id"))


class A3KeymapDriverTest(_Static):
    def test_keymap_and_driver_refused(self):
        self._all_refused(A3_HOSTILE)


class A2HostIoTest(_Static):
    def test_host_io_refused(self):
        self._all_refused(A2_HOSTILE)


class A6BudgetEvasionTest(_Static):
    def test_refused(self):
        self._all_refused(A6_HOSTILE)

    def test_benign(self):
        self._all_accepted(A6_BENIGN)


DRIVER = r"""
import os, sys
sys.path.insert(0, %(root)r)
import runner
STUBBORN = ("import signal, time, sys\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('up', flush=True)\n"
            "time.sleep(120)\n")
runner.agent_cmd = lambda *a, **k: [sys.executable, "-c", STUBBORN]
sys.exit(runner.run(%(rt)r, %(ev)r, ["modeler-agent"], stub=True, max_seconds=60,
                    do_init=True, log=lambda m: None))
"""


class A5SecondSignalTest(unittest.TestCase):
    """A second SIGTERM during the stop grace must not orphan the agents."""

    def setUp(self):
        self.rt = "%s-%s" % (RT_BASE, self.id().split(".")[-1])
        shutil.rmtree(self.rt, ignore_errors=True)
        os.makedirs(self.rt)
        self.ev = os.path.join(self.rt, "_evidence")
        self.child = None

    def tearDown(self):
        if self.child:
            pid, st = self.child
            if runner.alive(pid, st):
                runner.stop_pid(pid, st, grace=1)      # by PID + start time only
        shutil.rmtree(self.rt, ignore_errors=True)

    def test_second_sigterm_still_kills_and_writes_summary(self):
        drv = os.path.join(self.rt, "driver.py")
        with open(drv, "w") as fh:
            fh.write(DRIVER % {"root": ROOT, "rt": self.rt, "ev": self.ev})
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, drv], env=env, start_new_session=True)
        try:
            pids = os.path.join(self.ev, "pids.json")
            stream = os.path.join(self.ev, "modeler-agent", "agy_stream.jsonl")
            t0 = time.time()
            while time.time() - t0 < 15:
                if os.path.exists(pids) and os.path.exists(stream) and os.path.getsize(stream):
                    break
                time.sleep(0.1)
            with open(pids) as fh:
                info = json.load(fh)["agents"]["modeler-agent"]
            self.child = (info["pid"], info["starttime"])
            self.assertTrue(runner.alive(*self.child))
            os.kill(p.pid, signal.SIGTERM)
            time.sleep(1.0)                 # inside the 5 s SIGTERM grace of stop_all
            os.kill(p.pid, signal.SIGTERM)
            time.sleep(0.3)
            os.kill(p.pid, signal.SIGINT)
            try:
                rc = p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.fail("runner did not exit")
            self.assertEqual(rc, 1)         # interrupted run, clean exit (not 130 / traceback)
            self.assertFalse(runner.alive(*self.child), "agent orphaned")
            self.assertEqual(runner.group_pids(self.child[0]), [])
            with open(os.path.join(self.ev, "summary.json")) as fh:
                s = json.load(fh)
            self.assertTrue(s["interrupted"])
            self.assertFalse(s["ok"])
            with open(pids) as fh:
                self.assertEqual(json.load(fh)["agents"]["modeler-agent"]["stopped_by_runner"], "kill")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


class A4ClaimsPinTest(unittest.TestCase):
    """claims.json sits in the agents' writable RT: a rewrite during the run fails the run."""

    def setUp(self):
        self.rt = "%s-%s" % (RT_BASE, self.id().split(".")[-1])
        shutil.rmtree(self.rt, ignore_errors=True)
        os.makedirs(self.rt)
        self.ev = os.path.join(self.rt, "_evidence")
        self._cmd = runner.agent_cmd

    def tearDown(self):
        runner.agent_cmd = self._cmd
        shutil.rmtree(self.rt, ignore_errors=True)

    def _run(self):
        runner.init_rt(self.rt)
        make_frame.publish(self.rt, 0)
        return runner.run(self.rt, self.ev, ["modeler-agent"], stub=True, max_seconds=30,
                          do_init=True, log=lambda m: None)

    def test_claims_rewrite_fails_the_run(self):
        swapped = dict(contract.DEFAULT_CLAIMS)
        swapped["modeler-agent"], swapped["props-agent"] = (swapped["props-agent"],
                                                            swapped["modeler-agent"])
        code = ("import json,subprocess,sys\n"
                "json.dump(%r, open(%r, 'w'))\n"
                "sys.exit(subprocess.call(%r))\n"
                % (swapped, os.path.join(self.rt, "claims.json"),
                   self._cmd("modeler-agent", self.rt, self.ev, "", True)))
        runner.agent_cmd = lambda *a, **k: [sys.executable, "-c", code]
        self.assertEqual(self._run(), 1)
        with open(os.path.join(self.ev, "summary.json")) as fh:
            s = json.load(fh)
        self.assertFalse(s["ok"])
        self.assertTrue(s["claims_changed"])

    def test_control_untouched_claims_pass(self):
        self.assertEqual(self._run(), 0)
        with open(os.path.join(self.ev, "summary.json")) as fh:
            self.assertFalse(json.load(fh)["claims_changed"])


if __name__ == "__main__":
    unittest.main()
