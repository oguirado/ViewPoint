# @module {"id":"vc.pipeline","layer":4,"stage":"L4","owner":"ARCH","deps":["vc.canon","vc.change","vc.decode","vc.desc","vc.errors","vc.frame","vc.gop","vc.grid","vc.layout","vc.p","vc.parallel","vc.s4"],"inputs":"FrameSource,params,hints","outputs":"VisionRun, vision/ files","invariants":["integer-only","deterministic","refusal-leaves-out_dir"],"anti_uses":["per-engine branches","clock"],"oracle":"tests/test_pipeline.py","book":"HB-VC-a0-arch"}
"""@module vc.pipeline L4 - analyze(): S0-S4 per frame, optional S5 (gop) when present; writes the vision/ dir (SPEC_ARCH §3)."""
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np

from . import API_VERSION, canon, change, decode, desc, frame as fr, layout, params as P, regions as rg
from .errors import VqaError
from .grid import Grid

FILES = ("tiles.bin", "frames.jsonl", "regions.jsonl", "links.jsonl", "vision.json")
ROLE_FLAG = {"ui": 1 << 4, "actor": 1 << 5}  # UI_OCC b4, ACTOR_OCC b5 (hints may set only these)


def frame_number(f):
    """Frame number in every id = driver render_index when carried, else the dense index i (SPEC_TIME hypothesis)."""
    return f.i if f.render_index is None else f.render_index


SHM_ROOT = Path("/dev/shm")


