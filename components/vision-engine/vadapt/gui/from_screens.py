# @module {"id":"va.gui","layer":4,"stage":"ADAPT","owner":"ARCH","deps":["vc.canon","vc.errors"],"book":"HB-VC-a0-arch"}
"""@module va.gui - GUI / Computer Use adapter (SPEC_ARCH §5.2, W7): screenshot sequence + optional accessibility rects.
Never imports an engine. Input dir layout (all but the screenshots optional):
  screens/*.png            screenshots, natural-sorted unless steps.jsonl lists them
  steps.jsonl              rows {"file","step"?,"t_us"?}; step -> render_index (default = position), t_us default = step*1_000_000
  a11y.jsonl               rows {"step","name","bbox":[x,y,w,h],"role"?,"z"?}; becomes vqa.region-hint/v1 role "a11y"
  idle/*.png               unchanging-app screenshots (still probe)
  raw.json (+ frames.raw)  RAW RGB24 instead of screens/: {"width","height","stride"?,"file"|"files"}; stride = bytes per row
                           (default width*3); one concatenated file or one file per frame; read zero-copy (mmap + frombuffer);
                           manifest storage rgb24_raw; frame sha256 equals the PNG path on the same pixels
  app.json                 {"app","scene","build_seq"}; defaults app=gui scene=session build_seq=null
CLI: python3 -m vadapt.gui.from_screens --in <dir> --out <dir>  -> frames.manifest.json (+ region_hints.jsonl)."""
import hashlib
import mmap
import os
import re
import sys
from pathlib import Path

import numpy as np

from visioncore import canon, decode, frame as vframe
from visioncore.errors import VqaError, result_line, result_obj

ADAPTER_ID = "gui"
ADAPTER_VERSION = "1"
_NAME_BAD = re.compile(r"[^a-z0-9_]+")
_NUM = re.compile(r"(\d+)")


def _natural(p):
    return [int(t) if t.isdigit() else t for t in _NUM.split(p.name)]


def _read_jsonl(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    rows = []
    for k, line in enumerate(text.splitlines()):
        if line.strip():
            try:
                row = canon.loads(line)
            except VqaError:
                raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(path), "line": k + 1})
            if not isinstance(row, dict):
                raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(path), "line": k + 1})
            rows.append(row)
    return rows


def _u(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": what, "value": repr(v)[:40]})
    return v


def _list_pngs(d):
    return sorted(Path(d).glob("*.png"), key=_natural)


def _load(d, entries, label):
    """entries: [(path, t_us|None, render_index|None)] -> (frames, file_shas, W, H)."""
    frames, shas, size = [], [], None
    last_t = -1
    for i, (p, t, r) in enumerate(entries):
        try:
            data = Path(p).read_bytes()
        except OSError as e:
            raise VqaError("E_IO", {"path": str(p), "reason": str(e)[:80]})
        rgb = decode.png(data)
        if size is None:
            size = rgb.shape[:2]
        elif rgb.shape[:2] != size:
            raise VqaError("E_VC_FRAME_SIZE", {"path": str(p), "shape": list(rgb.shape[:2]), "expect": list(size)})
        r = i if r is None else r
        t = r * 1000000 if t is None else t
        if t < last_t:
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "t_us", "reason": "decreasing", "ctx": i})
        last_t = t
        frames.append(vframe.Frame(i, t, r, rgb))
        shas.append(hashlib.sha256(data).hexdigest())
    if not frames:
        raise VqaError("E_IO", {"path": str(d), "reason": "no %s png files" % label})
    return frames, shas, size[1], size[0]


def _raw_meta(run_dir):
    m = canon.loads(Path(run_dir, "raw.json").read_text(encoding="utf-8"))
    if not isinstance(m, dict):
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "raw.json"})
    W, H = _u(m.get("width"), 1, 1 << 30, "width"), _u(m.get("height"), 1, 1 << 30, "height")
    stride = _u(m["stride"], W * 3, 1 << 31, "stride") if "stride" in m else W * 3
    names = m.get("files") if "files" in m else [m.get("file", "frames.raw")]
    if not isinstance(names, list) or not names or not all(isinstance(f, str) and f and not Path(f).is_absolute()
                                                           and ".." not in Path(f).parts for f in names):
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "file(s)"})
    return W, H, stride, names, "files" in m


def _map_ro(path):
    try:
        with open(path, "rb") as fh:
            return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})


