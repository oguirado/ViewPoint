"""Deterministic synthetic scenarios (splitmix64 noise, no RNG library) with ground-truth fields.
Default 256x160 -> grid s16 16x10. Frames are rgb24; adapter writes raw storage + vqa.frame/v1."""
import hashlib
from pathlib import Path

import numpy as np

from visioncore import canon, ids
from visioncore.errors import VqaError

SCENARIOS = ("clean_static", "gpu_jitter", "flicker", "frozen_subregion", "small_overlap", "slow_drift", "scroll")
DEF_W, DEF_H, DEF_N = 256, 160, 24
HUD_A = (16, 8, 96, 24)
HUD_B = (180, 8, 64, 24)
TARGET = (48, 64, 64, 48)          # flicker / drift target (tile aligned)
FREEZE_T = (160, 64, 64, 64)       # animated ticker holding the frozen sub-rect
FREEZE_F = (176, 80, 32, 32)
BAND = (0, 128, 256, 32)           # animated band of the default 256x160 frame (275000 ppm of the tiles)
BAND_COV_PPM = 350000              # active tile coverage the band gives at every size (FROZEN_COV_PPM is 10000)
DRIFT_R = (112, 80, 48, 32)
M64 = (1 << 64) - 1


def noise(seed, tag, shape):
    """uint8 array, pure function of (seed, tag, index) via splitmix64."""
    n = int(np.prod(shape))
    with np.errstate(over="ignore"):
        x = np.arange(n, dtype=np.uint64) + np.uint64((seed * 0x9E3779B97F4A7C15 + tag * 0xD1B54A32D192ED03) & M64)
        x = x + np.uint64(0x9E3779B97F4A7C15)
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        x = x ^ (x >> np.uint64(31))
    return ((x >> np.uint64(24)) & np.uint64(0xFF)).astype(np.uint8).reshape(shape)


def base_scene(seed, W, H):
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.empty((H, W, 3), np.uint8)
    img[..., 0] = 64 + ((xx * 3 + yy * 2) & 63)
    img[..., 1] = 80 + ((xx + yy * 3) & 63)
    img[..., 2] = 96 + ((xx * 2 + yy) & 31)
    img = (img.astype(np.uint16) + (noise(seed, 1, (H, W, 3)) >> 3)).astype(np.uint8)  # static texture 0..31
    for k, (x, y, w, h) in enumerate((HUD_A, HUD_B)):
        img[y:y + h, x:x + w] = (230, 230, 40 + 60 * k)
        img[y + 4:y + h - 4:4, x + 4:x + w - 4] = (20, 20, 20)  # text-like stripes
    return img


def _rect_slice(img, r):
    x, y, w, h = r
    return img[y:y + h, x:x + w]


