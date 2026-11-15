"""Core contracts, real recorded evidence, and independently detectable faults.

No import of bpy, mujoco, numpy or Pillow is needed by this suite.
"""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blender_visual_qa.drive.png import decode, encode
from vqa3d.canon import (canonical_json, integer, metres_to_micrometres,
                        radians_to_millidegrees, sha256_canonical, state_copy)
from vqa3d.check import lookup, verify_step
from vqa3d.contract import AdapterNotImplemented, Observation
from vqa3d.fakes import (ReplayAdapter, atomic_json, compare_replay_runs, observation_from_record,
                        observation_record, seal_trace)
from vqa3d.registry import ADAPTERS, create_adapter
from vqa3d.route_intent import KEYWORDS, route_intent
from vqa3d.verdict import (ADVISORY, CANNOT_SEE, ERROR, FAIL, PASS, STAGE_RC,
                          VERDICT_RC, final_verdict, max_precedence, rc_of)

ROOT = Path(__file__).resolve().parents[1]
TRACE_ROOT = ROOT / "vqa3d/traces"
TEMP_ROOT = ROOT / "vqa3d"


def recorded(name, step=0):
    trace = json.loads((TRACE_ROOT / (name + ".json")).read_text(encoding="utf-8"))
    trace["events"] = trace["events"][step * 3:step * 3 + 3]
    trace["scenario"] = [trace["scenario"][step]]
    trace["receipts"] = [trace["receipts"][step]]
    return seal_trace(trace)


def observation(trace, after=True):
    return observation_from_record(trace["events"][2 if after else 0]["observation"])


def change_after(trace, *, state=None, png="retain"):
    old = observation(trace)
    changed = Observation.create(trace["adapter"]["id"], "after",
                                 old.state if state is None else state,
                                 old.frame_png if png == "retain" else png, old.notes)
    trace["events"][2]["observation"] = observation_record(changed, "after")
    return seal_trace(trace)


def run_trace(trace, obligation=None, action=None):
    adapter = ReplayAdapter(trace)
    step = trace["scenario"][0]
    try:
        return verify_step(adapter, step["action"] if action is None else action,
                           step["obligation"] if obligation is None else obligation)
    finally:
        adapter.close()


