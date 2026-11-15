import dataclasses
import hashlib
import json
import unittest
import numpy as np

from wender.perceive import ax
from wender.state import schema
from tests.test_ax import fixture


def make_valid_base_state():
    nodes = ax.prune(fixture())
    return schema.build_state(
        nodes,
        kind="cdp",
        url="file:///app",
        title="App Test",
        ax_sha256=ax.snapshot_sha(nodes),
        ax_stable_sha256=ax.snapshot_sha(nodes, False),
        prune={"name": ax.PRUNE_NAME, "version": 1},
        raw_count=8,
        boxes={nodes[2]["id"]: [10, 20, 100, 50]},
        viewport={"w": 1280, "h": 720},
        actionable_roles=ax.ACTIONABLE_ROLES,
    )


class TestTargetDescriptorHealthyControls(unittest.TestCase):
    def test_build_and_validate_all_supported_target_kinds(self):
        base_state = make_valid_base_state()
        for kind in schema.TARGET_KINDS:
            handle = schema.Handle(kind=kind, canonical_id=f"canon-{kind}-1", generation=1)
            desc = schema.build_target_descriptor(handle, epoch=1, state=base_state)
            self.assertIsInstance(desc, dict)
            self.assertEqual(sorted(desc.keys()), sorted(schema.TARGET_DESCRIPTOR_KEYS))
            self.assertEqual(desc["handle"], handle)
            self.assertEqual(desc["epoch"], 1)
            self.assertEqual(desc["frame_seq"], 0)
            self.assertEqual(desc["dpr_ppm"], 1000000)
            self.assertEqual(desc["state"], base_state)
            self.assertEqual(schema.validate_target_descriptor(desc), [])

    def test_build_with_dict_handle(self):
        base_state = make_valid_base_state()
        dict_handle = {"kind": "cdp_tab", "canonical_id": "tab-42", "generation": 1}
        desc = schema.build_target_descriptor(dict_handle, epoch=2, state=base_state, frame_seq=7, dpr_ppm=2000000)
        self.assertEqual(desc["handle"], dict_handle)
        self.assertEqual(desc["epoch"], 2)
        self.assertEqual(desc["frame_seq"], 7)
        self.assertEqual(desc["dpr_ppm"], 2000000)
        self.assertEqual(schema.validate_target_descriptor(desc), [])

    def test_canonical_json_dumps_preserves_envelope(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="x11_window", canonical_id="win-1", generation=3)
        desc = schema.build_target_descriptor(handle, epoch=0, state=base_state)
        dumped = schema.dumps(desc)
        self.assertIsInstance(dumped, str)
        self.assertEqual(dumped, schema.dumps(desc))
        loaded = json.loads(dumped)
        self.assertEqual(loaded["epoch"], 0)
        self.assertEqual(loaded["handle"]["canonical_id"], "win-1")

    def test_capsule_and_child_screen_actuation_resource(self):
        dest = "display:10"
        capsule_h = schema.Handle(kind="capsule", canonical_id=dest, generation=1)
        screen_h = schema.Handle(kind="xvfb_screen", canonical_id=dest, generation=1)
        self.assertEqual(schema.canonical_actuation_resource(capsule_h), dest)
        self.assertEqual(schema.canonical_actuation_resource(screen_h), dest)
        self.assertEqual(
            schema.canonical_actuation_resource(capsule_h),
            schema.canonical_actuation_resource(screen_h),
        )


