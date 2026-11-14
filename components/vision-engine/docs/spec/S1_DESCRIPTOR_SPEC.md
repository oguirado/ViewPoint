# Stage S1 Tile Descriptor Specification (`visioncore/desc.py`)

## 1. Scope and Module Metadata

This document specifies how Stage S1 (`visioncore/desc.py`) computes each tile descriptor value, derived exclusively from the bound source files at ref `bridge/integration`:

- `visioncore/desc.py` (lines `1`–`200`)
- `visioncore/layout.py` (lines `1`–`139`)
- `visioncore/params.py` (lines `1`–`86`)
- `AGENTS.md` (lines `1`–`57`)

### 1.1 Module Identity and Layering Rules

| Attribute | Value | Source (`file:line`) |
|---|---|---|
| Module ID | `"vc.s1"` | `visioncore/desc.py:1` |
| Layer | `1` (`L1`, modules: `decode grid desc`) | `visioncore/desc.py:1-2`, `AGENTS.md:19` |
| Stage | `"S1"` | `visioncore/desc.py:1-2` |
| Owner | `"CORE"` (`grid desc change pyramid regions`) | `visioncore/desc.py:1`, `AGENTS.md:28` |
| Declared dependencies | `["vc.p", "vc.layout", "vc.errors"]` | `visioncore/desc.py:1` |
| HyperBook tag | `"HB-VC-s1-desc"` | `visioncore/desc.py:1` |
| Allowed imports | Same or lower layer (`L0`: `ids params layout errors canon frame`; `L1`: `decode grid desc`), `numpy`, Python stdlib; never `vmem` or `vadapt` | `AGENTS.md:15-19, 23` |
| Actual imports in `desc.py` | `hashlib`, `numpy as np`, `.layout`, `.errors.VqaError`, `.params.PARAMS` | `visioncore/desc.py:3-9` |
| Integer-only rule | Integers only in `L0`–`L3` (no float literal, `float(`, `/`; gate `g_nofloat`); `isqrt` uses `v.astype(np.float64)` internally as a seed before integer corrections | `AGENTS.md:32`, `visioncore/desc.py:24-28` |

---

## 2. Descriptor Dtype (`TILE_DTYPE`) and Cross-Reference to `visioncore/layout.py`

`visioncore/desc.py:11-16` defines `TILE_DTYPE` as a structured `np.dtype` using `layout.TILE_FIELDS` (`visioncore/layout.py:95-96`), `layout.TILE_OFFSETS` (`visioncore/layout.py:97`), and `layout.TILE_SIZE = 32` (`visioncore/layout.py:98`), and asserts `TILE_DTYPE.itemsize == layout.TILE_SIZE` (`visioncore/desc.py:17`).

Cross-reference of `TILE_DTYPE` fields (`visioncore/desc.py:11-16`) to `visioncore/layout.py:94-98` offsets (the binary record layout itself is specified separately):

