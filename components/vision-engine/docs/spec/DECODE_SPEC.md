# Frame Decoding Specification

## 1. Scope, Module Identity, and Symbol Catalogue

This specification defines the canonical PNG decoder, the guarded OpenCV fast path, the raw RGB24 memory-mapped frame reader, and the companion PNG encoders implemented at branch **bridge/integration**:

- `visioncore/decode.py:1-247` — Layer 1 (`L1`) canonical PNG decoder (`png`), anti-diagonal wavefront unfilter (`unfilter`, `_wavefront`), test PNG encoder (`encode_png`), memory-mapped raw frame reader (`rgb24_raw`), and guarded OpenCV fast path (`png_fast`, `fast_state`, `reset_guard`).
- `vadapt/synth/pngenc.py:1-44` — Companion PNG encoder (`encode_png`, `_filter_row`, `_paeth`, `_chunk`) supporting filters 0 through 4 and colour types 2 and 6.
- `tests/test_decode.py:1-186` — Reference test encoder (`make_png`, `paeth`, `chunk`) and unit test suite (`TestDecode`, `TestFrame`).

### 1.1 Module Metadata

| Property | Value in Source | Citation |
|---|---|---|
| Module ID | `"vc.decode"` | `visioncore/decode.py:1` |
| Layer and stage | `"layer":1` and `"stage":"L1"` | `visioncore/decode.py:1` |
| Module owner | `"owner":"ARCH"` | `visioncore/decode.py:1` |
| Declared dependencies | `["vc.errors"]` | `visioncore/decode.py:1` |
| HyperBook binding | `"HB-VC-a0-arch"` | `visioncore/decode.py:1` |
| Module docstring | `"""@module vc.decode L1 - PNG -> uint8 (zlib + numpy unfilter, L-29 fix) and rgb24 raw memmap (SPEC_ARCH §4.4)."""` | `visioncore/decode.py:2` |
| Standard library imports | `import importlib`, `import struct`, `import zlib` | `visioncore/decode.py:3-5` |
| Array library import | `import numpy as np` | `visioncore/decode.py:7` |
| Error class import | `from .errors import VqaError` | `visioncore/decode.py:9` |
| PNG signature constant | `_SIG = b"\x89PNG\r\n\x1a\n"` | `visioncore/decode.py:11` |

### 1.2 Symbol Definitions and Exact Line Numbers

| Symbol | Anchor in Source | Definition Line | Full Range |
|---|---|---|---|
| `_SIG` | ABSENT | `visioncore/decode.py:11` | `visioncore/decode.py:11` |
| `_bad` | ABSENT | `visioncore/decode.py:14` | `visioncore/decode.py:14-15` |
| `_chunks` | ABSENT | `visioncore/decode.py:18` | `visioncore/decode.py:18-30` |
| `_wavefront` | ABSENT | `visioncore/decode.py:32` | `visioncore/decode.py:32-65` |
| `unfilter` | `@anchor:vc.decode.unfilter` | `visioncore/decode.py:68` | `visioncore/decode.py:68-90` |
| `png` | `@anchor:vc.decode.png` | `visioncore/decode.py:93` | `visioncore/decode.py:93-133` |
| `encode_png` | `@anchor:vc.decode.encode_png` | `visioncore/decode.py:136` | `visioncore/decode.py:136-145` |
| `rgb24_raw` | `@anchor:vc.decode.rgb24_raw` | `visioncore/decode.py:148` | `visioncore/decode.py:148-156` |
| `_GUARD` | ABSENT | `visioncore/decode.py:160` | `visioncore/decode.py:160` |
| `_CV2` | ABSENT | `visioncore/decode.py:161` | `visioncore/decode.py:161` |
| `_cv2` | ABSENT | `visioncore/decode.py:164` | `visioncore/decode.py:164-171` |
| `reset_guard` | `@anchor:vc.decode.reset_guard` | `visioncore/decode.py:174` | `visioncore/decode.py:174-177` |
| `fast_state` | `@anchor:vc.decode.fast_state` | `visioncore/decode.py:180` | `visioncore/decode.py:180-182` |
| `_simple_png` | ABSENT | `visioncore/decode.py:185` | `visioncore/decode.py:185-205` |
| `_cv2_decode` | ABSENT | `visioncore/decode.py:208` | `visioncore/decode.py:208-218` |
| `png_fast` | `@anchor:vc.decode.png_fast` | `visioncore/decode.py:221` | `visioncore/decode.py:221-247` |
| `_chunk` | ABSENT | `vadapt/synth/pngenc.py:8` | `vadapt/synth/pngenc.py:8-9` |
| `_paeth` | ABSENT | `vadapt/synth/pngenc.py:12` | `vadapt/synth/pngenc.py:12-15` |
| `_filter_row` | ABSENT | `vadapt/synth/pngenc.py:18` | `vadapt/synth/pngenc.py:18-27` |
| `encode_png` | ABSENT | `vadapt/synth/pngenc.py:30` | `vadapt/synth/pngenc.py:30-44` |
| `chunk` | ABSENT | `tests/test_decode.py:15` | `tests/test_decode.py:15-16` |
| `paeth` | ABSENT | `tests/test_decode.py:19` | `tests/test_decode.py:19-22` |
| `make_png` | ABSENT | `tests/test_decode.py:25` | `tests/test_decode.py:25-42` |
| `TestDecode` | ABSENT | `tests/test_decode.py:49` | `tests/test_decode.py:49-111` |

