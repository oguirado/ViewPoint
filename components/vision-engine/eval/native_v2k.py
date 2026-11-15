"""Shared loader and VM benchmark CLI for the vqa_native_v2k PyO3 extension."""

import argparse
import importlib.util
import os
from pathlib import Path
import statistics
import sys
import time
import numpy as np

MODULE_NAME = "vqa_native_v2k"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SO_PATH = (
    REPO_ROOT / "rust" / "vqa_native_v2k" / "target" / "release" / "libvqa_native_v2k.so"
)

_CACHE = {}


def resolve_so_path(so_path=None):
    """Return the Path to libvqa_native_v2k.so, honoring VQA_NATIVE_V2K_SO."""
    if so_path is not None:
        return Path(so_path)
    env_so = os.environ.get("VQA_NATIVE_V2K_SO")
    if env_so:
        return Path(env_so)
    return DEFAULT_SO_PATH


def load_native_v2k(so_path=None, *, force_reload=False):
    """Load libvqa_native_v2k.so under module name vqa_native_v2k via importlib.

    Raises ImportError naming the resolved path when the shared object is absent.
    """
    target = resolve_so_path(so_path)
    if not target.is_file():
        raise ImportError(f"vqa_native_v2k shared library not found at '{target}'")

    cache_key = str(target.resolve())
    if not force_reload and cache_key in _CACHE and MODULE_NAME in sys.modules:
        return _CACHE[cache_key]

    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(target))
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Unable to construct module spec for {MODULE_NAME} from '{target}'"
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    _CACHE[cache_key] = module
    return module


load = load_native_v2k
get_native = load_native_v2k
get_module = load_native_v2k


def st_proto8_subtile_motion(*args, **kwargs):
    return load_native_v2k().st_proto8_subtile_motion(*args, **kwargs)


def st_block4_subtile_motion(*args, **kwargs):
    return load_native_v2k().st_block4_subtile_motion(*args, **kwargs)


def st_block4_batch(*args, **kwargs):
    return load_native_v2k().st_block4_batch(*args, **kwargs)


def gm_estimate(*args, **kwargs):
    return load_native_v2k().gm_estimate(*args, **kwargs)


def gm_consensus(*args, **kwargs):
    return load_native_v2k().gm_consensus(*args, **kwargs)


if os.environ.get("VQA_NATIVE_V2K_SO") or DEFAULT_SO_PATH.is_file():
    load_native_v2k()


def _make_shifted_1080p_pair(seed=20261019, dx=3, dy=-2):
    """Build an in-memory 1920x1080 synthetic shifted pair and its cls-2 tile coordinates."""
    from visioncore.layout import grid_params

    w, h = 1920, 1080
    s, gw, gh = grid_params(w, h)
    rng = np.random.default_rng(seed)

    # Low-entropy background (luma 200) with textured foreground blocks inside active tiles
    prev = np.full((h, w), 200, dtype=np.uint8)
    cur = np.full((h, w), 200, dtype=np.uint8)

    cls2_tiles = []
    # Place 24 moving textured patches across interior tiles to exercise cls-2 subtile search
    patch_tiles = [
        (tx, ty)
        for ty in range(4, gh - 4, 5)
        for tx in range(6, gw - 6, 12)
    ]
    for tx, ty in patch_tiles:
        x0 = tx * s + 4
        y0 = ty * s + 4
        pw, ph = 16, 16
        patch = rng.integers(10, 190, size=(ph, pw), dtype=np.uint8)
        prev[y0 : y0 + ph, x0 : x0 + pw] = patch
        cur[y0 + dy : y0 + dy + ph, x0 + dx : x0 + dx + pw] = patch
        cls2_tiles.append((tx, ty))

    # Full-frame textured shifted pair for global motion
    gm_prev = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
    gm_cur = np.pad(
        gm_prev,
        ((max(0, dy), max(0, -dy)), (max(0, dx), max(0, -dx))),
        mode="edge",
    )
    y_start = max(0, -dy)
    x_start = max(0, -dx)
    gm_cur = np.ascontiguousarray(gm_cur[y_start : y_start + h, x_start : x_start + w])

    return prev, cur, s, cls2_tiles, gm_prev, gm_cur


def run_benchmark(iters=5):
    """Run 1920x1080 benchmark comparing native vs reference across cls-2 tiles (tagged VM)."""
    from eval import v2proto_subtile
    from eval.v2x import st_block4, st_block4_nat, st_proto8_nat

    prev, cur, s, cls2_tiles, _, _ = _make_shifted_1080p_pair()
    max_d = s - 1

    def time_median_ms(fn, runs):
        samples = []
        for _ in range(runs):
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000.0)
        return statistics.median(samples)

    # Warm-up
    for xy in cls2_tiles[:2]:
        st_proto8_nat.subtile_motion(prev, cur, s, xy, max_d)
        st_block4_nat.subtile_motion(prev, cur, s, xy, max_d)

    nat_st8_ms = time_median_ms(
        lambda: [st_proto8_nat.subtile_motion(prev, cur, s, xy, max_d) for xy in cls2_tiles],
        iters,
    )
    ref_st8_ms = time_median_ms(
        lambda: [v2proto_subtile.subtile_motion(prev, cur, s, xy, max_d) for xy in cls2_tiles],
        iters,
    )

    nat_stb4_ms = time_median_ms(
        lambda: [st_block4_nat.subtile_motion(prev, cur, s, xy, max_d) for xy in cls2_tiles],
        iters,
    )
    nat_stb4_batch_ms = time_median_ms(
        lambda: st_block4_batch(prev, cur, s, cls2_tiles, max_d),
        iters,
    )
    ref_stb4_ms = time_median_ms(
        lambda: [st_block4.subtile_motion(prev, cur, s, xy, max_d) for xy in cls2_tiles],
        iters,
    )

    print(
        f"[VM] frame=1920x1080 cls2_tiles={len(cls2_tiles)} max_d={max_d} "
        f"st_proto8 native_median_ms={nat_st8_ms:.3f} ref_median_ms={ref_st8_ms:.3f} tag=VM"
    )
    print(
        f"[VM] frame=1920x1080 cls2_tiles={len(cls2_tiles)} max_d={max_d} "
        f"st_block4 native_median_ms={nat_stb4_ms:.3f} batch_median_ms={nat_stb4_batch_ms:.3f} "
        f"ref_median_ms={ref_stb4_ms:.3f} tag=VM"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="vqa_native_v2k loader and benchmark CLI")
    parser.add_argument(
        "--bench",
        action="store_true",
        help="Run 1920x1080 synthetic shifted pair benchmark (tagged VM)",
    )
    parser.add_argument(
        "--iters",
        type=int,
        default=5,
        help="Number of benchmark iterations (default: 5)",
    )
    args = parser.parse_args(argv)
    if args.bench:
        run_benchmark(iters=args.iters)
    else:
        mod = load_native_v2k()
        print(f"Loaded {MODULE_NAME} from {mod.__file__}")


if __name__ == "__main__":
    main()
