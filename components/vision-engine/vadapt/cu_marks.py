# @module {"id":"va.cu_marks","layer":4,"owner":"ARCH","deps":[],"book":"HB-VC-s2-change"}
"""Deterministic UI edge islands and RAM-only set-of-marks rendering.

Reuse the existing V2 edge-map kernel, without importing higher core layers.
S4 pair regions require a previous frame and tile-quantise adjacent controls;
pixel edge islands preserve the gaps between their borders. Shapes are visual
candidates, not semantic or authoritative evidence of interactivity.
PNG filter-0 encoding and the 3x5 digits follow visioncore.pack's format; the
renderer here uses stdlib only and keeps the sealed pack implementation intact.
"""
import os
from pathlib import Path
import struct
import tempfile
import zlib

import numpy as np
from eval.v2x.st_edgemap import _compute_edge_map


def _islands(edge):
    """Run-length connected components; work in Python scales with edge runs."""
    parent, bounds, previous = [], [], []

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for y, row in enumerate(edge):
        transitions = np.flatnonzero(np.diff(np.r_[False, row != 0, False]))
        current = []
        j = 0
        for start, stop in zip(transitions[::2], transitions[1::2]):
            start, stop = int(start), int(stop)
            idx = len(parent)
            parent.append(idx)
            bounds.append([start, y, stop, y + 1])
            while j < len(previous) and previous[j][1] < start:
                j += 1
            k = j
            while k < len(previous) and previous[k][0] <= stop:
                other = root(previous[k][2])
                here = root(idx)
                if other != here:
                    parent[here] = other
                k += 1
            current.append((start, stop, idx))
        previous = current
    merged = {}
    for idx, (x, y, right, bottom) in enumerate(bounds):
        key = root(idx)
        if key not in merged:
            merged[key] = [x, y, right, bottom]
        else:
            b = merged[key]
            b[:] = min(b[0], x), min(b[1], y), max(b[2], right), max(b[3], bottom)
    return list(merged.values())


