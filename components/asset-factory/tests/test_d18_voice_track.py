"""Synthetic timing oracle for the ElevenLabs adapter and jaw fallback."""

from array import array
from copy import deepcopy
from fractions import Fraction
from importlib.resources import files
from io import BytesIO
import json
import random
import sys
import unittest
import wave

from asset_factory.core import ContractError
from tools.lipsync import VoiceError, build_track, validate_track
from tools.lipsync.witness import check_timing


RATE = 4800
COUNT = 4200
FPS = (24, 1)
QUIET = 40
DURATION = 275
SCRIPT = [
    (450, "M", "closed"),
    (1050, "a", "open"),
    (1650, "E", "wide"),
    (2250, "ó", "round"),
    (2850, "v", "bite"),
]


def timing_fixture(shift=0):
    samples = array("h", [0]) * COUNT
    rng = random.Random(81)
    for onset, _, shape in SCRIPT:
        for offset in range(DURATION):
            if shape == "bite":
                value = rng.choice((-1, 1)) * rng.randrange(2000, 13000)
            else:
                value = (12000, 8000, 0, -8000, -12000, -8000, 0, 8000)[offset % 8]
            samples[onset + shift + offset] = value
    if sys.byteorder != "little":
        samples.byteswap()
    dest = BytesIO()
    with wave.open(dest, "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return dest.getvalue()


def provider_fixture():
    return json.dumps({"alignment": {
        "characters": [char for _, char, _ in SCRIPT],
        "character_start_times_seconds": [onset / RATE for onset, _, _ in SCRIPT],
        "character_end_times_seconds": [(onset + DURATION) / RATE for onset, _, _ in SCRIPT],
    }}).encode()


def labels():
    starts = [onset for onset, _, _ in SCRIPT]
    return {
        "sample_rate": RATE,
        "sample_count": COUNT,
        "fps": list(FPS),
        "audio_onsets": starts,
        "mouth_onsets": [(onset, shape) for onset, _, shape in SCRIPT],
        "silences": list(zip([0] + [onset + DURATION for onset in starts], starts + [COUNT])),
    }


def build(audio=None, alignment=True, fps=FPS, max_frames=100):
    return build_track(
        timing_fixture() if audio is None else audio,
        provider_fixture() if alignment is True else alignment,
        fps=fps, silence_peak_pcm=QUIET, max_frames=max_frames,
    )


def check(track, audio=None, witness=None):
    return check_timing(track, timing_fixture() if audio is None else audio,
                        labels() if witness is None else witness,
                        onset_limit_frames=2, bridge_samples=8, silence_peak_pcm=QUIET)


class VoiceTrackTests(unittest.TestCase):
    def test_independently_labelled_alignment(self):
        track = build()
        result = check(track)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["fixture_verdict"], "PASS")
        self.assertEqual(result["production_verdict"], "CANNOT_SEE")
        self.assertEqual(result["criterion_basis"], "TARGET")
        self.assertEqual(result["audio_onset_error_frames"], [0, 1])
        self.assertEqual(result["mouth_onset_error_frames"], [1, 4])
        self.assertEqual(result["audio_mouth_onset_error_frames"], [1, 4])
        self.assertEqual(result["max_silence_movement_ppm"], 0)
        self.assertEqual(result["mouth_onsets_checked"], len(SCRIPT))
        from jsonschema import Draft202012Validator
        schema_file = files("tools.lipsync").joinpath(
            "../../schemas/af.viseme-track.v1.schema.json")
        schema = json.loads(schema_file.read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(track)
        self.assertFalse(validator.is_valid({**track, "invented": True}))

    def test_identical_length_shift_fails_even_with_audio_rebound(self):
        shifted = timing_fixture(shift=600)
        original = build()
        shifted_track = build(shifted)
        self.assertEqual(len(shifted), len(timing_fixture()))
        self.assertEqual(len(original["frames"]), len(shifted_track["frames"]))
        stale = check(original, shifted)
        self.assertEqual((stale["rc"], stale["reason_codes"]), (4, ["audio_identity_changed"]))
        result = check(shifted_track, shifted)
        self.assertEqual(result["rc"], 5, result)
        self.assertIn("audio_onset_out_of_bound", result["reason_codes"])
        self.assertEqual(result["audio_onset_error_frames"], [3, 1])
        print("VOICE_NEGATIVE", json.dumps(result, sort_keys=True), flush=True)

    def test_individually_in_bound_onsets_can_still_be_out_of_sync(self):
        audio = timing_fixture(shift=200)
        track = build(audio)
        frames = track["frames"]
        keys = [(row["mouth_ppm"], row["jaw_ppm"]) for row in frames[2:]]
        for row, (mouth, jaw) in zip(frames, keys):
            row["mouth_ppm"], row["jaw_ppm"] = mouth, jaw
        result = check(track, audio, {**labels(), "silences": [(3600, COUNT)]})
        self.assertEqual(result["audio_onset_error_frames"], [1, 1])
        self.assertEqual(result["mouth_onset_error_frames"], [5, 4])
        self.assertEqual(result["rc"], 5, result)
        self.assertEqual(result["reason_codes"], ["audio_mouth_onset_out_of_bound"])
        self.assertEqual(result["audio_mouth_onset_error_frames"], [9, 4])
        print("VOICE_NEGATIVE", json.dumps(result, sort_keys=True), flush=True)

    def test_held_open_jaw_through_silence_fails(self):
        track = build()
        track["frames"][0]["jaw_ppm"] = 600000
        result = check(track)
        self.assertEqual(result["rc"], 5, result)
        self.assertIn("mouth_moves_in_silence", result["reason_codes"])
        self.assertEqual(result["max_silence_movement_ppm"], 600000)
        print("VOICE_NEGATIVE", json.dumps(result, sort_keys=True), flush=True)

    def test_shifted_mouth_keys_and_lost_closure_fail(self):
        track = build()
        frames = track["frames"]
        keys = [(row["mouth_ppm"], row["jaw_ppm"]) for row in frames]
        for row, (mouth, jaw) in zip(frames, [keys[0]] * 3 + keys[:-3], strict=True):
            row["mouth_ppm"], row["jaw_ppm"] = mouth, jaw
        self.assertIn("mouth_onset_out_of_bound", check(track)["reason_codes"])
        track = build()
        for row in track["frames"]:
            if row["mouth_ppm"]["closed"]:
                row["mouth_ppm"]["closed"] = 0
                row["mouth_ppm"]["rest"] = 1000000
        self.assertIn("mouth_onset_coverage_changed", check(track)["reason_codes"])

    def test_fallback_is_jaw_only_and_silence_resets(self):
        track = build(alignment=None)
        result = check(track)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(track["source"], "audio_envelope")
        self.assertIsNone(track["alignment_sha256"])
        self.assertEqual(result["mouth_onsets_checked"], len(SCRIPT))
        self.assertTrue(any(row["jaw_ppm"] > 0 for row in track["frames"]))
        self.assertTrue(all(row["mouth_ppm"]["rest"] == 1000000 for row in track["frames"]))
        self.assertEqual(result["max_silence_movement_ppm"], 0)

    def test_rational_pts_and_unknown_timebase(self):
        for fps in (None, (0, 1), (24, 0), (48, 2), (24.0, 1), (True, 1)):
            with self.subTest(fps=fps), self.assertRaisesRegex(VoiceError, "unknown_timebase"):
                build(fps=fps)
        track = build(fps=(30000, 1001))
        row = track["frames"][7]
        self.assertEqual(Fraction(*row["pts"]), Fraction(7 * 1001, 30000))
        self.assertEqual(Fraction(*row["audio_sample"]), Fraction(*row["pts"]) * RATE)
        altered = deepcopy(track)
        altered["frames"][7]["audio_sample"] = [1, 1]
        with self.assertRaisesRegex(VoiceError, "frame_timebase_mismatch"):
            validate_track(altered)
        result = check(track, witness={**labels(), "sample_rate": 44100})
        self.assertEqual((result["rc"], result["reason_codes"]), (4, ["witness_timebase_changed"]))
        with self.assertRaisesRegex(VoiceError, "frame_budget_exceeded"):
            build(max_frames=20)

    def test_alignment_boundary_refusals(self):
        for raw in (
            b"{}", b'{"alignment":null}', b'{"alignment":{"characters":[]}}',
            b'{"alignment":{"characters":["a"],"character_start_times_seconds":[NaN],"character_end_times_seconds":[0.1]}}',
            b'{"alignment":{"characters":["a"],"character_start_times_seconds":[0],"character_end_times_seconds":[2]}}',
            b'{"alignment":{"characters":["a","i"],"character_start_times_seconds":[0,0.1],"character_end_times_seconds":[0.2,0.3]}}',
            b'{"alignment":{},"alignment":{}}',
            b'{"alignment":{"characters":["a"],"character_start_times_seconds":[false],"character_end_times_seconds":[0.1]}}',
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(VoiceError, "invalid_alignment"):
                build(alignment=raw)
        with self.assertRaisesRegex(VoiceError, "invalid_audio"):
            build(audio=timing_fixture()[:-8])
        result = check(build(), witness={**labels(), "silences": []})
        self.assertEqual((result["rc"], result["reason_codes"]), (3, ["witness_coverage_missing"]))

    def test_coarticulation_preserves_closure_and_rest(self):
        raw = b'{"characters":["a","e","m","o"],"character_start_times_seconds":[0.1,0.25,0.375,0.5],"character_end_times_seconds":[0.25,0.375,0.5,0.65]}'
        track = build(alignment=raw)
        at_boundary = track["frames"][6]["mouth_ppm"]
        self.assertEqual(at_boundary["open"], 500000)
        self.assertEqual(at_boundary["wide"], 500000)
        self.assertEqual(track["frames"][9]["mouth_ppm"]["closed"], 1000000)
        self.assertEqual(track["frames"][0]["mouth_ppm"]["rest"], 1000000)

    def test_policy_mismatch_refuses_and_unobserved_silence_stays_open(self):
        track = build()
        track["silence_peak_pcm"] += 1
        result = check(track)
        self.assertEqual((result["rc"], result["reason_codes"]), (4, ["silence_policy_changed"]))
        witness = labels()
        witness["silences"] = [(1, 2), *witness["silences"][1:]]
        result = check(build(), witness=witness)
        self.assertEqual((result["rc"], result["reason_codes"]), (3, ["silence_frame_coverage_missing"]))

    def test_bilabial_shape_cannot_drive_an_open_jaw(self):
        track = build()
        next(row for row in track["frames"] if row["mouth_ppm"]["closed"])["jaw_ppm"] = 1
        result = check(track)
        self.assertEqual((result["rc"], result["reason_codes"]), (5, ["incomplete_bilabial_closure"]))

    def test_structural_and_numeric_contract(self):
        for mutation in ("missing", "extra", "bool", "sum", "truncated"):
            with self.subTest(mutation=mutation):
                track = build()
                row = track["frames"][0]
                if mutation == "missing":
                    del row["mouth_ppm"]["rest"]
                if mutation == "extra":
                    row["mouth_ppm"]["unknown"] = 1
                if mutation == "bool":
                    row["jaw_ppm"] = False
                if mutation == "sum":
                    row["mouth_ppm"]["rest"] -= 1
                if mutation == "truncated":
                    track["frames"].pop()
                with self.assertRaises((VoiceError, ContractError)):
                    validate_track(track)