## 2. Supported PNG Colour Types, Bit Depths, and IHDR Constraints

Both the canonical decoder `png` and the plain-PNG check `_simple_png` unpack the 13-byte `IHDR` payload with `struct.unpack(">IIBBBBB", ihdr)` (`visioncore/decode.py:93-205`):

```python
W, H, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
if depth != 8 or ctype not in (2, 6) or interlace != 0 or comp != 0 or filt != 0:
    raise _bad("unsupported depth=%d colour=%d interlace=%d" % (depth, ctype, interlace))
if not (1 <= W <= 8192 and 1 <= H <= 8192):
    raise _bad("dimensions %dx%d" % (W, H))
```

### 2.1 IHDR Field Requirements

| IHDR Parameter | Unpack Expression | Required Condition in `_simple_png` | Rejection Condition in `png` | Citation |
|---|---|---|---|---|
| Header length | `len(ihdr)` | `len(ihdr) != 13` returns `False` | `ihdr is None or len(ihdr) != 13` | `visioncore/decode.py:113-200` |
| Width and height | `W, H` | `1 <= W <= 8192 and 1 <= H <= 8192` | `not (1 <= W <= 8192 and 1 <= H <= 8192)` | `visioncore/decode.py:117-203` |
| Bit depth | `depth` | `depth == 8` (1 byte per channel) | `depth != 8` | `visioncore/decode.py:117-203` |
| Colour type | `ctype` | `ctype in (2, 6)` (truecolour RGB or RGBA) | `ctype not in (2, 6)` | `visioncore/decode.py:117-203` |
| Compression method | `comp` | `comp == 0` (deflate/inflate) | `comp != 0` | `visioncore/decode.py:117-203` |
| Filter method | `filt` | `filt == 0` (adaptive 5-filter) | `filt != 0` | `visioncore/decode.py:117-203` |
| Interlace method | `interlace` | `interlace == 0` (non-interlaced) | `interlace != 0` | `visioncore/decode.py:117-203` |

### 2.2 Channels, Alpha Dropping, and Output Layout

1. **Bytes per pixel**: `bpp = 3 if ctype == 2 else 4` (`visioncore/decode.py:122`).
2. **Row byte stride**: `stride = W * bpp` (`visioncore/decode.py:123`).
3. **Unfiltering across all channels**: `img = unfilter(raw, H, stride, bpp).reshape(H, W, bpp)` reconstructs all 3 (RGB) or 4 (RGBA) channel lanes per pixel (`visioncore/decode.py:132`).
4. **Alpha discard and contiguity**: `return np.ascontiguousarray(img[:, :, :3])` drops channel index 3 when colour type is 6 without alpha premultiplication and returns a C-contiguous `uint8[H,W,3]` array (`visioncore/decode.py:93-133`).
5. **RGBA test verification**: `test_rgba_alpha_dropped` verifies that two `ctype=6` PNGs with identical RGB channels and differing alpha values decode to identical `(9, 11, 3)` arrays matching `a[..., :3]` (`tests/test_decode.py:56-62`).
6. **Colour profile and palette handling**: Interpretation of ancillary chunks or palettes inside `png` is ABSENT (`visioncore/decode.py:93-112`).

## 3. Chunk Parsing and CRC Handling

### 3.1 Input Source Loading and Signature Verification

1. **In-memory or file input**: In `png(src)`, if `isinstance(src, (bytes, bytearray))`, the input is converted via `data = bytes(src)` (`visioncore/decode.py:93-96`).
2. **Filesystem read**: Otherwise, `png(src)` opens `src` with `with open(src, "rb") as fh:` and reads `data = fh.read()` (`visioncore/decode.py:93-100`).
3. **Filesystem error**: If `open(src, "rb")` or `fh.read()` raises `OSError as e`, `png(src)` raises `VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})` (`visioncore/decode.py:93-102`).
4. **Signature check**: `if data[:8] != _SIG:` (where `_SIG = b"\x89PNG\r\n\x1a\n"`), `png(src)` raises `_bad("not a png")` (`visioncore/decode.py:11-104`).

### 3.2 Generator `_chunks(data)`

