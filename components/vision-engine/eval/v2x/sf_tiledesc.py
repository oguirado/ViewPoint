"""Screen identity from S1 layout bits and a phase-covered exact tile bag.

Source binding: field references below are to the unmodified source read at
``bridge/integration``. ``Grid.W/H/s/gw/gh`` are assigned at
visioncore/grid.py:13 and ``Grid.tx/ty`` map Morton ranks to spatial cells at
visioncore/grid.py:24-25. The grid size rule is visioncore/layout.py:15-25;
``describe`` validates the RGB array and edge-pads it at
visioncore/desc.py:60-64. Descriptor field names and wire offsets are declared
at visioncore/layout.py:95-98. This variant reads exactly these S1 fields:

* ``y_q4``: floor of sixteen times mean tile luma, visioncore/desc.py:193.
* ``edge``: floor of 255 times the fraction of pixels exceeding the engine's
  gradient cutoff, visioncore/desc.py:165-170,199.
* ``xh64``: exact BLAKE2b-64 over padded RGB tile bytes, personalized with
  ``vqa-tile``, visioncore/desc.py:54-57,101-109,141-145.

Layout design and constants
---------------------------
``_COARSE_COLS=8`` and ``_COARSE_ROWS=6`` give 48 spatial cells: coarse enough
to dilute local caret edits, with separate horizontal and vertical structure.
Each tile's position selects a cell using the integer grid coordinates above;
cell means weight the descriptors equally, including original edge tiles.
Empty cells of tiny frames encode zero. To achieve invariance under small global
brightness shifts (+1 or -1 offset) while separating extreme screens (e.g., solid
black vs solid white), tile lumas ``y_q4`` are centered relative to the global mean
tile luma ``mean(y_q4)`` prior to cell pooling, and a global luma thermometer code
is appended to the layout bitstring.
``_LUMA_STEP_Q4=32`` quantizes mean luma in two-grey-level increments,
while ``_EDGE_STEP=64`` quantizes edge density in approximately quarter-density
increments. Global mean luma is quantized in 8-grey-level increments (``_GLOBAL_LUMA_STEP_Q4=128``)
into 31 thermometer levels. The coarser edge and global luma bins keep small AA-induced
changes and minor brightness shifts from overwhelming the stable spatial layout code.
Unary (thermometer) encoding makes the XOR population count equal to the absolute
quantized difference, avoiding large binary-code changes at a single quantization boundary.
``_LUMA_MAX_Q4=4080`` is 16*255; ``_EDGE_MAX=255`` is the engine edge scale.
The derived ``_LUMA_LEVELS=127``, ``_EDGE_LEVELS=3``, ``_CELL_BITS=130``,
``_GLOBAL_BITS=31``, ``_LAYOUT_BITS=6271`` and ``_LAYOUT_BYTES=784`` define the packed layout code.

Exact bag and comparison
------------------------
The bag is the sorted set of original ``xh64`` values plus tiles from every
horizontal and vertical phase ``1..grid.s-1`` that fits the frame. It has no
scene IDs, seeds, split labels, pixel samples or approximate hashes. A plain
single-grid exact bag loses almost all overlap after a sub-tile scroll. This
phase coverage preserves some complete tile windows for any one-axis offset,
including offsets spanning several tiles. Each shifted view is edge-completed
in a temporary RAM array to retain the *original* Grid and tile size; auxiliary
tiles crossing the shifted valid edge are excluded. Two-axis phase combinations
are intentionally absent, so global diagonal shifts have less exact support.

``_BAG_SCALE=1000`` scales set-Dice dissimilarity to integer thousandths:
floor(1000 * (|A|+|B|-2*|A intersect B|) / (|A|+|B|)). Set rather than
multiplicity overlap prevents a repeated flat background from dominating it.
Distance is this term plus the layout XOR population count. ``SF_THRESHOLD=
1002`` admits complete exact-hash turnover with up to two changed layout bits
(a small allowance for pooled AA perturbations), or proportionately more layout
change when exact support survives. These are fixed, global constants, never
chosen using scene metadata. This heuristic is symmetric and nonnegative; it
is not a mathematical metric and does not establish semantic identity.

Serialization and storage
-------------------------
The returned fingerprint is immutable ``bytes``. ``_MAGIC=b'SFTD001\\0'`` is
the eight-byte format/version tag. ``_HEADER`` uses ``'<8sHHHI'``: that tag,
width, height and tile size as little-endian uint16, then set size as uint32.
The layout follows, then little-endian uint64 hashes (eight bytes each).
``_DIMENSION_DISTANCE=_BAG_SCALE+_LAYOUT_BITS+1`` is beyond every valid
same-dimension distance and rejects differing image dimensions/tile sizes.
All other numeric literals are indexing, packing or integer arithmetic units.

The byte size is ``_HEADER.size + _LAYOUT_BYTES + 8 * number_of_unique_hashes``;
Python object overhead is excluded. Up to ``2*grid.s-1`` complete ``describe``
calls are required. This deliberately trades CPU and fingerprint size for
sub-tile scroll support; large engine tiles are expensive. No input array is
mutated, no frames/luma/pixel buffers are retained, and this module performs no
I/O. All working arrays live only in RAM and are discarded at return.

Limitations: small captures can lack stable whole tiles; large or diagonal
motion, wholesale content replacement, and close perceptual lookalikes can
still fail. Exact 64-bit hash collisions remain possible. Quantization can
change near a boundary; tolerance is a heuristic, not a guarantee for every
adversarial one-level perturbation. Evaluation must report recall, separation,
transition rows and VM cost rather than treating this threshold as acceptance.
"""

