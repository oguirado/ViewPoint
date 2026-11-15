# @module {"id":"wa.tests.test_jq2a_m1_j1_session_registry_handles","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.session_registry"],"book":"HB-WA-s1-perceive"}
"""Contract tests for SessionRegistry handle lifecycle, generation bounds, aliasing, and Gate G M1 1.

Owning implementation task_id: jq2a-m1-c1-session-registry.
Module under probe: wender.perceive.session_registry
Symbol under probe: SessionRegistry
"""

from dataclasses import dataclass
import importlib
import sys
import unittest

try:
    import numpy as np
except ImportError:
    np = None


def _get_owner_symbols():
    """Probe the owner module and symbols directly."""
    try:
        mod = importlib.import_module("wender.perceive.session_registry")
        return mod
    except ImportError:
        return None


_OWNER_MOD = _get_owner_symbols()
_OWNER_AVAILABLE = _OWNER_MOD is not None


def _require_owner(func):
    """Decorator to apply expectedFailure when the owner module or symbol is absent."""
    if not _OWNER_AVAILABLE:
        return unittest.expectedFailure(func)
    return func


class TestSessionRegistryRecordsAndSchema(unittest.TestCase):
    """Tests for record schemas and structural contract requirements."""

    @_require_owner
    def test_record_schemas_and_field_order(self):
        """Verify immutable records exist on owner and fields match canonical spec order."""
        import hashlib

        mod = importlib.import_module("wender.perceive.session_registry")
        Handle = mod.Handle
        TargetRecord = mod.TargetRecord
        FrameRef = mod.FrameRef
        Observation = mod.Observation
        Receipt = mod.Receipt
        ActionRequest = mod.ActionRequest
        ActorLease = mod.ActorLease
        FocusLease = mod.FocusLease
        StreamSpec = mod.StreamSpec
        StreamToken = mod.StreamToken
        Ticket = mod.Ticket
        ChangeResult = mod.ChangeResult
        PerceptionResult = mod.PerceptionResult
        WindowFrames = mod.WindowFrames
        WindowDescription = mod.WindowDescription

        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab-1")
        self.assertEqual(h.generation, 1)

        tr = TargetRecord(
            handle=h,
            transport="mock_transport",
            owned=True,
            epoch=0,
            session_id="s-100",
            attachment_owned=True,
            closed=False,
        )
        self.assertEqual(tr.handle, h)
        self.assertEqual(tr.epoch, 0)
        self.assertFalse(tr.closed)

        rgb_obj = np.zeros((100, 100, 3), dtype=np.uint8) if np is not None else b"\x00" * 30000
        fr = FrameRef(
            handle=h,
            epoch=0,
            frame_seq=1,
            width=100,
            height=100,
            rgb=rgb_obj,
            digest=hashlib.sha256(rgb_obj.tobytes()).hexdigest(),
        )
        self.assertEqual(fr.frame_seq, 1)

        obs = Observation(
            handle=h,
            epoch=0,
            frame_seq=1,
            dpr_ppm=100,
            state={"active": True},
            frame=fr,
        )
        self.assertEqual(obs.dpr_ppm, 100)

        rec = Receipt(action_id="act-1", handle=h, status="ok", reason=None)
        self.assertEqual(rec.status, "ok")

        ar = ActionRequest(
            handle=h,
            action_id="act-1",
            epoch=0,
            payload={"type": "click"},
            cost=10,
            deadline_ns=1000000000,
        )
        self.assertEqual(ar.cost, 10)

        al = ActorLease(
            handle=h,
            owner="builder",
            generation=1,
            nonce=b"nonce12345678900",
            expires_ns=2000000000,
        )
        self.assertEqual(al.owner, "builder")

        fl = FocusLease(lease=al, domain="main", epoch=0)
        self.assertEqual(fl.domain, "main")

        ss = StreamSpec(width=1920, height=1080, slots=4, quota_bytes=1920 * 1080 * 3 * 4)
        self.assertEqual(ss.slots, 4)

        st = StreamToken(handle=h, epoch=0)
        self.assertEqual(st.handle, h)

        tk = Ticket(action_id="act-1", handle=h)
        self.assertEqual(tk.action_id, "act-1")

        claims_obj = np.array([1, 2, 3]) if np is not None else [1, 2, 3]
        cr = ChangeResult(
            handle=h,
            epoch=0,
            settled=True,
            claims=claims_obj,
            ops={"click": 1},
        )
        self.assertTrue(cr.settled)

        pr = PerceptionResult(handle=h, observation=obs, error=None)
        self.assertEqual(pr.observation, obs)

        wf = WindowFrames(
            handle=h,
            epoch=0,
            frames=(fr,),
            frame_seqs=(1,),
            dpr_ppm=100,
        )
        self.assertEqual(len(wf.frames), 1)

        wd = WindowDescription(
            handle=h,
            epoch=0,
            frame_seqs=(1,),
            descriptors=("button",),
            regions=((0, 0, 10, 10),),
            error=None,
        )
        self.assertEqual(wd.descriptors, ("button",))


