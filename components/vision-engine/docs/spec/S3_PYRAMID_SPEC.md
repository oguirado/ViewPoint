# Stage S3 Pyramid Specification (`vc.s3`)

This document is the implementation specification of Stage S3, the max-residual quadtree pyramid over Morton index space, derived exclusively from the bound source files at branch **bridge/integration**:

- `visioncore/pyramid.py:1-121` (`vc.s3`, Layer 2, 121 lines, 5,412 bytes, SHA-256 **523dc72cfbb918bbe23bdf7f2dbe23b5f2d566f1e3c5b18889a0a7bd66fb01de**)
- `visioncore/layout.py:1-139` (`vc.layout`, Layer 0, 139 lines, 4,714 bytes, SHA-256 **dcd341cc32ef984a0b009c20e5e8dc9ad08cf886d109549efbb3d06ba16327fe**)
- `tests/test_pyramid.py:1-147` (S3 unit test suite, 147 lines, 5,923 bytes, SHA-256 **cc7ff538a33b7f4b4b4dbcd5a289ee9f7d2a0cf153f6f79df44924bf9118ce30**)

Where any behaviour is not defined by the bound source code, this specification explicitly marks it **ABSENT**.

## 1. Module Identity, Dependencies, and Invariants

- **Module metadata header** (`visioncore/pyramid.py:1`): declares `"id":"vc.s3"`, `"layer":2`, `"stage":"S3"`, `"owner":"CORE"`, `"deps":["vc.errors","vc.ids","vc.layout","vc.p"]`, `"inputs":"tile records (morton, cls, resid), Grid"`, `"outputs":"max-residual quadtree, bounded node selection, O(1) AABB query"`, `"invariants":["integer-only","deterministic","zero-subtree-skip"]`, `"anti_uses":["float thresholds","random selectors"]`, `"oracle":"tests/test_pyramid.py"`, and `"book":"HB-VC-s3-pyramid"`.
- **Module docstring** (`visioncore/pyramid.py:2`): identifies `@module vc.s3 L2 - S3 max-residual quadtree pyramid over Morton index space (SPEC_CORE §4)`.
- **Imports** (`visioncore/pyramid.py:3-8`): imports `heapq` from the Python standard library, `numpy as np`, relative modules `from . import ids, layout, params as P`, and `from .errors import VqaError`.
- **Supporting L0 module** (`visioncore/layout.py:1-7`): declares `"id":"vc.layout"`, `"layer":0`, `"stage":"L0"`, `"owner":"ARCH"`, `"deps":["vc.errors","vc.p"]`, `"book":"HB-VC-a0-arch"`, and imports `struct`, `from .errors import VqaError`, and `from .params import PARAMS`.

## 2. Public Functions, Classes, and Methods: Inputs and Outputs

### 2.1 Public Symbol Summary in `visioncore/pyramid.py:11-121`

| Symbol Signature | Anchor | Citation | Inputs | Output |
|---|---|---|---|---|
| `levels_for(gw, gh)` | `vc.s3.levels_for` | `visioncore/pyramid.py:11-13` | Grid width `gw` and height `gh` in tiles (integers) | Integer pyramid height **L >= 1** via `max(1, (max(gw, gh) - 1).bit_length())` |
| `id_level_cap(L)` | None (**ABSENT**) | `visioncore/pyramid.py:16-24` | Integer pyramid depth `L` | Highest integer level `k` in `range(L, 0, -1)` accepted by `ids.node_id(0, k, 0)`, or `0` |
| `r_sel(params=None)` | `vc.s3.r_sel` | `visioncore/pyramid.py:27-30` | Optional parameter mapping `params` (defaults to `P.PARAMS`) | Integer selection threshold **R_SEL** (`T_exit >> 2`) in `resid` units |
| `class Pyramid:` / `__init__(self, tiles, grid)` | `vc.s3.pyramid` | `visioncore/pyramid.py:33-53` | Structured tile array `tiles` (`"morton"`, `"cls"`, `"resid"`) and `grid` (`grid.gw`, `grid.gh`) | Initialised `Pyramid` instance with attributes `self.L`, `self.grid`, `self.mx`, `self.cnt`, `self.real`, `self.last_visits`, `self.id_cap` |
| `node(self, level, i)` | `vc.s3.node` | `visioncore/pyramid.py:55-57` | Tree `level` (**0..L**) and level-local node index `i` (**0..4^(L-level)-1**) | 4-tuple `(level, m0, max, cnt)` computed as `(level, i << (2 * level), int(self.mx[level][i]), int(self.cnt[level][i]))` |
| `node_id(self, frame, level, i)` | `vc.s3.node_id` | `visioncore/pyramid.py:59-60` | Frame index `frame`, tree `level`, and level-local node index `i` | Canonical node identifier string via `ids.node_id(frame, level, i << (2 * level))` |
| `children(self, level, i)` | `vc.s3.children` | `visioncore/pyramid.py:62-66` | Tree `level` and level-local node index `i` | Ascending list of child indices `c` in `range(4 * i, 4 * i + 4)` where `self.real[level - 1][c]` is non-zero (`[]` if `level == 0`) |
| `bound(self, level, i)` | `vc.s3.bound` | `visioncore/pyramid.py:68-70` | Tree `level` and level-local node index `i` | Exact integer maximum residual `int(self.mx[level][i])` under the node |
| `query_aabb(self, x, y, w, h)` | `vc.s3.query_aabb` | `visioncore/pyramid.py:72-89` | Pixel rectangle origin `x`, `y` and dimensions `w`, `h` | Up to 4 node tuples `(level, m0, max, cnt)` at the smallest enclosing level `m`, sorted via `sorted(out, key=lambda n: n[1])` |
| `max_aabb(self, x, y, w, h)` | `vc.s3.max_aabb` | `visioncore/pyramid.py:91-92` | Pixel rectangle origin `x`, `y` and dimensions `w`, `h` | Integer maximum `max([n[2] for n in self.query_aabb(x, y, w, h)], default=0)` |
| `select(self, B, rsel=None)` | `vc.s3.select` | `visioncore/pyramid.py:94-121` | Positive integer node budget `B` (**B >= 1**) and optional threshold `rsel` | List `out` of **<= B** disjoint node tuples covering every tile with `resid > rsel`; sets `self.last_visits = visits` |

### 2.2 Module-Level Functions in `visioncore/pyramid.py:11-30`

