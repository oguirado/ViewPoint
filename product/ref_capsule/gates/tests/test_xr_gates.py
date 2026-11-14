"""Regression tests for the xr-capsule-auction cross-review of the gates (C7, C8, C15).

Each case was GREEN (or waived) at 8408bbf. Forbidden strings are assembled by concatenation
so that the suite can scan its own sources.
Run (from product/ref_capsule/gates):  python3 -I -B tests/test_xr_gates.py
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import common  # noqa: E402
import g1_paths  # noqa: E402
import g3_memcaps  # noqa: E402
import g4_pidstops  # noqa: E402
import run_gates  # noqa: E402
from test_gates import Tmp, rules, SDRUN, PK, KA  # noqa: E402


class C7MissingEvidence(Tmp):
    def test_a_declared_log_that_does_not_exist_is_never_green(self):
        missing = os.path.join(self.d, "nope", "*_stream.jsonl")
        ok_src = self.src("ok.sh", "#!/bin/bash\n" + SDRUN + "true\n")
        for gate, r in (("G1", g1_paths.run([missing], [ok_src])), ("G3", g3_memcaps.run([missing], [ok_src])),
                        ("G4", g4_pidstops.run([missing], [ok_src]))):
            self.assertEqual((r["status"], sorted({f["rule"] for f in r["findings"]})),
                             (common.UNOBSERVED, ["LOG_MISSING"]), gate)

    def test_runner_overall_is_not_green_when_the_log_glob_matches_nothing(self):
        ok_src = self.src("ok.sh", "#!/bin/bash\n" + SDRUN + "true\n")
        mp = self.src("m.json", json.dumps({"logs": [os.path.join(self.d, "no-such-*.jsonl")], "sources": [ok_src]}))
        rep = run_gates.run_all(json.load(open(mp)), only=("G1", "G3", "G4"))
        self.assertEqual({r["status"] for r in rep}, {common.UNOBSERVED})


class C8StopBypasses(Tmp):
    def array_log(self, argv):
        return self.log(extra_lines=[json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "shell", "input": {"command": argv}}]}})])

    def test_array_form_commands_are_scanned(self):
        r = g4_pidstops.run([self.array_log(["bash", "-lc", PK + " -f blender"])])
        self.assertEqual((r["status"], rules(r)), (common.RED, ["NAME_BASED_STOP"]))
        r = g4_pidstops.run([self.array_log([PK, "-f", "blender"])])
        self.assertEqual((r["status"], rules(r)), (common.RED, ["NAME_BASED_STOP"]))

    def test_wrappers_are_unwrapped(self):
        for cmd in ("bash -lc '%s -f blender'" % PK, "sh -ec '%s -f blender'" % PK, "sudo -u root %s Xvfb" % PK,
                    "timeout -s KILL 5 %s -f blender" % PK, "timeout --signal=KILL 5s %s ffmpeg" % KA,
                    "eval '%s -f blender'" % PK, "eval \"%s -9 ffmpeg\"" % KA, "busybox %s -f blender" % PK,
                    "bash <<EOF\n%s -f blender\nEOF" % PK, "sh -s <<'X'\n%s ffmpeg\nX" % KA,
                    "env -u DISPLAY %s -f blender" % PK, "nice -n 5 %s -f blender" % PK,
                    "bash -c \"bash -lc '%s -f blender'\"" % PK):
            r = g4_pidstops.run([self.log([cmd])])
            self.assertEqual((r["status"], rules(r)), (common.RED, ["NAME_BASED_STOP"]), cmd)

    def test_pattern_resolved_stops(self):
        for cmd, rule in (("kill $(pgrep -f blender)", "PATTERN_PID"),
                          ("docker ps -q | xargs docker kill", "PATTERN_PID"),
                          ("docker ps -q | xargs -r docker stop", "PATTERN_PID"),
                          ("systemctl --user stop blender.service", "NAME_BASED_STOP"),
                          ("systemctl --user kill vpref-x.scope other.service", "NAME_BASED_STOP")):
            r = g4_pidstops.run([self.log([cmd])])
            self.assertEqual((r["status"], rules(r)), (common.RED, [rule]), cmd)

    def test_controls_stay_green(self):
        p = self.log(["kill -INT 12345", "timeout -s INT 5 sleep 9", "sudo -u x true", "eval 'echo hi'",
                      "cat <<EOF > notes.txt\nnever %s -f\nEOF" % PK, "systemctl --user stop vpref-x.scope",
                      "systemctl --user status blender.service", "bash -lc 'kill -TERM $PID'",
                      "docker stop vpref-blender-program"])
        r = g4_pidstops.run([p])
        self.assertEqual((r["status"], rules(r)), (common.GREEN, []), r["findings"])


class C15Waiver(Tmp):
    def test_a_one_character_where_does_not_waive_everything(self):
        res = [{"gate": "G4_pid_only_stops", "status": common.RED, "observed_count": 1, "findings": [
            common.finding("NAME_BASED_STOP", "/a/b/log.jsonl:3", "x")]}]
        run_gates.apply_waivers(res, [{"rule": "NAME_BASED_STOP", "where": "/", "reason": "blanket"}])
        self.assertEqual(res[0]["status"], common.RED)
        run_gates.apply_waivers(res, [{"rule": "NAME_BASED_STOP", "where": "/a/b/log.jsonl:3", "reason": "exact"}])
        self.assertNotEqual(res[0]["status"], common.RED)


if __name__ == "__main__":
    unittest.main(verbosity=1)
