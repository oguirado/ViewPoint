"""Synthetic CALIB-PREP witnesses and BASE admission controls."""
import copy
import io
import json
import struct
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from asset_factory import math_acceptance
from tools.calib.annotations import CLOSURE, ONSET, SILENCE
from tools.calib.ledger import ExposureLedger
from tools.calib.protocol import Attempt, CalibrationError, digest, encode, freeze
from tools.lipsync import ALIGNMENT, ENVELOPE, PPM, SHAPES, build_track


def fixture(source=ALIGNMENT, *, held_samples=None, held_id="trial-held", calibration_samples=None,
            calibration_tail_frames=0):
    def case(case_id, split, active_samples):
        # The sample annotations and the presentation controls are authored
        # separately from the generated viseme track.
        out = io.BytesIO()
        tail = calibration_tail_frames if split == "calibration" else 0
        samples = [0] * 3 + active_samples + [0] * (5 + tail)
        end_pts = [6, 5] if not tail else [len(samples), 10]
        with wave.open(out, "wb") as wav:
            wav.setparams((1, 2, 10, len(samples), "NONE", "not compressed"))
            wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        audio = out.getvalue()
        alignment = json.dumps({
            "fixture": case_id, "characters": ["a", "b"],
            "character_start_times_seconds": [0.3, 0.5], "character_end_times_seconds": [0.5, 0.7],
        }).encode() if source == ALIGNMENT else None
        track = build_track(audio, alignment, fps=[10, 1], silence_peak_pcm=0, max_frames=40)
        onsets = [[3, "open"], [5, "closed"]] if source == ALIGNMENT else [[3, "jaw"]]
        closures = [[5, 7]] if source == ALIGNMENT else []
        labels = {"annotator": "fixture-labeler", "audio_digest": digest(audio),
                  "onsets": onsets, "closures": closures, "silences": [[0, 2], [8, 12]]}
        # A nonuniform presentation schedule distinguishes track onset 3/10
        # from rendered onset 7/20. The render is a synthetic observation.
        pts = [[0, 1], [1, 10], [1, 5], [7, 20], [2, 5], [1, 2],
               [3, 5], [7, 10], [4, 5], [9, 10], [1, 1], [11, 10]]
        rendered = []
        for index, timestamp in enumerate(pts):
            speaking = 3 <= index < 7
            opening = 3 <= index < 5
            state = ("open" if opening else "closed" if speaking else "rest") if source == ALIGNMENT else "rest"
            rendered.append({"pts": timestamp, "mouth_ppm": {k: PPM if k == state else 0 for k in SHAPES},
                             "jaw_ppm": 400_000 if speaking and (opening or source == ENVELOPE) else 0})
        observations = {"observer": "fixture-observer", "audio_digest": digest(audio),
                        "track_digest": digest(encode(track)), "frames": rendered, "end_pts": end_pts}
        labels_bytes = encode(labels)
        spec = {
            "id": case_id, "split": split, "audio_digest": digest(audio),
            "alignment_digest": digest(alignment) if alignment else None,
            "annotation_digest": digest(labels_bytes), "annotator": "fixture-labeler",
            "render_observer": "fixture-observer", "sample_rate": 10, "sample_count": len(samples),
            "track_fps": [10, 1], "silence_peak_pcm": 0,
            "annotation_counts": {ONSET: len(onsets), CLOSURE: len(closures), SILENCE: 2},
            "presentation": {"pts": pts, "end_pts": end_pts},
        }
        return spec, Attempt(audio, encode(track), labels_bytes, encode(observations), alignment)

    first = case("trial-cal", "calibration", [200, -40, 100, 60] if calibration_samples is None else calibration_samples)
    second = case(held_id, "held-out", [-300, 200, 400, -80] if held_samples is None else held_samples)
    specs = [row for row, _ in (first, second)]
    cases = {row["id"]: attempt for row, attempt in (first, second)}
    lineage = []
    for row in specs:
        lineage.extend([
            {"digest": row["audio_digest"], "parents": [], "origin": row["id"]},
            {"digest": row["annotation_digest"], "parents": [row["audio_digest"]], "origin": None},
        ])
        if row["alignment_digest"]:
            lineage.append({"digest": row["alignment_digest"], "parents": [row["audio_digest"]], "origin": None})
    scope = {
        "gate_id": "calib-fixture-voice", "operation": "lipsync", "role": "synthetic-fixture",
        **{key: digest(key.encode()) for key in ("policy_digest", "domain_digest", "evaluator_digest",
                                                  "toolchain_digest")},
        "input_refs": [digest(b"profile input")], "reference_refs": [digest(b"profile reference")],
    }
    limits = {ONSET: 50_000_000, SILENCE: 0}
    if source == ALIGNMENT:
        limits[CLOSURE] = 0
    return {
        "schema": "af.calibration-protocol/v1", "material": "synthetic", "source": source,
        "decision_rule": "all_cases_pass_fixed_limits", "method_digest": digest(b"fixture method"),
        "scope": scope, "limits": limits, "lineage": lineage, "cases": specs,
    }, cases


