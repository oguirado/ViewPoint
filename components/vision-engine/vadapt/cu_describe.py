# @module {"id":"va.cu_describe","layer":4,"owner":"ARCH","deps":["vc.decode","vc.errors","va.cu_regions","va.cu_marks","va.cu_ocr"],"book":"HB-VC-s2-change"}
"""C2 PNG/raw/ring pair description. Optional RAM-only marks; stdout is one canonical JSON line.

[TARGET, unit decision] Raw RGB (`--frame-raw <shm-path>,<w>,<h>`), seqlock ring (`--ring <path>,<index>`),
and uint8[H,W,3] array inputs; expected gain [TARGET]: remove 2x PNG zlib round-trips per tick.
Masks use nonnegative origins and positive extents, clipped to the frame; crops use changed regions only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import time
import zlib
import numpy as np
from visioncore.decode import png
from visioncore.errors import VqaError
from vnative.adapters.cu_regions import pair_regions, pair_regions_plus
from vadapt import ramws, rtring
from vadapt.cu_ocr import annotate_marks as ocr_annotate_marks
from vadapt.cu_marks import candidates, validate_ram_path, write_crops, write_marks
def _rect(text):
    try:
        x, y, w, h = map(int, text.split(','))
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise ValueError
        return x, y, w, h
    except ValueError:
        raise argparse.ArgumentTypeError('mask must be x,y,w,h with x,y >= 0 and w,h > 0') from None
def describe(frame=None, prev=None, masks=(), marks_out=None, *, s4_attrs=False, marks_extra=False, crop_dir=None, ocr_marks=False, tessdata_dir=None, frame_raw=None, prev_raw=None, ring=None, prev_ring=None):  # @anchor:va.cu_describe.describe
    """Decode PNGs and return C2 data; elapsed time includes decoding and hashing."""
    if ocr_marks and marks_out is None:
        raise ValueError('--ocr-marks requires --marks-out')
    start = time.perf_counter_ns()
    if marks_out is not None:
        validate_ram_path(marks_out)
    if crop_dir is not None:
        validate_ram_path(crop_dir, is_dir=True)
    current = _resolve_frame(frame, frame_raw, ring, 'frame')
    height, width, _ = current.shape
    digest = hashlib.sha256(current.tobytes()).hexdigest()
    found = []
    s4_adjacency = []
    if prev is not None or prev_raw is not None or prev_ring is not None:
        previous = _resolve_frame(prev, prev_raw, prev_ring, 'prev')
        if previous.shape != current.shape:
            raise ValueError('frame and prev dimensions differ')
        compared = current.copy() if masks else current
        for x, y, w, h in masks:
            compared[y:y+h, x:x+w] = previous[y:y+h, x:x+w]
        if s4_attrs:
            records, raw_adj = pair_regions_plus(previous, compared)
            s4_adjacency = [[str(a), str(b), int(c)] for a, b, c in raw_adj]
        else:
            records = pair_regions(previous, compared)
        dirty_tiles = sum(rec['n_tiles'] for rec in records)
        for rec in records:
            x, y, w, h = (int(v) for v in rec['bbox_px'])
            # Score is the region's share of dirty tiles; ties use raster order.
            entry = dict(x=x, y=y, w=w, h=h, score=float(rec['n_tiles']) / dirty_tiles)
            if s4_attrs:
                entry['s4'] = dict(
                    group=str(rec['group']), cls=str(rec['cls']), kind=str(rec['kind']),
                    mv=[int(v) for v in rec['mv']], edge_ppm=int(rec['edge_ppm']),
                    r_max=int(rec['r_max']), n_tiles=int(rec['n_tiles']),
                    shade_only=bool(rec['shade_only']))
            found.append(entry)
    found.sort(key=lambda r: (-r['score'], r['y'], r['x'], r['h'], r['w']))
    found = found[:16]
    crops = [dict(x=r['x'], y=r['y'], w=r['w'], h=r['h'], reason='changed')
             for r in found[:8]]
    result = dict(schema='ve.cu_describe/v1', frame_sha256=digest,
                width=int(width), height=int(height),
                changed=None if (prev is None and prev_raw is None and prev_ring is None) else bool(found),
                changed_regions=found, crops=crops,
                timing_us=(time.perf_counter_ns() - start) // 1000)
    if s4_attrs:
        result['s4_adjacency'] = s4_adjacency
    if crop_dir is not None:
        result.update(crop_dir=str(crop_dir), crop_pngs=write_crops(crop_dir, current, crops))
        result['timing_us'] = (time.perf_counter_ns() - start) // 1000
    if marks_out is not None:
        marks = candidates(current, extra=bool(marks_extra or s4_attrs))
        write_marks(marks_out, current, marks)
        if ocr_marks:
            marks = ocr_annotate_marks(current, marks, tessdata_dir=tessdata_dir, clean_sha256=digest)
        result.update(marks=marks, marks_png=str(marks_out))
        result['timing_us'] = (time.perf_counter_ns() - start) // 1000
    return result


def main(argv=None):  # @anchor:va.cu_describe.main
    """Exit 0 on C2 success; invalid arguments and invalid PNGs exit 2."""
    parser = argparse.ArgumentParser(description=__doc__)
    frame_group = parser.add_mutually_exclusive_group(required=True)
    frame_group.add_argument('--frame')
    frame_group.add_argument('--frame-raw')
    frame_group.add_argument('--ring')
    prev_group = parser.add_mutually_exclusive_group()
    prev_group.add_argument('--prev')
    prev_group.add_argument('--prev-raw')
    prev_group.add_argument('--prev-ring')
    parser.add_argument('--s4-attrs', action='store_true')
    parser.add_argument('--marks-extra', action='store_true')
    parser.add_argument('--crop-dir')
    parser.add_argument('--marks-out')
    parser.add_argument('--ocr-marks', action='store_true')
    parser.add_argument('--mask', nargs='+', action='extend', type=_rect, default=[])
    parser.add_argument('--json', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        result = describe(args.frame, args.prev, args.mask, args.marks_out,
                          s4_attrs=args.s4_attrs, marks_extra=args.marks_extra,
                          crop_dir=args.crop_dir, ocr_marks=args.ocr_marks,
                          frame_raw=args.frame_raw, prev_raw=args.prev_raw,
                          ring=args.ring, prev_ring=args.prev_ring)
    except (VqaError, OSError, ValueError, struct.error, zlib.error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
    return 0


class _RawDescribeError(VqaError, ValueError):
    """Typed C2 input error carrying both VqaError code/rc and ValueError compatibility."""


def _is_decimal_int(text):
    if not isinstance(text, str) or not text:
        return False
    return text.isdigit() or (text[0] == '-' and len(text) > 1 and text[1:].isdigit())


def _check_shm_path(raw_path, field='frame_raw'):
    if type(raw_path) is not str or not raw_path or '\0' in raw_path or not os.path.isabs(raw_path):
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(raw_path), 'reason': 'absolute /dev/shm path required'})
    resolved = Path(raw_path).resolve()
    if '..' in raw_path.split('/') or '.' in raw_path.split('/') or not raw_path.startswith('/dev/shm/') or not resolved.is_relative_to('/dev/shm') or resolved == Path('/dev/shm'):
        raise _RawDescribeError('E_IO', {'field': field, 'path': raw_path, 'reason': '/dev/shm confinement or path traversal refused'})
    if os.path.islink(raw_path) or any(p.is_symlink() for p in Path(raw_path).parents if str(p).startswith('/dev/shm/')):
        raise _RawDescribeError('E_IO', {'field': field, 'path': raw_path, 'reason': 'symlink refused'})
    if ramws.is_ram_fs(resolved) not in ramws.RAM_FS:
        raise _RawDescribeError('E_IO', {'field': field, 'path': raw_path, 'reason': 'path must reside on verified /dev/shm tmpfs'})
    return resolved


def _coerce_rgb(arr):
    if not isinstance(arr, np.ndarray):
        raise _RawDescribeError('E_VC_FRAME_SHAPE', {'reason': 'expected numpy.ndarray'})
    if arr.dtype != np.uint8 or arr.ndim != 3 or arr.shape[2] != 3:
        raise _RawDescribeError('E_VC_FRAME_SHAPE', {'reason': 'expected uint8[H,W,3] array'})
    height, width, _ = arr.shape
    if not (1 <= int(width) <= 8192 and 1 <= int(height) <= 8192):
        raise _RawDescribeError('E_VC_FRAME_SHAPE', {'reason': f'dimensions {width}x{height} out of 1..8192'})
    return arr if arr.flags.c_contiguous else np.ascontiguousarray(arr)


def _load_raw_rgb(spec, *, field='frame_raw'):
    """Load an exact-sized uint8[H,W,3] raw RGB24 frame from a confined /dev/shm file."""
    if isinstance(spec, str):
        parts = spec.rsplit(',', 2)
        if len(parts) != 3 or not (_is_decimal_int(parts[1]) and _is_decimal_int(parts[2])):
            raise _RawDescribeError('E_USAGE', {'field': field, 'reason': 'expected <shm-path>,<w>,<h>'})
        raw_path, width, height = parts[0], int(parts[1]), int(parts[2])
    elif isinstance(spec, (tuple, list)) and len(spec) == 3:
        raw_path, width, height = spec
    elif isinstance(spec, dict):
        raw_path = spec.get('path', spec.get('frame_raw'))
        width = spec.get('width', spec.get('w'))
        height = spec.get('height', spec.get('h'))
    else:
        raise _RawDescribeError('E_USAGE', {'field': field, 'reason': 'expected <shm-path>,<w>,<h>'})
    if type(width) is not int or type(height) is not int or not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise _RawDescribeError('E_VC_FRAME_SHAPE', {'field': field, 'reason': 'width and height must be ints in 1..8192'})
    _check_shm_path(raw_path, field=field)
    want = width * height * 3
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(raw_path, flags)
    except OSError as exc:
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(raw_path), 'reason': str(exc)[:80]}) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _RawDescribeError('E_IO', {'field': field, 'path': str(raw_path), 'reason': 'regular file required'})
        if st.st_size != want:
            raise _RawDescribeError('E_VC_FRAME_SIZE', {'field': field, 'path': str(raw_path), 'bytes': int(st.st_size), 'expected': want})
        chunks, remaining = [], want
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        buf = b''.join(chunks)
    except OSError as exc:
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(raw_path), 'reason': str(exc)[:80]}) from None
    finally:
        os.close(fd)
    if len(buf) != want:
        raise _RawDescribeError('E_VC_FRAME_SIZE', {'field': field, 'path': str(raw_path), 'bytes': len(buf), 'expected': want})
    return np.frombuffer(buf, dtype=np.uint8).copy().reshape(height, width, 3)


def _load_ring_rgb(spec, *, field='ring'):
    """Read a seqlock-verified uint8[H,W,3] RGB24 slot from a /dev/shm RingReader."""
    if isinstance(spec, str):
        parts = spec.rsplit(',', 1)
        if len(parts) != 2 or not _is_decimal_int(parts[1]):
            raise _RawDescribeError('E_USAGE', {'field': field, 'reason': 'expected <ring-path>,<index>'})
        ring_path, index = parts[0], int(parts[1])
    elif isinstance(spec, (tuple, list)) and len(spec) == 2:
        ring_path, index = spec
    elif isinstance(spec, dict):
        ring_path = spec.get('path', spec.get('ring'))
        index = spec.get('index')
    else:
        raise _RawDescribeError('E_USAGE', {'field': field, 'reason': 'expected <ring-path>,<index>'})
    if type(index) is not int or not (0 <= index < (1 << 63)):
        raise _RawDescribeError('E_USAGE', {'field': field, 'reason': 'ring index must be a non-negative integer'})
    _check_shm_path(ring_path, field=field)
    try:
        st = os.lstat(ring_path)
    except OSError as exc:
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(ring_path), 'reason': str(exc)[:80]}) from None
    if not stat.S_ISREG(st.st_mode):
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(ring_path), 'reason': 'regular file required'})
    try:
        reader = rtring.RingReader(ring_path, root='/dev/shm', verify=True)
    except (OSError, ValueError, struct.error, ramws.RamError) as exc:
        raise _RawDescribeError('E_IO', {'field': field, 'path': str(ring_path), 'reason': str(exc)[:80]}) from None
    frame_slot = None
    try:
        width, height = int(reader.width), int(reader.height)
        if not (1 <= width <= 8192 and 1 <= height <= 8192):
            raise _RawDescribeError('E_VC_FRAME_SHAPE', {'field': field, 'path': str(ring_path), 'width': width, 'height': height})
        if int(reader.slot_bytes) != width * height * 3:
            raise _RawDescribeError('E_VC_FRAME_SIZE', {'field': field, 'path': str(ring_path), 'bytes': int(reader.slot_bytes), 'expected': width * height * 3})
        frame_slot = reader.acquire(index)
        if frame_slot is None:
            raise _RawDescribeError('E_STALE', {'field': field, 'path': str(ring_path), 'index': index})
        rgb = np.array(frame_slot.view, dtype=np.uint8, copy=True)
        valid = frame_slot.valid()
        del frame_slot
        frame_slot = None
        if not valid:
            raise _RawDescribeError('E_STALE', {'field': field, 'path': str(ring_path), 'index': index})
        return rgb
    finally:
        frame_slot = None
        reader.close()


def _resolve_frame(src, src_raw, src_ring, label):
    present = sum(x is not None for x in (src, src_raw, src_ring))
    if present == 0:
        raise _RawDescribeError('E_USAGE', {'field': label, 'reason': f'missing {label} input'})
    if present > 1:
        raise _RawDescribeError('E_USAGE', {'field': label, 'reason': f'mutually exclusive {label} inputs'})
    if src_raw is not None:
        return _load_raw_rgb(src_raw, field=f'{label}_raw')
    if src_ring is not None:
        return _load_ring_rgb(src_ring, field='ring' if label == 'frame' else 'prev_ring')
    if isinstance(src, np.ndarray):
        return _coerce_rgb(src)
    if isinstance(src, rtring.Frame):
        if not src.valid():
            raise _RawDescribeError('E_STALE', {'field': label, 'reason': 'ring frame view is stale'})
        rgb = np.array(src.view, dtype=np.uint8, copy=True)
        if not src.valid():
            raise _RawDescribeError('E_STALE', {'field': label, 'reason': 'ring frame overwritten during read'})
        return _coerce_rgb(rgb)
    if isinstance(src, dict):
        if 'index' in src or 'ring' in src:
            return _load_ring_rgb(src, field='ring' if label == 'frame' else 'prev_ring')
        return _load_raw_rgb(src, field=f'{label}_raw')
    if isinstance(src, str) and ',' in src and not os.path.exists(src):
        parts3 = src.rsplit(',', 2)
        if len(parts3) == 3 and _is_decimal_int(parts3[1]) and _is_decimal_int(parts3[2]):
            return _load_raw_rgb(src, field=f'{label}_raw')
        parts2 = src.rsplit(',', 1)
        if len(parts2) == 2 and _is_decimal_int(parts2[1]):
            return _load_ring_rgb(src, field='ring' if label == 'frame' else 'prev_ring')
    return png(src)


if __name__ == '__main__':
    sys.exit(main())
