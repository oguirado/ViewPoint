# @module {"id":"wa.tests.test_jq2a_m3_j2_perceive_v_multiwindow_fusion","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter","wa.perceive.vision_bridge","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Contract tests for perceive-v multiwindow fusion isolation and per-target AX namespace bounds.

Gate G M3 2: Multiwindow AX evidence fusion never attaches sibling AX evidence.
Sibling windows with identical AX node IDs and overlapping visual boxes are scoped
strictly by matching Handle and Epoch.
"""

from dataclasses import dataclass
import importlib
import inspect
import sys
import numpy as np
import unittest

from wender.perceive.vision_adapter import (
    FakeVisionEngine,
    Frame,
    fuse_regions,
)
from wender.perceive import vision_bridge
from wender.state import schema


# Probe owner modules/symbols directly
try:
    from wender.perceive.session_registry import (
        Handle,
        TargetRecord,
        FrameRef,
        Observation,
        Receipt,
        ActionRequest,
        ActorLease,
        FocusLease,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
        WindowFrames,
        WindowDescription,
    )
except ImportError:
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: object
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


def make_ram_frame(width, height, bg=(0, 0, 0), patches=()):
    """Create in-memory RGB24 Frame without disk I/O."""
    buf = bytearray(bytes(bg) * (width * height))
    stride = width * 3
    for px, py, pw, ph, rgb in patches:
        row_bytes = bytes(rgb) * pw
        for y in range(py, py + ph):
            start = y * stride + px * 3
            buf[start:start + pw * 3] = row_bytes
    return Frame(width, height, bytes(buf))


def make_sibling_state(handle, epoch, node_id="0000000000000001", box=(0, 0, 20, 20), dpr_ppm=1000000):
    """Create a schema-valid state dictionary for a window target."""
    nodes = [
        {"id": node_id, "role": "button", "name": "Sibling Target Button", "depth": 0, "ordinal": 0, "attrs": ()}
    ]
    boxes = {node_id: list(box)}
    return schema.build_state(
        nodes,
        kind="cdp",
        url="https://example.test/sibling",
        title="Sibling Window",
        ax_sha256="1" * 64,
        ax_stable_sha256="2" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=1,
        boxes=boxes,
        viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": dpr_ppm},
        actionable_roles={"button"},
    )


# Helper functions to check missing owner features
def _fuse_regions_has_handle_epoch_kwargs():
    sig = inspect.signature(fuse_regions)
    return "handle" in sig.parameters and "epoch" in sig.parameters


def _vision_bridge_has_screen_identities():
    return hasattr(vision_bridge, "screen_identities")


def _has_parallel_perceive():
    try:
        importlib.import_module("wender.perceive.parallel_perceive")
        return True
    except ImportError:
        return False


def cond_expected_failure(condition):
    """Apply @unittest.expectedFailure if condition is True."""
    if condition:
        return unittest.expectedFailure
    return lambda func: func


class TestJq2aM3J2PerceiveVMultiwindowFusion(unittest.TestCase):
    """Test suite for jq2a-m3-j2-perceive-v-multiwindow-fusion."""

    def setUp(self):
        self.h_win1 = Handle(kind="cdp_tab", canonical_id="win_01", generation=1)
        self.h_win2 = Handle(kind="cdp_tab", canonical_id="win_02", generation=1)
        self.epoch_1 = 100
        self.epoch_2 = 200

        self.frame_base = make_ram_frame(32, 32, bg=(10, 10, 10))
        self.frame_patch = make_ram_frame(
            32, 32, bg=(10, 10, 10), patches=[(0, 0, 16, 16, (200, 200, 200))]
        )
        self.engine = FakeVisionEngine(tile=16, threshold=8)

    # --- Healthy Baseline Controls (Always passing) ---

    def test_healthy_control_record_dataclass_field_orders(self):
        """Verify field names and frozen immutability of shared record fallback / canonical classes."""
        h = Handle("cdp_tab", "tab_123", 1)
        self.assertEqual((h.kind, h.canonical_id, h.generation), ("cdp_tab", "tab_123", 1))
        with self.assertRaises((TypeError, AttributeError)):
            h.generation = 2

        fref = FrameRef.from_rgb(h, 1, 0, np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertEqual(fref.handle, h)
        self.assertEqual(fref.epoch, 1)

    def test_healthy_control_single_target_fuse_regions_baseline(self):
        """Single-target baseline: existing fuse_regions without handle/epoch kwarg returns valid hits."""
        state = make_sibling_state(self.h_win1, self.epoch_1, node_id="0000000000000001", box=(0, 0, 20, 20))
        regions = [{"x": 0, "y": 0, "w": 10, "h": 10, "tiles": 1}]
        fused = fuse_regions(state, regions, dpr_ppm=1000000)

        self.assertEqual(len(fused["hits"]), 1)
        self.assertEqual(fused["hits"][0]["node"], "0000000000000001")
        self.assertEqual(fused["unmatched_regions"], [])

    # --- Multiwindow Fusion Isolation & Gate G M3 2 Tests ---

    @cond_expected_failure(not _fuse_regions_has_handle_epoch_kwargs())
    def test_gate_g_m3_2_multiwindow_fusion_refuses_sibling_ax_evidence(self):
        """Gate G M3 2: Fuse only within matching handle and epoch.

        Sibling windows with identical AX ids and overlapping visual boxes must NOT
        attach sibling AX evidence if the handle or epoch does not match.
        """
        # Sibling Window 1 and Sibling Window 2 have identical AX node ID "0000000000000001" and box (0,0,20,20)
        state_win1 = make_sibling_state(self.h_win1, self.epoch_1, node_id="0000000000000001", box=(0, 0, 20, 20))
        regions = [{"x": 0, "y": 0, "w": 10, "h": 10, "tiles": 1}]

        state_win1 = {"state": state_win1, "handle": self.h_win1,
                      "epoch": self.epoch_1, "dpr_ppm": 1000000}

        # 1. Positive Control: Same handle and same epoch fuses correctly
        fused_same = fuse_regions(
            state_win1, regions, dpr_ppm=1000000, handle=self.h_win1, epoch=self.epoch_1
        )
        self.assertEqual(len(fused_same["hits"]), 1)
        self.assertEqual(fused_same["hits"][0]["node"], "0000000000000001")

        # 2. Negative Mutant: Mismatched handle (Sibling Handle h_win2) must refuse evidence
        with self.assertRaises(ValueError):
            fuse_regions(
                state_win1, regions, dpr_ppm=1000000, handle=self.h_win2, epoch=self.epoch_1
            )

        # 3. Negative Mutant: Mismatched epoch (Stale Epoch 2) must refuse evidence
        with self.assertRaises(ValueError):
            fuse_regions(
                state_win1, regions, dpr_ppm=1000000, handle=self.h_win1, epoch=self.epoch_2
            )

    @cond_expected_failure(not _vision_bridge_has_screen_identities())
    def test_vision_bridge_screen_identities_multiwindow_delegation(self):
        """Test vision_bridge.screen_identities delegation to perceive_many."""
        obs1 = Observation(
            handle=self.h_win1,
            epoch=self.epoch_1,
            frame_seq=0,
            dpr_ppm=1000000,
            state={},
            frame=FrameRef.from_rgb(self.h_win1, self.epoch_1, 0, np.frombuffer(self.frame_base.data, dtype=np.uint8).reshape(32, 32, 3)),
        )
        obs2 = Observation(
            handle=self.h_win2,
            epoch=self.epoch_1,
            frame_seq=0,
            dpr_ppm=1000000,
            state={},
            frame=FrameRef.from_rgb(self.h_win2, self.epoch_1, 0, np.frombuffer(self.frame_patch.data, dtype=np.uint8).reshape(32, 32, 3)),
        )

        fn = getattr(vision_bridge, "screen_identities")
        results = fn([obs1, obs2], adapter=self.engine)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].handle, self.h_win1)
        self.assertEqual(results[1].handle, self.h_win2)

    @cond_expected_failure(not _has_parallel_perceive())
    def test_parallel_perceive_perceive_many_inv_m3_1_and_inv_m3_2(self):
        """Test perceive_many validation and input-indexed perception results."""
        pp = importlib.import_module("wender.perceive.parallel_perceive")
        perceive_many = getattr(pp, "perceive_many")

        obs1 = Observation(
            handle=self.h_win1,
            epoch=self.epoch_1,
            frame_seq=0,
            dpr_ppm=1000000,
            state={},
            frame=FrameRef.from_rgb(self.h_win1, self.epoch_1, 0, np.frombuffer(self.frame_base.data, dtype=np.uint8).reshape(32, 32, 3)),
        )

        results = perceive_many([obs1], adapter=self.engine)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].handle, self.h_win1)


if __name__ == "__main__":
    unittest.main()