class FixtureAnchor:
    """Separate approver-side head for a synthetic fixture."""

    def __init__(self, ledger_path):
        self.ledger_path = ledger_path
        self.path = ledger_path.with_suffix(".anchor")
        self.lock = Lock()

    def read(self, path):
        if path != self.ledger_path:
            raise OSError("fixture ledger identity changed")
        try:
            return self.path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None

    def advance(self, path, previous, head):
        with self.lock:
            if self.read(path) != previous:
                return False
            staged = self.path.with_suffix(".next")
            staged.write_text(head, encoding="ascii")
            staged.replace(self.path)
            return True


def audio_reader(cases):
    by_digest = {digest(attempt.audio): attempt.audio for attempt in cases.values()
                 if type(attempt.audio) is bytes}
    return by_digest.get


def freeze_fixture(raw, *, ledger, cases):
    return freeze(raw, ledger=ledger, read_audio=audio_reader(cases))


def run(protocol, cases, *, input_cases=None):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "exposure.jsonl"
        ledger = ExposureLedger.initialize(path, anchor=FixtureAnchor(path))
        result, _ = run_with_frozen(protocol, cases, ledger=ledger, input_cases=input_cases)
        return result


def run_with_frozen(protocol, cases, *, ledger, input_cases=None):
    raw = encode(protocol)
    ledger.approve(raw)
    frozen = freeze_fixture(raw, ledger=ledger, cases=cases if input_cases is None else input_cases)
    return frozen.evaluate(cases.get), frozen


def read_witness(result):
    return json.loads(result.witness)


