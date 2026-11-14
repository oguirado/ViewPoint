"""Production unit tests for MultiWindowChain and per-window PostPass integration.

Exercises production public paths in eval.v2eval.multiwin_chain and
eval.v2eval.postpass across:
- Gate G M5 3: Blink veto survives sibling interleaving (rejects global frame
  sequence mutant; positive control recognizes local consecutive blink and P2 veto).
- Gate G M5 4: Motion, equal-luminance chroma, and eager hashes preserve real
  change (rejects lazy-zero hash reuse and mask-all-motion mutants; positive
  control vetoes false blink and retains moving RGB patch).
- Gate G M5 5: Settle oracle fails after sequence gap, motion, stale frame,
  missing evidence, or regressing/missing fake time; positive control settles
  after configured consecutive valid quiet observations within max_age_ns.
- Invariants INV-M5-1, INV-M5-2, INV-M5-3, canonical frozen dataclass schemas,
  stream-lane concurrency, canonical lock ordering, and offline-only execution.

All timings in this VM environment are labeled VM (no host speed claim).
"""
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError, fields, is_dataclass
import hashlib
import socket
import subprocess
from threading import Barrier
import time
import unittest
from unittest import mock

import numpy as np

from visioncore import change, desc
from visioncore.grid import Grid
from eval.v2x import fl_blinkprior, fl_v3, gm_bands_hv
from eval.v2eval.postpass import (
    BaselineFlickerVeto,
    PostPass,
    VARIANTS,
    apply_claims,
)
from eval.v2eval.multiwin_chain import (
    ActionRequest,
    ActorLease,
    ChainConfigError,
    ChangeResult,
    DetectorInputError,
    EpochValidationError,
    FocusLease,
    FrameRef,
    GeometryValidationError,
    Handle,
    IdentityMismatchError,
    METADATA_LOCK_ORDER,
    MissingEvidenceError,
    MultiWindowChain,
    Observation,
    PerWindowDetector,
    PerceptionResult,
    Receipt,
    SUPPORTED_TARGET_KINDS,
    SequenceValidationError,
    StreamSpec,
    StreamToken,
    TargetRecord,
    Ticket,
    TimestampValidationError,
    WindowDescription,
    WindowFrames,
    acquire_target_lanes,
    canonical_actuation_resource,
    canonical_handle_key,
)


def make_solid_rgb(value: int, width: int = 16, height: int = 16) -> np.ndarray:
    return np.full((height, width, 3), int(value), dtype=np.uint8)


def make_canvas_noise_rgb(seed: int, width: int = 16, height: int = 16) -> np.ndarray:
    """Deterministic integer UI-canvas redraw generator producing chroma cancellation."""
    x = np.arange(width, dtype=np.uint32)[None, :]
    y = np.arange(height, dtype=np.uint32)[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed & 0xFFFFFFFF)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    grey = (z & np.uint32(31)).astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    grey = (grey + bands * np.uint8(23) + np.uint8(45)).astype(np.uint8)
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = grey
    rgb[:, :, 1] = grey + np.uint8(12)
    rgb[:, :, 2] = grey + np.uint8(24)
    return rgb