```python
def _chunks(data):
    pos = len(_SIG)
    while pos + 12 <= len(data):
        ln, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + ln]
        crc = struct.unpack_from(">I", data, pos + 8 + ln)[0] if pos + 12 + ln <= len(data) else None
        if crc is None or zlib.crc32(typ + body) & 0xFFFFFFFF != crc:
            raise _bad("bad chunk crc/length")
        yield typ, body
```

1. **Starting offset**: `_chunks(data)` begins at byte offset `pos = len(_SIG)` (offset 8) (`visioncore/decode.py:18-19`).
2. **Chunk header loop**: Iterates while `pos + 12 <= len(data)`, unpacking unsigned 32-bit big-endian length `ln` and 4-byte type `typ` via `struct.unpack_from(">I4s", data, pos)` (`visioncore/decode.py:20-21`).
3. **Payload slice and CRC bounds**: Slices `body = data[pos + 8:pos + 8 + ln]` and reads the 4-byte trailer `crc = struct.unpack_from(">I", data, pos + 8 + ln)[0] if pos + 12 + ln <= len(data) else None` (`visioncore/decode.py:22-23`).
4. **CRC-32 verification**: Computes unsigned 32-bit CRC `zlib.crc32(typ + body) & 0xFFFFFFFF` over the 4-byte chunk type concatenated with the chunk body; if `crc is None` (truncated chunk) or the computed value differs from `crc`, raises `_bad("bad chunk crc/length")` (`visioncore/decode.py:24-25`).
5. **Step and termination**: Yields `typ, body`, advances `pos += 12 + ln`, and returns immediately when `typ == b"IEND"` (`visioncore/decode.py:26-29`).

### 3.3 Chunk Collection in `png(src)`

1. `png` initializes `ihdr, idat, iend = None, [], False` (`visioncore/decode.py:93-105`).
2. As `_chunks(data)` yields `typ, body` (`visioncore/decode.py:93-112`):
   - `typ == b"IEND"` sets `iend = True` (`visioncore/decode.py:107-108`).
   - `typ == b"IHDR"` sets `ihdr = body` (`visioncore/decode.py:109-110`).
   - `typ == b"IDAT"` appends `body` via `idat.append(body)` (`visioncore/decode.py:111-112`).
   - Any other chunk type is CRC-checked by `_chunks` and ignored by `png` (`visioncore/decode.py:93-112`).
3. If `ihdr is None or len(ihdr) != 13 or not idat`, `png` raises `_bad("missing IHDR/IDAT")` (`visioncore/decode.py:93-114`).
4. If `not iend` (for example, when the input buffer ends at a chunk boundary or with fewer than 12 trailing bytes before `b"IEND"`), `png` raises `_bad("missing IEND")` (`visioncore/decode.py:93-116`).
5. Validation of chunk ordering (such as requiring `b"IHDR"` before `b"IDAT"`, rejecting multiple `b"IHDR"` chunks, or requiring contiguous `b"IDAT"` chunks) and inspection of trailing bytes after `b"IEND"` are ABSENT (`visioncore/decode.py:18-116`).

## 4. Zlib Inflation and Decompression Bounds

```python
    bpp = 3 if ctype == 2 else 4
    stride = W * bpp
    want = H * (stride + 1)
    try:
        d = zlib.decompressobj()
        raw = d.decompress(b"".join(idat), want + 1)    # bounded: an inflate bomb stops at want+1 bytes
    except zlib.error as e:
        raise _bad("zlib: %s" % str(e)[:60])
    if len(raw) != want or d.unconsumed_tail or not d.eof:
        raise _bad("inflated size mismatch")
```

1. **Target unfiltered byte length**: `want = H * (stride + 1)` where `stride = W * bpp` (`visioncore/decode.py:122-124`).
2. **Bounded stream inflation**: Concatenates all `b"IDAT"` payloads via `b"".join(idat)` and decompresses with `d = zlib.decompressobj()` and `raw = d.decompress(b"".join(idat), want + 1)`, where the `want+1` cap prevents an inflate bomb from allocating more than `want + 1` bytes (`visioncore/decode.py:111-127`).
3. **Zlib error handling**: If `d.decompress` raises `zlib.error as e`, `png` raises `_bad("zlib: %s" % str(e)[:60])` (`visioncore/decode.py:93-129`).
4. **Exact length and EOF verification**: If `len(raw) != want` (too few or `want + 1` bytes), `d.unconsumed_tail` is non-empty (more compressed output remained beyond `want + 1`), or `not d.eof` (stream ended before the zlib final block/checksum), `png` raises `_bad("inflated size mismatch")` (`visioncore/decode.py:93-131`).
5. **Post-EOF trailing bytes**: Checking unused data after `d.eof` is ABSENT (`visioncore/decode.py:126-131`).

## 5. Unfilter Algorithm for Filters 0 to 4

### 5.1 Scanline Layout and Reconstruction Contract