1. **Function `levels_for(gw, gh)`** (`visioncore/pyramid.py:11-13`):
   - **Inputs**: `gw` (grid width in tiles) and `gh` (grid height in tiles).
   - **Operation**: Computes `return max(1, (max(gw, gh) - 1).bit_length())` (`visioncore/pyramid.py:13`).
   - **Output**: Python integer **L >= 1** representing the number of quadtree reduction levels above leaf level 0.
   - **Verified examples**: `tests/test_pyramid.py:32-34` verifies `[levels_for(*a) for a in ((80, 45), (60, 34), (64, 64), (1, 1), (2, 1))]` equals `[7, 6, 6, 1, 1]`, and `Pyramid(make(1280, 720, {})[1], Grid(1280, 720)).L` equals `7`.
2. **Function `id_level_cap(L)`** (`visioncore/pyramid.py:16-24`):
   - **Inputs**: Integer depth `L`.
   - **Operation**: Loops `for k in range(L, 0, -1):`, calls `ids.node_id(0, k, 0)` inside a `try` block, returns the first `k` that does not raise `VqaError`, and returns `0` if every `k` raises `VqaError` (`visioncore/pyramid.py:18-24`).
   - **Output**: Non-negative integer **0 <= id_cap <= L** giving the highest pyramid level representable by `ids.node_id`.
3. **Function `r_sel(params=None)`** (`visioncore/pyramid.py:27-30`):
   - **Inputs**: Optional dictionary `params`; when `params is None`, binds `p = P.PARAMS if params is None else params` (`visioncore/pyramid.py:29`).
   - **Operation**: Computes `return (p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000) >> 2` (`visioncore/pyramid.py:30`).
   - **Output**: Non-negative integer threshold `R_SEL = T_exit >> 2` in leaf `resid` units (`resid = r >> 2`, `visioncore/pyramid.py:28`).

### 2.3 `Pyramid` Constructor and Methods (`visioncore/pyramid.py:33-121`)

1. **Constructor `__init__(self, tiles, grid)`** (`visioncore/pyramid.py:36-53`):
   - **Inputs**:
     - `tiles`: 1-D NumPy structured array of length **N** providing fields `"morton"`, `"cls"`, and `"resid"` (`visioncore/pyramid.py:40-44`).
     - Test fixture dtype: `DT = np.dtype([("morton", "<u2"), ("cls", "u1"), ("resid", "u1")])` (`tests/test_pyramid.py:11`).
     - `grid`: Grid object providing attributes `grid.gw` and `grid.gh` in `__init__` (`visioncore/pyramid.py:36-81`), and `g.W`, `g.H`, and `g.s` in `query_aabb`.
     - The class definition of `Grid` imported via `from visioncore.grid import Grid` (`tests/test_pyramid.py:8`) is **ABSENT** from the bound files.
   - **Instance attributes set** (`visioncore/pyramid.py:37-53`):
     - `self.L`: integer level count from `self.L = L = levels_for(grid.gw, grid.gh)` (`visioncore/pyramid.py:37`).
     - `self.grid`: reference to `grid` (`visioncore/pyramid.py:38`).
     - `self.mx`: list of **L + 1** 1-D `np.uint8` arrays for levels **0 .. L** (`visioncore/pyramid.py:41-49`).
     - `self.cnt`: list of **L + 1** 1-D `np.uint16` arrays for levels **0 .. L** (`visioncore/pyramid.py:43-50`).
     - `self.real`: list of **L + 1** 1-D `np.uint16` arrays for levels **0 .. L** (`visioncore/pyramid.py:45-51`).
     - `self.last_visits`: integer visit counter initialised to `0` (`visioncore/pyramid.py:52`).
     - `self.id_cap`: integer level cap from `self.id_cap = id_level_cap(L)` (`visioncore/pyramid.py:53`).
2. **Method `node(self, level, i)`** (`visioncore/pyramid.py:55-57`):
   - **Inputs**: Integer `level` and node index `i`.
   - **Output**: Tuple `(level, i << (2 * level), int(self.mx[level][i]), int(self.cnt[level][i]))`, i.e., `(level, m0, max, cnt)` (`visioncore/pyramid.py:55-57`). Explicit bounds checking on `level` and `i` is **ABSENT** (delegated to Python/NumPy indexing).
3. **Method `node_id(self, frame, level, i)`** (`visioncore/pyramid.py:59-60`):
   - **Inputs**: Frame number `frame`, pyramid `level`, and level-local node index `i`.
   - **Output**: String returned by `ids.node_id(frame, level, i << (2 * level))` (`visioncore/pyramid.py:60`).
   - **Verified format**: `self.assertEqual(p.node_id(42, 2, 0x68 >> 4), "p42:L2:%04x" % ((0x68 >> 4) << 4))` (`tests/test_pyramid.py:143`).
4. **Method `children(self, level, i)`** (`visioncore/pyramid.py:62-66`):
   - **Inputs**: Pyramid `level` and level-local node index `i`.
   - **Output**: Empty list `[]` if `level == 0`, and otherwise `[c for c in range(4 * i, 4 * i + 4) if self.real[level - 1][c]]` (`visioncore/pyramid.py:64-66`).
5. **Method `bound(self, level, i)`** (`visioncore/pyramid.py:68-70`):
   - **Inputs**: Pyramid `level` and level-local node index `i`.
   - **Output**: `int(self.mx[level][i])` (`visioncore/pyramid.py:70`).
6. **Method `query_aabb(self, x, y, w, h)`** (`visioncore/pyramid.py:72-89`):
   - **Inputs**: Pixel rectangle origin `x`, `y` and size `w`, `h`.
   - **Output**: List of **<= 4** node tuples `(level, m0, max, cnt)` sorted by ascending **m0** via `sorted(out, key=lambda n: n[1])`, or `[]` if `w <= 0 or h <= 0` or if the clipped rectangle satisfies `x1 < x0 or y1 < y0` (`visioncore/pyramid.py:72-89`).
7. **Method `max_aabb(self, x, y, w, h)`** (`visioncore/pyramid.py:91-92`):
   - **Inputs**: Pixel rectangle `x, y, w, h`.
   - **Output**: `max([n[2] for n in self.query_aabb(x, y, w, h)], default=0)` (`visioncore/pyramid.py:92`).
8. **Method `select(self, B, rsel=None)`** (`visioncore/pyramid.py:94-121`):
   - **Inputs**: Integer budget `B` (**B >= 1**) and optional residual threshold `rsel` (defaults to `rs = r_sel() if rsel is None else rsel`, `visioncore/pyramid.py:94-98`).
   - **Output**: List `out` of **<= B** disjoint node tuples covering every tile with `resid > rsel`, setting `self.last_visits = visits` (`visioncore/pyramid.py:95-121`).

### 2.4 Public Supporting Functions in `visioncore/layout.py:15-139`

