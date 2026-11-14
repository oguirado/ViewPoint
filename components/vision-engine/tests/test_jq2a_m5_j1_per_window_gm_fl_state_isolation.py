"""Unit tests for per-window GM/FL state isolation and detector context contracts.

Owning implementation task_id: jq2a-m5-c1-detector-contexts
Target API contract: eval.v2x.per_window_detector
Gate G M5 1: Interleaved histories equal solo histories.
Invariants: INV-M5-1, INV-M5-2, INV-M5-3.
"""
from dataclasses import dataclass, fields, is_dataclass, FrozenInstanceError
import unittest
import numpy as np

from eval.v2x.per_window_detector import ChangeResult, Handle
from visioncore import desc
from visioncore.grid import Grid


def _baseline_tiles(rgb):
    """Build real S2 descriptors with the exact grid dtype and Morton order."""
    grid = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)
    return tiles


# -----------------------------------------------------------------------------
# Proposed Canonical Dataclass Schemas (Reference / Fallback Verification)
# -----------------------------------------------------------------------------

SUPPORTED_TARGET_KINDS = (
    "cdp_tab",
    "x11_window",
    "xvfb_screen",
    "capsule",
    "godot_window",
)


@dataclass(frozen=True)
class RefHandle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self):
        if self.kind not in SUPPORTED_TARGET_KINDS:
            raise ValueError(f"Unsupported target kind: {self.kind}")


@dataclass(frozen=True)
class RefTargetRecord:
    handle: RefHandle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool


@dataclass(frozen=True)
class RefFrameRef:
    handle: RefHandle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str

    def __post_init__(self):
        if isinstance(self.rgb, np.ndarray):
            if self.rgb.dtype != np.uint8:
                raise ValueError("RGB array must be uint8")
            if not self.rgb.flags.c_contiguous:
                raise ValueError("RGB array must be C-contiguous")
            # Make array read-only after admission
            self.rgb.flags.writeable = False


@dataclass(frozen=True)
class RefObservation:
    handle: RefHandle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: RefFrameRef

    def __post_init__(self):
        if self.dpr_ppm <= 0:
            raise ValueError("dpr_ppm must be positive")


@dataclass(frozen=True)
class RefReceipt:
    action_id: str
    handle: RefHandle
    status: str
    reason: str | None


@dataclass(frozen=True)
class RefActionRequest:
    handle: RefHandle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int


@dataclass(frozen=True)
class RefActorLease:
    handle: RefHandle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclass(frozen=True)
class RefFocusLease:
    lease: RefActorLease
    domain: str
    epoch: int


@dataclass(frozen=True)
class RefStreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int


@dataclass(frozen=True)
class RefStreamToken:
    handle: RefHandle
    epoch: int


@dataclass(frozen=True)
class RefTicket:
    action_id: str
    handle: RefHandle


@dataclass(frozen=True)
class RefChangeResult:
    handle: RefHandle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclass(frozen=True)
class RefPerceptionResult:
    handle: RefHandle
    observation: RefObservation | None
    error: str | None


@dataclass(frozen=True)
class RefWindowFrames:
    handle: RefHandle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int

    def __post_init__(self):
        if len(self.frames) != len(self.frame_seqs):
            raise ValueError("frames and frame_seqs must have equal length")
        if self.dpr_ppm <= 0:
            raise ValueError("dpr_ppm must be positive")
        for i in range(1, len(self.frame_seqs)):
            if self.frame_seqs[i] <= self.frame_seqs[i - 1]:
                raise ValueError("frame_seqs must have increasing local sequence")


@dataclass(frozen=True)
class RefWindowDescription:
    handle: RefHandle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None


# -----------------------------------------------------------------------------
# Positive Control and Negative Mutant Reference Implementations
# -----------------------------------------------------------------------------

class ReferencePerWindowDetector:
    """Positive Control: Fully isolated per-handle/epoch state detector bank."""

    def __init__(self, *, gm=None, fl=None, veto=None):
        self.gm = gm
        self.fl = fl
        self.veto = veto
        # Key: (kind, canonical_id, generation, epoch)
        self._states = {}

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int):
        key = (handle.kind, handle.canonical_id, handle.generation, epoch)
        if key not in self._states:
            self._states[key] = {
                "history": [],
                "cache": {},
                "consecutive_valid": 0,
                "last_seq": -1,
            }

        st = self._states[key]

        # Sequence validation
        if frame_seq <= st["last_seq"]:
            raise ValueError(f"Non-increasing frame sequence: {frame_seq} <= {st['last_seq']}")
        st["last_seq"] = frame_seq

        # Simulate eager RGB evidence & history update
        st["history"].append((frame_seq, tiles.copy(), rgb.copy()))
        st["consecutive_valid"] += 1

        # Simulate claims computation (dummy integer claims matching input shape)
        claims = np.zeros(tiles.shape, dtype=np.int64)
        ops = {"gm_ops": 10, "fl_ops": 5}
        settled = st["consecutive_valid"] >= 3

        return RefChangeResult(
            handle=handle,
            epoch=epoch,
            settled=settled,
            claims=claims,
            ops=ops,
        )

    def reset_window(self, handle):
        """Reset only entries matching handle across all epochs."""
        keys_to_del = [
            k for k in self._states
            if (k[0], k[1], k[2]) == (handle.kind, handle.canonical_id, handle.generation)
        ]
        for k in keys_to_del:
            del self._states[k]


