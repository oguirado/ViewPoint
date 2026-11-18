from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import PropertyMock, patch


SURFACE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SURFACE.parent), str(SURFACE.parents[1] / "blender-visual-qa")]

from blender_visual_qa.drive import journal, png
import vqa3d_bridge as bridge


SCENE = "a" * 64
OTHER = "b" * 64
BLACK = bytes((0, 0, 0))
WHITE = bytes((255, 255, 255))
RED = bytes((255, 0, 0))
BLUE = bytes((0, 0, 255))
# Reply pins before the R-SCH result extensions.
LEGACY_RESULT_PINS = {
    "vqa3d.score": "b932b895551c665d62dfa346e82df747efe6a4e70581759931384d0929b80b6a",
    "vqa3d.gate": "fc8756795078c1a25a39bafb09cbedb838d45d5ec5e204bcf63143d3d5052fee",
}


class CaptureFixture:
    def __init__(self, root, frame_pixels, still_pixels=()):
        self.root = root
        root.mkdir()
        frame_rows = []
        for index, pixels in enumerate(frame_pixels):
            data = self.write_png(f"render/frame_{index:06d}.png", pixels)
            frame_rows.append({
                "render_index": index, "seed": index,
                "sha256": journal.sha(pixels), "file_sha256": journal.sha(data),
            })
        still_rows = []
        for index, pixels in enumerate(still_pixels, 1):
            self.write_png(f"render/still/still_{index:06d}.png", pixels)
            still_rows.append({"ri": index, "sha256": journal.sha(pixels)})
        self.receipt = {
            "schema": "bvqa.capture-receipt/v1", "blend_sha256": SCENE,
            "scratch_sha256_after": SCENE, "mutant_id": None, "fault_receipt_sha256": None,
            "journal_sha256": OTHER, "profile_id": "test", "profile_sha256": OTHER,
            "env_sha256": OTHER, "argv_mask": 0, "machine": "fixture", "rc": 0,
            "blender": {"version": "fixture", "build_hash": "fixture", "binary_sha256": OTHER},
            "backend_fp": OTHER[:16], "threads": 1, "frames": frame_rows, "still": still_rows,
            "frames_digest": journal.sha(journal.canonical([[row["render_index"], row["sha256"]] for row in frame_rows])),
            "warnings": [], "wall_ms": {"s1": 0, "s2": 0, "s3": 0, "s4": 0},
            "artifact_bytes": 0, "mp4_sha256": OTHER,
        }
        self.pin = self.write_receipt()

    def write_png(self, relative, pixels):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = png.encode(2, 2, pixels)
        path.write_bytes(data)
        return data

    def write_receipt(self, data=None):
        if data is None:
            data = journal.canonical(self.receipt)
        (self.root / "capture.receipt.json").write_bytes(data)
        return journal.sha(data)

    def evaluate(self, *, blend_sha256=SCENE, receipt_sha256=None):
        return bridge.evaluate_capture(
            self.root, blend_sha256=blend_sha256,
            receipt_sha256=self.pin if receipt_sha256 is None else receipt_sha256,
        )


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_scene_digest_score_and_predicates_are_repeatable(self):
        first = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        second = CaptureFixture(self.root / "second", [BLACK + WHITE + RED + BLUE])
        second.receipt["wall_ms"]["s4"] = 42
        second.pin = second.write_receipt()
        self.assertNotEqual(first.pin, second.pin)
        result = first.evaluate()
        self.assertEqual(result, second.evaluate())
        self.assertEqual(result, first.evaluate())
        self.assertEqual(result.score, 750_000)
        self.assertEqual(result.predicates, (bridge.RenderPredicate("render/frame_000000.png", True),))
        self.assertTrue(result.accepted)

    def _assert_adapter_exchange(self, op, result):
        examples = SURFACE.parent / "schemas" / "tests" / "examples"
        request = json.loads((examples / "vp.control.req_v1.valid.json").read_bytes())
        reply = json.loads((examples / "vp.control.reply_v1.valid.json").read_bytes())
        request.update(op="vp_factory_call", args={"op": op, "goal_id": "fixture", "inputs": []})
        reply.update(op=request["op"], request_id=request["request_id"], code="OK", result=result)
        bridge._validation.validate_adapter_exchange(request, reply)

    def test_capture_score_and_gate_validate_with_current_reply_pins(self):
        fixture = CaptureFixture(self.root / "mixed", [BLACK + WHITE + RED + BLUE], [BLACK * 4])
        quality = fixture.evaluate()
        score = quality.to_adapter_result("vqa3d.score")
        self.assertEqual(score, {
            "scene_digest": quality.scene_digest,
            "score": 0,
            "predicates": [
                {"render": "render/frame_000000.png", "frame_not_blank": True},
                {"render": "render/still/still_000001.png", "frame_not_blank": False},
            ],
        })
        gate = quality.to_adapter_result("vqa3d.gate")
        self.assertEqual(gate, dict(score, accepted=False))
        self._assert_adapter_exchange("vqa3d.score", score)
        self._assert_adapter_exchange("vqa3d.gate", gate)

        passing = CaptureFixture(self.root / "passing", [BLACK + WHITE + RED + BLUE])
        gate = passing.evaluate().to_adapter_result("vqa3d.gate")
        self.assertTrue(gate["accepted"])
        self._assert_adapter_exchange("vqa3d.gate", gate)

    def test_legacy_result_pins_refuse_serialization(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        quality = fixture.evaluate()
        policy = deepcopy(bridge._validation._adapter_rows())
        for op, legacy_pin in LEGACY_RESULT_PINS.items():
            policy["vp_factory_call"]["ops"][op]["reply_schema_sha256"] = legacy_pin
        with patch.object(bridge._validation, "_adapter_rows", return_value=policy):
            for op in LEGACY_RESULT_PINS:
                with self.subTest(op=op):
                    with self.assertRaisesRegex(journal.DriveError, "reply schema pin mismatch") as refusal:
                        quality.to_adapter_result(op)
                    self.assertEqual(refusal.exception.code, "E_SCHEMA")

    def test_contradictory_gate_result_refuses_serialization(self):
        fixture = CaptureFixture(self.root / "blank", [BLACK * 4])
        quality = fixture.evaluate()
        with patch.object(bridge.QualityResult, "accepted", new_callable=PropertyMock, return_value=True):
            with self.assertRaisesRegex(journal.DriveError, "Invalid VQA3D result") as refusal:
                quality.to_adapter_result("vqa3d.gate")
        self.assertEqual(refusal.exception.code, "E_SCHEMA")

    def test_mutant_bypassing_reply_pin_accepts_legacy_pin(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        quality = fixture.evaluate()
        op = "vqa3d.score"
        policy = deepcopy(bridge._validation._adapter_rows())
        row = policy["vp_factory_call"]["ops"][op]
        row["reply_schema_sha256"] = LEGACY_RESULT_PINS[op]
        with patch.object(bridge._validation, "_adapter_rows", return_value=policy):
            with self.assertRaisesRegex(journal.DriveError, "reply schema pin mismatch"):
                quality.to_adapter_result(op)
            with patch.object(bridge, "_pinned_reply_contract", return_value=row["reply_schema"]) as bypass:
                self._assert_adapter_exchange(op, quality.to_adapter_result(op))
            bypass.assert_called_once_with(op)

    def test_no_model_call(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        with patch.dict(sys.modules, {"blender_visual_qa.judge": None}), patch(
            "subprocess.Popen", side_effect=AssertionError("subprocess dispatch attempted"),
        ) as process:
            self.assertTrue(fixture.evaluate().accepted)
        process.assert_not_called()

    def test_missing_declared_render_is_rejected(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE, BLACK * 4])
        (fixture.root / "render/frame_000001.png").unlink()
        with self.assertRaisesRegex(journal.DriveError, "render evidence unavailable") as refusal:
            fixture.evaluate()
        self.assertEqual(refusal.exception.code, "E_IO")

    def test_empty_render_sequence_is_rejected(self):
        fixture = CaptureFixture(self.root / "first", [])
        with self.assertRaisesRegex(journal.DriveError, "empty frames"):
            fixture.evaluate()

    def test_nan_is_refused_even_with_matching_receipt_pin(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        fixture.receipt["frames"][0]["render_index"] = float("nan")
        pin = fixture.write_receipt(json.dumps(fixture.receipt).encode())
        with self.assertRaises(journal.DriveError) as refusal:
            fixture.evaluate(receipt_sha256=pin)
        self.assertEqual(refusal.exception.code, "E_FLOAT")

    def test_scene_pin_and_receipt_pin_are_checked_independently(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        with self.assertRaisesRegex(journal.DriveError, "Blender scene digest mismatch"):
            fixture.evaluate(blend_sha256=OTHER)
        with self.assertRaisesRegex(journal.DriveError, "capture receipt digest mismatch"):
            fixture.evaluate(receipt_sha256=OTHER)

    def test_substituted_render_is_refused(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE])
        fixture.write_png("render/frame_000000.png", BLACK * 4)
        with self.assertRaisesRegex(journal.DriveError, "render file digest mismatch"):
            fixture.evaluate()

    def assert_score_distinguishes_renders(self, better, worse):
        better_result = better.evaluate()
        worse_result = worse.evaluate()
        self.assertGreater(better_result.score, worse_result.score)
        self.assertEqual((better_result.score, worse_result.score), (750_000, 250_000))
        self.assertNotEqual(better_result.scene_digest, worse_result.scene_digest)
        self.assertTrue(better_result.accepted and worse_result.accepted)

    def test_constant_score_mutant_is_killed(self):
        better = CaptureFixture(self.root / "better", [BLACK + WHITE + RED + BLUE])
        worse = CaptureFixture(self.root / "worse", [BLACK * 3 + WHITE])
        self.assert_score_distinguishes_renders(better, worse)
        with patch.object(bridge, "_quality_score", return_value=750_000), self.assertRaises(AssertionError):
            self.assert_score_distinguishes_renders(better, worse)

    def assert_failed_still_vetoes_otherwise_passing_capture(self, fixture):
        result = fixture.evaluate()
        self.assertEqual([row.frame_not_blank for row in result.predicates], [True, False])
        self.assertFalse(result.accepted)

    def test_swallowed_predicate_failure_mutant_is_killed(self):
        fixture = CaptureFixture(self.root / "first", [BLACK + WHITE + RED + BLUE], [BLACK * 4])
        self.assert_failed_still_vetoes_otherwise_passing_capture(fixture)
        with patch.object(bridge.QualityResult, "accepted", new_callable=PropertyMock, return_value=True):
            with self.assertRaises(AssertionError):
                self.assert_failed_still_vetoes_otherwise_passing_capture(fixture)


if __name__ == "__main__":
    unittest.main()