| Function Signature | Citation | Inputs | Output |
|---|---|---|---|
| `grid_params(W, H)` | `visioncore/layout.py:15-25` | Frame pixel width `W` and height `H` (`int` in **1..8192**) | 3-tuple `(s, gw, gh)` where `s` is tile size in px and `gw, gh` are tile counts |
| `grid_ident(W, H)` | `visioncore/layout.py:28-29` | Frame pixel dimensions `W, H` | Canonical string `"grid:%dx%d:s%d" % ((W, H, grid_params(W, H)[0]))` |
| `morton(tx, ty)` | `visioncore/layout.py:49-53` | Tile coordinates `tx, ty` satisfying `0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX` | 16-bit interleaved Morton integer via `_spread(tx)` and `(_spread(ty) << 1)` |
| `unmorton(m)` | `visioncore/layout.py:56-59` | 16-bit Morton integer `m` satisfying `0 <= m <= 0xFFFF` | 2-tuple **tx, ty** via `_compact(m), _compact(m >> 1)` |
| `cls_name(v)` | `visioncore/layout.py:74-77` | Integer class ID `v` in `0 <= v < len(CLS)` | Class name string `CLS[v]` |
| `cls_id(name)` | `visioncore/layout.py:80-83` | Class name string `name` in `CLS_ID` | Integer class ID `CLS_ID[name]` (**0..6**) |
| `is_dirty(cls)` | `visioncore/layout.py:86-88` | Integer class ID `cls` in **0..6** | Boolean `cls >= CLS_DIRTY_MIN` (**cls >= 2**) |
| `pack_tile_header(s, W, H, gw, gh, N, n_frames, f0, params_sha256_hex, api_version)` | `visioncore/layout.py:113-116` | Header fields `s, W, H, gw, gh, N, n_frames, f0, params_sha256_hex, api_version` | 64-byte packed `TILE_MAGIC` (**VQATILE1**) header |
| `parse_tile_header(b)` | `visioncore/layout.py:119-127` | Byte buffer `b` (**len(b) >= 64**) | Header dictionary with keys `"s"`, `"W"`, `"H"`, `"gw"`, `"gh"`, `"N"`, `"n_frames"`, `"f0"`, `"params_sha256_16"`, `"api_version"` |
| `unpack_tile(b, off=0)` | `visioncore/layout.py:130-131` | Byte buffer `b` and byte offset `off` | Dictionary `dict(zip(TILE_FIELDS, struct.unpack_from(TILE_FMT, b, off)))` |
| `check_tile_records(b)` | `visioncore/layout.py:134-139` | Byte buffer `b` (multiple of `TILE_SIZE` = 32 bytes) | **None** (validates `cls_name(b[off + 2])` for every 32 B record) |

## 3. Level Construction

### 3.1 Quadtree Depth `L`

Given grid dimensions `gw` and `gh`, the pyramid depth **L** is computed by `levels_for(gw, gh)` (`visioncore/pyramid.py:11-13`):

```python
def levels_for(gw, gh):  # @anchor:vc.s3.levels_for
    """L = max(1, ceil(log2(max(gw, gh)))) via bit_length (no float)."""
    return max(1, (max(gw, gh) - 1).bit_length())
```

- For $\max(\text{gw}, \text{gh}) \le 2$, `(max(gw, gh) - 1).bit_length()` is **0** or **1**, and `max(1, (max(gw, gh) - 1).bit_length())` clamps **L** to **1** (`visioncore/pyramid.py:13`).
- Verified test cases (`tests/test_pyramid.py:33`): `((80, 45), (60, 34), (64, 64), (1, 1), (2, 1))` maps to `[7, 6, 6, 1, 1]`.
- For $\max(\text{gw}, \text{gh}) > 2$, **L** is the smallest integer such that $2^L \ge \max(\text{gw}, \text{gh})$, which up to the axis limit `AXIS_MAX` = 255 (`visioncore/layout.py:11-22`) yields $L \in \{1, \dots, 8\}$.

### 3.2 Bottom-Up Array Allocation and Reduction (`visioncore/pyramid.py:36-53`)

`Pyramid.__init__(self, tiles, grid)` builds three parallel level pyramids `self.mx`, `self.cnt`, and `self.real`, each containing **L + 1** 1-D NumPy arrays indexed by level $k \in \{0, \dots, L\}$:

```python
        self.mx, self.cnt, self.real = [leaf], [cnt], [real]
        for _ in range(L):
            self.mx.append(self.mx[-1].reshape(-1, 4).max(axis=1))
            self.cnt.append(self.cnt[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))
            self.real.append(self.real[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))
```

1. **Leaf level ($k = 0$) initialization** (`visioncore/pyramid.py:39-46`):
   - `size = 4 ** L` (`visioncore/pyramid.py:39`).
   - `m = tiles["morton"].astype(np.int64)` (`visioncore/pyramid.py:40`).
   - `leaf = np.zeros(size, dtype=np.uint8)` followed by `leaf[m] = tiles["resid"]` (`visioncore/pyramid.py:41-42`).
   - `cnt = np.zeros(size, dtype=np.uint16)` followed by `cnt[m] = tiles["cls"] >= layout.CLS_DIRTY_MIN` (`visioncore/pyramid.py:43-44`).
   - `real = np.zeros(size, dtype=np.uint16)` followed by `real[m] = 1` (`visioncore/pyramid.py:45-46`).
2. **Coarser levels ($k = 1 \dots L$)** (`visioncore/pyramid.py:34-51`):
   - At each step $k \in \{1, \dots, L\}$, the previous level array of length $4^{L - k + 1}$ is reshaped via `.reshape(-1, 4)` into $(4^{L - k}, 4)$, grouping the 4 contiguous children $4i, 4i+1, 4i+2, 4i+3$ of each parent node $i \in \{0, \dots, 4^{L - k} - 1\}$ (`visioncore/pyramid.py:48-51`).
   - Level $k$ holds $4^{L - k}$ nodes (`visioncore/pyramid.py:34`).
   - The root level $k = L$ holds $4^0 = 1$ node at index **0** (`int(p.mx[p.L][0])`, `int(p.cnt[p.L][0])`, `int(p.real[p.L][0])` in `tests/test_pyramid.py:49-51`).

## 4. Residual, Dirty Count, and Max Rule per Node

### 4.1 Per-Node Aggregated Quantities

For any level $k \in \{0, \dots, L\}$ and node index $i \in \{0, \dots, 4^{L - k} - 1\}$, covering the leaf Morton interval $[i \cdot 4^k, (i + 1) \cdot 4^k)$ (`visioncore/pyramid.py:34`):