def _load_raw(run_dir):
    """raw.json layout -> (frames, shas, W, H, files). Pixels are read-only views over the mmap'd file (zero copy);
    only padded rows (stride > width*3) are compacted, since Frame needs C-contiguous (H,W,3)."""
    W, H, stride, names, per_frame = _raw_meta(run_dir)
    fb = stride * H
    frames, shas, files = [], [], []
    for name in names:
        path = Path(run_dir) / name
        mm = _map_ro(path)
        if len(mm) == 0 or len(mm) % fb or (per_frame and len(mm) != fb):
            raise VqaError("E_VC_FRAME_SIZE", {"path": str(path), "bytes": len(mm), "frame_bytes": fb})
        for k in range(len(mm) // fb):
            a = np.frombuffer(mm, dtype=np.uint8, count=fb, offset=k * fb).reshape(H, stride)
            rgb = a[:, :W * 3].reshape(H, W, 3) if stride == W * 3 else np.ascontiguousarray(a[:, :W * 3]).reshape(H, W, 3)
            i = len(frames)
            frames.append(vframe.Frame(i, i * 1000000, i, rgb))
            shas.append(frames[-1].sha256)
            files.append(name)
    return frames, shas, W, H, files


def _slug(name):
    s = _NAME_BAD.sub("_", str(name).lower()).strip("_")[:40].strip("_")
    return s or "node"


class GuiAdapter:
    id = ADAPTER_ID
    version = ADAPTER_VERSION

    # -- inputs ---------------------------------------------------------------------------------
    def _entries(self, run_dir):
        run_dir = Path(run_dir)
        sd = run_dir / "screens"
        steps = run_dir / "steps.jsonl"
        if steps.is_file():
            out = []
            for row in _read_jsonl(steps):
                f = row.get("file")
                if not isinstance(f, str) or not f or Path(f).is_absolute() or ".." in Path(f).parts:
                    raise VqaError("E_VC_FRAME_SCHEMA", {"field": "file", "value": repr(f)[:40]})
                t = _u(row["t_us"], 0, (1 << 64) - 1, "t_us") if "t_us" in row else None
                r = _u(row["step"], 0, vframe.U32, "step") if "step" in row else None
                out.append((sd / f, t, r))
            return out
        return [(p, None, None) for p in _list_pngs(sd)]

    def _app(self, run_dir):
        p = Path(run_dir) / "app.json"
        a = canon.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        if not isinstance(a, dict):
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "app.json"})
        return a

    def _is_raw(self, run_dir):
        return (Path(run_dir) / "raw.json").is_file() and not (Path(run_dir) / "screens").is_dir()

    def _loaded(self, run_dir):
        if self._is_raw(run_dir):
            return _load_raw(run_dir)[:4]
        return _load(Path(run_dir) / "screens", self._entries(run_dir), "screens")

    # -- Adapter protocol -----------------------------------------------------------------------
    def scope(self, run_dir):
        a = self._app(run_dir)
        W, H = self._loaded(run_dir)[2:]
        bs = a.get("build_seq")
        if bs is not None:
            bs = _u(bs, 0, vframe.U32, "build_seq")
        return {"app": _slug(a.get("app", "gui")), "scene": _slug(a.get("scene", "session")),
                "res": "%dx%d" % (W, H), "build_seq": bs}

    def _manifest(self, run_dir, frames, shas, W, H, files, scope, storage="png_dir", raw_path=None):
        rows = [{"i": f.i, "t_us": f.t_us, "render_index": f.render_index, "file": fn,
                 "sha256": f.sha256, "file_sha256": s} for f, s, fn in zip(frames, shas, files)]
        return {"schema": vframe.SCHEMA, "width": W, "height": H, "channels": 3, "pixel_format": "rgb24",
                "fps_num": 1, "fps_den": 1, "count": len(frames), "storage": storage, "raw_path": raw_path,
                "frame_bytes": W * H * 3, "frames": rows,
                "hints": "region_hints.jsonl" if (Path(run_dir) / "a11y.jsonl").is_file() else None,
                "still_probe": "probe/frames.manifest.json" if (Path(run_dir) / "idle").is_dir() else None,
                "scope": scope,
                "provenance": {"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
                               "capture_receipt_sha256": None}}

    def frames(self, run_dir):  # @anchor:va.gui.frames
        """Collect GUI screenshots into the frame manifest, preserving nested relative paths."""
        if self._is_raw(run_dir):
            frames, shas, W, H, files = _load_raw(run_dir)
            m = self._manifest(run_dir, frames, shas, W, H, files, self.scope(run_dir), "rgb24_raw", "frames.rgb24")
            vframe.validate_manifest(m)
            return vframe.MemoryFrameSource(frames, m)
        entries = self._entries(run_dir)
        frames, shas, W, H = _load(run_dir, entries, "screens")
        sd = Path(run_dir) / "screens"
        files = ["screens/" + Path(p).relative_to(sd).as_posix() for p, _, _ in entries]
        m = self._manifest(run_dir, frames, shas, W, H, files, self.scope(run_dir))
        vframe.validate_manifest(m)
        return vframe.MemoryFrameSource(frames, m)

    def hints(self, run_dir):
        p = Path(run_dir) / "a11y.jsonl"
        if not p.is_file():
            return None
        src = self.frames(run_dir)
        W, H = src.meta["width"], src.meta["height"]
        by_step = {f.render_index: f.i for f in src}
        out, seen = [], {}
        for k, row in enumerate(_read_jsonl(p)):
            step = _u(row.get("step"), 0, vframe.U32, "step")
            if step not in by_step:
                raise VqaError("E_VC_FRAME_SCHEMA", {"field": "step", "reason": "no such frame", "ctx": k})
            bb = row.get("bbox")
            if not isinstance(bb, list) or len(bb) != 4:
                raise VqaError("E_VC_FRAME_SCHEMA", {"field": "bbox", "ctx": k})
            x, y, w, h = [_u(v, -(1 << 20), 1 << 20, "bbox") for v in bb]
            x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
            if w <= 0 or h <= 0:
                raise VqaError("E_VC_FRAME_SCHEMA", {"field": "bbox", "reason": "empty", "ctx": k})
            if x1 <= x0 or y1 <= y0:
                continue  # entirely off-screen
            z = row.get("z")
            z = None if z is None else _u(z, -32768, 32767, "z")
            i, base = by_step[step], _slug(row.get("name", "node"))
            n = seen.get((i, base), 0) + 1
            seen[(i, base)] = n
            name = base if n == 1 else "%s_%d" % (base[:36], n)
            out.append({"schema": "vqa.region-hint/v1", "i": i, "rid": "h:a11y:" + name, "role": "a11y",
                        "bbox_px": [x0, y0, x1 - x0, y1 - y0], "z": z})
        out.sort(key=lambda r: (r["i"], r["rid"]))
        return iter(out)

    def still_probe(self, run_dir):
        d = Path(run_dir) / "idle"
        if not d.is_dir():
            return None
        frames, shas, W, H = _load(d, [(p, None, None) for p in _list_pngs(d)], "idle")
        files = ["idle/" + p.name for p in _list_pngs(d)]
        m = self._manifest(run_dir, frames, shas, W, H, files, dict(self.scope(run_dir), scene="still_probe"))
        m["hints"], m["still_probe"] = None, None
        vframe.validate_manifest(m)
        return vframe.MemoryFrameSource(frames, m)

    # -- materialise ----------------------------------------------------------------------------
    def adapt(self, run_dir, out_dir):
        """Write frames.manifest.json (+ region_hints.jsonl) into out_dir; frame file paths are rewritten relative
        to out_dir so the manifest resolves the original screenshots in place."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        src = self.frames(run_dir)
        hints = self.hints(run_dir)
        m = dict(src.meta)
        if m["storage"] == "rgb24_raw":  # compact the raw input into the engine's dense layout (frames.rgb24)
            with open(out_dir / "frames.rgb24", "wb") as fh:
                for f in src:
                    fh.write(f.rgb.tobytes())
            m["frames"] = [dict(r, file="frames.rgb24") for r in m["frames"]]
            if hints is not None:
                canon.write_atomic(out_dir / "region_hints.jsonl", "".join(canon.dumps(h) for h in hints).encode())
            else:
                m["hints"] = None
            vframe.write_manifest(out_dir / "frames.manifest.json", m)
            return m
        rel = os.path.relpath(Path(run_dir).resolve(), out_dir.resolve())
        m["frames"] = [dict(r, file=os.path.normpath(os.path.join(rel, r["file"])).replace(os.sep, "/"))
                       for r in m["frames"]]
        if hints is not None:
            canon.write_atomic(out_dir / "region_hints.jsonl", "".join(canon.dumps(h) for h in hints).encode())
        else:
            m["hints"] = None
        vframe.write_manifest(out_dir / "frames.manifest.json", m)
        return m


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(a) != 4 or a[0] != "--in" or a[2] != "--out":
            raise VqaError("E_USAGE", {"usage": "--in <dir> --out <dir>"})
        m = GuiAdapter().adapt(a[1], a[3])
        print(result_line(result_obj("OK", detail={"count": m["count"], "res": m["scope"]["res"]})), end="")
        return 0
    except VqaError as e:
        print(result_line(result_obj(e.code, detail=e.detail)), end="")
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