def shm_run_dir(run_id, root=SHM_ROOT):  # @anchor:vc.pipeline.shm_run_dir
    """Producer contract for the raw frame ring: <root>/vqa_<run_id>/ holds frames.manifest.json (storage rgb24_raw,
    raw_path "frames.rgb24") + frames.rgb24 (uint8[count,H,W,3], C-contiguous). Readers mmap it read-only; the
    engine never writes there. run_id: [a-z0-9_]{1,48}."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", run_id):
        raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "bad shm run id"})
    return Path(root) / ("vqa_" + run_id)


def open_shm_run(run_id, root=SHM_ROOT):  # @anchor:vc.pipeline.open_shm_run
    """ManifestSource over the /dev/shm raw ring of run_id (storage must be rgb24_raw)."""
    src = ManifestSource(shm_run_dir(run_id, root) / "frames.manifest.json")
    if src.meta["storage"] != "rgb24_raw":
        raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "shm run must use rgb24_raw storage"})
    return src


class ManifestSource:  # @anchor:vc.pipeline.manifest_source
    """FrameSource over a vqa.frame/v1 manifest file (png_dir or rgb24_raw storage)."""

    def __init__(self, manifest_path):
        self.path = Path(manifest_path)
        self.meta = {k: v for k, v in fr.read_manifest(self.path).items() if k != "frames"}
        self._rows = fr.read_manifest(self.path)["frames"]
        self._raw = None
        self.raw_array = None  # uint8[count,H,W,3] read-only memmap for rgb24_raw storage (parallel.frames_array)
        if self.meta["storage"] == "rgb24_raw":
            self._raw = self.raw_array = decode.rgb24_raw(self.path.parent / self.meta["raw_path"], self.meta["width"],
                                                          self.meta["height"], self.meta["count"])

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        W, H = self.meta["width"], self.meta["height"]
        for r in self._rows:
            if self._raw is not None:
                rgb = np.ascontiguousarray(self._raw[r["i"]])
            else:
                p = self.path.parent / r["file"]
                try:
                    rgb = decode.png(p.read_bytes())
                except OSError as e:
                    raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]})
            if rgb.shape != (H, W, 3):
                raise VqaError("E_VC_FRAME_SIZE", {"i": r["i"], "shape": list(rgb.shape)})
            yield fr.Frame(r["i"], r["t_us"], r.get("render_index"), rgb, r["sha256"])


class VisionRun:  # @anchor:vc.pipeline.vision_run
    def __init__(self, grid, meta, table, frames_sha, frame_nums):
        self.grid, self.meta, self.params = grid, meta, table
        self.params_sha256 = P.params_sha256(table)
        self.frames_sha256 = frames_sha
        self.frame_nums = frame_nums
        self.tiles = []          # per frame TILE_DT[N]
        self.frame_rows = []     # vqa.core-frame/v1
        self.regions = []        # per frame list of vqa.core-region/v1
        self.links = []          # per frame vqa.core-link/v1 (frame 0: empty)
        self.global_mv = []      # per frame (dx, dy)
        self.stages = {}
        self.gop_files = {}      # name -> bytes, filled by S5 when present
        self.gops_raw = None
        self.out_dir = None

    def vision_json(self):
        return {"schema": "vqa.vision/v1", "api_version": API_VERSION, "params_sha256": self.params_sha256,
                "frames_sha256": self.frames_sha256, "grid": self.grid.ident, "width": self.grid.W,
                "height": self.grid.H, "n_frames": len(self.tiles), "stages": dict(sorted(self.stages.items())),
                "files": dict(sorted(self._file_shas().items()))}

    def gops(self):
        return [canon.loads(l) for l in self.gop_files.get("gops.jsonl", b"").decode().splitlines() if l]

    def _file_shas(self):
        return {k: canon.sha256_bytes(v) for k, v in self._blobs(with_vision=False).items()}

    def _blobs(self, with_vision=True):
        g = self.grid
        hdr = layout.pack_tile_header(g.s, g.W, g.H, g.gw, g.gh, g.N, len(self.tiles), self.frame_nums[0],
                                      self.params_sha256, API_VERSION)
        out = {"tiles.bin": hdr + b"".join(t.tobytes() for t in self.tiles),
               "frames.jsonl": "".join(canon.dumps(r) for r in self.frame_rows).encode(),
               "regions.jsonl": "".join(canon.dumps(r) for rs in self.regions for r in rs).encode(),
               "links.jsonl": "".join(canon.dumps(l) for l in self.links).encode()}
        out.update(self.gop_files)
        if with_vision:
            out["vision.json"] = canon.dumps(self.vision_json()).encode()
        return out

    def write(self, out_dir):
        """Publish one complete directory; only absent or empty destinations are accepted (R4/JC-9)."""
        out_dir = Path(out_dir)
        if _output_occupied(out_dir):
            raise VqaError("E_VC_OUTPUT_EXISTS", {"path": str(out_dir)})
        blobs = self._blobs()
        stage = None
        created = []
        try:
            parent = out_dir.parent
            while not parent.exists():
                created.append(parent)
                parent = parent.parent
            for directory in reversed(created):
                directory.mkdir()
            stage = Path(tempfile.mkdtemp(prefix="." + out_dir.name + ".", dir=out_dir.parent))
            for name, data in blobs.items():
                canon.write_atomic(stage / name, data)
            if _output_occupied(out_dir):
                raise VqaError("E_VC_OUTPUT_EXISTS", {"path": str(out_dir)})
            os.rename(stage, out_dir)
            stage = None
        except OSError as e:
            raise VqaError("E_IO", {"path": str(out_dir), "reason": str(e)[:80]})
        finally:
            if stage is not None:
                shutil.rmtree(stage)
            if stage is not None or not out_dir.exists():
                for directory in created:
                    if directory.exists():
                        directory.rmdir()
        self.out_dir = out_dir


def _output_occupied(path):
    """An empty caller-created directory may be replaced as a whole; other existing paths are refused."""
    try:
        return path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir())))
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})


def _apply_hints(tiles, grid, rows):
    """Hints may only set UI_OCC / ACTOR_OCC flag bits of tiles under their bbox (g_hint_neutral)."""
    s = grid.s
    for h in rows:
        bit = ROLE_FLAG.get(h.get("role"))
        if not bit:
            continue
        x, y, w, hh = h["bbox_px"]
        if w <= 0 or hh <= 0:
            continue
        sel = ((grid.tx * s < x + w) & ((grid.tx + 1) * s > x) & (grid.ty * s < y + hh) & ((grid.ty + 1) * s > y))
        tiles["flags"][sel] |= np.uint8(bit)


def analyze(src, params=None, hints=None, out_dir=None, workers=1, cpus=None):  # @anchor:vc.pipeline.analyze
    """FrameSource -> VisionRun. params: effective table (None: defaults). Writes out_dir only on success.
    workers>1: S1 runs frame-parallel (parallel.describe_many, pinned to cpus / big cores); S2+ stay sequential and
    the outputs are byte-identical to workers=1."""
    table = dict(P.PARAMS) if params is None else dict(params)
    unknown = sorted(set(table) ^ set(P.PARAMS))
    if unknown:
        raise VqaError("E_VC_PARAM_UNKNOWN", {"param": unknown[0]})
    P._check(table)
    meta = dict(src.meta)
    W, H = meta["width"], meta["height"]
    grid = Grid(W, H)
    by_frame = {}
    for h in hints or ():
        by_frame.setdefault(h["i"], []).append(h)
    shapes = (grid.s, grid.gw, grid.gh)
    run = None
    state = prev_padded = prev_xh = prev_regs = prev_raw = None
    shas, nums = [], []
    pre = None
    if workers > 1:
        from . import parallel
        raw = getattr(src, "raw_array", None)
        if raw is None:
            src = list(src)
            for f in src:
                if f.rgb.shape != (H, W, 3):
                    raise VqaError("E_VC_FRAME_SIZE", {"i": f.i})
            raw = [np.asarray(f.rgb) for f in src]
        pre = parallel.describe_many(raw, grid, workers, cpus, table, lazy_xh=True, return_s0=True)
    for k, f in enumerate(src):
        n = frame_number(f)
        if f.rgb.shape != (H, W, 3):
            raise VqaError("E_VC_FRAME_SIZE", {"i": f.i})
        if pre is None:
            padded = grid.pad(f.rgb)
            y = desc.luma_of_padded(padded)   # RPERF-6: one luma plane per frame, shared by S1 and S2
            tiles, words_eq = desc.describe_padded(padded, grid, prev_padded, prev_xh, table,
                                                   prev_tiles=prev_raw, y_plane=y, lazy_xh=True)
        else:
            tiles = pre[0][k].copy()
            padded, y, words_eq = pre[1][k], pre[2][k], pre[3][k]
        if state is None:
            state = change.ClassState.init(tiles)
            out, state, summ = change.classify_first(state, tiles, y_cur=y)
        else:
            out, state, summ = change.classify_tiles(state, tiles, words_eq, y_cur=y, grid=shapes, params=table)
        prev_raw = tiles   # raw describe output (never the classified/hinted `out`): prev_tiles of the next frame
        out = out.copy()
        desc.fill_xh64(out, padded, grid, prev_xh, hash_all=prev_xh is None)
        _apply_hints(out, grid, by_frame.get(f.i, ()))
        regs = rg.regions_of(out, W, H, n)
        gmv = tuple(summ["global_mv"] or (0, 0))
        link = rg.match_regions(prev_regs, regs, gmv, grid.s) if prev_regs is not None else \
            {"schema": "vqa.core-link/v1", "frame": n, "links": [], "split": [], "merge": []}
        shas.append(f.sha256)
        nums.append(n)
        if run is None:
            run = VisionRun(grid, meta, table, None, nums)
        run.tiles.append(out)
        run.regions.append(regs)
        run.links.append(link)
        run.global_mv.append(gmv)
        run.frame_rows.append({"schema": "vqa.core-frame/v1", "frame": n, "grid": grid.ident, "n": summ["n"],
                               "cov_ppm": summ["cov_ppm"], "global_mv": summ["global_mv"], "regions": len(regs),
                               "tiles_sha256": hashlib.sha256(out.tobytes()).hexdigest()})
        prev_padded, prev_xh, prev_regs = padded, out["xh64"], regs
    if run is None:
        raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "no frames"})
    run.frames_sha256 = fr.frames_sha256(shas)
    run.stages = {"s0-s4": "ok"}
    _run_gop(run)
    if out_dir is not None:
        run.write(out_dir)
    return run


def _run_gop(run):
    """S5 when visioncore.gop is present (peer module); absence is recorded, never an error."""
    try:
        from . import gop
    except ImportError:
        run.stages["s5"] = "absent"
        return
    enc = getattr(gop, "encode_run", None)
    if enc is None:
        run.stages["s5"] = "absent"
        return
    gops = enc(run.tiles, run.grid, run.frame_nums, run.global_mv, run.regions, run.links, run.params)
    run.stages["s5"] = "ok"
    run.gops_raw = gops
    blob, lines, off = b"", [], 0
    for g in gops:
        lines.append(canon.dumps(gop.gop_record(g, run.grid.ident, run.params_sha256, off)))
        blob += g.blob
        off += len(g.blob)
    run.gop_files["gops.bin"] = blob
    run.gop_files["gops.jsonl"] = "".join(lines).encode()
