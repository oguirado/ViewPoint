"""Drop-in truth-bench runner entry point that persists per-tile classes (tile_cls.jsonl).

Usage:
  python3 -m blender_visual_qa.truth.run_bench_tc --out <dir> [same arguments as run_bench]

Wraps run_bench.persist_unit so that before evidence is copied each unit's vision/
and vision_reanalysis/ directories gain tile_cls.jsonl (compact text, never a frame),
then delegates to run_bench.main.
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from . import run_bench, tile_classes  # noqa: E402

_ORIG_PERSIST_UNIT = run_bench.persist_unit


def _vision_dirs(unit_dir):
    """Yield vision/ and vision_reanalysis/ directories under unit_dir that hold tiles.bin + frames.jsonl."""
    base = Path(unit_dir)
    if not base.is_dir() or base.is_symlink():
        return
    vdir = base / "vision"
    if vdir.is_dir() and not vdir.is_symlink():
        if (vdir / "tiles.bin").is_file() and (vdir / "frames.jsonl").is_file():
            yield vdir
    re_root = base / "vision_reanalysis"
    if re_root.is_dir() and not re_root.is_symlink():
        if (re_root / "tiles.bin").is_file() and (re_root / "frames.jsonl").is_file():
            yield re_root
        for sub in sorted(re_root.iterdir()):
            if sub.is_dir() and not sub.is_symlink():
                if (sub / "tiles.bin").is_file() and (sub / "frames.jsonl").is_file():
                    yield sub


def persist_unit(src, dst):
    """Write tile_cls.jsonl into each unit vision and vision_reanalysis dir, then copy unit evidence."""
    src_path, dst_path = Path(src), Path(dst)
    if not src_path.is_dir() or src_path.resolve() == dst_path.resolve():
        return 0
    for vdir in _vision_dirs(src_path):
        tile_classes.write_tile_classes(vdir)
    return _ORIG_PERSIST_UNIT(src_path, dst_path)


run_bench.persist_unit = persist_unit


def main(argv=None):
    run_bench.persist_unit = persist_unit
    return run_bench.main(argv)


if __name__ == "__main__":
    sys.exit(main())