`unfilter(raw, H, stride, bpp)` reshapes `raw` as a 2-D `uint8` view `a = np.frombuffer(raw, dtype=np.uint8).reshape(H, stride + 1)` and returns `uint8[H, stride]` (`visioncore/decode.py:68-72`):

- `a[:, 0]` holds the per-row filter type byte `ft` (`visioncore/decode.py:36-78`).
- `a[:, 1:]` holds the `stride = W * bpp` filtered bytes for each of the `H` scanlines (`visioncore/decode.py:43-123`).
- For pixel `(x,y)` across all `bpp` channel lanes (`visioncore/decode.py:32-53`):
  - Left neighbour `A` is pixel `(x-1,y)` in the same channel, or `0` when at column 0 (`visioncore/decode.py:33-48`).
  - Above neighbour `B` is pixel `(x,y-1)` in the same channel, or `0` when at row 0 (`visioncore/decode.py:33-80`).
  - Upper-left neighbour `C` is pixel `(x-1,y-1)` in the same channel, or `0` when at column 0 or row 0 (`visioncore/decode.py:33-53`).

### 5.2 Filter Definitions (Filters 0 to 4)

| Filter ID | Filter Name | Predictor Formula in Source | Row-Loop Implementation | Wavefront Implementation | Citation |
|---:|---|---|---|---|---|
| 0 | None | `0` | `if ft == 0:` (`row = a[y, 1:].copy()`) | `cur` unchanged when `pr is None` or `0` | `visioncore/decode.py:32-90` |
| 1 | Sub | `A` | `np.cumsum(row.reshape(-1, bpp), axis=0, dtype=np.uint8).reshape(-1)` | `(1, lambda: A)` | `visioncore/decode.py:32-90` |
| 2 | Up | `B` | `row = row + prev` | `(2, lambda: B)` | `visioncore/decode.py:32-90` |
| 3 | Average | `(A + B) >> 1` | Dispatched to `_wavefront` | `(3, lambda: (A + B) >> 1)` | `visioncore/decode.py:32-90` |
| 4 | Paeth | `np.where((pa <= pb) & (pa <= pc), A, np.where(pb <= pc, B, C))` | Dispatched to `_wavefront` | `bc, ac = B - C, A - C`; `pa, pb, pc = np.abs(bc), np.abs(ac), np.abs(bc + ac)` | `visioncore/decode.py:32-90` |

### 5.3 Row-Loop Path (Filters 0, 1, and 2 Only)

```python
    for y in range(H):
        ft = int(a[y, 0])
        row = a[y, 1:].copy()
        prev = out[y - 1] if y else zero
        if ft == 0:
            pass
        elif ft == 1:
            row = np.cumsum(row.reshape(-1, bpp), axis=0, dtype=np.uint8).reshape(-1)
        elif ft == 2:
            row = row + prev
```

1. **Dispatch condition**: `if bool(((a[:, 0] == 3) | (a[:, 0] == 4)).any()):` routes to `_wavefront`; otherwise `unfilter` executes the row loop (`visioncore/decode.py:68-75`).
2. **State buffers**: Allocates `out = np.zeros((H, stride), dtype=np.uint8)` and `zero = np.zeros(stride, dtype=np.uint8)` (`visioncore/decode.py:75-76`).
3. **Per-row reconstruction**: For each `y in range(H)`, copies `row = a[y, 1:].copy()` and selects `prev = out[y - 1] if y else zero` (`visioncore/decode.py:77-80`):
   - Filter 0 (`ft == 0`): no modification (`visioncore/decode.py:81-82`).
   - Filter 1 (`ft == 1`): reshapes `row` to `(-1, bpp)`, runs wrapping `dtype=np.uint8` cumulative sum along `axis=0`, and flattens back with `.reshape(-1)` (`visioncore/decode.py:83-84`).
   - Filter 2 (`ft == 2`): computes wrapping `dtype=np.uint8` sum `row = row + prev` (`visioncore/decode.py:75-86`).
   - Any other filter value: raises `_bad("filter type %d" % ft)` (`visioncore/decode.py:87-88`).
4. **Row store**: Writes `out[y] = row` and returns `out` (`visioncore/decode.py:89-90`).

### 5.4 Anti-Diagonal Wavefront Path (`_wavefront`) for Filters 0 to 4

```python
        if 4 in types:
            C = S[u, y0:y1].astype(np.int16)
            bc, ac = B - C, A - C
            pa, pb, pc = np.abs(bc), np.abs(ac), np.abs(bc + ac)
            pr = np.where((pa <= pb) & (pa <= pc), A, np.where(pb <= pc, B, C))
            if len(types) > 1:
                pr = np.where(f == 4, pr, 0)
        for t, val in ((3, lambda: (A + B) >> 1), (2, lambda: B), (1, lambda: A)):
            if t in types:
                v = np.where(f == t, val(), 0)
```

