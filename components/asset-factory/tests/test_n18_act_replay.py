"""ACT controls on the retained, independently reviewed connected skin."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import json
from itertools import permutations
import os
from pathlib import Path
import struct
import time
import unittest

from asset_factory import core
from tools.motion import action, replay


class DenseReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        witness = os.environ.get("ACT_EVIDENCE_DIR")
        if not witness:
            raise unittest.SkipTest("set ACT_EVIDENCE_DIR to the retained ACT evidence")
        loc = Path(witness)
        cls.rig = (loc / "skin.glb").read_bytes()
        cls.anatomy = (loc / "anatomy.json").read_bytes()
        cls.skin_receipt = (loc / "skin-receipt.json").read_bytes()
        cls.plan = json.loads((loc / "rp1" / "registration.json").read_bytes())
        cls.pin = (loc / "rp1" / "registration.sha256").read_text().strip()
        cls.support = (loc / "rp1" / "support-plane.json").read_bytes()
        if core.sha256_json(cls.plan) != cls.pin:
            raise ValueError("ACT registration digest mismatch")
        cls.witness = json.loads((loc / "blender-witness.json").read_bytes())
        cls.data = (loc / "walk-gesture.glb").read_bytes()
        cls.schema = json.loads(Path("schemas/af.motion-receipt.v1.schema.json").read_bytes())
        deadline = time.monotonic_ns() + 5_000_000_000
        cls.skin = action.open_glb(cls.rig, deadline)
        cls.tracks = action.decode(cls.data, cls.skin, name=cls.plan["name"],
                                   duration=Fraction(2), deadline_ns=deadline)
        cls.nodes = {node["name"]: i for i, node in enumerate(cls.skin.doc["nodes"])}

    def evaluate(self, data, fps=24, *, plan=None, pin=None, support="fixture"):
        receipt, trace = replay.evaluate(
            data, self.rig, self.anatomy, self.skin_receipt,
            self.plan if plan is None else plan, self.pin if pin is None else pin,
            (fps, 1) if isinstance(fps, int) else fps,
            support_data=self.support if support == "fixture" else support,
        )
        core.validate(receipt, self.schema)
        self.assertEqual(receipt["production_verdict"], "CANNOT_SEE")
        self.assertEqual(receipt["calibration_state"], "TARGET_UNCALIBRATED")
        if trace:
            self.assertEqual(receipt["trace_sha256"], core.sha256_json(trace))
        return receipt, trace

    def test_dense_nominal_and_fps_parity(self):
        for fps in (24, 30):
            with self.subTest(fps=fps):
                receipt, trace = self.evaluate(self.data, fps)
                self.assertEqual(receipt["rc"], 0, receipt)
                self.assertEqual(receipt["coverage"]["measured_frames"], 2 * fps + 1)
                self.assertEqual([row["pts"] for row in trace["frames"]][0], [0, 1])
                self.assertEqual(trace["frames"][-1]["pts"], [2, 1])
                self.assertEqual(receipt["metrics"]["contact_mismatches"], 0)
                self.assertGreater(receipt["metrics"]["rotation_rate_upper_urad_s"], 0)
                self.assertGreater(receipt["metrics"]["joint_displacement_um"], 0)

    def test_ntsc_output_grid_and_rounded_terminal_keys(self):
        duration = Fraction(21, 10)
        plan = deepcopy(self.plan)
        plan["duration"] = [21, 10]
        for intervals in plan["stances"].values():
            intervals[-1][-1] = plan["duration"]
        tracks = {key: replace(track, times=track.times[:-1] + (duration,))
                  for key, track in self.tracks.items()}
        extended_clip = self.encode(tracks)
        extended_digest = replay.sha256(extended_clip)
        self.assertNotEqual(extended_digest, replay.sha256(self.data))
        deadline = time.monotonic_ns() + 5_000_000_000
        decoded = action.decode(extended_clip, self.skin, name=plan["name"],
                                duration=duration, deadline_ns=deadline)
        terminal_key = Fraction(struct.unpack("<f", struct.pack("<f", duration))[0])
        self.assertGreater(terminal_key, Fraction(*self.plan["duration"]))
        self.assertEqual({track.times[-1] for track in decoded.values()}, {terminal_key})
        for clip, registration in ((self.data, self.plan), (extended_clip, plan)):
            end = Fraction(*registration["duration"])
            for fps in ((30000, 1001), (24000, 1001)):
                with self.subTest(fps=fps, duration=end):
                    receipt, trace = self.evaluate(clip, fps, plan=registration,
                                                   pin=core.sha256_json(registration))
                    self.assertEqual(receipt["rc"], 0, receipt)
                    self.assertEqual(receipt["action_sha256"], replay.sha256(clip))
                    self.assertEqual(trace["action_sha256"], receipt["action_sha256"])
                    self.assertEqual(receipt["coverage"]["duration"], registration["duration"])
                    step = 1 / Fraction(*fps)
                    count = int(end / step) + 1
                    self.assertEqual(receipt["coverage"]["measured_frames"], count)
                    self.assertEqual([Fraction(*row["pts"]) for row in trace["frames"]],
                                     [i * step for i in range(count)])
                    self.assertLessEqual((count - 1) * step, end)
                    self.assertGreater(count * step, end)
        receipt, trace = self.evaluate(self.data, (30000, 1001), plan=plan,
                                       pin=core.sha256_json(plan))
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (5, ["channel_time_coverage"], None))
        key = (self.nodes["left_foot"], "translation")
        early = replace(tracks[key], times=tracks[key].times[:-1] + (duration - Fraction(1, 2**22),))
        receipt, _ = self.evaluate(self.encode(tracks | {key: early}), (30000, 1001),
                                   plan=plan, pin=core.sha256_json(plan))
        self.assertEqual(receipt["reason_codes"], ["channel_time_coverage"])

    def test_hover_inside_contact_band_still_fails_stance_gap(self):
        plan = self.plan | {"limits": self.plan["limits"] | {"contact_tolerance_um": 10_000}}
        for offset_um in (1_000, 1_001, 5_000, 9_000):
            with self.subTest(offset_um=offset_um):
                tracks = self.tracks.copy()
                for foot in replay.FEET:
                    key = (self.nodes[foot], "translation")
                    track = tracks[key]
                    tracks[key] = replace(track, rows=tuple((x, y + offset_um / 1_000_000, z)
                                                           for x, y, z in track.rows))
                receipt, _ = self.evaluate(self.encode(tracks), plan=plan, pin=core.sha256_json(plan))
                over_limit = offset_um > 1_000
                self.assertEqual(receipt["rc"], 5 if over_limit else 0, receipt)
                self.assertEqual(receipt["reason_codes"], ["stance_gap_upper_um"] if over_limit else [])
                self.assertEqual(receipt["metrics"]["contact_mismatches"], 0)
                self.assertEqual(receipt["metrics"]["stance_gap_upper_um"] > 1_000, over_limit)
                self.assertEqual(receipt["limits"]["contact_tolerance_um"], 10_000)
        loose = plan | {"limits": plan["limits"] | {"stance_gap_upper_um": 1_001}}
        receipt, _ = self.evaluate(self.data, plan=loose, pin=core.sha256_json(loose))
        self.assertEqual((receipt["rc"], receipt["reason_codes"]), (2, ["stance_gap_limit_invalid"]))

    def test_rest_support_is_independent_of_conforming_animation(self):
        root = next(name for name, parent in json.loads(self.anatomy)["joint_parents"].items()
                    if parent is None)
        for offset, metric in (
            (-5_000, "rest_stance_gap_upper_um"),
            (5_000, "rest_floor_clearance_lower_um"),
        ):
            with self.subTest(offset=offset):
                node = self.nodes[root]
                x, y, z = self.skin.doc["nodes"][node].get("translation", (0, 0, 0))
                position = (x, y + offset / 1_000_000, z)
                track = action.Track(node, "translation", (Fraction(0), Fraction(2)),
                                     (position, position))
                source, plan = self.plane(point_um=[0, offset, 0])
                receipt, trace = self.evaluate(self.encode(self.tracks | {(node, "translation"): track}),
                                               plan=plan, pin=core.sha256_json(plan), support=source)
                self.assertEqual((receipt["rc"], receipt["reason_codes"]), (5, [metric]))
                self.assertIsNotNone(trace)
                self.assertEqual(receipt["metrics"][metric], abs(offset) if offset < 0 else -offset)

    def test_tilted_support_and_sole_patch_validation(self):
        source, plan = self.plane(normal=[0, 10_000, 1])
        receipt, trace = self.evaluate(self.data, plan=plan, pin=core.sha256_json(plan), support=source)
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertLess(receipt["metrics"]["rest_floor_clearance_lower_um"], 0)
        self.assertGreater(receipt["metrics"]["rest_stance_gap_upper_um"], 0)
        self.assertEqual(trace["support_plane_sha256"], replay.sha256(source))
        plan["soles"] = deepcopy(plan["soles"])
        plan["soles"]["left_foot"][0][0][0] += 1
        receipt, _ = self.evaluate(self.data, plan=plan, pin=core.sha256_json(plan), support=source)
        self.assertEqual((receipt["rc"], receipt["reason_codes"]), (5, ["sole_vertex_unregistered"]))

    def test_all_limit_and_rest_reasons_are_metric_keys(self):
        source, plan = self.plane(normal=[0, 10_000, 1])
        expected = {
            "joint_displacement_um", "joint_rest_displacement_um", "joint_speed_um_s",
            "rotation_rate_upper_urad_s", "stance_slide_upper_um", "plant_speed_upper_um_s",
            "stance_gap_upper_um", "floor_clearance_lower_um",
            "rest_floor_clearance_lower_um", "rest_stance_gap_upper_um",
        }
        plan["limits"] = {
            name: (-1 if name == "floor_clearance_lower_um" else 1) if name in expected else limit
            for name, limit in plan["limits"].items()
        }
        receipt, trace = self.evaluate(self.mutant("floor-penetration"), plan=plan,
                                       pin=core.sha256_json(plan), support=source)
        self.assertEqual((receipt["rc"], receipt["verdict"]), (5, "FAIL"))
        self.assertIsNotNone(trace)
        limit_reasons = [code for code in receipt["reason_codes"] if code != "contact_schedule"]
        for code in limit_reasons:
            with self.subTest(code=code):
                self.assertIn(code, receipt["metrics"])
        self.assertCountEqual(limit_reasons, expected)

    def test_support_normal_must_point_up(self):
        for normal in ([0, -1, 0], [1, 0, 0], [1000, 1, 0], [0, 1, 1], [1000000, 1732050, 0]):
            with self.subTest(normal=normal):
                source, plan = self.plane(normal=normal)
                receipt, trace = self.evaluate(self.data, plan=plan, pin=core.sha256_json(plan),
                                               support=source)
                self.assertEqual((receipt["rc"], receipt["reason_codes"], trace is None),
                                 (2, ["support_normal_invalid"], True))

    def test_support_normal_tilt_bound_is_thirty_degrees(self):
        # 29.999997 deg passes the normal gate; 30.000012 deg (above) is refused.
        source, plan = self.plane(normal=[1000000, 1732051, 0])
        receipt, _ = self.evaluate(self.data, plan=plan, pin=core.sha256_json(plan), support=source)
        self.assertNotIn("support_normal_invalid", receipt["reason_codes"])

    def test_off_frame_key_changes_sampled_verdict(self):
        key = (self.nodes["face"], "translation")
        t = Fraction(65, 128)
        d = Fraction(1, 256)
        rest = tuple(self.skin.doc["nodes"][key[0]]["translation"])
        spike = (rest[0] + 0.5, *rest[1:])
        track = action.Track(key[0], key[1], (Fraction(0), t-d, t, t+d, Fraction(2)),
                             (rest, rest, spike, rest, rest))
        for fps in (24, 30):
            with self.subTest(fps=fps):
                receipt, trace = self.evaluate(self.encode(self.tracks | {key: track}), fps)
                self.assertEqual(receipt["rc"], 5)
                self.assertIn("joint_speed_um_s", receipt["reason_codes"])
                self.assertEqual(receipt["coverage"]["additional_key_samples"], len(trace["key_samples"]))
                self.assertIn([65, 128], [row["pts"] for row in trace["key_samples"]])
                self.assertNotIn([65, 128], [row["pts"] for row in trace["frames"]])

    def test_dense_export_agrees_with_blender_witness(self):
        for fps in (24, 30):
            with self.subTest(fps=fps):
                _, trace = self.evaluate(self.data, fps)
                authored = self.witness["frames"][str(fps)]
                self.assertEqual(len(trace["frames"]), len(authored))
                for decoded, witness in zip(trace["frames"], authored):
                    self.assertEqual(decoded["pts"], witness["pts"])
                    self.assertEqual(decoded["joints"].keys(), witness["joints"].keys())
                    for joint, position in decoded["joints"].items():
                        self.assertLessEqual(coordinate_error(position, witness["joints"][joint]), 100)
                    for foot, sole in decoded["soles"].items():
                        corresponding = witness["soles"][foot]
                        self.assertEqual(len(sole["points_um"]), len(corresponding))
                        error = min(max(coordinate_error(a, b) for a, b in zip(sole["points_um"], order))
                                    for order in permutations(corresponding))
                        self.assertLessEqual(error, 3)

    def test_sparse_key_cubic_spike(self):
        receipt, trace = self.evaluate(self.mutant("intermediate-spike"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("joint_speed_um_s", receipt["reason_codes"])
        self.assertEqual(receipt["coverage"]["measured_frames"], len(trace["frames"]))

    def test_stationary_joint_does_not_hide_sole_slide(self):
        receipt, trace = self.evaluate(self.mutant("stationary-marker-slide"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("stance_slide_upper_um", receipt["reason_codes"])
        self.assertIn("plant_speed_upper_um_s", receipt["reason_codes"])
        start, moved = trace["frames"][0], trace["frames"][6]
        self.assertEqual(start["joints"]["right_foot"], moved["joints"]["right_foot"])
        self.assertNotEqual(start["soles"]["right_foot"]["points_um"],
                            moved["soles"]["right_foot"]["points_um"])

    def test_actual_sole_penetration_refuses(self):
        receipt, _ = self.evaluate(self.mutant("floor-penetration"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("floor_clearance_lower_um", receipt["reason_codes"])
        self.assertLess(receipt["metrics"]["floor_clearance_lower_um"], -2_000)

    def test_shifted_contact_times(self):
        receipt, _ = self.evaluate(self.mutant("shifted-contacts"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("contact_schedule", receipt["reason_codes"])
        self.assertGreater(receipt["metrics"]["contact_mismatches"], 0)

    def test_speed_is_in_physical_time_at_24_and_30_fps(self):
        bad = self.mutant("speed")
        measured = []
        for fps in (24, 30):
            with self.subTest(fps=fps):
                receipt, _ = self.evaluate(bad, fps)
                self.assertEqual(receipt["rc"], 5)
                self.assertIn("joint_speed_um_s", receipt["reason_codes"])
                measured.append(receipt["metrics"]["joint_speed_um_s"])
        self.assertLessEqual(abs(measured[0] - measured[1]), 3)

    def test_angular_velocity_limit(self):
        receipt, _ = self.evaluate(self.mutant("angular-speed"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("rotation_rate_upper_urad_s", receipt["reason_codes"])

    def test_displacement_limit_independent_of_speed(self):
        receipt, _ = self.evaluate(self.mutant("constant-offset"))
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("joint_rest_displacement_um", receipt["reason_codes"])
        self.assertNotIn("joint_speed_um_s", receipt["reason_codes"])

    def test_registration_and_malformed_action_refuse(self):
        support, changed = self.plane(point_um=[0, 5_000, 0])
        receipt, trace = self.evaluate(self.data, plan=changed, support=support)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (4, ["registration_stale"], None))
        receipt, trace = self.evaluate(self.data, support=support)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (4, ["support_plane_stale"], None))
        receipt, trace = self.evaluate(self.data, support=None)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (3, ["support_plane_unavailable"], None))
        invalid, changed = self.plane(normal=[0, 0, 0])
        receipt, trace = self.evaluate(self.data, plan=changed, pin=core.sha256_json(changed),
                                       support=invalid)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (2, ["support_normal_invalid"], None))
        legacy = json.loads((Path(os.environ["ACT_EVIDENCE_DIR"]) / "registration.json").read_bytes())
        receipt, trace = self.evaluate(self.data, plan=legacy, pin=core.sha256_json(legacy))
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (3, ["support_plane_registration_required"], None))
        receipt, trace = self.evaluate(self.data[:-1])
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (2, ["glb_header"], None))

    def test_static_skin_drift_and_dropped_time_refuse(self):
        for kind, reason in (("static-skin-drift", "action_skin_mismatch"),
                             ("dropped-terminal-key", "channel_time_coverage"),
                             ("missing-gesture", "required_channel_missing")):
            with self.subTest(kind=kind):
                receipt, trace = self.evaluate(self.mutant(kind))
                self.assertEqual(receipt["reason_codes"], [reason])
                self.assertIsNone(trace)

    def test_receipt_cannot_claim_pass_without_complete_evidence(self):
        receipt, _ = self.evaluate(self.data)
        for changed in ({"trace_sha256": None}, {"support_plane_sha256": None}, {"status": "INCOMPLETE"},
                        {"production_verdict": "PASS"}, {"coverage": None}):
            with self.subTest(changed=changed):
                with self.assertRaises(core.ContractError):
                    core.validate(receipt | changed, self.schema)

    def test_unmeasured_contact_phases_and_expired_budget_refuse(self):
        receipt, trace = self.evaluate(self.data, fps=1)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (3, ["contact_phase_coverage_missing"], None))
        receipt, trace = replay.evaluate(self.data, self.rig, self.anatomy, self.skin_receipt,
                                         self.plan, self.pin, (24, 1), support_data=self.support,
                                         wall_budget_ns=1)
        core.validate(receipt, self.schema)
        self.assertEqual((receipt["rc"], receipt["reason_codes"], trace),
                         (6, ["deadline_exceeded"], None))

    def mutant(self, name):
        return (Path(os.environ["ACT_EVIDENCE_DIR"]) / (name + ".glb")).read_bytes()

    def encode(self, tracks):
        return action.encode(self.skin, self.plan["name"], tracks.values())

    def plane(self, **changes):
        source = core.canonical_json(json.loads(self.support) | changes).encode("utf-8")
        return source, self.plan | {"support_plane_sha256": replay.sha256(source)}


def coordinate_error(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


class TrackSamplerTests(unittest.TestCase):
    def test_binary32_duration_uses_half_the_directional_step(self):
        key = Fraction(2)
        self.assertTrue(action._covers_duration(key, key - Fraction(1, 2**24)))
        self.assertFalse(action._covers_duration(key, key - Fraction(1, 2**23)))
        self.assertTrue(action._covers_duration(key, key + Fraction(1, 2**23)))
        self.assertFalse(action._covers_duration(key, key + Fraction(3, 2**24)))

    def test_antipodal_quaternions_remain_stationary(self):
        q = (0.0, 0.0, 0.0, 1.0)
        track = action.Track(0, "rotation", (Fraction(0), Fraction(1)),
                             (q, tuple(-v for v in q)))
        self.assertEqual(track.sample(Fraction(1, 2)), q)
        self.assertEqual(action.relative_angle(q, track.sample(Fraction(1))), 0)

    def test_cubic_translation_between_unchanged_keys(self):
        zero = (0, 0, 0)
        track = action.Track(0, "translation", (Fraction(0), Fraction(2)),
                             (zero, zero, (12, 0, 0), (-12, 0, 0), zero, zero), "CUBICSPLINE")
        self.assertEqual(track.sample(Fraction(0)), zero)
        self.assertEqual(track.sample(Fraction(1)), (6, 0, 0))
        self.assertEqual(track.sample(Fraction(2)), zero)


if __name__ == "__main__":
    unittest.main()
