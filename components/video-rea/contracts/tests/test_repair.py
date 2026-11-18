# SPDX-License-Identifier: AGPL-3.0-only
"""Regression cases for the independent frozen-contract review."""
import json
from pathlib import Path
import shutil
import tempfile

from contracts.tests import test_contracts as base


AGGREGATES = """
keyframe_set workflow software_model spec reimpl_plan scaffold_receipt verification report
game.physics game.level_map game.model game.replay_metrics
""".split()


def record(name):
    return base.example(name)["valid"]


class ReviewTests(base.ContractCase):
    def reject(self, name, value):
        self.assertFalse(self.validator(name).is_valid(value), f"{name} accepted {value!r}")

    def test_frame_provenance_guards(self):
        item = record("rea.frame")
        for key, value in (("label", "guessed"), ("extra", True), ("video_sha256", "g" * 64)):
            with self.subTest(key=key):
                self.reject("rea.frame", dict(item, provenance=dict(item["provenance"], **{key: value})))

    def test_ok_element_guards(self):
        item = record("rea.ui_element")
        self.reject("rea.ui_element", dict(item, confidence=None))
        self.reject("rea.ui_element", dict(item, degraded_reason="unavailable"))

    def test_exact_replay_rejects_distribution(self):
        pair = base.example("rea.game.replay_metrics")
        self.reject("rea.game.replay_metrics", dict(pair["valid"], distribution=pair["distribution"]["distribution"]))

    def test_scale_requires_bounds_and_witness(self):
        physics = record("rea.game.physics")
        for key in ("lower", "upper", "provenance"):
            with self.subTest(key=key):
                scale = physics["pixel_to_world"].copy()
                del scale[key]
                self.reject("rea.game.physics", dict(physics, pixel_to_world=scale))

    def test_ingest_rejects_output_dir(self):
        name = "vp_rea_call.rea.ingest.args"
        args = dict(record(name), output_dir="/example/output")
        self.reject(name, args)
        self.reject("vp_rea_call.args", args)

    def test_telemetry_operation(self):
        self.reject("rea.telemetry", dict(record("rea.telemetry"), operation="exfil"))

    def test_transition_brackets(self):
        for key in ("before", "after"):
            with self.subTest(key=key):
                item = record("rea.transition")
                del item[key]
                self.reject("rea.transition", item)

    def test_nonempty_held_out(self):
        self.reject("rea.verification", dict(record("rea.verification"), held_out=[]))

    def test_required_time_base(self):
        source = record("rea.video_source")
        del source["time_base"]
        self.reject("rea.video_source", source)

    def test_pin_inventory_rejects_stale_entry(self):
        from contracts import pins
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            for path in base.ROOT.glob("*.json"):
                shutil.copyfile(path, stage / path.name)
            manifest = stage / "PINS.json"
            value = base.read(manifest)
            value["files"]["removed.schema.json"] = "0" * 64
            manifest.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(pins.check(stage), 5)

    def test_gui_action_and_branch(self):
        action = record("rea.action")
        self.assertEqual(action["kind"], "click")
        self.validator("rea.action").validate(dict(action, kind="cursor_move", target=None, pointer=[0, 9]))
        for edit in ({"kind": "warp"}, {"pointer": [-1, 3]}, {"target_element_sha256": "?"}):
            with self.subTest(edit=edit):
                self.reject("rea.action", dict(action, **edit))
        transition = record("rea.transition")
        self.validator("rea.transition").validate(dict(
            transition, guard=transition["trigger"], alternatives=["cancel"], action_refs=["a" * 64],
        ))
        for key, bad in (("guard", True), ("alternatives", [""]), ("action_refs", ["?"])):
            with self.subTest(key=key):
                self.reject("rea.transition", dict(transition, **{key: bad}))
                shortened = transition.copy()
                del shortened[key]
                self.reject("rea.transition", shortened)

    def test_tile_map_and_input(self):
        level = record("rea.game.level_map")
        tiles = level["tiles"]
        self.reject("rea.game.level_map", dict(level, tiles=dict(tiles, legend={"X": "whatever"})))
        self.reject("rea.game.level_map", dict(level, tiles=dict(tiles, tile_size_px=0)))
        event = dict(record("rea.game.event"), event_kind="input")
        self.reject("rea.game.event", event)
        self.validator("rea.game.event").validate(dict(event, input={"control": "jump", "state": "pressed"}))
        self.reject("rea.game.event", dict(event, input={"control": "jump", "state": "maybe"}))
        self.reject("rea.game.event", dict(event, event_kind="win", input={"control": "jump", "state": "released"}))

    def test_aggregate_lineage(self):
        for short in AGGREGATES:
            name = "rea." + short
            with self.subTest(name=name):
                item = record(name)
                self.assertNotIn("provenance", item)
                for key in ("derived_from", "evidence"):
                    self.assertIn(key, item)
                    self.reject(name, dict(item, **{key: []}))
                    del_field = item.copy()
                    del del_field[key]
                    self.reject(name, del_field)
                self.reject(name, dict(item, derived_from=["?"]))
                self.reject(name, dict(item, evidence=[{"label": "inferred"}]))

    def test_source_metadata(self):
        source = record("rea.video_source")
        self.assertEqual(set(source["provenance"]), {"video_sha256", "byte_size"})
        for key in ("start_pts", "duration_ms", "frame_count", "width", "height", "codec", "container", "vfr", "pts_source"):
            with self.subTest(key=key):
                self.assertIn(key, source)
                self.validator("rea.video_source").validate(dict(source, **{key: {"value": None, "reason": "unavailable"}}))
                self.reject("rea.video_source", dict(source, **{key: {"value": None, "reason": ""}}))
                self.reject("rea.video_source", dict(source, **{key: None}))
        self.reject("rea.video_source", dict(source, pts_source="index_times_nominal_fps"))

    def test_physics_estimates_and_ticks(self):
        physics = record("rea.game.physics")
        unknown = {"value": None, "reason": "camera cut"}
        for key in ("gravity", "run_speed", "jump_velocity", "friction", "pixel_to_world", "tick_hz"):
            with self.subTest(key=key):
                self.validator("rea.game.physics").validate(dict(physics, **{key: unknown}))
                self.reject("rea.game.physics", dict(physics, **{key: None}))
                self.reject("rea.game.physics", dict(physics, **{key: {"value": None}}))
        self.reject("rea.game.physics", dict(physics, tick_hz={"value": -1, "lower": -2, "upper": 5, "unit": "Hz"}))

    def test_semantic_checks(self):
        from contracts.semantics import check_record
        physics = record("rea.game.physics")
        check_record(physics)
        for key in ("gravity", "run_speed", "jump_velocity", "friction", "pixel_to_world", "tick_hz"):
            for bad in ({"upper": -1000}, {"lower": 1000}, {"lower": 3, "upper": 1}):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    check_record(dict(physics, **{key: dict(physics[key], **bad)}))
        level = record("rea.game.level_map")
        check_record(level)
        for grid in ([["undefined"]], [[None, None], [None]]):
            with self.assertRaises(ValueError):
                check_record(dict(level, tiles=dict(level["tiles"], grid=grid)))
        verification = record("rea.verification")
        check_record(verification)
        with self.assertRaises(ValueError):
            check_record(dict(verification, train_frames_sha256=[verification["held_out"][0]["frame_sha256"]]))

    def test_scaffold_paths(self):
        args_name = "vp_rea_call.rea.scaffold.args"
        receipt_name = "rea.scaffold_receipt"
        receipt = record(receipt_name)
        args = record(args_name)
        invalid_paths = (
            "/", "//", "/./", "/../", "/tmp/out//", "rel", "../../etc", "/a/../etc",
            "/a/.", "/a\n", "/a/\x00b", "/a//b", "/a\\b",
        )
        for name, instance, key in (
            (args_name, args, "output_dir"),
            ("vp_rea_call.args", args, "output_dir"),
            (receipt_name, receipt, "output_dir"),
            (receipt_name, receipt, "resolved_output_dir"),
        ):
            for path in ("/tmp/out", "/tmp/out/"):
                with self.subTest(name=name, key=key, path=path):
                    self.validator(name).validate(dict(instance, **{key: path}))
            for path in invalid_paths:
                with self.subTest(name=name, key=key, path=path):
                    self.reject(name, dict(instance, **{key: path}))
        for key in ("resolved_output_dir", "repo_root_excluded"):
            shortened = receipt.copy()
            del shortened[key]
            self.reject(receipt_name, shortened)
        self.assertIs(receipt["repo_root_excluded"], True)
        self.reject(receipt_name, dict(receipt, repo_root_excluded=False))
        for path in ("a\n", "a/\tb"):
            self.reject(receipt_name, dict(receipt, files=[dict(receipt["files"][0], relative_path=path)]))

    def test_op_options(self):
        invalid_options = {
            "sample": {"strategy": "guess", "budget_frames": 0},
            "verify": {"held_out_split": ""},
            "game.replay": {"mode": "unknown", "sample_count": 0},
            "live.tap": {"surface": "", "duration_ms": 0, "privacy_fence_ref": ""},
        }
        for op, invalid in invalid_options.items():
            name = "vp_rea_call.rea." + op + ".args"
            args = base.example(name)["options"]
            for target in (name, "vp_rea_call.args"):
                with self.subTest(op=op, target=target):
                    self.validator(target).validate(args)
                    self.reject(target, dict(args, output_dir="/scratch"))
                    for key, bad in invalid.items():
                        with self.subTest(field=key):
                            self.reject(target, dict(args, **{key: bad}))

    def test_live_tap_requires_surface_and_privacy_fence(self):
        name = "vp_rea_call.rea.live.tap.args"
        args = base.example(name)["options"]
        del args["duration_ms"]
        for target in (name, "vp_rea_call.args"):
            with self.subTest(target=target):
                self.validator(target).validate(args)
                for key in ("surface", "privacy_fence_ref"):
                    with self.subTest(missing=key):
                        shortened = args.copy()
                        del shortened[key]
                        self.reject(target, shortened)

    def test_measured_verification(self):
        item = record("rea.verification")
        self.assertTrue(item["train_frames_sha256"])
        for bad in ([], ["?"]):
            self.reject("rea.verification", dict(item, train_frames_sha256=bad))
        for key in ("value", "threshold"):
            entry = item["checks"][0].copy()
            del entry[key]
            self.reject("rea.verification", dict(item, checks=[entry]))

    def test_element_tracking_and_replay_modes(self):
        item = record("rea.ui_element")
        self.assertTrue(item["element_id"])
        for kind in ("tab", "scrollbar", "slider", "cursor"):
            self.validator("rea.ui_element").validate(dict(item, kind=kind))
        self.reject("rea.ui_element", dict(item, element_id=""))
        for name in ("valid", "distribution"):
            metric = base.example("rea.game.replay_metrics")[name]
            expected = "distribution" if metric["distribution_match"] else "exact"
            self.assertEqual(metric["mode"], expected)
            self.reject("rea.game.replay_metrics", dict(metric, mode="exact" if expected == "distribution" else "distribution"))
