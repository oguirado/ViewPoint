# @module {"id":"va.godot","layer":4,"stage":"ADAPT","owner":"ARCH","book":"HB-VC-a0-arch","deps":["vc.canon","vc.decode","vc.errors"]}
"""@module va.godot - godot-visual-qa capture dir (vqa.capture/v1 manifest + PNG frames + telemetry.jsonl) ->
vqa.frame/v1 manifest + vqa.region-hint/v1 rows (SPEC_ARCH §4-§5). Reads only what v1.0 wrote; no Godot import.

Layout read (run root R, or R/capture itself): manifest.json | capture/manifest.json (frames[].file.path relative to R,
frames[].decoded_rgb_sha256 optional), telemetry (manifest telemetry.path, default telemetry.jsonl) with `ui` and
`actors` rects in px_q16 [x,y,w,h]; optional still probe: R/still_probe/*.png (or probe/)."""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

import numpy as np

from visioncore import canon, frame as vframe

from visioncore.decode import png as read_png
from visioncore.errors import VqaError, result_line, result_obj

ADAPTER_ID = "godot"
ADAPTER_VERSION = "1"
_NAME_BAD = re.compile(r"[^a-z0-9_]+")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    except ValueError as e:
        raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(path), "reason": str(e)[:80]})


def _slug(s, n):
    s = _NAME_BAD.sub("_", str(s).lower()).strip("_")[:n]
    return s or "unknown"


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})


def _inside_root(root_real, target_real):
    try:
        return os.path.commonpath([root_real, target_real]) == root_real and target_real != root_real
    except ValueError:
        return False


def _check_root(root, reject_all_parent_symlinks=False):
    if not isinstance(root, (str, os.PathLike)) or not os.fspath(root) or "\x00" in str(root):
        raise VqaError("E_IO", {"path": str(root), "reason": "invalid path"})
    d = Path(root)
    if ".." in d.parts:
        raise VqaError("E_IO", {"path": str(d), "reason": "escaping ancestor"})
    abs_d = Path(os.path.abspath(d))
    if ".." in abs_d.parts:
        raise VqaError("E_IO", {"path": str(d), "reason": "escaping ancestor"})
    for a in (d, *d.parents):
        if str(a) == ".":
            continue
        st_a = _lstat(a)
        if st_a is None:
            continue
        if stat.S_ISLNK(st_a.st_mode):
            if reject_all_parent_symlinks or a == d:
                raise VqaError("E_IO", {"path": str(a), "reason": "symlink refused"})
            parent_real = os.path.realpath(a.parent)
            target_real = os.path.realpath(a)
            if not _inside_root(parent_real, target_real):
                raise VqaError("E_IO", {"path": str(a), "reason": "escaping ancestor"})
        elif not stat.S_ISDIR(st_a.st_mode):
            raise VqaError("E_IO", {"path": str(a), "reason": "not a directory"})
    st_d = _lstat(d)
    if st_d is None:
        return d, None
    if stat.S_ISLNK(st_d.st_mode) or not stat.S_ISDIR(st_d.st_mode):
        raise VqaError("E_IO", {"path": str(d), "reason": "not a directory"})
    return d, os.path.realpath(d)


def _check_manifest_base(root, root_real, manifest_path):
    if not isinstance(manifest_path, (str, os.PathLike)) or not os.fspath(manifest_path) or "\x00" in str(manifest_path):
        raise VqaError("E_IO", {"path": str(manifest_path), "reason": "invalid path"})
    mp = Path(manifest_path)
    if ".." in mp.parts:
        raise VqaError("E_IO", {"path": str(mp), "reason": "escaping ancestor"})
    abs_root = Path(os.path.abspath(root))
    abs_mp = Path(os.path.abspath(mp))
    try:
        rel_mp = abs_mp.relative_to(abs_root)
    except ValueError:
        raise VqaError("E_IO", {"path": str(mp), "reason": "path outside root"})
    if not rel_mp.parts:
        raise VqaError("E_IO", {"path": str(mp), "reason": "invalid manifest path"})
    cur = root
    for part in rel_mp.parts[:-1]:
        cur = cur / part
        st_cur = _lstat(cur)
        if st_cur is None or stat.S_ISLNK(st_cur.st_mode) or not stat.S_ISDIR(st_cur.st_mode):
            raise VqaError("E_IO", {"path": str(cur), "reason": "invalid ancestor"})
        if not _inside_root(root_real, os.path.realpath(cur)):
            raise VqaError("E_IO", {"path": str(cur), "reason": "path outside root"})
    st_mp = _lstat(mp)
    if st_mp is not None:
        if stat.S_ISLNK(st_mp.st_mode) or not stat.S_ISREG(st_mp.st_mode):
            raise VqaError("E_IO", {"path": str(mp), "reason": "invalid manifest"})
        if not _inside_root(root_real, os.path.realpath(mp)):
            raise VqaError("E_IO", {"path": str(mp), "reason": "path outside root"})
    return mp, cur


