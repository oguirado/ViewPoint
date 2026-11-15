# @module {"id":"tests.test_p1_wc_score_seal_unit","layer":3,"owner":"TESTS","deps":["wa.learn.multiwin_scorecard","wa.perceive.session_registry"]}
"""C-score/C-lanes: offline, exact-identity scorecard and registry evidence.

Providers are imported by undecorated tests, never replaced with fallback APIs.
Only the external stream/action transport is simulated. No frame is written.
"""
from __future__ import annotations

import copy
from dataclasses import fields, replace
import hashlib
import json
import random
from types import SimpleNamespace
import unittest


class _Transport:
    def __init__(self):
        self.cleanup_calls = 0

    def close(self):
        self.cleanup_calls += 1


class _OfflineLane:
    """Real session registry, frame ownership, detector, budget and actor lock."""

    offline = True

    def __init__(self, m, reg, gov, locks, vision, handle, *, mode=None, caller_rgb=None):
        self.m, self.reg, self.gov, self.locks_api, self.vision_api = m, reg, gov, locks, vision
        self.handle, self.mode, self.caller_rgb = handle, mode, caller_rgb
        self.resource_key = handle.kind + "/" + handle.canonical_id
        self.focus_domain = "focus/" + self.resource_key
        self.gates = {g: {"status": "PASS", "evidence": "injected RAM fixture " + g,
                          "expected_failure": False} for g in m.PREREQUISITE_GATES}
        self.stream = SimpleNamespace(deliver=self.deliver)
        self.perception = SimpleNamespace(observe=self.observe, reduce=self.reduce)
        self.scheduler = SimpleNamespace(admit=self.admit)
        self.actor = SimpleNamespace(execute=self.execute)
        self.send_log = []
        self.admission_log = []
        self.reset_log = []
        self.transport = _Transport()
        # Preflight inspects the real resolver before it calls reset().
        self.registry = self.reg.SessionRegistry(clock=lambda: 0)
        self.registry.register(self.handle.kind, self.handle.canonical_id,
                               self.transport, owned=False)

    def reset(self, *, seed, clock):
        self.clock = clock
        self.rng = random.Random(seed)
        self.cursor = 0
        self.registry = self.reg.SessionRegistry(clock=clock)
        issued = self.registry.register(self.handle.kind, self.handle.canonical_id,
                                        self.transport, owned=False)
        if issued != self.handle:
            raise AssertionError("registry issued unexpected incarnation")
        self.vision = self.vision_api.FakeVisionEngine(tile=1, threshold=0)
        self.budget_events = []
        self.budget = self.gov.BudgetGovernor(100, 100, service_cap_ns=1,
                                              clock=clock, telemetry=self.budget_events)
        self.lock_registry = self.locks_api.TargetLockRegistry(self.registry, clock=clock)
        self.reset_log.append(seed)

    def deliver(self, frame):
        valid = self.registry.admit_frame(frame.handle, frame.frame_seq, frame.rgb,
                                          epoch=frame.epoch)
        self.admission_log.append((valid.handle, valid.epoch, valid.frame_seq,
                                   valid.digest, valid.rgb.tobytes()))
        if self.caller_rgb is not None:
            self.caller_rgb.fill(231)
        if self.mode == "torn":
            # Constructor must first accept the authentic RGB24 buffer.
            # Corrupt the already-constructed delivery to exercise the scorecard.
            object.__setattr__(valid, "digest", "0" * 64)
        elif self.mode == "constructor_reject":
            return self.reg.FrameRef(valid.handle, valid.epoch, valid.frame_seq,
                                     valid.width, valid.height, valid.rgb, "0" * 64)
        elif self.mode == "pixels":
            modified = valid.rgb.copy()
            modified[0, 0, 0] = 0 if modified[0, 0, 0] else 255
            return self.reg.FrameRef.from_rgb(valid.handle, valid.epoch,
                                              valid.frame_seq, modified)
        elif self.mode == "foreign":
            foreign = self.reg.Handle(valid.handle.kind, valid.handle.canonical_id,
                                      valid.handle.generation + 1)
            return self.reg.FrameRef.from_rgb(foreign, valid.epoch, valid.frame_seq,
                                              valid.rgb)
        return valid

    def observe(self, frame):
        value = self.vision_api.Frame(frame.width, frame.height, frame.rgb.tobytes())
        state = {"cursor": self.cursor, "fingerprint": self.vision.screen_identity(value),
                 "random": self.rng.getrandbits(64)}
        return self.registry.admit_observation(self.handle, frame, state=state,
                                                dpr_ppm=1_000_000)

    def reduce(self, observation):
        frame = observation.frame
        empty = self.vision_api.Frame(frame.width, frame.height,
                                       bytes(frame.width * frame.height * 3))
        actual = self.vision_api.Frame(frame.width, frame.height, frame.rgb.tobytes())
        regions = self.vision.changed_regions(empty, actual)
        if self.mode == "no_regions":
            regions = []  # No detector evidence cannot establish settled.
        import numpy as np
        claims = np.zeros((frame.height, frame.width), dtype=np.bool_)
        for r in regions:
            claims[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]] = True
        self.cursor += 1
        return self.reg.ChangeResult(self.handle, observation.epoch, bool(regions), claims,
                                     {"pixels": int(claims.size), "regions": len(regions),
                                      "reductions": 1})

    def admit(self, action, observation, change, now_ns):
        self.reservation = self.budget.reserve(action.handle, action.cost,
                                                deadline_ns=action.deadline_ns,
                                                now_ns=now_ns, action_id=action.action_id)
        committed = self.budget.spent_global
        telemetry = {
            "resource_key": self.resource_key, "active_resource": 1, "active_focus": 1,
            "focus_authorized": True, "handle_fresh": True, "fence_clear": True,
            "guard_passed": True, "geometry_matches": True,
            "global_committed": committed, "global_reserved": action.cost, "global_cap": 100,
            "target_committed": committed, "target_reserved": action.cost, "target_cap": 100,
            "wait_opportunities": 0, "queue_depth": 1, "fairness_cost": 1,
            "quantum": 1, "urgent_cap": 1,
        }
        return {"admitted": True, "eligible": True, "reason": None,
                "telemetry": telemetry}

    def execute(self, action, observation, now_ns):
        lease = self.lock_registry.acquire(action.handle, "offline-driver",
                                            expires_ns=action.deadline_ns,
                                            now_ns=now_ns)
        if not self.lock_registry.owns(lease, now_ns=now_ns):
            raise AssertionError("live lock ownership missing")
        self.lock_registry.mark_inflight(lease)
        self.budget.commit(self.reservation)  # irreversible before fake external send
        self.send_log.append((action.action_id, observation.frame.digest,
                              observation.frame.rgb.tobytes()))
        self.lock_registry.reconcile_quarantine(action.handle, acknowledged=True,
                                                 expected_lease=lease)
        return self.reg.Receipt(action.action_id, action.handle, "ADMITTED", None)

    def terminal(self, action, receipt, now_ns):
        return self.reg.Receipt(action.action_id, action.handle, "SUCCESS", None)


