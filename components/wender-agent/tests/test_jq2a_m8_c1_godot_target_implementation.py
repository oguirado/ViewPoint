"""Offline production-path Godot target controls (no process, X, Godot or disk I/O)."""
import copy
import hashlib
import unittest
from unittest import mock
import numpy as np

from wender.perceive import godot, godot_adapter as adapter
from wender.perceive.godot_window_target import (
    GodotWindowTarget, GodotProtocolError, GodotPrerequisiteError,
    GodotMetadataObservation, Handle, FrameRef,
)
from wender.act.godot_executor import GodotExecutor
from wender.perceive.session_registry import SessionRegistry
from wender.state import schema
from tests.test_godot_t2_adapter import (
    _fixture_encode_batch, _fixture_decode_batches, _make_action_schemas,
    _make_valid_base_and_sidecar,
)


def fixture_pair(tick=12):
    """Build a canonical *real* validator fixture, then bind an explicit tick."""
    base, sidecar = _make_valid_base_and_sidecar()
    base["nodes"][0]["states"]["tick"] = str(tick)
    sidecar["base_sha256"] = hashlib.sha256(
        schema.dumps(base).encode("utf-8")).hexdigest()
    return base, sidecar


def minimal_pair(path, *, screen_id="Holdings"):
    """Independent literal hash oracle for both supported path grammars."""
    nid = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    base = {
        "schema": "wender.state/v1",
        "source": {"kind": "godot", "url": None, "title": "Offline"},
        "prune": {"name": "fixture", "version": 1},
        "ax_sha256": "a" * 64,
        "ax_stable_sha256": "b" * 64,
        "viewport": {"dpr_ppm": 1000000},
        "nodes": [{
            "id": nid, "role": "window", "name": "Root",
            "depth": 0, "ordinal": 0, "parent": None,
            "states": {"tick": "12"}, "actionable": False, "box": None,
        }],
        "actionable": [],
        "counts": {"raw": 1, "nodes": 1, "actionable": 0},
    }
    sidecar = {
        "schema": "holdings.godot.uitree/v1",
        "nodes": {nid: {"control_path": path, "screen_id": screen_id}},
        "base_sha256": hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest(),
    }
    return base, sidecar


def make_target(*, name="window-1", registries=None, context=None):
    registry = SessionRegistry()
    handle = registry.register("godot_window", name, object(), owned=False)
    chunks = []
    client = adapter.GodotT2Client(
        "/virtual/inbox", "/virtual/outbox", "fixture",
        decode_batches=_fixture_decode_batches,
        encode_batch=_fixture_encode_batch, sink=chunks.append)
    executor = GodotExecutor(
        {"principal": "offline", "channel": "t2", "slot_kind": "sandbox",
         "automation_active": False, **(context or {})},
        _make_action_schemas(), _fixture_encode_batch, chunks.append)
    regs = {"session_registry": registry, "epoch": 0, "screens": {"portfolio_overview", "Holdings"},
            "cadences": {"tick", "quarterly"},
            "prerequisite_gate": lambda h, e: True}
    if registries:
        regs.update(registries)
    return GodotWindowTarget.bind(handle, client, executor, registries=regs), chunks


