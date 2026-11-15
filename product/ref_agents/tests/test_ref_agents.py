#!/usr/bin/env python3
"""Tests for unit F4 (agent runner, briefs, intent validator). Stub mode only: no model.

Run (from product/ref_agents):
  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
      python3 -m unittest discover -s tests -v
"""
import copy
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
sys.path.insert(0, os.path.join(ROOT, "tools"))
import briefs  # noqa: E402
import contract  # noqa: E402
import make_frame  # noqa: E402
import runner  # noqa: E402
import stub_agent  # noqa: E402

RT_BASE = "/dev/shm/vqa-cu/ref-f4-test-%d" % os.getpid()


def good_script():
    return stub_agent.build_intents("modeler-agent", 0)[0]


def good_gui():
    return stub_agent.build_intents("camera-agent", 0)[0]


class ValidatorTest(unittest.TestCase):
    def test_all_stub_intents_valid(self):
        n = 0
        for a in contract.AGENT_IDS:
            for rec in stub_agent.build_intents(a, 3):
                self.assertEqual(contract.validate_intent(
                    rec, agent_id=a, event_id=rec["event_id"], newest=3), [], rec["event_id"])
                n += 1
        self.assertEqual(n, 10)

    def test_negative_control_foreign_collection_rejected(self):
        """NEGATIVE CONTROL: modeler-agent writing props-agent's collection is refused.
        Goes red if the write_set-inside-claim check is removed from validate_intent."""
        rec = good_script()
        rec["write_set"] = ["col:CHAR_props"]
        errs = contract.validate_intent(rec)
        self.assertTrue(errs, "foreign write_set was accepted")
        self.assertTrue(any("CLAIM_REFUSED" in e and "CHAR_props" in e for e in errs), errs)
        # the same record is accepted once the claim table grants that collection:
        # the rejection above is caused by the claim check and by nothing else.
        granted = copy.deepcopy(contract.DEFAULT_CLAIMS)
        granted["modeler-agent"] = ["col:CHAR_mesh", "col:CHAR_props"]
        self.assertEqual(contract.validate_intent(rec, granted), [])

    def test_own_plus_foreign_rejected(self):
        rec = good_script()
        rec["write_set"] = ["col:CHAR_mesh", "col:CHAR_look"]
        self.assertTrue(any("CLAIM_REFUSED" in e for e in contract.validate_intent(rec)))

    def test_missing_fields(self):
        for k in contract.REQUIRED:
            rec = good_script()
            del rec[k]
            errs = contract.validate_intent(rec)
            self.assertTrue(any(e.startswith(k + ":") for e in errs), (k, errs))

    def test_lane_kind_consistency(self):
        rec = good_script()
        rec["kind"] = "key"
        self.assertTrue(any("not valid on lane" in e for e in contract.validate_intent(rec)))
        rec = good_gui()
        rec["kind"] = "job"
        self.assertTrue(any("not valid on lane" in e for e in contract.validate_intent(rec)))
        rec = good_script()
        rec["lane"] = "control"
        self.assertTrue(any(e.startswith("lane:") for e in contract.validate_intent(rec)))

    def test_script_must_compile(self):
        rec = good_script()
        rec["script"] = "import bpy\ndef broken(:\n    pass\n"
        self.assertTrue(any("does not compile" in e for e in contract.validate_intent(rec)))
        rec["script"] = ""
        self.assertTrue(any(e.startswith("script:") for e in contract.validate_intent(rec)))
        del rec["script"]
        self.assertTrue(any(e.startswith("script:") for e in contract.validate_intent(rec)))

    def test_script_host_access_rejected(self):
        for bad in ("import os\nos.system('true')\n", "import subprocess\n",
                    "from pathlib import Path\n", "open('/etc/passwd')\n",
                    "import bpy\nbpy.ops.wm.quit_blender()\n", "__import__('os')\n"):
            rec = good_script()
            rec["script"] = bad
            self.assertTrue(contract.validate_intent(rec), bad)

    def test_gui_rules(self):
        self.assertEqual(contract.validate_intent(good_gui()), [])
        rec = good_gui()
        rec["keys"] = ["KP_1", "ctrl+q"]
        self.assertTrue(any("ctrl+q" in e for e in contract.validate_intent(rec)))
        rec = good_gui()
        rec["keys"] = ["KP_4"] * 9
        self.assertTrue(any("acts" in e for e in contract.validate_intent(rec)))
        rec = good_gui()
        rec["keys"] = []
        self.assertTrue(contract.validate_intent(rec))
        rec = good_gui()
        rec["script"] = "import bpy\n"
        self.assertTrue(any("not allowed on lane gui" in e for e in contract.validate_intent(rec)))
        # gui lane is the camera agent's only
        rec = good_gui()
        rec["agent_id"], rec["event_id"] = "props-agent", "props-agent-1"
        self.assertTrue(any("reserved" in e for e in contract.validate_intent(rec)))
        # xy
        rec = good_gui()
        del rec["keys"]
        rec.update(kind="xy", x=800, y=500)
        self.assertEqual(contract.validate_intent(rec), [])
        rec["x"] = "800"
        self.assertTrue(contract.validate_intent(rec))

    def test_identity_and_frame(self):
        rec = good_script()
        self.assertTrue(contract.validate_intent(rec, agent_id="look-agent"))
        self.assertTrue(contract.validate_intent(rec, event_id="modeler-agent-9"))
        rec["event_id"] = "props-agent-1"
        self.assertTrue(any("prefix" in e for e in contract.validate_intent(rec)))
        rec = good_script()
        rec["based_on_frame"] = 7
        self.assertTrue(any("newer" in e for e in contract.validate_intent(rec, newest=3)))
        rec["based_on_frame"] = True
        self.assertTrue(contract.validate_intent(rec))
        rec = good_script()
        rec["rec"] = "close"
        self.assertTrue(contract.validate_intent(rec))
        rec = good_script()
        rec["v"] = 2
        self.assertTrue(contract.validate_intent(rec))
        rec = good_script()
        rec["expect"] = " "
        self.assertTrue(contract.validate_intent(rec))
        self.assertTrue(contract.validate_intent([1, 2]))

    def test_foreign_reference_warning(self):
        rec = good_script()
        self.assertEqual(contract.foreign_collections(
            rec["script"], "modeler-agent", contract.DEFAULT_CLAIMS), [])
        s = rec["script"] + "\nx = bpy.data.collections.get('CHAR_props')\n"
        self.assertEqual(contract.foreign_collections(
            s, "modeler-agent", contract.DEFAULT_CLAIMS), ["CHAR_props"])