class TestGateGM86ClosedInnerStateAndLeakedKeys(unittest.TestCase):
    def test_leaked_descriptor_keys_in_base_state_rejected(self):
        for leaked_key in ("handle", "epoch", "frame_seq", "dpr_ppm"):
            bad_state = make_valid_base_state()
            bad_state[leaked_key] = 123
            handle = schema.Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(handle, epoch=0, state=bad_state)
            env = {
                "handle": handle,
                "epoch": 0,
                "frame_seq": 0,
                "dpr_ppm": 1000000,
                "state": bad_state,
            }
            violations = schema.validate_target_descriptor(env)
            self.assertTrue(violations)
            self.assertTrue(any("top-level keys" in v for v in violations))

    def test_base_top_keys_closed_and_unchanged(self):
        expected_top_keys = (
            "actionable", "ax_sha256", "ax_stable_sha256", "counts",
            "nodes", "prune", "schema", "source", "viewport",
        )
        self.assertEqual(sorted(schema.TOP_KEYS), sorted(expected_top_keys))

    def test_extra_envelope_keys_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="godot_window", canonical_id="g-1", generation=1)
        env = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": base_state,
            "extra_metadata": "leak",
        }
        violations = schema.validate_target_descriptor(env)
        self.assertTrue(any("envelope keys" in v for v in violations))

    def test_missing_envelope_keys_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="godot_window", canonical_id="g-1", generation=1)
        for key in schema.TARGET_DESCRIPTOR_KEYS:
            env = {
                "handle": handle,
                "epoch": 0,
                "frame_seq": 0,
                "dpr_ppm": 1000000,
                "state": base_state,
            }
            del env[key]
            violations = schema.validate_target_descriptor(env)
            self.assertTrue(violations)


class TestTargetDescriptorNestedStateValidation(unittest.TestCase):
    def test_non_dict_state_rejected(self):
        handle = schema.Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        for bad_state in (None, "not_a_dict", [1, 2, 3], 42):
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(handle, epoch=0, state=bad_state)
            env = {
                "handle": handle,
                "epoch": 0,
                "frame_seq": 0,
                "dpr_ppm": 1000000,
                "state": bad_state,
            }
            violations = schema.validate_target_descriptor(env)
            self.assertTrue(any("state is not an object" in v for v in violations))

    def test_malformed_nested_state_fields_rejected(self):
        handle = schema.Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        bad_state = make_valid_base_state()
        bad_state["schema"] = "invalid.schema/v0"
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=bad_state)
        env = {
            "handle": handle,
            "epoch": 0,
            "frame_seq": 0,
            "dpr_ppm": 1000000,
            "state": bad_state,
        }
        violations = schema.validate_target_descriptor(env)
        self.assertTrue(any("schema must be" in v for v in violations))


class TestTargetDescriptorMalformedHandle(unittest.TestCase):
    def test_non_handle_types_rejected(self):
        base_state = make_valid_base_state()
        for bad_handle in (None, "cdp_tab", 123, ["cdp_tab", "tab-1", 0], False):
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(bad_handle, epoch=0, state=base_state)
            env = {
                "handle": bad_handle,
                "epoch": 0,
                "frame_seq": 0,
                "dpr_ppm": 1000000,
                "state": base_state,
            }
            violations = schema.validate_target_descriptor(env)
            self.assertTrue(any("handle must be a Handle or dict" in v for v in violations))

    def test_foreign_target_kind_rejected(self):
        base_state = make_valid_base_state()
        for foreign_kind in ("browser", "unity", "unreal", "screen", "unknown"):
            bad_dict_handle = {"kind": foreign_kind, "canonical_id": "c1", "generation": 0}
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(bad_dict_handle, epoch=0, state=base_state)
            env = {
                "handle": bad_dict_handle,
                "epoch": 0,
                "frame_seq": 0,
                "dpr_ppm": 1000000,
                "state": base_state,
            }
            violations = schema.validate_target_descriptor(env)
            self.assertTrue(any("handle.kind must be one of" in v for v in violations))

    def test_malformed_canonical_id_rejected(self):
        base_state = make_valid_base_state()
        for bad_id in ("", None, 123):
            bad_dict_handle = {"kind": "cdp_tab", "canonical_id": bad_id, "generation": 0}
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(bad_dict_handle, epoch=0, state=base_state)

    def test_malformed_generation_rejected(self):
        base_state = make_valid_base_state()
        for bad_gen in (-1, 1.5, True, False, "0"):
            bad_dict_handle = {"kind": "cdp_tab", "canonical_id": "c1", "generation": bad_gen}
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(bad_dict_handle, epoch=0, state=base_state)


