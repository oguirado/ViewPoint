"""Exercise the live decoder, BASE qualification and receipt publication."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from asset_factory import proc
import test_d18_input_contract as fixtures
from tools.input.evidence import (InputIssue, METRIC_RULES, bytes_digest, canonical_bytes,
                                  publish_input_receipt, qualify_input, verify_input_receipt)
from tools.input.frames import Frame, select_video


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.views = fixtures.Views()
        self.views.setUp()
        self.native = self.views.run_gate()
        self.assertEqual(self.native["source_rc"], 0, self.native)

    def base_pair(self, *, target=None, override_value=False):
        store, native = self.views.store, self.native
        bindings = {name: bytes_digest(name.encode()) for name in
                    ("policy_digest", "evaluator_digest", "toolchain_digest")}
        bindings["domain_digest"] = native["domain_digest"]
        gate_id, subject = native["gate_id"], native["subject_digest"]
        control = store.add("negative-control", {
            **bindings, "gate_id": gate_id, "expected_rc": 5, "observed_rc": 5,
            "witness_digest": bytes_digest(b"cropped-hand-control"),
        })
        profile = {"schema": "af.acceptance-profile/v1", **bindings, "gate_id": gate_id,
                   "operation": "input", "role": "synthetic-character",
                   "input_refs": native["input_refs"], "reference_refs": native["reference_refs"],
                   "negative_control_receipt_refs": [control], "criteria": []}
        limits = {"coverage": 1_000_000, "landmark_error": 100, "lighting_flatness": 50_000}
        observations = []
        for name, (unit, scale, operator, basis) in METRIC_RULES[gate_id].items():
            method = bytes_digest(name.encode())
            rule = {"id": name, "unit": unit, "scale": scale, "operator": operator,
                    "limit": limits[name], "method_digest": method, "uncertainty_kind": "exact",
                    "basis": "TARGET" if name == target else basis, "min_confidence_ppm": None}
            if name == target:
                qualification = {"target_reason": "fixture target", "proof_receipt_digest": None,
                                 "calibration_receipt_digest": None}
            else:
                role, witness_role = (("proof", "derivation") if basis == "DERIVED" else
                                      ("calibration", "held-out-calibration"))
                witness = store.add(witness_role, {"fixture": name})
                scope = {key: profile[key] for key in ("gate_id", "input_refs", "reference_refs", "operation", "role")}
                reviewed = store.add(role, {**bindings, **scope, **rule, "witness_digest": witness})
                qualification = {"target_reason": None, "proof_receipt_digest": None,
                                 "calibration_receipt_digest": None, role + "_receipt_digest": reviewed}
            profile["criteria"].append({**rule, **qualification})

            lower, upper = native["metrics"][name]
            if override_value and name == "landmark_error":
                lower = upper = [1, 1]
            observations.append({"id": name, "unit": unit, "method_digest": method,
                                 "lower": lower, "upper": upper, "confidence_ppm": None})

        profile_pin = store.add("profile", profile)
        for obs in observations:
            obs["certificate_ref"] = store.add("certificate", {
                **obs, "subject_digest": subject, "profile_digest": profile_pin, "gate_id": gate_id,
                "domain_digest": native["domain_digest"], "witness_digest": bytes_digest(b"fixture"),
            })
        stage = {"schema": "af.math-evidence/v1", **bindings, "gate_id": gate_id,
                 "input_refs": native["input_refs"], "reference_refs": native["reference_refs"],
                 "subject_digest": subject, "profile_digest": profile_pin,
                 "negative_control_receipt_refs": [control], "source_rc": 0,
                 "wall_ns": 100, "peak_bytes": 10, "work_counts": {"views": 6},
                 "observations": observations}
        return {"profile_digest": profile_pin, "receipt_digest": store.add("stage", stage),
                "read_checked": store.read}

    def test_base_fixture_reaches_pass(self):
        verdict = qualify_input(self.native, **self.base_pair())
        self.assertEqual(verdict["rc"], 0, verdict)
        self.assertEqual(verdict["gate"]["required_check_ids"],
                         ["coverage", "landmark_error", "lighting_flatness"])

    def test_target_and_missing_calibration_witness_remain_diagnostic(self):
        target = qualify_input(self.native, **self.base_pair(target="landmark_error"))
        self.assertEqual((target["rc"], target["disposition"]), (3, "DIAGNOSTIC"))
        pair = self.base_pair()
        del self.views.store["held-out-calibration", bytes_digest(canonical_bytes({"fixture": "landmark_error"}))]
        result = qualify_input(self.native, **pair)
        self.assertEqual((result["rc"], result["disposition"]), (3, "DIAGNOSTIC"), result)

    def test_foreign_subject_and_rebound_observation_are_stale(self):
        pair = self.base_pair()
        foreign = {**self.native, "subject_digest": bytes_digest(b"another selection")}
        self.assertEqual(qualify_input(foreign, **pair)["rc"], 4)
        altered = qualify_input(self.native, **self.base_pair(override_value=True))
        self.assertEqual(altered["reasons"], ["input_observation_drift"])

    def test_receipt_commit_is_exclusive_and_reverified(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            pin = publish_input_receipt(target, self.native)
            self.assertEqual(pin, verify_input_receipt(target.read_bytes(), self.native))
            changed = copy.deepcopy(self.native)
            changed["selection"]["raster"][0] += 1
            with self.assertRaises(FileExistsError):
                publish_input_receipt(target, changed)
            with self.assertRaises(InputIssue) as refused:
                verify_input_receipt(target.read_bytes(), changed)
            self.assertEqual(refused.exception.rc, 4)
            self.assertEqual(bytes_digest(target.read_bytes()), pin)

    def test_live_owned_decode_and_rational_pts(self):
        binary = Path("/usr/bin/ffmpeg")
        pixels = b"".join(frame.rgb for frame in self.views.video.frames)
        argv = [str(binary), "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "rawvideo",
                "-pixel_format", "rgb24", "-video_size", "8x8", "-framerate", "25", "-i", "pipe:0",
                "-threads", "1", "-c:v", "ffv1", "-pix_fmt", "bgr0", "-f", "matroska", "pipe:1"]
        job = proc.popen(argv, start_new_session=True, input=pixels, runtime_s=10)
        try:
            self.assertEqual(job.wait(10), 0, job.stderr_path.read_text())
            rgb_video = job.stdout_path.read_bytes()
        finally:
            self.assertEqual(proc.cleanup(job), [])
        plan, store = self.views.plan, self.views.store
        source = bytes_digest(rgb_video)
        plan["source_digest"] = source
        plan["rights_digest"] = store.add("input-rights", {
            "schema": "af.input-rights/v1", "source_digest": source, "allowed_uses": ["held_out"],
        })
        plan["split_digest"] = store.add("input-split", {
            "schema": "af.input-split/v1", "held_out": [source],
            "calibration": [bytes_digest(b"separate-capture")],
        })
        for row in plan["observations"]:
            row["pts"] = [row["index"], 25]
        plan_pin = store.add("input-plan", plan)
        config = {"schema": "af.input-decoder/v1", "ffmpeg_path": str(binary),
                  "ffmpeg_digest": bytes_digest(binary.read_bytes()), "format": "matroska",
                  "max_encoded_bytes": 10000, "max_decoded_bytes": 2000,
                  "max_frames": 6, "timeout_seconds": 10, "max_codec_pixels": 1024}
        cfg_pin = store.add("input-decoder", config)
        receipt, selected = select_video(rgb_video, plan_digest=plan_pin, decoder_digest=cfg_pin,
                                         read_checked=store.read)
        self.assertEqual(receipt["source_rc"], 0, receipt)
        self.assertEqual(selected["back"], Frame(3, (3, 25), self.views.frames[3].rgb))
        self.assertEqual(receipt["decoder_digest"], cfg_pin)
        config["max_frames"] = 5
        refusal, frames = select_video(rgb_video, plan_digest=plan_pin,
                                       decoder_digest=store.add("input-decoder", config), read_checked=store.read)
        self.assertEqual((refusal["source_rc"], frames), (6, {}), refusal)


if __name__ == "__main__":
    unittest.main()
