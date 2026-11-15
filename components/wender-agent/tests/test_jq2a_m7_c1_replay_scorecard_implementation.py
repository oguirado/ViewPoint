# @module {"id":"tests.test_jq2a_m7_c1_replay_scorecard_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.multiwin_scorecard","wa.act.godot_executor","wa.perceive.godot"]}
"""Public-path M7 tests with typed RAM-only component fixtures.

Prerequisite PASS records below describe injected fixtures only; they are not
claims that unmerged native M1--M8 implementations passed their owner gates.
No fixture implements scoring, replay comparison, seal validation or aggregation.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, dataclass, fields, replace
import hashlib
import io
import json
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from wender.learn import multiwin_scorecard as m
from wender.act.godot_executor import GodotExecutor
from wender.perceive.godot import read_outbox_batches


def _key(handle):
    return handle.kind, handle.canonical_id, handle.generation


def _readonly(rgb):
    return np.frombuffer(rgb.tobytes(), dtype=np.uint8).reshape(rgb.shape)


@dataclass
class FixtureRegistry:
    adapter: "FixtureAdapter"

    def resolve(self, handle):
        a = self.adapter
        a.calls.append(("resolve", _key(handle)))
        return SimpleNamespace(handle=a.registry_handle or a.handle, epoch=a.epoch,
                               closed=a.closed, transport=self, owned=True,
                               session_id=a.handle.kind, attachment_owned=True)


@dataclass
class FixtureStream:
    adapter: "FixtureAdapter"

    def deliver(self, frame):
        a = self.adapter
        a.calls.append(("deliver", frame.frame_seq))
        if frame.frame_seq in a.drop_seqs:
            return None
        mode = a.stream_mode
        if mode == "foreign":
            return replace(frame, handle=m.Handle("cdp_tab", "foreign", 1))
        if mode == "torn":
            # A strict FrameRef validates its digest at construction. Simulate a
            # corrupt stream only AFTER constructing a valid isolated frame;
            # keep the source frame intact for the exact-pixel comparison.
            corrupted = replace(frame)
            object.__setattr__(corrupted, "digest", "0" * 64)
            return corrupted
        if mode == "pixels":
            changed = _readonly(np.full(frame.rgb.shape, 4, dtype=np.uint8))
            return replace(frame, rgb=changed,
                           digest=hashlib.sha256(changed.tobytes()).hexdigest())
        if mode == "borrow":
            a.borrow_owner = np.array(frame.rgb, copy=True)
            view = a.borrow_owner.view()
            view.setflags(write=False)
            return replace(frame, rgb=view)
        return frame


@dataclass
class FixturePerception:
    adapter: "FixtureAdapter"

    def observe(self, frame):
        a = self.adapter
        a.calls.append(("observe", frame.frame_seq))
        state = {"cursor": a.memory.get("cursor", 0),
                 "previous": a.memory.get("previous"),
                 "random": a.rng.getrandbits(80),
                 "target": list(_key(a.handle))}
        if a.extra_state is not None:
            state.update(a.extra_state())
        a.last_observation = m.Observation(a.handle, frame.epoch, frame.frame_seq,
                                          a.dpr_ppm, state, frame)
        return a.last_observation

    def reduce(self, observation):
        a = self.adapter
        a.calls.append(("reduce", observation.frame_seq))
        a.memory["cursor"] = a.memory.get("cursor", 0) + 1
        a.memory["previous"] = observation.frame.digest
        claims = observation.frame.rgb[:, :, 0] > 0
        if a.miss:
            claims = np.zeros_like(claims)
        ops = {} if a.empty_ops else {"pixels": int(claims.size), "reductions": 1}
        if a.operation_bleed:
            ops["reductions"] = a.memory["cursor"]
        return m.ChangeResult(a.change_handle or a.handle, observation.epoch,
                              a.settled, claims, ops)


@dataclass
class FixtureScheduler:
    adapter: "FixtureAdapter"

    def admit(self, action, observation, change, now_ns):
        a = self.adapter
        a.calls.append(("admit", action.action_id))
        telemetry = {
            "resource_key": a.resource_key, "active_resource": 1,
            "focus_authorized": True, "handle_fresh": True, "fence_clear": True,
            "guard_passed": True, "geometry_matches": True,
            "global_committed": a.committed, "global_reserved": action.cost,
            "global_cap": 10000, "target_committed": a.committed,
            "target_reserved": action.cost, "target_cap": 10000,
            "wait_opportunities": 0, "queue_depth": 1,
            "fairness_cost": 1, "quantum": 1, "urgent_cap": 1,
            "active_focus": 1,
        }
        telemetry.update(a.telemetry)
        telemetry.update(action.payload.get("telemetry", {}))
        for name in a.missing_telemetry:
            telemetry.pop(name, None)
        if a.mutate_borrow:
            a.borrow_owner.fill(231)
            a.last_observation.state["cursor"] = 999
        return {"admitted": a.admit, "eligible": a.eligible, "reason": a.reason,
                "telemetry": telemetry}


@dataclass
class FixtureActor:
    adapter: "FixtureAdapter"

    def execute(self, action, observation, now_ns):
        a = self.adapter
        a.calls.append(("execute", action.action_id))
        a.actor_pixels.append(observation.frame.rgb.tobytes())
        a.actor_states.append(copy.deepcopy({k: v for k, v in observation.state.items()
                                           if k != "diagnostics"}))
        a.committed += action.cost
        a.clock.advance(action.payload.get("advance_ns", 0))
        if action.payload.get("raise") == "timeout":
            raise TimeoutError("diagnostic-only message")
        if action.payload.get("raise") == "crash":
            raise ConnectionError("diagnostic-only message")
        handle = a.receipt_handle or a.handle
        return m.Receipt(action.action_id, handle,
                         action.payload.get("admission", "ADMITTED"), None)


class FixtureAdapter:
    offline = True

    def __init__(self, handle, *, resource_key=None, shared_memory=None):
        self.handle = handle
        self.resource_key = resource_key or (handle.kind + "/" + handle.canonical_id)
        self.focus_domain = "focus/" + self.resource_key
        self.gates = {g: {"status": "PASS", "evidence": "typed-offline-fixture:" + g,
                          "expected_failure": False} for g in m.PREREQUISITE_GATES}
        self.registry = FixtureRegistry(self)
        self.stream = FixtureStream(self)
        self.perception = FixturePerception(self)
        self.scheduler = FixtureScheduler(self)
        self.actor = FixtureActor(self)
        self.calls, self.seeds, self.actor_pixels, self.actor_states = [], [], [], []
        self.memory = {} if shared_memory is None else shared_memory
        self.epoch, self.closed, self.dpr_ppm = 0, False, 1250000
        self.registry_handle = self.receipt_handle = self.change_handle = None
        self.stream_mode = None
        self.drop_seqs = set()
        self.telemetry, self.missing_telemetry = {}, set()
        self.admit, self.eligible, self.reason = True, True, None
        self.settled, self.empty_ops, self.miss = True, False, False
        self.operation_bleed, self.mutate_borrow = False, False
        self.before_reset = self.extra_state = None
        self.committed = 0

    def reset(self, *, seed, clock):
        if self.before_reset is not None:
            self.before_reset()
        self.calls.append(("reset", seed))
        self.seeds.append(seed)
        self.rng = random.Random(seed)
        self.clock = clock
        self.memory.clear()
        self.committed = 0

    def terminal(self, action, receipt, now_ns):
        self.calls.append(("terminal", action.action_id))
        return m.Receipt(action.action_id, self.handle,
                         action.payload.get("terminal", "SUCCESS"), None)


_BATCH_END = b"M7_OFFLINE_FIXTURE_END\n"


def _encode_fixture(records):
    return b"".join(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()
                    + b"\n" for r in records) + _BATCH_END


def _decode_fixture(data):
    parts = data.split(_BATCH_END)
    return parts[:-1], parts[-1]


class GodotFixtureAdapter(FixtureAdapter):
    """Actual Godot admission and outbox validators, injected RAM codecs/sink."""

    def reset(self, *, seed, clock):
        super().reset(seed=seed, clock=clock)
        self.batches = []
        context = {"principal": "offline-test", "channel": "t2", "slot_kind": "sandbox",
                   "automation_active": True, "mandates": {"owner": True}}
        self.executor = GodotExecutor(context,
            {"pause": {"kind": "current", "boundary": "current", "args": {}}},
            _encode_fixture, self.batches.append)
        self.actor = SimpleNamespace(execute=self.execute)

    def execute(self, action, observation, now_ns):
        self.calls.append(("execute", action.action_id))
        self.committed += action.cost
        result = self.executor.dispatch({"tick": observation.frame_seq,
            "seq": self.executor.next_seq, "cmd": "pause", "args": {}},
            committed_tick=observation.frame_seq, basis_sha256=observation.frame.digest)
        return m.Receipt(action.action_id, self.handle, result["status"].upper(),
                         result.get("reason"))

    def terminal(self, action, receipt, now_ns):
        self.calls.append(("terminal", action.action_id))
        record = {"seq": self.executor.next_seq - 1, "order_seq_id": self.executor.next_seq,
                  "requested_tick": 0, "applied_tick": 0, "status": "accepted",
                  "reason": None, "state_sha256": "a" * 64}
        payload = _encode_fixture([record])
        prefix, suffix = payload[:7], payload[7:]
        complete, pending = read_outbox_batches(prefix, _decode_fixture)
        if complete:
            raise AssertionError("partial batch escaped production outbox reader")
        complete, pending = read_outbox_batches(pending + suffix, _decode_fixture)
        if pending or complete[0][0] != record:
            raise AssertionError("outbox round-trip changed engine outcome")
        return m.Receipt(action.action_id, self.handle, complete[0][0]["status"].upper(), None)


def _event(handle, seq, at, *, action_id=None, payload=None, expected="SUCCESS"):
    rgb = np.zeros((2, 3, 3), dtype=np.uint8)
    rgb[:, :, 0] = np.array([[1, 0, 1], [0, 0, 1]], dtype=np.uint8)
    rgb[:, :, 1] = seq % 255
    aid = action_id or (handle.kind + ":" + str(handle.generation) + ":" + str(seq))
    event = {"handle": handle, "scene": "same-scene", "epoch": 0,
             "frame_seq": seq, "at_ns": at, "rgb": rgb, "truth": rgb[:, :, 0] > 0,
             "action": m.ActionRequest(handle, aid, 0, payload or {}, 1, 1000000)}
    return event, (aid, expected)


def _scenario(kinds=("cdp_tab", "godot_window"), count=2):
    handles = tuple(m.Handle(kind, "same-id", 2 ** 100) for kind in kinds)
    adapters = {_key(h): FixtureAdapter(h) for h in handles}
    events, expected = [], {}
    for seq in range(count):
        for handle in handles:
            event, outcome = _event(handle, seq, 10 * (len(events) + 1))
            events.append(event)
            expected.update([outcome])
    return m.Scenario("offline", handles, tuple(events), expected), adapters


class TestMultiwinScorecard(unittest.TestCase):
    def run_case(self, scenario, adapters, *, seed=7, clock=None):
        report = m.run_trial(scenario, seed=seed, clock=clock or m.FakeClock(), adapters=adapters)
        return report, m.score_windows(report.rows)

    def assert_pass(self, card):
        self.assertTrue(card.passed, (card.violations,
            [k for k, v in card.metrics.items() if not v["passed"]]))

    def test_record_order_frozen_values_and_integer_fake_clock(self):
        expected = {
            m.Scenario: "name targets events expected_outcomes",
            m.WindowEvidence: "handle scene epoch frame_seq tp fp fn tn ops frame_supported dropped exact terminal expected_terminal seal violations",
            m.TrialReport: "rows outcomes faults", m.Scorecard: "windows pooled metrics violations passed",
        }
        for record, names in expected.items():
            with self.subTest(record=record.__name__):
                self.assertEqual([f.name for f in fields(record)], names.split())
        scenario, adapters = _scenario(count=1)
        report, _ = self.run_case(scenario, adapters)
        with self.assertRaises(FrozenInstanceError):
            report.rows[0].epoch = 9
        clock = m.FakeClock(10)
        self.assertEqual(clock.advance(2 ** 80), 10 + 2 ** 80)
        self.assertEqual(clock(), clock.now_ns)
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clock.advance(bad)

    def test_all_five_kinds_independent_cursors_and_full_gate_vector(self):
        kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
        scenario, adapters = _scenario(kinds)
        report, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(set(report.outcomes["supported_kinds"]), set(kinds))
        self.assertEqual(report.outcomes["missing_supported_kinds"], [])
        self.assertEqual(len(card.windows), 10)
        self.assertEqual(set(card.metrics), set(m.METRIC_KEYS))
        gates = card.metrics["terminal_completeness"]["gates"]
        self.assertEqual(set(gates), set(m.REQUIRED_GATES))
        self.assertEqual(len(gates), 40)
        self.assertTrue(all(g["status"] == "PASS" for g in gates.values()))
        self.assertTrue(all(a.memory["cursor"] == 2 for a in adapters.values()))
        self.assertEqual(len({a.seeds[0] for a in adapters.values()}), len(kinds))
        self.assertTrue(all(len(set(a.seeds)) == 1 for a in adapters.values()))
        self.assertEqual(card.metrics["terminal_completeness"]["simulated_throughput"],
            {"numerator": 10, "denominator": 100,
             "unit": "successful_actions_per_simulated_nanosecond"})

    def test_same_seed_replay_and_diagnostic_wall_clock_exclusion(self):
        scenario, adapters = _scenario()
        first, card = self.run_case(scenario, adapters)
        second, _ = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(m.canonical_bytes(first), m.canonical_bytes(second))
        self.assertTrue(first.outcomes["canonical_replay"]["matched"])
        noisy = replace(first, outcomes={**first.outcomes,
            "diagnostics": {"wall_clock_timestamp": 123.1, "host": object()},
            "wall_clock_timestamp": 456.2, "pid": 800})
        self.assertEqual(m.canonical_bytes(first), m.canonical_bytes(noisy))
        self.assertNotIn(b"wall_clock_timestamp", m.canonical_bytes(noisy))
        with self.assertRaises(ValueError):
            m.canonical_bytes(replace(first, outcomes={**first.outcomes, "unbound_float": 0.5}))
        changed, _ = self.run_case(scenario, adapters, seed=8)
        self.assertNotEqual(m.canonical_bytes(first), m.canonical_bytes(changed))

    def test_shared_reducer_bleed_cannot_hide_behind_perfect_pooled_counts(self):
        scenario, adapters = _scenario()
        shared = {}
        for adapter in adapters.values():
            adapter.memory = shared
        report, card = self.run_case(scenario, adapters)
        self.assertEqual(card.pooled["fp"], 0)
        self.assertEqual(card.pooled["fn"], 0)
        self.assertTrue(card.metrics["terminal_completeness"]["passed"])
        self.assertFalse(card.passed)
        self.assertFalse(card.metrics["cross_window_bleed"]["passed"])
        self.assertIn("cross_window_state_bleed", card.violations)
        self.assertEqual(report.outcomes["gates"]["G-M7-2"]["status"], "FAIL")

    def test_missing_duplicate_foreign_rows_and_seal_mutants(self):
        scenario, adapters = _scenario()
        report, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        rows = report.rows
        mutants = {
            "omit_godot": tuple(r for r in rows if r.handle.kind != "godot_window"),
            "duplicate": rows + (rows[0],),
            "foreign": (replace(rows[0], handle=m.Handle("cdp_tab", "foreign", 1)),) + rows[1:],
            "overwrite_tab_identity": (replace(rows[0], handle=rows[1].handle),) + rows[1:],
            "missing_seal": (replace(rows[0], seal=""),) + rows[1:],
            "mixed_seal": (replace(rows[0], seal="0" * 64),) + rows[1:],
            "missing_terminal": (replace(rows[0], terminal=None),) + rows[1:],
        }
        for name, mutant in mutants.items():
            with self.subTest(mutant=name):
                self.assertFalse(m.score_windows(mutant).passed)

    def test_integer_pool_and_isolated_operation_totals_no_scene_renaming(self):
        scenario, adapters = _scenario()
        _, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual({r.scene for r in card.windows}, {"same-scene"})
        self.assertEqual(card.pooled, {"tp": 12, "fp": 0, "fn": 0, "tn": 12})
        for name in ("tp", "fp", "fn", "tn"):
            self.assertEqual(card.pooled[name], sum(getattr(r, name) for r in card.windows))
            self.assertIs(type(card.pooled[name]), int)
        ops = card.metrics["operation_totals"]
        self.assertEqual(ops["actual"], {"pixels": 24, "reductions": 4})
        self.assertEqual(ops["actual"], ops["isolated"])
        self.assertTrue(all(w["matches"] for w in ops["windows"]))
        shared = {}
        for adapter in adapters.values():
            adapter.memory, adapter.operation_bleed = shared, True
        _, bad = self.run_case(scenario, adapters)
        self.assertFalse(bad.metrics["operation_totals"]["passed"])
        self.assertFalse(bad.passed)

    def test_missing_denominators_and_no_operation_support_fail(self):
        empty = m.score_windows(())
        self.assertFalse(empty.passed)
        for name, metric in empty.metrics.items():
            with self.subTest(metric=name):
                self.assertIsNone(metric["denominator"])
                self.assertIsNone(metric["numerator"])
                self.assertFalse(metric["passed"])
        scenario, adapters = _scenario()
        for adapter in adapters.values():
            adapter.empty_ops = True
        _, card = self.run_case(scenario, adapters)
        self.assertIsNone(card.metrics["operation_totals"]["denominator"])
        self.assertFalse(card.passed)

    def test_frame_identity_torn_pixels_and_false_negative_controls(self):
        for mode in ("foreign", "torn", "pixels", "miss"):
            with self.subTest(mode=mode):
                scenario, adapters = _scenario(count=1)
                adapter = adapters[_key(scenario.targets[0])]
                if mode == "miss":
                    adapter.miss = True
                else:
                    adapter.stream_mode = mode
                _, card = self.run_case(scenario, adapters)
                self.assertFalse(card.passed)
                if mode in ("foreign", "torn"):
                    self.assertFalse(any(c[0] == "execute" for c in adapter.calls))
                if mode == "torn":
                    self.assertIn("torn_frame", card.violations)
                if mode == "pixels":
                    self.assertFalse(card.metrics["frame_exactness"]["passed"])
                if mode == "miss":
                    self.assertGreater(card.pooled["fn"], 0)
                    self.assertFalse(card.metrics["pooled_confusion"]["passed"])

    def test_expected_drops_preserved_and_all_drop_support_is_absent(self):
        scenario, adapters = _scenario()
        target = scenario.targets[0]
        adapters[_key(target)].drop_seqs.add(1)
        events = tuple({**e, "drop_expected": True} if e["handle"] == target and e["frame_seq"] == 1
                       else e for e in scenario.events)
        expected = dict(scenario.expected_outcomes)
        expected[events[2]["action"].action_id] = "DROPPED"
        scenario = replace(scenario, events=events, expected_outcomes=expected)
        _, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(card.metrics["frame_drops"]["observed"], 1)
        self.assertEqual(card.metrics["frame_drops"]["expected"], 1)
        for adapter in adapters.values():
            adapter.drop_seqs.update({0, 1})
        events = tuple({**e, "drop_expected": True} for e in events)
        scenario = replace(scenario, events=events,
                           expected_outcomes={k: "DROPPED" for k in expected})
        _, absent = self.run_case(scenario, adapters)
        self.assertFalse(absent.passed)
        self.assertIsNone(absent.metrics["frame_exactness"]["denominator"])
        self.assertEqual(absent.metrics["frame_drops"]["observed"], 4)

    def test_expected_faults_unknown_and_local_refusal_are_not_success(self):
        for terminal in ("CRASH", "TIMEOUT", "UNKNOWN", "ADMITTED"):
            with self.subTest(terminal=terminal):
                scenario, adapters = _scenario(count=1)
                events = list(scenario.events)
                action = events[0]["action"]
                events[0] = {**events[0], "action": replace(action, payload={"terminal": terminal})}
                expected = {**scenario.expected_outcomes, action.action_id: terminal}
                report, card = self.run_case(replace(scenario, events=tuple(events),
                                                     expected_outcomes=expected), adapters)
                if terminal in ("CRASH", "TIMEOUT"):
                    self.assert_pass(card)
                    self.assertEqual(card.metrics["terminal_completeness"]["fault_outcomes"]["numerator"], 1)
                    self.assertTrue(report.faults)
                else:
                    self.assertFalse(card.passed)
                    self.assertEqual(card.metrics["terminal_completeness"]["fault_outcomes"]["numerator"], 0)
                self.assertEqual(card.metrics["terminal_completeness"]["numerator"], 1)
        scenario, adapters = _scenario(count=1)
        first = scenario.events[0]
        action = replace(first["action"], payload={"admission": "REFUSED"})
        scenario = replace(scenario, events=({**first, "action": action},) + scenario.events[1:])
        report, card = self.run_case(scenario, adapters)
        self.assertFalse(card.passed)
        self.assertEqual(report.rows[0].terminal, "REFUSED")
        self.assertFalse(any(c[0] == "terminal" for c in adapters[_key(scenario.targets[0])].calls))

    def test_fault_exception_quarantine_and_acknowledged_new_generation_recovery(self):
        old, new, peer = (m.Handle("cdp_tab", "recover", 2),
                          m.Handle("cdp_tab", "recover", 2 ** 100),
                          m.Handle("godot_window", "peer", 1))
        adapters = {_key(h): FixtureAdapter(h) for h in (old, new, peer)}
        events, expected = [], {}
        for handle, seq, at, aid, payload, terminal in (
            (old, 0, 10, "crash", {"raise": "crash"}, "CRASH"),
            (old, 1, 20, "fenced", {}, "QUARANTINED")):
            event, outcome = _event(handle, seq, at, action_id=aid, payload=payload, expected=terminal)
            events.append(event); expected.update([outcome])
        events.append({"kind": "recover", "handle": new, "previous_handle": old,
                       "acknowledged": True, "at_ns": 30})
        for handle, at, aid in ((new, 40, "recovered"), (peer, 50, "peer")):
            event, outcome = _event(handle, 0, at, action_id=aid)
            events.append(event); expected.update([outcome])
        scenario = m.Scenario("recovery", (old, new, peer), tuple(events), expected)
        report, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(report.outcomes["actions"]["fenced"]["terminal"], "QUARANTINED")
        self.assertEqual(report.outcomes["actions"]["recovered"]["terminal"], "SUCCESS")
        self.assertEqual(report.outcomes["recoveries"][0]["generation"], 2 ** 100)
        self.assertTrue(report.outcomes["recoveries"][0]["recovered"])
        events[2] = {**events[2], "acknowledged": False}
        report, bad = self.run_case(replace(scenario, events=tuple(events)), adapters)
        self.assertFalse(bad.passed)
        self.assertEqual(report.outcomes["actions"]["recovered"]["terminal"], "QUARANTINED")

    def test_unsafe_scheduler_telemetry_refuses_before_actor(self):
        mutants = [{name: False} for name in
                   ("focus_authorized", "handle_fresh", "fence_clear", "guard_passed", "geometry_matches")]
        mutants += [{"active_resource": 2}, {"active_resource": 0}, {"active_focus": 2},
                    {"global_cap": 0}, {"target_cap": 0}, {"global_reserved": 0},
                    {"target_reserved": 0}]
        for telemetry in mutants:
            with self.subTest(telemetry=telemetry):
                scenario, adapters = _scenario(count=1)
                adapter = adapters[_key(scenario.targets[0])]
                adapter.telemetry = telemetry
                _, card = self.run_case(scenario, adapters)
                self.assertFalse(card.passed)
                self.assertFalse(any(c[0] == "execute" for c in adapter.calls))
        scenario, adapters = _scenario(count=1)
        adapter = adapters[_key(scenario.targets[0])]
        adapter.missing_telemetry.add("handle_fresh")
        _, card = self.run_case(scenario, adapters)
        self.assertFalse(card.passed)
        self.assertFalse(any(c[0] == "execute" for c in adapter.calls))

    def test_eligibility_stalls_and_fair_opportunity_bound_preserved(self):
        scenario, adapters = _scenario(count=1)
        stalled = adapters[_key(scenario.targets[0])]
        stalled.admit = stalled.eligible = False
        stalled.reason = "FOCUS_BUSY"
        stalled.telemetry = {"focus_authorized": False, "wait_opportunities": 100,
                              "queue_depth": 5}
        action_id = scenario.events[0]["action"].action_id
        scenario = replace(scenario, expected_outcomes={**scenario.expected_outcomes, action_id: "FOCUS_BUSY"})
        _, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        metric = card.metrics["eligibility_stalls"]
        self.assertEqual(sum(v.get("FOCUS_BUSY", 0) for v in metric["windows"].values()), 1)
        self.assertEqual(max(metric["queue_maxima"].values()), 5)
        self.assertFalse(any(c[0] == "execute" for c in stalled.calls))
        for wait, passes in ((4, True), (5, False)):
            scenario, adapters = _scenario(count=1)
            for adapter in adapters.values():
                adapter.telemetry["wait_opportunities"] = wait
            _, result = self.run_case(scenario, adapters)
            self.assertEqual(result.metrics["starvation"]["passed"], passes)
            self.assertEqual(result.metrics["starvation"]["bound_opportunities"], 4)

    def test_prerequisites_absent_or_expected_failure_block_before_any_callback(self):
        for gate in m.PREREQUISITE_GATES:
            with self.subTest(missing=gate):
                scenario, adapters = _scenario(count=1)
                adapters[_key(scenario.targets[0])].gates.pop(gate)
                report, card = self.run_case(scenario, adapters)
                self.assertTrue(report.outcomes["blocked"])
                self.assertEqual(report.outcomes["gates"][gate]["status"], "AUSENTE")
                self.assertEqual(report.rows, ())
                self.assertFalse(card.passed)
                self.assertTrue(all(not a.calls for a in adapters.values()))
        for mode in ("expected_failure", "failed", "offline_missing", "adapter_missing"):
            with self.subTest(mode=mode):
                scenario, adapters = _scenario(count=1)
                adapter = adapters[_key(scenario.targets[0])]
                if mode == "expected_failure":
                    adapter.gates["G-M1-1"]["expected_failure"] = True
                elif mode == "failed":
                    adapter.gates["G-M1-1"]["status"] = "FAIL"
                elif mode == "offline_missing":
                    adapter.offline = False
                else:
                    adapters.pop(_key(scenario.targets[0]))
                report, _ = self.run_case(scenario, adapters)
                self.assertTrue(report.outcomes["blocked"])
                self.assertTrue(all(not a.calls for a in adapters.values()))

    def test_structural_handles_and_invalid_sequences_refuse_before_callbacks(self):
        scenario, adapters = _scenario(count=1)
        original = scenario.targets[0]
        structural = SimpleNamespace(kind=original.kind, canonical_id=original.canonical_id,
                                     generation=original.generation)
        event = scenario.events[0]
        action = event["action"]
        event = {**event, "handle": structural,
                 "action": SimpleNamespace(handle=structural, action_id=action.action_id,
                    epoch=action.epoch, payload={}, cost=1, deadline_ns=action.deadline_ns)}
        scenario = replace(scenario, targets=(structural,) + scenario.targets[1:],
                           events=(event,) + scenario.events[1:])
        _, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        for mutant in (replace(scenario, events=scenario.events + (event,)),
                       replace(scenario, targets=scenario.targets + (structural,))):
            with self.assertRaises(ValueError):
                self.run_case(mutant, adapters)
        with self.assertRaises(ValueError):
            m.score_windows((replace(card.windows[0], tp=True),))

    def test_unsettled_change_deadline_and_cross_target_receipts_fail(self):
        for mode in ("unsettled", "deadline", "receipt", "change", "registry"):
            with self.subTest(mode=mode):
                scenario, adapters = _scenario(count=1)
                adapter = adapters[_key(scenario.targets[0])]
                if mode == "unsettled":
                    adapter.settled = False
                elif mode == "deadline":
                    event = scenario.events[0]
                    event = {**event, "action": replace(event["action"], payload={"advance_ns": 1000000})}
                    scenario = replace(scenario, events=(event,) + scenario.events[1:])
                else:
                    setattr(adapter, mode + "_handle", scenario.targets[1])
                report, card = self.run_case(scenario, adapters)
                self.assertFalse(card.passed)
                if mode in ("unsettled", "change", "registry"):
                    self.assertFalse(any(c[0] == "execute" for c in adapter.calls))
                if mode == "deadline":
                    self.assertEqual(report.rows[0].terminal, "TIMEOUT")

    def test_readonly_borrow_and_mutated_observation_do_not_reach_actor(self):
        scenario, adapters = _scenario(count=1)
        adapter = adapters[_key(scenario.targets[0])]
        adapter.stream_mode, adapter.mutate_borrow = "borrow", True
        _, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertTrue(adapter.actor_pixels)
        self.assertTrue(all(p == scenario.events[0]["rgb"].tobytes() for p in adapter.actor_pixels))
        self.assertTrue(all(s["cursor"] == 0 for s in adapter.actor_states))

    def test_capsule_child_share_resource_and_timeout_fences_destination(self):
        capsule = m.Handle("capsule", "capsule-a", 1)
        child = m.Handle("xvfb_screen", "child-screen", 1)
        peer = m.Handle("cdp_tab", "peer", 1)
        adapters = {_key(capsule): FixtureAdapter(capsule, resource_key="display/same"),
                    _key(child): FixtureAdapter(child, resource_key="display/same"),
                    _key(peer): FixtureAdapter(peer)}
        events, expected = [], {}
        cases = ((child, 0, {}, "SUCCESS"), (capsule, 0, {"terminal": "TIMEOUT"}, "TIMEOUT"),
                 (child, 1, {}, "QUARANTINED"), (peer, 0, {}, "SUCCESS"))
        for index, (handle, seq, payload, terminal) in enumerate(cases):
            event, outcome = _event(handle, seq, 10 * (index + 1), payload=payload, expected=terminal)
            events.append(event); expected.update([outcome])
        report, card = self.run_case(m.Scenario("shared-resource", (capsule, child, peer),
                                              tuple(events), expected), adapters)
        self.assertEqual(report.rows[2].terminal, "QUARANTINED")
        self.assertFalse(report.rows[2].ops["sent"])
        self.assertEqual(report.rows[0].ops["resource_key"], report.rows[1].ops["resource_key"])
        self.assertNotIn("cross_window_state_bleed", card.violations)

    def test_real_godot_executor_and_complete_outbox_with_independent_tab(self):
        scenario, adapters = _scenario(count=2)
        godot = scenario.targets[1]
        adapters[_key(godot)] = GodotFixtureAdapter(godot)
        expected = {aid: ("ACCEPTED" if aid.startswith("godot_window:") else terminal)
                    for aid, terminal in scenario.expected_outcomes.items()}
        scenario = replace(scenario, expected_outcomes=expected)
        report, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(adapters[_key(godot)].executor.next_seq, 3)
        self.assertEqual(len(adapters[_key(godot)].batches), 2)
        self.assertEqual(adapters[_key(scenario.targets[0])].memory["cursor"], 2)
        self.assertEqual([r.frame_seq for r in report.rows if r.handle.kind == "godot_window"], [0, 1])

    def test_forbidden_io_raises_and_trial_stays_entirely_in_ram(self):
        scenario, adapters = _scenario(count=1)
        with ExitStack() as stack:
            for target in ("builtins.open", "io.open", "socket.socket", "socket.create_connection",
                           "subprocess.Popen", "os.system", "time.sleep", "time.time",
                           "time.monotonic", "time.perf_counter", "numpy.save", "numpy.savez"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden I/O or real clock")))
            report, card = self.run_case(scenario, adapters)
            self.assert_pass(card)
            self.assertTrue(m.canonical_bytes(report))
        self.assertEqual(report.outcomes["absent"], {"measured_host_speed": "AUSENTE",
                         "native_capture": "AUSENTE", "G-T4": "AUSENTE", "G-T5": "AUSENTE"})

    def test_callbacks_cannot_rewrite_future_scenario_truth_or_expectations(self):
        reference, reference_adapters = _scenario()
        pristine, pristine_card = self.run_case(reference, reference_adapters)
        self.assert_pass(pristine_card)
        scenario, adapters = _scenario()
        future = scenario.events[2]
        original_action = future["action"]

        def mutate_original_inputs():
            original_action.payload["terminal"] = "UNKNOWN"
            future["action"] = replace(original_action, cost=100001)
            future["rgb"].fill(0)
            future["truth"].fill(True)
            scenario.expected_outcomes[original_action.action_id] = "UNKNOWN"

        adapters[_key(scenario.targets[0])].before_reset = mutate_original_inputs
        detached, card = self.run_case(scenario, adapters)
        self.assert_pass(card)
        self.assertEqual(m.canonical_bytes(detached), m.canonical_bytes(pristine))
        self.assertEqual(detached.rows[2].expected_terminal, "SUCCESS")
        self.assertEqual(detached.rows[2].tp, 3)
        self.assertEqual(scenario.expected_outcomes[original_action.action_id], "UNKNOWN")

    def test_semantic_timestamp_is_not_erased_by_diagnostic_projection(self):
        for resets in (True, False):
            with self.subTest(resets=resets):
                scenario, adapters = _scenario()
                shared = {"tick": 0}

                def timestamp_state():
                    shared["tick"] += 1
                    return {"timestamp": shared["tick"]}

                for adapter in adapters.values():
                    adapter.extra_state = timestamp_state
                    if resets:
                        adapter.before_reset = lambda: shared.update(tick=0)
                report, card = self.run_case(scenario, adapters)
                self.assertFalse(card.passed)
                self.assertFalse(card.metrics["cross_window_bleed"]["passed"])
                self.assertEqual(card.pooled["fn"], 0)
                self.assertTrue(card.metrics["terminal_completeness"]["passed"])
                if not resets:
                    self.assertFalse(report.outcomes["canonical_replay"]["matched"])

    def test_replayed_or_stale_recovery_cannot_clear_a_newer_fence(self):
        for jump_generation in (False, True):
            with self.subTest(jump_generation=jump_generation):
                old = m.Handle("cdp_tab", "recover-fence", 1)
                new = m.Handle("cdp_tab", "recover-fence", 2)
                retry = m.Handle("cdp_tab", "recover-fence", 3) if jump_generation else new
                peer = m.Handle("godot_window", "healthy", 1)
                targets = (old, new, retry, peer) if jump_generation else (old, new, peer)
                adapters = {_key(h): FixtureAdapter(h) for h in targets}
                events, expected = [], {}
                for handle, at, aid in ((old, 10, "old-timeout"), (new, 30, "new-timeout")):
                    event, outcome = _event(handle, 0, at, action_id=aid,
                                           payload={"terminal": "TIMEOUT"}, expected="TIMEOUT")
                    events.append(event); expected.update([outcome])
                    if handle == old:
                        events.append({"kind": "recover", "handle": new,
                            "previous_handle": old, "acknowledged": True, "at_ns": 20})
                events.append({"kind": "recover", "handle": retry,
                    "previous_handle": old, "acknowledged": True, "at_ns": 40})
                for handle, seq, at, aid, terminal in (
                    (retry, 0 if jump_generation else 1, 50, "still-fenced", "QUARANTINED"),
                    (peer, 0, 60, "healthy", "SUCCESS")):
                    event, outcome = _event(handle, seq, at, action_id=aid, expected=terminal)
                    events.append(event); expected.update([outcome])
                report, card = self.run_case(m.Scenario("stale-recovery", targets,
                                                       tuple(events), expected), adapters)
                self.assertFalse(card.passed)
                self.assertEqual([r["recovered"] for r in report.outcomes["recoveries"]], [True, False])
                self.assertEqual(report.outcomes["actions"]["still-fenced"]["terminal"], "QUARANTINED")
                self.assertFalse(report.outcomes["actions"]["still-fenced"]["sent"])

    def test_charged_ledgers_reject_underreported_global_and_shared_resource_totals(self):
        for scope in ("global", "resource"):
            with self.subTest(scope=scope):
                kinds = ("cdp_tab", "godot_window") if scope == "global" else ("capsule", "xvfb_screen")
                scenario, adapters = _scenario(kinds)
                for adapter in adapters.values():
                    adapter.telemetry = {"global_committed": 0, "target_committed": 0,
                        "global_cap": 2 if scope == "global" else 100,
                        "target_cap": 2 if scope == "resource" else 100}
                    if scope == "resource":
                        adapter.resource_key = "same-canonical-screen"
                        adapter.focus_domain = "same-canonical-focus"
                report, card = self.run_case(scenario, adapters)
                self.assertFalse(card.passed)
                self.assertEqual([r.ops["sent"] for r in report.rows], [True, True, False, False])
                violation = "global_budget_exceeded" if scope == "global" else "target_budget_exceeded"
                self.assertIn(violation, card.violations)


if __name__ == "__main__":
    unittest.main()