def _roots(run_dir):
    """-> (manifest_path, root) where root resolves the relative `capture/frames/...` paths."""
    d, d_real = _check_root(run_dir, reject_all_parent_symlinks=False)
    if d_real is None:
        raise VqaError("E_IO", {"path": str(d), "reason": "no capture/manifest.json or manifest.json"})
    cap_dir = d / "capture"
    st_cap = _lstat(cap_dir)
    if st_cap is not None:
        if stat.S_ISLNK(st_cap.st_mode) or not stat.S_ISDIR(st_cap.st_mode):
            raise VqaError("E_IO", {"path": str(cap_dir), "reason": "invalid capture directory"})
        if not _inside_root(d_real, os.path.realpath(cap_dir)):
            raise VqaError("E_IO", {"path": str(cap_dir), "reason": "path outside root"})
        cand1 = cap_dir / "manifest.json"
        st_c1 = _lstat(cand1)
        if st_c1 is not None:
            if stat.S_ISLNK(st_c1.st_mode) or not stat.S_ISREG(st_c1.st_mode):
                raise VqaError("E_IO", {"path": str(cand1), "reason": "invalid manifest"})
            if not _inside_root(d_real, os.path.realpath(cand1)):
                raise VqaError("E_IO", {"path": str(cand1), "reason": "path outside root"})
            return cand1, d
    cand2 = d / "manifest.json"
    st_c2 = _lstat(cand2)
    if st_c2 is not None:
        root = d.parent if d.name == "capture" else d
        root, root_real = _check_root(root, reject_all_parent_symlinks=False)
        if root_real is None:
            raise VqaError("E_IO", {"path": str(root), "reason": "invalid root"})
        if stat.S_ISLNK(st_c2.st_mode) or not stat.S_ISREG(st_c2.st_mode):
            raise VqaError("E_IO", {"path": str(cand2), "reason": "invalid manifest"})
        if not _inside_root(root_real, os.path.realpath(cand2)):
            raise VqaError("E_IO", {"path": str(cand2), "reason": "path outside root"})
        return cand2, root
    raise VqaError("E_IO", {"path": str(d), "reason": "no capture/manifest.json or manifest.json"})


def _resolve(root, manifest_path, rel):
    if not isinstance(rel, str) or not rel or "\x00" in rel or os.path.isabs(rel) or rel.endswith("/"):
        raise VqaError("E_IO", {"path": str(rel), "reason": "invalid path"})
    root, root_real = _check_root(root, reject_all_parent_symlinks=False)
    if root_real is None:
        raise VqaError("E_IO", {"path": str(root), "reason": "invalid root"})
    _mp, mp_parent = _check_manifest_base(root, root_real, manifest_path)
    parts = rel.split("/")
    if parts[-1] in ("", ".", "..") or ".." in parts:
        raise VqaError("E_IO", {"path": str(root / rel), "reason": "invalid path"})
    steps = [s for s in parts if s and s != "."]
    if not steps:
        raise VqaError("E_IO", {"path": str(root / rel), "reason": "invalid path"})
    bases = (root,) if mp_parent == root else (root, mp_parent)
    for base in bases:
        cur = base
        missing_ancestor = False
        for s in steps[:-1]:
            cur = cur / s
            st_cur = _lstat(cur)
            if st_cur is None:
                missing_ancestor = True
                break
            if stat.S_ISLNK(st_cur.st_mode):
                raise VqaError("E_IO", {"path": str(cur), "reason": "symlink refused"})
            if not stat.S_ISDIR(st_cur.st_mode):
                raise VqaError("E_IO", {"path": str(cur), "reason": "non-directory ancestor refused"})
            if not _inside_root(root_real, os.path.realpath(cur)):
                raise VqaError("E_IO", {"path": str(cur), "reason": "path outside root"})
        if missing_ancestor:
            continue
        leaf = cur / steps[-1]
        st_leaf = _lstat(leaf)
        if st_leaf is None:
            continue
        if stat.S_ISLNK(st_leaf.st_mode):
            raise VqaError("E_IO", {"path": str(leaf), "reason": "symlink refused"})
        if not stat.S_ISREG(st_leaf.st_mode):
            raise VqaError("E_IO", {"path": str(leaf), "reason": "nonregular file refused"})
        leaf_real = os.path.realpath(leaf)
        if not _inside_root(root_real, leaf_real):
            raise VqaError("E_IO", {"path": str(leaf), "reason": "path outside root"})
        return base / rel
    raise VqaError("E_IO", {"path": str(root / rel), "reason": "frame file missing"})


