#!/usr/bin/env python3
"""The checker of e2e_agent.py against the stored audit of a real run, and
against the same run with one thing broken at a time (negative controls).

Run: python3 -I tests/test_e2e_check.py
The transcript is rebuilt from tests/evidence/e2e_agent_run/audit.jsonl (the
raw stream-json of the run holds host paths and is not stored here).
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import e2e_agent as e  # noqa: E402

RUN = os.path.join(HERE, "evidence", "e2e_agent_run")


def transcript(rows, extra_uses=(), final=None):
    lines = []
    calls = [r for r in rows if r.get("method") == "tools/call"]
    for i, row in enumerate(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t%d" % i, "name": e.PREFIX + row["tool"], "input": row["arguments"]}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i,
             "content": [{"type": "text", "text": 'text\n{"code":"%s","ok":true}' % row["code"]}]}]}}))
    for name in extra_uses:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x", "name": name, "input": {}}]}}))
    lines.append(json.dumps(final if final is not None else {"type": "result", "is_error": False}))
    return lines


class CheckTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(RUN, "audit.jsonl")) as fh:
            self.rows = [json.loads(line) for line in fh]

    def run_check(self, rows, **kw):
        return e.check(transcript(rows, **kw), rows)

    def call(self, rows, tool):
        return [r for r in rows if r.get("tool") == tool][0]

    def test_stored_real_run_conforms(self):
        self.assertEqual(self.run_check(self.rows), [])
        with open(os.path.join(RUN, "verdict.json")) as fh:
            verdict = json.load(fh)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["claude_rc"], 0)
        self.assertTrue(all(name.startswith(e.PREFIX) for name in verdict["tool_uses"]))
        self.assertEqual(len(verdict["tool_uses"]), len(verdict["audit_calls"]))

    def mutated(self, fn):
        rows = copy.deepcopy(self.rows)
        fn(rows)
        return rows

    def test_negative_builtin_tool_used(self):
        bad = self.run_check(self.rows, extra_uses=["Bash"])
        self.assertTrue(any("outside viewpoint-ops" in b and "Bash" in b for b in bad), bad)

    def test_negative_start_without_dry_run(self):
        def fn(rows):
            row = self.call(rows, "vp_blender_start")
            row["dry_run"] = False
            row["arguments"].pop("dry_run")
            row["code"] = "OK"
        bad = self.run_check(self.mutated(fn))
        self.assertTrue(any("vp_blender_start (class X) ran without dry_run" in b for b in bad), bad)
        self.assertTrue(any("missing step: blender start dry run" in b for b in bad), bad)

    def test_refused_destructive_call_is_not_a_violation(self):
        def fn(rows):
            row = copy.deepcopy(self.call(rows, "vp_blender_stop"))
            row.update(dry_run=False, code="CONFIRM_REQUIRED", ok=False, started=False)
            row["arguments"].pop("dry_run")
            rows.insert(rows.index(self.call(rows, "vp_blender_stop")), row)
        self.assertEqual(self.run_check(self.mutated(fn)), [])

    def test_negative_events_not_read(self):
        rows = [r for r in self.rows if r.get("tool") != "vp_queue_events"]
        self.assertIn("missing step: events really read", self.run_check(rows))

    def test_negative_wrong_runtime_root_live_lane_and_job(self):
        bad = self.run_check(self.mutated(lambda rows: self.call(rows, "vp_queue_run")["arguments"].update(rt="/dev/shm/vqa-cu/swarm")))
        self.assertTrue(any("runtime root other than the assigned one" in b for b in bad), bad)
        self.assertTrue(any("live lane" in b for b in bad), bad)
        bad = self.run_check(self.mutated(lambda rows: self.call(rows, "vp_blender_start")["arguments"].update(job="blender-main")))
        self.assertTrue(any("job outside the prefix" in b for b in bad), bad)
        bad = self.run_check(self.mutated(lambda rows: self.call(rows, "vp_blender_start")["arguments"].update(allow_hybrid=True)))
        self.assertTrue(any("override" in b for b in bad), bad)

    def test_negative_one_agent_is_not_a_two_agent_scenario(self):
        bad = self.run_check(self.mutated(lambda rows: self.call(rows, "vp_agents_run")["arguments"].update(agents="modeler-agent")))
        self.assertIn("missing step: 2-agent stub run dry run", bad)

    def test_negative_transcript_and_audit_disagree(self):
        lines = transcript(self.rows)
        self.assertTrue(any("audited" in b for b in e.check(lines, self.rows[:-1])))  # a call the server never saw
        swapped = copy.deepcopy(self.rows)
        self.call(swapped, "vp_queue_events")["code"] = "NOT_FOUND"
        bad = e.check(lines, swapped)  # the model was told OK, the server recorded NOT_FOUND
        self.assertTrue(any("is not in what the model was told" in b for b in bad), bad)

    def test_negative_no_result_or_error_result(self):
        self.assertIn("transcript has no result event", e.check(transcript(self.rows)[:-1], self.rows))
        bad = self.run_check(self.rows, final={"type": "result", "is_error": True})
        self.assertIn("the agent run ended with is_error", bad)

    def test_negative_did_not_end_with_job_listing(self):
        self.assertIn("missing step: owned jobs listed at the end", self.run_check(self.rows[:-1]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
