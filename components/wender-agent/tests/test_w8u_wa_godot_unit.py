"""Offline W8U Godot controls; fixture choices are TARGET, never host evidence.

Pixel bytes are generated at runtime in RAM. No encoded image or reconstructible
pixel fixture is stored in this source, a log, or an output file. The verifier is
an explicitly injected test capability, not a substitute production OS contract.
"""
from contextlib import ExitStack
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import unittest
from unittest import mock

from wender.perceive.session_registry import Handle
from wender.perceive.w8u_godot import (
    CapsuleBinding, CommittedPresentation, VerifiedCapsulePair,
    W8UGodotError, W8UGodotPerception, W8UGodotUnavailable,
)
from wender.state import schema


TARGET_REVISION = hashlib.sha256(b"TARGET offline contract revision").hexdigest()
TARGET_SUBSET = "TARGET-test-only-common-subset"
TARGET_PIXEL_FORMAT = "TARGET-test-only-packed-RGB"
_DEFAULT = object()


def canonical(value):
    """Independent oracle; intentionally does not call schema.dumps."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def jsonl(value):
    return canonical(value) + b"\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def make_state(path="portfolio/table_companies", *, screen="portfolio", tick=12):
    # The hash always uses the literal UTF-8 path, independently of production.
    node_id = digest(path.encode("utf-8"))[:16]
    base = {
        "schema": "wender.state/v1",
        "source": {"kind": "godot", "url": None, "title": "TARGET offline"},
        "prune": {"name": "TARGET fixture", "version": 1},
        "ax_sha256": digest(b"TARGET AX full"),
        "ax_stable_sha256": digest(b"TARGET AX stable"),
        "viewport": {"width": 3, "height": 2, "dpr_ppm": 1000000},
        "nodes": [{
            "id": node_id, "role": "window", "name": "TARGET root",
            "depth": 0, "ordinal": 0, "parent": None,
            "states": {"tick": str(tick)}, "actionable": False,
            "box": [0, 0, 3, 2],
        }],
        "actionable": [],
        "counts": {"raw": 1, "nodes": 1, "actionable": 0},
    }
    sidecar = {
        "schema": "holdings.godot.uitree/v1",
        "base_sha256": digest(canonical(base)),
        "nodes": {node_id: {"control_path": path, "screen_id": screen}},
    }
    return base, sidecar


class TargetContract:
    """Test-only revision-pinned authenticated vectors, issued out of band.

    Possessing an arbitrary dict with an ``authenticated`` key grants nothing.
    Only issued opaque tokens and separately enrolled exact outcome bytes pass.
    Malformed but authenticated data can be enrolled to test consumer validation.
    """

    contract_name = "w8u-os-contract"
    revision = TARGET_REVISION
    xwd_subset = TARGET_SUBSET
    pixel_format = TARGET_PIXEL_FORMAT

    def __init__(self, binding):
        self.current = binding
        self.pairs = {}
        self.presentations = {}
        self.outcomes = set()
        self.verifications = []

    def current_binding(self):
        return self.current

    def issue_pair(self, pair):
        token = object()
        self.pairs[token] = copy.deepcopy(pair)
        return token

    def issue_presentation(self, presentation):
        token = object()
        self.presentations[token] = copy.deepcopy(presentation)
        return token

    def issue_outcome(self, raw, binding):
        self.outcomes.add((raw, binding))

    def verify_pair(self, token, binding):
        self.verifications.append("pair")
        try:
            value = self.pairs[token]
        except (KeyError, TypeError) as exc:
            raise ValueError("TARGET unauthenticated pair vector") from exc
        return copy.deepcopy(value)

    def verify_outcome(self, raw, binding):
        self.verifications.append("outcome")
        if (raw, binding) not in self.outcomes:
            raise ValueError("TARGET unauthenticated outcome vector")
        return raw

    def verify_presentation(self, token, binding):
        self.verifications.append("presentation")
        try:
            value = self.presentations[token]
        except (KeyError, TypeError) as exc:
            raise ValueError("TARGET unauthenticated presentation vector") from exc
        return copy.deepcopy(value)


class Case:
    """All numeric fixture dimensions, ticks and IDs in this class are TARGET."""

    def __init__(self, path="portfolio/table_companies", *, screen="portfolio",
                 publish=None):
        self.binding = CapsuleBinding(
            "target-run", "target-lease", "target-session", "target-stream",
            Handle("godot_window", "target-window", 2), 3)
        self.contract = TargetContract(self.binding)
        base, sidecar = make_state(path, screen=screen)
        pixels = os.urandom(3 * 2 * 3)
        metadata = {
            "seq": 1, "tick": 12, "tick_valid": True,
            "capture_mono_ns": 123456, "width": 3, "height": 2,
            "stride": 9, "pixel_format": TARGET_PIXEL_FORMAT,
            "sha256": digest(pixels),
            "binding_sha256": digest(b"TARGET externally authenticated binding"),
        }
        self.pair = VerifiedCapsulePair(
            self.binding, base, sidecar, metadata, pixels, pixels,
            "target-grab-1", "target-grab-1", TARGET_SUBSET)
        self.command = {"tick": 12, "seq": 1, "cmd": "set_view",
                        "args": {"screen_id": "portfolio"}}
        self.action_id = "target-action-1"
        self.outcome = {
            "seq": 1, "requested_tick": 12, "applied_tick": 12,
            "order_seq_id": 1, "status": "accepted", "reason": None,
            "state_sha256": sidecar["base_sha256"],
        }
        self.outcome_raw = None
        self.presentation = CommittedPresentation(
            self.binding, "target-presentation-1", "target-grab-1", 1,
            metadata["sha256"], metadata["binding_sha256"],
            sidecar["base_sha256"], self.action_id, 1, 12, 12,
            digest(jsonl(self.outcome)), True)
        self.published = []
        self.command_checks = []
        self.publisher = publish if publish is not None else self.published.append
        self.adapter = self.new_adapter()

    def validate_command(self, record):
        self.command_checks.append(copy.deepcopy(record))
        if record.get("cmd") != "set_view":
            return ["TARGET producer has no such command"]
        args = record.get("args")
        if not isinstance(args, dict) or set(args) != {"screen_id"}:
            return ["TARGET producer requires exactly screen_id"]
        if type(args["screen_id"]) is not str or not args["screen_id"]:
            return ["TARGET screen_id must be a nonempty string"]
        return []

    def new_adapter(self, **overrides):
        params = {
            "contract": self.contract, "contract_revision": TARGET_REVISION,
            "registries": {"screens": {"portfolio", "other"},
                           "cadences": {"tick", "quarterly"}},
            "command_validator": self.validate_command,
            "publish": self.publisher,
        }
        params.update(overrides)
        return W8UGodotPerception(self.binding, **params)

    def update_basis(self):
        basis = digest(canonical(self.pair.base))
        self.pair.sidecar["base_sha256"] = basis
        self.outcome["state_sha256"] = basis
        self.presentation = replace(self.presentation, basis_sha256=basis)

    def call(self, *, capsule=_DEFAULT, command=_DEFAULT, action_id=_DEFAULT,
             outcome=_DEFAULT, presentation=_DEFAULT, correlate=True,
             enroll_outcome=True):
        pair_token = self.contract.issue_pair(self.pair) if capsule is _DEFAULT else capsule
        if not correlate:
            return self.adapter.perceive(pair_token)
        raw_outcome = (jsonl(self.outcome) if self.outcome_raw is None
                       else self.outcome_raw) if outcome is _DEFAULT else outcome
        if enroll_outcome and isinstance(raw_outcome, bytes):
            self.contract.issue_outcome(raw_outcome, self.binding)
        proof = replace(self.presentation, outcome_sha256=digest(raw_outcome)) \
            if isinstance(raw_outcome, bytes) else self.presentation
        proof_token = self.contract.issue_presentation(proof) if presentation is _DEFAULT \
            else presentation
        return self.adapter.perceive(
            pair_token,
            command=jsonl(self.command) if command is _DEFAULT else command,
            action_id=self.action_id if action_id is _DEFAULT else action_id,
            outcome=raw_outcome,
            presentation=proof_token,
        )


class W8UGodotControls(unittest.TestCase):
    def assert_refused(self, case, code, rc, *, events=0, **kwargs):
        with self.assertRaises(W8UGodotError) as caught:
            case.call(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.rc, rc)
        self.assertTrue(str(caught.exception).startswith(code + ":"))
        self.assertEqual(len(case.published), events)
        return caught.exception

    def test_literal_dotted_and_screen_rooted_slash_goldens_are_distinct(self):
        expected = {
            "portfolio/table_companies": "a388e17dafda554b",
            "portfolio.table_companies": "8b0ac696871ad4b1",
        }
        observed = []
        for path, independent_golden in expected.items():
            with self.subTest(path=path):
                self.assertEqual(digest(path.encode("utf-8"))[:16], independent_golden)
                case = Case(path)
                result = case.call()
                observed.append(result.observation.state["nodes"][0]["id"])
                self.assertEqual(observed[-1], independent_golden)
                self.assertTrue(result.tick_bound)
                self.assertEqual(len(case.published), 1)
                self.assertEqual(len(case.command_checks), 1)
        self.assertNotEqual(observed[0], observed[1])

    def test_fully_correlated_outcome_presentation_state_and_frame_pass(self):
        case = Case()
        result = case.call()
        self.assertEqual(result.basis_sha256, digest(canonical(case.pair.base)))
        self.assertEqual(result.outcome_sha256, digest(jsonl(case.outcome)))
        self.assertEqual(result.outcome, case.outcome)
        self.assertEqual(result.presentation_id, "target-presentation-1")
        self.assertEqual(result.frame_metadata, case.pair.frame_metadata)
        self.assertEqual(result.observation.handle, case.binding.target_handle)
        self.assertEqual(result.observation.epoch, case.binding.epoch)
        self.assertEqual(result.observation.frame_seq, 1)
        self.assertEqual(result.observation.state, case.pair.base)
        self.assertEqual(schema.validate_state(result.observation.state), [])
        self.assertEqual(set(result.observation.state), set(schema.TOP_KEYS))
        self.assertNotIn("_perception", result.observation.state)
        self.assertFalse(result.observation.frame.rgb.flags.writeable)
        self.assertTrue(result.tick_bound)
        self.assertEqual(len(case.published), 1)

    def test_wrong_root_malformed_segments_controls_and_invalid_utf8_refuse(self):
        paths = (
            "other/table_companies", "portfolio//table_companies",
            "portfolio/../table_companies", "portfolio/./table_companies",
            "portfolio/table_companies/", "/portfolio/table_companies",
            "portfolio/table\x00companies", "portfolio/table\ncompanies",
            "portfolio/table\u200bcompanies", "portfolio/table\ud800companies",
            "portfolio/table companies",
        )
        for path in paths:
            with self.subTest(path=ascii(path)):
                # Wrong-root vector has a valid independent literal identity:
                # a hash mismatch cannot accidentally satisfy this refusal.
                case = Case(path, screen="portfolio") if path.startswith("other/") else Case()
                key = next(iter(case.pair.sidecar["nodes"]))
                case.pair.sidecar["nodes"][key]["control_path"] = path
                self.assert_refused(case, "E_SCHEMA", 2)

    def test_normalized_path_with_original_hash_and_hash_reuse_refuse(self):
        for path in ("portfolio.table_companies", "portfolio/another_control"):
            with self.subTest(path=path):
                case = Case()
                node_id = next(iter(case.pair.sidecar["nodes"]))
                case.pair.sidecar["nodes"][node_id]["control_path"] = path
                self.assert_refused(case, "E_SCHEMA", 2)

    def test_missing_or_mismatched_base_sidecar_identity_refuses(self):
        for mutation in ("missing", "orphan", "wrong-schema", "extra", "base-hash"):
            with self.subTest(mutation=mutation):
                case = Case()
                sidecar = case.pair.sidecar
                if mutation == "missing":
                    sidecar["nodes"].clear()
                elif mutation == "orphan":
                    sidecar["nodes"]["f" * 16] = {
                        "control_path": "portfolio/other", "screen_id": "portfolio"}
                elif mutation == "wrong-schema":
                    sidecar["schema"] = "wrong/v1"
                elif mutation == "extra":
                    sidecar["unknown"] = 1
                else:
                    sidecar["base_sha256"] = "0" * 64
                self.assert_refused(case, "E_STALE_FRAME" if mutation == "base-hash"
                                    else "E_SCHEMA", 5 if mutation == "base-hash" else 2)

    def test_state_unknown_keys_wrappers_invalid_types_and_bool_coordinates_refuse(self):
        mutations = (
            lambda s: s.update(unknown=1),
            lambda s: s.update(_perception={"authenticated": True}),
            lambda s: s["nodes"][0].update(unknown=1),
            lambda s: s["nodes"][0].update(box=[True, 0, 3, 2]),
            lambda s: s["nodes"][0].update(box=[0, 0, 3.0, 2]),
            lambda s: s["nodes"][0].update(box=[0, 0, float("nan"), 2]),
            lambda s: s["nodes"][0].update(depth=True),
            lambda s: s["nodes"][0].update(ordinal=False),
            lambda s: s["nodes"][0].update(name="invalid\ud800"),
            lambda s: s["counts"].update(nodes=True),
            lambda s: s["viewport"].update(width=True),
            lambda s: s["source"].update(unknown="authority"),
            lambda s: s["prune"].update(unknown=1),
            lambda s: s["counts"].update(unknown=1),
            lambda s: s.update(nodes=None),
            lambda s: s.update(nodes={}),
            lambda s: s.update(actionable=None),
            lambda s: s["nodes"][0].update(states=[]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(vector=index):
                case = Case()
                mutate(case.pair.base)
                self.assert_refused(case, "E_SCHEMA", 2)

    def test_only_empty_state_validation_error_list_is_success(self):
        for response in (["invalid"], None, False, True, (), {}):
            with self.subTest(response=response):
                case = Case()
                with mock.patch("wender.state.schema.validate_state", return_value=response):
                    self.assert_refused(case, "E_SCHEMA", 2)

    def test_generation_target_and_session_binding_mismatches_refuse(self):
        fields = (
            {"target_handle": Handle("godot_window", "target-window", 3)},
            {"target_handle": Handle("godot_window", "other-window", 2)},
            {"target_handle": Handle("x11_window", "target-window", 2)},
            {"session_id": "other-session"},
            {"stream_id": "other-stream"},
            {"run_id": "other-run"},
            {"lease_id": "other-lease"},
        )
        for changes in fields:
            with self.subTest(fields=tuple(changes)):
                case = Case()
                case.pair = replace(case.pair, binding=replace(case.binding, **changes))
                self.assert_refused(case, "E_STALE_FRAME" if "target_handle" in changes
                                    else "E_LOCK", 5)

    def test_current_generation_and_epoch_are_checked(self):
        for changes, code in (
            ({"target_handle": Handle("godot_window", "target-window", 3)}, "E_STALE_FRAME"),
            ({"epoch": 4}, "E_EPOCH"),
        ):
            with self.subTest(code=code):
                case = Case()
                case.contract.current = replace(case.binding, **changes)
                self.assert_refused(case, code, 5)
        case = Case()
        case.pair = replace(case.pair, binding=replace(case.binding, epoch=4))
        self.assert_refused(case, "E_EPOCH", 5)
        case = Case()
        case.presentation = replace(case.presentation,
                                    binding=replace(case.binding, session_id="other-session"))
        self.assert_refused(case, "E_LOCK", 5)
        case = Case()
        case.presentation = replace(case.presentation,
                                    binding=replace(case.binding, epoch=4))
        self.assert_refused(case, "E_EPOCH", 5)

    def test_binding_is_rechecked_immediately_before_publication(self):
        case = Case()
        original = case.contract.verify_presentation

        def replace_generation(token, binding):
            proof = original(token, binding)
            case.contract.current = replace(binding, target_handle=Handle(
                "godot_window", "target-window", 3))
            return proof

        case.contract.verify_presentation = replace_generation
        self.assert_refused(case, "E_STALE_FRAME", 5)

    def test_independent_accepted_and_rejected_authentication_vectors(self):
        accepted = Case()
        accepted.call()
        self.assertEqual(len(accepted.published), 1)
        for vector in (object(), {"authenticated": True, "revision": TARGET_REVISION}):
            with self.subTest(kind=type(vector).__name__):
                rejected = Case()
                self.assert_refused(rejected, "E_LOCK", 5, capsule=vector)
        rejected = Case()
        self.assert_refused(rejected, "E_LOCK", 5, enroll_outcome=False)
        rejected = Case()
        self.assert_refused(rejected, "E_LOCK", 5, presentation={"committed": True})

    def test_capability_output_must_be_exact_evidence_and_authenticate_exact_bytes(self):
        case = Case()
        case.contract.verify_pair = lambda token, binding: {"authenticated": True}
        self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        case.contract.verify_outcome = lambda raw, binding: raw[:-1]
        self.assert_refused(case, "E_LOCK", 5)
        case = Case()
        case.contract.verify_presentation = lambda token, binding: {"committed": True}
        self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        case.presentation = replace(case.presentation, committed=1)
        self.assert_refused(case, "E_UNCERTAIN", 6)

    def test_unticked_pair_equality_does_not_claim_engine_revision(self):
        case = Case()
        case.pair.frame_metadata.update(tick_valid=False, tick=None)
        result = case.call(correlate=False)
        self.assertFalse(result.tick_bound)
        self.assertIsNone(result.frame_metadata["tick"])
        self.assertIsNone(result.outcome)
        self.assertIsNone(result.outcome_sha256)
        self.assertIsNone(result.presentation_id)
        self.assertEqual(len(case.published), 1)
        self.assertEqual(len(case.command_checks), 0)
        joined_without_tick = Case()
        joined_without_tick.pair.frame_metadata.update(tick_valid=False, tick=None)
        self.assert_refused(joined_without_tick, "E_SCHEMA", 2)

    def test_tick_bound_pair_alone_missing_witnesses_and_uncommitted_presentation_refuse(self):
        case = Case()
        self.assert_refused(case, "E_UNCERTAIN", 6, correlate=False)
        for missing in ("outcome", "presentation", "command", "action_id"):
            with self.subTest(missing=missing):
                case = Case()
                self.assert_refused(case, "E_UNCERTAIN", 6, **{missing: None})
        case = Case()
        case.presentation = replace(case.presentation, committed=False)
        self.assert_refused(case, "E_UNCERTAIN", 6)

    def test_identical_pixels_from_different_grabs_and_unequal_pair_refuse(self):
        case = Case()
        case.pair = replace(case.pair, right_grab_id="other-grab")
        self.assert_refused(case, "E_STALE_FRAME", 5)
        case = Case()
        changed = bytes([case.pair.right_rgb[0] ^ 1]) + case.pair.right_rgb[1:]
        case.pair = replace(case.pair, right_rgb=changed)
        self.assert_refused(case, "E_STALE_FRAME", 5)

    def test_frame_metadata_exact_keys_types_and_pixel_layout(self):
        mutations = (
            ("extra", 1), ("seq", True), ("seq", 0), ("tick", True),
            ("tick_valid", 1), ("width", True), ("height", 0),
            ("stride", True), ("stride", 10), ("capture_mono_ns", -1),
            ("capture_mono_ns", 0.5), ("binding_sha256", "F" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                case = Case()
                case.pair.frame_metadata[key] = value
                self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        del case.pair.frame_metadata["tick_valid"]
        self.assert_refused(case, "E_SCHEMA", 2)
        for valid, tick in ((False, 0), (True, None), (False, 12)):
            with self.subTest(valid=valid, tick=tick):
                case = Case()
                case.pair.frame_metadata.update(tick_valid=valid, tick=tick)
                self.assert_refused(case, "E_SCHEMA", 2)

    def test_invalid_frame_digest_and_noncanonical_pair_layout_refuse(self):
        case = Case()
        case.pair.frame_metadata["sha256"] = "0" * 64
        self.assert_refused(case, "E_STALE_FRAME", 5)
        case = Case()
        case.pair = replace(case.pair, left_rgb=case.pair.left_rgb[:-1],
                            right_rgb=case.pair.right_rgb[:-1])
        self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        case.pair = replace(case.pair, left_rgb=bytearray(case.pair.left_rgb))
        self.assert_refused(case, "E_SCHEMA", 2)

    def test_uncopyable_typed_evidence_is_schema_refusal_not_a_crash(self):
        case = Case()
        malformed = replace(case.pair, left_rgb=memoryview(case.pair.left_rgb))
        # Bypass fixture deepcopy to exercise the adapter's own trust boundary.
        case.contract.verify_pair = lambda token, binding: malformed
        self.assert_refused(case, "E_SCHEMA", 2, capsule=object())

    def test_frame_sequence_replay_and_tick_rollback_never_republish(self):
        case = Case()
        case.call()
        self.assertEqual(len(case.published), 1)
        self.assert_refused(case, "E_STALE_FRAME", 5, events=1)
        case.pair.frame_metadata.update(seq=2, tick=11)
        case.pair.base["nodes"][0]["states"]["tick"] = "11"
        case.command["tick"] = 11
        case.outcome.update(requested_tick=11, applied_tick=11)
        case.presentation = replace(case.presentation, frame_seq=2,
                                    requested_tick=11, applied_tick=11)
        case.update_basis()
        self.assert_refused(case, "E_STALE_FRAME", 5, events=1)

    def test_unticked_frame_cannot_publish_state_older_than_committed_basis(self):
        case = Case()
        case.call()
        base, sidecar = make_state(tick=11)
        case.pair = replace(case.pair, base=base, sidecar=sidecar)
        case.pair.frame_metadata.update(seq=2, tick=None, tick_valid=False)
        self.assert_refused(case, "E_STALE_FRAME", 5, events=1, correlate=False)

    def test_monotonic_new_frame_can_publish_without_reopening_a_channel(self):
        case = Case()
        case.call()
        case.pair.frame_metadata["seq"] = 2
        case.presentation = replace(case.presentation, frame_seq=2,
                                    presentation_id="target-presentation-2")
        case.call()
        self.assertEqual(len(case.published), 2)
        self.assertEqual([r.observation.frame_seq for r in case.published], [1, 2])

    def test_tick_and_canonical_basis_join_mutants_refuse(self):
        mutations = (
            lambda c: c.pair.frame_metadata.update(tick=13),
            lambda c: c.outcome.update(requested_tick=13),
            lambda c: c.outcome.update(applied_tick=11),
            lambda c: c.outcome.update(applied_tick=13),
            lambda c: c.outcome.update(state_sha256="0" * 64),
            lambda c: c.outcome.update(seq=2),
            lambda c: setattr(c, "presentation", replace(c.presentation, basis_sha256="0" * 64)),
            lambda c: setattr(c, "presentation", replace(c.presentation, requested_tick=13)),
            lambda c: setattr(c, "presentation", replace(c.presentation, applied_tick=13)),
            lambda c: setattr(c, "presentation", replace(c.presentation, action_seq=2)),
            lambda c: setattr(c, "presentation", replace(c.presentation, action_id="other-action")),
            lambda c: setattr(c, "presentation", replace(c.presentation, frame_seq=2)),
            lambda c: setattr(c, "presentation", replace(c.presentation, frame_sha256="0" * 64)),
            lambda c: setattr(c, "presentation", replace(c.presentation, binding_sha256="0" * 64)),
            lambda c: setattr(c, "presentation", replace(c.presentation, grab_id="other-grab")),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(vector=index):
                case = Case()
                mutate(case)
                self.assert_refused(case, "E_STALE_FRAME", 5)

    def test_missing_and_conflicting_state_ticks_never_bind_screenshot(self):
        case = Case()
        del case.pair.base["nodes"][0]["states"]["tick"]
        case.update_basis()
        self.assert_refused(case, "E_STALE_FRAME", 5)
        case = Case()
        other_path = "portfolio/other"
        other_id = digest(other_path.encode("utf-8"))[:16]
        other = copy.deepcopy(case.pair.base["nodes"][0])
        other.update(id=other_id, ordinal=1, states={"tick": "13"})
        case.pair.base["nodes"].append(other)
        case.pair.base["counts"].update(raw=2, nodes=2)
        case.pair.sidecar["nodes"][other_id] = {
            "control_path": other_path, "screen_id": "portfolio"}
        case.update_basis()
        self.assert_refused(case, "E_STALE_FRAME", 5)

    def test_presentation_is_bound_to_exact_authenticated_outcome_jsonl(self):
        case = Case()
        original = case.contract.verify_presentation
        case.contract.verify_presentation = lambda token, binding: replace(
            original(token, binding), outcome_sha256="0" * 64)
        self.assert_refused(case, "E_STALE_FRAME", 5)

    def test_admission_rejected_and_proposed_are_not_accepted_t2_effects(self):
        for status, code, rc in (("admitted", "E_SCHEMA", 2),
                                 ("rejected", "E_UNCERTAIN", 6),
                                 ("proposed", "E_UNCERTAIN", 6)):
            with self.subTest(status=status):
                case = Case()
                case.outcome["status"] = status
                self.assert_refused(case, code, rc)
        case = Case()
        case.outcome = {"status": "admitted", "code": "OK_ADMITTED", "seq": 1}
        self.assert_refused(case, "E_SCHEMA", 2)

    def test_outcome_exact_closed_schema_integer_and_reason_rules(self):
        mutations = (
            ("extra", 0), ("seq", True), ("seq", 0), ("order_seq_id", False),
            ("order_seq_id", 0), ("requested_tick", -1), ("applied_tick", True),
            ("applied_tick", 12.0), ("reason", "bad_code"),
            ("reason", "1BAD"), ("reason", "BAD-CODE"), ("reason", ""),
            ("reason", 7), ("state_sha256", "A" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                case = Case()
                case.outcome[key] = value
                self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        del case.outcome["order_seq_id"]
        self.assert_refused(case, "E_SCHEMA", 2)
        case = Case()
        case.outcome["reason"] = "APPLIED_1"
        case.call()
        self.assertEqual(len(case.published), 1)

    def test_canonical_jsonl_duplicate_keys_invalid_utf8_and_trailing_data_refuse(self):
        case = Case()
        original = jsonl(case.outcome)
        vectors = (
            original.replace(b'"seq":1', b'"seq":1,"seq":1'),
            original.replace(b'"seq":1', b'"seq":1.0'),
            original.replace(b'"seq":1', b'"seq":NaN'),
            original.replace(b'"reason":null', b'"reason":"\xff"'),
            original[:-1], original + b"\n", original + b"{}\n",
            b" " + original,
            original.replace(b'"seq":1', b'"seq" : 1'),
        )
        for index, raw in enumerate(vectors):
            with self.subTest(vector=index):
                case = Case()
                case.outcome_raw = raw
                self.assert_refused(case, "E_SCHEMA", 2)

    def test_closed_producer_commands_refuse_before_any_publication(self):
        for mutation, code, rc in (
            (lambda c: c.update(extra=1), "E_SCHEMA", 2),
            (lambda c: c.update(seq=True), "E_SCHEMA", 2),
            (lambda c: c.update(tick=False), "E_SCHEMA", 2),
            (lambda c: c.update(cmd="invented"), "E_VERB", 5),
            (lambda c: c["args"].update(extra=1), "E_VERB", 5),
            (lambda c: c["args"].update(screen_id=True), "E_VERB", 5),
        ):
            with self.subTest(code=code):
                case = Case()
                mutation(case.command)
                self.assert_refused(case, code, rc)
        for response in (None, False, True, (), {}, ["producer refusal"]):
            with self.subTest(response=response):
                case = Case()
                case.adapter = case.new_adapter(command_validator=lambda record: response)
                self.assert_refused(case, "E_VERB", 5)

    def test_contract_revision_subset_and_pixel_format_remain_pinned(self):
        case = Case()
        case.contract.revision = "f" * 64
        self.assert_refused(case, "E_LOCK", 5)
        case = Case()
        case.pair = replace(case.pair, xwd_subset="other-subset")
        self.assert_refused(case, "E_STALE_FRAME", 5)
        case = Case()
        case.pair.frame_metadata["pixel_format"] = "other-format"
        self.assert_refused(case, "E_SCHEMA", 2)

    def test_missing_capabilities_are_explicitly_unavailable(self):
        for attr in ("verify_pair", "verify_outcome", "verify_presentation",
                     "current_binding", "xwd_subset", "pixel_format"):
            with self.subTest(attr=attr):
                case = Case()
                setattr(case.contract, attr, None)
                with self.assertRaises(W8UGodotUnavailable) as caught:
                    case.new_adapter()
                self.assertEqual(caught.exception.rc, 4)
                self.assertIsNone(caught.exception.code)
                self.assertIn("AUSENTE", str(caught.exception))
                self.assertEqual(len(case.published), 0)

    def test_lost_publication_ack_quarantines_without_new_id_retry(self):
        case = Case()

        def lost_ack(result):
            case.published.append(result)
            raise RuntimeError("TARGET lost acknowledgement")

        case.adapter = case.new_adapter(publish=lost_ack)
        self.assert_refused(case, "E_UNCERTAIN", 6, events=1)
        case.pair.frame_metadata["seq"] = 2
        case.action_id = "new-action-must-not-retry"
        case.presentation = replace(case.presentation, frame_seq=2,
                                    action_id=case.action_id)
        self.assert_refused(case, "E_UNCERTAIN", 6, events=1)

    def test_publication_cancellation_quarantines_before_propagating(self):
        class TargetCancellation(BaseException):
            pass

        case = Case()

        def cancelled_after_emission(result):
            case.published.append(result)
            raise TargetCancellation("TARGET cancellation after emission")

        case.adapter = case.new_adapter(publish=cancelled_after_emission)
        with self.assertRaises(TargetCancellation):
            case.call()
        self.assertTrue(case.adapter.quarantined)
        self.assertEqual(case.adapter.published_frame_seq, 1)
        self.assertEqual(len(case.published), 1)
        case.pair.frame_metadata["seq"] = 2
        case.action_id = "new-action-after-cancellation"
        case.presentation = replace(case.presentation, frame_seq=2,
                                    action_id=case.action_id)
        self.assert_refused(case, "E_UNCERTAIN", 6, events=1)

    def test_reentrant_publication_refuses_without_second_emission(self):
        case = Case()
        reentrant_refusals = []

        def reentrant_publisher(result):
            case.published.append(result)
            try:
                case.adapter.perceive(object())
            except W8UGodotError as exc:
                reentrant_refusals.append((exc.code, exc.rc))

        case.adapter = case.new_adapter(publish=reentrant_publisher)
        result = case.call()
        self.assertTrue(result.tick_bound)
        self.assertEqual(reentrant_refusals, [("E_LOCK", 5)])
        self.assertEqual(len(case.published), 1)
        self.assertEqual(case.contract.verifications.count("pair"), 1)
        self.assertEqual(case.adapter.published_frame_seq, 1)
        self.assertFalse(case.adapter.quarantined)

    def test_ram_only_disk_sink_and_process_network_spies(self):
        case = Case()
        bad = Case()
        bad.pair.frame_metadata["sha256"] = "0" * 64
        forbidden = (
            ("builtins.open", "open"), ("io.open", "io.open"),
            ("os.open", "os.open"), ("pathlib.Path.write_bytes", "write_bytes"),
            ("pathlib.Path.write_text", "write_text"),
            ("subprocess.Popen", "process"), ("os.system", "system"),
            ("socket.socket", "socket"),
        )
        spies = []
        with ExitStack() as stack:
            for target, name in forbidden:
                spies.append(stack.enter_context(mock.patch(
                    target, side_effect=AssertionError("forbidden " + name))))
            result = case.call()
            self.assert_refused(bad, "E_STALE_FRAME", 5)
        self.assertTrue(result.tick_bound)
        self.assertEqual(len(case.published), 1)
        self.assertTrue(all(spy.call_count == 0 for spy in spies))


if __name__ == "__main__":
    unittest.main()