def _collect_probe_pngs(root, root_real, sd):
    try:
        entries = sorted(sd.iterdir(), key=lambda p: p.name)
    except OSError as e:
        raise VqaError("E_IO", {"path": str(sd), "reason": str(e)[:80]})
    pngs, manifests = [], []
    for p in entries:
        st_p = _lstat(p)
        if st_p is None:
            raise VqaError("E_IO", {"path": str(p), "reason": "missing entry"})
        if stat.S_ISLNK(st_p.st_mode):
            raise VqaError("E_IO", {"path": str(p), "reason": "symlink refused"})
        if not stat.S_ISREG(st_p.st_mode):
            raise VqaError("E_IO", {"path": str(p), "reason": "nonregular file refused"})
        p_real = os.path.realpath(p)
        if not _inside_root(root_real, p_real):
            raise VqaError("E_IO", {"path": str(p), "reason": "path outside root"})
        if p.name.endswith(".png"):
            pngs.append(p)
        elif p.name in ("manifest.json", "frames.manifest.json") and st_p.st_size > 0:
            manifests.append(p)
    if manifests:
        sub_mp = manifests[0]
        try:
            sub_cap = _load_json(sub_mp)
        except VqaError:
            raise VqaError("E_IO", {"path": str(sub_mp), "reason": "invalid probe manifest"})
        if not isinstance(sub_cap, dict) or not isinstance(sub_cap.get("frames"), list):
            raise VqaError("E_IO", {"path": str(sub_mp), "reason": "invalid probe manifest"})
        resolved_pngs = []
        for k, item in enumerate(sub_cap["frames"]):
            rel = item if isinstance(item, str) else (
                (item.get("file") or {}).get("path") if isinstance(item, dict) and isinstance(item.get("file"), dict)
                else (item.get("file") if isinstance(item, dict) and isinstance(item.get("file"), str)
                      else (item.get("path") if isinstance(item, dict) else None))
            )
            if not isinstance(rel, str):
                raise VqaError("E_IO", {"path": str(sub_mp), "reason": "invalid probe frame[%d]" % k})
            resolved_pngs.append(_resolve(root, sub_mp, rel))
        return resolved_pngs
    return pngs


