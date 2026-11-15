# @module {"id":"va.blender","layer":4,"stage":"ADAPT","owner":"ARCH","deps":["vc.canon","vc.errors"],"book":"HB-VC-a0-arch"}
"""@module va.blender - `blender -b f.blend -a` output dir (+ optional scene.json / bounds.jsonl) -> vqa.frame/v1
manifest, frames and vqa.region-hint/v1 rows (SPEC_ARCH §5). Reads files only; never imports bpy or Blender.

Run dir layout (all optional except the PNG frames):
  <name><digits>.png   frame number = trailing digits of the stem; render_index = frame number
  scene.json           {"app","scene","build_seq","fps_num","fps_den","adapter_version"}
  bounds.jsonl         {"frame":N,"name":str,"role":"object|gizmo|ui|actor|a11y","bbox_px":[x,y,w,h],"z":int|null}
  still/*.png          optional unchanging-scene re-renders (CORE calibration probe)
EXR frames are not decoded (E_VC_PNG): render PNG or convert upstream."""
import re
import shutil
from pathlib import Path

import numpy as np

from visioncore import canon, decode, frame as vframe
from visioncore.errors import VqaError

ADAPTER_ID = "blender"
ADAPTER_VERSION = "1"
ROLES = ("ui", "actor", "a11y", "object", "gizmo")
_NUM = re.compile(r"(\d+)$")
_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
_SLUG = re.compile(r"[^a-z0-9_]+")


def _slug(s, n=32, default="unknown"):
    s = _SLUG.sub("_", str(s).lower()).strip("_")[:n]
    return s or default


def _read_json(path):
    try:
        return canon.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    except ValueError as e:
        raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(path), "reason": str(e)[:80]})


def _list_pngs(d):
    """-> [(frame_number, path)] sorted by frame number; duplicate numbers refused."""
    d = Path(d)
    if not d.is_dir():
        raise VqaError("E_IO", {"path": str(d), "reason": "not a directory"})
    out, seen = [], {}
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext == ".exr":
            raise VqaError("E_VC_PNG", {"path": p.name, "reason": "EXR not supported; render PNG"})
        if ext != ".png":
            continue
        m = _NUM.search(p.stem)
        if not m:
            raise VqaError("E_VC_FRAME_SCHEMA", {"path": p.name, "reason": "no trailing frame number"})
        n = int(m.group(1))
        if n in seen:
            raise VqaError("E_VC_FRAME_SCHEMA", {"path": p.name, "reason": "duplicate frame %d (%s)" % (n, seen[n])})
        seen[n] = p.name
        out.append((n, p))
    out.sort(key=lambda t: t[0])
    return out


def _scene_meta(run_dir):
    p = Path(run_dir) / "scene.json"
    if not p.is_file():
        return {}
    m = _read_json(p)
    if not isinstance(m, dict):
        raise VqaError("E_VC_FRAME_SCHEMA", {"path": "scene.json"})
    return m


def _decode_all(pngs):
    frames, shape = [], None
    for n, p in pngs:
        rgb = decode.png(p)
        if shape is None:
            shape = rgb.shape
        elif rgb.shape != shape:
            raise VqaError("E_VC_FRAME_SIZE", {"path": p.name, "shape": list(rgb.shape)})
        frames.append(rgb)
    return frames


def _build(pngs, rgbs, meta, kind):
    fps_num, fps_den = meta.get("fps_num", 24), meta.get("fps_den", 1)
    for v in (fps_num, fps_den):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "fps"})
    first = pngs[0][0]
    H, W, _ = rgbs[0].shape
    items, frames = [], []
    for k, ((n, p), rgb) in enumerate(zip(pngs, rgbs)):
        t_us = (n - first) * 1000000 * fps_den // fps_num
        f = vframe.Frame(k, t_us, n, rgb)
        frames.append(f)
        items.append({"i": k, "t_us": t_us, "render_index": n, "file": "frames/%06d.png" % k,
                      "sha256": f.sha256, "file_sha256": canon.sha256_bytes(p.read_bytes())})
    bs = meta.get("build_seq")
    if bs is not None and (isinstance(bs, bool) or not isinstance(bs, int) or not 0 <= bs < 1 << 32):
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "build_seq"})
    man = {"schema": vframe.SCHEMA, "width": W, "height": H, "channels": 3, "pixel_format": "rgb24",
           "fps_num": fps_num, "fps_den": fps_den, "count": len(frames), "storage": "png_dir", "raw_path": None,
           "frame_bytes": W * H * 3, "frames": items, "hints": None, "still_probe": None,
           "scope": {"app": _slug(meta.get("app", "blender"), 32), "scene": _slug(meta.get("scene", kind), 48),
                     "res": "%dx%d" % (W, H), "build_seq": bs},
           "provenance": {"adapter_id": ADAPTER_ID,
                          "adapter_version": str(meta.get("adapter_version", ADAPTER_VERSION))[:32],
                          "capture_receipt_sha256": None}}
    vframe.validate_manifest(man)
    return man, frames


