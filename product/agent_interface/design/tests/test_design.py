#!/usr/bin/env python3
"""Tests of the ops-design artifacts, with negative controls (Track P, claude-fable-5-2).

    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -I tests/test_design.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DESIGN = os.path.dirname(HERE)
sys.path.insert(0, DESIGN)
import caps  # noqa: E402
import gen  # noqa: E402
import validate_cmd as V  # noqa: E402


def run(*a, cwd=DESIGN):
    return subprocess.run([sys.executable, "-I"] + list(a), cwd=cwd, capture_output=True, text=True, timeout=60)


class Generated(unittest.TestCase):
    def test_no_drift(self):
        for script in ("gen.py", "caps.py"):
            r = run(script, "--check")
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_drift_is_detected(self):  # negative control
        d = tempfile.mkdtemp(prefix="vpops-design-")
        try:
            for f in ("gen.py", "COMMANDS.md", "MCP_TOOLS.json"):
                shutil.copy(os.path.join(DESIGN, f), d)
            self.assertEqual(run("gen.py", "--check", cwd=d).returncode, 0)
            with open(os.path.join(d, "COMMANDS.md"), "a") as f:
                f.write("tampered\n")
            r = run("gen.py", "--check", cwd=d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("DRIFT COMMANDS.md", r.stdout)
        finally:
            shutil.rmtree(d)


class Schema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(DESIGN, "MCP_TOOLS.json"), encoding="utf-8") as f:
            cls.doc = json.load(f)
        cls.tools = cls.doc["tools"]

    def test_names_unique_and_derived(self):
        names = [t["name"] for t in self.tools]
        self.assertEqual(len(names), len(set(names)))
        for t in self.tools:
            self.assertEqual(t["name"], "vp_%s_%s" % (t["argv"][0], t["argv"][1].replace("-", "_")))
            self.assertRegex(t["name"], r"^[a-z0-9_]{1,64}$")

    def test_schema_shape(self):
        for t in self.tools:
            s = t["inputSchema"]
            self.assertEqual(s["type"], "object")
            self.assertFalse(s["additionalProperties"])
            for r in s["required"] + t["positional"]:
                self.assertIn(r, s["properties"], t["name"])
            for p in s["properties"].values():
                self.assertIn(p["type"], ("string", "integer", "number", "boolean"))
                self.assertTrue(p["description"])
            self.assertIn("dry_run", s["properties"], t["name"])

    def test_destructive_needs_confirm(self):
        d = [t for t in self.tools if t["destructive"]]
        self.assertGreaterEqual(len(d), 8)
        for t in self.tools:
            self.assertEqual("confirm" in t["inputSchema"]["properties"], t["destructive"], t["name"])
            self.assertEqual(t["safety"] == "D", t["destructive"])
            self.assertEqual(t["annotations"]["readOnlyHint"], t["safety"] == "R")
            if t["destructive"]:
                self.assertIn("CONFIRM_REQUIRED", t["codes"])

    def test_codes_closed(self):
        codes, classes = self.doc["codes"], self.doc["rc_classes"]
        for c, rc in codes.items():
            self.assertIn(str(rc), classes, c)
        for t in self.tools:
            for c in t["codes"]:
                self.assertIn(c, codes, "%s uses undeclared code %s" % (t["name"], c))

    def test_core_listing_is_bounded(self):
        core = [{k: t[k] for k in ("name", "description", "inputSchema", "annotations")} for t in self.tools if t["tier"] == "core"]
        self.assertLess(len(json.dumps(core, separators=(",", ":"))), 50000)
        self.assertIn("vp_sys_help", [t["name"] for t in core])

    def test_commands_md_has_every_command(self):
        with open(os.path.join(DESIGN, "COMMANDS.md"), encoding="utf-8") as f:
            md = f.read()
        for t in self.tools:
            self.assertIn("#### `vp %s %s` " % tuple(t["argv"]), md)

    def test_required_families(self):
        nouns = {t["argv"][0] for t in self.tools}
        for n in ("capsule", "vision", "queue", "rewind", "agents", "record", "room", "docs", "blender", "browser", "human", "tasklight", "out"):
            self.assertIn(n, nouns)


class Capabilities(unittest.TestCase):
    def test_every_cited_command_exists(self):
        tools = V.load()
        with open(os.path.join(DESIGN, "CAPABILITIES.md"), encoding="utf-8") as f:
            md = f.read()
        cited = set(re.findall(r"`vp ([a-z]+) ([a-z*-]+)", md))
        self.assertGreater(len(cited), 40)
        nouns = {k[0] for k in tools}
        for noun, verb in cited:
            if verb == "*":
                self.assertIn(noun, nouns)
            else:
                self.assertTrue((noun, verb) in tools, "CAPABILITIES.md cites unknown vp %s %s" % (noun, verb))

    def test_every_wrapping_family_has_a_capability_row(self):
        with open(os.path.join(DESIGN, "CAPABILITIES.md"), encoding="utf-8") as f:
            md = f.read()
        for noun in {k[0] for k in V.load()} - {"sys"}:
            self.assertIn("`vp %s " % noun, md, noun)

    def test_no_host_paths_in_design(self):
        # xr-ops-docs A1: the repo's public gate finds nothing under design/.
        import subprocess
        root = os.path.normpath(os.path.join(DESIGN, "..", "..", ".."))
        gate = os.path.join(root, "tools", "check_public.sh")
        if not os.path.isfile(gate):
            self.skipTest("tools/check_public.sh absent")
        out = subprocess.run([gate, "--no-history"], cwd=root, capture_output=True, text=True).stdout
        bad = [l for l in out.splitlines() if "\tproduct/agent_interface/design/" in l]
        self.assertEqual(bad[:5], [])

    def test_citations_have_lines(self):
        with open(os.path.join(DESIGN, "CAPABILITIES.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertGreater(len(re.findall(r"`(?:\$VP_OPS_ROOT|~)/[^`]+:\d+`", md)), 50)
        self.assertNotIn("ANCHOR_LOST", md)
        self.assertNotIn("**AUSENTE**", md)

    def test_lost_anchor_and_missing_file(self):  # negative controls
        real = caps.ROWS[0][2]
        self.assertEqual(caps.locate(real, caps.ROWS[0][3])[1], "OK")
        self.assertEqual(caps.locate(real, "this anchor is not in the launcher 7f3a"), (None, "ANCHOR_LOST"))
        self.assertEqual(caps.locate("/nonexistent/vpops/file.py", "x"), (None, "AUSENTE"))


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = V.load()
        cls.path = os.path.join(DESIGN, "E2E_SCENARIOS.md")
        cls.rows = V.sh_lines(cls.path)

    def test_all_lines_valid(self):
        self.assertGreater(len(self.rows), 60)
        for n, s in self.rows:
            self.assertEqual(V.validate(s, self.tools), [], "line %d: %s" % (n, s))

    def test_five_scenarios_cover_the_brief(self):
        with open(self.path, encoding="utf-8") as f:
            md = f.read()
        self.assertEqual(len(re.findall(r"^## \d\. ", md, re.M)), 5)
        used = {tuple(s.split()[1:3]) for _, s in self.rows}
        for need in (("blender", "start"), ("record", "start"), ("record", "stop"), ("queue", "run"), ("queue", "enqueue"),
                     ("record", "compose"), ("tasklight", "start"), ("rewind", "restore"), ("rewind", "checkpoint"),
                     ("browser", "task"), ("browser", "read"), ("human", "handback"), ("human", "resume"), ("agents", "run")):
            self.assertIn(need, used)

    def test_forbidden_targets_only_with_override(self):
        for n, s in self.rows:
            if re.search(r"--display :1(\s|$)", s):
                self.assertIn("--allow-display :1", s, "line %d" % n)
            if "hybrid" in s:
                self.assertIn("--allow-hybrid", s, "line %d" % n)

    def test_validator_rejects(self):  # negative controls
        bad = {
            "vp queue fly --rt /x": "unknown command",
            "vp queue run --job vpops1 --rt /x --turns 3 --warp 9": "unknown flag",
            "vp queue run --job vpops1 --rt /x": "missing required turns",
            "vp queue run --job vpops1 --rt /x --turns many": "needs an integer",
            "vp rewind restore --job vpops1 --rt /x --to last_good": "without --confirm",
            "vp vision stop --job vpops1": "without --confirm",
            "vp capsule stop": "without --confirm",
            "vp room post --room r --agent a --act claim --text": "needs a value",
            "vp docs ask": "missing required query",
            "ls -la": "not a `vp",
        }
        for line, want in bad.items():
            errs = V.validate(line, self.tools)
            self.assertTrue(any(want in e for e in errs), "%s -> %s" % (line, errs))
        ok = ["vp rewind restore --job vpops1 --rt /x --to last_good --dry-run",
              "vp vision stop --job vpops1 --confirm vpops1", "vp docs ask 'how do I rewind'", "vp out read /tmp/x 1:60"]
        for line in ok:
            self.assertEqual(V.validate(line, self.tools), [], line)

    def test_cli_rc(self):
        self.assertEqual(run("validate_cmd.py", "--file", "E2E_SCENARIOS.md").returncode, 0)
        self.assertEqual(run("validate_cmd.py", "vp queue fly").returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