class BriefTest(unittest.TestCase):
    RT = "/dev/shm/vqa-cu/ref"

    def test_shared_prefix_byte_identical(self):
        prefix = briefs.shared_prefix(self.RT).encode("utf-8")
        self.assertTrue(prefix.endswith(b"<!-- CACHE_BREAKPOINT -->\n"))
        tails = set()
        for a in contract.AGENT_IDS:
            b = briefs.build_brief(a, self.RT).encode("utf-8")
            self.assertEqual(b[:len(prefix)], prefix, a)
            self.assertNotIn(b"{{", b)
            self.assertEqual(b.count(b"<!-- CACHE_BREAKPOINT -->"), 1)
            tails.add(b[len(prefix):])
            self.assertIn(("You are `%s`" % a).encode(), b[len(prefix):])
        self.assertEqual(len(tails), 4)

    def test_prefix_carries_the_contract(self):
        p = briefs.shared_prefix(self.RT)
        for needle in ("CHAR_mesh", "CHAR_props", "CHAR_look", "CHAR_cam", "Blender 4.0.2",
                       "based_on_frame", "write_set", "KP_1", "KP_3", "KP_4", "KP_Add", "Home",
                       "NON-INTERACTIVE", "Never call `vision`", self.RT + "/frames",
                       self.RT + "/intents", self.RT + "/inbox"):
            self.assertIn(needle, p)

    def test_example_json_in_prefix_matches_key_set(self):
        p = briefs.shared_prefix(self.RT)
        for k in contract.REQUIRED:
            self.assertIn('"%s"' % k, p)

    def test_bad_args(self):
        with self.assertRaises(ValueError):
            briefs.build_brief("director-agent", self.RT)
        with self.assertRaises(ValueError):
            briefs.build_brief("modeler-agent", self.RT, max_intents=9)