1. **Maximum residual (`self.mx`, dtype `np.uint8`)**:
   - At $k = 0$: `leaf[m] = tiles["resid"]` for each present tile at Morton index `m`, and **0** (`np.zeros(size, dtype=np.uint8)`) for any unpopulated slot (`visioncore/pyramid.py:41-42`).
   - At $k > 0$: `self.mx.append(self.mx[-1].reshape(-1, 4).max(axis=1))` (`visioncore/pyramid.py:49`).
   - By induction, level $k$ equals the exact maximum over the $4^k$ leaf slots `blk = leaf.reshape(-1, 4 ** k).max(axis=1)` (`tests/test_pyramid.py:46-48`).
   - `bound(self, level, i)` returns `int(self.mx[level][i])` (`visioncore/pyramid.py:68-70`).
   - Note: `visioncore/pyramid.py:28` states `R_SEL = T_exit >> 2 in resid units (resid = r >> 2)`; the upstream Stage S2 formula that computes `r` and writes `resid` is **ABSENT** from the bound files.
2. **Dirty tile count (`self.cnt`, dtype `np.uint16`)**:
   - At $k = 0$: `cnt[m] = tiles["cls"] >= layout.CLS_DIRTY_MIN` (`visioncore/pyramid.py:43-44`).
   - In `visioncore/layout.py:63-65`, `CLS_DIRTY_MIN = 2` selects the dirty classes `"CHANGED"`, `"MOVED"`, `"FLICKER"`, `"FROZEN"`, and `"DRIFT"` in `CLS`, whereas `"UNCHANGED"` (**0**), `"JITTER"` (**1**), and unpopulated slots have count **0**.
   - At $k > 0$: `self.cnt.append(self.cnt[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))` (`visioncore/pyramid.py:50`).
3. **Real tile count (`self.real`, dtype `np.uint16`)**:
   - At $k = 0$: `real = np.zeros(size, dtype=np.uint16)` and `real[m] = 1` for every Morton slot present in `tiles["morton"]`, leaving **0** for every unpopulated slot outside the $\text{gw} \times \text{gh}$ grid (`visioncore/pyramid.py:40-46`).
   - At $k > 0$: `self.real.append(self.real[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))` (`visioncore/pyramid.py:51`).
   - At the root ($k = L$), `self.assertEqual(int(p.real[p.L][0]), g.N)` holds (`tests/test_pyramid.py:51`).

### 4.2 Residual Threshold `r_sel` and Zero-Subtree Skip Rule

- **Selection threshold `r_sel(params=None)`** (`visioncore/pyramid.py:27-30`):
  ```python
  def r_sel(params=None):  # @anchor:vc.s3.r_sel
      """R_SEL = T_exit >> 2 in resid units (resid = r >> 2)."""
      p = P.PARAMS if params is None else params
      return (p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000) >> 2
  ```
- **Strict inequality rule (`> rs`)**:
  - In `select(self, B, rsel=None)`, `rs = r_sel() if rsel is None else rsel` (`visioncore/pyramid.py:94-98`).
  - A root or seed node is pushed onto `heap` only if `self.mx[L][0] > rs` or `self.mx[self.id_cap][i] > rs` (`visioncore/pyramid.py:99-110`).
  - When expanding a popped node `(level, i)`, a child `c` in `self.children(level, i)` is pushed only if `self.mx[level - 1][c] > rs` (`visioncore/pyramid.py:117-119`).
  - Any subtree whose maximum residual satisfies $\text{mx} \le \text{rs}$ is skipped in $O(1)$ without descending into its children, satisfying the `"zero-subtree-skip"` invariant (`visioncore/pyramid.py:1`).
  - Boundary equality (`resid == R_SEL is not > R_SEL`) returns `[]` (`tests/test_pyramid.py:116-118`).
  - A single dirty leaf visits at most `p.L + 1` nodes (`self.assertLessEqual(p.last_visits, p.L + 1)` in `tests/test_pyramid.py:111-115`).

### 4.3 Bounded Covering Selection Algorithm (`select`, `visioncore/pyramid.py:94-121`)

1. **Budget validation**: If `not isinstance(B, int) or B < 1`, raises `VqaError("E_USAGE", {"B": B})` (`visioncore/pyramid.py:96-97`).
2. **Threshold and state initialization**: Sets `rs = r_sel() if rsel is None else rsel` and `L, out, heap, visits = self.L, [], [], 0` (`visioncore/pyramid.py:98-99`).
3. **Seed step** (`visioncore/pyramid.py:104-110`):
   - If `self.id_cap >= L`: if `self.mx[L][0] > rs`, calls `push(L, 0)` (`visioncore/pyramid.py:104-106`).
   - Otherwise (**self.id_cap < L**): iterates `for i in range(len(self.mx[self.id_cap])):` and calls `push(self.id_cap, i)` whenever `self.real[self.id_cap][i] and self.mx[self.id_cap][i] > rs` (`visioncore/pyramid.py:107-110`).
4. **Best-first refinement loop** (`visioncore/pyramid.py:111-120`):
   ```python
        while heap:
            _, _, _, level, i = heapq.heappop(heap)
            visits += 1
            if level == 0 or self.cnt[level][i] == self.real[level][i] or len(out) + len(heap) + 4 > B:
                out.append(self.node(level, i))
                continue
            for c in self.children(level, i):
                if self.mx[level - 1][c] > rs:
                    push(level - 1, c)
   ```
5. **Stopping conditions for a popped node** (`visioncore/pyramid.py:114-116`):
   - **Leaf reached**: `level == 0` (`visioncore/pyramid.py:114`).
   - **Homogeneous dirty subtree**: `self.cnt[level][i] == self.real[level][i]` (`visioncore/pyramid.py:114`).
   - **Homogeneous root test**: verified when all tiles are dirty by `self.assertEqual(out, [(6, 0, 50, 4096)])` (`tests/test_pyramid.py:120-126`).
   - **Budget guard**: `len(out) + len(heap) + 4 > B` (`visioncore/pyramid.py:114`), ensuring that replacing 1 node with up to 4 children can never cause `len(out)` to exceed `B`.
6. **Return**: Sets `self.last_visits = visits` and returns `out` (`visioncore/pyramid.py:120-121`).

## 5. Non-Power-of-Two Grids and Padding Handling

### 5.1 Grid Dimension Rules (`visioncore/layout.py:8-29`)

`grid_params(W, H)` (`visioncore/layout.py:15-25`) determines tile size `s` and grid dimensions `(s, gw, gh)`:

```python
    s = TILE_MIN_PX
    while s <= 1 << 16:
        gw, gh = -(-W // s), -(-H // s)
        if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
            return s, gw, gh
        s <<= 1
```

