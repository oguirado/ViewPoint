"""Receipt and decoded-byte controls for the retained character fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory, mkdtemp
import time
import unittest

from asset_factory import core
from tools.motion import action
from tools.pipeline import animated_check, character_stages
from tools.pipeline.contracts import RUN, validate_stage


def write_record(path, value):
    core.atomic_write_json(path, value)
    return ref(path)


def ref(path, pin=None):
    return {"path": str(path), "sha256": pin or core.sha256_file(path)}


def record(reference):
    return json.loads(Path(reference["path"]).read_bytes())


def replace_row(doc, binary, index, edit, fmt, row=0):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    offset += row * view.get("byteStride", struct.calcsize(fmt))
    values = struct.unpack_from(fmt, binary, offset)
    struct.pack_into(fmt, binary, offset, *edit(values))


class WireCharacterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        location = os.environ.get("WIRE_D_FIXTURES")
        if location is None:
            raise unittest.SkipTest("WIRE_D_FIXTURES must name the retained campaign out directory")
        cls.sources = Path(location)
        cls.act = cls.sources / "act"
        cls.skin = ref(cls.act / "skin.glb", "ed6cfa4d821e57f82d0c6e5165bedc52712e094f85d1ad955c5d6fcbd3cfa79c")
        cls.clip = ref(cls.act / "walk-gesture.glb",
                       "e24b0b6748b288cd125db021b7b1ab2db3ac30c0ab30d9e22a6b4f220e6604fe")
        cls.act_registration = ref(cls.act / "rp1/registration.json",
                                   "f54bca3f5e9bfac542ea0b8ef4ccd38e3b8ca29b42b2b17c4ec44a5a091d63aa")
        cls.act_plan = record(cls.act_registration)

    def setUp(self):
        evidence = os.environ.get("WIRE_D_EVIDENCE")
        if evidence:
            self.root = Path(mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        else:
            temporary = TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)

    def make_runner(self, *, other_rig=False, export_other=False, historical_repin=False):
        from test_d18_voice_track import FPS, QUIET, labels, provider_fixture, timing_fixture
        from test_n18_face_blender import run_blender

        face = self.sources / "face/fu1/verified/reference"
        phys = self.sources / "phys/rp1/verified"
        policy = Path("providers/unirig/gate-policy.json").resolve()
        source_skin = ref(self.act / "skin-receipt.json", self.act_plan["skin_receipt_sha256"])
        if other_rig:
            skin_glb = action.open_glb(Path(self.skin["path"]).read_bytes(),
                                       time.monotonic_ns() + 5_000_000_000)
            skin_glb.doc["nodes"][skin_glb.doc["skins"][0]["joints"][0]]["name"] = "foreign_joint"
            foreign_path = self.root / "foreign-rig.glb"
            foreign_path.write_bytes(action.pack_glb(skin_glb.doc, skin_glb.binary[:skin_glb.buffer_length]))
            foreign = record(source_skin) | {"rigged_sha256": core.sha256_file(foreign_path)}
            source_skin = write_record(self.root / "foreign-skin.json", foreign)
        elif historical_repin:
            altered = record(source_skin)
            altered["wall_ns"] += 1
            source_skin = write_record(self.root / "repinned-skin.json", altered)
        voice_audio, alignment = self.root / "voice.wav", self.root / "alignment.json"
        voice_audio.write_bytes(timing_fixture())
        alignment.write_bytes(provider_fixture())
        witness = labels()
        for field in ("mouth_onsets", "silences"):
            witness[field] = [list(row) for row in witness[field]]
        plan = {
            "schema": "af.character-fixture-registration/v1",
            "candidate_sha256": self.skin["sha256"],
            "face": {"args": ref(face / "args.json",
                                 "02c44a7f882751c6edff2297ffbc5bb059c66c122ca73990f54e93c04887d497")},
            "skin": {"anatomy": ref(self.act / "anatomy.json", self.act_plan["anatomy_sha256"]),
                     "policy": ref(policy), "registered_receipt": source_skin},
            "act": {"clip": self.clip, "registration": self.act_registration,
                    "support": ref(self.act / "rp1/support-plane.json", self.act_plan["support_plane_sha256"]),
                    "fps": list(FPS)},
            "voice": {"audio": ref(voice_audio), "alignment": ref(alignment),
                      "witness": write_record(self.root / "voice-witness.json", witness),
                      "fps": list(FPS), "silence_peak_pcm": QUIET, "max_frames": 100,
                      "onset_limit_frames": 2, "bridge_samples": 24},
            "phys": {"proxy": ref(phys / "render.glb",
                                  "e8ec1f4dd1f5bd884228a3a22b57f9060a9417437e630872855e739b5d26040d"),
                     "registration": ref(phys / "registration.json",
                                         "87b11fd15ca18e745bb1a2909f2d58140dc5c27638ccc01dac62ccd97bd780ec"),
                     "support": ref(phys / "support.json",
                                    "b53833ad949fdc4ebdd1105a72bc4f5940bc081f4e435f446e90d6262854ea2a"),
                     "collider": ref(phys / "collider.json",
                                     "235cac5d258f7ca43722c7c851a99816537c30c7b4fe4e1819e0f34fed0d8d87"),
                     "support_height_um": 0, "gravity_um_s2": [0, 0, -9_810_000]},
        }
        pin = core.sha256_file(Path(character_stages.__file__))
        upstream = self.clip if export_other else self.skin
        export = write_record(self.root / "export.json", {
            "schema": "af.pipeline-stage/v1", "stage": "export", "key": core.sha256_json(upstream),
            "rc": 0, "status": "OK", "verdict": "PASS", "inputs": [upstream], "previous": None,
            "parameters": {"scope": "fixture"}, "toolchain_sha256": pin,
            "outputs": [upstream], "evidence": [upstream], "subject": upstream["path"],
            "checks_evaluated": 1, "wall_ms": 0, "peak_rss_bytes": None,
            "absence_reasons": {"peak_rss_bytes": "fixture_receipt"}, "reason_codes": [],
        })

        def face_runner(args, directory):
            self.assertEqual(args, core.load_json(face / "args.json", max_bytes=16_384))
            run_blender("build", face, directory)
            run_blender("verify", face, directory, directory / "head.blend")
            return core.load_json(directory / "receipt.json", max_bytes=1_048_576)

        return character_stages.CharacterStages(
            self.root / "chain", candidate=self.skin, export_receipt=export,
            registration=write_record(self.root / "registration.json", plan),
            toolchain_sha256=pin, face_runner=face_runner,
        )

    def check(self, clip):
        return animated_check.check_animated(
            Path(clip["path"]), expected_sha256=clip["sha256"],
            skin_path=Path(self.skin["path"]), skin_sha256=self.skin["sha256"],
            animation_name=self.act_plan["name"], required_channels=self.act_plan["required_channels"],
        )

    def test_accepted_fixtures_chain_to_reopened_animated_glb(self):
        runner = self.make_runner()
        run = runner.run()
        core.validate(run, RUN)
        self.assertEqual((run["rc"], run["verdict"], run["artifact"]), (3, "CANNOT_SEE", None), run)
        self.assertEqual(run["reason_codes"], ["TARGET_UNCALIBRATED"])
        expected_order = ("face", "skin", "act", "voice", "phys")
        self.assertEqual(tuple(row["stage"] for row in run["stages"]), expected_order)
        prior = ref(self.root / "export.json")
        for row in run["stages"]:
            with self.subTest(stage=row["stage"]):
                self.assertEqual((row["rc"], row["verdict"]), (0, "PASS"), record(row["receipt"]))
                self.assertEqual(row["receipt"], ref(Path(row["receipt"]["path"])))
                stage = record(row["receipt"])
                validate_stage(stage)
                self.assertEqual(stage["previous"], prior)
                self.assertIn(self.skin, stage["inputs"])
                self.assertEqual(stage["parameters"]["calibration_state"], "TARGET_UNCALIBRATED")
                self.assertEqual(stage["parameters"]["production_verdict"], "CANNOT_SEE")
                prior = row["receipt"]
        act_stage = record(run["stages"][2]["receipt"])
        clip = next(value for value in act_stage["outputs"] if value["path"] == act_stage["subject"])
        self.assertEqual(clip["sha256"], self.clip["sha256"])
        check = self.check(clip)
        write_record(self.root / "reopen.json", check)
        self.assertEqual((check["rc"], check["verdict"], check["production_verdict"]),
                         (0, "PASS", "CANNOT_SEE"), check)
        self.assertEqual(check["metrics"]["channels"], len(self.act_plan["required_channels"]))

    def test_skin_receipt_for_other_rig_is_stale(self):
        run = self.make_runner(other_rig=True).run()
        self.assertEqual((run["rc"], run["reason_codes"]), (4, ["skin_receipt_rig_stale"]))
        self.assertEqual([row["stage"] for row in run["stages"]], ["face", "skin"])

    def test_export_receipt_must_bind_candidate(self):
        run = self.make_runner(export_other=True).run()
        self.assertEqual((run["rc"], run["reason_codes"]), (4, ["export_candidate_stale"]))
        self.assertEqual([row["stage"] for row in run["stages"]], ["face"])

    def test_historical_skin_repin_cannot_mint_act_authority(self):
        run = self.make_runner(historical_repin=True).run()
        self.assertEqual((run["rc"], run["reason_codes"]), (4, ["skin_receipt_stale"]))
        self.assertEqual([row["stage"] for row in run["stages"]], ["face", "skin", "act"])

    def substitute_previous(self, *, repin):
        runner = self.make_runner()
        face = runner.step(ref(self.root / "export.json"))
        self.assertEqual(face["rc"], 0)
        original = face["receipt"]
        substituted = record(original) | {"reason_codes": ["substituted"]}
        modified = write_record(Path(original["path"]), substituted)
        row = runner.step(modified if repin else original)
        self.assertEqual(row["rc"], 4)
        self.assertEqual(record(row["receipt"])["reason_codes"], [
            "previous_receipt_stale" if repin else "receipt_bytes_stale"])
        self.assertEqual(record(row["receipt"])["evidence"], [])
        run = runner.run()
        core.validate(run, RUN)
        self.assertEqual((run["rc"], run["artifact"]), (4, None), run)

    def test_mid_chain_substitution_refused(self):
        self.substitute_previous(repin=False)

    def test_mid_chain_repin_refused(self):
        self.substitute_previous(repin=True)

    def test_reopened_binary_and_channel_controls(self):
        clip = action.open_glb(Path(self.clip["path"]).read_bytes(), time.monotonic_ns() + 5_000_000_000)
        self.assertEqual(self.check(self.clip)["rc"], 0)

        def drop(doc, binary):
            animation = doc["animations"][0]
            animation["channels"].pop()
            animation["samplers"].pop()

        def edit_attr(name, fmt, edit):
            def mutate(doc, binary):
                index = doc["meshes"][0]["primitives"][0]["attributes"][name]
                replace_row(doc, binary, index, edit, fmt)
            return mutate

        def bad_time(doc, binary):
            sampler = doc["animations"][0]["samplers"][0]
            replace_row(doc, binary, sampler["input"], lambda _: (0.0,), "<f", row=1)

        def nonfinite_time(doc, binary):
            sampler = doc["animations"][0]["samplers"][0]
            replace_row(doc, binary, sampler["input"], lambda _: (float("inf"),), "<f", row=1)

        def bad_target(doc, binary):
            doc["animations"][0]["channels"][0]["target"]["node"] = len(doc["nodes"])

        def bad_joint(doc, binary):
            doc["skins"][0]["joints"][0] = len(doc["nodes"])

        def bad_bind(doc, binary):
            index = doc["skins"][0]["inverseBindMatrices"]
            replace_row(doc, binary, index, lambda _: (0.0,) * 15 + (1.0,), "<16f")

        def missing_normal(doc, binary):
            del doc["meshes"][0]["primitives"][0]["attributes"]["NORMAL"]

        cases = {
            "required_channel_missing": (drop, 3),
            "normal_orientation_changed": (edit_attr("NORMAL", "<3f", lambda xs: tuple(-x for x in xs)), 5),
            "normal_not_unit": (edit_attr("NORMAL", "<3f", lambda xs: tuple(x * 2 for x in xs)), 5),
            "normal_missing": (missing_normal, 3),
            "skin_weight_sum": (edit_attr("WEIGHTS_0", "<4f", lambda _: (0.0,) * 4), 5),
            "skin_weight_negative": (edit_attr("WEIGHTS_0", "<4f", lambda _: (-0.5, 1.5, 0.0, 0.0)), 5),
            "animation_time_order": (bad_time, 5),
            "animation_target_missing": (bad_target, 5),
            "inverse_bind_singular": (bad_bind, 5),
            "joints_invalid": (bad_joint, 5),
            "nonfinite_weight": (edit_attr("WEIGHTS_0", "<4f", lambda _: (float("nan"), 0., 0., 0.)), 2),
            "nonfinite_time": (nonfinite_time, 2),
        }
        for reason, (mutate, rc) in cases.items():
            with self.subTest(reason=reason):
                doc = deepcopy(clip.doc)
                binary = bytearray(clip.binary[:clip.buffer_length])
                mutate(doc, binary)
                path = self.root / (reason + ".glb")
                path.write_bytes(action.pack_glb(doc, binary))
                result = self.check(ref(path))
                write_record(path.with_suffix(".json"), result)
                expected = "nonfinite_accessor" if reason.startswith("nonfinite_") else reason
                self.assertEqual((result["rc"], result["reason_codes"]), (rc, [expected]), result)