def lineage_node(protocol, pin):
    return next(row for row in protocol["lineage"] if row["digest"] == pin)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory) / "exposure.jsonl"
        self.anchor = FixtureAnchor(path)
        self.ledger = ExposureLedger.initialize(path, anchor=self.anchor)

    def test_alignment_uses_rendered_rational_pts_and_admits_base(self):
        protocol, cases = fixture()
        result, frozen = run_with_frozen(protocol, cases, ledger=self.ledger)
        witness = read_witness(result)
        self.assertEqual(witness["rc"], 0, witness)
        self.assertEqual(witness["splits"]["held-out"], {"expected": 1, "passed": 1, "refused": 0})
        self.assertEqual(witness["attempts"][0]["metrics"][ONSET]["value"], [50_000_000, 1])
        self.assertEqual(witness["attempts"][1]["metrics"][CLOSURE]["checked"], 1)
        self.assertEqual(self.assess_base(protocol, result, frozen=frozen, approved=digest(result.witness))["rc"], 0)
        self.assertEqual(self.assess_base(protocol, result, frozen=frozen, approved=digest(b"unreviewed"))["rc"], 3)
        drift = copy.deepcopy(protocol)
        drift["scope"]["role"] = "character"
        self.assertEqual(self.assess_base(drift, result, frozen=frozen, approved=digest(result.witness),
                                          protocol_pin=digest(encode(protocol)))["rc"], 4)

    def test_envelope_keeps_jaw_onsets_and_silence_without_closure_claim(self):
        protocol, cases = fixture(ENVELOPE)
        result = run(protocol, cases)
        witness = read_witness(result)
        self.assertEqual(witness["rc"], 0, witness)
        self.assertNotIn(CLOSURE, witness["attempts"][0]["metrics"])
        self.assertEqual({json.loads(raw)["id"] for raw in result.qualifications}, {ONSET, SILENCE})

    def test_transitive_derived_duplicate_across_splits(self):
        protocol, cases = fixture()
        first, held = protocol["cases"]
        proxy = digest(b"re-encode")
        lineage = protocol["lineage"]
        lineage_node(protocol, held["audio_digest"]).update(origin=None, parents=[proxy])
        lineage.append({"digest": proxy, "origin": None, "parents": [first["audio_digest"]]})
        raw = encode(protocol)
        self.ledger.approve(raw)
        with self.assertRaisesRegex(CalibrationError, "cross_split_lineage"):
            freeze_fixture(raw, ledger=self.ledger, cases=cases)
        self.assertNotEqual(first["audio_digest"], held["audio_digest"])

    def test_shared_source_group_with_distinct_root_bytes_refused(self):
        protocol, cases = fixture()
        first, held = protocol["cases"]
        lineage_node(protocol, held["audio_digest"])["origin"] = lineage_node(protocol, first["audio_digest"])["origin"]
        raw = encode(protocol)
        self.ledger.approve(raw)
        with self.assertRaisesRegex(CalibrationError, "cross_split_lineage"):
            freeze_fixture(raw, ledger=self.ledger, cases=cases)

    def test_post_exposure_threshold_edit_and_replay_refused(self):
        protocol, cases = fixture()
        protocol["limits"][ONSET] -= 1
        raw = encode(protocol)
        self.ledger.approve(raw)
        frozen = freeze_fixture(raw, ledger=self.ledger, cases=cases)
        result = frozen.evaluate(cases.get)
        self.assertEqual(read_witness(result)["rc"], 5)
        protocol["limits"][ONSET] += 1
        edited = encode(protocol)
        self.ledger.approve(edited)
        with self.assertRaisesRegex(CalibrationError, "held_out_protocol_changed"):
            freeze_fixture(edited, ledger=ExposureLedger(self.ledger.path, anchor=self.anchor), cases=cases)
        with self.assertRaisesRegex(CalibrationError, "evaluation_already_started"):
            frozen.evaluate(cases.get)
        with self.assertRaisesRegex(CalibrationError, "evaluation_already_started"):
            freeze_fixture(raw, ledger=ExposureLedger(self.ledger.path, anchor=self.anchor), cases=cases).evaluate(cases.get)

    def test_missing_annotation_retained_with_null_error_and_blocks_base(self):
        protocol, cases = fixture()
        accepted, frozen = run_with_frozen(protocol, cases, ledger=self.ledger)
        cases["trial-held"] = replace(cases["trial-held"], annotations=None)
        result = run(protocol, cases)
        witness = read_witness(result)
        held = witness["attempts"][1]
        self.assertEqual((witness["rc"], held["rc"]), (3, 3))
        self.assertEqual(held["metrics"][ONSET], {"expected": 2, "checked": 0, "value": None})
        self.assertEqual(witness["splits"]["held-out"], {"expected": 1, "passed": 0, "refused": 1})
        self.assertEqual(result.qualifications, ())
        self.assertIsNone(result.read_checked("held-out-calibration", digest(result.witness),
                                                frozen_protocol=frozen, approved_protocol_digest=digest(encode(protocol)),
                                               approved_fixture_witness_digest=digest(result.witness)))
        verdict = self.assess_base(protocol, accepted, frozen=frozen, approved=digest(accepted.witness),
                                   reader_result=result)
        self.assertEqual(verdict["rc"], 3)

    def test_pinned_empty_annotation_cannot_bypass_expected_coverage(self):
        protocol, cases = fixture()
        spec = protocol["cases"][1]
        attempt = cases[spec["id"]]
        labels = json.loads(attempt.annotations)
        labels["onsets"] = []
        raw = encode(labels)
        lineage_node(protocol, spec["annotation_digest"])["digest"] = spec["annotation_digest"] = digest(raw)
        cases[spec["id"]] = replace(attempt, annotations=raw)
        witness = read_witness(run(protocol, cases))
        self.assertEqual(witness["attempts"][1]["reason_codes"], ["annotation_coverage_incomplete"])
        self.assertIsNone(witness["attempts"][1]["metrics"][ONSET]["value"])

    def test_render_identity_and_observed_motion_refuse(self):
        protocol, cases = fixture()
        held = cases["trial-held"]
        render = json.loads(held.render)
        render["frames"][3]["pts"] = [3, 10]
        cases["trial-held"] = replace(held, render=encode(render))
        self.assertEqual(read_witness(run(protocol, cases))["attempts"][1]["reason_codes"],
                         ["presentation_pts_changed"])
        render = json.loads(held.render)
        render["frames"][5]["jaw_ppm"] = 1
        render["frames"][-1]["jaw_ppm"] = 1
        cases["trial-held"] = replace(held, render=encode(render))
        witness = read_witness(run(protocol, cases))
        self.assertEqual(witness["rc"], 5)
        self.assertEqual(witness["attempts"][1]["reason_codes"], [CLOSURE, SILENCE])
        render = json.loads(held.render)
        render["frames"][7]["jaw_ppm"] = 1
        render["frames"][8]["pts"] = protocol["cases"][1]["presentation"]["pts"][8] = [17, 20]
        cases["trial-held"] = replace(held, render=encode(render))
        self.assertIn(SILENCE, read_witness(run(protocol, cases))["attempts"][1]["reason_codes"])

    def test_provider_refusals_stay_in_split_denominators(self):
        protocol, input_cases = fixture()
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(rc=rc):
                cases = {**input_cases, "trial-held": Attempt(source_rc=rc)}
                result = run(protocol, cases, input_cases=input_cases)
                witness = read_witness(result)
                self.assertEqual(witness["attempts"][1]["rc"], rc)
                self.assertEqual(witness["splits"]["held-out"]["refused"], 1)
                self.assertEqual(result.qualifications, ())

    def test_render_observer_independence_and_missing_frame(self):
        protocol, cases = fixture()
        held = cases["trial-held"]
        render = json.loads(held.render)
        render["observer"] = protocol["cases"][1]["annotator"]
        cases["trial-held"] = replace(held, render=encode(render))
        self.assertEqual(read_witness(run(protocol, cases))["attempts"][1]["reason_codes"],
                         ["render_identity_changed"])
        render = json.loads(held.render)
        render["frames"].pop()
        cases["trial-held"] = replace(held, render=encode(render))
        self.assertEqual(read_witness(run(protocol, cases))["attempts"][1]["rc"], 3)

    def test_provider_alignment_cannot_be_the_label_source_or_change_at_runtime(self):
        protocol, cases = fixture()
        held = protocol["cases"][1]
        attempt = cases[held["id"]]
        cases[held["id"]] = replace(attempt, alignment=cases["trial-cal"].alignment)
        witness = read_witness(run(protocol, cases))
        self.assertEqual(witness["attempts"][1]["reason_codes"], ["alignment_digest_changed"])
        lineage_node(protocol, held["annotation_digest"])["parents"].append(held["alignment_digest"])
        raw = encode(protocol)
        self.ledger.approve(raw)
        with self.assertRaisesRegex(CalibrationError, "annotation_uses_alignment"):
            freeze_fixture(raw, ledger=self.ledger, cases=cases)

    def test_freeze_precedes_ordered_one_shot_access_and_coverage_is_closed(self):
        protocol, cases = fixture()
        protocol["cases"].reverse()
        raw = encode(protocol)
        opened = []

        def read_attempt(case_id):
            opened.append(case_id)
            if case_id == "trial-cal":
                raise OSError("fixture I/O outage")
            return cases[case_id]

        self.ledger.approve(raw)
        frozen = freeze_fixture(raw, ledger=self.ledger, cases=cases)
        self.assertFalse(opened)
        witness = read_witness(frozen.evaluate(read_attempt))
        self.assertEqual(opened, ["trial-cal", "trial-held"])
        self.assertEqual(witness["splits"]["calibration"]["refused"], 1)
        self.assertEqual(witness["splits"]["held-out"]["passed"], 1)

    def test_subframe_closure_without_observed_pts_is_unavailable(self):
        protocol, cases = fixture()
        held = protocol["cases"][1]
        attempt = cases[held["id"]]
        # The presentation has no start PTS in the independently labelled
        # closure [5/10, 6/10), even though the held frame overlaps it.
        labels = json.loads(attempt.annotations)
        labels["closures"] = [[5, 6]]
        label_bytes = encode(labels)
        lineage_node(protocol, held["annotation_digest"])["digest"] = held["annotation_digest"] = digest(label_bytes)
        render = json.loads(attempt.render)
        render["frames"][5]["pts"] = held["presentation"]["pts"][5] = [49, 100]
        cases[held["id"]] = replace(attempt, annotations=label_bytes, render=encode(render))
        row = read_witness(run(protocol, cases))["attempts"][1]
        self.assertEqual(row["rc"], 3)
        self.assertEqual(row["metrics"][CLOSURE], {"expected": 1, "checked": 0, "value": None})

    def assess_base(self, protocol, result, *, frozen, approved, protocol_pin=None, reader_result=None):
        # Supply the existing BASE stage/profile/certificate interfaces
        # independently of the calibration reader.
        store = {}

        def put(role, obj):
            raw = encode(obj)
            pin = digest(raw)
            store[role, pin] = raw
            return pin

        scope = protocol["scope"]
        stage_scope = {k: v for k, v in scope.items() if k not in ("operation", "role")}
        bindings = {k: v for k, v in stage_scope.items() if k not in ("input_refs", "reference_refs")}
        control = put("negative-control", {**bindings, "expected_rc": 5, "observed_rc": 5,
                                          "witness_digest": digest(b"render motion control")})
        criteria = []
        for raw in result.qualifications:
            body = json.loads(raw)
            criteria.append({k: v for k, v in body.items() if k not in scope and k != "witness_digest"}
                            | {"target_reason": None, "proof_receipt_digest": None,
                               "calibration_receipt_digest": digest(raw)})
        profile = put("profile", {**scope, "schema": "af.acceptance-profile/v1",
                                  "negative_control_receipt_refs": [control], "criteria": criteria})
        subject_case = read_witness(result)["attempts"][1]
        subject = subject_case["artifact_digests"]["render"]
        observations = []
        for criterion in criteria:
            obs = {k: criterion[k] for k in ("id", "unit", "method_digest")}
            value = subject_case["metrics"][criterion["id"]]["value"]
            obs.update(lower=value, upper=value, confidence_ppm=None)
            obs["certificate_ref"] = put("certificate", {
                **obs, "subject_digest": subject, "profile_digest": profile,
                "gate_id": scope["gate_id"], "domain_digest": scope["domain_digest"],
                "witness_digest": digest(result.witness),
            })
            observations.append(obs)
        stage = put("stage", {**stage_scope, "schema": "af.math-evidence/v1",
                              "subject_digest": subject, "profile_digest": profile,
                              "source_rc": 0, "wall_ns": 1, "peak_bytes": 1,
                              "work_counts": {"cases": 1}, "observations": observations,
                              "negative_control_receipt_refs": [control]})

        def reader(role, pin):
            if role in ("calibration", "held-out-calibration"):
                return (reader_result or result).read_checked(
                    role, pin, frozen_protocol=frozen, approved_protocol_digest=protocol_pin or digest(encode(protocol)),
                    approved_fixture_witness_digest=approved,
                )
            return store.get((role, pin))

        return math_acceptance.evaluate_gate(profile_digest=profile, subject_digest=subject,
                                             receipt_digest=stage, read_checked=reader)