def _hint_rows(run_dir, frame_to_i):
    p = Path(run_dir) / "bounds.jsonl"
    if not p.is_file():
        return None
    rows = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]})
    for ln, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            o = canon.loads(line)
        except ValueError:
            raise VqaError("E_VC_FRAME_SCHEMA", {"path": "bounds.jsonl", "line": ln})
        bad = lambda why: VqaError("E_VC_FRAME_SCHEMA", {"path": "bounds.jsonl", "line": ln, "reason": why})
        if not isinstance(o, dict):
            raise bad("not an object")
        fn, role, name, bb, z = o.get("frame"), o.get("role", "object"), o.get("name"), o.get("bbox_px"), o.get("z")
        if fn not in frame_to_i or isinstance(fn, bool):
            raise bad("unknown frame")
        if role not in ROLES:
            raise bad("role")
        if not isinstance(name, str) or not _NAME.match(name):
            raise bad("name")
        if (not isinstance(bb, list) or len(bb) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in bb)
                or bb[2] < 1 or bb[3] < 1):
            raise bad("bbox_px")
        if z is not None and (isinstance(z, bool) or not isinstance(z, int) or not -32768 <= z <= 32767):
            raise bad("z")
        rows.append({"schema": "vqa.region-hint/v1", "i": frame_to_i[fn], "rid": "h:%s:%s" % (role, name),
                     "role": role, "bbox_px": bb, "z": z})
    rows.sort(key=lambda r: (r["i"], r["rid"], r["bbox_px"]))
    return rows


class BlenderAdapter:
    id = ADAPTER_ID
    version = ADAPTER_VERSION

    def frames(self, run_dir):
        pngs = _list_pngs(run_dir)
        if not pngs:
            raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "no PNG frames in %s" % run_dir})
        man, frames = _build(pngs, _decode_all(pngs), _scene_meta(run_dir), "render")
        return vframe.MemoryFrameSource(frames, man)

    def hints(self, run_dir):
        pngs = _list_pngs(run_dir)
        rows = _hint_rows(run_dir, {n: k for k, (n, _) in enumerate(pngs)})
        return None if rows is None else iter(rows)

    def still_probe(self, run_dir):
        sd = Path(run_dir) / "still"
        if not sd.is_dir():
            return None
        pngs = _list_pngs(sd)
        if not pngs:
            return None
        man, frames = _build(pngs, _decode_all(pngs), _scene_meta(run_dir), "still_probe")
        return vframe.MemoryFrameSource(frames, man)

    def scope(self, run_dir):
        return dict(self.frames(run_dir).meta["scope"])


def adapt(in_dir, out_dir):
    """Write frames/NNNNNN.png copies, frames.manifest.json and (if bounds.jsonl) region_hints.jsonl into out_dir.
    All validation precedes the first write, so a refusal leaves out_dir untouched. Returns the manifest."""
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    pngs = _list_pngs(in_dir)
    if not pngs:
        raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "no PNG frames in %s" % in_dir})
    man, _ = _build(pngs, _decode_all(pngs), _scene_meta(in_dir), "render")
    rows = _hint_rows(in_dir, {n: k for k, (n, _) in enumerate(pngs)})
    if rows is not None:
        man["hints"] = "region_hints.jsonl"
    vframe.validate_manifest(man)
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    for k, (_, p) in enumerate(pngs):
        shutil.copyfile(p, out_dir / "frames" / ("%06d.png" % k))
    if rows is not None:
        canon.write_atomic(out_dir / "region_hints.jsonl", "".join(canon.dumps(r) for r in rows).encode())
    vframe.write_manifest(out_dir / "frames.manifest.json", man)
    return man