class RunnerUnitTest(unittest.TestCase):
    def test_real_command_shape(self):
        cmd = runner.agent_cmd("modeler-agent", "/dev/shm/vqa-cu/ref", "/ev", "BRIEF TEXT")
        self.assertEqual(cmd[0], runner.AGY)
        self.assertEqual(cmd[-2:], ["-p", "BRIEF TEXT"])          # -p LAST
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-4-hydrogen-high")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "1800s")
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_rt_guard(self):
        for bad in ("/dev/shm/vqa-cu/swarm", "/dev/shm/vqa-cu/hybrid", "/tmp/x",
                    "/dev/shm/vqa-cu/ref/../swarm"):
            with self.assertRaises(ValueError):
                runner.check_rt(bad)
        self.assertEqual(runner.check_rt("/dev/shm/vqa-cu/ref-x"), "/dev/shm/vqa-cu/ref-x")

    def test_nonce_never_reaches_agents(self):
        e = runner.child_env({"VP_SWARM_NONCE": "secret", "PATH": "/usr/bin"})
        self.assertNotIn("VP_SWARM_NONCE", e)
        self.assertEqual(e["PATH"], "/usr/bin")

    def test_stop_by_pid(self):
        p = subprocess.Popen(["sh", "-c", "sleep 300 & sleep 300"], start_new_session=True)
        try:
            st = runner.proc_starttime(p.pid)
            time.sleep(0.3)
            self.assertEqual(len(runner.group_pids(p.pid)), 3)
            # wrong start time (PID reuse guard): nothing is touched
            self.assertEqual(runner.stop_pid(p.pid, st + 1, grace=1), "gone")
            self.assertTrue(runner.alive(p.pid, st))
            self.assertEqual(runner.stop_pid(p.pid, st, grace=3), "term")
            p.wait(timeout=5)
            self.assertEqual(runner.group_pids(p.pid), [])
        finally:
            if p.poll() is None:
                p.kill()


class RunnerStubE2E(unittest.TestCase):
    def setUp(self):
        self.rt = "%s-%s" % (RT_BASE, self.id().split(".")[-1])
        shutil.rmtree(self.rt, ignore_errors=True)
        os.makedirs(self.rt)
        self.ev = os.path.join(self.rt, "_evidence")
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.rt, ignore_errors=True)

    def _run(self, **kw):
        kw.setdefault("stub", True)
        kw.setdefault("max_seconds", 30)
        kw.setdefault("do_init", True)
        return runner.run(self.rt, self.ev, kw.pop("agents", list(contract.AGENT_IDS)),
                          log=self.log.append, **kw)

    def test_four_stub_agents(self):
        runner.init_rt(self.rt)
        make_frame.publish(self.rt, 4)
        self.assertEqual(self._run(), 0, self.log)
        res = contract.validate_rt(self.rt)
        self.assertEqual(len(res), 10)
        self.assertEqual([e for e in res.values() if e], [])
        for p in res:
            with open(p) as fh:
                self.assertEqual(json.load(fh)["based_on_frame"], 4)
        with open(os.path.join(self.ev, "pids.json")) as fh:
            st = json.load(fh)
        self.assertEqual(sorted(st["agents"]), sorted(contract.AGENT_IDS))
        self.assertEqual(len({i["prefix_sha256"] for i in st["agents"].values()}), 1)
        for a, info in st["agents"].items():
            self.assertIsInstance(info["pid"], int)
            self.assertEqual(info["rc"], 0)
            self.assertFalse(runner.alive(info["pid"], info["starttime"]))
            self.assertGreater(os.path.getsize(os.path.join(self.ev, a, "agy_stream.jsonl")), 0)
            self.assertTrue(os.path.exists(os.path.join(self.ev, a, "prompt.md")))
        # agents side never produced broker-owned files
        for forbidden in ("inbox", "outbox", "events.jsonl", "cursor.jsonl"):
            self.assertFalse(os.path.exists(os.path.join(self.rt, forbidden)), forbidden)
        self.assertEqual(sorted(os.listdir(os.path.join(self.rt, "frames"))),
                         ["frame_4.json", "frame_4.png"])
        # CLI validator agrees
        r = subprocess.run([sys.executable, os.path.join(ROOT, "contract.py"), "--rt", self.rt],
                           stdout=subprocess.PIPE)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_second_run_continues_numbering(self):
        runner.init_rt(self.rt)
        make_frame.publish(self.rt, 0)
        self.assertEqual(self._run(agents=["props-agent"]), 0)
        self.assertEqual(self._run(agents=["props-agent"]), 0)
        names = sorted(os.listdir(os.path.join(self.rt, "intents", "props-agent")))
        self.assertEqual(names, ["props-agent-%d.json" % i for i in (1, 2, 3, 4)])

    def test_run_goes_red_on_invalid_intent(self):
        """NEGATIVE CONTROL at runner level: a foreign-collection intent on disk fails the run."""
        runner.init_rt(self.rt)
        make_frame.publish(self.rt, 0)
        rec = stub_agent.build_intents("modeler-agent", 0)[0]
        rec["event_id"] = "modeler-agent-99"
        rec["write_set"] = ["col:CHAR_cam"]
        stub_agent.write_intent(self.rt, rec)
        self.assertEqual(self._run(agents=["modeler-agent"]), 1)
        with open(os.path.join(self.ev, "summary.json")) as fh:
            s = json.load(fh)
        self.assertFalse(s["ok"])
        self.assertEqual(s["agents"]["modeler-agent"]["invalid"], 1)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "contract.py"), "--rt", self.rt],
                           stdout=subprocess.PIPE)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"CLAIM_REFUSED", r.stdout)

    def test_run_goes_red_without_frames(self):
        runner.init_rt(self.rt)
        self.assertEqual(self._run(agents=["look-agent"]), 1)   # stub exits 3, writes nothing

    def test_timeout_stops_agent_by_pid(self):
        runner.init_rt(self.rt)          # no frame: the stub waits for one
        orig = runner.agent_cmd
        runner.agent_cmd = lambda aid, rt, ev, brief, *a, **k: [
            sys.executable, os.path.join(ROOT, "stub_agent.py"), "--agent", aid, "--rt", rt,
            "--wait-frame-s", "120"]
        try:
            t0 = time.time()
            self.assertEqual(self._run(agents=["camera-agent"], max_seconds=1.0), 1)
            self.assertLess(time.time() - t0, 20)
        finally:
            runner.agent_cmd = orig
        with open(os.path.join(self.ev, "pids.json")) as fh:
            info = json.load(fh)["agents"]["camera-agent"]
        self.assertTrue(info["timed_out"])
        self.assertEqual(info["stopped_by_runner"], "term")
        self.assertFalse(runner.alive(info["pid"], info["starttime"]))