| Field Name (`layout.py:95-96`) | Byte Offset (`layout.py:97`) | `TILE_DTYPE` Format (`desc.py:13`) | `TILE_FMT` Token (`layout.py:94`) | Stage S1 Behavior (`desc.py`) |
|---|---:|---|---|---|
| `morton` | `0` | `"<u2"` (`uint16` LE) | `H` | Set to `grid.mortons` (`visioncore/desc.py:80`) |
| `cls` | `2` | `"u1"` (`uint8`) | `B` | Initialized to `0` (`np.zeros`); left `0` for S2 (`visioncore/desc.py:73, 79`) |
| `flags` | `3` | `"u1"` (`uint8`) | `B` | Set by `_compute` (`visioncore/desc.py:187-192`) or masked copy on reuse (`visioncore/desc.py:94`) |
| `y_q4` | `4` | `"<u2"` (`uint16` LE) | `H` | Set by `_compute` (`visioncore/desc.py:193`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `co_h` | `6` | `"i1"` (`int8`) | `b` | Set by `_compute` (`visioncore/desc.py:194`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `cg_h` | `7` | `"i1"` (`int8`) | `b` | Set by `_compute` (`visioncore/desc.py:195`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `sig_q4` | `8` | `"<u2"` (`uint16` LE) | `H` | Set by `_compute` (`visioncore/desc.py:197`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `grad_q6` | `10` | `"<u2"` (`uint16` LE) | `H` | Set by `_compute` (`visioncore/desc.py:198`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `edge` | `12` | `"u1"` (`uint8`) | `B` | Set by `_compute` (`visioncore/desc.py:199`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `resid` | `13` | `"u1"` (`uint8`) | `B` | Initialized to `0` (`np.zeros`); left `0` for S2 (`visioncore/desc.py:73, 79`) |
| `mv_dx` | `14` | `"i1"` (`int8`) | `b` | Initialized to `0` (`np.zeros`); left `0` for S2 (`visioncore/desc.py:73, 79`) |
| `mv_dy` | `15` | `"i1"` (`int8`) | `b` | Initialized to `0` (`np.zeros`); left `0` for S2 (`visioncore/desc.py:73, 79`) |
| `sig64` | `16` | `"<u8"` (`uint64` LE) | `Q` | Set by `_compute` (`visioncore/desc.py:200`) or copied on reuse (`visioncore/desc.py:92-93`) |
| `xh64` | `24` | `"<u8"` (`uint64` LE) | `Q` | Set in `describe_padded` (`visioncore/desc.py:101-109`), left `0` when `lazy_xh=True` (`visioncore/desc.py:99-100`), or filled by `fill_xh64` (`visioncore/desc.py:113-127`) |

---

## 3. Entry Points, Grid Parameters, and Tile Gathering

### 3.1 `describe(frame, grid, words_prev=None, prev_xh64=None, p=None)` (`visioncore/desc.py:60-64`)

- Anchor: `# @anchor:vc.s1.describe` (`visioncore/desc.py:60`).
- Input validation (`visioncore/desc.py:62-63`):
  - Checks `getattr(frame, "shape", None) != (grid.H, grid.W, 3) or frame.dtype != np.uint8`.
  - If true, raises `VqaError("E_VC_FRAME_SHAPE", {"shape": list(getattr(frame, "shape", ())), "grid": grid.ident})`.
- Delegation (`visioncore/desc.py:64`):
  - Calls and returns `describe_padded(grid.pad(frame), grid, words_prev, prev_xh64, p)`.
  - Note: Does not expose `prev_tiles`, `y_plane`, or `lazy_xh` parameters (`visioncore/desc.py:60, 64`).

### 3.2 `describe_padded(padded, grid, words_prev=None, prev_xh64=None, p=None, prev_tiles=None, y_plane=None, lazy_xh=False)` (`visioncore/desc.py:67-110`)

- Anchor: `# @anchor:vc.s1.describe_padded` (`visioncore/desc.py:67`).
- Parameter table resolution (`visioncore/desc.py:74`):
  - `p = PARAMS if p is None else p`, where `PARAMS` is imported from `visioncore/params.py:9, 49`.
- Grid geometry unpacking (`visioncore/desc.py:75-76`):
  - `s, gw, gh, N = grid.s, grid.gw, grid.gh, grid.N` (`visioncore/desc.py:75`).
  - `n = s * s` (`visioncore/desc.py:76`, number of pixels per tile).
  - Cross-reference to `visioncore/layout.py:9-25` and `visioncore/params.py:12-14`: `grid_params(W, H)` selects the smallest power of two `s >= TILE_MIN_PX` (`16`, `visioncore/params.py:12`, `visioncore/layout.py:9, 19-24`) with `s <= 1 << 16` (`visioncore/layout.py:20`) such that `gw = -(-W // s)` and `gh = -(-H // s)` (`visioncore/layout.py:21`) satisfy `gw * gh <= CELL_MAX` (`4096`, `visioncore/params.py:13`, `visioncore/layout.py:10, 22`) and `max(gw, gh) <= AXIS_MAX` (`255`, `visioncore/params.py:14`, `visioncore/layout.py:11, 22`) for integer `W, H` in `1..DIM_MAX` (`8192`, `visioncore/layout.py:12, 17-18`).
- Padded shape check (`visioncore/desc.py:77-78`):
  - If `padded.shape != (gh * s, gw * s, 3)`, raises `VqaError("E_VC_FRAME_SHAPE", {"shape": list(padded.shape), "grid": grid.ident})`.
- Output array initialization (`visioncore/desc.py:79-80`):
  - `out = np.zeros(N, dtype=TILE_DTYPE)` (`visioncore/desc.py:79`).
  - `out["morton"] = grid.mortons` (`visioncore/desc.py:80`). Cross-reference: `morton(tx, ty)` in `visioncore/layout.py:33-54` interleaves 8-bit `tx` (even bits via `_spread(tx)`) and 8-bit `ty` (odd bits via `_spread(ty) << 1`).
- Return value (`visioncore/desc.py:100, 110`):
  - Tuple `(out, words_eq)` where `out` has shape `(N,)` and dtype `TILE_DTYPE`, and `words_eq` has shape `(N,)` and dtype `bool`.

### 3.3 Tile Gathering: `_gather(padded, grid, idx)` (`visioncore/desc.py:134-138`)

- Reshapes `padded` into a 5-D view `P = padded.reshape(grid.gh, s, grid.gw, s, 3)` (`visioncore/desc.py:136-137`).
- Extracts the `M = len(idx)` tiles at Morton ranks `idx` via advanced indexing `P[grid.ty[idx], :, grid.tx[idx], :, :]` (`visioncore/desc.py:138`).
- Because advanced indexing indices `grid.ty[idx]` and `grid.tx[idx]` are separated by slice `:`, NumPy places the indexed dimension first, returning a tile-major `uint8` array of shape `(M, s, s, 3)` where each tile `k` is a contiguous `(s, s, 3)` RGB block (`visioncore/desc.py:73, 135, 138`).

---

## 4. YCoCg-R Transform, Integer Rounding, and Luma Plane

### 4.1 Standalone YCoCg-R Lifting: `ycocg(rgb)` (`visioncore/desc.py:32-41`)

- Anchor: `# @anchor:vc.s1.ycocg` (`visioncore/desc.py:32`).
- Given an array `rgb` whose last axis has 3 channels (`R`, `G`, `B`), converts each channel to `np.int16` and applies lossless YCoCg-R lifting (`visioncore/desc.py:34-41`):
  1. `r = rgb[..., 0].astype(np.int16)` (`visioncore/desc.py:34`)
  2. `g = rgb[..., 1].astype(np.int16)` (`visioncore/desc.py:35`)
  3. `b = rgb[..., 2].astype(np.int16)` (`visioncore/desc.py:36`)
  4. `co = r - b` (`visioncore/desc.py:37`)
  5. `t = b + (co >> 1)` (`visioncore/desc.py:38`)
  6. `cg = g - t` (`visioncore/desc.py:39`)
  7. `y = t + (cg >> 1)` (`visioncore/desc.py:40`)
  8. Returns `(y, co, cg)` (`visioncore/desc.py:41`), all with dtype `np.int16`.

### 4.2 Integer Rounding Semantics of YCoCg-R (`visioncore/desc.py:37-40, 50-51, 157-158`)

- All intermediate values (`r`, `g`, `b`, `co`, `t`, `cg`, `y`) are signed 16-bit integers (`np.int16`).
- Division by 2 is performed exclusively by signed arithmetic right shift `>> 1` (`co >> 1` and `cg >> 1` at `visioncore/desc.py:38, 40`, `(r - b) >> 1` and `(g - t) >> 1` at `visioncore/desc.py:50-51`, and `co >> 1` at `visioncore/desc.py:158`).
- In two's-complement `int16` arithmetic, `x >> 1` computes exact floor division $\lfloor x / 2 \rfloor$ (rounding toward $-\infty$ for odd negative integers, e.g. `-1 >> 1 == -1`).
- For unsigned 8-bit inputs $R, G, B \in [0, 255]$:
  - $Co = R - B \in [-255, 255]$ (`np.int16`).
  - $t = B + \lfloor (R - B) / 2 \rfloor \in [0, 255]$ (`np.int16`).
  - $Cg = G - t \in [-255, 255]$ (`np.int16`).
  - $Y = t + \lfloor (G - t) / 2 \rfloor \in [0, 255]$ (`np.int16`).

### 4.3 Full-Frame Padded Luma Plane: `luma_of_padded(padded)` (`visioncore/desc.py:44-51`)

- Anchor: `# @anchor:vc.s1.luma_of_padded` (`visioncore/desc.py:44`).
- Computes the `np.int16` YCoCg-R luma plane of an entire padded frame `(gh * s, gw * s, 3)` (`visioncore/desc.py:47-51`):
  1. `r = padded[..., 0].astype(np.int16)` (`visioncore/desc.py:47`)
  2. `g = padded[..., 1].astype(np.int16)` (`visioncore/desc.py:48`)
  3. `b = padded[..., 2].astype(np.int16)` (`visioncore/desc.py:49`)
  4. `t = b + ((r - b) >> 1)` (`visioncore/desc.py:50`)
  5. Returns `t + ((g - t) >> 1)` (`visioncore/desc.py:51`), shape `(gh * s, gw * s)`, dtype `np.int16`.
- Luma plane handling in `describe_padded` (`visioncore/desc.py:71-72, 95-98`):
  - If `len(idx) > 0` (at least one tile must be computed) and `y_plane is None`, `describe_padded` calls `y_plane = luma_of_padded(padded)` (`visioncore/desc.py:96-97`) and passes `y_plane` to `_compute(out, idx, padded, grid, p, y_plane)` (`visioncore/desc.py:98`).
  - If the caller passes a precomputed `y_plane` (not `None`), `describe_padded` passes that `y_plane` directly to `_compute` without calling `luma_of_padded` (`visioncore/desc.py:96-98`).
  - If `len(idx) == 0` (all tiles are reused via `prev_tiles` and `words_prev`), `luma_of_padded` is not called (`visioncore/desc.py:95-98`).

### 4.4 Per-Tile Luma and Chroma Inside `_compute` (`visioncore/desc.py:154-158`)

Inside `_compute(out, idx, padded, grid, p, y_plane)` (`visioncore/desc.py:148-158`), `_compute` does not call `ycocg(T)`; instead it reuses `y_plane` for `y` and computes only `co` and `cg` from the gathered tile pixels `T = _gather(padded, grid, idx)`:

1. `y = y_plane.reshape(grid.gh, s, grid.gw, s)[grid.ty[idx], :, grid.tx[idx], :]` (`visioncore/desc.py:155`, shape `(M, s, s)`, dtype `np.int16`).
2. `b = T[..., 2].astype(np.int16)` (`visioncore/desc.py:156`).
3. `co = np.subtract(T[..., 0], b, dtype=np.int16)` (`visioncore/desc.py:157`, shape `(M, s, s)`, dtype `np.int16`).
4. `cg = np.subtract(T[..., 1], b + (co >> 1), dtype=np.int16)` (`visioncore/desc.py:158`, shape `(M, s, s)`, dtype `np.int16`).

---

## 5. Integer Square Root (`isqrt`)

### 5.1 Function Specification: `isqrt(v)` (`visioncore/desc.py:22-29`)

- Anchor: `# @anchor:vc.s1.isqrt` (`visioncore/desc.py:22`).
- Domain stated in docstring: `int64` array `v` with `0 <= v < 2**52` (`visioncore/desc.py:24`).
- Exact 4-step computation (`visioncore/desc.py:25-29`):
  1. `v = np.asarray(v, dtype=np.int64)` (`visioncore/desc.py:25`) — converts input to `np.int64`.
  2. `r = np.floor(np.sqrt(v.astype(np.float64))).astype(np.int64)` (`visioncore/desc.py:26`) — computes a `float64` square-root seed, floors it, and casts to `np.int64`.
  3. `r -= (r * r > v)` (`visioncore/desc.py:27`) — subtracts `1` (boolean `True` coerced to `1`) wherever `r * r > v` in `int64` arithmetic.
  4. `r += ((r + 1) * (r + 1) <= v)` (`visioncore/desc.py:28`) — adds `1` wherever `(r + 1) * (r + 1) <= v` in `int64` arithmetic.
  5. Returns `r` (`visioncore/desc.py:29`), dtype `np.int64`, equal to $\lfloor \sqrt{v} \rfloor$.

### 5.2 Usage in `_compute` (`visioncore/desc.py:196`)

- Called at `visioncore/desc.py:196`: `sigma = isqrt(n * sum_y2 - sum_y * sum_y)`.
- Here `n = s * s` (`visioncore/desc.py:151`), `sum_y` is $\sum Y$ (`int64`, `visioncore/desc.py:159`), and `sum_y2` is $\sum Y^2$ (`int64`, `visioncore/desc.py:161`).
- By Cauchy–Schwarz, $n \cdot \text{sum\_y2} - \text{sum\_y}^2 = n^2 \operatorname{Var}(Y) \ge 0$, and for $Y \in [0, 255]$ and $s \le 128$ ($n \le 16384$), $n \cdot \text{sum\_y2} \le 16384^2 \times 65025 \approx 1.745 \times 10^{13} < 2^{45} < 2^{52}$.

---

## 6. Per-Tile Intermediate Accumulators in `_compute`

For the `M = len(idx)` tiles requiring computation (`visioncore/desc.py:148-174`):

- **Accumulator dtype (`acc`)**:
  - `acc = np.int32 if s <= 128 else np.int64` (`visioncore/desc.py:153`).
- **Luma and Chroma sums (`visioncore/desc.py:159-163`)**:
  - `sum_y = y.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)` (`visioncore/desc.py:159`)
  - `y32 = y.astype(acc)` (`visioncore/desc.py:160`)
  - `sum_y2 = np.einsum("mi,mi->m", y32.reshape(M, n), y32.reshape(M, n)).astype(np.int64)` (`visioncore/desc.py:161`)
  - `sum_co = co.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)` (`visioncore/desc.py:162`)
  - `sum_cg = cg.reshape(M, n).sum(axis=1, dtype=acc).astype(np.int64)` (`visioncore/desc.py:163`)
- **Intra-tile L1 gradient map `gs`, `grad_sum`, and edge count `e` (`visioncore/desc.py:165-170`)**:
  - `gs = np.zeros((M, s, s), dtype=np.int16)` (`visioncore/desc.py:165`)
  - `gs[:, :, :-1] = np.abs(y[:, :, 1:] - y[:, :, :-1])` (`visioncore/desc.py:166`) — horizontal forward absolute difference for columns `0 .. s-2` (column `s-1` has `0` horizontal contribution; does not cross tile boundaries).
  - `gs[:, :-1, :] += np.abs(y[:, 1:, :] - y[:, :-1, :])` (`visioncore/desc.py:167`) — vertical forward absolute difference added for rows `0 .. s-2` (row `s-1` has `0` vertical contribution; does not cross tile boundaries).
  - `gs = gs.reshape(M, n)` (`visioncore/desc.py:168`)
  - `grad_sum = gs.sum(axis=1, dtype=acc).astype(np.int64)` (`visioncore/desc.py:169`)
  - `e = np.count_nonzero(gs > p["TAU_EDGE"], axis=1).astype(np.int64)` (`visioncore/desc.py:170`) — counts pixels per tile where `gs` is strictly greater than `p["TAU_EDGE"]`.
  - Cross-reference to `visioncore/params.py:23`: `"TAU_EDGE": (48, "grey", "CORE", False, (1, 255))` (`# @anchor:vc.p.TAU_EDGE`), default `48`, not profile-overridable (`False`).
- **Saturated pixel count `sat` (`visioncore/desc.py:171-173`)**:
  - `d = T - np.uint8(1)` (`visioncore/desc.py:171`) — `uint8` modular wrap maps channel value `0 -> 255`, `255 -> 254`, and `1..254 -> 0..253`.
  - `sm = np.maximum(np.maximum(d[..., 0], d[..., 1]), d[..., 2]) >= 254` (`visioncore/desc.py:172`) — `True` at a pixel iff at least one RGB channel in `T` is `0` or `255`.
  - `sat = np.count_nonzero(sm.reshape(M, n), axis=1).astype(np.int64)` (`visioncore/desc.py:173`).

---

## 7. Descriptor Fields Specification

### 7.1 Summary Table of Stage S1 Descriptor Fields

| Field | Offset (`layout.py:97`) | Dtype (`desc.py:13`) | Formula in `desc.py` | Scale | Clamp in `desc.py` | Value Range | Source (`file:line`) |
|---|---:|---|---|---|---|---|---|
| `y_q4` | `4` | `<u2` (`uint16`) | `16 * sum_y // n` | Q4 ($\times 16$) | `ABSENT` (none in code) | `[0, 4080]` | `visioncore/desc.py:193` |
| `co_h` | `6` | `i1` (`int8`) | `np.clip(sum_co // (2 * n), -128, 127)` | $\times 1/2$ (`// (2 * n)`) | `np.clip(..., -128, 127)` | `[-128, 127]` | `visioncore/desc.py:194` |
| `cg_h` | `7` | `i1` (`int8`) | `np.clip(sum_cg // (2 * n), -128, 127)` | $\times 1/2$ (`// (2 * n)`) | `np.clip(..., -128, 127)` | `[-128, 127]` | `visioncore/desc.py:195` |
| `sig_q4` | `8` | `<u2` (`uint16`) | `16 * isqrt(n * sum_y2 - sum_y * sum_y) // n` | Q4 ($\times 16$) | `ABSENT` (none in code) | `[0, 2040]` | `visioncore/desc.py:196-197` |
| `grad_q6` | `10` | `<u2` (`uint16`) | `64 * grad_sum // n` | Q6 ($\times 64$) | `ABSENT` (none in code) | `[0, 32640)` | `visioncore/desc.py:198` |
| `edge` | `12` | `u1` (`uint8`) | `255 * e // n` | $\times 255 / n$ | `ABSENT` (none in code) | `[0, 255]` | `visioncore/desc.py:170, 199` |
| `sig64` | `16` | `<u8` (`uint64`) | `np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0].astype(np.uint64)` | 64-bit bitmask (1 bit per $b \times b$ sub-block) | `ABSENT` (bitmask) | `[0, 2**64 - 1]` | `visioncore/desc.py:175-185, 200` |
| `xh64` | `24` | `<u8` (`uint64`) | Little-endian `uint64` of `blake2b(tile_rgb_bytes, digest_size=8, person=b"vqa-tile")` (or `0` if `lazy_xh=True`) | 64-bit hash | `ABSENT` (hash digest) | `[0, 2**64 - 1]` | `visioncore/desc.py:54-57, 99-109, 113-127, 141-145` |
| `flags` | `3` | `u1` (`uint8`) | Bitwise OR of `F_EDGE`, `F_TEXTURE`, `F_SAT`, `F_PAD` | 8-bit bitmask | `ABSENT` (bitmask) | Subset of `0x8B` (`_DESC_FLAGS`) | `visioncore/desc.py:19, 131, 187-192` |

### 7.2 Detailed Field Definitions

1. **`y_q4`** (`visioncore/desc.py:193`, offset `4` at `visioncore/layout.py:95, 97`):
   - Formula: `out["y_q4"][idx] = 16 * sum_y // n` (`visioncore/desc.py:193`), where `sum_y` is `int64` (`visioncore/desc.py:159`) and `n = s * s` (`visioncore/desc.py:151`).
   - Division `//` is integer floor division ($\lfloor 16 \sum Y / n \rfloor$).
   - Scale: Q4 ($16 \times$ mean luma).
   - Clamp: `ABSENT` in code; since $0 \le Y \le 255$, $0 \le \text{sum\_y} \le 255n$, so $0 \le \text{y\_q4} \le 4080$.
   - Dtype: `<u2` (`np.uint16`, `visioncore/desc.py:13`).

2. **`co_h`** (`visioncore/desc.py:194`, offset `6` at `visioncore/layout.py:95, 97`):
   - Formula: `out["co_h"][idx] = np.clip(sum_co // (2 * n), -128, 127)` (`visioncore/desc.py:194`), where `sum_co` is `int64` (`visioncore/desc.py:162`).
   - Division `//` is floor division toward $-\infty$ ($\lfloor \sum Co / (2n) \rfloor$).
   - Scale: Half-scale ($1/2 \times$ mean $Co$).
   - Clamp: `np.clip(..., -128, 127)` (`visioncore/desc.py:194`).
   - Dtype: `i1` (`np.int8`, `visioncore/desc.py:13`).

3. **`cg_h`** (`visioncore/desc.py:195`, offset `7` at `visioncore/layout.py:95, 97`):
   - Formula: `out["cg_h"][idx] = np.clip(sum_cg // (2 * n), -128, 127)` (`visioncore/desc.py:195`), where `sum_cg` is `int64` (`visioncore/desc.py:163`).
   - Division `//` is floor division toward $-\infty$ ($\lfloor \sum Cg / (2n) \rfloor$).
   - Scale: Half-scale ($1/2 \times$ mean $Cg$).
   - Clamp: `np.clip(..., -128, 127)` (`visioncore/desc.py:195`).
   - Dtype: `i1` (`np.int8`, `visioncore/desc.py:13`).

4. **`sig_q4`** (`visioncore/desc.py:196-197`, offset `8` at `visioncore/layout.py:95, 97`):
   - Formula:
     - `sigma = isqrt(n * sum_y2 - sum_y * sum_y)` (`visioncore/desc.py:196`)
     - `out["sig_q4"][idx] = 16 * sigma // n` (`visioncore/desc.py:197`)
   - Computes $\lfloor 16 \cdot \lfloor \sqrt{n \sum Y^2 - (\sum Y)^2} \rfloor / n \rfloor$.
   - Scale: Q4 ($16 \times$ sample standard deviation of tile luma).
   - Clamp: `ABSENT` in code; for $Y \in [0, 255]$, $\sigma \le \lfloor 127.5 n \rfloor$, so $0 \le \text{sig\_q4} \le 2040$.
   - Dtype: `<u2` (`np.uint16`, `visioncore/desc.py:13`).

5. **`grad_q6`** (`visioncore/desc.py:165-169, 198`, offset `10` at `visioncore/layout.py:95, 97`):
   - Formula: `out["grad_q6"][idx] = 64 * grad_sum // n` (`visioncore/desc.py:198`), where `grad_sum` is the tile sum of `gs(r, c) = |Y(r, c+1) - Y(r, c)| [c < s-1] + |Y(r+1, c) - Y(r, c)| [r < s-1]` (`visioncore/desc.py:165-169`).
   - Division `//` is integer floor division ($\lfloor 64 \cdot \text{grad\_sum} / n \rfloor$).
   - Scale: Q6 ($64 \times$ mean per-pixel L1 forward difference).
   - Clamp: `ABSENT` in code; since $gs(r, c) \le 510$, $\text{grad\_q6} \le 64 \times 510 = 32640$.
   - Dtype: `<u2` (`np.uint16`, `visioncore/desc.py:13`).

6. **`edge`** (`visioncore/desc.py:170, 199`, offset `12` at `visioncore/layout.py:96, 97`):
   - Formula: `out["edge"][idx] = 255 * e // n` (`visioncore/desc.py:199`), where `e = np.count_nonzero(gs > p["TAU_EDGE"], axis=1).astype(np.int64)` (`visioncore/desc.py:170`) and default `TAU_EDGE = 48` (`visioncore/params.py:23`).
   - Division `//` is integer floor division ($\lfloor 255 \cdot e / n \rfloor$).
   - Scale: $255 / n$ (maps fraction of edge pixels $e/n \in [0, 1]$ into $\{0, \dots, 255\}$).
   - Clamp: `ABSENT` in code; since $0 \le e \le n$, $0 \le \text{edge} \le 255$.
   - Dtype: `u1` (`np.uint8`, `visioncore/desc.py:13`).

7. **`flags`** (`visioncore/desc.py:19, 131, 187-192`, offset `3` at `visioncore/layout.py:95, 97`):
   - Dtype: `u1` (`np.uint8`, `visioncore/desc.py:13, 187`).
   - Flag bit constants in `visioncore/desc.py:19` corresponding to `TILE_FLAGS` in `visioncore/layout.py:66`:

| Constant (`desc.py:19`) | Name in `TILE_FLAGS` (`layout.py:66`) | Bit / Value | Condition Set in `_compute` (`desc.py:187-192`) | Included in `_DESC_FLAGS` (`desc.py:131`) |
|---|---|---|---|---|
| `F_EDGE` | `"EDGE"` | `1 << 0` (`1`, `0x01`) | `(2 * e >= s) & (e < 4 * s)` (`visioncore/desc.py:188`) | Yes |
| `F_TEXTURE` | `"TEXTURE"` | `1 << 1` (`2`, `0x02`) | `e >= 4 * s` (`visioncore/desc.py:189`) | Yes |
| `F_SHADE_ONLY` | `"SHADE_ONLY"` | `1 << 2` (`4`, `0x04`) | Not set in S1 (`ABSENT` in `desc.py`) | No |
| `F_SAT` | `"SAT"` | `1 << 3` (`8`, `0x08`) | `4 * sat >= n` (`visioncore/desc.py:190`) | Yes |
| `F_UI_OCC` | `"UI_OCC"` | `1 << 4` (`16`, `0x10`) | Not set in S1 (`ABSENT` in `desc.py`) | No |
| `F_ACTOR_OCC` | `"ACTOR_OCC"` | `1 << 5` (`32`, `0x20`) | Not set in S1 (`ABSENT` in `desc.py`) | No |
| `F_GLOBAL_MV` | `"GLOBAL_MV"` | `1 << 6` (`64`, `0x40`) | Not set in S1 (`ABSENT` in `desc.py`) | No |
| `F_PAD` | `"PAD"` | `1 << 7` (`128`, `0x80`) | `grid.pad_flags()[idx]` is truthy (`visioncore/desc.py:191`) | Yes |

   - `F_EDGE` and `F_TEXTURE` are mutually exclusive because `e < 4 * s` (`visioncore/desc.py:188`) and `e >= 4 * s` (`visioncore/desc.py:189`) are disjoint.

---

## 8. Hashing and Signature Computation (`sig64` and `xh64`)

### 8.1 64-Bit Sub-Block Luma Signature (`sig64`, `visioncore/desc.py:175-185, 200`)

1. **Sub-block size**: `b = s // 8` (`visioncore/desc.py:175`). Since $s \ge 16$ is a power of two (`visioncore/layout.py:19-24`), $b = s / 8 \ge 2$ is an exact integer, partitioning each $s \times s$ tile into an $8 \times 8$ grid of 64 sub-blocks of $b \times b$ pixels.
2. **Horizontal summation across $b$ columns** (`visioncore/desc.py:176-179`):
   - `y4 = y.reshape(M, s, 8, b)` (`visioncore/desc.py:176`).
   - `cs = y4[..., 0].astype(acc)` (`visioncore/desc.py:177`), then `for k in range(1, b): cs += y4[..., k]` (`visioncore/desc.py:178-179`).
3. **Vertical summation across $b$ rows** (`visioncore/desc.py:180-183`):
   - `cs = cs.reshape(M, 8, b, 8)` (`visioncore/desc.py:180`).
   - `sub = cs[:, :, 0].astype(np.int64)` (`visioncore/desc.py:181`), then `for k in range(1, b): sub += cs[:, :, k]` (`visioncore/desc.py:182-183`).
   - `sub` has shape `(M, 8, 8)` and dtype `np.int64`, where `sub[m, i, j]` is the sum of $Y$ over rows `i * b .. (i + 1) * b - 1` and columns `j * b .. (j + 1) * b - 1` of tile `m`.
4. **Thresholding against tile luma sum** (`visioncore/desc.py:184`):
   - `bits = (sub * n > sum_y[:, None, None] * (b * b)).reshape(M, 64)` (`visioncore/desc.py:184`).
   - Uses strict inequality `>` in `int64`. Flattening `(8, 8)` in row-major order maps sub-block `(i, j)` ($0 \le i, j < 8$) to bit index $k = 8i + j \in \{0, \dots, 63\}$.
5. **Packing into `<u8` (`uint64`)** (`visioncore/desc.py:185, 200`):
   - `sig64 = np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0].astype(np.uint64)` (`visioncore/desc.py:185`).
   - `bitorder="little"` packs bit $k \in \{0, \dots, 63\}$ into bit position $k$ (weight $2^k = 1 \ll (8i + j)$) of the little-endian 64-bit unsigned integer `sig64`.
   - Stored via `out["sig64"][idx] = sig64` (`visioncore/desc.py:200`, offset `16` at `visioncore/layout.py:96-97`).

### 8.2 64-Bit Exact Pixel Hash (`xh64`, `visioncore/desc.py:54-57, 99-109, 113-127, 141-145`)

1. **Hash algorithm and personalization (`xh64_of` and `_hash_tiles`)**:
   - Single-tile helper `xh64_of(block)` (`visioncore/desc.py:54-57`, anchor `# @anchor:vc.s1.xh64_of`):
     - `h = hashlib.blake2b(np.ascontiguousarray(block).tobytes(), digest_size=8, person=b"vqa-tile").digest()` (`visioncore/desc.py:56`).
     - Returns `int.from_bytes(h, "little")` (`visioncore/desc.py:57`).
   - Batch helper `_hash_tiles(padded, grid, idx)` (`visioncore/desc.py:141-145`):
     - `T = _gather(padded, grid, idx)` (`visioncore/desc.py:142`, shape `(len(idx), s, s, 3)`, `uint8`).
     - `rows = T.reshape(len(idx), -1)` (`visioncore/desc.py:143`, each `rows[k]` is the $3s^2$ row-major padded RGB bytes of tile `idx[k]`).
     - `dig = b"".join(hashlib.blake2b(rows[k], digest_size=8, person=b"vqa-tile").digest() for k in range(len(idx)))` (`visioncore/desc.py:144`).
     - Returns `np.frombuffer(dig, dtype="<u8")` (`visioncore/desc.py:145`).
2. **Eager mode in `describe_padded` (`lazy_xh=False`, default at `visioncore/desc.py:67, 101-109`)**:
   - Initializes `xh = np.zeros(N, dtype=np.uint64)` (`visioncore/desc.py:101`).
   - Note: if `prev_xh64 is None` and `reuse` is `True` (`prev_tiles is not None and words_prev is not None`), `prev_xh64` was set to `prev_tiles["xh64"]` at `visioncore/desc.py:87-88`.
   - If `prev_xh64 is not None` (`visioncore/desc.py:102`):
     - `xh[words_eq] = np.asarray(prev_xh64, dtype=np.uint64)[words_eq]` (`visioncore/desc.py:103`).
     - `hidx = np.nonzero(~words_eq)[0]` (`visioncore/desc.py:104`).
   - Else (`prev_xh64 is None`, `visioncore/desc.py:105`):
     - `hidx = np.arange(N)` (`visioncore/desc.py:106`).
   - If `len(hidx)` is non-zero, `xh[hidx] = _hash_tiles(padded, grid, hidx)` (`visioncore/desc.py:107-108`).
   - Sets `out["xh64"] = xh` (`visioncore/desc.py:109`).
3. **Lazy mode in `describe_padded` (`lazy_xh=True`) and post-S2 `fill_xh64` (`visioncore/desc.py:99-100, 113-127`)**:
   - When `lazy_xh=True`, `describe_padded` returns `(out, words_eq)` immediately at `visioncore/desc.py:99-100` before `xh` is allocated or computed, leaving `out["xh64"] == 0` for all `N` tiles.
   - `fill_xh64(out, padded, grid, prev_xh64=None, hash_all=False)` (`visioncore/desc.py:113-127`, anchor `# @anchor:vc.s1.fill_xh64`):
     - Reads `cls = out["cls"]` (`visioncore/desc.py:117`).
     - If `prev_xh64 is None` (`visioncore/desc.py:118`): sets `hash_all = True` (`visioncore/desc.py:119`) and `xh = np.zeros(grid.N, dtype=np.uint64)` (`visioncore/desc.py:120`).
     - Else (`visioncore/desc.py:121`): sets `xh = np.array(prev_xh64, dtype=np.uint64)` (`visioncore/desc.py:122`).
     - Selects `idx = np.arange(grid.N) if hash_all else np.nonzero(cls >= layout.CLS_DIRTY_MIN)[0]` (`visioncore/desc.py:123`), where `layout.CLS_DIRTY_MIN = 2` (`visioncore/layout.py:65`).
     - If `len(idx)` is non-zero, computes `xh[idx] = _hash_tiles(padded, grid, idx)` (`visioncore/desc.py:124-125`). Thus, when `prev_xh64 is not None` and `hash_all=False`, only tiles with `cls >= 2` are hashed with BLAKE2b-64, while tiles with `cls < 2` (`0` `"UNCHANGED"` and `1` `"JITTER"`, `visioncore/layout.py:63, 65`) retain their copied `prev_xh64` values (`visioncore/desc.py:114-116, 122-125`).
     - Assigns `out["xh64"] = xh` in place and returns `out` (`visioncore/desc.py:126-127`).

---

## 9. Word-Equality (`words_eq`) and Previous-Frame Reuse Rule

### 9.1 Word-Equality Computation (`visioncore/desc.py:81-85`)

- If `words_prev is None` (`visioncore/desc.py:81`):
  - `words_eq = np.zeros(N, dtype=bool)` (`visioncore/desc.py:82`).
- If `words_prev is not None` (`visioncore/desc.py:83`):
  - `eq = (padded == words_prev).reshape(gh, s, gw, s * 3).all(axis=(1, 3))` (`visioncore/desc.py:84`) — `True` for raster tile `(ty, tx)` iff every one of its `s * (s * 3)` padded `uint8` RGB bytes equals `words_prev`.
  - `words_eq = eq.reshape(-1)[grid.flat]` (`visioncore/desc.py:85`) — reorders the boolean mask from raster order into Morton rank order (`bool[N]`).

### 9.2 Descriptor Reuse Rule (`visioncore/desc.py:86-98, 101-109, 130-131`)

- Reuse condition: `reuse = prev_tiles is not None and words_prev is not None` (`visioncore/desc.py:86`).
- If `prev_xh64 is None and reuse`: `prev_xh64 = prev_tiles["xh64"]` (`visioncore/desc.py:87-88`).
- Computed tile indices: `idx = np.nonzero(~words_eq)[0] if reuse else np.arange(N)` (`visioncore/desc.py:89`).
- Unchanged tile indices when `reuse` is `True`: `keep = np.nonzero(words_eq)[0]` (`visioncore/desc.py:90-91`).

When a tile's pixels are unchanged (`words_eq[k] == True`) and `reuse` is `True`, each field of `out[keep]` is handled as follows:

| Field(s) | Action on Unchanged Tiles (`keep`) when `reuse` is `True` | Constant / Mask | Source (`file:line`) |
|---|---|---|---|
| `y_q4`, `co_h`, `cg_h`, `sig_q4`, `grad_q6`, `edge`, `sig64` | Copied verbatim from `prev_tiles[f][keep]` | `_COPY_FIELDS = ("y_q4", "co_h", "cg_h", "sig_q4", "grad_q6", "edge", "sig64")` | `visioncore/desc.py:92-93, 130` |
| `flags` | Copied with bitwise AND mask: `prev_tiles["flags"][keep] & _DESC_FLAGS` (retains `F_EDGE`, `F_TEXTURE`, `F_SAT`, `F_PAD`; strips `F_SHADE_ONLY`, `F_UI_OCC`, `F_ACTOR_OCC`, `F_GLOBAL_MV`) | `_DESC_FLAGS = F_EDGE \| F_TEXTURE \| F_SAT \| F_PAD` (`0x8B`) | `visioncore/desc.py:94, 131` |
| `xh64` | When `lazy_xh=False`: copied from `prev_xh64[words_eq]` (which defaults to `prev_tiles["xh64"]` at line `88` if `prev_xh64` was `None`). When `lazy_xh=True`: left `0` in `describe_padded` (`visioncore/desc.py:99-100`) | `prev_xh64` | `visioncore/desc.py:87-88, 99-103, 109` |
| `morton` | Set from `grid.mortons` (not copied from `prev_tiles`) | `grid.mortons` | `visioncore/desc.py:80` |
| `cls`, `resid`, `mv_dx`, `mv_dy` | Left `0` from `np.zeros(N, dtype=TILE_DTYPE)` (never copied from `prev_tiles`) | `0` | `visioncore/desc.py:73, 79` |

Note: If `prev_tiles is None` (`reuse` is `False`) but `words_prev is not None` and `prev_xh64 is not None`, `_compute` runs on all `N` tiles (`idx = np.arange(N)`, `visioncore/desc.py:89`), while `xh[words_eq]` still copies `prev_xh64[words_eq]` and `_hash_tiles` runs only on `hidx = np.nonzero(~words_eq)[0]` (`visioncore/desc.py:102-108`, when `lazy_xh=False`).

---

## 10. Items Marked `ABSENT` in the Bound Source Files

The following items are referenced by or adjacent to `visioncore/desc.py` but are **not defined** in the four bound source files (`visioncore/desc.py`, `visioncore/layout.py`, `visioncore/params.py`, `AGENTS.md`) and are therefore marked `ABSENT`:

1. **`Grid` class implementation (`visioncore/grid.py`)**: `ABSENT` from the bound source files. `visioncore/desc.py` accesses `grid.s`, `grid.gw`, `grid.gh`, `grid.N`, `grid.H`, `grid.W`, `grid.ident`, `grid.pad(frame)`, `grid.mortons`, `grid.flat`, `grid.ty`, `grid.tx`, and `grid.pad_flags()` (`visioncore/desc.py:62-64, 75, 78, 80, 85, 120, 123, 136-138, 150, 155, 191`), while only `grid_params`, `grid_ident`, `morton`, and `unmorton` are defined in `visioncore/layout.py:15-59`.
2. **`VqaError` class implementation (`visioncore/errors.py`)**: `ABSENT` from the bound source files. Imported at `visioncore/desc.py:8`, `visioncore/layout.py:5`, and `visioncore/params.py:6`; exit code mapping (`0 OK, 2 usage/schema, 3 reject, 4 stale, 5 gate, 6 budget`) is summarized in `AGENTS.md:37`.
3. **`canon` module implementation (`visioncore/canon.py`)**: `ABSENT` from the bound source files. Imported at `visioncore/params.py:5` and called via `canon.sha256_obj` at `visioncore/params.py:86`.
4. **Rules for setting non-S1 tile flags (`F_SHADE_ONLY`, `F_UI_OCC`, `F_ACTOR_OCC`, `F_GLOBAL_MV`) and Stage S2 fields (`cls`, `resid`, `mv_dx`, `mv_dy`)**: `ABSENT` from the bound source files. In `visioncore/desc.py`, those flags are defined as constants (`visioncore/desc.py:19`) and masked out on reuse (`visioncore/desc.py:94, 131`), and `cls`, `resid`, `mv_dx`, `mv_dy` are initialized to `0` and left `0` for Stage S2 (`visioncore/desc.py:73, 79`).
5. **Explicit clamp operations on `y_q4`, `sig_q4`, `grad_q6`, and `edge`**: `ABSENT` in `visioncore/desc.py:193, 197-199` (only `co_h` and `cg_h` apply `np.clip(..., -128, 127)` at `visioncore/desc.py:194-195`).
6. **External specification documents (`SPEC_CORE §2`, `SPEC_INT I2`, `VG-0040`)**: Referenced in comments/docstrings (`visioncore/desc.py:2, 33, 72, 114`), `ABSENT` from the bound source files.