1. **Trigger**: Executed whenever `bool(((a[:, 0] == 3) | (a[:, 0] == 4)).any())` is truthy; its result is converted via `np.ascontiguousarray(_wavefront(a, H, stride // bpp, bpp)).reshape(H, stride)` (`visioncore/decode.py:73-74`).
2. **Filter byte validation**: Extracts `ft = a[:, 0]` and checks `int(ft.max()) > 4`, raising `_bad("filter type %d" % int(ft.max()))` (`visioncore/decode.py:36-38`).
3. **Skewed anti-diagonal buffers**:
   - Sets `yy, xx = np.indices((H, W))` and `U = W + H - 1` (`visioncore/decode.py:40-41`).
   - Allocates `raw = np.zeros((U + 2, H + 1, bpp), dtype=np.uint8)` and `S = np.zeros_like(raw)` (`visioncore/decode.py:42-44`).
   - Maps pixel `(x,y)` onto anti-diagonal `u = x+y` at skewed slot `raw[u+2, y+1]` via `raw[xx + yy + 2, yy + 1] = a[:, 1:].reshape(H, W, bpp)` (`visioncore/decode.py:33-43`).
   - Leading zero padding in `S` (2 anti-diagonals on axis 0 and 1 row on axis 1) ensures that out-of-bounds neighbours at column 0 and row 0 read as `0` without branching (`visioncore/decode.py:42-53`).
4. **Wavefront sweep (`for u in range(U)`)**:
   - Computes active row slice `y0, y1 = max(0, u - W + 1), min(H - 1, u) + 1` and row filter slice `f = f_all[y0:y1]` where `f_all = ft.astype(np.int16)[:, None]` (`visioncore/decode.py:45-51`).
   - Fetches `A = S[u + 1, y0 + 1:y1 + 1].astype(np.int16)` and `B = S[u + 1, y0:y1].astype(np.int16)` (`visioncore/decode.py:48-49`).
   - If `4 in types`: fetches `C = S[u, y0:y1].astype(np.int16)`, computes `bc, ac = B - C, A - C` and `pa, pb, pc = np.abs(bc), np.abs(ac), np.abs(bc + ac)`, selects `pr = np.where((pa <= pb) & (pa <= pc), A, np.where(pb <= pc, B, C))`, and if `len(types) > 1` masks with `pr = np.where(f == 4, pr, 0)` (`visioncore/decode.py:52-58`).
   - For each `t, val` in `((3, lambda: (A + B) >> 1), (2, lambda: B), (1, lambda: A))`: if `t in types`, computes `v = np.where(f == t, val(), 0)` and accumulates `pr = v if pr is None else pr + v` (`visioncore/decode.py:59-62`).
   - Reconstructs the anti-diagonal slice: `cur = raw[u + 2, y0 + 1:y1 + 1]` and `S[u + 2, y0 + 1:y1 + 1] = cur if pr is None else (cur + pr.astype(np.uint8))` (`visioncore/decode.py:63-64`).
5. **Unskew**: Returns `S[xx + yy + 2, yy + 1]` of shape `uint8[H, W, bpp]` (`visioncore/decode.py:35-65`).

## 6. Fast Path in `png_fast` with `fast_state` and `reset_guard`

### 6.1 State Variables and Guard Controls

```python
_GUARD = None      # None = unchecked, True = cv2 bit-equal on the first frame, False = disabled for this run
_CV2 = False       # False = not probed yet, None = unavailable, else the cv2 module
```

1. **Module globals**: `_GUARD = None` and `_CV2 = False` (`visioncore/decode.py:160-161`).
2. **Lazy OpenCV loader `_cv2()`**: When `_CV2 is False`, tries `_CV2 = importlib.import_module("cv2")` and sets `_CV2 = None` on `Exception`; returns `_CV2` (`visioncore/decode.py:164-171`).
3. **Guard reset `reset_guard()`**: Sets `global _GUARD` to `_GUARD = None` to forget the one-time equality verdict at the start of a new run (`visioncore/decode.py:174-177`).
4. **State query `fast_state()`**: Evaluates `return "unchecked" if _GUARD is None else ("on" if _GUARD else "off")` (`visioncore/decode.py:180-182`).

### 6.2 Plain-PNG Eligibility Check (`_simple_png(data)`)

```python
        for typ, body in _chunks(data):
            if typ == b"IHDR":
                ihdr = body
            elif typ == b"IEND":
                iend = True
            elif typ != b"IDAT":
                return False
        if not iend or ihdr is None or len(ihdr) != 13:
            return False
```

`_simple_png(data)` wraps its body in `try:` and `except Exception:` returning `False` on any doubt (`visioncore/decode.py:185-205`), and returns `True` iff:

