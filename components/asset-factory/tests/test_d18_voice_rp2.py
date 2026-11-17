"""Regression controls for contextual bilabials and Rhubarb cue identity."""

from hashlib import sha256
import json
import unittest

from test_d18_voice_rp1 import alignment, voiced_track, wav
from test_d18_voice_track import FPS, QUIET, timing_fixture
from tools.lipsync import PPM
from tools.lipsync.rhubarb import track_from_cues


def rhubarb_track(audio, cues, sound_file="/tmp/voice.wav"):
    metadata = json.dumps({"soundFile": sound_file, "duration": 0.875})
    raw = f'{{"metadata":{metadata},"mouthCues":{cues}}}'.encode()
    return track_from_cues(audio, raw, fps=FPS, silence_peak_pcm=QUIET, max_frames=100)


class VoiceRepairRoundTwoTests(unittest.TestCase):
    def test_silent_p_closure(self):
        # The reviewer's exact a-p-a fixture and its above-threshold control.
        tone = [9000, 0, -9000, 0] * 240
        timing = alignment(list("apa"), [480, 1440, 1728], [1440, 1728, 2688])
        silent = wav([0] * 480 + tone + [0] * 288 + tone + [0] * 1512)
        noisy = wav([0] * 480 + tone + [60, -60] * 144 + tone + [0] * 1512)
        track = voiced_track(timing, silent)
        self.assertEqual(track["frames"][7]["mouth_ppm"]["closed"], PPM)
        self.assertEqual(track["frames"][7]["jaw_ppm"], 0)
        self.assertEqual(track["frames"], voiced_track(timing, noisy)["frames"])
        self.assertEqual(track["inactive_aligned_spans"], [])
        rhubarb = rhubarb_track(silent, """[
            {"start":0.1,"end":0.3,"value":"D"},
            {"start":0.3,"end":0.36,"value":"A"},
            {"start":0.36,"end":0.56,"value":"D"}
        ]""")
        self.assertEqual(track["frames"], rhubarb["frames"])

    def test_bilabial_gap_is_bounded_by_both_activity_edges(self):
        # The short cue ends well before the right edge of the PCM gap.
        timing = alignment(["b"], [600], [680])
        for gap, expected in ((20, "closed"), (720, "closed"), (721, "rest")):
            with self.subTest(gap=gap):
                first = 600 if gap != 20 else 640
                cue = timing if gap != 20 else alignment(["b"], [645], [655])
                audio = wav([8000] * first + [0] * gap + [8000] * 200)
                track = voiced_track(cue, audio)
                self.assertEqual(track["frames"][3]["mouth_ppm"][expected], PPM)
                self.assertEqual(bool(track["inactive_aligned_spans"]), expected == "rest")

    def test_vowels_and_unbracketed_closures_stay_gated(self):
        for character, before, after in (
            ("a", 8000, 8000), ("p", 0, 8000), ("p", 8000, 0), ("p", 0, 0),
        ):
            with self.subTest(character=character, before=before, after=after):
                audio = wav([before] * 600 + [0] * 288 + [after] * 600)
                cue = alignment([character], [600], [888])
                track = voiced_track(cue, audio)
                self.assertEqual(len(track["inactive_aligned_spans"]), 1)
                self.assertTrue(all(frame["mouth_ppm"]["rest"] == PPM
                                    for frame in track["frames"]))

    def test_rhubarb_cue_identity_binds_original_values_and_exact_times(self):
        audio = timing_fixture()
        tracks = [
            rhubarb_track(audio, f'[{{"start":0,"end":0.875,"value":"{value}"}}]')
            for value in "BCH"
        ]
        self.assertTrue(all(track["frames"] == tracks[0]["frames"] for track in tracks))
        self.assertEqual(len({track["alignment_sha256"] for track in tracks}), 3)
        expected = sha256(b'[{"end":0.875,"start":0,"value":"B"}]\n').hexdigest()
        self.assertEqual(tracks[0]["alignment_sha256"], expected)
        for start, end in (("0.00001", "0.875"), ("0", "0.87499")):
            changed = rhubarb_track(audio, f'[{{"start":{start},"end":{end},"value":"B"}}]')
            self.assertEqual(changed["frames"], tracks[0]["frames"])
            self.assertNotEqual(changed["alignment_sha256"], expected)

    def test_rhubarb_cue_identity_ignores_metadata_path_and_json_format(self):
        audio = timing_fixture()
        first = rhubarb_track(audio, '[{"start":0,"end":0.875,"value":"C"}]')
        moved = rhubarb_track(audio, '[ {"value": "C", "end": 8.750e-1, "start": 0.0} ]',
                              sound_file="/different/temp/clip.wav")
        self.assertEqual(first, moved)