class CanonTests(unittest.TestCase):
    def test_sorted_compact_unicode(self):
        self.assertEqual(canonical_json({"z": [1, True, None], "é": "é", "a": -3}),
                         '{"a":-3,"z":[1,true,null],"é":"é"}')

    def test_reject_every_float_including_nonfinite(self):
        for value in (0.0, -0.0, 1.2, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_json({"nested": [{"value": value}]})

    def test_reject_non_json_types_and_keys(self):
        for value in ({1: "key"}, (1, 2), {1, 2}, b"bytes", object()):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                canonical_json(value)

    def test_bool_never_integer(self):
        for value in (True, False, 1.0, "1"):
            with self.assertRaises(ValueError):
                integer(value)
        with self.assertRaises(ValueError):
            state_copy({"hidden": True})
        self.assertNotEqual(sha256_canonical(True), sha256_canonical(1))

    def test_quantisation_policy(self):
        self.assertEqual(metres_to_micrometres(1.2345675), 1234568)
        self.assertEqual(metres_to_micrometres(-0.0000005), 0)
        self.assertEqual(metres_to_micrometres(-0.0000015), -2)
        import math
        self.assertEqual(radians_to_millidegrees(math.pi), 180000)
        for value in (True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                metres_to_micrometres(value)

    def test_state_copy_is_detached(self):
        state = {"x": [1]}
        observed = Observation.create("x", "before", state)
        state["x"][0] = 2
        self.assertEqual(observed.state, {"x": [1]})


class VerdictTests(unittest.TestCase):
    def test_worst_wins_not_numerical_rc(self):
        self.assertEqual(max_precedence([]), PASS)
        self.assertEqual(max_precedence([PASS, ADVISORY, CANNOT_SEE, FAIL, ERROR]), ERROR)
        self.assertEqual(max_precedence([CANNOT_SEE, FAIL]), FAIL)
        self.assertEqual(rc_of(max_precedence([CANNOT_SEE, FAIL])), 1)
        with self.assertRaises(ValueError):
            max_precedence(["nonsense"])

    def test_distinct_code_tables(self):
        self.assertEqual(VERDICT_RC, {PASS: 0, ADVISORY: 0, FAIL: 1, ERROR: 2, CANNOT_SEE: 3})
        self.assertEqual(STAGE_RC, {"OK": 0, "ERROR": 2, "INCOMPLETE": 3,
                                   "STALE": 4, "GATE_FAILURE": 5, "BUDGET_EXCEEDED": 6})
        self.assertNotIn(1, STAGE_RC.values())

    def test_judge_advisory_documented_contract(self):
        # Pins vqa3d to the docstring at harness.py:43-46, not its differing body.
        self.assertEqual(final_verdict([PASS], [PASS], [ADVISORY]), CANNOT_SEE)
        for value in ("unparsed", None, {}, []):
            self.assertEqual(final_verdict([PASS], [], [value]), CANNOT_SEE)

    def test_judge_cannot_improve_any_deterministic_verdict(self):
        for deterministic in VERDICT_RC:
            for judge in (PASS, ADVISORY, CANNOT_SEE, FAIL, ERROR, "unparsed"):
                result = final_verdict([deterministic], [], [judge])
                self.assertEqual(max_precedence([result, deterministic]), result)


class ObservationReceiptTests(unittest.TestCase):
    def test_decoded_rgb_hash_and_identity(self):
        png = encode(2, 1, b"\0\1\2\3\4\5")
        obs = Observation.create("test", "before", {"x": 1}, png)
        self.assertEqual(obs.frame_sha256, hashlib.sha256(b"\0\1\2\3\4\5").hexdigest())
        self.assertEqual(obs.obs_id, sha256_canonical({"adapter": "test", "tag": "before",
                         "frame_sha256": obs.frame_sha256, "state_sha256": obs.state_sha256}))
        obs.validate("test", "before")
        with self.assertRaises(ValueError):
            obs.validate("test", "after")
        with self.assertRaises(ValueError):
            replace(obs, width=True).validate("test", "before")

    def test_unavailable_never_fakes_pixels(self):
        obs = Observation.create("test", "before", {"x": 1}, notes=["unavailable"])
        self.assertFalse(obs.capture_available)
        self.assertIsNone(obs.frame_sha256)
        self.assertEqual((obs.width, obs.height), (0, 0))
        with self.assertRaises(ValueError):
            replace(obs, capture_available=True).validate("test", "before")

    def test_receipt_determinism_and_digest(self):
        trace = recorded("blender_move")
        first, second = run_trace(trace), run_trace(trace)
        self.assertEqual(first, second)
        self.assertEqual(first, trace["receipts"][0])
        payload = dict(first)
        digest = payload.pop("receipt_sha256")
        self.assertEqual(digest, sha256_canonical(payload))
        self.assertEqual(first["schema"], "vqa3d.receipt/v1")
        self.assertEqual(first["final_verdict"], PASS)
        self.assertTrue(first["state_sha256_change"])
        self.assertEqual(first["after_frame"], first["after"])

    def test_receipt_does_not_claim_v02_validity(self):
        receipt = run_trace(recorded("blender_move"))
        self.assertNotEqual(receipt["schema"], "action_receipt/v0.2")
        self.assertNotIn("executed_at_ns", receipt)

    def test_atomic_refusal_preserves_caller_bytes(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as tmp:
            target = Path(tmp) / "receipt.json"
            target.write_bytes(b"original")
            with self.assertRaises(ValueError):
                atomic_json(target, {"bad": 0.5})
            self.assertEqual(target.read_bytes(), b"original")
            atomic_json(target, {"good": 1})
            self.assertEqual(target.read_text(), '{"good":1}\n')


class StubRouteTests(unittest.TestCase):
    def test_stub_methods_are_typed_and_check_is_incomplete(self):
        for name in ("autocad", "omniverse", "isaac_sim"):
            with self.subTest(adapter=name):
                adapter = create_adapter(name)
                probe = adapter.probe()
                self.assertEqual(probe["status"], "not_implemented")
                self.assertFalse(probe["available"])
                self.assertFalse(probe["capabilities"]["capture_available"])
                for fn, args in ((adapter.capture, ("test",)), (adapter.act, ({},)),
                                 (adapter.scene_state, ()), (adapter.close, ())):
                    with self.assertRaises(AdapterNotImplemented):
                        fn(*args)
                receipt = verify_step(adapter, {}, {"kind": "frame_change"})
                self.assertEqual(receipt["final_verdict"], CANNOT_SEE)
                self.assertEqual(receipt["verdict_rc"], 3)
                self.assertEqual(receipt["verdicts"][0]["reason"], "adapter_not_implemented")

    def test_every_keyword_routes_and_reports_status(self):
        for name, words in KEYWORDS.items():
            for word in words:
                with self.subTest(keyword=word):
                    routed = route_intent("please open " + word)
                    self.assertEqual(routed["adapter"], name)
                    self.assertEqual(routed["status"], ADAPTERS[name].status)
                    self.assertEqual(routed["rc"], 0 if name in ("blender", "mujoco") else 3)
                    self.assertIn("rows", routed["code"])

    def test_router_abstains_unknown_ambiguous_and_substrings(self):
        for text in ("do something", "blender robot", "unblendered", "autocadence"):
            self.assertEqual(route_intent(text)["rc"], 3)
            self.assertIsNone(route_intent(text)["adapter"])
        self.assertEqual(route_intent("change blender position")["adapter"], "blender")

    def test_coordinates_are_real_read_only_component_coordinates(self):
        result = route_intent("blender decode PNG RGB")
        self.assertTrue(result["code"]["rows"])
        for row in result["code"]["rows"]:
            path, line = row["at"].rsplit(":", 1)
            self.assertTrue((ROOT / path).is_file())
            self.assertGreater(int(line), 0)
        # New, untracked adapter code is resolved through the read-only AST overlay.
        mujoco_rows = route_intent("mujoco")["code"]["rows"]
        self.assertTrue(mujoco_rows)
        self.assertTrue(any(row["at"].startswith("vqa3d/adapters/mujoco_sim.py:") for row in mujoco_rows))

    def test_unknown_adapter_refuses(self):
        with self.assertRaises(ValueError):
            create_adapter("invented")


class RecordedCheckTests(unittest.TestCase):
    def test_live_traces_reproduce_every_receipt_without_runtimes(self):
        for name in ("blender_move", "blender_wrong_expectation", "mujoco_simulation"):
            trace = json.loads((TRACE_ROOT / (name + ".json")).read_text())
            adapter = ReplayAdapter(trace)
            self.assertEqual(trace["provenance"]["recording"], "live")
            for step, expected in zip(trace["scenario"], trace["receipts"]):
                actual = verify_step(adapter, step["action"], step["obligation"])
                self.assertEqual(actual, expected)
                self.assertEqual(actual["final_verdict"], FAIL if name == "blender_wrong_expectation" else PASS)
            adapter.close()

    def test_state_equals_requires_exact_integer(self):
        trace = recorded("mujoco_simulation", 1)
        expected = observation(trace).state["qpos"][2]
        for value, verdict in ((expected, PASS), (expected + 1, FAIL), (True, ERROR), (0.1, ERROR)):
            receipt = run_trace(trace, {"kind": "state_equals", "expected": {"qpos.2": value}})
            self.assertEqual(receipt["final_verdict"], verdict)
        self.assertEqual(run_trace(trace, {"kind": "state_equals", "expected": {"missing": 0}})["final_verdict"], FAIL)

    def test_state_change_forbids_unlisted_change(self):
        trace = recorded("blender_move")
        state = copy.deepcopy(observation(trace).state)
        state["objects"]["Camera"]["location_um"][0] += 1
        trace = change_after(trace, state=state)
        rejected = run_trace(trace)
        self.assertEqual(rejected["final_verdict"], FAIL)
        obligation = copy.deepcopy(trace["scenario"][0]["obligation"])
        obligation["allow_changes"] = ["objects.Camera.location_um"]
        self.assertEqual(run_trace(trace, obligation)["final_verdict"], PASS)

    def test_frame_change_compares_actual_region(self):
        trace = recorded("blender_move")
        before = observation(trace, False)
        w, h, rgb = decode(before.frame_png)
        altered = bytearray(rgb)
        altered[0] ^= 255
        trace = change_after(trace, png=encode(w, h, bytes(altered)))
        self.assertEqual(run_trace(trace, {"kind": "frame_change", "region": [0, 0, 1, 1]})["final_verdict"], PASS)
        self.assertEqual(run_trace(trace, {"kind": "frame_change", "region": [1, 0, 1, 1]})["final_verdict"], FAIL)
        self.assertEqual(run_trace(trace, {"kind": "frame_change", "region": [w, 0, 1, 1]})["final_verdict"], ERROR)

    def test_no_change_is_legitimate(self):
        trace = recorded("blender_move")
        before = observation(trace, False)
        trace = change_after(trace, state=before.state, png=before.frame_png)
        self.assertEqual(run_trace(trace, {"kind": "no_change"})["final_verdict"], PASS)
        self.assertEqual(run_trace(recorded("blender_move"), {"kind": "no_change"})["final_verdict"], FAIL)
        missing = change_after(trace, png=None)
        self.assertEqual(run_trace(missing, {"kind": "no_change"})["final_verdict"], CANNOT_SEE)

    def test_unavailable_capture_does_not_preclude_state_obligation(self):
        trace = change_after(recorded("blender_move"), png=None)
        self.assertEqual(run_trace(trace)["final_verdict"], PASS)
        self.assertEqual(run_trace(trace, {"kind": "frame_change"})["final_verdict"], CANNOT_SEE)

    def test_action_failure_cannot_be_cleared_by_postcondition(self):
        trace = recorded("blender_move")
        trace["events"][1]["result"]["ok"] = False
        receipt = run_trace(seal_trace(trace))
        self.assertEqual(receipt["final_verdict"], FAIL)
        self.assertEqual(receipt["verdict_rc"], 1)

    def test_exception_is_error(self):
        trace = recorded("blender_move")
        adapter = ReplayAdapter(trace)
        with patch.object(adapter, "act", side_effect=RuntimeError("injected")):
            self.assertEqual(verify_step(adapter, {}, {"kind": "no_change"})["final_verdict"], ERROR)

    def test_invalid_obligations_refuse_before_act(self):
        for obligation in ({"kind": "unknown"}, {"kind": "state_change", "keys": []},
                           {"kind": "state_equals", "expected": {}},
                           {"kind": "frame_change", "region": [True, 0, 1, 1]}):
            adapter = ReplayAdapter(recorded("blender_move"))
            receipt = verify_step(adapter, {}, obligation)
            self.assertEqual(receipt["final_verdict"], ERROR)
            self.assertEqual(adapter._cursor, 0)

    def test_noncanonical_list_indices_refuse_before_act(self):
        for key in ("-1", "+1", "01", " 1", "١", "1.0"):
            with self.subTest(index=key):
                with self.assertRaises(ValueError):
                    lookup({"qpos": [0, 1]}, "/qpos/" + key)
                for obligation in ({"kind": "state_equals", "expected": {"/qpos/" + key: 0}},
                                   {"kind": "state_change", "keys": ["qpos"], "allow_changes": ["/qpos/" + key]}):
                    adapter = ReplayAdapter(recorded("mujoco_simulation", 1))
                    receipt = verify_step(adapter, adapter.trace["scenario"][0]["action"], obligation)
                    self.assertEqual(receipt["final_verdict"], ERROR)
                    self.assertIn("noncanonical_list_index:", receipt["error"])
                    self.assertEqual(adapter._cursor, 1)
                    adapter.close()
        self.assertEqual(lookup({"qpos": [0, 1]}, "qpos.1"), 1)
        self.assertEqual(lookup({"objects": {"01": 7}}, "/objects/01"), 7)

    def test_unknown_obligation_keys_refuse_before_capture(self):
        for obligation in ({"kind": "state_change", "keys": ["qpos"], "allow_change": []},
                           {"kind": "state_change", "keys": ["qpos"], "region": [0, 0, 1, 1]},
                           {"kind": "state_equals", "expected": {"qpos.0": 0}, "keys": ["qpos"]},
                           {"kind": "frame_change", "expected": {}},
                           {"kind": "no_change", "allow_changes": []}):
            adapter = ReplayAdapter(recorded("mujoco_simulation", 1))
            receipt = verify_step(adapter, {}, obligation)
            self.assertEqual(receipt["final_verdict"], ERROR)
            self.assertIn("unknown_obligation_keys:", receipt["error"])
            self.assertEqual(adapter._cursor, 0)
            adapter.close()

    def test_trace_integrity_order_action_and_tag(self):
        trace = recorded("blender_move")
        trace["provenance"]["recording"] = "tampered"
        with self.assertRaises(ValueError):
            ReplayAdapter(trace)
        trace = recorded("blender_move")
        self.assertEqual(run_trace(trace, action={"op": "wrong"})["final_verdict"], ERROR)
        trace["events"][0]["observation"]["tag"] = "after"
        self.assertEqual(run_trace(seal_trace(trace))["final_verdict"], ERROR)


class FaultInjectionTests(unittest.TestCase):
    """Six fault classes for EACH implemented adapter, using recorded live data."""

    def fixtures(self):
        for name, step in (("blender_move", 0), ("mujoco_simulation", 1)):
            yield name, recorded(name, step)

    def test_mutant_action_not_applied(self):
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                before = observation(trace, False)
                mutant = change_after(trace, state=before.state, png=before.frame_png)
                self.assertEqual(run_trace(mutant)["final_verdict"], FAIL)

    def test_mutant_wrong_object_or_joint_changed(self):
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                expected_state = copy.deepcopy(observation(trace).state)
                state = copy.deepcopy(observation(trace, False).state)
                if name == "blender_move":
                    state["objects"]["Camera"]["location_um"][0] += 1000000
                    obligation = trace["scenario"][0]["obligation"]
                else:
                    state["qpos"][7] += 1000000
                    obligation = {"kind": "state_change", "keys": ["qpos.2"]}
                # Intended target is unchanged; another object/joint changed.
                rejected = run_trace(change_after(trace, state=state), obligation)
                self.assertEqual(rejected["final_verdict"], FAIL)
                self.assertIn("required_state_unchanged:", rejected["error"])
                self.assertIn("unexpected_state_change:", rejected["error"])
                # Independent contrast: intended target now moves too. Only the
                # unexpected-change detector can reject this valid observation.
                if name == "blender_move":
                    state["objects"]["Cube.001 Big"]["location_um"] = expected_state["objects"]["Cube.001 Big"]["location_um"]
                else:
                    state["qpos"][2] = expected_state["qpos"][2]
                rejected = run_trace(change_after(trace, state=state), obligation)
                self.assertEqual(rejected["final_verdict"], FAIL)
                self.assertTrue(rejected["error"].startswith("unexpected_state_change:"))
                self.assertNotIn("required_state_unchanged:", rejected["error"])

    def test_mutant_frozen_frame(self):
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                self.assertNotEqual(observation(trace).frame_sha256, observation(trace, False).frame_sha256)
                self.assertEqual(run_trace(trace, {"kind": "frame_change"})["final_verdict"], PASS)
                mutant = change_after(trace, png=observation(trace, False).frame_png)
                self.assertEqual(run_trace(mutant, {"kind": "frame_change"})["final_verdict"], FAIL)

    def test_mutant_capture_unavailable(self):
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                mutant = change_after(trace, png=None)
                self.assertEqual(run_trace(mutant, {"kind": "frame_change"})["final_verdict"], CANNOT_SEE)

    def test_mutant_float_in_state(self):
        class FloatReplay(ReplayAdapter):
            def capture(self, tag):
                obs = super().capture(tag)
                return replace(obs, state={"invalid_float": 0.5}) if tag == "after" else obs
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                adapter = FloatReplay(trace)
                try:
                    step = trace["scenario"][0]
                    receipt = verify_step(adapter, step["action"], step["obligation"])
                    self.assertEqual(receipt["final_verdict"], ERROR)
                    self.assertIn("noncanonical_type:float", receipt["error"])
                finally:
                    adapter.close()

    def test_mutant_nondeterministic_replay_breaks_identity(self):
        for name, trace in self.fixtures():
            with self.subTest(adapter=name):
                class UnstableReplay(ReplayAdapter):
                    runs = 0

                    def __init__(self, source):
                        super().__init__(source)
                        type(self).runs += 1
                        self.run_number = type(self).runs

                    def capture(self, tag):
                        obs = super().capture(tag)
                        if self.run_number == 2 and tag == "after":
                            state = copy.deepcopy(obs.state)
                            if self.id == "blender":
                                state["objects"]["Cube.001 Big"]["location_um"][0] += 1
                            else:
                                state["qpos"][2] += 1
                            # A DIFFERENT, validly hashed observation, still
                            # satisfying the same obligation and same action.
                            return Observation.create(self.id, tag, state, obs.frame_png, obs.notes)
                        return obs
                step = trace["scenario"][0]
                runs = []
                for _ in range(2):
                    adapter = UnstableReplay(trace)
                    try:
                        runs.append([verify_step(adapter, step["action"], step["obligation"])])
                    finally:
                        adapter.close()
                first, second = runs
                self.assertEqual(first[0]["final_verdict"], PASS)
                self.assertEqual(second[0]["final_verdict"], PASS)
                self.assertNotEqual(first[0]["after"]["state_sha256"], second[0]["after"]["state_sha256"])
                self.assertEqual(compare_replay_runs(first, first)["final_verdict"], PASS)
                comparison = compare_replay_runs(first, second)
                self.assertEqual(comparison["final_verdict"], FAIL)
                self.assertEqual(comparison["reason"], "nondeterministic_replay:step:0")

    def test_replay_comparison_refuses_missing_or_invalid_evidence(self):
        run = [run_trace(recorded("blender_move"))]
        for first, second in (([], []), (run, []), (run, run * 2)):
            self.assertEqual(compare_replay_runs(first, second)["final_verdict"], FAIL)
        bad = copy.deepcopy(run)
        bad[0]["receipt_sha256"] = "tampered"
        self.assertEqual(compare_replay_runs(run, bad)["final_verdict"], FAIL)
        incomplete = copy.deepcopy(run)
        incomplete[0]["state_comparison_available"] = False
        incomplete[0].pop("receipt_sha256")
        incomplete[0]["receipt_sha256"] = sha256_canonical(incomplete[0])
        self.assertEqual(compare_replay_runs(incomplete, incomplete)["final_verdict"], FAIL)


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "vqa3d", *args], cwd=ROOT,
                              text=True, capture_output=True, timeout=30)

    def test_route_cli_and_abstention(self):
        result = self.run_cli("route", "blender RGB")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["adapter"], "blender")
        result = self.run_cli("route", "unknown environment")
        self.assertEqual(result.returncode, 3)

    def test_probe_stub_and_check_stub_cli(self):
        result = self.run_cli("probe", "--adapter", "autocad")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["status"], "not_implemented")
        result = self.run_cli("check", "--adapter", "autocad", "--scenario", "vqa3d/scenarios/blender_move.json")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["final_verdict"], CANNOT_SEE)

    def test_missing_scenario_is_error(self):
        result = self.run_cli("check", "--adapter", "blender", "--scenario", "vqa3d/scenarios/absent.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["final_verdict"], ERROR)


if __name__ == "__main__":
    unittest.main()