def candidates(rgb, *, extra=False):  # @anchor:va.cu_marks.candidates
    """Return at most 24 raster-numbered pixel boxes; no model or randomness."""
    height, width, _ = rgb.shape
    # Same integer YCoCg-R luma lift as the engine descriptor.
    a = rgb.astype(np.int16)
    co = a[..., 0] - a[..., 2]
    temp = a[..., 2] + (co >> 1)
    luma = temp + ((a[..., 1] - temp) >> 1)
    edge = _compute_edge_map(luma)
    boxes = []
    for left, top, right, bottom in _islands(edge):
        # Kernel differences are located one pixel before the boundary.
        x, y = max(0, left), max(0, top)
        right, bottom = min(width, right + 1), min(height, bottom + 1)
        w, h = right - x, bottom - y
        if w < 12 or h < 10 or w > width * 3 // 4 or h > height // 3:
            continue
        # Border evidence outranks text fragments; it is not confidence that
        # the application will accept a click or reach the requested goal.
        rim = (np.count_nonzero(np.any(edge[top:min(top+2, bottom), left:right], axis=0))
               + np.count_nonzero(np.any(edge[max(top, bottom-3):bottom, left:right], axis=0))
               + np.count_nonzero(np.any(edge[top:bottom, left:min(left+2, right)], axis=1))
               + np.count_nonzero(np.any(edge[top:bottom, max(left, right-3):right], axis=1)))
        rim_int = int(rim)
        rim_promille = min(1000, (1000 * rim_int) // (2 * (w + h)))
        score = round(min(1.0, rim_int / (2 * (w + h))), 6)
        sub_edge = edge[y+2:y+h-3, x+2:x+w-3]
        sub_luma = luma[y+2:y+h-3, x+2:x+w-3]
        area = (h - 5) * (w - 5)
        interior = (1000 * int(np.count_nonzero(sub_edge))) // area
        horizontal = (1000 * int(np.count_nonzero(
            np.abs(sub_luma[1:, :] - sub_luma[:-1, :]) > 24))) // area
        vertical = (1000 * int(np.count_nonzero(
            np.abs(sub_luma[:, 1:] - sub_luma[:, :-1]) > 24))) // area
        kind = _classify_kind(w, h, rim_promille, interior, horizontal, vertical)
        box = dict(x=int(x), y=int(y), w=int(w), h=int(h), kind=kind, score=score)
        if extra:
            box['rim_promille'] = int(rim_promille)
        boxes.append(box)
    # Suppress glyph/text islands enclosed by a control outline. Retain
    # separately bordered adjacent controls even when their padding touches.
    boxes = [b for b in boxes if not any(
        a is not b and a['score'] >= 0.5
        and a['x'] <= b['x'] and a['y'] <= b['y']
        and a['x'] + a['w'] >= b['x'] + b['w']
        and a['y'] + a['h'] >= b['y'] + b['h']
        and a['w'] * a['h'] > b['w'] * b['h'] for a in boxes)]
    boxes.sort(key=lambda b: (-b['score'], b['y'], b['x'], b['h'], b['w']))
    boxes = boxes[:24]
    boxes.sort(key=lambda b: (b['y'], b['x'], b['h'], b['w']))
    return [dict(id=i, **b) for i, b in enumerate(boxes, 1)]


def annotate(rgb, marks):  # @anchor:va.cu_marks.annotate
    """Return a PNG with black/yellow outlines and white-on-black digits."""
    height, width, _ = rgb.shape
    pixels = bytearray(rgb.tobytes())
    digits = ('111101101101111', '010110010010111', '111001111100111',
              '111001111001111', '101101111001001', '111100111001111',
              '111100111101111', '111001001010010', '111101111101111',
              '111101111001111')

    def paint(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset:offset+3] = color

    corners = _badge_corners(marks)
    for idx, mark in enumerate(marks):
        x, y, w, h = (mark[k] for k in ('x', 'y', 'w', 'h'))
        for inset, color in ((0, b'\0\0\0'), (1, b'\xff\xff\0')):
            for xx in range(x+inset, x+w-inset):
                paint(xx, y+inset, color)
                paint(xx, y+h-1-inset, color)
            for yy in range(y+inset, y+h-inset):
                paint(x+inset, yy, color)
                paint(x+w-1-inset, yy, color)
        text = str(mark['id'])
        label_w = 4 * len(text) + 2
        left = x + w - label_w if corners[idx] == 'top-right' else x
        lx, ly = min(max(0, left), max(0, width-label_w)), min(y, max(0, height-7))
        for yy in range(7):
            for xx in range(label_w):
                paint(lx+xx, ly+yy, b'\0\0\0')
        for i, digit in enumerate(text):
            for offset, bit in enumerate(digits[int(digit)]):
                if bit == '1':
                    paint(lx+1+i*4+offset % 3, ly+1+offset // 3, b'\xff\xff\xff')

    return _encode_png(width, height, pixels)


def _encode_png(width, height, pixels):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    raw = b''.join(b'\0' + pixels[y*width*3:(y+1)*width*3] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def write_marks(path, rgb, marks):  # @anchor:va.cu_marks.write_marks
    """Validate resolved RAM destination and atomically replace only on success."""
    destination = Path(path).resolve()
    if not destination.is_relative_to('/dev/shm/vqa-cu'):
        raise ValueError('marks-out must be under /dev/shm/vqa-cu')
    payload = annotate(rgb, marks)
    # Parent creation is the caller's responsibility, as for other C2 inputs.
    fd, temporary = tempfile.mkstemp(prefix='.cu-marks-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _classify_kind(w, h, rim_promille, in_promille, hin_promille, vin_promille):
    if rim_promille >= 500:
        if max(w, h) <= 24 and 3 * min(w, h) >= 2 * max(w, h):
            return 'icon'
        if w >= 6 * h:
            return 'field' if in_promille <= 40 else 'row'
        if 3 * h <= w < 6 * h and h <= 28:
            return 'tab'
        return 'button'
    if in_promille >= 100 and (vin_promille >= hin_promille or in_promille >= 180):
        return 'text'
    if w >= 6 * h and rim_promille >= 200:
        return 'row'
    return 'unknown'


def _badge_corners(marks):
    indexed = sorted(enumerate(marks), key=lambda item: (
        item[1]['y'] + item[1]['h'] // 2, item[1]['y'], item[1]['x'], item[0]))
    bands = []
    for idx, mark in indexed:
        cy = mark['y'] + mark['h'] // 2
        if not bands or cy - bands[-1][0] > 12:
            bands.append([cy, [idx]])
        else:
            prev_cy, members = bands[-1]
            members.append(idx)
            bands[-1][0] = (prev_cy * (len(members) - 1) + cy) // len(members)
    corners = ['top-left'] * len(marks)
    i = 0
    while i < len(bands):
        j = i
        while j + 1 < len(bands) and 0 < bands[j+1][0] - bands[j][0] <= 32:
            j += 1
        if j > i:
            for offset, (_, members) in enumerate(bands[i:j+1]):
                for idx in members:
                    corners[idx] = 'top-right' if offset % 2 else 'top-left'
        i = j + 1
    return corners


def validate_ram_path(path, *, is_dir=False):  # @anchor:va.cu_marks.validate_ram_path
    """Reject missing, escaped or unsuitable output destinations before writes."""
    dest = Path(path).resolve()
    valid = dest.is_dir() if is_dir else dest.parent.is_dir() and not dest.is_dir()
    if not dest.is_relative_to('/dev/shm/vqa-cu') or not valid:
        label = 'crop-dir must be an existing directory' if is_dir else 'marks-out must be a file'
        raise ValueError(label + ' under /dev/shm/vqa-cu')
    return dest


def write_crops(crop_dir, rgb, crops):  # @anchor:va.cu_marks.write_crops
    """Stage filter-0 RGB crops in RAM, then atomically replace each target."""
    dest = validate_ram_path(crop_dir, is_dir=True)
    targets = [dest / ('crop_%d.png' % i) for i in range(len(crops))]
    for target in targets:
        validate_ram_path(target)
    staged = []
    try:
        for crop, target in zip(crops, targets):
            x, y, w, h = (crop[k] for k in ('x', 'y', 'w', 'h'))
            payload = _encode_png(w, h, rgb[y:y+h, x:x+w].tobytes())
            fd, temporary = tempfile.mkstemp(prefix='.cu-crop-', dir=dest)
            staged.append((temporary, target))
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return [str(target) for target in targets]