class TestTargetDescriptorNumericGuards(unittest.TestCase):
    def test_negative_epoch_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=-1, state=base_state)

    def test_float_epoch_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=2.5, state=base_state)

    def test_negative_frame_seq_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=base_state, frame_seq=-1)

    def test_float_frame_seq_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=base_state, frame_seq=0.5)

    def test_nonpositive_dpr_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        for bad_dpr in (0, -100, -1):
            with self.assertRaises(schema.StateError):
                schema.build_target_descriptor(handle, epoch=0, state=base_state, dpr_ppm=bad_dpr)

    def test_float_dpr_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=base_state, dpr_ppm=1000000.0)

    def test_boolean_numerics_rejected(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=True, state=base_state)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=base_state, frame_seq=True)
        with self.assertRaises(schema.StateError):
            schema.build_target_descriptor(handle, epoch=0, state=base_state, dpr_ppm=True)


class TestCallerEpochComparison(unittest.TestCase):
    def test_callers_refuse_stale_descriptors(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        envelope = schema.build_target_descriptor(handle, epoch=3, state=base_state)

        current_registry_epoch = 4
        is_stale = envelope["epoch"] != current_registry_epoch
        self.assertTrue(is_stale)

        matching_registry_epoch = 3
        is_stale_matching = envelope["epoch"] != matching_registry_epoch
        self.assertFalse(is_stale_matching)

    def test_validate_target_descriptor_with_registry_epoch(self):
        base_state = make_valid_base_state()
        handle = schema.Handle(kind="cdp_tab", canonical_id="c1", generation=1)
        envelope = schema.build_target_descriptor(handle, epoch=3, state=base_state)

        errs_stale = schema.validate_target_descriptor(envelope, registry_epoch=4)
        self.assertTrue(any("does not match registry epoch" in e for e in errs_stale))

        errs_fresh = schema.validate_target_descriptor(envelope, registry_epoch=3)
        self.assertEqual(errs_fresh, [])


class TestProposedImmutableRecords(unittest.TestCase):
    def test_handle_frozen_and_fields(self):
        h = schema.Handle("cdp_tab", "tab-1", 1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-1")
        self.assertEqual(h.generation, 1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-1")
        self.assertEqual(h.generation, 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            h.generation = 1

    def test_target_record_frozen_and_fields(self):
        h = schema.Handle("cdp_tab", "tab-1", 1)
        tr = schema.TargetRecord(handle=h, transport="pipe", owned=True, epoch=1, session_id="s1", attachment_owned=True, closed=False)
        self.assertEqual(tr.handle, h)
        self.assertEqual(tr.epoch, 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tr.closed = True

    def test_frame_ref_rgb_immutability(self):
        h = schema.Handle("cdp_tab", "tab-1", 1)
        rgb_data = np.zeros((480, 640, 3), dtype=np.uint8)
        fref = schema.FrameRef(handle=h, epoch=1, frame_seq=0, width=640, height=480, rgb=rgb_data, digest=hashlib.sha256(rgb_data.tobytes()).hexdigest())
        self.assertFalse(fref.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            fref.rgb[0, 0, 0] = 255

    def test_observation_fields(self):
        h = schema.Handle("cdp_tab", "tab-1", 1)
        base_state = make_valid_base_state()
        rgb_data = np.zeros((100, 100, 3), dtype=np.uint8)
        fref = schema.FrameRef(handle=h, epoch=1, frame_seq=0, width=100, height=100, rgb=rgb_data, digest=hashlib.sha256(rgb_data.tobytes()).hexdigest())
        obs = schema.Observation(handle=h, epoch=1, frame_seq=0, dpr_ppm=1000000, state=base_state, frame=fref)
        self.assertEqual(obs.dpr_ppm, 1000000)

    def test_receipt_status_and_optional_reason(self):
        h = schema.Handle("cdp_tab", "tab-1", 1)
        r1 = schema.Receipt(action_id="act-1", handle=h, status="ok", reason=None)
        self.assertIsNone(r1.reason)
        r2 = schema.Receipt(action_id="act-2", handle=h, status="refused", reason="busy")
        self.assertEqual(r2.reason, "busy")

    def test_window_frames_ordering_and_length_invariants(self):
        h = schema.Handle("x11_window", "win-1", 1)
        wf = schema.WindowFrames(handle=h, epoch=1, frames=(np.zeros((2, 2, 3), dtype=np.uint8), np.zeros((2, 2, 3), dtype=np.uint8)), frame_seqs=(1, 2), dpr_ppm=1000000)
        self.assertEqual(len(wf.frames), 2)
        with self.assertRaises(ValueError):
            schema.WindowFrames(handle=h, epoch=1, frames=(np.zeros((2, 2, 3), dtype=np.uint8),), frame_seqs=(1, 2), dpr_ppm=1000000)
        with self.assertRaises(ValueError):
            schema.WindowFrames(handle=h, epoch=1, frames=(np.zeros((2, 2, 3), dtype=np.uint8), np.zeros((2, 2, 3), dtype=np.uint8)), frame_seqs=(2, 1), dpr_ppm=1000000)

    def test_window_description_invariants(self):
        h = schema.Handle("x11_window", "win-1", 1)
        wd = schema.WindowDescription(handle=h, epoch=1, frame_seqs=(0, 1), descriptors=("d0", "d1"), regions=("r0", "r1"), error=None)
        self.assertEqual(len(wd.descriptors), 2)
        with self.assertRaises(ValueError):
            schema.WindowDescription(handle=h, epoch=1, frame_seqs=(0, 1), descriptors=("d0",), regions=("r0", "r1"), error=None)
        with self.assertRaises(ValueError):
            schema.WindowDescription(handle=h, epoch=1, frame_seqs=(0, 1), descriptors=("d0", "d1"), regions=("r0",), error=None)

    def test_window_description_optional_payloads(self):
        h = schema.Handle("x11_window", "win-1", 1)
        success = schema.WindowDescription(h, 1, (0, 1), ("d0", "d1"), [], None)
        self.assertEqual(success.regions, ())
        self.assertEqual(success.descriptors, ("d0", "d1"))
        failure = schema.WindowDescription(h, 1, (0, 1), (), (), "timeout")
        self.assertEqual(failure.descriptors, ())
        self.assertEqual(failure.regions, ())
        self.assertEqual(failure.error, "timeout")
        unsequenced = schema.WindowDescription(h, 1, (), ("d0",), ("r0",), None)
        self.assertEqual(unsequenced.frame_seqs, ())
        self.assertEqual(unsequenced.descriptors, ("d0",))
        self.assertEqual(unsequenced.regions, ("r0",))

    def test_remaining_proposed_records_construction(self):
        h = schema.Handle("godot_window", "g-1", 1)
        lease = schema.ActorLease(handle=h, owner="worker-1", generation=1, nonce=b"nonce123", expires_ns=1000)
        fl = schema.FocusLease(lease=lease, domain="game", epoch=1)
        self.assertEqual(fl.domain, "game")
        spec = schema.StreamSpec(width=800, height=600, slots=4, quota_bytes=10000000)
        self.assertEqual(spec.slots, 4)
        tok = schema.StreamToken(handle=h, epoch=2)
        self.assertEqual(tok.epoch, 2)
        ticket = schema.Ticket(action_id="t-1", handle=h)
        self.assertEqual(ticket.action_id, "t-1")
        req = schema.ActionRequest(handle=h, action_id="a-1", epoch=1, payload={"cmd": "click"}, cost=10, deadline_ns=5000)
        self.assertEqual(req.cost, 10)
        cr = schema.ChangeResult(handle=h, epoch=1, settled=True, claims=np.zeros((2, 2)), ops={"draw": 1})
        self.assertTrue(cr.settled)
        pr = schema.PerceptionResult(handle=h, observation=None, error="timeout")
        self.assertEqual(pr.error, "timeout")

    def test_metadata_lock_order_ordering(self):
        expected_order = (
            "scheduler_or_fence", "registry", "budget",
            "target_actor_metadata", "focus_domain", "stream_bank",
            "per_stream_metadata",
        )
        self.assertEqual(schema.METADATA_LOCK_ORDER, expected_order)


if __name__ == "__main__":
    unittest.main()