def _fixture(m, reg, gov, locks, vision, *, lanes=2, events_per_lane=2, mode=None,
             caller_rgb=None):
    import numpy as np
    kinds = ("cdp_tab", "godot_window")[:lanes]
    handles = tuple(reg.Handle(kind, "common", 1) for kind in kinds)
    adapters = {(h.kind, h.canonical_id, h.generation):
                _OfflineLane(m, reg, gov, locks, vision, h,
                             mode=mode if h == handles[0] else None,
                             caller_rgb=caller_rgb if h == handles[0] else None)
                for h in handles}
    events, expected = [], {}
    for seq in range(events_per_lane):
        for h in handles:
            rgb = np.zeros((2, 3, 3), dtype=np.uint8)
            rgb[0, 0, 0] = 100 + seq
            rgb[0, 2, 0] = 100 + seq
            rgb[1, 2, 0] = 100 + seq
            identity = f"{h.kind}-{h.generation}-{seq}"
            action = reg.ActionRequest(h, identity, 0, {}, 1, 1_000_000)
            events.append({"handle": h, "scene": "same scene", "epoch": 0,
                           "frame_seq": seq, "at_ns": len(events) * 10 + 10,
                           "rgb": rgb, "truth": rgb[:, :, 0] != 0, "action": action})
            expected[identity] = "SUCCESS"
    return m.Scenario("C-offline", handles, tuple(events), expected), adapters


def _oracle_row(row, *, include_seal):
    # Independent explicit wire contract; do not call module _plain/_row_plain/_seal.
    wire = {"handle": [row.handle.kind, row.handle.canonical_id, row.handle.generation],
            "scene": row.scene, "epoch": row.epoch, "frame_seq": row.frame_seq,
            "tp": row.tp, "fp": row.fp, "fn": row.fn, "tn": row.tn,
            "ops": {k: copy.deepcopy(v) for k, v in row.ops.items() if k != "_trial"},
            "frame_supported": row.frame_supported, "dropped": row.dropped,
            "exact": row.exact, "terminal": row.terminal,
            "expected_terminal": row.expected_terminal,
            "violations": list(row.violations)}
    if include_seal:
        wire["seal"] = row.seal
        wire["ops"]["_trial"] = copy.deepcopy(row.ops["_trial"])
    return wire