1. `if data[:8] != _SIG:` is not taken (`visioncore/decode.py:189-190`).
2. Every chunk yielded by `_chunks(data)` has valid length/CRC and `typ` equal to `b"IHDR"`, `b"IEND"`, or `b"IDAT"`; any ancillary chunk triggers `elif typ != b"IDAT":` and `return False` (`visioncore/decode.py:192-198`).
3. `if not iend or ihdr is None or len(ihdr) != 13:` is not taken (`visioncore/decode.py:199-200`).
4. Unpacked `W, H, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)` satisfies `depth == 8 and ctype in (2, 6) and interlace == 0 and comp == 0 and filt == 0` and `1 <= W <= 8192 and 1 <= H <= 8192` (`visioncore/decode.py:201-203`).

### 6.3 OpenCV Decode Wrapper (`_cv2_decode(data)`)

1. Calls `cv = _cv2()`; returns `None` if `cv is None` (`visioncore/decode.py:208-211`).
2. Calls `bgr = cv.imdecode(np.frombuffer(data, dtype=np.uint8), cv.IMREAD_COLOR)` (`visioncore/decode.py:213`).
3. If `bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.dtype != np.uint8`, returns `None` (`visioncore/decode.py:214-215`).
4. Converts BGR to C-contiguous RGB via `return np.ascontiguousarray(bgr[:, :, ::-1])`, returning `None` on any `Exception` (`visioncore/decode.py:216-218`).

### 6.4 `png_fast(src)` Execution Flow

```python
    if not _simple_png(data):
        return png(data)
    if _GUARD is None:
        ref = png(data)
        fast = _cv2_decode(data)
        if fast is None:
            _GUARD = False
        else:
            _GUARD = bool(fast.shape == ref.shape and np.array_equal(fast, ref))
        return ref
```

1. **Fast-off short circuit**: If `_GUARD is False`, immediately returns `png(src)` (`visioncore/decode.py:226-227`).
2. **Source read**: Converts `bytes`/`bytearray` via `data = bytes(src)` or reads `src` via `open(src, "rb")`, raising `VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})` on `OSError as e` (`visioncore/decode.py:228-235`).
3. **Non-simple fallback**: If `not _simple_png(data)`, returns `png(data)` without modifying `_GUARD` (`visioncore/decode.py:225-237`).
4. **First-frame guard probe (`_GUARD is None`)**:
   - Calls `ref = png(data)` first so any `b"IDAT"` refusal raises `VqaError` before `_GUARD` is written (`visioncore/decode.py:197-239`).
   - Calls `fast = _cv2_decode(data)` (`visioncore/decode.py:240`).
   - Sets `_GUARD = False` when `fast is None`, or `_GUARD = bool(fast.shape == ref.shape and np.array_equal(fast, ref))` otherwise, and returns `ref` (`visioncore/decode.py:241-245`).
5. **Steady-state fast path**: When `_GUARD` is truthy, calls `fast = _cv2_decode(data)` and returns `fast if fast is not None else png(data)` (`visioncore/decode.py:225-247`).

## 7. `rgb24_raw` Memory-Map Layout

```python
def rgb24_raw(path, width, height, count):  # @anchor:vc.decode.rgb24_raw
    """Zero-copy read-only memmap of count raw rgb24 frames -> uint8[count,H,W,3]."""
    try:
        mm = np.memmap(path, dtype=np.uint8, mode="r")
    except (OSError, ValueError) as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    if mm.size != count * height * width * 3:
        raise VqaError("E_VC_FRAME_SIZE", {"path": str(path), "bytes": int(mm.size)})
    return mm.reshape(count, height, width, 3)
```

1. **Read-only mapping**: Opens `path` as `mm = np.memmap(path, dtype=np.uint8, mode="r")` (`visioncore/decode.py:148-151`).
2. **Open/mmap failure**: Catches `(OSError, ValueError) as e` and raises `VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})` (`visioncore/decode.py:152-153`).
3. **Exact byte length check**: Checks `if mm.size != count * height * width * 3:` and raises `VqaError("E_VC_FRAME_SIZE", {"path": str(path), "bytes": int(mm.size)})` (`visioncore/decode.py:154-155`).
4. **Shape and byte stride**: Returns `mm.reshape(count, height, width, 3)`, a zero-copy 4-D `uint8[count,H,W,3]` view (`visioncore/decode.py:148-156`).
5. **Test coverage**: `test_rgb24_raw` verifies both multi-frame slicing `decode.rgb24_raw(p, 10, 6, 2)` and `"E_VC_FRAME_SIZE"` rejection (`tests/test_decode.py:101-111`).
6. **Bounds checks on `width`, `height`, `count`**: Explicit positive/upper-bound checks on `width`, `height`, or `count` inside `rgb24_raw` (other than `np.memmap` and `mm.size != count * height * width * 3`) are ABSENT (`visioncore/decode.py:148-156`).

## 8. `encode_png` in `visioncore/decode.py` and `make_png` in `tests/test_decode.py`

### 8.1 `visioncore.decode.encode_png(rgb)`