def _resolve_probe_spec(root, root_real, mp, mp_parent, spec):
    if isinstance(spec, dict):
        if isinstance(spec.get("frames"), list):
            spec = spec["frames"]
        elif "path" in spec:
            spec = spec["path"]
        else:
            raise VqaError("E_IO", {"path": str(mp), "reason": "invalid still_probe spec"})
    if isinstance(spec, list):
        if not spec:
            return []
        pngs = []
        for k, item in enumerate(spec):
            rel = item if isinstance(item, str) else (
                (item.get("file") or {}).get("path") if isinstance(item, dict) and isinstance(item.get("file"), dict)
                else (item.get("file") if isinstance(item, dict) and isinstance(item.get("file"), str)
                      else (item.get("path") if isinstance(item, dict) else None))
            )
            if not isinstance(rel, str):
                raise VqaError("E_IO", {"path": str(mp), "reason": "invalid still_probe frame[%d]" % k})
            pngs.append(_resolve(root, mp, rel))
        return pngs
    if not isinstance(spec, str) or not spec or "\x00" in spec or os.path.isabs(spec):
        raise VqaError("E_IO", {"path": str(spec), "reason": "invalid path"})
    parts = spec.rstrip("/").split("/") if spec.endswith("/") else spec.split("/")
    if not parts or ".." in parts:
        raise VqaError("E_IO", {"path": str(root / spec), "reason": "invalid path"})
    steps = [s for s in parts if s and s != "."]
    if not steps:
        raise VqaError("E_IO", {"path": str(root / spec), "reason": "invalid path"})
    bases = (root,) if mp_parent == root else (root, mp_parent)
    for base in bases:
        cur = base
        missing_ancestor = False
        for s in steps[:-1]:
            cur = cur / s
            st_cur = _lstat(cur)
            if st_cur is None:
                missing_ancestor = True
                break
            if stat.S_ISLNK(st_cur.st_mode):
                raise VqaError("E_IO", {"path": str(cur), "reason": "symlink refused"})
            if not stat.S_ISDIR(st_cur.st_mode):
                raise VqaError("E_IO", {"path": str(cur), "reason": "non-directory ancestor refused"})
            if not _inside_root(root_real, os.path.realpath(cur)):
                raise VqaError("E_IO", {"path": str(cur), "reason": "path outside root"})
        if missing_ancestor:
            continue
        leaf = cur / steps[-1]
        st_leaf = _lstat(leaf)
        if st_leaf is None:
            continue
        if stat.S_ISLNK(st_leaf.st_mode):
            raise VqaError("E_IO", {"path": str(leaf), "reason": "symlink refused"})
        leaf_real = os.path.realpath(leaf)
        if not _inside_root(root_real, leaf_real):
            raise VqaError("E_IO", {"path": str(leaf), "reason": "path outside root"})
        if stat.S_ISDIR(st_leaf.st_mode):
            pngs = _collect_probe_pngs(root, root_real, leaf)
            if not pngs:
                raise VqaError("E_IO", {"path": str(leaf), "reason": "no probe frames"})
            return pngs
        if stat.S_ISREG(st_leaf.st_mode):
            if spec.endswith("/"):
                raise VqaError("E_IO", {"path": str(leaf), "reason": "invalid probe file"})
            if leaf.name in ("manifest.json", "frames.manifest.json"):
                try:
                    sub_cap = _load_json(leaf)
                except VqaError:
                    raise VqaError("E_IO", {"path": str(leaf), "reason": "invalid probe manifest"})
                if not isinstance(sub_cap, dict) or not isinstance(sub_cap.get("frames"), list):
                    raise VqaError("E_IO", {"path": str(leaf), "reason": "invalid probe manifest"})
                return _resolve_probe_spec(root, root_real, leaf, leaf.parent, sub_cap["frames"])
            if not leaf.name.endswith(".png"):
                raise VqaError("E_IO", {"path": str(leaf), "reason": "invalid probe file"})
            return [leaf]
        raise VqaError("E_IO", {"path": str(leaf), "reason": "nonregular file refused"})
    raise VqaError("E_IO", {"path": str(root / spec), "reason": "probe path missing"})


def _sha_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _rect_px(r, W, H):
    """px_q16 [x,y,w,h] -> clipped integer [x,y,w,h] covering the rect (floor origin, ceil far edge) or None."""
    if not isinstance(r, (list, tuple)) or len(r) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in r):
        return None
    x0, y0 = r[0] >> 16, r[1] >> 16
    x1, y1 = -((-(r[0] + r[2])) >> 16), -((-(r[1] + r[3])) >> 16)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def _rows_for(tel, i, W, H):
    out, seen = [], set()
    for kind, key, bkey, role in (("ui", "ui", "rect_px_q16", "ui"), ("actors", "actors", "screen_bounds_px_q16", "actor")):
        items = tel.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict) or it.get("visible") is not True:
                continue
            if kind == "actors" and (it.get("alive") is False or it.get("frustum_inside") is False or it.get("in_front") is False):
                continue
            bb = _rect_px(it.get(bkey), W, H)
            if bb is None:
                continue
            rid = "h:%s:%s" % (role, _slug(it.get("id", ""), 40))
            if rid in seen:
                continue
            seen.add(rid)
            out.append({"i": i, "rid": rid, "role": role, "bbox_px": bb, "z": None})
    return sorted(out, key=lambda r: r["rid"])


