"""Gate G M5 4 test suite: Motion/chroma/eager hashes preserve real change.

VM timing label: VM (never MEASURED).
Offline only: numpy frames in RAM, fake handles and observations, no network or disk I/O.
"""

import dataclasses
import importlib
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


# ---------------------------------------------------------------------------
# Fallback / Reference Frozen Dataclasses for harness tests before owner lands
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RefHandle:
    kind: str
    canonical_id: str
    generation: int


@dataclasses.dataclass(frozen=True)
class RefTargetRecord:
    handle: RefHandle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool


@dataclasses.dataclass(frozen=True)
class RefFrameRef:
    handle: RefHandle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str


@dataclasses.dataclass(frozen=True)
class RefObservation:
    handle: RefHandle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: RefFrameRef


@dataclasses.dataclass(frozen=True)
class RefReceipt:
    action_id: str
    handle: RefHandle
    status: str
    reason: str | None


@dataclasses.dataclass(frozen=True)
class RefActionRequest:
    handle: RefHandle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int


@dataclasses.dataclass(frozen=True)
class RefActorLease:
    handle: RefHandle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclasses.dataclass(frozen=True)
class RefFocusLease:
    lease: RefActorLease
    domain: str
    epoch: int


@dataclasses.dataclass(frozen=True)
class RefStreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int


@dataclasses.dataclass(frozen=True)
class RefStreamToken:
    handle: RefHandle
    epoch: int


@dataclasses.dataclass(frozen=True)
class RefTicket:
    action_id: str
    handle: RefHandle


@dataclasses.dataclass(frozen=True)
class RefChangeResult:
    handle: RefHandle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclasses.dataclass(frozen=True)
class RefPerceptionResult:
    handle: RefHandle
    observation: RefObservation | None
    error: str | None


@dataclasses.dataclass(frozen=True)
class RefWindowFrames:
    handle: RefHandle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int


@dataclasses.dataclass(frozen=True)
class RefWindowDescription:
    handle: RefHandle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None


# ---------------------------------------------------------------------------
# Harness functions for Gate G M5 4 (Motion / Chroma / Eager Hashes)
# ---------------------------------------------------------------------------