import struct

import numpy as np

from visioncore.desc import describe
from visioncore.grid import Grid


_COARSE_COLS = 8
_COARSE_ROWS = 6
_LUMA_STEP_Q4 = 32
_EDGE_STEP = 64
_GLOBAL_LUMA_STEP_Q4 = 128
_LUMA_MAX_Q4 = 4080
_EDGE_MAX = 255
_LUMA_LEVELS = _LUMA_MAX_Q4 // _LUMA_STEP_Q4
_EDGE_LEVELS = _EDGE_MAX // _EDGE_STEP
_GLOBAL_BITS = _LUMA_MAX_Q4 // _GLOBAL_LUMA_STEP_Q4
_CELL_BITS = _LUMA_LEVELS + _EDGE_LEVELS
_LAYOUT_BITS = _COARSE_COLS * _COARSE_ROWS * _CELL_BITS + _GLOBAL_BITS
_LAYOUT_BYTES = (_LAYOUT_BITS + 7) // 8
_BAG_SCALE = 1000
SF_THRESHOLD = 1002
_MAGIC = b"SFTD001\0"
_HEADER = struct.Struct("<8sHHHI")
_DIMENSION_DISTANCE = _BAG_SCALE + _LAYOUT_BITS + 1


def _layout_code(tiles, grid):
    """Pool Morton-ordered fields into a packed spatial thermometer code."""
    cells = _COARSE_COLS * _COARSE_ROWS
    cell = ((grid.ty * _COARSE_ROWS // grid.gh) * _COARSE_COLS
            + grid.tx * _COARSE_COLS // grid.gw)
    counts = np.bincount(cell, minlength=cells)
    divisor = np.maximum(counts, 1)

    y_q4 = tiles["y_q4"].astype(np.int64)
    frame_mean_y_q4 = int(np.mean(y_q4)) if len(y_q4) > 0 else 0
    rel_y_q4 = y_q4 - frame_mean_y_q4
    offset_y_q4 = np.clip(rel_y_q4 + (_LUMA_MAX_Q4 // 2), 0, _LUMA_MAX_Q4)

    pooled = []
    for name_vals, step in ((offset_y_q4, _LUMA_STEP_Q4), (tiles["edge"], _EDGE_STEP)):
        sums = np.zeros(cells, dtype=np.int64)
        np.add.at(sums, cell, name_vals.astype(np.int64))
        pooled.append(sums // divisor // step)
    code = 0
    for index, (y_level, edge_level) in enumerate(zip(*pooled)):
        cell_code = ((1 << int(y_level)) - 1
                     | (((1 << int(edge_level)) - 1) << _LUMA_LEVELS))
        code |= cell_code << (index * _CELL_BITS)

    # Global mean luma thermometer code
    global_luma_level = min(_GLOBAL_BITS, frame_mean_y_q4 // _GLOBAL_LUMA_STEP_Q4)
    global_code = (1 << global_luma_level) - 1
    code |= global_code << (_COARSE_COLS * _COARSE_ROWS * _CELL_BITS)
    return code.to_bytes(_LAYOUT_BYTES, "little")


def screen_fingerprint(frame):
    """Return immutable descriptor bytes for an unmodified uint8[H,W,3] array."""
    if (not isinstance(frame, np.ndarray) or frame.ndim != 3
            or frame.shape[2] != 3 or frame.dtype != np.uint8):
        raise ValueError("frame must be a uint8 NumPy array of shape (H, W, 3)")
    height, width = frame.shape[:2]
    grid = Grid(width, height)
    tiles, _ = describe(frame, grid)
    layout = _layout_code(tiles, grid)
    bags = [tiles["xh64"].copy()]
    shifted = np.empty_like(frame)
    for dx in range(1, min(grid.s, width)):
        shifted[:, :width - dx] = frame[:, dx:]
        shifted[:, width - dx:] = frame[:, -1:]
        current, _ = describe(shifted, grid)
        valid = (grid.tx + 1) * grid.s <= width - dx
        bags.append(current["xh64"][valid])
    for dy in range(1, min(grid.s, height)):
        shifted[:height - dy] = frame[dy:]
        shifted[height - dy:] = frame[-1:]
        current, _ = describe(shifted, grid)
        valid = (grid.ty + 1) * grid.s <= height - dy
        bags.append(current["xh64"][valid])
    hashes = np.unique(np.concatenate(bags)).astype("<u8", copy=False)
    return (_HEADER.pack(_MAGIC, width, height, grid.s, len(hashes))
            + layout + hashes.tobytes())


def _unpack(fingerprint):
    """Read only the versioned descriptor representation, with no pixel data."""
    if not isinstance(fingerprint, bytes):
        raise TypeError("fingerprint must be bytes returned by screen_fingerprint")
    if len(fingerprint) < _HEADER.size + _LAYOUT_BYTES:
        raise ValueError("truncated screen fingerprint")
    magic, width, height, tile_size, count = _HEADER.unpack_from(fingerprint)
    offset = _HEADER.size + _LAYOUT_BYTES
    if (magic != _MAGIC or min(width, height, tile_size, count) < 1
            or len(fingerprint) != offset + 8 * count):
        raise ValueError("invalid screen fingerprint format")
    layout = int.from_bytes(fingerprint[_HEADER.size:offset], "little")
    hashes = np.frombuffer(fingerprint, dtype="<u8", count=count, offset=offset)
    return (width, height, tile_size), layout, hashes


def fingerprint_distance(first, second):
    """Return a symmetric nonnegative Python int combining layout and overlap."""
    shape_a, layout_a, hashes_a = _unpack(first)
    shape_b, layout_b, hashes_b = _unpack(second)
    if shape_a != shape_b:
        return _DIMENSION_DISTANCE
    if first == second:
        return 0
    common = len(np.intersect1d(hashes_a, hashes_b, assume_unique=True))
    total = len(hashes_a) + len(hashes_b)
    overlap_distance = _BAG_SCALE * (total - 2 * common) // total
    return int((layout_a ^ layout_b).bit_count() + overlap_distance)


def same_screen(first, second):
    """Apply the one global threshold to descriptor fingerprints."""
    return bool(fingerprint_distance(first, second) <= SF_THRESHOLD)
