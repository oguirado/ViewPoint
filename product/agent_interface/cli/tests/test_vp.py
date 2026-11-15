#!/usr/bin/env python3
"""Tests of the vp dispatcher (Track P, lane ops-cli). Stdlib unittest; no Blender, VM or container is started.

Run:  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
        python3 -I product/agent_interface/cli/tests/test_vp.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.join(os.path.dirname(HERE), "vp")
TMP = tempfile.mkdtemp(prefix="vpops-test-")
ENV = dict(os.environ, VP_OPS_STATE=os.path.join(TMP, "state"), XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()))
ROOM = "viewpoint-agentops"


def vp(*args, env=None):
    p = subprocess.run(["python3", "-I", VP, *args], capture_output=True, text=True, env=env or ENV, timeout=180)
    lines = p.stdout.strip().splitlines()
    rec = json.loads(lines[-1])
    assert set(rec) == {"ok", "code", "rc", "data", "next"}, rec
    assert rec["rc"] == p.returncode, (rec["rc"], p.returncode)
    assert rec["ok"] == (rec["rc"] == 0)
    assert len(lines[-1]) <= 12500
    assert len("\n".join(lines[:-1])) <= 4200, "human text is bounded"
    return rec, p


def design():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader("vpmod", VP)
    spec = importlib.util.spec_from_loader("vpmod", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


M = design()
SAMPLE = {"job": "vpopsT", "display": ":97", "pid": "999999", "rt": os.path.join(TMP, "rt"), "kind": "digest", "op": "stop",
          "step": "plan", "url": "https://example.com", "to": "last_good", "turns": "1", "tick": "1", "evidence": os.path.join(TMP, "ev"),
          "cmd": "true", "path": "/etc/hostname", "glb": os.path.join(TMP, "a.glb"), "outdir": os.path.join(TMP, "rec")}


class DryRunEveryVerb(unittest.TestCase):
    def test_every_command_dry_run(self):
        tools = M.design_tools()
        keys = sorted("%s %s" % k for k in M.VERBS)
        self.assertGreaterEqual(len(keys), 88)
        if tools:
            self.assertEqual(sorted(set(tools) - set(keys)), [], "every design command is implemented")
        ran = 0
        for key in keys:
            t = tools.get(key, {"inputSchema": {"required": [], "properties": {}}, "positional": []})
            pos, flags = [], []
            for r in t["inputSchema"].get("required", []):
                if r == "confirm":
                    continue
                v = SAMPLE.get(r, "x")
                if r in (t.get("positional") or []):
                    pos.append(v)
                else:
                    flags += ["--" + r.replace("_", "-"), v]
            extra = ["--foreign-ok", "vpopsT", "--foreign-ok", "999999"]
            if key == "blender exec":
                extra += ["--bridge", "hybrid", "--allow-hybrid"]
            if key == "queue enqueue":
                extra += ["--file", os.path.join(TMP, "intent.json")]
            rec, p = vp(*key.split(), *pos, *flags, *extra, "--dry-run")
            self.assertEqual((rec["code"], rec["rc"]), ("DRY_RUN", 0), (key, rec))
            self.assertIsInstance(rec["data"]["argv"], list, key)
            if M.VERBS[tuple(key.split())]["kind"] == "D":
                self.assertTrue(rec["data"]["confirm_id"], key)
                self.assertIn("--confirm", rec["next"][0])
            ran += 1
        self.assertEqual(ran, len(keys))
        self.assertFalse(os.path.exists(os.path.join(ENV["VP_OPS_STATE"], "jobs.json")), "dry-run never writes the ledger")


class RealReadOnly(unittest.TestCase):
    def test_sys(self):
        rec, _ = vp("sys", "doctor")
        self.assertEqual(rec["code"], "OK", rec)
        self.assertEqual(rec["data"]["missing"], [])
        cov = rec["data"]["coverage"]
        if cov:
            self.assertEqual(cov["not_implemented"], [])
        self.assertIn("npy", vp("sys", "paths")[0]["data"]["paths"])
        self.assertEqual(vp("sys", "jobs")[0]["code"], "OK")
        h = vp("sys", "help", "vision", "stop")[0]["data"]
        self.assertEqual(h["command_class"], "D")
        self.assertEqual(vp("sys", "help", "nope", "nope")[0]["rc"], 5)

    def test_vision_doctor_real(self):
        rec, _ = vp("vision", "doctor")
        self.assertEqual(rec["code"], "OK", rec)
        self.assertEqual(rec["data"]["spooler"], "newton-py")
        self.assertRegex(rec["data"]["spool"]["sha256"], "^[0-9a-f]{64}$")

    def test_room_read_real(self):
        rec, p = vp("room", "read", "--room", ROOM, "--since", "1")
        self.assertEqual(rec["code"], "OK", rec)
        self.assertGreater(rec["data"]["spool"]["lines"], 0)
        rec, _ = vp("room", "read", "--room", "vpops-no-such-room")   # negative: osb rc 4
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["data"]["upstream_rc"], 4)
        self.assertEqual(vp("room", "status", "--room", ROOM)[0]["code"], "OK")

    def test_docs_real(self):
        rec, p = vp("docs", "route", "take a screenshot of a GUI")
        self.assertEqual(rec["code"], "OK", rec)
        self.assertIn("vision", p.stdout)
        if any(os.path.isfile(M.P[k]) for k in ("docs_router", "docs_router_alt")):
            rec, _ = vp("docs", "card")
            self.assertEqual(rec["code"], "OK", rec)

    def test_docs_file_find_stats_wired(self):
        # xr-ops-docs A3: router GUIDE documents `vp docs file|find|stats`; they must dispatch, not return USAGE.
        for verb, arg in (("file", ["README.md"]), ("find", ["VERBS"]), ("stats", [])):
            self.assertIn(("docs", verb), M.VERBS, verb)
            rec, _ = vp("docs", verb, *arg, "--dry-run")
            self.assertEqual((rec["code"], rec["rc"]), ("DRY_RUN", 0), verb)
            self.assertEqual(rec["data"]["argv"][3], verb)

    def test_docstring_names_real_nouns(self):
        # xr-ops-docs A6: every `vp <noun> <verb>` in the module docstring resolves.
        import re
        nouns = {n for n, _ in M.VERBS}
        for noun in re.findall(r"`vp ([a-z]+) [a-z]+`", M.__doc__):
            self.assertIn(noun, nouns)

    def test_no_host_paths_in_cli(self):
        # xr-ops-docs A1: the repo's own public gate must find nothing under cli/.
        root = os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
        gate = os.path.join(root, "tools", "check_public.sh")
        if not os.path.isfile(gate):
            self.skipTest("tools/check_public.sh absent")
        out = subprocess.run([gate, "--no-history"], cwd=root, capture_output=True, text=True).stdout
        bad = [l for l in out.splitlines() if "\tproduct/agent_interface/cli/" in l]
        self.assertEqual(bad, [])

    def test_defaults_follow_the_checkout(self):
        # A1: ref_* defaults resolve inside this checkout when the unit is merged there, else under $VP_OPS_ROOT.
        r = subprocess.run(["python3", "-I", "-c", "import importlib.machinery as m,sys;v=m.SourceFileLoader('v',sys.argv[1]).load_module();print(v.DEFAULT_PATHS['ref_capsule_dir'])", VP],
                           capture_output=True, text=True, env=dict(ENV, VP_OPS_ROOT="/srv/ws"))
        # In the assembled release ref_capsule is merged next to agent_interface, so the in-checkout branch applies.
        here = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(VP)), "..", "..", "ref_capsule"))
        self.assertEqual(r.stdout.strip(), here if os.path.isdir(here) else "/srv/ws/vp-wt-fable-ct/product/ref_capsule")

    def test_out_paging_real(self):
        rec, p = vp("out", "sh", "--cmd", "seq 1 500")
        self.assertEqual(rec["code"], "OK", rec)
        self.assertEqual(rec["data"]["spool"]["lines"], 500)
        self.assertLessEqual(len(p.stdout.splitlines()), 8, "500 lines are spooled, not printed")
        nxt = rec["next"][0].split()
        self.assertEqual(nxt[:3], ["vp", "out", "read"])
        page, p2 = vp(*nxt[1:])
        self.assertEqual(page["code"], "OK")
        self.assertIn("60: 60", p2.stdout)
        self.assertEqual(vp("out", "tokens", "/etc/hostname")[0]["code"], "OK")
        self.assertEqual(vp("out", "read", os.path.join(TMP, "absent.txt"))[0]["ok"], False)

    def test_ref_units_real(self):
        rec, p = vp("rewind", "script", "--kind", "digest")
        self.assertEqual(rec["code"], "OK", rec)
        rec, _ = vp("rewind", "status", "--rt", os.path.join(TMP, "no-rt"))   # negative: no runtime root
        self.assertIn(rec["rc"], (0, 1, 5, 9))
        rec, _ = vp("agents", "briefs")
        self.assertIn(rec["rc"], (0, 1, 9))


class Negative(unittest.TestCase):
    def test_display_1_refused(self):
        for a in (["vision", "start", "--job", "vpopsT", "--app", "DISPLAY=:1 xterm"],
                  ["record", "start", "--display", ":1", "--name", "vpopsT", "--outdir", TMP],
                  ["record", "start", "--display", ":1.0", "--name", "vpopsT", "--outdir", TMP, "--dry-run"],
                  ["out", "sh", "--cmd", "xdotool --display :1 key a"]):
            rec, _ = vp(*a)
            self.assertEqual((rec["code"], rec["rc"]), ("FORBIDDEN_DISPLAY", 3), a)
        rec, _ = vp("record", "start", "--display", ":97", "--name", "vpopsT", "--outdir", TMP, "--dry-run")  # control
        self.assertEqual(rec["code"], "DRY_RUN")
        rec, _ = vp("record", "start", "--display", ":1", "--name", "vpopsT", "--outdir", TMP, "--allow-display", ":1", "--dry-run")
        self.assertEqual(rec["code"], "DRY_RUN", "explicit override is honoured (dry-run only here)")

    def test_env_display_1_scrubbed(self):
        rec, p = vp("out", "sh", "--cmd", "echo D=[${DISPLAY:-unset}]", env=dict(ENV, DISPLAY=":1"))
        self.assertIn("D=[unset]", p.stdout)
        self.assertEqual(rec["data"]["env_scrubbed"], ["DISPLAY"])

    def test_hybrid_path_refused(self):
        a = ["queue", "status", "--rt", "/dev/shm/vqa-cu/hybrid/rt"]
        self.assertEqual(vp(*a)[0]["code"], "FORBIDDEN_PATH")
        self.assertEqual(vp(*a, "--allow-hybrid", "--dry-run")[0]["code"], "DRY_RUN")
        self.assertEqual(vp("blender", "start", "--job", "vpopsT", "--bridge", "hybrid")[0]["code"], "FORBIDDEN_PATH")

    def test_secret_paths_refused(self):
        for path in (os.path.expanduser("~/.gemini/settings.json"), "/tmp/x/credentials.json", "/home/user/." + "ssh/id_ed25519", "./.env"):
            rec, _ = vp("out", "read", path)
            self.assertEqual((rec["code"], rec["rc"]), ("SECRET_PATH", 3), path)

    def test_confirm_gate(self):
        rec, _ = vp("capsule", "stop")
        self.assertEqual((rec["code"], rec["rc"]), ("CONFIRM_REQUIRED", 4))
        self.assertEqual(rec["data"]["confirm_id"], "take")
        self.assertEqual(vp("capsule", "stop", "--confirm", "nope")[0]["rc"], 4)
        self.assertEqual(vp("identity", "delete", "--id", "vpopsid", "--confirm", "other")[0]["rc"], 4)
        self.assertEqual(vp("vision", "sweep")[0]["rc"], 4)
        rec, _ = vp("rewind", "restore", "--job", "vpopsT", "--rt", TMP, "--to", "p1", "--foreign-ok", "vpopsT")
        self.assertEqual((rec["rc"], rec["data"]["confirm_id"]), (4, "p1"))

    def test_foreign_jobs_refused(self):
        self.assertEqual(vp("vision", "stop", "--job", "someone-elses", "--confirm", "someone-elses")[0]["code"], "FOREIGN_JOB")
        self.assertEqual(vp("vision", "look", "--job", "someone-elses")[0]["code"], "FOREIGN_JOB")
        self.assertEqual(vp("vision", "start", "--job", "notmine", "--app", "true")[0]["code"], "FOREIGN_JOB")
        self.assertEqual(vp("record", "stop", "--pid", "1234567", "--confirm", "1234567")[0]["code"], "FOREIGN_JOB")
        rec, _ = vp("record", "stop", "--pid", "1234567", "--confirm", "1234567", "--foreign-ok", "1234567")
        self.assertEqual((rec["code"], rec["rc"]), ("NOT_FOUND", 5), "explicit foreign pid that does not exist")
        rec, _ = vp("vision", "stop", "--job", "someone-elses", "--dry-run")
        self.assertEqual((rec["code"], rec["data"]["would_refuse"]), ("DRY_RUN", ["FOREIGN_JOB"]), "dry run reports, starts nothing")
        self.assertEqual(vp("agents", "run", "--rt", TMP, "--evidence", TMP, "--kill-all", "--impl", "swarm")[0]["rc"], 2)

    def test_secrets_redacted(self):
        fake = ["ghp_" + "A1b2C3d4E5" * 3, "sk-" + "abcdefgh12345678XYZ0", "hunter2hunter2"]
        cmd = "echo token: %s; echo OPENAI_API_KEY=%s; echo '{\"password\": \"%s\", \"ok\": true}'" % tuple(fake)
        rec, p = vp("out", "sh", "--cmd", cmd, "--dry-run")
        rec2, p2 = vp("out", "sh", "--cmd", "printf '%s\\n' \"$S1\" \"key=$S2\" \"{\\\"password\\\": \\\"$S3\\\", \\\"ok\\\": true}\"",
                      env=dict(ENV, S1=fake[0], S2=fake[1], S3=fake[2]))
        self.assertEqual(rec2["code"], "OK", rec2)
        for out in (p.stdout + p.stderr, p2.stdout + p2.stderr):
            for f in fake:
                self.assertNotIn(f, out)
        self.assertGreaterEqual(rec2["data"]["redactions"], 2)
        self.assertIn("[REDACTED]", p2.stdout)
        rec3, p3 = vp("out", "sh", "--cmd", "echo plain words stay")       # control: no false redaction
        self.assertNotIn("redactions", rec3["data"])
        self.assertIn("plain words stay", p3.stdout)

    def test_usage_and_upstream_codes(self):
        self.assertEqual(vp("nope", "nope")[0]["rc"], 2)
        self.assertEqual(vp("vision")[0]["rc"], 2)
        rec, _ = vp("vision", "look")
        self.assertEqual((rec["code"], rec["rc"]), ("USAGE", 2))
        self.assertIn("--job", rec["data"]["missing"])
        self.assertEqual(vp("out", "sh", "--cmd", "true", "--tail", "abc")[0]["rc"], 2)
        rec, _ = vp("out", "sh", "--cmd", "exit 7")
        self.assertEqual((rec["code"], rec["rc"], rec["data"]["upstream_rc"]), ("UPSTREAM", 9, 7))
        rec, _ = vp("out", "sh", "--cmd", "exit 1")
        self.assertEqual((rec["code"], rec["rc"]), ("FAIL", 1))
        rec, _ = vp("out", "sh", "--cmd", "sleep 20", "--timeout-s", "1")
        self.assertEqual((rec["code"], rec["rc"]), ("TIMEOUT", 124))
        rec, _ = vp("out", "sh", "--cmd", "true", env=dict(ENV, VP_OPS_NPY="/nonexistent/newton"))   # builtin spooler fallback
        self.assertEqual((rec["code"], rec["data"]["spooler"]), ("OK", "builtin"))
        rec, _ = vp("vision", "doctor", env=dict(ENV, VP_OPS_VISION="/nonexistent/vision"))
        self.assertEqual((rec["code"], rec["rc"]), ("DEPENDENCY", 8))

    def test_json_flag_one_line(self):
        rec, p = vp("out", "sh", "--cmd", "echo hello", "--json")
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)
        self.assertEqual(p.stderr, "")


class Lifecycle(unittest.TestCase):
    def test_enqueue_atomic_and_refusals(self):
        rt = os.path.join(TMP, "rt-enq")
        os.makedirs(rt)
        f = os.path.join(TMP, "intent.json")
        with open(f, "w") as h:
            json.dump({"agent_id": "modeler-agent", "event_id": "vpops-e1", "kind": "script"}, h)
        rec, _ = vp("queue", "enqueue", "--rt", rt, "--agent", "modeler-agent", "--file", f)
        self.assertEqual(rec["code"], "OK", rec)
        dst = os.path.join(rt, "intents", "modeler-agent", "vpops-e1.json")
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(os.listdir(os.path.dirname(dst)), ["vpops-e1.json"], "no tmp file left")
        self.assertFalse(vp("queue", "enqueue", "--rt", rt, "--agent", "modeler-agent", "--file", f)[0]["ok"], "duplicate")
        self.assertFalse(vp("queue", "enqueue", "--rt", rt, "--agent", "camera-agent", "--file", f)[0]["ok"], "agent mismatch")
        self.assertFalse(vp("queue", "enqueue", "--rt", os.path.join(TMP, "nope"), "--agent", "modeler-agent", "--file", f)[0]["ok"])
        self.assertEqual(vp("queue", "enqueue", "--rt", rt, "--agent", "modeler-agent")[0]["rc"], 5, "AUSENTE without --file")

    def test_detach_ledger_stop_by_pid(self):
        rec, _ = vp("out", "sh", "--cmd", "exec sleep 60", "--detach")
        self.assertEqual(rec["code"], "OK", rec)
        pid = rec["data"]["pid"]
        try:
            jobs = vp("sys", "jobs")[0]["data"]["jobs"]
            row = [j for j in jobs if j["id"] == str(pid)][0]
            self.assertTrue(row["alive"])
            self.assertEqual(vp("record", "stop", "--pid", str(pid))[0]["rc"], 4, "needs --confirm <pid>")
            self.assertTrue(os.path.exists("/proc/%d" % pid))
            rec, _ = vp("record", "stop", "--pid", str(pid), "--confirm", str(pid))
            self.assertEqual(rec["code"], "OK", rec)
            for _ in range(50):
                if not os.path.exists("/proc/%d" % pid) or open("/proc/%d/stat" % pid).read().split(")")[1].split()[0] == "Z":
                    break
                time.sleep(0.1)
            else:
                self.fail("pid still alive after SIGINT")
        finally:
            try:
                os.kill(pid, 15)
            except OSError:
                pass


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