```python
def encode_png(rgb):  # @anchor:vc.decode.encode_png
    """uint8[H,W,3] -> PNG bytes (colour type 2, filter 0, zlib level 6). Deterministic for fixed zlib."""
    H, W, _ = rgb.shape
    raw = np.concatenate([np.zeros((H, 1), dtype=np.uint8), rgb.reshape(H, W * 3)], axis=1).tobytes()

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    return _SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
```

1. **Scanline packing**: Extracts `H, W, _ = rgb.shape` and prepends a `0` filter byte to each row via `np.concatenate([np.zeros((H, 1), dtype=np.uint8), rgb.reshape(H, W * 3)], axis=1).tobytes()` (`visioncore/decode.py:136-139`).
2. **Chunk assembly**: Packs `_SIG`, a 13-byte `b"IHDR"` (`struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)`), a single `b"IDAT"` (`zlib.compress(raw, 6)`), and an empty `b"IEND"` (`visioncore/decode.py:141-145`).
3. **Roundtrip and determinism test**: `test_roundtrip_encode` verifies `np.array_equal(decode.png(decode.encode_png(img)), img)` and `self.assertEqual(decode.encode_png(img), decode.encode_png(img))` (`tests/test_decode.py:64-67`).

### 8.2 Test Reference Encoder `make_png` in `tests/test_decode.py`

1. **Signature**: `def make_png(img, ftype_of_row, ctype=2, interlace=0):` (`tests/test_decode.py:25`).
2. **Per-row filter callback**: Evaluates `ft = ftype_of_row(y)` for each row `y in range(H)` (`tests/test_decode.py:30-31`).
3. **Predictor and invalid-filter injection**: Computes `pred = (0, a, b, (a + b) // 2, paeth(a, b, c))[ft if ft < 5 else 0]` and `(cur[x] - pred) & 255`, then prepends `bytes([ft])`, allowing `self.rejects(make_png(img, lambda y: 5))` (`tests/test_decode.py:25-99`) to test filter byte rejection.
4. **Chunk output**: Emits `decode._SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")` (`tests/test_decode.py:41-42`).

## 9. Companion Encoder in `vadapt/synth/pngenc.py`

```python
def encode_png(rgb, filt=0, alpha=None):
    """rgb uint8 (H,W,3); filt in 0..4, or -1 = cycle filters per row; alpha uint8 (H,W) -> colour type 6."""
    H, W, _ = rgb.shape
    img = rgb if alpha is None else np.concatenate([rgb, alpha[:, :, None]], axis=2)
    bpp = img.shape[2]
    rows = img.reshape(H, W * bpp)
    prev = np.zeros(W * bpp, np.uint8)
    raw = bytearray()
```

1. **Chunk helper `_chunk(tag, data)`**: Returns `struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)` (`vadapt/synth/pngenc.py:8-9`).
2. **Paeth predictor `_paeth(a, b, c)`**: Computes `p = a + b - c` and `pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)`, returning `a if pa <= pb and pa <= pc else (b if pb <= pc else c)` (`vadapt/synth/pngenc.py:12-15`).
3. **Forward row filter `_filter_row(ft, row, prev, bpp)`**:
   - Allocates `out = np.zeros(n, np.uint8)` where `n = len(row)` (`vadapt/synth/pngenc.py:18-20`).
   - For each byte `i in range(n)`, reads unfiltered bytes `a = int(row[i - bpp]) if i >= bpp else 0`, `b = int(prev[i])`, `c = int(prev[i - bpp]) if i >= bpp else 0`, computes `pred = (0, a, b, (a + b) // 2, _paeth(a, b, c))[ft]`, and stores `out[i] = (int(row[i]) - pred) & 0xFF` (`vadapt/synth/pngenc.py:21-27`).
4. **Encoder `encode_png(rgb, filt=0, alpha=None)`**:
   - Builds `img = rgb if alpha is None else np.concatenate([rgb, alpha[:, :, None]], axis=2)` with `bpp = img.shape[2]` (`vadapt/synth/pngenc.py:30-34`).
   - Selects row filter `ft = (y % 5) if filt < 0 else filt` for each `y in range(H)`, appends `ft` to `raw`, appends `(rows[y] if ft == 0 else _filter_row(ft, rows[y], prev, bpp)).tobytes()`, and updates `prev = rows[y]` (`vadapt/synth/pngenc.py:38-42`).
   - Packs `ihdr = struct.pack(">IIBBBBB", W, H, 8, 2 if alpha is None else 6, 0, 0, 0)` and returns `b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _chunk(b"IEND", b"")` (`vadapt/synth/pngenc.py:43-44`).
5. **Input validation**: Explicit validation of input array dtypes, shapes, or out-of-range `filt` values inside `_filter_row` and `encode_png` is ABSENT (`vadapt/synth/pngenc.py:18-44`).

## 10. Rejected Inputs and Error Codes

