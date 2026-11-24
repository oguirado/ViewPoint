# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic controls for fixture attribution."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


_SPEC = spec_from_file_location("at_api_method", Path(__file__).resolve().parents[1] / "at_api_method.py")
_METHOD = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_METHOD)


def observation(source, /, **overrides):
    row = {
        "episode_id": "episode-synthetic",
        "action_id": "activate-toggle",
        "source": source,
        "signal": "enabled",
        "status": "observed",
        "value": True,
    }
    row.update(overrides)
    return row


def metadata_only(row):
    """Leave an unusable value so payload validation must stay gated."""
    return dict(row, value=object())


class AttributionTests(unittest.TestCase):
    def check(self, *rows, signals=("enabled",)):
        return _METHOD.check_attribution(
            episode_id="episode-synthetic",
            action_id="activate-toggle",
            expected_signals=signals,
            observations=iter(rows),
        )

    def test_agreeing_witnesses_bind_to_one_occurrence(self):
        result = self.check(observation("api"), observation("at"), observation("at"))
        self.assertEqual(result, {
            "schema": "rea.at_api_attribution/v1",
            "verdict": "pass",
            "coverage": {"expected_count": 1, "observed_count": 1, "absent_count": 0,
                         "denied_count": 0, "conflict_count": 0},
            "episode_id": "episode-synthetic",
            "action_id": "activate-toggle",
            "signals": {
                "enabled": {
                    "status": "observed",
                    "value": True,
                    "sources": {
                        "at": {"status": "observed", "observation_indexes": [1, 2]},
                        "api": {"status": "observed", "observation_indexes": [0]},
                    },
                },
            },
        })

    def test_rejects_reused_action_id_in_another_episode_and_other_action(self):
        for override in ({"episode_id": "another-episode"}, {"action_id": "another-action"}):
            with self.subTest(override=override):
                wrong = metadata_only(observation("api", **override))
                with self.assertRaisesRegex(ValueError, "different episode or action"):
                    self.check(observation("at"), wrong)

    def test_rejects_observation_reassigned_after_yield(self):
        row = metadata_only(observation("at"))

        def rows():
            yield row
            row["action_id"] = "another-action"

        with self.assertRaisesRegex(ValueError, "metadata changed"):
            _METHOD.check_attribution(
                episode_id="episode-synthetic",
                action_id="activate-toggle",
                expected_signals=("enabled",),
                observations=rows(),
            )

    def test_conflict_across_sources_exposes_witnesses_without_a_value(self):
        result = self.check(observation("at"), observation("api", value=False))
        signal = result["signals"]["enabled"]
        self.assertEqual(signal["status"], "conflict")
        self.assertNotIn("value", signal)
        self.assertEqual(signal["sources"]["at"]["observation_indexes"], [0])
        self.assertEqual(signal["sources"]["api"]["observation_indexes"], [1])

    def test_conflict_within_source_survives_agreement_with_another_source(self):
        result = self.check(observation("at"), observation("at", value=1), observation("api"))
        signal = result["signals"]["enabled"]
        self.assertEqual(signal["status"], "conflict")
        self.assertEqual(signal["sources"]["at"]["status"], "conflict")
        self.assertNotIn("value", signal)

    def test_explicit_absence_conflicts_with_an_observation_from_the_same_source(self):
        rows = [
            metadata_only(observation("at", status="absent")),
            observation("at", value=True),
            observation("api", value=True),
        ]
        report = self.check(*rows)
        signal = report["signals"]["enabled"]
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(signal["status"], "conflict")
        self.assertNotIn("value", signal)
        self.assertEqual(signal["sources"]["at"], {
            "status": "conflict", "observation_indexes": [0, 1]
        })

    def test_unavailable_adapter_is_distinct_from_absent_observation(self):
        result = _METHOD.check_attribution(
            episode_id="episode-synthetic", action_id="activate-toggle",
            expected_signals=["enabled"], observations=[], available_sources=["at"],
        )
        self.assertEqual(result["verdict"], "incomplete")
        self.assertEqual(result["signals"]["enabled"]["sources"], {
            "at": {"status": "absent"}, "api": {"status": "unavailable"}
        })
        with self.assertRaisesRegex(ValueError, "unavailable source"):
            _METHOD.check_attribution(
                episode_id="episode-synthetic", action_id="activate-toggle",
                expected_signals=["enabled"], observations=[metadata_only(observation("api"))],
                available_sources=["at"],
            )

    def test_no_rows_and_explicit_absence_have_no_value(self):
        absent = metadata_only(observation("at", status="absent"))
        for rows in ((), (absent,)):
            with self.subTest(row_count=len(rows)):
                signal = self.check(*rows)["signals"]["enabled"]
                self.assertEqual(signal, {
                    "status": "absent",
                    "sources": {"at": {"status": "absent"}, "api": {"status": "absent"}},
                })

    def test_observed_null_is_a_value_from_one_source(self):
        signal = self.check(observation("at", value=None))["signals"]["enabled"]
        self.assertEqual(signal["status"], "observed")
        self.assertIsNone(signal["value"])
        self.assertEqual(signal["sources"]["api"], {"status": "absent"})

    def test_denial_blocks_payload_reads_in_either_row_order(self):
        denied = metadata_only(observation("at", status="denied"))
        protected = metadata_only(observation("at"))
        for rows in ((protected, denied), (denied, protected)):
            signal = self.check(*rows)["signals"]["enabled"]
            self.assertEqual(signal, {
                "status": "denied",
                "sources": {"at": {"status": "denied"}, "api": {"status": "absent"}},
            })

    def test_denial_is_scoped_to_source_and_signal(self):
        result = self.check(
            metadata_only(observation("at", status="denied")),
            observation("api", value=False),
            observation("at", signal="label", value="Toggle"),
            signals=("enabled", "label"),
        )
        enabled = result["signals"]["enabled"]
        self.assertEqual((enabled["status"], enabled["value"]), ("observed", False))
        self.assertEqual(enabled["sources"]["at"], {"status": "denied"})
        self.assertEqual(result["signals"]["label"]["value"], "Toggle")

    def test_rejects_malformed_metadata_before_payload_access(self):
        cases = (
            ({"source": "video"}, "unknown observation source"),
            ({"signal": "unrequested"}, "unexpected observation signal"),
            ({"status": "pending"}, "unknown observation status"),
            ({"action_id": None}, "observation action_id"),
        )
        for override, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    self.check(metadata_only(observation("at", **override)))

    def test_rejects_missing_and_invalid_permitted_values(self):
        missing = observation("api")
        del missing["value"]
        cases = (
            (missing, "missing value"),
            (observation("api", value=float("nan")), "must be finite"),
            (observation("api", value=[True]), "must be a JSON scalar"),
        )
        for row, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    self.check(row)

    def test_rejects_mutated_denied_and_absent_metadata_without_reading_payload(self):
        for status in ("denied", "absent"):
            with self.subTest(status=status):
                row = metadata_only(observation("at", status=status))

                def rows():
                    yield row
                    row["source"] = "api"
                    row["status"] = "observed"
                    yield row

                with self.assertRaisesRegex(ValueError, "metadata changed"):
                    _METHOD.check_attribution(
                        episode_id="episode-synthetic", action_id="activate-toggle",
                        expected_signals=["enabled"], observations=rows(),
                    )

    def test_rejects_mapping_or_string_as_observation_collection(self):
        for observations in ({}, "", None):
            with self.subTest(observations=observations):
                with self.assertRaisesRegex(ValueError, "observations must be"):
                    _METHOD.check_attribution(
                        episode_id="episode-synthetic", action_id="activate-toggle",
                        expected_signals=["enabled"], observations=observations,
                    )

    def test_rejects_extra_identity_and_keeps_number_types_distinct(self):
        with self.assertRaisesRegex(ValueError, "unexpected observation field"):
            self.check(metadata_only(observation("at", occurrence_id="different-event")))
        report = self.check(observation("at", value=1), observation("api", value=1.0))
        self.assertEqual(report["verdict"], "fail")

    def test_rejects_ambiguous_coverage_scope(self):
        for signals in ((), ("enabled", "enabled"), "enabled"):
            with self.subTest(signals=signals):
                with self.assertRaises(ValueError):
                    self.check(signals=signals)


if __name__ == "__main__":
    unittest.main()