def make_moving_patch_pair(dx: int = 16, dy: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Create a deterministic 128x160 RGB pair with a 64x64 textured patch moving by (dx, dy)."""
    rng = np.random.default_rng(20261020)
    prev = np.full((128, 160, 3), 240, dtype=np.uint8)
    cur = prev.copy()
    patch_y = rng.integers(20, 200, size=(64, 64), dtype=np.uint8)
    patch_rgb = np.stack([patch_y, patch_y ^ np.uint8(15), patch_y], axis=-1)
    y0, x0 = 32, 32
    prev[y0:y0 + 64, x0:x0 + 64] = patch_rgb
    cur[y0 + dy:y0 + dy + 64, x0 + dx:x0 + dx + 64] = patch_rgb
    return prev, cur


def make_lazy_baseline(rgb: np.ndarray, cls: int = 0) -> np.ndarray:
    """Create S2 baseline tiles with adversarial lazy zero xh64 hashes."""
    g = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(g.pad(rgb), g, lazy_xh=True)
    tiles = tiles.copy()
    tiles["cls"] = cls
    tiles["mv_dx"] = 0
    tiles["mv_dy"] = 0
    tiles["xh64"] = 0
    return tiles


def make_observation(
    handle: Handle,
    rgb: np.ndarray,
    *,
    epoch: int = 1,
    frame_seq: int = 0,
    dpr_ppm: int = 1_000_000,
    state: dict | None = None,
) -> Observation:
    arr = np.array(rgb, dtype=np.uint8, copy=True, order="C")
    digest = hashlib.sha256(arr.tobytes(order="C")).hexdigest()
    fref = FrameRef(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        width=arr.shape[1],
        height=arr.shape[0],
        rgb=arr,
        digest=digest,
    )
    return Observation(
        handle=handle,
        epoch=epoch,
        frame_seq=frame_seq,
        dpr_ppm=dpr_ppm,
        state=dict(state) if state is not None else {},
        frame=fref,
    )


# ---------------------------------------------------------------------------
# Named Mutants for Gate Distinction Verification
# ---------------------------------------------------------------------------

class GlobalFrameSequenceMutantDetector:
    """Named Mutant for Gate G M5 3: uses a single global frame counter across handles."""

    def __init__(self):
        self._inner = PerWindowDetector()
        self._global_seq = 0

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int) -> ChangeResult:
        # BUG: ignores target-local frame_seq and substitutes global sequence
        g_seq = self._global_seq
        self._global_seq += 1
        return self._inner.update(handle, tiles, rgb, epoch=epoch, frame_seq=g_seq)

    def reset_window(self, handle):
        self._inner.reset_window(handle)


class ReuseLazyZeroHashesMutantDetector:
    """Named Mutant for Gate G M5 4: reuses incoming lazy zero hashes instead of eager RGB hashes."""

    def __init__(self):
        self._trackers = {}

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int) -> ChangeResult:
        from eval.v2x.fl_v3 import Reason, veto_flicker

        grid = Grid(rgb.shape[1], rgb.shape[0])
        key = (handle, epoch)
        if key not in self._trackers:
            self._trackers[key] = {
                "tracker": fl_v3.FlickerTracker(grid.N, max_half=64),
                "history": [],
            }
        st = self._trackers[key]
        # BUG: uses caller's lazy tiles (with xh64 == 0) instead of computing eager RGB hashes
        lazy_tiles = tiles.copy()
        y = desc.luma_of_padded(grid.pad(rgb))
        old = st["history"][-2] if len(st["history"]) == 2 else None
        recent = st["history"][-1] if st["history"] else None
        evidence = {
            "evidence": {
                "frame_ids": (frame_seq - 2, frame_seq - 1, frame_seq),
                "history_valid": np.ones(grid.N, dtype=np.bool_),
                "hash_valid": np.ones((3, grid.N), dtype=np.bool_),
            }
        }
        decision = veto_flicker(
            lazy_tiles,
            old[0] if old else None,
            recent[0] if recent else None,
            np.zeros(grid.N, dtype=np.bool_),
            (grid.s, grid.gw, grid.gh),
            y_prev2=old[1] if old else None,
            y_prev1=recent[1] if recent else None,
            y_cur=y,
            params=evidence,
        )
        st["history"] = (st["history"] + [(lazy_tiles, y)])[-2:]
        states, _ = st["tracker"].update(lazy_tiles["xh64"])
        out = lazy_tiles.copy()
        out["cls"][decision.mask] = 2
        out["cls"][states == 4] = 4
        out.flags.writeable = False
        return ChangeResult(handle=handle, epoch=epoch, settled=False, claims=out, ops={"gm": 0, "fl": 1})


class MaskAllMotionMutantDetector:
    """Named Mutant for Gate G M5 4: masks all motion claims to UNCHANGED."""

    def __init__(self):
        self._inner = PerWindowDetector()

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int) -> ChangeResult:
        res = self._inner.update(handle, tiles, rgb, epoch=epoch, frame_seq=frame_seq)
        masked = res.claims.copy()
        # BUG: zeroes out all motion claims
        is_moved = masked["cls"] == 3
        masked["cls"][is_moved] = 0
        masked["mv_dx"][:] = 0
        masked["mv_dy"][:] = 0
        masked.flags.writeable = False
        return ChangeResult(handle=handle, epoch=epoch, settled=res.settled, claims=masked, ops=res.ops)


class SettledOnMissingEvidenceMutantChain:
    """Named Mutant for Gate G M5 5: returns True when evidence or timestamp is missing."""

    def __init__(self, quiet_frames: int, max_age_ns: int):
        self._inner = MultiWindowChain(quiet_frames=quiet_frames, max_age_ns=max_age_ns)

    def update(self, observation: Observation, *, now_ns: int | None = None) -> ChangeResult:
        return self._inner.update(observation, now_ns=now_ns)

    def is_settled(self, handle: Handle, *, now_ns: int) -> bool:
        if handle not in self._inner._lanes or now_ns is None:
            return True  # BUG: treats missing evidence as settled
        return self._inner.is_settled(handle, now_ns=now_ns)


# ---------------------------------------------------------------------------
# Main Production Test Suite
# ---------------------------------------------------------------------------

class TestMultiWindowChainImplementation(unittest.TestCase):
    """Tests for MultiWindowChain and per-window PostPass integration."""

    def setUp(self):
        self._orig_open = builtins.open
        self._orig_socket = socket.socket
        self._orig_popen = subprocess.Popen

        def _guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"Forbidden disk write attempted in offline test: {file}")
            return self._orig_open(file, mode, *args, **kwargs)

        def _forbidden_network(*args, **kwargs):
            raise AssertionError("Forbidden network socket creation in offline test")

        def _forbidden_process(*args, **kwargs):
            raise AssertionError("Forbidden subprocess launch in offline test")

        self._patches = [
            mock.patch("builtins.open", side_effect=_guarded_open),
            mock.patch("socket.socket", side_effect=_forbidden_network),
            mock.patch("subprocess.Popen", side_effect=_forbidden_process),
        ]
        for p in self._patches:
            p.start()

        self.h_x11 = Handle("x11_window", "win:101", 1)
        self.h_cdp = Handle("cdp_tab", "tab:202", 0)
        self.h_xvfb = Handle("xvfb_screen", "screen:0", 2)
        self.h_cap = Handle("capsule", "cap:main", 3)
        self.h_godot = Handle("godot_window", "godot:arena", 2**70)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # -----------------------------------------------------------------------
    # Gate G M5 3: Blink veto survives sibling interleaving
    # -----------------------------------------------------------------------

    def test_gate_g_m5_3_blink_veto_and_local_blink_survive_sibling_interleaving(self):
        """Gate G M5 3 positive control: sibling interleaving preserves P2 veto, P1 A-B-A return, and local blink."""
        # 1. P2 Veto on non-returning canvas noise redraws under sibling interleaving
        f0, f1, f2 = (make_canvas_noise_rgb(seed) for seed in (0, 1, 2))
        sib_frames = [make_solid_rgb(40), make_solid_rgb(90), make_solid_rgb(140)]

        solo_chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)
        solo_results = []
        for seq, rgb in enumerate((f0, f1, f2)):
            obs = make_observation(
                self.h_x11,
                rgb,
                epoch=1,
                frame_seq=seq,
                state={"tiles": make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)},
            )
            solo_results.append(solo_chain.update(obs, now_ns=(seq + 1) * 10_000))

        # Frame 2 baseline FLICKER (4) is demoted to CHANGED (2) by P2 veto
        self.assertEqual(int(solo_results[2].claims["cls"][0]), change.C_CHANGED)

        interleaved_chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)
        int_results_x11 = []
        for seq, rgb in enumerate((f0, f1, f2)):
            obs_x11 = make_observation(
                self.h_x11,
                rgb,
                epoch=1,
                frame_seq=seq,
                state={"tiles": make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)},
            )
            int_results_x11.append(interleaved_chain.update(obs_x11, now_ns=(seq + 1) * 10_000))
            # Interleave two sibling updates on h_cdp with different cadence
            for sub in range(2):
                sib_seq = seq * 2 + sub
                obs_cdp = make_observation(
                    self.h_cdp,
                    sib_frames[sib_seq % len(sib_frames)],
                    epoch=1,
                    frame_seq=sib_seq,
                )
                interleaved_chain.update(obs_cdp, now_ns=(seq + 1) * 10_000 + sub + 1)

        for solo_r, int_r in zip(solo_results, int_results_x11):
            np.testing.assert_array_equal(int_r.claims, solo_r.claims)
            self.assertEqual(int_r.ops, solo_r.ops)
        self.assertEqual(int(int_results_x11[2].claims["cls"][0]), change.C_CHANGED)

        # 2. Exact A-B-A return (P1) and 25-frame caret blink sequence under sibling interleaving
        caret_a = make_solid_rgb(25)
        caret_b = make_solid_rgb(220)
        caret_stream = [caret_a] * 8 + [caret_b] * 8 + [caret_a] * 8 + [caret_b]

        solo_blink_chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)
        int_blink_chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)
        solo_blink_res = []
        int_blink_res = []

        for seq, rgb in enumerate(caret_stream):
            obs_a = make_observation(self.h_x11, rgb, epoch=2, frame_seq=seq)
            solo_blink_res.append(solo_blink_chain.update(obs_a, now_ns=(seq + 1) * 1_000))
            int_blink_res.append(int_blink_chain.update(obs_a, now_ns=(seq + 1) * 1_000))
            obs_b = make_observation(self.h_godot, make_solid_rgb((seq * 17) % 256), epoch=5, frame_seq=seq)
            int_blink_chain.update(obs_b, now_ns=(seq + 1) * 1_000 + 500)

        self.assertEqual(int(int_blink_res[24].claims["cls"][0]), change.C_FLICKER)
        for s_r, i_r in zip(solo_blink_res, int_blink_res):
            np.testing.assert_array_equal(i_r.claims, s_r.claims)
            self.assertEqual(i_r.ops, s_r.ops)

    def test_gate_g_m5_3_reject_mutant_global_frame_sequence(self):
        """Gate G M5 3 mutant check: global frame sequence mutant fails P2 veto under sibling interleaving."""
        f0, f1, f2 = (make_canvas_noise_rgb(seed) for seed in (0, 1, 2))
        mutant_chain = MultiWindowChain(
            quiet_frames=3,
            max_age_ns=1_000_000_000,
            detector=GlobalFrameSequenceMutantDetector(),
        )
        prod_chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)

        for seq, rgb in enumerate((f0, f1, f2)):
            obs_a = make_observation(
                self.h_x11,
                rgb,
                epoch=1,
                frame_seq=seq,
                state={"tiles": make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)},
            )
            res_mutant = mutant_chain.update(obs_a, now_ns=(seq + 1) * 1_000)
            res_prod = prod_chain.update(obs_a, now_ns=(seq + 1) * 1_000)

            obs_b = make_observation(self.h_cdp, make_solid_rgb(50 + seq), epoch=1, frame_seq=seq)
            mutant_chain.update(obs_b, now_ns=(seq + 1) * 1_000 + 100)
            prod_chain.update(obs_b, now_ns=(seq + 1) * 1_000 + 100)

        # Production demotes frame 2 baseline FLICKER (4) -> CHANGED (2)
        self.assertEqual(int(res_prod.claims["cls"][0]), change.C_CHANGED)
        # Global-sequence mutant sees non-consecutive IDs (0, 2, 4), resets veto history, and fails to demote!
        self.assertEqual(int(res_mutant.claims["cls"][0]), change.C_FLICKER)
        self.assertNotEqual(int(res_mutant.claims["cls"][0]), int(res_prod.claims["cls"][0]))

    # -----------------------------------------------------------------------
    # Gate G M5 4: Motion, chroma, and eager hashes preserve real change
    # -----------------------------------------------------------------------

    def test_gate_g_m5_4_motion_chroma_and_eager_hashes_positive_control(self):
        """Gate G M5 4 positive control: eager hashes veto chroma-cancelled blink while retaining moving RGB patch."""
        chain = MultiWindowChain(quiet_frames=3, max_age_ns=1_000_000_000)

        # 1. Chroma-cancelled non-returning canvas sequence with lazy-zero incoming baseline hashes
        f0, f1, f2 = (make_canvas_noise_rgb(seed) for seed in (0, 1, 2))
        for seq, rgb in enumerate((f0, f1, f2)):
            lazy_in = make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)
            self.assertEqual(int(lazy_in["xh64"][0]), 0)
            res_chroma = chain.update(
                make_observation(self.h_cdp, rgb, epoch=1, frame_seq=seq, state={"tiles": lazy_in}),
                now_ns=(seq + 1) * 10_000,
            )
        # False baseline FLICKER is vetoed to CHANGED (2) thanks to eager RGB hashing
        self.assertEqual(int(res_chroma.claims["cls"][0]), change.C_CHANGED)
        self.assertFalse(res_chroma.settled)

        # 2. Exact A-B-A blink control on a separate handle is preserved as FLICKER (4)
        a_rgb, b_rgb = make_solid_rgb(30), make_solid_rgb(210)
        for seq, rgb in enumerate((a_rgb, b_rgb, a_rgb)):
            res_aba = chain.update(
                make_observation(
                    self.h_xvfb,
                    rgb,
                    epoch=1,
                    frame_seq=seq,
                    state={"tiles": make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)},
                ),
                now_ns=(seq + 1) * 10_000,
            )
        self.assertEqual(int(res_aba.claims["cls"][0]), change.C_FLICKER)

        # 3. Moving RGB patch on h_godot is retained as MOVED (cls=3, mv_dx=16, mv_dy=0)
        prev_rgb, cur_rgb = make_moving_patch_pair(dx=16, dy=0)
        chain.update(
            make_observation(self.h_godot, prev_rgb, epoch=1, frame_seq=0),
            now_ns=10_000,
        )
        res_motion = chain.update(
            make_observation(
                self.h_godot,
                cur_rgb,
                epoch=1,
                frame_seq=1,
                state={"tiles": make_lazy_baseline(cur_rgb, cls=4)},
            ),
            now_ns=20_000,
        )
        moved_tiles = res_motion.claims["cls"] == change.C_MOVED
        self.assertTrue(moved_tiles.any(), "Moving RGB patch must produce MOVED claims")
        self.assertTrue(np.all(res_motion.claims["mv_dx"][moved_tiles] == 16))
        self.assertTrue(np.all(res_motion.claims["mv_dy"][moved_tiles] == 0))
        self.assertGreater(res_motion.ops["gm"], 0)
        self.assertFalse(res_motion.settled)

    def test_gate_g_m5_4_reject_mutants_lazy_zero_hashes_and_mask_all_motion(self):
        """Gate G M5 4 mutant check: reject lazy-zero hash reuse mutant and mask-all-motion mutant."""
        # Mutant 1: Reuses lazy zero hashes -> fails to veto chroma-cancelled false flicker
        f0, f1, f2 = (make_canvas_noise_rgb(seed) for seed in (0, 1, 2))
        lazy_mutant_chain = MultiWindowChain(
            quiet_frames=3,
            max_age_ns=1_000_000_000,
            detector=ReuseLazyZeroHashesMutantDetector(),
        )
        for seq, rgb in enumerate((f0, f1, f2)):
            res_lazy_mut = lazy_mutant_chain.update(
                make_observation(
                    self.h_cdp,
                    rgb,
                    epoch=1,
                    frame_seq=seq,
                    state={"tiles": make_lazy_baseline(rgb, cls=4 if seq == 2 else 2)},
                ),
                now_ns=(seq + 1) * 1_000,
            )
        # Because lazy hashes are all 0, the mutant mistakes f2 for an exact hash return to f0 and keeps cls=4!
        self.assertEqual(int(res_lazy_mut.claims["cls"][0]), change.C_FLICKER)

        # Mutant 2: Masks all motion -> loses moving RGB patch
        prev_rgb, cur_rgb = make_moving_patch_pair(dx=16, dy=0)
        mask_mutant_chain = MultiWindowChain(
            quiet_frames=3,
            max_age_ns=1_000_000_000,
            detector=MaskAllMotionMutantDetector(),
        )
        mask_mutant_chain.update(make_observation(self.h_godot, prev_rgb, epoch=1, frame_seq=0), now_ns=1_000)
        res_mask_mut = mask_mutant_chain.update(
            make_observation(self.h_godot, cur_rgb, epoch=1, frame_seq=1),
            now_ns=2_000,
        )
        self.assertFalse(np.any(res_mask_mut.claims["cls"] == change.C_MOVED))

    # -----------------------------------------------------------------------
    # Gate G M5 5: Settle fails after gap, motion, or stale frame
    # -----------------------------------------------------------------------

    def test_gate_g_m5_5_settle_oracle_positive_control_and_failure_modes(self):
        """Gate G M5 5: valid quiet run settles; gap, motion, stale age, missing evidence, or regressing time unsettle."""
        max_age = 50_000_000
        chain = MultiWindowChain(quiet_frames=3, max_age_ns=max_age)

        # 1. Missing evidence before any update is UNSETTLED (False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=1_000), False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=None), False)

        # 2. Positive control: 3 consecutive valid quiet observations settle
        static_rgb = make_solid_rgb(100)
        r1 = chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=1), now_ns=10_000_000)
        self.assertIs(r1.settled, False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=10_000_000), False)

        r2 = chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=2), now_ns=20_000_000)
        self.assertIs(r2.settled, False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=20_000_000), False)

        r3 = chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=3), now_ns=30_000_000)
        self.assertIs(r3.settled, True)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=30_000_000), True)
        # Exact age boundary (30_000_000 + 50_000_000 = 80_000_000) remains settled
        self.assertIs(chain.is_settled(self.h_x11, now_ns=80_000_000), True)

        # Also settle sibling handle h_cap so we can verify gap isolation
        for seq in (1, 2, 3):
            chain.update(make_observation(self.h_cap, static_rgb, epoch=1, frame_seq=seq), now_ns=10_000_000 * seq)
        self.assertIs(chain.is_settled(self.h_cap, now_ns=30_000_000), True)

        # 3. Stale frame fails settle (80_000_001 > 30_000_000 + 50_000_000)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=80_000_001), False)

        # 4. Regressing time fails settle in is_settled and raises in update
        self.assertIs(chain.is_settled(self.h_x11, now_ns=29_999_999), False)
        with self.assertRaises(TimestampValidationError):
            chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=4), now_ns=25_000_000)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=30_000_000), False)

        # 5. Sequence gap (jumping to frame_seq=6) resets only h_x11 and fails settle
        r_gap = chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=6), now_ns=40_000_000)
        self.assertIs(r_gap.settled, False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=40_000_000), False)
        # Sibling h_cap remains settled!
        self.assertIs(chain.is_settled(self.h_cap, now_ns=40_000_000), True)

        # 6. Recover settle after 3 new consecutive quiet frames (seq=7, 8, 9)
        chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=7), now_ns=41_000_000)
        chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=8), now_ns=42_000_000)
        r_rec = chain.update(make_observation(self.h_x11, static_rgb, epoch=1, frame_seq=9), now_ns=43_000_000)
        self.assertIs(r_rec.settled, True)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=43_000_000), True)

        # 7. Real motion / change breaks settle immediately
        changed_rgb = make_solid_rgb(200)
        r_chg = chain.update(make_observation(self.h_x11, changed_rgb, epoch=1, frame_seq=10), now_ns=44_000_000)
        self.assertIs(r_chg.settled, False)
        self.assertIs(chain.is_settled(self.h_x11, now_ns=44_000_000), False)

        # 8. Missing fake time (now_ns=None) never consults wall clock or yields settled
        with mock.patch("time.time", side_effect=AssertionError("wall clock forbidden")), \
             mock.patch("time.monotonic", side_effect=AssertionError("monotonic clock forbidden")), \
             mock.patch("time.perf_counter", side_effect=AssertionError("perf_counter forbidden")):
            for seq in (1, 2, 3, 4):
                r_notime = chain.update(
                    make_observation(self.h_xvfb, static_rgb, epoch=1, frame_seq=seq),
                    now_ns=None,
                )
                self.assertIs(r_notime.settled, False)
            self.assertIs(chain.is_settled(self.h_xvfb, now_ns=50_000_000), False)

    def test_gate_g_m5_5_reject_mutant_settled_on_missing_evidence(self):
        """Gate G M5 5 mutant check: reject mutant that returns settled=True on missing evidence."""
        mutant = SettledOnMissingEvidenceMutantChain(quiet_frames=3, max_age_ns=50_000_000)
        prod = MultiWindowChain(quiet_frames=3, max_age_ns=50_000_000)
        unknown = Handle("cdp_tab", "unseen_tab", 0)
        self.assertIs(prod.is_settled(unknown, now_ns=1_000), False)
        self.assertIs(mutant.is_settled(unknown, now_ns=1_000), True)
        self.assertNotEqual(prod.is_settled(unknown, now_ns=1_000), mutant.is_settled(unknown, now_ns=1_000))

    # -----------------------------------------------------------------------
    # INV-M5-1, INV-M5-2, INV-M5-3 & Per-Window PostPass Integration
    # -----------------------------------------------------------------------

    def test_per_window_postpass_partitioning_and_gap_isolation(self):
        """INV-M5-1: PostPass partitioned by (handle, epoch) isolates tracker, veto, prev luma, and cache."""
        grid = Grid(16, 16)
        post = PostPass(grid, accelerated=False, baseline_flicker_veto=True)

        f0, f1, f2 = (make_canvas_noise_rgb(seed) for seed in (0, 1, 2))
        t0, t1, t2 = (make_lazy_baseline(rgb, cls=4 if i == 2 else 2) for i, rgb in enumerate((f0, f1, f2)))

        # Step 0 & 1 on both h_x11 and h_cdp
        post.update(t0, f0, frame_key="f0", frame_id=0, handle=self.h_x11, epoch=1)
        post.update(t0, f0, frame_key="f0", frame_id=0, handle=self.h_cdp, epoch=1)

        post.update(t1, f1, frame_key="f1", frame_id=1, handle=self.h_x11, epoch=1)
        post.update(t1, f1, frame_key="f1", frame_id=1, handle=self.h_cdp, epoch=1)

        # Trigger a sequence gap ONLY on h_x11 (frame_id=9)
        out_x11_gap, _ = post.update(t2, f2, frame_key="f2", frame_id=9, handle=self.h_x11, epoch=1)
        # Because h_x11 had a gap, its 3-frame veto history reset and abstained (cls stays 4)
        self.assertEqual(int(out_x11_gap["combined"]["cls"][0]), change.C_FLICKER)

        # h_cdp continues at frame_id=2 without gap -> P2 veto fires and demotes cls 4 -> 2!
        out_cdp_ok, claims_cdp = post.update(t2, f2, frame_key="f2", frame_id=2, handle=self.h_cdp, epoch=1)
        self.assertEqual(int(out_cdp_ok["combined"]["cls"][0]), change.C_CHANGED)
        self.assertTrue(bool(claims_cdp["flicker"]["veto_mask"][0]))

        # Resetting h_cdp does not affect a third handle
        post.reset_window(self.h_cdp)
        with self.assertRaises(EpochValidationError):
            post.update(t0, f0, frame_id=0, handle=self.h_x11, epoch=0)

    def test_inv_m5_2_concurrent_contextvar_op_accounting_and_finally_restore(self):
        """INV-M5-2: Concurrent updates isolate GM/FL ContextVar counters and restore outer context in finally."""
        fl_blinkprior.reset_ops()
        gm_bands_hv.reset_ops()
        fl_blinkprior.FlickerTracker(1).update([17])
        outer_fl_ops = fl_blinkprior.OPS

        chain = MultiWindowChain(quiet_frames=2, max_age_ns=1_000_000_000)
        small_rgb_0 = make_solid_rgb(30, width=16, height=16)
        small_rgb_1 = make_solid_rgb(90, width=16, height=16)
        large_rgb_0, large_rgb_1 = make_moving_patch_pair(dx=16, dy=0)

        # Solo baselines
        solo_chain = MultiWindowChain(quiet_frames=2, max_age_ns=1_000_000_000)
        solo_chain.update(make_observation(self.h_x11, small_rgb_0, epoch=1, frame_seq=0), now_ns=1_000)
        solo_small = solo_chain.update(make_observation(self.h_x11, small_rgb_1, epoch=1, frame_seq=1), now_ns=2_000)

        solo_chain.update(make_observation(self.h_godot, large_rgb_0, epoch=1, frame_seq=0), now_ns=1_000)
        solo_large = solo_chain.update(make_observation(self.h_godot, large_rgb_1, epoch=1, frame_seq=1), now_ns=2_000)

        # Concurrent execution on shared MultiWindowChain
        chain.update(make_observation(self.h_x11, small_rgb_0, epoch=1, frame_seq=0), now_ns=1_000)
        chain.update(make_observation(self.h_godot, large_rgb_0, epoch=1, frame_seq=0), now_ns=1_000)

        barrier = Barrier(2)

        def run_small():
            barrier.wait(timeout=5)
            return chain.update(make_observation(self.h_x11, small_rgb_1, epoch=1, frame_seq=1), now_ns=2_000)

        def run_large():
            barrier.wait(timeout=5)
            return chain.update(make_observation(self.h_godot, large_rgb_1, epoch=1, frame_seq=1), now_ns=2_000)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_s = pool.submit(copy_context().run, run_small)
            f_l = pool.submit(copy_context().run, run_large)
            conc_small = f_s.result()
            conc_large = f_l.result()

        self.assertEqual(conc_small.ops, solo_small.ops)
        self.assertEqual(conc_large.ops, solo_large.ops)
        np.testing.assert_array_equal(conc_small.claims, solo_small.claims)
        np.testing.assert_array_equal(conc_large.claims, solo_large.claims)
        self.assertEqual(fl_blinkprior.OPS, outer_fl_ops)

    # -----------------------------------------------------------------------
    # Canonical Frozen Dataclasses, Validation & Resource Addressing
    # -----------------------------------------------------------------------

    def test_canonical_frozen_dataclasses_and_field_orders(self):
        """Verify all 15 canonical frozen dataclasses have exact field orders and enforce immutability."""
        expected = [
            (Handle, ("kind", "canonical_id", "generation")),
            (TargetRecord, ("handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed")),
            (FrameRef, ("handle", "epoch", "frame_seq", "width", "height", "rgb", "digest")),
            (Observation, ("handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame")),
            (Receipt, ("action_id", "handle", "status", "reason")),
            (ActionRequest, ("handle", "action_id", "epoch", "payload", "cost", "deadline_ns")),
            (ActorLease, ("handle", "owner", "generation", "nonce", "expires_ns")),
            (FocusLease, ("lease", "domain", "epoch")),
            (StreamSpec, ("width", "height", "slots", "quota_bytes")),
            (StreamToken, ("handle", "epoch")),
            (Ticket, ("action_id", "handle")),
            (ChangeResult, ("handle", "epoch", "settled", "claims", "ops")),
            (PerceptionResult, ("handle", "observation", "error")),
            (WindowFrames, ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm")),
            (WindowDescription, ("handle", "epoch", "frame_seqs", "descriptors", "regions", "error")),
        ]
        for cls, exp_fields in expected:
            with self.subTest(dataclass=cls.__name__):
                self.assertTrue(is_dataclass(cls))
                self.assertEqual(tuple(f.name for f in fields(cls)), exp_fields)

        # Verify frozen immutability and RGB read-only enforcement
        raw_rgb = make_solid_rgb(128)
        fref = FrameRef(self.h_x11, 1, 0, 16, 16, raw_rgb, "sha256:test")
        self.assertFalse(fref.rgb.flags.writeable)
        with self.assertRaises(FrozenInstanceError):
            fref.epoch = 2
        with self.assertRaises(ValueError):
            fref.rgb[0, 0, 0] = 0

        # WindowFrames validation
        wf = WindowFrames(self.h_x11, 1, (fref,), (0,), 1_000_000)
        self.assertEqual(wf.frame_seqs, (0,))
        with self.assertRaises(SequenceValidationError):
            WindowFrames(self.h_x11, 1, (fref, fref), (0, 0), 1_000_000)
        with self.assertRaises(DetectorInputError):
            WindowFrames(self.h_x11, 1, (fref,), (0, 1), 1_000_000)

        # WindowDescription validation
        wd = WindowDescription(self.h_x11, 1, (0,), (np.zeros(1),), ((),), None)
        self.assertIsNone(wd.error)

    def test_admission_validation_and_canonical_actuation_resource(self):
        """Verify typed ValueError subclasses on invalid inputs and capsule/screen canonical resource sharing."""
        for kind in SUPPORTED_TARGET_KINDS:
            h = Handle(kind, "dest_1", 0)
            self.assertEqual(h.kind, kind)

        with self.assertRaises(DetectorInputError):
            Handle("invalid_kind", "dest_1", 0)
        with self.assertRaises(DetectorInputError):
            Handle("cdp_tab", "", 0)
        with self.assertRaises(DetectorInputError):
            Handle("cdp_tab", "dest_1", -1)
        with self.assertRaises(DetectorInputError):
            Handle("cdp_tab", "dest_1", True)

        # Capsule and child screen addressing the same destination share canonical actuation resource
        cap = Handle("capsule", "capsule:workspace_7", 1)
        child_screen = Handle("xvfb_screen", "workspace_7/screen", 1)
        other_tab = Handle("cdp_tab", "workspace_7", 1)
        self.assertEqual(
            MultiWindowChain.canonical_actuation_resource(cap),
            MultiWindowChain.canonical_actuation_resource(child_screen),
        )
        self.assertNotEqual(
            MultiWindowChain.canonical_actuation_resource(cap),
            MultiWindowChain.canonical_actuation_resource(other_tab),
        )

        # Canonical lock ordering and target lane acquisition
        self.assertEqual(
            METADATA_LOCK_ORDER,
            (
                "scheduler_or_fence",
                "registry",
                "budget",
                "target_actor_metadata",
                "focus_domain",
                "stream_bank",
                "per_stream_metadata",
            ),
        )
        chain = MultiWindowChain(quiet_frames=2, max_age_ns=100_000)
        for h in (self.h_godot, self.h_cdp, self.h_x11):
            chain._get_lane(h)
        with acquire_target_lanes(chain._lanes, [self.h_godot, self.h_cdp, self.h_x11]) as ordered:
            self.assertEqual(ordered, sorted([self.h_godot, self.h_cdp, self.h_x11], key=canonical_handle_key))


if __name__ == "__main__":
    unittest.main()