- `W` and `H` must be integers in `1 <= W <= DIM_MAX and 1 <= H <= DIM_MAX` where `DIM_MAX = 8192` (`visioncore/layout.py:12-18`).
- `s` starts at `TILE_MIN_PX` and doubles via `s <<= 1` until `gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX`, where `gw, gh = -(-W // s), -(-H // s)` (`visioncore/layout.py:19-24`).
- Consequently, `s` is always a power of two, whereas **gw** and **gh** can be arbitrary non-power-of-two integers in **1..255** whose product is at most **4096** (for example, `(80, 45)` or `(60, 34)` in `tests/test_pyramid.py:33`).

### 5.2 Embedding Non-Power-of-Two Grids in $2^L \times 2^L$ Morton Space

- The quadtree spans the full $2^L \times 2^L$ grid (`size = 4 ** L` leaves, `visioncore/pyramid.py:11-39`) where $L = \max(1, \lceil \log_2(\max(\text{gw}, \text{gh})) \rceil)$ (`levels_for(gw, gh)`).
- Only the $N = \text{gw} \times \text{gh}$ valid grid cells exist in `tiles["morton"]`; at those Morton indices `m`, `__init__` sets `leaf[m] = tiles["resid"]`, `cnt[m] = tiles["cls"] >= layout.CLS_DIRTY_MIN`, and `real[m] = 1` (`visioncore/pyramid.py:36-46`).
- All remaining $4^L - N$ leaf positions in $[0, 4^L)$ corresponding to virtual coordinates $(tx \ge \text{gw}) \lor (ty \ge \text{gh})$ remain zero in `leaf`, `cnt`, and `real` (`visioncore/pyramid.py:41-46`).
- Test verification: `self.assertEqual(int(p.real[0].sum()), g.N)` (`tests/test_pyramid.py:53-56`).
- Because **0** is the identity element for unsigned `.max(axis=1)` (**uint8**) and `.sum(axis=1, dtype=np.uint16)`, virtual outside-grid slots never increase `self.mx`, `self.cnt`, or `self.real` (`visioncore/pyramid.py:48-51`).
- Virtual subtrees with zero real tiles are excluded by:
  - `children(self, level, i)` via `if self.real[level - 1][c]` (`visioncore/pyramid.py:62-66`),
  - `query_aabb(self, x, y, w, h)` via `if self.real[m][i]:` (`visioncore/pyramid.py:72-88`), and
  - `select(self, B, rsel=None)` via `if self.real[self.id_cap][i]` at seed time and `for c in self.children(level, i):` during expansion (`visioncore/pyramid.py:94-119`).

### 5.3 Frame Border Pixel Padding

- `visioncore/layout.py:66` defines `"PAD"` as bit 7 (**1 << 7**) of `TILE_FLAGS`.
- `visioncore/pyramid.py:36-53` does not read tile flags or distinguish edge-padded real tiles from interior real tiles; every tile record passed in `tiles` is assigned `real[m] = 1`.
- The pixel-level frame padding algorithm in `from visioncore.grid import Grid` (`tests/test_pyramid.py:8`) is **ABSENT** from the bound files.

## 6. Morton Addressing per Level

### 6.1 2-D Tile Coordinate to 16-Bit Morton Code (`visioncore/layout.py:32-60`)

`visioncore/layout.py:33-59` implements 8-bit-per-axis Morton (Z-order) bit interleaving and compaction:

```python
def _spread(v):
    v &= 0xFF
    v = (v | (v << 4)) & 0x0F0F
    v = (v | (v << 2)) & 0x3333
    v = (v | (v << 1)) & 0x5555
    return v
```

- `morton(tx, ty)` (`visioncore/layout.py:50-53`): checks `if not (0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX):`, then returns `_spread(tx) | (_spread(ty) << 1)`, placing the 8 bits of `tx` at even bit positions ($2b$) and the 8 bits of `ty` at odd bit positions ($2b + 1$) of a 16-bit unsigned integer $m \in [0, \text{0xFFFF}]$.
- `unmorton(m)` (`visioncore/layout.py:41-59`): checks `if not 0 <= m <= 0xFFFF:`, then returns `_compact(m), _compact(m >> 1)` using `_compact(v)`.

### 6.2 Multi-Level Node Indexing, Spans, and `query_aabb` Addressing

1. **Level $k$ node span** (`visioncore/pyramid.py:34-57`):
   - At level $k \in \{0, \dots, L\}$, node index $i \in \{0, \dots, 4^{L - k} - 1\}$ covers the aligned Morton leaf range $[i \cdot 4^k, (i + 1) \cdot 4^k)$ (`visioncore/pyramid.py:34`) where:
     - lower bound $m_0 = i \ll (2k) = i \cdot 4^k$ (`i << (2 * level)` in `visioncore/pyramid.py:57`), and
     - upper bound $m_1 = (i + 1) \ll (2k) = (i + 1) \cdot 4^k$ (`lo, hi = i << (2 * level), (i + 1) << (2 * level)` in `tests/test_pyramid.py:26-28`).
   - Conversely, a node's aligned base Morton code $m_0$ maps back to its level-$k$ index via $i = m_0 \gg (2k)$, as tested by `0x68 >> 4` for level `2` in `tests/test_pyramid.py:143`.
2. **Parent–child index relation** (`visioncore/pyramid.py:62-66`):
   - For $k \ge 1$, node $(k, i)$ has four children at level $k - 1$ with indices $c \in \{4i, 4i + 1, 4i + 2, 4i + 3\}$ (`range(4 * i, 4 * i + 4)`, `visioncore/pyramid.py:66`).