def _animate(img, r, seed, f):
    s = _rect_slice(img, r)
    s[...] = (s.astype(np.uint16) // 2 + (noise(seed, 100 + f, s.shape) >> 1)).astype(np.uint8)


def band_rect(W, H):
    """Animated band keeping active tile coverage >= BAND_COV_PPM at every W,H (RPERF-2). The fixed BAND gives only
    12222 / 5882 / 2941 ppm at 720p / 1080p / 4K, so FROZEN could never fire there. The default frame keeps BAND
    (golden frames stay byte-stable); other sizes get full-width tile-aligned rows at the bottom, below every rect."""
    if (W, H) == (DEF_W, DEF_H):
        return BAND
    s = int(ids.grid_ident(W, H).rsplit(":s", 1)[1])   # tile size from the registry id (vadapt may not import layout)
    gh = -(-H // s)
    rows = max(2, -(-BAND_COV_PPM * gh // 1000000) + 1)
    y0 = max(112, (gh - rows) * s)          # 112 = bottom of FREEZE_F's tile row: the band never touches the frozen rect
    return (0, y0, W, H - y0)


def _fields(rect, frames, W, H):
    out = []
    for f in frames:
        out += ids.field_ids_of_rect(rect, W, H, f)
    return out


def render_scenario(name, seed=1, W=DEF_W, H=DEF_H, n=DEF_N):
    """-> (frames list[uint8 (H,W,3)], truth dict, hints list[dict]). truth.fields/ frames are ground truth."""
    if name not in SCENARIOS:
        raise VqaError("E_USAGE", {"scenario": name})
    if W < 256 or H < 160 or n < 16:
        raise VqaError("E_USAGE", {"reason": "synth needs W>=256,H>=160,n>=16"})
    base = base_scene(seed, W, H)
    frames, truth = [], {"scenario": name, "seed": seed, "W": W, "H": H, "n": n, "frames": [], "fields": [], "rect": None}
    hints = [{"i": 0, "rid": "h:ui:hud_a", "role": "ui", "bbox_px": list(HUD_A), "z": 1},
             {"i": 0, "rid": "h:ui:hud_b", "role": "ui", "bbox_px": list(HUD_B), "z": 1}]
    for f in range(n):
        img = base.copy()
        if name == "gpu_jitter":
            m = noise(seed, 200 + f, (H, W, 3))
            d = np.where(m & 3 == 0, np.where(m & 4, 1, -1), 0).astype(np.int16)
            img = np.clip(img.astype(np.int16) + d, 0, 255).astype(np.uint8)
        elif name == "flicker":
            if f % 2 == 1:
                s = _rect_slice(img, TARGET)
                s[...] = 255 - s
        elif name == "frozen_subregion":
            _animate(img, band_rect(W, H), seed, f)
            _animate(img, FREEZE_T, seed, f)
            _rect_slice(img, FREEZE_F)[...] = _rect_slice(base, FREEZE_F)
            _animate(img, FREEZE_F, seed, min(f, 8))
        elif name == "small_overlap":
            if f >= 8:  # HUD_B slides left until it overlaps HUD_A by 3 px
                x, y, w, h = HUD_B
                img[y:y + h, x:x + w] = base[y:y + h, x:x + w]
                nx = HUD_A[0] + HUD_A[2] - 3
                img[y:y + h, nx:nx + w] = (230, 230, 100)
                img[y + 4:y + h - 4:4, nx + 4:nx + w - 4] = (20, 20, 20)
        elif name == "slow_drift":
            s = _rect_slice(img, DRIFT_R)
            s[...] = np.clip(s.astype(np.int16) + f // 2, 0, 255).astype(np.uint8)  # +0.5 grey/frame
        elif name == "scroll":
            img = np.roll(img, 4 * f, axis=1)
        frames.append(img)
    if name == "flicker":
        truth.update(rect=list(TARGET), frames=list(range(1, n, 2)))
    elif name == "frozen_subregion":
        truth.update(rect=list(FREEZE_F), frames=list(range(16, n)), freeze_from=8)
    elif name == "small_overlap":
        x = HUD_A[0] + HUD_A[2] - 3
        truth.update(rect=[x, HUD_B[1], 3, HUD_B[3]], frames=list(range(8, n)))
    elif name == "slow_drift":
        truth.update(rect=list(DRIFT_R), frames=list(range(5, n)))
    elif name == "scroll":
        truth.update(content_shift_px=[4, 0])
    if truth["rect"]:
        truth["fields"] = _fields(tuple(truth["rect"]), truth["frames"], W, H)
    return frames, truth, hints


def frame_sha(rgb):
    return hashlib.sha256(rgb.tobytes()).hexdigest()


def frames_sha256(shas):
    return canon.sha256_obj(list(shas))


class SynthFrame:
    def __init__(self, i, rgb):
        self.i, self.t_us, self.render_index, self.rgb = i, i * 33333, i, rgb
        self.rgb.setflags(write=False)
        self.sha256 = frame_sha(rgb)


class SynthSource:
    def __init__(self, frames, meta):
        self._f = [SynthFrame(i, f) for i, f in enumerate(frames)]
        self.meta = meta

    def __iter__(self):
        return iter(self._f)

    def __len__(self):
        return len(self._f)


def build_manifest(frames, name, seed, raw_path="frames.rgb"):
    H, W = frames[0].shape[:2]
    shas = [frame_sha(f) for f in frames]
    fb = W * H * 3
    return {"schema": "vqa.frame/v1", "width": W, "height": H, "channels": 3, "pixel_format": "rgb24", "fps_num": 30,
            "fps_den": 1, "count": len(frames), "storage": "rgb24_raw", "raw_path": raw_path, "frame_bytes": fb,
            "frames": [{"i": i, "t_us": i * 33333, "render_index": i, "file": raw_path, "sha256": s, "file_sha256": s}
                       for i, s in enumerate(shas)],
            "hints": "region_hints.jsonl", "still_probe": "probe/frames.manifest.json",
            "scope": {"app": "synth", "scene": name, "res": "%dx%d" % (W, H), "build_seq": 0},
            "provenance": {"adapter_id": "synth", "adapter_version": "1", "capture_receipt_sha256": None}}


class SynthAdapter:
    id = "synth"
    version = "1"

    def _spec(self, run_dir):
        p = Path(run_dir) / "scenario.json"
        try:
            s = canon.loads(p.read_text())
        except OSError as e:
            raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]})
        return s["name"], s.get("seed", 1), s.get("W", DEF_W), s.get("H", DEF_H), s.get("n", DEF_N)

    def frames(self, run_dir):
        name, seed, W, H, n = self._spec(run_dir)
        fr, _, _ = render_scenario(name, seed, W, H, n)
        return SynthSource(fr, build_manifest(fr, name, seed))

    def hints(self, run_dir):
        name, seed, W, H, n = self._spec(run_dir)
        return iter(render_scenario(name, seed, W, H, n)[2])

    def still_probe(self, run_dir):
        name, seed, W, H, n = self._spec(run_dir)
        base = base_scene(seed, W, H)
        fr = []
        for f in range(30):
            m = noise(seed, 300 + f, (H, W, 3))
            d = np.where(m & 3 == 0, np.where(m & 4, 1, -1), 0).astype(np.int16)
            fr.append(np.clip(base.astype(np.int16) + d, 0, 255).astype(np.uint8))
        return SynthSource(fr, build_manifest(fr, "still_probe", seed))

    def scope(self, run_dir):
        name, seed, W, H, n = self._spec(run_dir)
        return {"app": "synth", "scene": name, "res": "%dx%d" % (W, H), "build_seq": 0}

    def write_run(self, run_dir, name, seed=1, W=DEF_W, H=DEF_H, n=DEF_N):
        """Materialise scenario.json, frames.rgb, frames.manifest.json, region_hints.jsonl, truth.json."""
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        fr, truth, hints = render_scenario(name, seed, W, H, n)
        man = build_manifest(fr, name, seed)
        canon.write_atomic(run_dir / "scenario.json", canon.dumps({"name": name, "seed": seed, "W": W, "H": H, "n": n}).encode())
        canon.write_atomic(run_dir / "frames.rgb", b"".join(f.tobytes() for f in fr))
        canon.write_atomic(run_dir / "frames.manifest.json", canon.dumps(man).encode())
        canon.write_atomic(run_dir / "region_hints.jsonl", "".join(canon.dumps(h) for h in hints).encode())
        canon.write_atomic(run_dir / "truth.json", canon.dumps(truth).encode())
        return man, truth
