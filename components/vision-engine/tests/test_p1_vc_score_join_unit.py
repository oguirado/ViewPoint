"""Area C: independent replay, generational joins, content seals and pooled gates.

VM gate: python3 -m unittest tests.test_p1_vc_score_join_unit
"""
from __future__ import annotations

import copy
from dataclasses import fields, replace
from fractions import Fraction
import hashlib
import json
import threading
import unittest

import numpy as np

from eval.v2eval import multiwin_scorecard as sc


def _handle_key(handle):
    """Independent test coordinate, not a call to subject canonical_handle_key."""
    return handle.kind, handle.canonical_id, handle.generation


def _row(handle, *, scene="scene", epoch=0, frame_seq=0,
         tp=3, fp=1, fn=1, tn=9, ops=None, supported=True,
         dropped=False, exact=True, terminal="OK", expected="OK"):
    if ops is None:
        ops = {"decode": 2, "fl": 3, "isolated_ops": {"decode": 2, "fl": 3}}
    return sc.WindowEvidence(
        handle=handle, scene=scene, epoch=epoch, frame_seq=frame_seq,
        tp=tp, fp=fp, fn=fn, tn=tn, ops=ops, frame_supported=supported,
        dropped=dropped, exact=exact, terminal=terminal,
        expected_terminal=expected, seal="", violations=(),
    )


def _report(*rows, outcomes=None):
    return sc.seal_trial(sc.TrialReport(tuple(rows), outcomes or {}))


def _replace_row(report, index, *, reseal=False, **updates):
    rows = list(report.rows)
    rows[index] = replace(rows[index], **updates)
    changed = replace(report, rows=tuple(rows))
    return sc.seal_trial(changed) if reseal else changed


def _assert_code(test, expected, fn):
    with test.assertRaises(sc.ScorecardValidationError) as caught:
        fn()
    test.assertEqual(caught.exception.code, expected)
    test.assertTrue(str(caught.exception).startswith(expected + ":"))


def _oracle_clean(value):
    """Independent small literal JSON oracle for this test's primitive fixtures."""
    if isinstance(value, dict):
        return {k: _oracle_clean(v) for k, v in value.items()
                if k not in ("measured_host_speed", "wall_ms", "wall_ns",
                             "diagnostic_timing", "host_ms", "host_name")}
    if isinstance(value, (tuple, list)):
        return [_oracle_clean(x) for x in value]
    return value


def _oracle_row(row, *, blank_seal=False):
    """Literal field-by-field preimage: no dataclasses.asdict or subject serialization."""
    h = {"kind": row.handle.kind, "canonical_id": row.handle.canonical_id,
         "generation": row.handle.generation}
    return {
        "handle": h, "scene": row.scene, "epoch": row.epoch, "frame_seq": row.frame_seq,
        "tp": row.tp, "fp": row.fp, "fn": row.fn, "tn": row.tn,
        "ops": _oracle_clean(row.ops), "frame_supported": row.frame_supported,
        "dropped": row.dropped, "exact": row.exact, "terminal": row.terminal,
        "expected_terminal": row.expected_terminal,
        "seal": "" if blank_seal else row.seal, "violations": list(row.violations),
    }