3. **O(1) AABB query addressing (`query_aabb`, `visioncore/pyramid.py:72-89`)**:
   ```python
        tx0, ty0, tx1, ty1 = x0 // g.s, y0 // g.s, x1 // g.s, y1 // g.s
        m = (max(tx1 - tx0 + 1, ty1 - ty0 + 1) - 1).bit_length()
        out = []
        for by in range(ty0 >> m, (ty1 >> m) + 1):
            for bx in range(tx0 >> m, (tx1 >> m) + 1):
                i = layout.morton(bx, by)
                if self.real[m][i]:
                    out.append(self.node(m, i))
        return sorted(out, key=lambda n: n[1])
   ```
   - Clamps pixel rectangle **[x, x + w) x [y, y + h)** to **[0, g.W - 1] x [0, g.H - 1]** via `x1, y1 = min(x + w, g.W) - 1, min(y + h, g.H) - 1` and `x0, y0 = max(x, 0), max(y, 0)` (`visioncore/pyramid.py:77-80`).
   - Converts inclusive pixel bounds to inclusive tile bounds `tx0, ty0, tx1, ty1 = x0 // g.s, y0 // g.s, x1 // g.s, y1 // g.s` (`visioncore/pyramid.py:81`).
   - Chooses level `m = (max(tx1 - tx0 + 1, ty1 - ty0 + 1) - 1).bit_length()` (`visioncore/pyramid.py:82`), the smallest integer $m \ge 0$ such that $2^m \ge \max(tx_1 - tx_0 + 1, ty_1 - ty_0 + 1)$.
   - Because the tile span on each axis has length at most $2^m$, shifting endpoints by `>> m` yields $0 \le (tx_1 \gg m) - (tx_0 \gg m) \le 1$ and $0 \le (ty_1 \gg m) - (ty_0 \gg m) \le 1$, so `for by in range(ty0 >> m, (ty1 >> m) + 1):` and `for bx in range(tx0 >> m, (tx1 >> m) + 1):` visit at most $2 \times 2 = 4$ nodes at level `m` (`visioncore/pyramid.py:73-88`).
   - Upper bound of 4 returned nodes verified by `self.assertLessEqual(len(nodes), 4)` (`tests/test_pyramid.py:76`).
   - At level `m`, block coordinates `(bx, by)` map directly to level-`m` node index `i = layout.morton(bx, by)` (`visioncore/pyramid.py:86`) because interleaving $tx \gg m$ and $ty \gg m$ equals $\text{morton}(tx, ty) \gg (2m)$.
4. **Node ID formatting and `id_cap` fallback** (`visioncore/pyramid.py:16-60`):
   - `node_id(self, frame, level, i)` delegates to `ids.node_id(frame, level, i << (2 * level))` (`visioncore/pyramid.py:59-60`).
   - The internal regex and range checks of `ids.node_id` are **ABSENT** from the bound files, except as probed by `id_level_cap(L)` (`visioncore/pyramid.py:16-24`).
   - Tested node ID format: `"p42:L2:%04x" % ((0x68 >> 4) << 4)` (`tests/test_pyramid.py:143`).
   - If `self.id_cap >= L` is false (**self.id_cap < L**), `select` starts from the forest of all nodes at level `self.id_cap` where `self.real[self.id_cap][i] and self.mx[self.id_cap][i] > rs` instead of the root `(L, 0)` (`visioncore/pyramid.py:94-110`).

## 7. Integer Arithmetic and Rounding

Stage S3 enforces the `"integer-only"` invariant (`visioncore/pyramid.py:1`); no floating-point literals, types, or `/` division operators appear anywhere in the bound S3 implementation or layout module:

| Operation | Formula in Code | Rounding / Arithmetic Rule | Citation |
|---|---|---|---|
| Grid tile count ceiling | `gw, gh = -(-W // s), -(-H // s)` | Exact ceiling division $\lceil W/s \rceil, \lceil H/s \rceil$ via negative floor division | `visioncore/layout.py:21` |
| Pyramid depth $L$ | `max(1, (max(gw, gh) - 1).bit_length())` | Exact integer $\max(1, \lceil \log_2(\max(\text{gw}, \text{gh})) \rceil)$ via `.bit_length()` | `visioncore/pyramid.py:13` |
| AABB block level $m$ | `(max(tx1 - tx0 + 1, ty1 - ty0 + 1) - 1).bit_length()` | Exact integer $\lceil \log_2(\max(\Delta tx, \Delta ty)) \rceil$ via `.bit_length()` (**0** for $1 \times 1$) | `visioncore/pyramid.py:82` |
| Hysteresis exit threshold in `r_sel` | `p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000` | Integer floor division `// 1000` (rounds toward $-\infty$, truncating fractional **q4** part for non-negative inputs) | `visioncore/pyramid.py:27-30` |
| **q4** to `resid` scale in `r_sel` | `(p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000) >> 2` | Arithmetic right shift `>> 2` (floor division by 4, matching `resid = r >> 2`) | `visioncore/pyramid.py:27-30` |
| Pixel to tile coordinate | `tx0, ty0, tx1, ty1 = x0 // g.s, y0 // g.s, x1 // g.s, y1 // g.s` | Integer floor division `// g.s` | `visioncore/pyramid.py:81` |
| Tile to level-$m$ block coordinate | `ty0 >> m, (ty1 >> m) + 1` and `tx0 >> m, (tx1 >> m) + 1` | Bitwise right shift `>> m` (floor division by $2^m$) | `visioncore/pyramid.py:84-85` |
| Level-$k$ Morton base $m_0$ | `i << (2 * level)` | Bitwise left shift by $2 \cdot \text{level}$ (exact multiplication by $4^{\text{level}}$) | `visioncore/pyramid.py:57` |
| Leaf-to-root `cnt` and `real` sums | `.reshape(-1, 4).sum(axis=1, dtype=np.uint16)` | Exact unsigned 16-bit integer addition ($\max \le 4096 < 65535$, no overflow) | `visioncore/pyramid.py:50-51` |
| Leaf-to-root `mx` reduction | `.reshape(-1, 4).max(axis=1)` | Exact unsigned 8-bit maximum (**uint8**, range **0..255**) | `visioncore/pyramid.py:49` |

## 8. Parameters Read from the Params Table

### 8.1 Parameters Read in `visioncore/pyramid.py:27-30`

`r_sel(params=None)` (`visioncore/pyramid.py:27-30`) resolves `p = P.PARAMS if params is None else params` and reads two keys from `p`:

| Parameter Key | Read Location | Role in Stage S3 | Default Value in Bound Files |
|---|---|---|---|
| `"T0_Q4"` | `visioncore/pyramid.py:30` | Base change threshold multiplying `(1000 - p["HYST_BAND_PERMILLE"])` | **ABSENT** from bound files (defined in **visioncore/params.py** imported at line 7) |
| `"HYST_BAND_PERMILLE"` | `visioncore/pyramid.py:30` | Hysteresis band width subtracted in `(1000 - p["HYST_BAND_PERMILLE"])` | **ABSENT** from bound files (defined in **visioncore/params.py** imported at line 7) |

### 8.2 Parameters Read in `visioncore/layout.py:6-24`

`visioncore/layout.py:6-11` imports `PARAMS` from `.params` and binds three module-level constants at import time:

