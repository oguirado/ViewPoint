"""LIVE test: real osb (room viewpoint-capsule-test) and real context-wall MCP
(TaskLight room "demo"). Skipped unless VP_LIVE=1. Saves evidence/ ."""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVID = os.path.join(ROOT, "evidence")
OSB = ["python3", "-I", os.environ.get("VP_OSB_CLI", "switchboard-skill/bin/osb")]
OSB_ROOM, TL_ROOM = "viewpoint-capsule-test", "demo"
AGENTS = {"modeler-agent", "props-agent", "look-agent", "camera-agent"}


@unittest.skipUnless(os.environ.get("VP_LIVE") == "1", "set VP_LIVE=1 to post to the real rooms")
class TestLive(unittest.TestCase):
    def test_live_projection(self):
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())
        rt = "/dev/shm/vqa-cu/ref-f3-live-%d" % os.getpid()
        os.makedirs(rt)
        self.addCleanup(shutil.rmtree, rt, True)
        shutil.copy(os.path.join(HERE, "fixtures", "events_complete.jsonl"), os.path.join(rt, "events.jsonl"))
        with open(os.path.join(rt, "claims.json"), "w") as f:
            json.dump({"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS"],
                       "look-agent": ["col:LOOK"], "camera-agent": ["col:CAMERA"]}, f)
        before = subprocess.run(OSB + ["read", "--room", OSB_ROOM, "--json"], env=env, capture_output=True, text=True)
        n_before = len(before.stdout.splitlines()) if before.returncode == 0 else 0

        p = subprocess.run([sys.executable, os.path.join(ROOT, "project.py"), "--rt", rt, "--once",
                            "--osb-room", OSB_ROOM, "--tasklight-room", TL_ROOM],
                           env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        summary = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((summary["osb_errors"], summary["tasklight_errors"]), (0, 0))
        self.assertEqual(set(summary["agents"]), AGENTS)
        self.assertTrue(all(a["closed"] for a in summary["agents"].values()))

        with open(os.path.join(rt, "projection", "ledger.jsonl")) as f:
            ledger = [json.loads(l) for l in f]
        tools = set(ledger[0]["handshake"]["tools/list"]["tools"][i]["name"]
                    for i in range(len(ledger[0]["handshake"]["tools/list"]["tools"])))
        self.assertTrue({"post_plan", "post_step", "post_status", "close_task"} <= tools)
        tl = [r for r in ledger if r.get("sink") == "tasklight" and "tool" in r]
        self.assertTrue(all(r["ok"] and r["response"].get("isError") is False for r in tl))
        self.assertEqual({r["agent"] for r in tl if r["tool"] == "close_task"}, AGENTS)
        osb = [r for r in ledger if r.get("sink") == "osb"]
        self.assertEqual(len(osb), 20)
        self.assertTrue(all(r["rc"] == 0 and json.loads(r["stdout"])["id"] for r in osb))

        # The room itself, read back: every act we posted is there, refutes carry a ref.
        after = subprocess.run(OSB + ["read", "--room", OSB_ROOM, "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(after.returncode, 0, after.stderr)
        room = [json.loads(l) for l in after.stdout.splitlines()]
        self.assertEqual(len(room) - n_before, 20)
        posted_ids = {json.loads(r["stdout"])["id"] for r in osb}
        self.assertTrue(posted_ids <= {e.get("id") for e in room})
        refutes = [e for e in room if e.get("id") in posted_ids and e.get("act") == "refute"]
        self.assertEqual(len(refutes), 2)
        for e in refutes:
            self.assertIn("-agent-1", json.dumps(e))

        rec = subprocess.run([sys.executable, os.path.join(ROOT, "reconcile.py"), "--events",
                              os.path.join(rt, "events.jsonl"), "--ledger",
                              os.path.join(rt, "projection", "ledger.jsonl"), "--room", TL_ROOM],
                             capture_output=True, text=True)
        self.assertEqual(rec.returncode, 0, rec.stdout)

        # Evidence.
        os.makedirs(EVID, exist_ok=True)
        shutil.copy(os.path.join(rt, "projection", "ledger.jsonl"), os.path.join(EVID, "live_ledger.jsonl"))
        with open(os.path.join(EVID, "mcp_responses.jsonl"), "w") as f:
            for r in ledger:
                if r.get("sink") == "tasklight":
                    f.write(json.dumps(r, sort_keys=True) + "\n")
        with open(os.path.join(EVID, "live_summary.json"), "w") as f:
            json.dump({"projector": summary, "reconciler": json.loads(rec.stdout),
                       "osb_room": OSB_ROOM, "tasklight_room": TL_ROOM,
                       "osb_events_before": n_before, "osb_events_after": len(room)}, f, indent=1, sort_keys=True)
        m = subprocess.run(OSB + ["mirror", "--room", OSB_ROOM, "--out", os.path.join(EVID, "osb_mirror")],
                           env=env, capture_output=True, text=True)
        self.assertEqual(m.returncode, 0, m.stderr)
        self.assertTrue(os.path.exists(os.path.join(EVID, "osb_mirror", "LOG.md")))


if __name__ == "__main__":
    unittest.main()