class GodotTargetImplementation(unittest.TestCase):
    def test_public_compatibility_reexports_have_exact_identity(self):
        for name in (
            "node_id_for_control_path", "parse_ui_state", "validate_uitree",
            "read_outbox_batches", "write_inbox_batch", "GodotT2Client",
            "OutboxReadResult",
        ):
            self.assertIs(getattr(adapter, name), getattr(godot, name))
        self.assertIs(adapter.Handle, Handle)
        self.assertEqual(adapter.node_id_for_control_path("Root.Panel"),
                         hashlib.sha256(b"Root.Panel").hexdigest()[:16])

    def test_dotted_and_screen_rooted_slash_have_distinct_literal_goldens(self):
        target, sink = make_target(registries={"screens": {"portfolio"}})
        goldens = {
            "portfolio.table_companies": "8b0ac696871ad4b1",
            "portfolio/table_companies": "a388e17dafda554b",
        }
        self.assertEqual(len(set(goldens.values())), 2)
        for count, (path, golden) in enumerate(goldens.items(), 1):
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256(path.encode("utf-8")).hexdigest()[:16], golden)
                base, sidecar = minimal_pair(path, screen_id="portfolio")
                self.assertEqual(base["nodes"][0]["id"], golden)
                self.assertEqual(adapter.validate_uitree(base, sidecar), [])
                obs = target.perceive(_fixture_encode_batch([base, sidecar]))
                self.assertEqual(obs.handle, target.handle)
                self.assertEqual(obs.state["nodes"][0]["id"], golden)
                self.assertEqual(target.sidecar["nodes"][golden]["control_path"], path)
                self.assertEqual(target.basis_sha256, sidecar["base_sha256"])
                self.assertEqual(len(target.tick_trail), count * 2)
                self.assertEqual(len(target.receipts), 0)
                self.assertEqual(len(sink), 0)

    def test_slash_gate_refuses_wrong_root_segments_and_reassigned_identity(self):
        target, sink = make_target(registries={"screens": {"portfolio"}})
        base, sidecar = minimal_pair("portfolio/table_companies", screen_id="portfolio")
        target.perceive(_fixture_encode_batch([base, sidecar]))
        golden = base["nodes"][0]["id"]
        vectors = (
            ("market/table_companies", "control_path must be rooted at screen_id",
             "control_path must be rooted at screen_id in outbox sidecar"),
            ("portfolio//table_companies", "invalid control_path",
             "invalid control_path or screen_id in outbox sidecar"),
            ("portfolio/../table_companies", "invalid control_path",
             "invalid control_path or screen_id in outbox sidecar"),
            ("portfolio/./table_companies", "invalid control_path",
             "invalid control_path or screen_id in outbox sidecar"),
            ("portfolio/tab\x00le_companies", "invalid control_path",
             "invalid control_path or screen_id in outbox sidecar"),
            ("portfolio.table_companies", "control_path hash mismatch",
             "control_path hash mismatch in outbox sidecar"),
            ("portfolio/table_other", "control_path hash mismatch",
             "control_path hash mismatch in outbox sidecar"),
        )
        for path, direct_error, transport_error in vectors:
            with self.subTest(path=path):
                mutant = copy.deepcopy(sidecar)
                mutant["nodes"][golden]["control_path"] = path
                # Exercise the actual target gate independently of its codec.
                with self.assertRaises(GodotProtocolError) as refusal:
                    target._verify_pair(base, mutant)
                self.assertEqual(str(refusal.exception), direct_error)
                # Then exercise the real public parser/target pipeline.
                with self.assertRaises(godot.GodotError) as refusal:
                    target.perceive(_fixture_encode_batch([base, mutant]))
                self.assertEqual(str(refusal.exception), transport_error)
                self.assertEqual(len(target.tick_trail), 2)
                self.assertEqual(target.basis_sha256, sidecar["base_sha256"])
                self.assertEqual(target.sidecar, sidecar)
                self.assertEqual(target.command_seq, 1)
                self.assertEqual(len(target.receipts), 0)
                self.assertEqual(len(sink), 0)

    def test_invalid_utf8_path_refuses_before_publication(self):
        target, sink = make_target(registries={"screens": {"portfolio"}})
        base, sidecar = minimal_pair("portfolio/table_companies", screen_id="portfolio")
        nid = base["nodes"][0]["id"]
        malformed = copy.deepcopy(sidecar)
        malformed["nodes"][nid]["control_path"] = "portfolio/\ud800"
        with self.assertRaises(GodotProtocolError) as refusal:
            target._verify_pair(base, malformed)
        self.assertEqual(str(refusal.exception), "invalid control_path")
        raw = _fixture_encode_batch([base, sidecar]).replace(
            b"portfolio/table_companies", b"portfolio/\xff")
        offset = raw.index(b"\xff")
        with self.assertRaises(godot.GodotError) as refusal:
            target.perceive(raw)
        self.assertEqual(str(refusal.exception),
                         "invalid UTF-8 in batch payload: 'utf-8' codec can't decode "
                         f"byte 0xff in position {offset}: invalid start byte")
        self.assertIsNone(target.basis_sha256)
        self.assertEqual(len(target.tick_trail), 0)
        self.assertEqual(len(target.receipts), 0)
        self.assertEqual(len(sink), 0)

    def test_only_empty_validation_error_lists_admit_pairs(self):
        target, sink = make_target()
        base, sidecar = minimal_pair("Root.Panel")
        for invalid_result in (None, False, (), {}):
            for validator, message in (
                ("schema.validate_state", "base validator must return an error list"),
                ("validate_uitree", "sidecar validator must return an error list"),
            ):
                with self.subTest(validator=validator, result=invalid_result), \
                     mock.patch("wender.perceive.godot_window_target." + validator,
                                return_value=invalid_result):
                    with self.assertRaises(GodotProtocolError) as refusal:
                        target._verify_pair(base, sidecar)
                    self.assertEqual(str(refusal.exception), message)
        malformed = copy.deepcopy(base)
        malformed["nodes"] = None
        with self.assertRaises(GodotProtocolError) as refusal:
            target._verify_pair(malformed, sidecar)
        self.assertEqual(str(refusal.exception), "base invalid shape")
        self.assertEqual(len(target.tick_trail), 0)
        self.assertEqual(len(target.receipts), 0)
        self.assertEqual(len(sink), 0)

    def test_separate_base_sidecar_and_partial_bytes_are_not_accepted_early(self):
        target, _ = make_target()
        base, sidecar = fixture_pair()
        raw = _fixture_encode_batch([base])
        with self.assertRaises(GodotPrerequisiteError):
            target.perceive(raw[:7])
        self.assertEqual(target.pending_bytes, raw[:7])
        with self.assertRaises(GodotPrerequisiteError):
            target.perceive(raw[7:])
        self.assertEqual(len(target.tick_trail), 1)
        obs = target.perceive(_fixture_encode_batch([sidecar]))
        self.assertEqual(obs.state, base)
        self.assertEqual(target.committed_tick, 12)
        self.assertEqual(len(target.tick_trail), 2)
        self.assertIsNone(obs.frame)
        self.assertIsInstance(obs, GodotMetadataObservation)
        self.assertEqual(schema.validate_target_descriptor({
            "handle": obs.handle, "epoch": obs.epoch, "frame_seq": obs.frame_seq,
            "dpr_ppm": obs.dpr_ppm, "state": obs.state,
        }), [])

    def test_backpressure_complete_batches_and_oversize_protocol_error(self):
        target, _ = make_target()
        base, sidecar = fixture_pair()
        target.perceive(_fixture_encode_batch([base, sidecar] + [
            {"tick": n} for n in range(11)]))
        self.assertFalse(target.is_backpressured())
        target.perceive(_fixture_encode_batch([{"tick": n} for n in range(13)]))
        self.assertTrue(target.is_backpressured())
        self.assertEqual(len(target.tick_trail), 26)
        self.assertEqual([r["tick"] for r in target.tick_trail if "tick" in r],
                         list(range(11)) + list(range(13)))
        with self.assertRaises(GodotPrerequisiteError):
            target.dispatch({"seq": 1, "tick": 12, "cmd": "new_game",
                             "args": {"seed": 1}}, action_id="blocked")
        first = target.take_batches(1)
        self.assertEqual(len(first[0]), 13)
        self.assertFalse(target.is_backpressured())
        before = tuple(target.tick_trail)
        with self.assertRaisesRegex(GodotProtocolError, "oversized"):
            target.perceive(_fixture_encode_batch([{"tick": n} for n in range(25)]))
        self.assertEqual(tuple(target.tick_trail), before)

    def test_frame_identity_tick_and_basis_corroboration_healthy_and_mutants(self):
        base, sidecar = fixture_pair()
        sha = sidecar["base_sha256"]
        target, _ = make_target(registries={"frame_evidence": lambda f: (12, sha)})
        pixels = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        digest = hashlib.sha256(pixels.tobytes()).hexdigest()
        frame = FrameRef(target.handle, 0, 1, 3, 2, pixels, digest)
        obs = target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        self.assertTrue(target.visual_corroborated)
        self.assertFalse(obs.frame.rgb.flags.writeable)
        pixels[:] = 0
        self.assertNotEqual(int(obs.frame.rgb.sum()), 0)
        sibling = FrameRef(Handle("godot_window", "sibling", 2), 0, 2, 3, 2,
                           pixels, hashlib.sha256(pixels.tobytes()).hexdigest())
        before = tuple(target.tick_trail)
        with self.assertRaisesRegex(GodotPrerequisiteError, "stale pixel capture identity or epoch"):
            target.perceive(_fixture_encode_batch([{"tick": 12}]), frame=sibling)
        self.assertEqual(tuple(target.tick_trail), before)
        self.assertTrue(target.visual_corroborated)
        other, _ = make_target(registries={"frame_evidence": lambda f: (11, sha)})
        wrong_proof = FrameRef(other.handle, 0, 1, 3, 2, pixels,
                               hashlib.sha256(pixels.tobytes()).hexdigest())
        other.perceive(_fixture_encode_batch([base, sidecar]), frame=wrong_proof)
        self.assertFalse(other.visual_corroborated)
        stale = FrameRef(target.handle, 1, 2, 3, 2, pixels,
                         hashlib.sha256(pixels.tobytes()).hexdigest())
        with self.assertRaisesRegex(GodotPrerequisiteError, "stale pixel capture identity or epoch"):
            target.perceive(_fixture_encode_batch([{"tick": 13}]), frame=stale)
        self.assertEqual(tuple(target.tick_trail), before)
        self.assertTrue(target.visual_corroborated)

    def test_executor_public_admission_not_game_completion(self):
        base, sidecar = fixture_pair()
        target, sink = make_target(registries={
            "frame_evidence": lambda f: (12, sidecar["base_sha256"])})
        frame = FrameRef.from_rgb(target.handle, 0, 1, np.zeros((2, 3, 3), dtype=np.uint8))
        target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        cmd = {"tick": 12, "seq": 1, "cmd": "new_game", "args": {"seed": 1}}
        admitted = target.dispatch(cmd, action_id="action-1")
        self.assertEqual(admitted["status"], "admitted")
        self.assertEqual(target.command_seq, 2)
        self.assertTrue(sink)
        self.assertFalse(target.receipts)
        wrong = {"seq": 1, "requested_tick": 12, "applied_tick": 12,
                 "order_seq_id": 1, "status": "accepted", "reason": None,
                 "state_sha256": "f" * 64}
        target.perceive(_fixture_encode_batch([wrong]))
        self.assertFalse(target.receipts)
        correct = dict(wrong, state_sha256=sidecar["base_sha256"])
        target.perceive(_fixture_encode_batch([correct]))
        self.assertEqual(len(target.receipts), 1)
        self.assertEqual(target.receipts[0].action_id, "action-1")
        self.assertEqual(target.receipts[0].status, "accepted")
        target.perceive(b"", frame=FrameRef.from_rgb(
            target.handle, 0, 2, np.zeros((2, 3, 3), dtype=np.uint8)))
        bad = target.dispatch({"tick": 12, "seq": 2, "cmd": "invented",
                               "args": {}}, action_id="action-2")
        self.assertEqual(bad["reason"], "ACTION_UNAVAILABLE")
        self.assertEqual(len(target.receipts), 1)

    def test_missing_gates_and_duplicate_session_binding_fail_closed(self):
        base, sidecar = fixture_pair()
        target, _ = make_target(registries={
            "prerequisite_gate": lambda h, e: False,
            "frame_evidence": lambda f: (12, sidecar["base_sha256"])})
        frame = FrameRef.from_rgb(target.handle, 0, 1, np.zeros((2, 3, 3), dtype=np.uint8))
        target.perceive(_fixture_encode_batch([base, sidecar]), frame=frame)
        with self.assertRaisesRegex(GodotPrerequisiteError, "upstream integration prerequisites AUSENTE"):
            target.dispatch({"tick": 12, "seq": 1, "cmd": "new_game",
                             "args": {"seed": 1}}, action_id="missing-gate")
        registry = target.registries["session_registry"]
        other = registry.register("godot_window", "other", object(), owned=False)
        with self.assertRaisesRegex(GodotProtocolError, "client/executor already bound"):
            GodotWindowTarget.bind(other, target.client, target.executor,
                                   registries={"session_registry": registry, "epoch": 0})
        with self.assertRaises(GodotProtocolError):
            GodotWindowTarget.bind(Handle("cdp_tab", "tab", 2), target.client,
                                   target.executor, registries={})

    def test_explicit_bytes_do_not_touch_filesystem(self):
        target, _ = make_target()
        base, sidecar = fixture_pair()
        with mock.patch("pathlib.Path.is_file", side_effect=AssertionError("filesystem")), \
             mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("filesystem")):
            obs = target.perceive(_fixture_encode_batch([base, sidecar]))
        self.assertEqual(obs.state["source"]["kind"], "godot")


if __name__ == "__main__":
    unittest.main()