| Parameter Key | Module Constant (`visioncore/layout.py:9-11`) | Usage in `visioncore/layout.py:15-53` | Documented Value in `visioncore/layout.py:16` |
|---|---|---|---|
| `"TILE_MIN_PX"` | `TILE_MIN_PX = PARAMS["TILE_MIN_PX"]` | Initial tile size `s = TILE_MIN_PX` in `grid_params(W, H)` | `16` (`>= 16` in docstring; table row in **visioncore/params.py** is **ABSENT** from bound files) |
| `"CELL_MAX"` | `CELL_MAX = PARAMS["CELL_MAX"]` | Upper bound `gw * gh <= CELL_MAX` in `grid_params(W, H)` | `4096` (`<= 4096` in docstring; table row in **visioncore/params.py** is **ABSENT** from bound files) |
| `"AXIS_MAX"` | `AXIS_MAX = PARAMS["AXIS_MAX"]` | Upper bound `max(gw, gh) <= AXIS_MAX` and `0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX` | `255` (`<= 255` in docstring; table row in **visioncore/params.py** is **ABSENT** from bound files) |

Note: `DIM_MAX = 8192`, `CLS_DIRTY_MIN = 2`, `TILE_SIZE = 32`, `TILE_HDR_SIZE = 64`, and `GLOBAL_MOTION_SIZE = 8` are module constants in `visioncore/layout.py:6-109`, not keys read from `PARAMS`.

## 9. Output Byte Layout and Ordering

### 9.1 S3 In-Memory Output Tuples and Ordering (`visioncore/pyramid.py:55-121`)

- **Binary wire/disk format for S3 pyramid nodes**: **ABSENT** (Stage S3 does not serialize pyramid levels to a binary file; it exposes in-memory Python tuples via `node`, `query_aabb`, and `select`, and string node IDs via `node_id` in `visioncore/pyramid.py:55-121`).
- **Node tuple layout** (`visioncore/pyramid.py:55-57`): `node(self, level, i)` returns the 4-tuple `(level, m0, max, cnt)` computed as `(level, i << (2 * level), int(self.mx[level][i]), int(self.cnt[level][i]))`:
  - **level**: Python **int** in **0..L**,
  - **m0**: Python **int** (`i << (2 * level)`), the aligned lower Morton leaf bound of the node,
  - **max**: Python **int** (`int(self.mx[level][i])` in **0..255**), the maximum leaf residual under the node,
  - **cnt**: Python **int** (`int(self.cnt[level][i])` in **0..4096**), the count of dirty leaves (**cls >= 2**) under the node.
- **Ordering of `children(self, level, i)`** (`visioncore/pyramid.py:62-66`):
  - Iterates `for c in range(4 * i, 4 * i + 4)` and filters by `if self.real[level - 1][c]`, returning valid child indices in strictly ascending index (and therefore ascending Morton **m0**) order.
- **Ordering of `query_aabb(self, x, y, w, h)`** (`visioncore/pyramid.py:72-89`):
  - Returns `sorted(out, key=lambda n: n[1])` (`visioncore/pyramid.py:89`), sorting the **<= 4** returned node tuples in strictly ascending order of **m0** (`n[1]`).
- **Ordering of `select(self, B, rsel=None)`** (`visioncore/pyramid.py:94-121`):
  - Each candidate node `(level, i)` is pushed onto the binary min-heap `heap` as the 5-tuple (`visioncore/pyramid.py:101-102`):
    ```python
        def push(level, i):
            heapq.heappush(heap, (-int(self.mx[level][i]), -level, i << (2 * level), level, i))
    ```
  - When **self.id_cap < L**, initial seed nodes at level `self.id_cap` are pushed in ascending `i` order (`for i in range(len(self.mx[self.id_cap])):`, `visioncore/pyramid.py:108-110`).
  - `heapq.heappop(heap)` pops candidates in lexicographical order of `(-int(self.mx[level][i]), -level, i << (2 * level), level, i)` (`visioncore/pyramid.py:102-112`):
    1. **Highest max residual first** (`-int(self.mx[level][i])` ascending),
    2. **Coarsest level first on residual ties** (`-level` ascending, i.e. larger `level` popped first),
    3. **Lowest Morton base first on (max, level) ties** (`i << (2 * level)` ascending).
  - A popped node is appended to `out` immediately when it satisfies the stopping condition `if level == 0 or self.cnt[level][i] == self.real[level][i] or len(out) + len(heap) + 4 > B:`, and otherwise its non-empty children with `self.mx[level - 1][c] > rs` are pushed onto `heap` (`visioncore/pyramid.py:114-119`).
  - `out` is returned in exact heap-finalisation order without post-sorting (`visioncore/pyramid.py:115-121`).
  - Because every popped ancestor either finalises or splits into disjoint children, all spans `spans = [(m0, m0 + 4 ** lv) for lv, m0, _, _ in out]` are pairwise disjoint (`self.assertLessEqual(a1, b0)` in `tests/test_pyramid.py:97-105`).

### 9.2 Binary Record and Header Layouts in `visioncore/layout.py:91-139`

1. **32-Byte Tile Record (`vqa.tile/v1`, `visioncore/layout.py:91-99`)**:
   - Format string: `TILE_FMT = "<HBBHbbHHBBbbQQ"` (little-endian, `TILE_SIZE = 32`, `visioncore/layout.py:94-99`).
   - Field names `TILE_FIELDS` and byte offsets `TILE_OFFSETS = (0, 2, 3, 4, 6, 7, 8, 10, 12, 13, 14, 15, 16, 24)` (`visioncore/layout.py:95-97`):

| Index | Field Name (`visioncore/layout.py:95-96`) | Byte Offset | **struct** Token | Size (Bytes) | Used by **Pyramid.__init__** |
|---:|---|---:|---|---:|---|
| 0 | `"morton"` | `0` | **<H** (**uint16**) | 2 | Yes: `m = tiles["morton"].astype(np.int64)` |
| 1 | `"cls"` | `2` | **B** (**uint8**) | 1 | Yes: `cnt[m] = tiles["cls"] >= layout.CLS_DIRTY_MIN` |
| 2 | `"flags"` | `3` | **B** (**uint8**) | 1 | No |
| 3 | `"y_q4"` | `4` | **<H** (**uint16**) | 2 | No |
| 4 | `"co_h"` | `6` | **b** (**int8**) | 1 | No |
| 5 | `"cg_h"` | `7` | **b** (**int8**) | 1 | No |
| 6 | `"sig_q4"` | `8` | **<H** (**uint16**) | 2 | No |
| 7 | `"grad_q6"` | `10` | **<H** (**uint16**) | 2 | No |
| 8 | `"edge"` | `12` | **B** (**uint8**) | 1 | No |
| 9 | `"resid"` | `13` | **B** (**uint8**) | 1 | Yes: `leaf[m] = tiles["resid"]` |
| 10 | `"mv_dx"` | `14` | **b** (**int8**) | 1 | No |
| 11 | `"mv_dy"` | `15` | **b** (**int8**) | 1 | No |
| 12 | `"sig64"` | `16` | **<Q** (**uint64**) | 8 | No |
| 13 | `"xh64"` | `24` | **<Q** (**uint64**) | 8 | No |