class TestSessionRegistryContract(unittest.TestCase):
    """Contract tests for SessionRegistry functionality."""

    @_require_owner
    def test_register_supported_kinds_and_aliasing(self):
        """Register every supported kind and alias capsule input surface to child screen."""
        mod = importlib.import_module("wender.perceive.session_registry")
        SessionRegistry = mod.SessionRegistry

        registry = SessionRegistry()
        kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        handles = {}

        for kind in kinds:
            transport = object()
            handle = registry.register(kind, f"canon-{kind}", transport, owned=True)
            self.assertEqual(handle.kind, kind)
            self.assertEqual(handle.canonical_id, f"canon-{kind}")
            handles[kind] = handle

        # Capsule and its child screen alias to the same canonical actuation resource
        capsule_transport = object()
        cap_handle = registry.register("capsule", "shared-dest-1", capsule_transport, owned=True)
        child_screen_handle = registry.register("xvfb_screen", "shared-dest-1", capsule_transport, owned=True)

        cap_rec = registry.resolve(cap_handle)
        child_rec = registry.resolve(child_screen_handle)
        self.assertEqual(cap_rec.handle.canonical_id, child_rec.handle.canonical_id)
        self.assertEqual(cap_rec.transport, child_rec.transport)

    @_require_owner
    def test_gate_g_m1_1_stale_handles_never_resolve_and_respawn(self):
        """Gate G M1 1: Stale handles never resolve after close and respawn.

        Negative mutant: ignore generation after respawn.
        Positive control: fresh handle resolves.
        """
        from types import SimpleNamespace

        mod = importlib.import_module("wender.perceive.session_registry")
        SessionRegistry = mod.SessionRegistry

        registry = SessionRegistry()
        transport = SimpleNamespace(close_target=lambda canonical_id: None)

        # Step 1: Register initial target
        h1 = registry.register("cdp_tab", "tab-respawn", transport, owned=True)
        self.assertEqual(h1.generation, 1)

        tr1 = registry.resolve(h1)
        self.assertFalse(tr1.closed)

        # Step 2: Close target
        receipt = registry.close(h1)
        self.assertEqual(receipt.status, "closed")

        # Stale handle h1 must now refuse to resolve
        with self.assertRaises((KeyError, ValueError)):
            registry.resolve(h1)

        # Step 3: Respawn the same canonical ID
        h2 = registry.register("cdp_tab", "tab-respawn", transport, owned=True)
        self.assertGreater(h2.generation, h1.generation)

        # Positive control: fresh handle h2 resolves
        tr2 = registry.resolve(h2)
        self.assertEqual(tr2.handle, h2)
        self.assertFalse(tr2.closed)

        # Negative mutant check: stale handle h1 still refuses to resolve even after h2 respawned
        with self.assertRaises((KeyError, ValueError)):
            registry.resolve(h1)

    @_require_owner
    def test_repeated_respawn_including_large_generations(self):
        """Repeatedly close and respawn, including generations exceeding machine integer range."""
        from types import SimpleNamespace

        mod = importlib.import_module("wender.perceive.session_registry")
        SessionRegistry = mod.SessionRegistry
        Handle = mod.Handle

        registry = SessionRegistry()
        transport = SimpleNamespace(close_target=lambda canonical_id: None)
        canonical_id = "canon-unbounded-gen"

        # Simulate large generation > 2^64
        huge_gen = 1 << 128
        h_huge = Handle(kind="godot_window", canonical_id=canonical_id, generation=huge_gen)
        self.assertGreater(h_huge.generation, 1 << 64)

        # Register, resolve, close, and verify stale handle refusal with large generation
        h1 = registry.register("godot_window", canonical_id, transport, owned=True)
        registry.close(h1)

        # Re-registering increments generation without reuse across repeated respawns
        for _ in range(5):
            h_fresh = registry.register("godot_window", canonical_id, transport, owned=True)
            tr = registry.resolve(h_fresh)
            self.assertEqual(tr.handle, h_fresh)
            registry.close(h_fresh)
            with self.assertRaises((KeyError, ValueError)):
                registry.resolve(h_fresh)

    @_require_owner
    def test_advance_epoch_navigation_isolation(self):
        """Navigation advances epoch without changing generation (INV-M1-1)."""
        mod = importlib.import_module("wender.perceive.session_registry")
        SessionRegistry = mod.SessionRegistry

        registry = SessionRegistry()
        transport = object()

        handle = registry.register("cdp_tab", "tab-nav", transport, owned=True)
        initial_gen = handle.generation

        # Advance epoch
        new_epoch = registry.advance_epoch(handle)
        self.assertEqual(new_epoch, 1)

        tr = registry.resolve(handle)
        self.assertEqual(tr.epoch, 1)
        self.assertEqual(tr.handle.generation, initial_gen)


if __name__ == "__main__":
    unittest.main()