class GodotAdapter:
    id = ADAPTER_ID
    version = ADAPTER_VERSION

    def _read(self, run_dir):
        mp, root = _roots(run_dir)
        cap = _load_json(mp)
        if not isinstance(cap, dict) or not isinstance(cap.get("frames"), list) or not cap["frames"]:
            raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(mp), "reason": "no frames"})
        recs = sorted(cap["frames"], key=lambda r: r.get("render_index", -1))
        return mp, root, cap, recs

    def frames(self, run_dir):
        mp, root, cap, recs = self._read(run_dir)
        fps = cap.get("fps") if isinstance(cap.get("fps"), int) and cap["fps"] > 0 else 30
        resolved = []
        for k, r in enumerate(recs):
            ri = r.get("render_index")
            f = r.get("file") or {}
            if not isinstance(ri, int) or isinstance(ri, bool) or ri < 0 or not isinstance(f.get("path"), str):
                raise VqaError("E_VC_FRAME_SCHEMA", {"field": "frames[%d]" % k})
            resolved.append((k, r, ri, f, _resolve(root, mp, f["path"])))
        frames, entries, W = [], [], None
        for k, r, ri, f, p in resolved:
            fsha = _sha_file(p)
            if f.get("sha256") not in (None, fsha):
                raise VqaError("E_VC_FRAME_SHA", {"i": k, "reason": "file sha256"})
            rgb = read_png(p)
            fr = vframe.Frame(k, ri * 1000000 // fps, ri, rgb, r.get("decoded_rgb_sha256"))
            if W is None:
                W = rgb.shape[1], rgb.shape[0]
            elif (rgb.shape[1], rgb.shape[0]) != W:
                raise VqaError("E_VC_FRAME_SIZE", {"i": k, "shape": list(rgb.shape[:2])})
            frames.append(fr)
            entries.append({"i": k, "t_us": fr.t_us, "render_index": ri, "file": os.path.relpath(p, mp.parent),
                            "sha256": fr.sha256, "file_sha256": fsha})
        meta = {"schema": vframe.SCHEMA, "width": W[0], "height": W[1], "channels": 3, "pixel_format": "rgb24",
                "fps_num": fps, "fps_den": 1, "count": len(frames), "storage": "png_dir", "raw_path": None,
                "frame_bytes": W[0] * W[1] * 3, "frames": entries, "hints": "region_hints.jsonl",
                "still_probe": None, "scope": self.scope(run_dir),
                "provenance": {"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
                               "capture_receipt_sha256": _sha_file(mp)}}
        vframe.validate_manifest(meta)
        return vframe.MemoryFrameSource(frames, meta)

    def hints(self, run_dir):
        mp, root, cap, recs = self._read(run_dir)
        tp = (cap.get("telemetry") or {}).get("path", "capture/telemetry.jsonl")
        try:
            tpath = _resolve(root, mp, tp)
        except VqaError as e:
            if e.detail.get("reason") != "frame file missing":
                raise
            try:
                tpath = _resolve(root, mp, "telemetry.jsonl")
            except VqaError as e2:
                if e2.detail.get("reason") != "frame file missing":
                    raise
                return None
        by_ri = {}
        with open(tpath, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        raise VqaError("E_VC_FRAME_SCHEMA", {"path": str(tpath), "line": n})
                    if isinstance(row, dict) and isinstance(row.get("render_index"), int):
                        by_ri[row["render_index"]] = row
        W = H = None
        rows = []
        vp = cap.get("viewport_px")
        if isinstance(vp, list) and len(vp) == 2:
            W, H = vp
        for k, r in enumerate(recs):
            tel = by_ri.get(r.get("render_index"))
            if tel is None:
                continue
            w, h = (W, H) if W else tuple(tel.get("viewport_px", (0, 0)))
            if w and h:
                rows += _rows_for(tel, k, w, h)
        return iter(rows)

    def still_probe(self, run_dir):
        d, d_real = _check_root(run_dir, reject_all_parent_symlinks=True)
        if d_real is None:
            return None
        root, root_real, mp, mp_parent = d, d_real, None, d
        cap_dir = d / "capture"
        st_cap = _lstat(cap_dir)
        if st_cap is not None:
            if stat.S_ISLNK(st_cap.st_mode) or not stat.S_ISDIR(st_cap.st_mode):
                raise VqaError("E_IO", {"path": str(cap_dir), "reason": "invalid capture directory"})
            if not _inside_root(d_real, os.path.realpath(cap_dir)):
                raise VqaError("E_IO", {"path": str(cap_dir), "reason": "path outside root"})
            cm = cap_dir / "manifest.json"
            st_cm = _lstat(cm)
            if st_cm is not None:
                if stat.S_ISLNK(st_cm.st_mode) or not stat.S_ISREG(st_cm.st_mode):
                    raise VqaError("E_IO", {"path": str(cm), "reason": "invalid manifest"})
                if not _inside_root(d_real, os.path.realpath(cm)):
                    raise VqaError("E_IO", {"path": str(cm), "reason": "path outside root"})
                mp, mp_parent = cm, cap_dir
        if mp is None:
            dm = d / "manifest.json"
            st_dm = _lstat(dm)
            if st_dm is not None:
                if d.name == "capture":
                    root, root_real = _check_root(d.parent, reject_all_parent_symlinks=True)
                    if root_real is None or not _inside_root(root_real, d_real):
                        raise VqaError("E_IO", {"path": str(d), "reason": "escaping ancestor"})
                if stat.S_ISLNK(st_dm.st_mode) or not stat.S_ISREG(st_dm.st_mode):
                    raise VqaError("E_IO", {"path": str(dm), "reason": "invalid manifest"})
                if not _inside_root(root_real, os.path.realpath(dm)):
                    raise VqaError("E_IO", {"path": str(dm), "reason": "path outside root"})
                mp, mp_parent = dm, d
        pngs = None
        if mp is not None and _lstat(mp).st_size > 0:
            cap = _load_json(mp)
            if isinstance(cap, dict):
                for mkey in ("still_probe", "probe"):
                    if mkey in cap and cap[mkey] is not None:
                        pngs = _resolve_probe_spec(root, root_real, mp, mp_parent, cap[mkey])
                        break
        if pngs is None:
            bases = (root,) if mp_parent == root else (root, mp_parent)
            for sub in ("still_probe", "probe"):
                for base in bases:
                    sd = base / sub
                    st_sd = _lstat(sd)
                    if st_sd is None:
                        continue
                    if stat.S_ISLNK(st_sd.st_mode):
                        raise VqaError("E_IO", {"path": str(sd), "reason": "symlink refused"})
                    if not stat.S_ISDIR(st_sd.st_mode):
                        raise VqaError("E_IO", {"path": str(sd), "reason": "not a directory"})
                    sd_real = os.path.realpath(sd)
                    if not _inside_root(root_real, sd_real):
                        raise VqaError("E_IO", {"path": str(sd), "reason": "path outside root"})
                    cand_pngs = _collect_probe_pngs(root, root_real, sd)
                    if cand_pngs:
                        pngs = cand_pngs
                        break
                if pngs:
                    break
        if not pngs:
            return None
        frames = []
        for k, p in enumerate(pngs):
            frames.append(vframe.Frame(k, k * 33333, None, read_png(p)))
        h, w = frames[0].rgb.shape[:2]
        meta = {"schema": vframe.SCHEMA, "width": w, "height": h, "channels": 3, "pixel_format": "rgb24",
                "storage": "png_dir", "count": len(frames)}
        return vframe.MemoryFrameSource(frames, meta)

    def scope(self, run_dir):
        mp, root, cap, recs = self._read(run_dir)
        bs = cap.get("build_seq")
        return {"app": _slug(cap.get("adapter_id", "godot"), 32), "scene": _slug(cap.get("ruleset_id", "unknown"), 48),
                "res": "%dx%d" % tuple(cap.get("viewport_px") or (0, 0)),
                "build_seq": bs if isinstance(bs, int) and not isinstance(bs, bool) and bs >= 0 else None}


def convert(in_dir, out_dir):
    """Write out_dir/frames.manifest.json (+ region_hints.jsonl when telemetry has rects). All-or-nothing: on error
    nothing is written."""
    a = GodotAdapter()
    src = a.frames(in_dir)
    hints = a.hints(in_dir)
    rows = list(hints) if hints is not None else None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = dict(src.meta)
    # frame files are referenced relative to the out dir
    mp, _root = _roots(in_dir)
    for e in meta["frames"]:
        e["file"] = os.path.relpath((mp.parent / e["file"]).resolve(), out.resolve())
    if rows is None:
        meta["hints"] = None
    else:
        canon.write_atomic(out / "region_hints.jsonl", "".join(canon.dumps(r) for r in rows).encode("utf-8"))
    vframe.write_manifest(out / "frames.manifest.json", meta)
    return {"code": "OK", "rc": 0, "detail": {"frames": len(src), "hints": None if rows is None else len(rows),
                                              "manifest_sha256": _sha_file(out / "frames.manifest.json")}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m vadapt.godot.from_capture_dir")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        sys.stdout.write(result_line(result_obj("E_USAGE")))
        return 2
    try:
        res = convert(ns.inp, ns.out)
    except VqaError as e:
        sys.stdout.write(result_line(e.to_obj()))
        return e.rc
    sys.stdout.write(result_line(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