def _compact(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _oracle_seal(report):
    rows = sorted(report.rows, key=lambda r: (*_handle_key(r.handle), r.scene, r.epoch, r.frame_seq))
    manifest = {
        "schema": "vqa.multiwin_manifest/v1",
        "registered_handles": [list(x) for x in sorted({_handle_key(r.handle) for r in rows})],
        "row_refs": [list((*_handle_key(r.handle), r.scene, r.epoch, r.frame_seq)) for r in rows],
        "outcomes": _oracle_clean(report.outcomes), "faults": list(report.faults),
    }
    data = {"schema": "vqa.multiwin_evidence_seal/v1", "manifest": manifest,
            "rows": [_oracle_row(r, blank_seal=True) for r in rows]}
    return hashlib.sha256(_compact(data)).hexdigest()


class TestScoreJoin(unittest.TestCase):
    def test_00_contract_field_orders_and_handle_identity(self):
        self.assertEqual(
            tuple(f.name for f in fields(sc.WindowEvidence)),
            ("handle", "scene", "epoch", "frame_seq", "tp", "fp", "fn", "tn",
             "ops", "frame_supported", "dropped", "exact", "terminal",
             "expected_terminal", "seal", "violations"),
        )
        self.assertEqual(tuple(f.name for f in fields(sc.TrialReport)), ("rows", "outcomes", "faults"))
        self.assertEqual(tuple(f.name for f in fields(sc.Scorecard)),
                         ("windows", "pooled", "metrics", "violations", "passed"))
        h1 = sc.Handle("cdp_tab", "a:b:2", 1)
        h2 = sc.Handle("cdp_tab", "a:b", 2)
        self.assertNotEqual(sc.canonical_handle_key(h1), sc.canonical_handle_key(h2))
        self.assertNotEqual(sc.canonical_handle_key(h1),
                            sc.canonical_handle_key(replace(h1, generation=2)))
        self.assertEqual(sc.Handle("cdp_tab", "x").generation, 1)
        for bad in (0, -1, True, 1.0, "1"):
            with self.subTest(generation=bad):
                _assert_code(self, "E_WINDOW_IDENTITY", lambda: sc.Handle("cdp_tab", "x", bad))

    def test_01_production_solo_replay_and_interleaving(self):
        handles = (
            sc.Handle("cdp_tab", "tab-A", 1),
            sc.Handle("x11_window", "display-B", 2),
            sc.Handle("capsule", "capsule-C", 1),
            sc.Handle("godot_window", "godot-D", 2),
        )
        seed = 37
        events = tuple(
            {"handle": handle, "scene": "shared-scene", "epoch": 0, "frame_seq": frame,
             "tp": 1 + i * 7 + frame, "fp": i + 1, "fn": frame + 1,
             "tn": 10 + i, "terminal": "OK", "expected_terminal": "OK"}
            for frame in range(2) for i, handle in enumerate(handles)
        )
        scenario = sc.Scenario("p1-c-score", handles, events, {"default_terminal": "OK"})
        mixed = sc.run_trial(scenario, seed=seed, clock=sc.FakeClock(), adapters={})
        independent = {}
        for handle in handles:
            solo_scenario = sc.Scenario(
                "p1-c-score", (handle,),
                tuple(e for e in events if e["handle"] == handle),
                {"default_terminal": "OK"},
            )
            solo = sc.run_trial(solo_scenario, seed=seed, clock=sc.FakeClock(), adapters={})
            self.assertEqual(len(solo.rows), 2)
            for row in solo.rows:
                independent[(*_handle_key(row.handle), row.epoch, row.frame_seq)] = row

        joined = []
        for row in mixed.rows:
            solo_row = independent[(*_handle_key(row.handle), row.epoch, row.frame_seq)]
            self.assertEqual(row.tp, solo_row.tp)
            self.assertEqual(row.fp, solo_row.fp)
            self.assertEqual(row.fn, solo_row.fn)
            self.assertEqual(row.tn, solo_row.tn)
            self.assertEqual(row.ops, solo_row.ops)
            joined.append(replace(row, ops={**row.ops, "isolated_ops": dict(solo_row.ops)}))
        report = sc.seal_trial(replace(mixed, rows=tuple(joined)))
        score = sc.validate_scorecard(sc.score_windows(report, allowed_handles=handles))

        self.assertEqual(len(score.windows), 8)
        self.assertTrue(score.passed, score.violations)
        self.assertEqual(score.violations, ())
        self.assertEqual(set(score.metrics), set(sc.MANDATORY_METRIC_KEYS))
        self.assertEqual([score.pooled[k] for k in ("tp", "fp", "fn", "tn")],
                         [sum(1 + i * 7 + frame for frame in range(2) for i in range(4)),
                          sum(i + 1 for frame in range(2) for i in range(4)),
                          sum(frame + 1 for frame in range(2) for i in range(4)),
                          sum(10 + i for frame in range(2) for i in range(4))])
        tp, fp, fn, tn = (score.pooled[k] for k in ("tp", "fp", "fn", "tn"))
        self.assertEqual((tp, fp, fn, tn), (96, 20, 12, 92))
        pc = score.metrics["pooled_confusion"]
        self.assertEqual((pc["numerator"], pc["denominator"]), (96, 108))
        for name, num, den in (("precision", 96, 116), ("recall", 96, 108),
                               ("f1", 192, 224), ("accuracy", 188, 220)):
            with self.subTest(ratio=name):
                record = pc[name]
                self.assertEqual((record["numerator"], record["denominator"]), (num, den))
                self.assertEqual(record["fraction"], f"{num}/{den}")
                self.assertEqual(record["permille"], num * 1000 // den)

        expectations = {
            "cross_window_bleed": (0, 8), "safety_violations": (0, 8),
            "frame_exactness": (8, 8), "frame_drops": (0, 8),
            "operation_totals": (8, 8), "eligibility_stalls": (0, 8),
            "starvation": (0, 8), "terminal_completeness": (8, 8),
            "pooled_confusion": (96, 108),
        }
        for name, pair in expectations.items():
            with self.subTest(metric=name):
                m = score.metrics[name]
                self.assertEqual((m["numerator"], m["denominator"]), pair)
                self.assertTrue(m["defined"])
                self.assertTrue(m["passed"])
        self.assertEqual(score.pooled["measured_host_speed"], None)
        self.assertEqual(score.pooled["absent_gates"]["measured_host_speed"], sc.AUSENTE)

        self.assertEqual(report.rows[0].seal, _oracle_seal(report))
        self.assertEqual(len(report.rows[0].seal), 64)
        ordered = sorted(report.rows, key=lambda r: (*_handle_key(r.handle), r.scene, r.epoch, r.frame_seq))
        independent_bytes = _compact({
            "schema": "vqa.multiwin_trial_report/v1",
            "rows": [_oracle_row(row) for row in ordered],
            "outcomes": _oracle_clean(report.outcomes), "faults": [],
        })
        actual = sc.canonical_bytes(report)
        self.assertEqual(actual, independent_bytes)
        self.assertEqual(hashlib.sha256(actual).hexdigest(),
                         hashlib.sha256(independent_bytes).hexdigest())
        self.assertEqual(sc.canonical_bytes(replace(report, rows=tuple(reversed(report.rows)))), actual)
        self.assertEqual(score.pooled["seal"], _oracle_seal(report))

    def test_02_frames_have_real_owned_digest(self):
        handle = sc.Handle("xvfb_screen", "test-screen", 1)
        mutable = bytearray(range(12))
        actual = hashlib.sha256(bytes(mutable)).hexdigest()
        frame = sc.FrameRef(handle, 0, 0, 2, 2, mutable, actual)
        mutable[:] = bytes([0] * 12)
        self.assertEqual(frame.digest, hashlib.sha256(frame.rgb.tobytes()).hexdigest())
        self.assertEqual(frame.rgb.shape, (2, 2, 3))
        self.assertFalse(frame.rgb.flags.writeable)
        valid = sc.WindowFrames(handle, 0, (frame,), (0,))
        self.assertEqual(valid.frames[0].digest, actual)
        for bad_digest in ("fake", actual.upper(), "0" * 64):
            with self.subTest(digest=bad_digest[:8]):
                _assert_code(
                    self, "E_FRAME_SEQUENCE",
                    lambda: sc.FrameRef(handle, 0, 1, 2, 2, bytes(range(12)), bad_digest),
                )
        other = sc.FrameRef(handle, 0, 1, 2, 2, bytes(range(12)),
                            hashlib.sha256(bytes(range(12))).hexdigest())
        _assert_code(self, "E_FRAME_SEQUENCE",
                     lambda: sc.WindowFrames(handle, 0, (other, frame), (1, 0)))

    def test_03_content_seals_are_not_string_equality(self):
        a = sc.Handle("cdp_tab", "A", 1)
        b = sc.Handle("cdp_tab", "B", 1)
        report = _report(_row(a, tp=3), _row(b, tp=9))
        self.assertTrue(sc.score_windows(report).passed)
        self.assertEqual(report.rows[0].seal, report.rows[1].seal)
        self.assertEqual(_oracle_seal(report), report.rows[0].seal)
        _assert_code(self, "E_SEAL_MISMATCH",
                     lambda: sc.score_windows(_replace_row(report, 0, tp=99)))
        fake = replace(report, rows=tuple(replace(r, seal="a" * 64) for r in report.rows))
        _assert_code(self, "E_SEAL_MISMATCH", lambda: sc.score_windows(fake))
        swapped = replace(report, rows=(
            replace(report.rows[0], handle=b), replace(report.rows[1], handle=a)))
        _assert_code(self, "E_SEAL_MISMATCH",
                     lambda: sc.score_windows(swapped, allowed_handles=(a, b)))

    def test_04_mixed_incarnations_and_foreign_registration_refuse(self):
        a = sc.Handle("cdp_tab", "ABA", 1)
        later = sc.Handle("cdp_tab", "ABA", 2)
        mixed = _report(_row(a, frame_seq=0), _row(later, frame_seq=1))
        _assert_code(self, "E_WINDOW_IDENTITY", lambda: sc.score_windows(mixed))
        report = _report(_row(a))
        _assert_code(self, "E_WINDOW_IDENTITY",
                     lambda: sc.score_windows(report, allowed_handles=(later,)))
        event = {"handle": later, "frame_seq": 0}
        scenario = sc.Scenario("foreign", (a,), (event,), {})
        _assert_code(self, "E_WINDOW_IDENTITY",
                     lambda: sc.run_trial(scenario, seed=1, clock=sc.FakeClock(), adapters={}))

    def test_05_frame_sequence_and_geometry_epoch(self):
        h = sc.Handle("cdp_tab", "A", 1)
        report = _report(_row(h, frame_seq=1), _row(h, frame_seq=0))
        _assert_code(self, "E_FRAME_SEQUENCE", lambda: sc.score_windows(report))
        scenario = sc.Scenario("resize", (h,), ({"handle": h, "resize": True},), {})
        _assert_code(self, "E_FRAME_SEQUENCE",
                     lambda: sc.run_trial(scenario, seed=1, clock=sc.FakeClock(), adapters={}))
        explicit = sc.Scenario(
            "resize", (h,),
            ({"handle": h, "epoch": 0, "frame_seq": 0},
             {"handle": h, "resize": True, "epoch": 1, "frame_seq": 0}),
            {},
        )
        result = sc.run_trial(explicit, seed=1, clock=sc.FakeClock(), adapters={})
        self.assertEqual([(r.epoch, r.frame_seq) for r in result.rows], [(0, 0), (1, 0)])
        _assert_code(self, "E_FRAME_SEQUENCE",
                     lambda: sc.run_trial(sc.Scenario(
                         "bad", (h,), ({"handle": h, "frame_seq": 1},
                                      {"handle": h, "frame_seq": 1}), {}),
                         seed=1, clock=sc.FakeClock(), adapters={}))

    def test_06_missing_isolated_ops_refuses_before_acceptance(self):
        h = sc.Handle("x11_window", "A", 1)
        missing = _report(_row(h, ops={"decode": 1}))
        published = []
        def evaluate_and_publish():
            score = sc.validate_scorecard(sc.score_windows(missing))
            if score.passed:
                published.append("accepted")
        _assert_code(self, "E_ISOLATED_OPS", evaluate_and_publish)
        self.assertEqual(published, [])
        _assert_code(self, "E_ISOLATED_OPS",
                     lambda: _report(_row(h, ops={"decode": 1, "isolated_ops": []})))

    def test_07_independent_ops_mismatch_fails_metric(self):
        h = sc.Handle("cdp_tab", "A", 1)
        report = _report(_row(h, ops={"decode": 4, "fl": 1,
                                       "isolated_ops": {"decode": 3, "fl": 1}}))
        score = sc.score_windows(report)
        self.assertFalse(score.passed)
        self.assertFalse(score.metrics["operation_totals"]["passed"])
        self.assertEqual(score.pooled["ops"], {"decode": 4, "fl": 1})
        self.assertEqual(score.pooled["isolated_ops"], {"decode": 3, "fl": 1})
        self.assertIn("operation_totals_mismatch", score.violations)

    def test_08_pooled_ratios_cannot_be_averaged(self):
        h1 = sc.Handle("cdp_tab", "small", 1)
        h2 = sc.Handle("cdp_tab", "large", 1)
        report = _report(_row(h1, tp=1, fp=1, fn=1),
                         _row(h2, tp=9, fp=0, fn=1))
        score = sc.validate_scorecard(sc.score_windows(report))
        self.assertEqual((score.metrics["pooled_confusion"]["precision"]["numerator"],
                          score.metrics["pooled_confusion"]["precision"]["denominator"]),
                         (10, 11))
        averaged = (Fraction(1, 2) + Fraction(9, 9)) / 2
        altered_metrics = copy.deepcopy(score.metrics)
        altered_metrics["pooled_confusion"]["precision"].update(
            numerator=averaged.numerator, denominator=averaged.denominator,
            fraction=f"{averaged.numerator}/{averaged.denominator}",
            permille=averaged.numerator * 1000 // averaged.denominator,
        )
        _assert_code(self, "E_MUTANT_AVERAGE_RATIO",
                     lambda: sc.validate_scorecard(replace(score, metrics=altered_metrics)))

    def test_09_absent_denominators_are_null_not_zero(self):
        h = sc.Handle("capsule", "empty", 1)
        r = _row(h, tp=0, fp=0, fn=0, tn=0, supported=False,
                 dropped=True, exact=None)
        score = sc.validate_scorecard(sc.score_windows(_report(r)))
        self.assertFalse(score.passed)
        for name in ("pooled_confusion", "frame_exactness", "frame_drops"):
            with self.subTest(metric=name):
                metric = score.metrics[name]
                self.assertIsNone(metric["denominator"])
                self.assertIsNone(metric["fraction"])
                self.assertEqual(metric["status"], sc.AUSENTE)
                self.assertFalse(metric["passed"])
        self.assertIsNone(score.metrics["pooled_confusion"]["precision"]["denominator"])
        bad = copy.deepcopy(score.metrics)
        bad["pooled_confusion"]["precision"]["denominator"] = 0
        _assert_code(self, "E_MUTANT_NULL_ZERO",
                     lambda: sc.validate_scorecard(replace(score, metrics=bad)))
        bad2 = copy.deepcopy(score.metrics)
        bad2["frame_exactness"]["denominator"] = 0
        _assert_code(self, "E_MUTANT_NULL_ZERO",
                     lambda: sc.validate_scorecard(replace(score, metrics=bad2)))

    def test_10_mandatory_metric_negative_controls(self):
        h = sc.Handle("cdp_tab", "gate", 1)
        original = _report(_row(h))
        mutations = (
            ("cross_window_bleed", {"ops": {**original.rows[0].ops, "cross_window_bleed": 1}}),
            ("safety_violations", {"ops": {**original.rows[0].ops, "safety_violations": 1}}),
            ("frame_exactness", {"exact": False}),
            ("frame_drops", {"dropped": True}),
            ("operation_totals", {"ops": {"decode": 2, "fl": 3,
                                           "isolated_ops": {"decode": 1, "fl": 3}}}),
            ("eligibility_stalls", {"ops": {**original.rows[0].ops, "eligibility_stalls": 1}}),
            ("starvation", {"ops": {**original.rows[0].ops, "wait_dispatches": 100}}),
            ("terminal_completeness", {"terminal": "CRASH"}),
            ("pooled_confusion", {"ops": {**original.rows[0].ops, "silent_misses": 1}}),
        )
        for key, changes in mutations:
            with self.subTest(metric=key):
                changed = _replace_row(original, 0, reseal=True, **changes)
                score = sc.score_windows(changed)
                self.assertFalse(score.passed)
                self.assertFalse(score.metrics[key]["passed"])

    def test_11_canonical_numeric_admission(self):
        h = sc.Handle("cdp_tab", "x", 1)
        _assert_code(self, "E_SCORECARD_VALIDATION",
                     lambda: _row(h, tp=True))
        _assert_code(self, "E_SCORECARD_VALIDATION",
                     lambda: _row(h, fn=1.0))
        for number in (0.0, float("nan"), np.float64(1.0)):
            with self.subTest(float=number):
                _assert_code(self, "E_CANONICAL_FLOAT",
                             lambda: sc.canonical_bytes({"count": number}))
        _assert_code(self, "E_CANONICAL_TYPE", lambda: sc.canonical_bytes({12: "bad-key"}))
        _assert_code(self, "E_CANONICAL_TYPE", lambda: sc.canonical_bytes({"x": object()}))
        _assert_code(self, "E_CANONICAL_TYPE", lambda: sc.canonical_bytes({"x": {1, 2}}))
        _assert_code(self, "E_OPS_TYPE",
                     lambda: _report(_row(h, ops={"decode": True,
                                                  "isolated_ops": {"decode": 1}})))
        _assert_code(self, "E_OPS_TYPE",
                     lambda: _report(_row(h, ops={"decode": 1.0})))

    def test_12_diagnostic_wall_time_is_not_identity(self):
        h = sc.Handle("godot_window", "stable", 1)
        base = _report(_row(h))
        row = replace(base.rows[0], ops={**base.rows[0].ops,
                                       "wall_ms": 1.25, "host_ms": 99.5})
        after = replace(base, rows=(row,), outcomes={**base.outcomes, "wall_ns": 101})
        self.assertEqual(sc.canonical_bytes(base), sc.canonical_bytes(after))
        self.assertEqual(_oracle_seal(base), _oracle_seal(after))
        self.assertTrue(sc.validate_scorecard(sc.score_windows(after)).passed)

    def test_13_validate_rejects_changed_manifest(self):
        h = sc.Handle("cdp_tab", "A", 1)
        score = sc.score_windows(_report(_row(h)))
        polluted = dict(score.pooled)
        polluted["seal_manifest"] = {**polluted["seal_manifest"],
                                     "registered_handles": [["cdp_tab", "other", 1]]}
        _assert_code(self, "E_SEAL_MISMATCH",
                     lambda: sc.validate_scorecard(replace(score, pooled=polluted)))
        mutated = dict(score.pooled)
        mutated["tp"] = 999
        _assert_code(self, "E_MUTANT_POOL",
                     lambda: sc.validate_scorecard(replace(score, pooled=mutated)))

    def test_14_repeatable_concurrent_solo_replay(self):
        h = sc.Handle("xvfb_screen", "parallel", 1)
        scenario = sc.Scenario("parallel-trial", (h,), ({"handle": h},), {})
        barrier = threading.Barrier(3)
        outputs = [None] * 3
        errors = [None] * 3
        def worker(index):
            try:
                barrier.wait(timeout=3)
                r = sc.run_trial(scenario, seed=43, clock=sc.FakeClock(), adapters={})
                outputs[index] = sc.canonical_bytes(r)
            except BaseException as exc:
                errors[index] = exc
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads), "joined threads required")
        for error in errors:
            if error is not None:
                raise error
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])


    def test_15_forged_scorecard_acceptance_and_metrics_refuse(self):
        h = sc.Handle("godot_window", "validation", 1)
        mismatch = _report(_row(h, ops={"decode": 4, "isolated_ops": {"decode": 3}}))
        rejected = sc.score_windows(mismatch)
        self.assertFalse(rejected.passed)
        _assert_code(self, "E_MUTANT_GATE_PASS",
                     lambda: sc.validate_scorecard(replace(rejected, passed=True)))
        metrics = copy.deepcopy(rejected.metrics)
        metrics["operation_totals"]["passed"] = True
        _assert_code(self, "E_MUTANT_METRIC",
                     lambda: sc.validate_scorecard(replace(
                         rejected, metrics=metrics, passed=True, violations=())))
        valid = sc.score_windows(_report(_row(h)))
        pooled = copy.deepcopy(valid.pooled)
        pooled["frames_supported"] = 100
        _assert_code(self, "E_MUTANT_POOL",
                     lambda: sc.validate_scorecard(replace(valid, pooled=pooled)))

    def test_16_runtime_numeric_refusals_and_direct_row_seal(self):
        h = sc.Handle("x11_window", "geometry", 1)
        float_cursor = sc.Scenario("cursor", (h,), ({"handle": h, "dx": 1.0},), {})
        _assert_code(self, "E_FRAME_SEQUENCE",
                     lambda: sc.run_trial(float_cursor, seed=1, clock=sc.FakeClock(), adapters={}))
        raw = _row(h)
        sealed = sc.seal_trial(sc.TrialReport((raw,), {})).rows[0]
        scenario = sc.Scenario("direct", (h,), (sealed,), {})
        _assert_code(self, "E_SEAL_MISMATCH",
                     lambda: sc.run_trial(scenario, seed=1, clock=sc.FakeClock(), adapters={}))
        _assert_code(self, "E_SCORECARD_VALIDATION",
                     lambda: _report(_row(h, ops={"decode": 1, "silent_misses": True,
                                                  "isolated_ops": {"decode": 1}})))
        class DuckFrame:
            handle = h
            epoch = 0
            frame_seq = 0
            width = 2.0
            height = 2
            rgb = np.zeros((2, 2, 3), dtype=np.uint8)
        _assert_code(self, "E_FRAME_SEQUENCE",
                     lambda: sc.WindowFrames(h, 0, (DuckFrame(),), (0,)))


if __name__ == "__main__":
    unittest.main()
