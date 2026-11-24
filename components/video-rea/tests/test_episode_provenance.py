"""Isolated standard-library tests for the episode receipt."""

import hashlib
import json
from pathlib import Path
import runpy
import unittest

MODULE = Path(__file__).resolve().parents[1] / "episode_provenance.py"
seal_episode = runpy.run_path(str(MODULE))["seal_episode"]


def episode():
    target = "synthetic-browser-option"
    return {
        "episode_id": "épisode",
        "target_id": target,
        "clock": "host-monotonic",
        "source_sha256": "a" * 64,
        "steps": {
            "before": {
                "target_id": target, "start_ns": 0, "end_ns": 1,
                "frame_index": 0, "frame_sha256": "b" * 64,
            },
            "action": {
                "target_id": target, "start_ns": 2, "end_ns": 2,
                "action_id": "toggle",
            },
            "after": {
                "target_id": target, "start_ns": 3, "end_ns": 4,
                "frame_index": 1, "frame_sha256": "c" * 64,
            },
        },
    }


class SealEpisodeTests(unittest.TestCase):
    def test_stable_receipt_over_canonical_utf8_evidence(self):
        record = episode()
        canonical = (
            '{"clock":"host-monotonic","episode_id":"épisode","source_sha256":"' + "a" * 64
            + '","steps":{"action":{"action_id":"toggle","end_ns":2,"start_ns":2,'
            '"target_id":"synthetic-browser-option"},"after":{"end_ns":4,"frame_index":1,'
            '"frame_sha256":"' + "c" * 64 + '","start_ns":3,"target_id":"synthetic-browser-option"},'
            '"before":{"end_ns":1,"frame_index":0,"frame_sha256":"' + "b" * 64
            + '","start_ns":0,"target_id":"synthetic-browser-option"}},'
            '"target_id":"synthetic-browser-option"}'
        )
        expected = {
            "schema": "rea.episode_provenance/v1",
            "evidence": record,
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        self.assertEqual(seal_episode(record), expected)
        self.assertEqual(record, episode())
        self.assertEqual(seal_episode(json.loads(canonical)), expected)

    def test_receipt_is_independent_and_binds_changed_evidence(self):
        record = episode()
        receipt = seal_episode(record)
        record["steps"]["action"]["action_id"] = "other-toggle"
        self.assertEqual(receipt["evidence"], episode())
        for owner, field, value in (
            (record, "episode_id", "episode-2"),
            (record, "clock", "another-monotonic-clock"),
            (record, "source_sha256", "d" * 64),
            (record["steps"]["after"], "frame_index", 2),
            (record["steps"]["after"], "frame_sha256", "e" * 64),
            (record["steps"]["after"], "end_ns", 5),
        ):
            with self.subTest(field=field):
                previous = seal_episode(record)["sha256"]
                owner[field] = value
                self.assertNotEqual(seal_episode(record)["sha256"], previous)
        changed_action = episode()
        changed_action["steps"]["action"]["action_id"] = "other-toggle"
        self.assertNotEqual(seal_episode(changed_action)["sha256"], receipt["sha256"])

    def test_each_step_rejects_cross_target_evidence(self):
        for name in ("before", "action", "after"):
            with self.subTest(step=name):
                record = episode()
                record["steps"][name]["target_id"] = "foreign-target"
                with self.assertRaisesRegex(ValueError, rf"steps\.{name}\.target_id"):
                    seal_episode(record)

    def test_intervals_must_be_ordered_and_strictly_separated(self):
        cases = (
            ("before", "end_ns", 2, "steps.before.end_ns"),
            ("after", "start_ns", 2, "steps.action.end_ns"),
            ("before", "end_ns", 3, "steps.before.end_ns"),
            ("after", "start_ns", 1, "steps.action.end_ns"),
            ("action", "start_ns", 3, "steps.action.start_ns"),
        )
        for name, field, value, path in cases:
            with self.subTest(step=name, field=field, value=value):
                record = episode()
                record["steps"][name][field] = value
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    seal_episode(record)

    def test_rejects_stale_or_reversed_frame_ordinals(self):
        for after_index in (0, 1):
            with self.subTest(after_index=after_index):
                record = episode()
                record["steps"]["before"]["frame_index"] = 1
                record["steps"]["after"]["frame_index"] = after_index
                with self.assertRaisesRegex(ValueError, r"steps\.before\.frame_index"):
                    seal_episode(record)

    def test_rejects_invalid_identifiers_and_digests(self):
        cases = (
            (None, "episode_id", ""),
            (None, "episode_id", "\ud800"),
            (None, "target_id", "synthetic-browser-option "),
            (None, "clock", None),
            (None, "source_sha256", "A" * 64),
            (None, "source_sha256", "a" * 64 + "\n"),
            ("action", "action_id", " \t "),
            ("before", "frame_sha256", "f" * 63),
            ("after", "frame_sha256", "g" * 64),
        )
        for name, field, value in cases:
            with self.subTest(step=name, field=field, value=value):
                record = episode()
                owner = record if name is None else record["steps"][name]
                owner[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    seal_episode(record)

    def test_rejects_non_json_objects_and_invalid_integer_types(self):
        for record in (None, [], (), object(), {1: "non-string key"}):
            with self.subTest(record=record):
                with self.assertRaisesRegex(ValueError, "record must be a JSON object"):
                    seal_episode(record)
        for field in ("start_ns", "end_ns", "frame_index"):
            for value in (True, -1, 0.0, "0", None):
                with self.subTest(field=field, value=value):
                    record = episode()
                    record["steps"]["before"][field] = value
                    with self.assertRaisesRegex(ValueError, rf"steps\.before\.{field}"):
                        seal_episode(record)

    def test_rejects_missing_and_foreign_fields(self):
        cases = (
            ("record", "source_sha256", False),
            ("record", "pixels", True),
            ("steps", "after", False),
            ("steps", "foreign", True),
            ("before", "frame_sha256", False),
            ("action", "action_id", False),
            ("after", "end_ns", False),
            ("after", "source_sha256", True),
        )
        for name, field, unexpected in cases:
            with self.subTest(object=name, field=field):
                record = episode()
                containers = {"record": record, "steps": record["steps"], **record["steps"]}
                owner = containers[name]
                if unexpected:
                    owner[field] = "foreign"
                else:
                    del owner[field]
                problem = "unexpected" if unexpected else "missing"
                with self.assertRaisesRegex(ValueError, rf"{problem}.*{field}"):
                    seal_episode(record)


if __name__ == "__main__":
    unittest.main()
