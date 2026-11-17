"""Regression controls reproduced from the independent VOICE review."""

import base64
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from jsonschema import Draft202012Validator

from test_d18_voice_track import COUNT, FPS, QUIET, RATE, build, provider_fixture, timing_fixture
from tools.lipsync import VoiceError, build_track, validate_track


def wav(samples, rate=RATE):
    result = BytesIO()
    with wave.open(result, "wb") as writer:
        writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return result.getvalue()


def alignment(characters, starts, ends):
    return {
        "characters": characters,
        "character_start_times_seconds": [sample / RATE for sample in starts],
        "character_end_times_seconds": [sample / RATE for sample in ends],
    }


def voiced_track(payload, audio=None):
    return build(audio=audio, alignment=json.dumps(payload).encode())


class VoiceRepairTests(unittest.TestCase):
    def test_aligned_motion_requires_audio_and_reports_unsupported_spans(self):
        track = build(audio=wav([0] * COUNT))
        self.assertTrue(all(row["mouth_ppm"]["rest"] == 1_000_000 and row["jaw_ppm"] == 0
                            for row in track["frames"]))
        self.assertIn({"start_sample": 1050, "end_sample": 1325, "shape": "open"},
                      track["inactive_aligned_spans"])
        self.assertEqual(build()["inactive_aligned_spans"], [])
        misaligned = voiced_track(alignment(["a"], [740], [800]))
        self.assertEqual(misaligned["inactive_aligned_spans"],
                         [{"start_sample": 740, "end_sample": 800, "shape": "open"}])
        self.assertTrue(all(row["jaw_ppm"] == 0 for row in misaligned["frames"]))

    def test_subframe_bilabial_wins_over_vowel_in_same_interval(self):
        audio = wav([0] * 450 + [8000] * 280 + [0] * (COUNT - 730))
        for closure in "mbp":
            with self.subTest(closure=closure):
                payload = alignment([closure, "a"], [450, 490], [490, 730])
                frames = voiced_track(payload, audio)["frames"]
                self.assertEqual(frames[2]["mouth_ppm"]["closed"], 1_000_000)
                self.assertEqual(frames[2]["jaw_ppm"], 0)
                self.assertEqual(frames[3]["mouth_ppm"]["open"], 1_000_000)
                self.assertEqual(frames[4]["mouth_ppm"]["rest"], 1_000_000)
        # Two short, disjoint spans may both overlap a frame without touching.
        disconnected = voiced_track(alignment(["m", "a"], [450, 510], [490, 730]), audio)
        self.assertEqual(disconnected["frames"][2]["mouth_ppm"]["closed"], 1_000_000)

    def test_normalized_spoken_text_and_null_raw_fallback(self):
        normalized = alignment(list("five"), [450, 505, 560, 620], [505, 560, 620, 725])
        raw = alignment(["5"], [450], [725])
        for top in ({"alignment": raw, "normalized_alignment": normalized},
                    {"alignment": None, "normalized_alignment": normalized}):
            self.assertTrue(any(row["mouth_ppm"]["bite"] for row in voiced_track(top)["frames"]))
        self.assertTrue(all(row["mouth_ppm"]["rest"] == 1_000_000 for row in
                            voiced_track({"alignment": raw, "normalized_alignment": None})["frames"]))
        for top in ({"alignment": None, "normalized_alignment": None},
                    {"alignment": raw, "normalized_alignment": {}}):
            with self.assertRaisesRegex(VoiceError, "invalid_alignment"):
                voiced_track(top)

    def test_nfd_combined_and_split_forms_preserve_the_timed_grapheme(self):
        expected = voiced_track(alignment(["á"], [450], [725]))["frames"]
        for payload in (alignment(["a\u0301"], [450], [725]),
                        alignment(["a", "\u0301"], [450, 550], [550, 725])):
            self.assertEqual(voiced_track(payload)["frames"], expected)
        for chars in (["mb"], ["\u0301"]):
            with self.assertRaisesRegex(VoiceError, "invalid_alignment"):
                voiced_track(alignment(chars, [450], [725]))

    def test_wav_budget_is_checked_without_decoding_frames(self):
        with patch.object(wave.Wave_read, "readframes", side_effect=AssertionError("decode")):
            with self.assertRaisesRegex(VoiceError, "frame_budget_exceeded") as refusal:
                build(max_frames=1)
        self.assertEqual(refusal.exception.rc, 6)

    def test_schema_rejects_rational_overflow_bad_bounds_and_bool(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas/af.viseme-track.v1.schema.json"
        validator = Draft202012Validator(json.loads(schema_path.read_bytes()))
        track = build()
        validator.validate(track)
        for path, value in (
            (("fps",), [24, 1, 7]), (("fps",), [0, 1]), (("fps",), [24, 0]),
            (("sample_rate",), 0), (("sample_count",), 0), (("silence_peak_pcm",), 32768),
            (("frames", 0, "pts"), [0, 1, 7]), (("frames", 0, "audio_sample"), [-1, 1]),
            (("frames", 0, "index"), -1), (("frames", 0, "jaw_ppm"), True),
            (("frames", 0, "jaw_ppm"), 1_000_001), (("frames", 0, "mouth_ppm", "rest"), -1),
        ):
            with self.subTest(path=path, value=value):
                mutant = deepcopy(track)
                item = mutant
                for key in path[:-1]:
                    item = item[key]
                item[path[-1]] = value
                self.assertFalse(validator.is_valid(mutant))
                with self.assertRaises((VoiceError, ValueError)):
                    validate_track(mutant)

    def test_empty_audio_has_its_own_refusal(self):
        for audio in (b"", wav([])):
            for timing in (None, provider_fixture()):
                with self.assertRaisesRegex(VoiceError, "empty_audio"):
                    build_track(audio, timing, fps=FPS, silence_peak_pcm=QUIET, max_frames=100)

    def test_full_elevenlabs_pcm_response_and_format_refusals(self):
        from tools.lipsync.media import build_elevenlabs_track

        source = timing_fixture()
        with wave.open(BytesIO(source)) as reader:
            pcm = reader.readframes(COUNT)
        response = json.dumps({
            "audio_base64": base64.b64encode(pcm).decode(),
            "alignment": None,
            "normalized_alignment": json.loads(provider_fixture())["alignment"],
        }).encode()
        track, decoded = build_elevenlabs_track(
            response, output_format=f"pcm_{RATE}", fps=FPS, silence_peak_pcm=QUIET, max_frames=100)
        self.assertEqual(decoded, source)
        self.assertEqual(track["audio_sha256"], sha256(source).hexdigest())
        self.assertEqual(track["alignment_sha256"], sha256(response).hexdigest())

        def from_response(data, fmt):
            return build_elevenlabs_track(
                data, output_format=fmt, fps=FPS, silence_peak_pcm=QUIET, max_frames=100)

        for fmt, code in (("pcm", "unknown_timebase"), ("mp3_44100_128", "mp3_timing_unproven")):
            with self.assertRaisesRegex(VoiceError, code):
                from_response(response, fmt)
        obj = json.loads(response)
        obj["audio_base64"] = "x"
        with self.assertRaisesRegex(VoiceError, "invalid_audio_base64"):
            from_response(json.dumps(obj).encode(), f"pcm_{RATE}")
        obj["audio_base64"] = base64.b64encode(pcm).decode()
        obj["normalized_alignment"] = None
        with self.assertRaisesRegex(VoiceError, "invalid_alignment"):
            from_response(json.dumps(obj).encode(), f"pcm_{RATE}")

    def test_rhubarb_cues_share_the_viseme_contract(self):
        from tools.lipsync.rhubarb import track_from_cues

        cues = json.dumps({"mouthCues": [
            {"start": 450 / RATE, "end": 490 / RATE, "value": "A"},
            {"start": 490 / RATE, "end": 725 / RATE, "value": "D"},
        ]}).encode()

        def from_cues(raw):
            return track_from_cues(
                timing_fixture(), raw, fps=FPS, silence_peak_pcm=QUIET, max_frames=100)

        track = from_cues(cues)
        validate_track(track)
        self.assertEqual(track["source"], "rhubarb")
        self.assertEqual(track["frames"][2]["mouth_ppm"]["closed"], 1_000_000)
        self.assertEqual(track["frames"][3]["mouth_ppm"]["open"], 1_000_000)
        self.assertEqual(track["frames"][0]["mouth_ppm"]["rest"], 1_000_000)
        for updates in ({"value": "Z"}, {"end": 0.11}, {"start": -1}):
            mutant = json.loads(cues)
            mutant["mouthCues"][0].update(updates)
            with self.assertRaisesRegex(VoiceError, "invalid_rhubarb_cues"):
                from_cues(json.dumps(mutant).encode())

        for cue, visual in zip("ABCDEFGHX", ("closed", "wide", "wide", "open", "round",
                                             "round", "bite", "wide", "rest"), strict=True):
            entry = {"start": 450 / RATE, "end": 725 / RATE, "value": cue}
            raw = {"mouthCues": [entry]}
            canonical = from_cues(json.dumps(raw).encode())
            self.assertEqual(canonical["frames"][2]["mouth_ppm"][visual], 1_000_000)
            raw["metadata"] = {"soundFile": "/tmp/another-file.wav"}
            self.assertEqual(from_cues(json.dumps(raw).encode()), canonical)

    def test_timed_mp3_requires_measured_gapless_trim(self):
        from tools.lipsync.media import _run_local_tool, build_elevenlabs_track, decode_mp3_timed

        # Encode a known PCM clock at a supported MP3 rate. Use the same
        # source for the green and the missing-Xing negative.
        rate = 48000
        source_count = 42000
        original = wav([0] * 4500 + [10000] * 2750 + [0] * (source_count - 7250), rate)
        options = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe",
                   "timeout": 20, "fps": FPS, "max_frames": 100}
        with TemporaryDirectory(prefix="voice-mp3-fixture-") as directory:
            root = Path(directory)
            (root / "source.wav").write_bytes(original)
            for name, xing in (("timed.mp3", "1"), ("untimed.mp3", "0")):
                _run_local_tool(
                    [options["ffmpeg"], "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
                     "-i", "source.wav", "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", xing, name],
                    cwd=root, timeout=options["timeout"], failure_code="fixture_mp3_encode_failed",
                )
            timed = (root / "timed.mp3").read_bytes()
            untimed = (root / "untimed.mp3").read_bytes()

        delivered, receipt = decode_mp3_timed(timed, **options)
        self.assertGreater(receipt["leading_skip_samples"], 0)
        self.assertGreater(receipt["trailing_discard_samples"], 0)
        self.assertEqual(receipt["untrimmed_samples"] - receipt["leading_skip_samples"]
                         - receipt["trailing_discard_samples"], source_count)
        self.assertEqual(receipt["decoded_audio_sha256"], sha256(delivered).hexdigest())
        with wave.open(BytesIO(delivered)) as reader:
            values = struct.unpack(f"<{source_count}h", reader.readframes(source_count))
        onset = next(index for index, sample in enumerate(values) if sample > 5000)
        self.assertLessEqual(abs(onset - 4500), 10)
        print("VOICE_MP3_TRIM", json.dumps({
            **receipt, "expected_onset_sample": 4500, "decoded_onset_sample": onset,
        }, sort_keys=True), flush=True)
        response = json.dumps({
            "audio_base64": base64.b64encode(timed).decode(),
            "alignment": alignment(["a"], [450], [725]), "normalized_alignment": None,
        }).encode()
        track, decoded = build_elevenlabs_track(
            response, output_format="mp3_48000_128", silence_peak_pcm=QUIET, **options)
        self.assertEqual(decoded, delivered)
        self.assertEqual(track["sample_count"], source_count)
        self.assertEqual(track["audio_sha256"], receipt["decoded_audio_sha256"])
        with self.assertRaisesRegex(VoiceError, "mp3_timing_unproven") as refusal:
            decode_mp3_timed(untimed, **options)
        self.assertEqual(refusal.exception.rc, 3)
        with self.assertRaisesRegex(VoiceError, "frame_budget_exceeded"):
            decode_mp3_timed(timed, **{**options, "max_frames": 1})

    def test_packet_clock_refuses_discontinuity_or_moved_trim(self):
        from tools.lipsync.media import _gapless_clock

        def trim(skip, discard):
            return {"side_data_type": "Skip Samples", "skip_samples": skip,
                    "discard_padding": discard, "skip_reason": 0, "discard_reason": 0}

        def packet(pts, side):
            return {"pts": pts, "duration": 1152, "stream_index": 0, "side_data_list": [side]}

        fixture = {
            "streams": [{"codec_name": "mp3", "index": 0, "sample_rate": "48000",
                         "time_base": "1/48000", "channels": 1}],
            "packets": [packet(0, trim(1105, 0)), packet(1152, trim(0, 267))],
        }
        self.assertEqual(_gapless_clock(json.dumps(fixture).encode())["sample_count"], 932)
        for first, last in (
            (packet(0, trim(0, 0)), packet(1152, trim(1105, 267))),
            (packet(0, trim(1105, 267)), packet(1152, trim(0, 0))),
            (packet(0, trim(1105, 0)), packet(1153, trim(0, 267))),
            (packet(0, trim(1105, 0)), packet(1152, trim(0, 0))),
        ):
            with self.assertRaisesRegex(VoiceError, "mp3_timing_unproven"):
                _gapless_clock(json.dumps({**fixture, "packets": [first, last]}).encode())