def _oracle_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _oracle_seal(rows):
    return hashlib.sha256(_oracle_json({
        "manifest": rows[0].ops["_trial"],
        "rows": [_oracle_row(r, include_seal=False) for r in rows],
    })).hexdigest()


def _reseal(rows):
    seal = _oracle_seal(rows)
    return tuple(replace(r, seal=seal) for r in rows)


class TestP1ScoreSeal(unittest.TestCase):
    def _providers(self):
        from wender.learn import multiwin_scorecard as m
        from wender.perceive import session_registry as reg
        from wender.act import budget_governor as gov
        from wender.act import target_lock_registry as locks
        from wender.perceive import vision_adapter as vision
        return m, reg, gov, locks, vision

    def _positive(self, *, lanes=2, events_per_lane=2, mode=None, caller_rgb=None):
        m, reg, gov, locks, vision = self._providers()
        scenario, adapters = _fixture(m, reg, gov, locks, vision, lanes=lanes,
                                      events_per_lane=events_per_lane, mode=mode,
                                      caller_rgb=caller_rgb)
        report = m.run_trial(scenario, seed=173, clock=m.FakeClock(), adapters=adapters)
        return m, reg, scenario, adapters, report, m.score_windows(report.rows)

    def test_c_score_fields_hash_bytes_integer_pool_and_per_target_denominators(self):
        m, reg, _, _, report, card = self._positive()
        expected_fields = {
            m.Scenario: "name targets events expected_outcomes",
            m.TrialReport: "rows outcomes faults",
            m.Scorecard: "windows pooled metrics violations passed",
            m.WindowEvidence: "handle scene epoch frame_seq tp fp fn tn ops frame_supported dropped exact terminal expected_terminal seal violations",
            reg.Handle: "kind canonical_id generation",
        }
        for record, ordered in expected_fields.items():
            self.assertEqual([f.name for f in fields(record)], ordered.split())
        self.assertTrue(card.passed, (card.violations, card.metrics))
        self.assertEqual(len(card.windows), 4)
        self.assertEqual(card.pooled, {"tp": 12, "fp": 0, "fn": 0, "tn": 12})
        self.assertEqual((card.metrics["pooled_confusion"]["numerator"],
                          card.metrics["pooled_confusion"]["denominator"]), (24, 24))
        self.assertEqual(card.metrics["operation_totals"]["actual"],
                         {"pixels": 24, "reductions": 4, "regions": 8})
        self.assertEqual(card.metrics["operation_totals"]["isolated"],
                         {"pixels": 24, "reductions": 4, "regions": 8})
        self.assertEqual(card.metrics["frame_exactness"]["denominator"], 4)
        self.assertEqual(len(card.metrics["terminal_completeness"]["gates"]), 40)
        self.assertEqual(report.rows[0].seal, _oracle_seal(report.rows))
        self.assertTrue(all(r.seal == report.rows[0].seal for r in report.rows))
        # Public serialization checked against a separately assembled wire object.
        expected = _oracle_json({
            "rows": [_oracle_row(r, include_seal=True) for r in report.rows],
            "outcomes": report.outcomes, "faults": list(report.faults),
        })
        self.assertEqual(m.canonical_bytes(report), expected)
        self.assertEqual(hashlib.sha256(m.canonical_bytes(report)).hexdigest(),
                         hashlib.sha256(expected).hexdigest())
        self.assertEqual(report.outcomes["absent"]["measured_host_speed"], "AUSENTE")

    def test_c_lanes_isolated_independent_interleaved_replay_real_sends_and_cleanup(self):
        m, reg, scenario, adapters, report, card = self._positive()
        self.assertTrue(card.passed, card.violations)
        self.assertEqual(report.outcomes["execution_runs"], 4)
        self.assertEqual(report.outcomes["canonical_replay"]["matched"], True)
        for handle in scenario.targets:
            kind, cid, gen = handle.kind, handle.canonical_id, handle.generation
            solo_events = tuple(e for e in scenario.events if e["handle"] == handle)
            solo_expected = {e["action"].action_id: "SUCCESS" for e in solo_events}
            solo = m.Scenario(scenario.name, (handle,), solo_events, solo_expected)
            # Build an independent lane with matching concrete handle type/kind.
            solo_adapter = {(kind, cid, gen): _OfflineLane(m, reg, self._providers()[2],
                              self._providers()[3], self._providers()[4], handle)}
            isolated = m.run_trial(solo, seed=173, clock=m.FakeClock(), adapters=solo_adapter)
            isolated_card = m.score_windows(isolated.rows)
            self.assertTrue(isolated_card.passed, isolated_card.violations)
            main_rows = [r for r in report.rows if r.handle == handle]
            self.assertEqual(len(main_rows), len(isolated.rows))
            for mixed, separate in zip(main_rows, isolated.rows):
                self.assertEqual((mixed.tp, mixed.fp, mixed.fn, mixed.tn),
                                 (separate.tp, separate.fp, separate.fn, separate.tn))
                for field in ("counts", "state_digest", "claims_digest", "dpr_ppm"):
                    self.assertEqual(mixed.ops[field], separate.ops[field])
            self.assertEqual(isolated_card.pooled,
                             {"tp": 6, "fp": 0, "fn": 0, "tn": 6})
            a = adapters[(kind, cid, gen)]
            self.assertEqual(a.budget.spent_global, 2)
            self.assertEqual(len(a.send_log), 6)  # interleaved + isolated + repeated
            self.assertEqual(len(a.admission_log), 6)
            self.assertTrue(all(hashlib.sha256(raw).hexdigest() == digest
                                for _, digest, raw in a.send_log))
            self.assertEqual(a.transport.cleanup_calls, 0)
            a.registry.stop(handle)
            with self.assertRaises(reg.StaleHandleError):
                a.registry.resolve(handle)
            with self.assertRaises(reg.QuarantinedTargetError):
                a.registry.register(kind, cid, a.transport, owned=False)
            a.registry.acknowledge_cleanup(handle)
            reincarnated = a.registry.register(kind, cid, a.transport, owned=False)
            self.assertGreater(reincarnated.generation, handle.generation)
            self.assertEqual(a.transport.cleanup_calls, 0)
        same = _fixture(m, reg, self._providers()[2], self._providers()[3],
                        self._providers()[4])[0]
        # Independent fresh external providers, identical input/seed/clock.
        new_adapters = {tuple((h.kind, h.canonical_id, h.generation)):
                        _OfflineLane(m, reg, self._providers()[2], self._providers()[3],
                                     self._providers()[4], h) for h in same.targets}
        repeated = m.run_trial(same, seed=173, clock=m.FakeClock(), adapters=new_adapters)
        self.assertEqual(m.canonical_bytes(report), m.canonical_bytes(repeated))

    def test_c_score_content_seal_mutant_matrix(self):
        m, reg, _, _, report, good = self._positive()
        self.assertTrue(good.passed, good.violations)  # green before mutants
        rows = report.rows
        broken = {
            "tp_changed": (replace(rows[0], tp=rows[0].tp + 1),) + rows[1:],
            "swapped_identities": (replace(rows[0], handle=rows[1].handle),
                                   replace(rows[1], handle=rows[0].handle)) + rows[2:],
            "foreign_generation": (replace(rows[0], handle=reg.Handle(
                                   rows[0].handle.kind, rows[0].handle.canonical_id, 2)),) + rows[1:],
            "missing_isolated_comparator": _reseal((replace(rows[0], ops={
                                   **rows[0].ops, "expected_counts": None}),) + rows[1:]),
            "missing_denominator": _reseal(tuple(replace(r, ops={
                                   **r.ops, "counts": {}, "expected_counts": {}})
                                   for r in rows)),
            "uniform_fake_seal": tuple(replace(r, seal="f" * 64) for r in rows),
            "reordered_evidence": rows[::-1],
        }
        violation = {
            "tp_changed": "missing_mixed_or_invalid_seal",
            "swapped_identities": "missing_mixed_or_invalid_seal",
            "foreign_generation": "missing_or_foreign_rows",
            "missing_isolated_comparator": None,
            "missing_denominator": "missing_operation_support:",
            "uniform_fake_seal": "missing_mixed_or_invalid_seal",
            "reordered_evidence": "missing_mixed_or_invalid_seal",
        }
        kills, survivors = [], []
        for name, mutant in broken.items():
            with self.subTest(mutant=name):
                card = m.score_windows(mutant)
                if card.passed:
                    survivors.append(name)
                else:
                    kills.append(name)
                expected = violation[name]
                if expected:
                    self.assertTrue(any(v.startswith(expected) for v in card.violations),
                                    (name, card.violations))
                if name.startswith("missing_"):
                    self.assertFalse(card.metrics["operation_totals"]["passed"])
        self.assertEqual(survivors, [], "valid semantic survivor(s)")
        self.assertEqual(len(kills), 7, "7 valid mutations, 0 equivalents or invalid controls")
        self.assertEqual(m.score_windows(()).metrics["operation_totals"]["denominator"], None)
        self.assertFalse(m.score_windows(()).passed)

    def test_c_lanes_constructor_rejection_torn_rgb_pixel_and_foreign_generation(self):
        m, reg, _, _, original, positive = self._positive(lanes=1, events_per_lane=1)
        self.assertTrue(positive.passed, positive.violations)
        good_frame = reg.FrameRef.from_rgb(original.rows[0].handle, 0, 0,
                                           self._test_rgb())
        with self.assertRaisesRegex(reg.FrameValidationError,
                                    "RGB content digest mismatch"):
            reg.FrameRef(good_frame.handle, 0, 0, 3, 2, good_frame.rgb, "0" * 64)
        expectations = {
            "constructor_reject": "component_stream_FrameValidationError",
            "torn": "torn_frame",
            "pixels": "pixel_mismatch",
            "foreign": "cross_window_frame",
        }
        for mode, exact in expectations.items():
            with self.subTest(mode=mode):
                _, _, _, adapters, report, card = self._positive(
                    lanes=1, events_per_lane=1, mode=mode)
                self.assertFalse(card.passed)
                self.assertIn(exact, card.violations)
                if mode != "torn":
                    self.assertNotIn("torn_frame", card.violations)
                self.assertEqual(sum(len(a.send_log) for a in adapters.values()), 0)
                self.assertEqual(len(report.faults), 1)
                self.assertEqual(report.faults[0]["stage"], "stream")

    def test_empty_detector_support_never_dispatches_or_spends(self):
        m, reg, _, adapters, report, card = self._positive(
            lanes=1, events_per_lane=1, mode="no_regions")
        self.assertFalse(card.passed)
        self.assertEqual(report.rows[0].terminal, "NOT_READY")
        self.assertEqual(card.pooled["fn"], 3)
        lane = next(iter(adapters.values()))
        self.assertEqual(lane.send_log, [])
        self.assertEqual(lane.budget.spent_global, 0)
        self.assertFalse(report.rows[0].ops["scheduler_attempted"])
        self.assertEqual(len(lane.admission_log), 3)
        self.assertFalse(card.metrics["pooled_confusion"]["passed"])

    @staticmethod
    def _test_rgb():
        import numpy as np
        rgb = np.zeros((2, 3, 3), dtype=np.uint8)
        rgb[0, 0, 0] = rgb[0, 2, 0] = rgb[1, 2, 0] = 100
        return rgb

    def test_c_lanes_caller_mutation_after_admission_and_structural_generation(self):
        m, reg, gov, locks, vision = self._providers()
        import numpy as np
        rgb = self._test_rgb()
        expected = rgb.tobytes()
        scenario, adapters = _fixture(m, reg, gov, locks, vision, lanes=1,
                                      events_per_lane=1, caller_rgb=rgb)
        # Event pixels are detached before the first component callback.
        scenario = replace(scenario, events=({**scenario.events[0], "rgb": rgb,
                  "truth": np.array([[True, False, True], [False, False, True]], dtype=np.bool_)},))
        report = m.run_trial(scenario, seed=173, clock=m.FakeClock(), adapters=adapters)
        card = m.score_windows(report.rows)
        self.assertTrue(card.passed, card.violations)
        self.assertNotEqual(rgb.tobytes(), expected)
        actual_adapter = next(iter(adapters.values()))
        self.assertEqual(len(actual_adapter.send_log), 3)
        self.assertTrue(all(blob == expected for _, _, blob in actual_adapter.send_log))
        self.assertTrue(all(not a[4] == rgb.tobytes() for a in actual_adapter.admission_log))
        valid = SimpleNamespace(kind="cdp_tab", canonical_id="common", generation=1)
        self.assertEqual(m.handle_key(valid), ("cdp_tab", "common", 1))
        with self.assertRaisesRegex(m.EvidenceError, "generation must be an integer >= 1"):
            m.handle_key(SimpleNamespace(kind="cdp_tab", canonical_id="common", generation=0))
        with self.assertRaisesRegex(reg.RegistryError, "generation must be an integer >= 1"):
            reg.Handle("cdp_tab", "common", 0)


if __name__ == "__main__":
    unittest.main()
