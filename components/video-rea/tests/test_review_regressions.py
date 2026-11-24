"""Independent regression tests for confirmed review findings."""

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_adjacent(name):
    path = Path(__file__).resolve().parents[1] / (name + ".py")
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_attribution = load_adjacent("at_api_method").check_attribution
RGB8Frame = load_adjacent("pixel_compare").RGB8Frame
compare_replay_events = load_adjacent("replay_compare").compare_replay_events


class ReviewRegressions(unittest.TestCase):
    def check_attribution(self, *rows):
        return check_attribution(
            episode_id="episode", action_id="action", expected_signals=["enabled"],
            observations=rows,
        )

    def test_malformed_permitted_value_refuses_even_with_a_conflict(self):
        metadata = dict(episode_id="episode", action_id="action", source="at", signal="enabled")
        absent = dict(metadata, status="absent")
        for payload in ({}, {"value": float("nan")}, {"value": [True]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.check_attribution(absent, dict(metadata, status="observed", **payload))

    def test_payload_access_cannot_mutate_binding(self):
        class MutatingRow(dict):
            def get(self, key, default=None):
                if key == "value":
                    self["occurrence_id"] = "foreign"
                return super().get(key, default)

        row = MutatingRow(episode_id="episode", action_id="action", source="at",
                          signal="enabled", status="observed", value=True)
        with self.assertRaisesRegex(ValueError, "plain dict"):
            self.check_attribution(row)

    def test_trace_container_subclass_cannot_mask_event_order(self):
        class MaskedTrace(list):
            def __iter__(self):
                return iter([{"event_id": "goal", "time_us": 100}])

        reference = [{"event_id": "goal", "time_us": 100}]
        candidate = MaskedTrace([{"event_id": "goal", "time_us": 500}])
        with self.assertRaisesRegex(ValueError, "candidate"):
            compare_replay_events(reference, candidate, late_tolerance_us=0)

    def test_pixel_buffer_subclass_cannot_mask_changes(self):
        class ZeroBytes(bytes):
            def __getitem__(self, index):
                return 0

        with self.assertRaises(ValueError):
            RGB8Frame(1, 1, ZeroBytes(b"\xff\xff\xff"))

    def test_untrusted_collection_subclass_is_refused(self):
        class CustomList(list):
            pass

        metadata = dict(episode_id="episode", action_id="action", source="at",
                        signal="enabled", status="observed", value=True)
        base = dict(episode_id="episode", action_id="action",
                    expected_signals=["enabled"], observations=[metadata])
        for field, entries in (("available_sources", ["at"]),
                               ("expected_signals", ["enabled"]),
                               ("observations", [metadata])):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    check_attribution(**dict(base, **{field: CustomList(entries)}))

    def test_plain_records_require_plain_string_keys(self):
        class CustomKey(str):
            pass

        row = {CustomKey("episode_id"): "episode", "action_id": "action",
               "source": "at", "signal": "enabled", "status": "observed", "value": True}
        with self.assertRaises(ValueError):
            self.check_attribution(row)
        event = {CustomKey("event_id"): "goal", "time_us": 100}
        with self.assertRaises(ValueError):
            compare_replay_events([event], [], late_tolerance_us=0)

    def test_unexpected_duplicates_count_in_both_categories(self):
        reference = [{"event_id": "goal", "time_us": 100}]
        candidate = reference + [{"event_id": "other", "time_us": t} for t in (101, 102)]
        result = compare_replay_events(reference, candidate, late_tolerance_us=0)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["coverage"]["unexpected_count"], 1)
        self.assertEqual(result["coverage"]["duplicate_count"], 1)


if __name__ == "__main__":
    unittest.main()