1. **PNG error helper `_bad(reason)`**: Returns `VqaError("E_VC_PNG", {"reason": reason})` (`visioncore/decode.py:14-15`).
2. **Exit code verification**: `self.assertEqual((c.exception.code, c.exception.rc), ("E_VC_PNG", 3))` in `rejects` verifies exit code 3 for `"E_VC_PNG"` (`tests/test_decode.py:79-82`).
3. **External error class**: `VqaError` is imported via `from .errors import VqaError` (`visioncore/decode.py:9`); its class definition is ABSENT from the three bound source files.

| Rejected Condition in Source | Error Code | Raised Expression in Source | Citation |
|---|---|---|---|
| `png(src)` file open/read raises `OSError as e` | `"E_IO"` | `raise VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})` | `visioncore/decode.py:93-102` |
| `png_fast(src)` file open/read raises `OSError as e` | `"E_IO"` | `raise VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})` | `visioncore/decode.py:221-235` |
| `rgb24_raw` `np.memmap` raises `(OSError, ValueError) as e` | `"E_IO"` | `raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})` | `visioncore/decode.py:148-153` |
| `rgb24_raw` `mm.size != count * height * width * 3` | `"E_VC_FRAME_SIZE"` | `raise VqaError("E_VC_FRAME_SIZE", {"path": str(path), "bytes": int(mm.size)})` | `visioncore/decode.py:148-155` |
| `data[:8] != _SIG` | `"E_VC_PNG"` | `raise _bad("not a png")` | `visioncore/decode.py:14-104` |
| `crc is None or zlib.crc32(typ + body) & 0xFFFFFFFF != crc` | `"E_VC_PNG"` | `raise _bad("bad chunk crc/length")` | `visioncore/decode.py:14-25` |
| `ihdr is None or len(ihdr) != 13 or not idat` | `"E_VC_PNG"` | `raise _bad("missing IHDR/IDAT")` | `visioncore/decode.py:14-114` |
| `not iend` | `"E_VC_PNG"` | `raise _bad("missing IEND")` | `visioncore/decode.py:14-116` |
| `depth != 8 or ctype not in (2, 6) or interlace != 0 or comp != 0 or filt != 0` | `"E_VC_PNG"` | `raise _bad("unsupported depth=%d colour=%d interlace=%d" % (depth, ctype, interlace))` | `visioncore/decode.py:14-119` |
| `not (1 <= W <= 8192 and 1 <= H <= 8192)` | `"E_VC_PNG"` | `raise _bad("dimensions %dx%d" % (W, H))` | `visioncore/decode.py:14-121` |
| `d.decompress(b"".join(idat), want + 1)` raises `zlib.error as e` | `"E_VC_PNG"` | `raise _bad("zlib: %s" % str(e)[:60])` | `visioncore/decode.py:14-129` |
| `len(raw) != want or d.unconsumed_tail or not d.eof` | `"E_VC_PNG"` | `raise _bad("inflated size mismatch")` | `visioncore/decode.py:14-131` |
| `int(ft.max()) > 4` in `_wavefront` | `"E_VC_PNG"` | `raise _bad("filter type %d" % int(ft.max()))` | `visioncore/decode.py:14-38` |
| `ft` not in `0`, `1`, `2` in `unfilter` row loop | `"E_VC_PNG"` | `raise _bad("filter type %d" % ft)` | `visioncore/decode.py:14-88` |

## 11. Undefined or Unspecified Behaviours (ABSENT)

Where behaviour is not defined in the bound source files, it is recorded as ABSENT:

- **Non-8-bit depths and colour types other than 2 and 6**: Decoding support for bit depths other than 8 and colour types other than `(2, 6)` is ABSENT (`visioncore/decode.py:118-119`).
- **Adam7 interlacing**: De-interlacing support for `interlace != 0` is ABSENT (`visioncore/decode.py:118-119`).
- **Ancillary PNG chunks**: Colour space conversion and transparency chunk application (`gAMA/iCCP/tRNS`) are ABSENT (`visioncore/decode.py:185-205`).
- **Chunk ordering and duplicate header validation**: Checks that `b"IHDR"` is the first chunk, that only one `b"IHDR"` chunk is present, or that `b"IDAT"` chunks are contiguous are ABSENT (`visioncore/decode.py:105-116`).
- **Trailing bytes after `b"IEND"` or after `d.eof`**: Validation of bytes following `b"IEND"` or post-stream bytes after `d.eof` is ABSENT (`visioncore/decode.py:28-131`).
- **Concurrency synchronization on `_GUARD` and `_CV2`**: Thread locks around `_GUARD` and `_CV2` are ABSENT (`visioncore/decode.py:160-247`).
- **Definition of `VqaError` and exit-code table**: Imported via `from .errors import VqaError` and ABSENT from the three bound source files (`visioncore/decode.py:9`).