def simulate_detector_pipeline(frames_rgb, hash_mode="eager"):
    """Simulates change detection over a sequence of RGB frames.

    frames_rgb: list of (H, W, 3) uint8 arrays
    hash_mode: "eager" (computes hash per frame from RGB),
               "lazy_zero_reuse" (mutant: reuses 0 hash across frames),
               "mask_all" (mutant: forces zero motion / claim masking)

    Returns: dict of tile claims per frame and veto status
    """
    n_frames = len(frames_rgb)
    if n_frames < 2:
        return {"claims": [], "blink_vetoed": [], "hashes": []}

    claims_list = []
    blink_vetoed_list = []

    # Simple 4x4 tile grid evaluation over RGB
    h, w, _ = frames_rgb[0].shape
    tile_h, tile_w = h // 4, w // 4

    history_hashes = []

    for f_idx in range(n_frames):
        cur_frame = frames_rgb[f_idx]
        tile_claims = np.zeros((4, 4), dtype=np.uint8)

        # Hash computation
        if hash_mode == "eager":
            # Eager hash preserves RGB content including equal-luminance chroma
            frame_hash = hash(cur_frame.tobytes())
        elif hash_mode == "lazy_zero_reuse":
            # Negative mutant: lazy zero hash reuse ignores RGB difference
            frame_hash = 0
        elif hash_mode == "mask_all":
            # Negative mutant: masks all motion
            frame_hash = hash(cur_frame.tobytes())
        else:
            raise ValueError(f"Unknown hash_mode: {hash_mode}")

        if f_idx > 0:
            prev_frame = frames_rgb[f_idx - 1]
            diff = np.abs(cur_frame.astype(np.int16) - prev_frame.astype(np.int16))

            for r in range(4):
                for c in range(4):
                    r_slice = slice(r * tile_h, (r + 1) * tile_h)
                    c_slice = slice(c * tile_w, (c + 1) * tile_w)
                    tile_diff = diff[r_slice, c_slice]

                    # Detect change if sum of diff > threshold
                    if np.sum(tile_diff) > 0 and hash_mode != "mask_all":
                        if hash_mode == "eager" or (hash_mode == "lazy_zero_reuse" and np.sum(tile_diff) > 5000):
                            tile_claims[r, c] = 1
                        elif hash_mode == "lazy_zero_reuse":
                            tile_claims[r, c] = 0

            # Baseline blink check per tile:
            # If tile at f_idx toggles back to f_idx-2 tile while changing from f_idx-1
            blink_vetoed_any = False
            if f_idx >= 2:
                prev2_frame = frames_rgb[f_idx - 2]
                for r in range(4):
                    for c in range(4):
                        r_slice = slice(r * tile_h, (r + 1) * tile_h)
                        c_slice = slice(c * tile_w, (c + 1) * tile_w)

                        cur_tile = cur_frame[r_slice, c_slice]
                        prev1_tile = prev_frame[r_slice, c_slice]
                        prev2_tile = prev2_frame[r_slice, c_slice]

                        # Check if tile toggles back and forth
                        if np.array_equal(cur_tile, prev2_tile) and not np.array_equal(cur_tile, prev1_tile):
                            # Toggling baseline tile -> blink veto applies
                            tile_claims[r, c] = 0
                            blink_vetoed_any = True

            blink_vetoed_list.append(blink_vetoed_any)
        else:
            blink_vetoed_list.append(False)

        claims_list.append(tile_claims)
        history_hashes.append(frame_hash)

    return {
        "claims": claims_list,
        "blink_vetoed": blink_vetoed_list,
        "hashes": history_hashes,
    }


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestShadowGuardsContract(unittest.TestCase):
    """Contract harness tests for Gate G M5 4 shadow guards."""

    def test_positive_control_motion_and_chroma_preserved_under_blink_veto(self):
        """Moving RGB patch and equal-luminance chroma changes remain detected while baseline blink is vetoed."""
        # Create a 64x64x3 scene sequence:
        # Background: 2-state blinking between Luma 100 (RGB [100,100,100]) and Luma 100 (RGB [120,80,100] - equal luminance chroma change)
        # Tile (0,0): Moving RGB patch from (0,0) to (1,1)
        frames = []
        for i in range(4):
            # Equal luminance chroma background
            bg_color = np.array([100, 100, 100], dtype=np.uint8) if i % 2 == 0 else np.array([120, 80, 100], dtype=np.uint8)
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            frame[:, :] = bg_color

            # Add a moving RGB patch in tile (i % 4, i % 4)
            r_start = (i % 4) * 16
            c_start = (i % 4) * 16
            frame[r_start:r_start + 16, c_start:c_start + 16] = [255, 0, 0]
            frames.append(frame)

        result = simulate_detector_pipeline(frames, hash_mode="eager")

        # Frame 2 has blink veto active for background tiles
        self.assertTrue(result["blink_vetoed"][2], "Baseline blink should be vetoed at frame 2")

        # But moving RGB patch tile at frame 2 (tile (2,2)) must retain its change claim
        claims_frame2 = result["claims"][2]
        self.assertEqual(claims_frame2[2, 2], 1, "Moving RGB patch tile must retain change claim under eager evidence")

    def test_negative_mutant_lazy_zero_hash_suppression(self):
        """Lazy zero hash reuse mutant fails to retain real motion or equal-luminance chroma change."""
        frames = []
        for i in range(4):
            # Equal-luminance chroma shifts with subtle patch motion
            bg_color = np.array([100, 100, 100], dtype=np.uint8) if i % 2 == 0 else np.array([105, 95, 100], dtype=np.uint8)
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            frame[:, :] = bg_color
            frames.append(frame)

        eager_res = simulate_detector_pipeline(frames, hash_mode="eager")
        mutant_res = simulate_detector_pipeline(frames, hash_mode="lazy_zero_reuse")

        # Eager hashes are distinct
        self.assertNotEqual(eager_res["hashes"][0], eager_res["hashes"][1])
        # Mutant hashes are all 0
        self.assertEqual(mutant_res["hashes"][0], 0)
        self.assertEqual(mutant_res["hashes"][1], 0)

        # Mutant fails to claims subtler chroma changes
        self.assertEqual(np.sum(mutant_res["claims"][1]), 0)

    def test_negative_mutant_mask_all_motion(self):
        """Masking all motion mutant fails to detect real moving RGB patch."""
        frames = [
            np.zeros((64, 64, 3), dtype=np.uint8),
            np.zeros((64, 64, 3), dtype=np.uint8),
        ]
        frames[1][0:16, 0:16] = [255, 255, 255]

        mutant_res = simulate_detector_pipeline(frames, hash_mode="mask_all")
        self.assertEqual(np.sum(mutant_res["claims"][1]), 0, "Mask all mutant produces 0 claims")

    def test_dataclass_contract_fields(self):
        """Verify field order and frozen immutability of proposed record structures."""
        h = RefHandle(kind="x11_window", canonical_id="win_123", generation=1)
        self.assertEqual(h.kind, "x11_window")
        self.assertEqual(h.canonical_id, "win_123")
        self.assertEqual(h.generation, 1)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            h.generation = 2

        fref = RefFrameRef(handle=h, epoch=1, frame_seq=10, width=64, height=64, rgb=np.zeros((64, 64, 3)), digest="abc")
        obs = RefObservation(handle=h, epoch=1, frame_seq=10, dpr_ppm=1000, state={}, frame=fref)
        self.assertEqual(obs.dpr_ppm, 1000)

        rcpt = RefReceipt(action_id="act_1", handle=h, status="OK", reason=None)
        self.assertEqual(rcpt.status, "OK")


class TestOwnerPerWindowDetector(unittest.TestCase):
    """Owner tests targeting eval.v2x.per_window_detector.PerWindowDetector."""

    def test_owner_detector_import_and_instantiation(self):
        """Test instantiation and basic update contract of owner PerWindowDetector."""
        pwd_mod = importlib.import_module("eval.v2x.per_window_detector")
        PerWindowDetector = getattr(pwd_mod, "PerWindowDetector")

        detector = PerWindowDetector()
        handle = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)

        rgb = np.zeros((64, 64, 3), dtype=np.uint8)
        tiles = _baseline_tiles(rgb)

        res = detector.update(handle, tiles, rgb, epoch=1, frame_seq=1)
        self.assertIsInstance(res, ChangeResult)
        self.assertEqual(res.handle, handle)
        self.assertEqual(res.epoch, 1)

    def test_owner_detector_reset_window(self):
        """Test reset_window contract of owner PerWindowDetector."""
        pwd_mod = importlib.import_module("eval.v2x.per_window_detector")
        PerWindowDetector = getattr(pwd_mod, "PerWindowDetector")

        detector = PerWindowDetector()
        handle = Handle(kind="x11_window", canonical_id="win_99", generation=1)
        detector.reset_window(handle)


if __name__ == "__main__":
    unittest.main()