@unittest.skipUnless(shutil.which("blender"), "blender not installed")
class BlenderStubScripts(unittest.TestCase):
    """The stub bpy jobs really run in Blender 4.0.2 and stay inside their collection."""

    def test_scripts_run_and_respect_collections(self):
        rt = RT_BASE + "-blender"
        shutil.rmtree(rt, ignore_errors=True)
        os.makedirs(rt)
        try:
            jobs = []
            for a in contract.AGENT_IDS:
                for rec in stub_agent.build_intents(a, 0):
                    if rec["lane"] == "script":
                        jobs.append([rec["event_id"], rec["write_set"][0][4:], rec["script"]])
            # run every job twice (idempotence), in round-robin-ish order
            with open(os.path.join(rt, "jobs.json"), "w") as fh:
                json.dump(jobs + jobs, fh)
            driver = (
                "import bpy, json\n"
                "for o in list(bpy.data.objects): bpy.data.objects.remove(o)\n"
                "out = {'ran': [], 'errors': []}\n"
                "for eid, col, src in json.load(open(%r)):\n"
                "    try:\n"
                "        exec(compile(src, eid, 'exec'), {'__name__': '__vp_job__'})\n"
                "        out['ran'].append(eid)\n"
                "    except Exception as e:\n"
                "        out['errors'].append([eid, repr(e)])\n"
                "out['objects'] = {o.name: [c.name for c in o.users_collection] for o in bpy.data.objects}\n"
                "out['version'] = list(bpy.app.version)\n"
                "json.dump(out, open(%r, 'w'))\n"
            ) % (os.path.join(rt, "jobs.json"), os.path.join(rt, "out.json"))
            r = subprocess.run(["blender", "-b", "--factory-startup", "--python-expr", driver],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            self.assertEqual(r.returncode, 0, r.stdout[-800:])
            with open(os.path.join(rt, "out.json")) as fh:
                out = json.load(fh)
            self.assertEqual(out["errors"], [])
            self.assertEqual(len(out["ran"]), 2 * len(jobs))
            self.assertEqual(len(jobs), 7)
            per = {}
            for name, cols in out["objects"].items():
                self.assertEqual(len(cols), 1, (name, cols))
                self.assertTrue(name.startswith(cols[0] + "."), (name, cols))
                per[cols[0]] = per.get(cols[0], 0) + 1
            # 11 mesh parts, 4 props, 1 floor + 3 lights; no duplicates after the second pass
            self.assertEqual(per, {"CHAR_mesh": 11, "CHAR_props": 4, "CHAR_look": 4})
        finally:
            shutil.rmtree(rt, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
