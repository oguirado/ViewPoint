# @module {"id":"va.blender","layer":4,"stage":"ADAPT","owner":"ARCH","deps":["vc.canon","vc.errors"],"book":"HB-VC-a0-arch"}
"""@module va.blender - `blender -b f.blend -a` output dir (+ optional scene.json / bounds.jsonl) -> vqa.frame/v1
manifest, frames and vqa.region-hint/v1 rows (SPEC_ARCH §5). Reads files only; never imports bpy or Blender.

Run dir layout (all optional except the PNG frames):
  <name><digits>.png   frame number = trailing digits of the stem; render_index = frame number
  scene.json           {"app","scene","build_seq","fps_num","fps_den","adapter_version"}
  bounds.jsonl         {"frame":N,"name":str,"role":"object|gizmo|ui|actor|a11y","bbox_px":[x,y,w,h],"z":int|null}
  still/*.png          optional unchanging-scene re-renders (CORE calibration probe)
EXR frames are not decoded (E_VC_PNG): render PNG or convert upstream."""
import os
import re
import shutil
import stat
import tempfile
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


def _input_stat(path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]}) from e
    if stat.S_ISLNK(st.st_mode):
        raise VqaError("E_IO", {"path": str(path), "reason": "expected an input file in the render directory"})
    return st


def _regular_input(path):
    st = _input_stat(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _list_pngs(d):
    """-> [(frame_number, path)] sorted by frame number; duplicate numbers refused."""
    d = Path(d)
    st = _input_stat(d)
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise VqaError("E_IO", {"path": str(d), "reason": "not a directory"})
    try:
        entries = sorted(d.iterdir())
    except OSError as e:
        raise VqaError("E_IO", {"path": str(d), "reason": str(e)[:80]}) from e
    out, seen = [], {}
    for p in entries:
        ext = p.suffix.lower()
        if ext not in (".png", ".exr") or not _regular_input(p):
            continue
        if ext == ".exr":
            raise VqaError("E_VC_PNG", {"path": p.name, "reason": "EXR not supported; render PNG"})
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
    if not _regular_input(p):
        return {}
    m = _read_json(p)
    if not isinstance(m, dict):
        raise VqaError("E_VC_FRAME_SCHEMA", {"path": "scene.json"})
    return m


def _decode_all(pngs, *, keep_bytes=False):
    frames, shape = [], None
    for _, p in pngs:
        try:
            data = p.read_bytes()
        except OSError as e:
            raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]}) from e
        rgb = decode.png(data)
        if shape is None:
            shape = rgb.shape
        elif rgb.shape != shape:
            raise VqaError("E_VC_FRAME_SIZE", {"path": p.name, "shape": list(rgb.shape)})
        frames.append((rgb, canon.sha256_bytes(data), data if keep_bytes else None))
    return frames


def _build(pngs, captured, meta, kind):
    fps_num, fps_den = meta.get("fps_num", 24), meta.get("fps_den", 1)
    for v in (fps_num, fps_den):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "fps"})
    first = pngs[0][0]
    H, W, _ = captured[0][0].shape
    items, frames = [], []
    for k, ((n, _), (rgb, digest, _)) in enumerate(zip(pngs, captured)):
        t_us = (n - first) * 1000000 * fps_den // fps_num
        f = vframe.Frame(k, t_us, n, rgb)
        frames.append(f)
        items.append({"i": k, "t_us": t_us, "render_index": n, "file": "frames/%06d.png" % k,
                      "sha256": f.sha256, "file_sha256": digest})
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
    if not _regular_input(p):
        return None
    rows = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]})
    except UnicodeError as e:
        raise VqaError("E_VC_FRAME_SCHEMA", {"path": "bounds.jsonl", "reason": str(e)[:80]}) from e
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
        if isinstance(fn, bool) or not isinstance(fn, int) or fn not in frame_to_i:
            raise bad("unknown frame")
        if role not in ROLES:
            raise bad("role")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
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
        root = Path(run_dir)
        sd = root / "still"
        _input_stat(root)
        st = _input_stat(sd)
        if st is None or not stat.S_ISDIR(st.st_mode):
            return None
        pngs = _list_pngs(sd)
        if not pngs:
            return None
        man, frames = _build(pngs, _decode_all(pngs), _scene_meta(run_dir), "still_probe")
        return vframe.MemoryFrameSource(frames, man)

    def scope(self, run_dir):
        return dict(self.frames(run_dir).meta["scope"])


class _RollbackError(OSError):
    pass


def _publish(stage, out_dir, files):
    if out_dir.is_symlink():
        raise OSError("output directory is a symbolic link")
    if not out_dir.exists():
        stage.rename(out_dir)
        return

    frames = out_dir / "frames"
    if frames.is_symlink():
        raise OSError("output frames directory is a symbolic link")
    backup = stage / ".backup"
    backup.mkdir()
    had_frames = frames.exists()
    published = []
    try:
        frames.mkdir(exist_ok=True)
        for rel in files:
            target = out_dir / rel
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise OSError("expected a regular output file: %s" % target)
            saved = None
            if target.is_file():
                saved = backup / str(len(published))
                os.link(target, saved)
            (stage / rel).replace(target)
            published.append((target, saved))
    except OSError:
        try:
            for target, saved in reversed(published):
                if saved is None:
                    target.unlink()
                else:
                    saved.replace(target)
            if not had_frames and frames.is_dir():
                frames.rmdir()
        except OSError as e:
            raise _RollbackError(str(e)) from e
        raise


def adapt(in_dir, out_dir):
    """Write frames/NNNNNN.png copies, frames.manifest.json and (if bounds.jsonl) region_hints.jsonl into out_dir.
    Validate and stage before publishing. Existing destinations use single-writer rollback. Returns the manifest."""
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    pngs = _list_pngs(in_dir)
    if not pngs:
        raise VqaError("E_VC_FRAME_SCHEMA", {"reason": "no PNG frames in %s" % in_dir})
    captured = _decode_all(pngs, keep_bytes=True)
    man, _ = _build(pngs, captured, _scene_meta(in_dir), "render")
    rows = _hint_rows(in_dir, {n: k for k, (n, _) in enumerate(pngs)})
    if rows is not None:
        man["hints"] = "region_hints.jsonl"
    vframe.validate_manifest(man)
    files = [Path(item["file"]) for item in man["frames"]]
    staging = None
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".vqa-blender-", dir=out_dir.parent))
        stage = staging / "result"
        (stage / "frames").mkdir(parents=True)
        for (_, _, data), rel in zip(captured, files):
            (stage / rel).write_bytes(data)
        if rows is not None:
            canon.write_atomic(stage / "region_hints.jsonl", "".join(canon.dumps(r) for r in rows).encode())
            files.append(Path("region_hints.jsonl"))
        vframe.write_manifest(stage / "frames.manifest.json", man)
        files.append(Path("frames.manifest.json"))
        _publish(stage, out_dir, files)
    except _RollbackError as e:
        recovery = staging
        staging = None
        raise VqaError("E_IO", {"path": str(out_dir), "recovery": str(recovery),
                               "reason": "output rollback failed: %s" % str(e)[:80]}) from e
    except OSError as e:
        raise VqaError("E_IO", {"path": str(out_dir), "reason": str(e)[:80]}) from e
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return man