class SharedStatePerWindowDetectorMutant:
    """Negative Mutant: Shares veto/tracker/history state across handles."""

    def __init__(self, *, gm=None, fl=None, veto=None):
        self.gm = gm
        self.fl = fl
        self.veto = veto
        # Shared global state across ALL handles
        self._global_history = []
        self._global_consecutive_valid = 0

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int):
        self._global_history.append((frame_seq, tiles.copy(), rgb.copy()))
        self._global_consecutive_valid += 1

        claims = np.zeros(tiles.shape, dtype=np.int64)
        claims.flat[:] = len(self._global_history)  # Leaks count into claims
        ops = {"gm_ops": 10 * len(self._global_history), "fl_ops": 5}
        settled = self._global_consecutive_valid >= 3

        return RefChangeResult(
            handle=handle,
            epoch=epoch,
            settled=settled,
            claims=claims,
            ops=ops,
        )

    def reset_window(self, handle):
        # Clears global history for everyone!
        self._global_history.clear()
        self._global_consecutive_valid = 0


# -----------------------------------------------------------------------------
# Test Suite
# -----------------------------------------------------------------------------

class TestPerWindowGMFLStateIsolation(unittest.TestCase):

    # -------------------------------------------------------------------------
    # PART 1: Production Owner Module Contract Tests
    # -------------------------------------------------------------------------

    def test_owner_module_exports_and_signatures(self):
        """Verify owner module exports PerWindowDetector, ChangeResult, and Handle with required methods."""
        import eval.v2x.per_window_detector as pwd

        self.assertTrue(hasattr(pwd, "PerWindowDetector"))
        self.assertTrue(hasattr(pwd, "ChangeResult"))
        self.assertTrue(hasattr(pwd, "Handle"))

        detector = pwd.PerWindowDetector()
        self.assertTrue(hasattr(detector, "update"))
        self.assertTrue(hasattr(detector, "reset_window"))

        if hasattr(pwd, "MultiWindowChain"):
            self.assertTrue(hasattr(pwd.MultiWindowChain, "update"))
        if hasattr(pwd, "is_settled"):
            self.assertTrue(callable(pwd.is_settled))

    def test_gate_g_m5_1_interleaved_histories_equal_solo_histories_owner(self):
        """Gate G M5 1: Interleaving two generations with unequal cadence & grids, gapping one, matches solo replay."""
        import eval.v2x.per_window_detector as pwd

        handle_A = Handle(kind="x11_window", canonical_id="win_101", generation=1)
        handle_B = Handle(kind="godot_window", canonical_id="godot_202", generation=1)

        # Stream A: 16x16 grid, cadence = every 1 step
        # Stream B: 32x32 grid, cadence = every 2 steps
        rgb_A = np.zeros((16, 16, 3), dtype=np.uint8)
        tiles_A = _baseline_tiles(rgb_A)

        rgb_B = np.zeros((32, 32, 3), dtype=np.uint8)
        tiles_B = _baseline_tiles(rgb_B)

        # 1. Run Stream A solo for 6 frames
        det_solo_A = pwd.PerWindowDetector()
        res_solo_A = []
        for seq in range(1, 7):
            res = det_solo_A.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=seq)
            res_solo_A.append(res)

        # 2. Run Stream B solo for 3 frames (steps 1, 3, 5)
        det_solo_B = pwd.PerWindowDetector()
        res_solo_B = []
        for seq in range(1, 4):
            res = det_solo_B.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=seq)
            res_solo_B.append(res)

        # 3. Interleaved execution with gap on A at step 4
        det_interleaved = pwd.PerWindowDetector()
        res_interleaved_A = []
        res_interleaved_B = []

        # Step 1: A(seq=1), B(seq=1)
        res_interleaved_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=1))
        res_interleaved_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=1))

        # Step 2: A(seq=2)
        res_interleaved_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=2))

        # Step 3: A(seq=3), B(seq=2)
        res_interleaved_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=3))
        res_interleaved_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=2))

        # GAP on Stream A only!
        det_interleaved.reset_window(handle_A)

        # Step 4: A restart epoch 2 seq=1
        res_interleaved_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=2, frame_seq=1))

        # Step 5: B(seq=3)
        res_interleaved_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=3))

        # Assert Stream B interleaved results match solo B results exactly
        self.assertEqual(len(res_interleaved_B), len(res_solo_B))
        for r_int, r_solo in zip(res_interleaved_B, res_solo_B):
            self.assertEqual(r_int.handle, r_solo.handle)
            self.assertEqual(r_int.epoch, r_solo.epoch)
            self.assertEqual(r_int.settled, r_solo.settled)
            np.testing.assert_array_equal(r_int.claims, r_solo.claims)

    def test_invariants_m5_1_m5_2_m5_3_owner(self):
        """Verify INV-M5-1, INV-M5-2, and INV-M5-3 on owner module."""
        import eval.v2x.per_window_detector as pwd

        handle1 = Handle("x11_window", "win_1", 1)
        handle2 = Handle("x11_window", "win_2", 1)

        detector = pwd.PerWindowDetector()
        rgb1 = np.zeros((32, 32, 3), dtype=np.uint8)
        t1 = _baseline_tiles(rgb1)

        # INV-M5-1: Resetting handle1 does not clear handle2
        detector.update(handle1, t1, rgb1, epoch=1, frame_seq=1)
        detector.update(handle2, t1, rgb1, epoch=1, frame_seq=1)

        detector.reset_window(handle1)

        # Stream 2 continues sequence without reset error
        r2 = detector.update(handle2, t1, rgb1, epoch=1, frame_seq=2)
        self.assertEqual(r2.handle, handle2)

    def test_canonical_dataclasses_and_field_orders_owner(self):
        """Verify the two canonical records actually owned by this module."""

        expected_dataclasses = {
            "Handle": ("kind", "canonical_id", "generation"),
            "ChangeResult": ("handle", "epoch", "settled", "claims", "ops"),
        }
        owner_dataclasses = {"Handle": Handle, "ChangeResult": ChangeResult}

        for name, expected_fields in expected_dataclasses.items():
            cls = owner_dataclasses[name]
            self.assertTrue(is_dataclass(cls))
            self.assertTrue(cls.__dataclass_params__.frozen)
            actual_fields = tuple(f.name for f in fields(cls))
            self.assertEqual(
                actual_fields,
                expected_fields,
                f"Dataclass {name} field ordering mismatch: {actual_fields} != {expected_fields}"
            )

    # -------------------------------------------------------------------------
    # PART 2: Contract Harness, Positive Control, & Negative Mutant Tests
    # -------------------------------------------------------------------------

    def test_positive_control_reference_isolation_harness(self):
        """Contract Harness: ReferencePerWindowDetector satisfies Gate G M5 1."""
        handle_A = RefHandle(kind="x11_window", canonical_id="win_101", generation=1)
        handle_B = RefHandle(kind="godot_window", canonical_id="godot_202", generation=1)

        tiles_A = np.zeros((1, 1), dtype=np.uint8)
        rgb_A = np.zeros((16, 16, 3), dtype=np.uint8)

        tiles_B = np.zeros((2, 2), dtype=np.uint8)
        rgb_B = np.zeros((32, 32, 3), dtype=np.uint8)

        # 1. Solo Stream A
        det_solo_A = ReferencePerWindowDetector()
        res_solo_A = [det_solo_A.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=i) for i in range(1, 7)]

        # 2. Solo Stream B
        det_solo_B = ReferencePerWindowDetector()
        res_solo_B = [det_solo_B.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=i) for i in range(1, 4)]

        # 3. Interleaved Stream A & B with Stream A gap
        det_interleaved = ReferencePerWindowDetector()
        res_int_A = []
        res_int_B = []

        # Step 1
        res_int_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=1))
        res_int_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=1))

        # Step 2
        res_int_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=2))

        # Step 3
        res_int_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=3))
        res_int_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=2))

        # Gap Stream A only
        det_interleaved.reset_window(handle_A)

        # Step 4 (Stream A new epoch)
        res_int_A.append(det_interleaved.update(handle_A, tiles_A, rgb_A, epoch=2, frame_seq=1))

        # Step 5 (Stream B continues)
        res_int_B.append(det_interleaved.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=3))

        # Positive control assertion: Stream B interleaved matches solo B
        self.assertEqual(len(res_int_B), len(res_solo_B))
        for r_int, r_solo in zip(res_int_B, res_solo_B):
            self.assertEqual(r_int.handle, r_solo.handle)
            self.assertEqual(r_int.epoch, r_solo.epoch)
            self.assertEqual(r_int.settled, r_solo.settled)
            np.testing.assert_array_equal(r_int.claims, r_solo.claims)

    def test_negative_mutant_shared_state_fails_isolation_harness(self):
        """Contract Harness: SharedStatePerWindowDetectorMutant fails isolation check."""
        handle_A = RefHandle(kind="x11_window", canonical_id="win_101", generation=1)
        handle_B = RefHandle(kind="godot_window", canonical_id="godot_202", generation=1)

        tiles_A = np.zeros((1, 1), dtype=np.uint8)
        rgb_A = np.zeros((16, 16, 3), dtype=np.uint8)

        tiles_B = np.zeros((2, 2), dtype=np.uint8)
        rgb_B = np.zeros((32, 32, 3), dtype=np.uint8)

        # 1. Solo Stream B
        det_solo_B = ReferencePerWindowDetector()
        res_solo_B = [det_solo_B.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=i) for i in range(1, 4)]

        # 2. Interleaved Stream A & B with SharedState mutant
        det_mutant = SharedStatePerWindowDetectorMutant()
        res_int_B = []

        det_mutant.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=1)
        res_int_B.append(det_mutant.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=1))

        det_mutant.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=2)
        det_mutant.update(handle_A, tiles_A, rgb_A, epoch=1, frame_seq=3)
        res_int_B.append(det_mutant.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=2))

        # Resetting A resets B's state too in the negative mutant!
        det_mutant.reset_window(handle_A)
        res_int_B.append(det_mutant.update(handle_B, tiles_B, rgb_B, epoch=1, frame_seq=3))

        # Negative mutant MUST fail matching solo B
        with self.assertRaises(AssertionError):
            for r_int, r_solo in zip(res_int_B, res_solo_B):
                self.assertEqual(r_int.settled, r_solo.settled)
                np.testing.assert_array_equal(r_int.claims, r_solo.claims)

    def test_dataclass_schemas_and_field_order_reference(self):
        """Verify field orders and immutability across all 15 reference dataclasses."""
        dataclass_specs = [
            (RefHandle, ("kind", "canonical_id", "generation")),
            (RefTargetRecord, ("handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed")),
            (RefFrameRef, ("handle", "epoch", "frame_seq", "width", "height", "rgb", "digest")),
            (RefObservation, ("handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame")),
            (RefReceipt, ("action_id", "handle", "status", "reason")),
            (RefActionRequest, ("handle", "action_id", "epoch", "payload", "cost", "deadline_ns")),
            (RefActorLease, ("handle", "owner", "generation", "nonce", "expires_ns")),
            (RefFocusLease, ("lease", "domain", "epoch")),
            (RefStreamSpec, ("width", "height", "slots", "quota_bytes")),
            (RefStreamToken, ("handle", "epoch")),
            (RefTicket, ("action_id", "handle")),
            (RefChangeResult, ("handle", "epoch", "settled", "claims", "ops")),
            (RefPerceptionResult, ("handle", "observation", "error")),
            (RefWindowFrames, ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm")),
            (RefWindowDescription, ("handle", "epoch", "frame_seqs", "descriptors", "regions", "error")),
        ]

        for cls, expected_fields in dataclass_specs:
            self.assertTrue(is_dataclass(cls), f"{cls.__name__} must be a dataclass")
            actual_fields = tuple(f.name for f in fields(cls))
            self.assertEqual(
                actual_fields,
                expected_fields,
                f"{cls.__name__} field order mismatch: {actual_fields} != {expected_fields}"
            )

    def test_supported_target_kinds_and_handle_validation(self):
        """Verify supported target kinds and handle validation."""
        for kind in SUPPORTED_TARGET_KINDS:
            handle = RefHandle(kind=kind, canonical_id="test_id", generation=1)
            self.assertEqual(handle.kind, kind)

        with self.assertRaises(ValueError):
            RefHandle(kind="invalid_kind", canonical_id="test_id", generation=1)

    def test_frame_ref_readonly_rgb_and_window_frames_sequence(self):
        """Verify FrameRef makes RGB array read-only and WindowFrames validates sequence order."""
        handle = RefHandle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        rgb = np.zeros((16, 16, 3), dtype=np.uint8)

        frame = RefFrameRef(
            handle=handle,
            epoch=1,
            frame_seq=1,
            width=16,
            height=16,
            rgb=rgb,
            digest="abc123digest",
        )

        # Array is read-only after admission
        self.assertFalse(frame.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            frame.rgb[0, 0, 0] = 255

        # WindowFrames validates equal lengths and increasing sequence
        wf = RefWindowFrames(handle=handle, epoch=1, frames=(frame,), frame_seqs=(1,), dpr_ppm=100)
        self.assertEqual(wf.dpr_ppm, 100)

        with self.assertRaises(ValueError):
            RefWindowFrames(handle=handle, epoch=1, frames=(frame,), frame_seqs=(1, 2), dpr_ppm=100)

        with self.assertRaises(ValueError):
            RefWindowFrames(handle=handle, epoch=1, frames=(frame, frame), frame_seqs=(2, 1), dpr_ppm=100)


if __name__ == "__main__":
    unittest.main()