2. **64-Byte Tile Header (`visioncore/layout.py:101-127`)**:
   - Constants: `TILE_MAGIC = b"VQATILE1"`, `TILE_HDR_SIZE = 64`, `TILE_HDR_FMT = "<8sHHHHHHHHII16s8s8s"` (`visioncore/layout.py:101-105`).
   - Packed by `pack_tile_header(s, W, H, gw, gh, N, n_frames, f0, params_sha256_hex, api_version)` (`visioncore/layout.py:113-116`) with version `1`, `TILE_SIZE` (**32**), `bytes.fromhex(params_sha256_hex)[:16]`, `api_version.encode("ascii")[:8].ljust(8, b"\0")`, and trailing `b"\0" * 8`.
   - Parsed by `parse_tile_header(b)` (`visioncore/layout.py:119-127`).
3. **8-Byte GlobalMotion Record (`visioncore/layout.py:107-110`)**:
   - Constants: `GLOBAL_MOTION_FMT = "<hhI"`, `GLOBAL_MOTION_SIZE = 8` (`visioncore/layout.py:108-110`). Dedicated pack/unpack helpers for GlobalMotion are **ABSENT** from `visioncore/layout.py:1-139`.

## 10. Error Codes and Exception Conditions

- All errors raised in `visioncore/pyramid.py:1-121` construct `VqaError`.
- All errors raised in `visioncore/layout.py:1-139` also construct `VqaError`.
- The class definition of `VqaError` and the full error-code-to-exit-code lookup table are **ABSENT** from the bound files, except that `tests/test_pyramid.py:137-139` verifies `self.assertEqual(c.exception.rc, 2)` when `p.select(B)` raises `VqaError`.

| Error Code | Raised By | Trigger Condition | Detail Payload | Citation |
|---|---|---|---|---|
| `"E_USAGE"` | `select(self, B, rsel=None)` | `if not isinstance(B, int) or B < 1:` (note: in Python **bool** subclasses **int**, so **True** is not rejected by this guard) | `{"B": B}` | `visioncore/pyramid.py:94-97` |
| `"E_GRID"` | `grid_params(W, H)` | `if not (isinstance(W, int) and isinstance(H, int)) or not (1 <= W <= DIM_MAX and 1 <= H <= DIM_MAX):` | `{"W": W, "H": H, "reason": "dimension outside 1..%d" % DIM_MAX}` | `visioncore/layout.py:15-18` |
| `"E_GRID"` | `grid_params(W, H)` | `while s <= 1 << 16:` loop finishes without satisfying `gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX` | `{"W": W, "H": H}` | `visioncore/layout.py:15-25` |
| `"E_VC_ID_RANGE"` | `morton(tx, ty)` | `if not (0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX):` | `{"tx": tx, "ty": ty}` | `visioncore/layout.py:50-52` |
| `"E_VC_ID_RANGE"` | `unmorton(m)` | `if not 0 <= m <= 0xFFFF:` | `{"m": m}` | `visioncore/layout.py:56-58` |
| `"E_SCHEMA"` | `cls_name(v)` | `if not isinstance(v, int) or not 0 <= v < len(CLS):` (also invoked by `is_dirty` and `check_tile_records`) | `{"cls": v}` | `visioncore/layout.py:74-139` |
| `"E_SCHEMA"` | `cls_id(name)` | `if name not in CLS_ID:` | `{"cls_name": name}` | `visioncore/layout.py:80-82` |
| `"E_VC_FORMAT"` | `parse_tile_header(b)` | `if len(b) < TILE_HDR_SIZE:` | `{"reason": "short header"}` | `visioncore/layout.py:119-121` |
| `"E_VC_FORMAT"` | `parse_tile_header(b)` | `if f[0] != TILE_MAGIC or f[1] != 1 or f[8] != TILE_SIZE:` | `{"reason": "bad magic/ver/rec"}` | `visioncore/layout.py:119-124` |
| `"E_VC_FORMAT"` | `check_tile_records(b)` | `if len(b) % TILE_SIZE:` | `{"reason": "length not multiple of 32"}` | `visioncore/layout.py:134-137` |
| `VqaError` (from `ids.node_id`) | `id_level_cap(L)` and `node_id(self, frame, level, i)` | Raised by `ids.node_id` when arguments are out of range; caught in `id_level_cap(L)` and propagated by `node_id` | Defined in **visioncore/ids.py** (**ABSENT** from bound files) | `visioncore/pyramid.py:16-60` |

## 11. Summary of Items Marked ABSENT in Bound Source Files

The following definitions and behaviours are referenced by or adjacent to Stage S3, but are **ABSENT** from the three bound source files:

1. **Parameter table definitions**:
   - Numeric defaults and bounds for `"T0_Q4"` and `"HYST_BAND_PERMILLE"` in `visioncore/pyramid.py:7-30` are **ABSENT** from the bound files.
   - Registry table definitions of `"TILE_MIN_PX"`, `"CELL_MAX"`, and `"AXIS_MAX"` in `visioncore/layout.py:6-11` are **ABSENT** from the bound files.
2. **ID registry implementation**: The internal implementation, level range limit, and error code of `ids.node_id` called in `visioncore/pyramid.py:7-60` are **ABSENT** from the bound files.
3. **Error registry implementation**:
   - The class definition of `VqaError` imported in `visioncore/pyramid.py:8` is **ABSENT** from the bound files.
   - The error code table imported via `from .errors import VqaError` in `visioncore/layout.py:5` is **ABSENT** from the bound files.
4. **Stage S0 `Grid` implementation**: The class definition of `Grid` (`g.N`, `g.mortons`, `g.order`) and pixel border padding are **ABSENT** from the bound files (`tests/test_pyramid.py:8-23`).
5. **Stage S2 residual and class computation**: How raw residual `r` is computed and shifted (`resid = r >> 2` in `visioncore/pyramid.py:28-44`) and how `"cls"` is assigned are **ABSENT** from the bound files.
6. **Serialized binary byte layout for `Pyramid`**: A binary wire or file format for pyramid nodes is **ABSENT** from `visioncore/pyramid.py:1-121`.
7. **Bounds checking on `node`, `children`, and `bound`**: Explicit validation of `level` and `i` in `node`, `children`, and `bound` (`visioncore/pyramid.py:55-70`) is **ABSENT** from the bound files.
