# Stage S0 Grid, Padding, Morton Order, and Binary Tile Record Layout Specification

## 1. Scope and Bound Modules

Stage S0 and the pure-int binary tile record layout are defined by four bound files on branch `bridge/integration`:

- `visioncore/params.py:11` (`_ROWS`) and `visioncore/params.py:49` (`PARAMS`): integer-only parameter registry defining `TILE_MIN_PX` (`visioncore/params.py:12`), `CELL_MAX` (`visioncore/params.py:13`), and `AXIS_MAX` (`visioncore/params.py:14`), plus bounds validation in `_check` (`visioncore/params.py:56`), profile resolution in `effective` (`visioncore/params.py:66`), and canonical hashing in `params_sha256` (`visioncore/params.py:84`).
- `visioncore/layout.py:15` (`grid_params`), `visioncore/layout.py:28` (`grid_ident`), `visioncore/layout.py:33` (`_spread`), `visioncore/layout.py:41` (`_compact`), `visioncore/layout.py:50` (`morton`), `visioncore/layout.py:56` (`unmorton`), `visioncore/layout.py:63` (`CLS`), `visioncore/layout.py:64` (`CLS_ID`), `visioncore/layout.py:65` (`CLS_DIRTY_MIN`), `visioncore/layout.py:74` (`cls_name`), `visioncore/layout.py:80` (`cls_id`), `visioncore/layout.py:86` (`is_dirty`), `visioncore/layout.py:94` (`TILE_FMT`), `visioncore/layout.py:104` (`TILE_HDR_FMT`), `visioncore/layout.py:108` (`GLOBAL_MOTION_FMT`), `visioncore/layout.py:113` (`pack_tile_header`), `visioncore/layout.py:119` (`parse_tile_header`), `visioncore/layout.py:130` (`unpack_tile`), and `visioncore/layout.py:134` (`check_tile_records`).
- `visioncore/grid.py:8` (`Grid`), `visioncore/grid.py:11` (`__init__`), `visioncore/grid.py:30` (`pad`), `visioncore/grid.py:37` (`pad_flags`), `visioncore/grid.py:42` (`field_ids_of_rect`), and `visioncore/grid.py:46` (`aligned_block`).
- `tests/test_grid.py:10` (`TABLE`) and `tests/test_grid.py:15` (`TestGrid`): unit tests covering the resolution table (`tests/test_grid.py:16`), invalid dimensions (`tests/test_grid.py:23`), Morton order and roundtrip (`tests/test_grid.py:29`), quadtree block contiguity (`tests/test_grid.py:39`), edge-replicated padding (`tests/test_grid.py:47`), rectangle tile ID queries (`tests/test_grid.py:60`), and aligned blocks (`tests/test_grid.py:69`).

## 2. Parameter Registry Constants and Limits (`visioncore/params.py` and `visioncore/layout.py`)

Each entry in `_ROWS` (`visioncore/params.py:11`) is a 5-tuple `(default, unit, owner, profile-overridable, (lo, hi))` (`visioncore/params.py:10`) whose key matches `THRESHOLD_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")` (`visioncore/params.py:8`). `visioncore/layout.py:6` imports `PARAMS` (`visioncore/params.py:49`) and binds the S0 grid constants alongside `DIM_MAX` (`visioncore/layout.py:12`):

| Symbol | Value | Unit (`UNITS`) | Owner (`OWNERS`) | Profile-Overridable (`PROFILE_KEYS`) | Bounds (`BOUNDS`) | Citations |
|---|---:|---|---|---|---|---|
| `TILE_MIN_PX` | `16` | `"px"` | `"CORE"` | `False` | `(16, 16)` | `visioncore/params.py:12`, `visioncore/layout.py:9` |
| `CELL_MAX` | `4096` | `"cells"` | `"CORE"` | `False` | `(4096, 4096)` | `visioncore/params.py:13`, `visioncore/layout.py:10` |
| `AXIS_MAX` | `255` | `"cells"` | `"CORE"` | `False` | `(255, 255)` | `visioncore/params.py:14`, `visioncore/layout.py:11` |
| `DIM_MAX` | `8192` | `ABSENT` | `ABSENT` | `False` (module constant) | `1..8192` | `visioncore/layout.py:12`, `visioncore/layout.py:17` |

Registry tables and profile validation rules:
- `PARAMS` (`visioncore/params.py:49`), `BOUNDS` (`visioncore/params.py:50`), `UNITS` (`visioncore/params.py:51`), `OWNERS` (`visioncore/params.py:52`), and `PROFILE_KEYS` (`visioncore/params.py:53`) are comprehension views over `_ROWS` (`visioncore/params.py:11`).
- Because `TILE_MIN_PX` (`visioncore/params.py:12`), `CELL_MAX` (`visioncore/params.py:13`), and `AXIS_MAX` (`visioncore/params.py:14`) have `v[3] == False`, none is included in `PROFILE_KEYS` (`visioncore/params.py:53`). Attempting to override any of them via `effective(profile)` (`visioncore/params.py:66`) raises `VqaError("E_SCHEMA", {"param": key, "reason": "not profile-overridable"})` (`visioncore/params.py:78`).
- `DIM_MAX = 8192` is defined in `visioncore/layout.py:12`; an entry for `DIM_MAX` in `_ROWS` (`visioncore/params.py:11`) is `ABSENT`.

## 3. Grid Rule: `grid_params` and `grid_ident` (`visioncore/layout.py`)

### 3.1 `grid_params(W, H)` (`visioncore/layout.py:15`)

`grid_params(W, H)` (`visioncore/layout.py:15`) maps frame pixel dimensions `(W, H)` to `(s, gw, gh)`, selecting the smallest power-of-two tile side `s >= 16` (`TILE_MIN_PX`, `visioncore/layout.py:9`) such that the padded cell counts `gw` and `gh` (`visioncore/layout.py:21`) satisfy both `gw * gh <= 4096` (`CELL_MAX`, `visioncore/layout.py:10`) and `max(gw, gh) <= 255` (`AXIS_MAX`, `visioncore/layout.py:11`):

```python
def grid_params(W, H):
    if not (isinstance(W, int) and isinstance(H, int)) or not (1 <= W <= DIM_MAX and 1 <= H <= DIM_MAX):
        raise VqaError("E_GRID", {"W": W, "H": H, "reason": "dimension outside 1..%d" % DIM_MAX})
    s = TILE_MIN_PX
    while s <= 1 << 16:
        gw, gh = -(-W // s), -(-H // s)
        if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
            return s, gw, gh
        s <<= 1
    raise VqaError("E_GRID", {"W": W, "H": H})
```

Step-by-step specification:
1. **Input validation**: Checks `isinstance(W, int) and isinstance(H, int)` and `1 <= W <= DIM_MAX and 1 <= H <= DIM_MAX` (`visioncore/layout.py:17`). If violated, raises `VqaError("E_GRID", {"W": W, "H": H, "reason": "dimension outside 1..%d" % DIM_MAX})` (`visioncore/layout.py:18`, tested at `tests/test_grid.py:25`). Because `bool` is a subclass of `int` in Python, explicit `isinstance(..., bool)` rejection in `grid_params` (`visioncore/layout.py:17`) is `ABSENT` (whereas `_check` in `visioncore/params.py:58` explicitly rejects `isinstance(v, bool)`).
2. **Candidate iteration**: Starts at `s = TILE_MIN_PX` (`16`, `visioncore/layout.py:19`) and loops while `s <= 1 << 16` (`65536`, `visioncore/layout.py:20`), doubling `s <<= 1` (`visioncore/layout.py:24`) whenever a candidate fails the cap check.
3. **Ceiling cell counts**: Computes `gw, gh = -(-W // s), -(-H // s)` (`visioncore/layout.py:21`), which evaluates `ceil(W / s)` and `ceil(H / s)` using pure-integer arithmetic.
4. **Acceptance predicate (`CELL_MAX` and `AXIS_MAX`)**: Returns `(s, gw, gh)` (`visioncore/layout.py:23`) for the first `s` where `gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX` (`visioncore/layout.py:22`).
5. **Fallback error**: If the loop exits without returning, raises `VqaError("E_GRID", {"W": W, "H": H})` (`visioncore/layout.py:25`). For all valid `1 <= W, H <= 8192` (`DIM_MAX`, `visioncore/layout.py:12`), `s = 128` yields `1 <= gw, gh <= 64 <= 255` and `gw * gh <= 4096`, so `s` always terminates in `{16, 32, 64, 128}` (`tests/test_grid.py:10`).

### 3.2 Canonical Frame-to-Grid Mapping Table (`tests/test_grid.py:10`)

The reference dimensions verified in `TABLE` (`tests/test_grid.py:10`) and `test_pad_edge_replication` (`tests/test_grid.py:49`) illustrate how `CELL_MAX` (`visioncore/layout.py:10`) and `AXIS_MAX` (`visioncore/layout.py:11`) govern `s`:

| Frame `W x H` | `s` (px) | `gw` | `gh` | `N = gw * gh` | Padded `(gw*s) x (gh*s)` | Padding `(pw, ph)` | `grid_ident(W, H)` | Binding Constraint | Citations |
|---|---:|---:|---:|---:|---|---|---|---|---|
| `1280 x 720` | `16` | `80` | `45` | `3600` | `1280 x 720` | `(0, 0)` | `"grid:1280x720:s16"` | Minimum `s = TILE_MIN_PX = 16` | `tests/test_grid.py:10` |
| `1920 x 1080` | `32` | `60` | `34` | `2040` | `1920 x 1088` | `(0, 8)` | `"grid:1920x1080:s32"` | At `s=16`: `gw*gh = 120*68 = 8160 > CELL_MAX (4096)` | `tests/test_grid.py:10` |
| `3840 x 2160` | `64` | `60` | `34` | `2040` | `3840 x 2176` | `(0, 16)` | `"grid:3840x2160:s64"` | At `s=32`: `gw*gh = 120*68 = 8160 > CELL_MAX (4096)` | `tests/test_grid.py:10` |
| `1024 x 1024` | `16` | `64` | `64` | `4096` | `1024 x 1024` | `(0, 0)` | `"grid:1024x1024:s16"` | Minimum `s = 16` (`N = 4096 == CELL_MAX`) | `tests/test_grid.py:11` |
| `2560 x 1440` | `32` | `80` | `45` | `3600` | `2560 x 1440` | `(0, 0)` | `"grid:2560x1440:s32"` | At `s=16`: `gw*gh = 160*90 = 14400 > CELL_MAX (4096)` | `tests/test_grid.py:11` |
| `8192 x 8192` | `128` | `64` | `64` | `4096` | `8192 x 8192` | `(0, 0)` | `"grid:8192x8192:s128"` | At `s=64`: `gw*gh = 128*128 = 16384 > CELL_MAX (4096)` | `tests/test_grid.py:11` |
| `8192 x 16` | `64` | `128` | `1` | `128` | `8192 x 64` | `(0, 48)` | `"grid:8192x16:s64"` | At `s=32`: `gw = 256 > AXIS_MAX (255)` (`gw*gh = 256 <= 4096`) | `tests/test_grid.py:12` |
| `100 x 70` | `16` | `7` | `5` | `35` | `112 x 80` | `(12, 10)` | `"grid:100x70:s16"` | Minimum `s = 16` | `tests/test_grid.py:49` |

### 3.3 `grid_ident(W, H)` (`visioncore/layout.py:28`)

```python
def grid_ident(W, H):
    return "grid:%dx%d:s%d" % ((W, H, grid_params(W, H)[0]))
```

- Returns `"grid:<W>x<H>:s<s>"` where `s = grid_params(W, H)[0]` (`visioncore/layout.py:29`, verified at `tests/test_grid.py:20`).
- Propagates `VqaError("E_GRID", ...)` from `grid_params(W, H)` (`visioncore/layout.py:18`) if `W` or `H` is invalid.

## 4. Morton and Unmorton Bit Layouts (`visioncore/layout.py:33`)

### 4.1 8-Bit Coordinate Dilate and Compact (`_spread` and `_compact`)

Tile coordinates `tx` and `ty` are 8-bit unsigned integers in `0..255` (`AXIS_MAX`, `visioncore/layout.py:11`). `_spread(v)` (`visioncore/layout.py:33`) spreads the 8 bits of `v` into the even bits of a 16-bit integer, and `_compact(v)` (`visioncore/layout.py:41`) compacts the even bits of a 16-bit integer back into an 8-bit integer:

```python
def _spread(v):
    v &= 0xFF
    v = (v | (v << 4)) & 0x0F0F
    v = (v | (v << 2)) & 0x3333
    v = (v | (v << 1)) & 0x5555
    return v
```

```python
def _compact(v):
    v &= 0x5555
    v = (v | (v >> 1)) & 0x3333
    v = (v | (v >> 2)) & 0x0F0F
    v = (v | (v >> 4)) & 0x00FF
    return v
```

Mask progression:
- In `_spread(v)` (`visioncore/layout.py:33`): `v &= 0xFF` (`visioncore/layout.py:34`) isolates bits `7..0`; `(v | (v << 4)) & 0x0F0F` (`visioncore/layout.py:35`) places nibbles in bits `11..8` and `3..0`; `(v | (v << 2)) & 0x3333` (`visioncore/layout.py:36`) separates bit pairs; `(v | (v << 1)) & 0x5555` (`visioncore/layout.py:37`) places input bit `b` at bit position `2 * b`, returned at `visioncore/layout.py:38`.
- In `_compact(v)` (`visioncore/layout.py:41`): `v &= 0x5555` (`visioncore/layout.py:42`) clears odd bits; `(v | (v >> 1)) & 0x3333` (`visioncore/layout.py:43`), `(v | (v >> 2)) & 0x0F0F` (`visioncore/layout.py:44`), and `(v | (v >> 4)) & 0x00FF` (`visioncore/layout.py:45`) pack even bits `2 * b` back into bit `b` (`0..7`), returned at `visioncore/layout.py:46`.

### 4.2 `morton(tx, ty)` (`visioncore/layout.py:50`) and `unmorton(m)` (`visioncore/layout.py:56`)

```python
def morton(tx, ty):
    if not (0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX):
        raise VqaError("E_VC_ID_RANGE", {"tx": tx, "ty": ty})
    return _spread(tx) | (_spread(ty) << 1)

def unmorton(m):
    if not 0 <= m <= 0xFFFF:
        raise VqaError("E_VC_ID_RANGE", {"m": m})
    return _compact(m), _compact(m >> 1)
```

Bit-level layout of the 16-bit Morton index `m = morton(tx, ty)` (`visioncore/layout.py:53`, anchor `# @anchor:vc.layout.morton` at `visioncore/layout.py:49`):

| Bit Position of `m` | `15` | `14` | `13` | `12` | `11` | `10` | `9` | `8` | `7` | `6` | `5` | `4` | `3` | `2` | `1` | `0` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Coordinate Bit | `ty[7]` | `tx[7]` | `ty[6]` | `tx[6]` | `ty[5]` | `tx[5]` | `ty[4]` | `tx[4]` | `ty[3]` | `tx[3]` | `ty[2]` | `tx[2]` | `ty[1]` | `tx[1]` | `ty[0]` | `tx[0]` |

- **Even bits (`0, 2, 4, 6, 8, 10, 12, 14`, mask `0x5555`)**: Hold `tx` bits `0..7` (`_spread(tx)` at `visioncore/layout.py:53`), unpacked as `_compact(m)` (`visioncore/layout.py:59`).
- **Odd bits (`1, 3, 5, 7, 9, 11, 13, 15`, mask `0xAAAA`)**: Hold `ty` bits `0..7` (`_spread(ty) << 1` at `visioncore/layout.py:53`), unpacked as `_compact(m >> 1)` (`visioncore/layout.py:59`).
- **Bounds and error codes**:
  - `morton(tx, ty)` checks `0 <= tx <= AXIS_MAX and 0 <= ty <= AXIS_MAX` (`visioncore/layout.py:51`) and raises `VqaError("E_VC_ID_RANGE", {"tx": tx, "ty": ty})` (`visioncore/layout.py:52`) when `tx` or `ty` is outside `0..255`.
  - `unmorton(m)` checks `0 <= m <= 0xFFFF` (`visioncore/layout.py:57`) and raises `VqaError("E_VC_ID_RANGE", {"m": m})` (`visioncore/layout.py:58`) when `m` is outside `0..65535`.
  - Explicit `isinstance(..., int)` or `bool` type guards inside `morton` (`visioncore/layout.py:50`) and `unmorton` (`visioncore/layout.py:56`) are `ABSENT`.
- **Aligned block contiguity**: Every `2^k x 2^k` tile block aligned to a `2^k` grid boundary (`tx0 % (1 << k) == 0`, `ty0 % (1 << k) == 0`) maps to the contiguous Morton code range `range(m0, m0 + 4 ** k)` where `m0 = morton(tx0, ty0)` (`tests/test_grid.py:44`, `tests/test_grid.py:45`).

## 5. The `Grid` Class, Padding Rule, and Border Flags (`visioncore/grid.py:8`)

### 5.1 `Grid.__init__(self, W, H)` (`visioncore/grid.py:11`)

`Grid` (`visioncore/grid.py:8`, `# @anchor:vc.s0.grid`) computes grid parameters and pre-builds lookup tables ordered by ascending Morton code over the `N = gw * gh` existing cells:

```python
    def __init__(self, W, H):
        s, gw, gh = layout.grid_params(W, H)
        self.W, self.H, self.s, self.gw, self.gh = W, H, s, gw, gh
        self.N = gw * gh
        self.ident = layout.grid_ident(W, H)
        mort = np.empty((gh, gw), dtype=np.uint16)
        for ty in range(gh):
            for tx in range(gw):
                mort[ty, tx] = layout.morton(tx, ty)
        self.morton = mort
```

```python
        perm = np.argsort(mort.reshape(-1), kind="stable")
        self.flat = perm  # Morton rank -> row-major cell index
        self.mortons = mort.reshape(-1)[perm]
        self.tx = (perm % gw).astype(np.int32)
        self.ty = (perm // gw).astype(np.int32)
        order = np.empty(self.N, dtype=np.int16)
        order[perm] = np.arange(self.N, dtype=np.int16)
        self.order = order.reshape(gh, gw)
```

Instance attributes of `Grid` (`visioncore/grid.py:8`):

| Attribute | Type / Shape / Dtype | Definition and Invariant | Citations |
|---|---|---|---|
| `W`, `H` | `int` | Unpadded frame width and height in pixels (`1 <= W, H <= 8192`). | `visioncore/grid.py:13` |
| `s` | `int` | Tile side in pixels (`16, 32, 64, 128`) from `layout.grid_params(W, H)`. | `visioncore/grid.py:12`, `visioncore/grid.py:13` |
| `gw`, `gh` | `int` | Grid width and height in tiles (`1 <= gw, gh <= 255`). | `visioncore/grid.py:12`, `visioncore/grid.py:13` |
| `N` | `int` | Total existing cells `gw * gh` (`1 <= N <= 4096`). | `visioncore/grid.py:14`, `tests/test_grid.py:21` |
| `ident` | `str` | Grid identifier `"grid:%dx%d:s%d" % (W, H, s)` from `layout.grid_ident(W, H)`. | `visioncore/grid.py:15`, `tests/test_grid.py:20` |
| `morton` | `np.ndarray`, `(gh, gw)`, `uint16` | 2D table with `morton[ty, tx] = layout.morton(tx, ty)`. | `visioncore/grid.py:16`, `visioncore/grid.py:19`, `visioncore/grid.py:20` |
| `flat` | `np.ndarray`, `(N,)`, `intp` | Permutation `np.argsort(mort.reshape(-1), kind="stable")` mapping Morton rank `r in 0..N-1` to row-major index `ty * gw + tx`. | `visioncore/grid.py:21`, `visioncore/grid.py:22` |
| `mortons` | `np.ndarray`, `(N,)`, `uint16` | Strictly ascending Morton codes of existing cells (`mort.reshape(-1)[perm]`). Sparse whenever `gw` or `gh` is not an equal power of two. | `visioncore/grid.py:23`, `tests/test_grid.py:32` |
| `tx` | `np.ndarray`, `(N,)`, `int32` | Tile X coordinate `(perm % gw).astype(np.int32)` at Morton rank `r`. | `visioncore/grid.py:24`, `tests/test_grid.py:35` |
| `ty` | `np.ndarray`, `(N,)`, `int32` | Tile Y coordinate `(perm // gw).astype(np.int32)` at Morton rank `r`. | `visioncore/grid.py:25`, `tests/test_grid.py:35` |
| `order` | `np.ndarray`, `(gh, gw)`, `int16` | 2D inverse permutation mapping `(ty, tx)` to Morton rank `r in 0..N-1` (`order.reshape(-1)[flat] == np.arange(N)` and `order[ty, tx] == np.arange(N)`). | `visioncore/grid.py:26`, `visioncore/grid.py:27`, `visioncore/grid.py:28`, `tests/test_grid.py:36`, `tests/test_grid.py:37` |

### 5.2 Padding Rule (`Grid.pad`) and Border Flags (`Grid.pad_flags`)

```python
    def pad(self, rgb):
        """Complete edge tiles by edge replication (SPEC_CORE §1 Padding). -> uint8[gh*s, gw*s, 3]."""
        ph, pw = self.gh * self.s - self.H, self.gw * self.s - self.W
        if ph == 0 and pw == 0:
            return rgb
        return np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="edge")

    def pad_flags(self):
        """bool[N]: tile crosses the right or bottom border."""
        return ((self.tx + 1) * self.s > self.W) | ((self.ty + 1) * self.s > self.H)
```

- **Padding widths**: `ph = self.gh * self.s - self.H` (bottom pad rows, `0 <= ph < s`) and `pw = self.gw * self.s - self.W` (right pad columns, `0 <= pw < s`) (`visioncore/grid.py:32`).
- **Unpadded fast path**: If `ph == 0 and pw == 0` (`visioncore/grid.py:33`), `pad(rgb)` returns `rgb` without allocating or copying (`visioncore/grid.py:34`).
- **Edge replication**: Otherwise `pad(rgb)` returns `np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="edge")` (`visioncore/grid.py:35`) with output shape `(gh * s, gw * s, 3)` (`tests/test_grid.py:52`):
  - `p[:H, :W]` is identical to `rgb` (`tests/test_grid.py:53`);
  - `p[H:, :W]` replicates the last row `rgb[H - 1]` (`tests/test_grid.py:54`);
  - `p[:, W:]` replicates the last column `p[:, W - 1:W]`, including the corner pixel `rgb[H - 1, W - 1]` in `p[H:, W:]` (`tests/test_grid.py:55`).
- **Input validation in `pad`**: Validation of `rgb.shape` or `rgb.dtype` inside `Grid.pad` (`visioncore/grid.py:30`) is `ABSENT`.
- **`pad_flags(self)`**: Returns a `bool[N]` array in Morton rank order (`visioncore/grid.py:37`) evaluated as `((self.tx + 1) * self.s > self.W) | ((self.ty + 1) * self.s > self.H)` (`visioncore/grid.py:39`), marking tiles in the last column (`tx == gw - 1` when `pw > 0`) or last row (`ty == gh - 1` when `ph > 0`) (`tests/test_grid.py:56`, `tests/test_grid.py:57`, `tests/test_grid.py:58`).

## 6. Spatial Queries: `field_ids_of_rect` and `aligned_block` (`visioncore/grid.py`)

### 6.1 `field_ids_of_rect(frame, rect_px, grid)` (`visioncore/grid.py:42`)

```python
def field_ids_of_rect(frame, rect_px, grid):  # @anchor:vc.s0.field_ids_of_rect
    return ids.field_ids_of_rect(rect_px, grid.W, grid.H, frame)
```

- Defined at `visioncore/grid.py:42` (`# @anchor:vc.s0.field_ids_of_rect`) and delegates directly to `ids.field_ids_of_rect(rect_px, grid.W, grid.H, frame)` (`visioncore/grid.py:43`), where `ids` is imported at `visioncore/grid.py:5`.
- Because `visioncore/ids.py` is outside the four bound files, the internal implementation lines of `ids.field_ids_of_rect` are `ABSENT` from the bound files; the bound test suite `test_field_ids_of_rect` (`tests/test_grid.py:60`) establishes its contract:
  - A single-tile aligned rectangle `(0, 0, 16, 16)` on frame `7` at `1280 x 720` (`tests/test_grid.py:61`) returns `["f7:t0000"]` (`tests/test_grid.py:62`).
  - A rectangle straddling tile borders `(15, 15, 2, 2)` on frame `7` returns `4` field IDs in ascending Morton order (`tests/test_grid.py:63`).
  - A non-positive dimension rectangle `(5, 5, 0, 3)` returns `[]` (`tests/test_grid.py:64`).
  - An out-of-frame rectangle `(5000, 5000, 10, 10)` raises `VqaError` (`tests/test_grid.py:65`) with `code == "E_VC_ID_RANGE"` (`tests/test_grid.py:66`, `tests/test_grid.py:67`).

### 6.2 `aligned_block(bbox_t)` (`visioncore/grid.py:46`)

```python
def aligned_block(bbox_t):  # @anchor:vc.s0.aligned_block
    """bbox_t (tx0,ty0,tx1,ty1) -> (k, m0, m1): smallest aligned 2^k block containing it."""
    tx0, ty0, tx1, ty1 = bbox_t
    k = 0
    while (tx0 >> k) != (tx1 >> k) or (ty0 >> k) != (ty1 >> k):
        k += 1
    m0 = layout.morton((tx0 >> k) << k, (ty0 >> k) << k)
    return k, m0, m0 + 4 ** k - 1
```

- Unpacks the inclusive tile bounding box `tx0, ty0, tx1, ty1 = bbox_t` (`visioncore/grid.py:48`).
- Initializes `k = 0` (`visioncore/grid.py:49`) and increments `k += 1` (`visioncore/grid.py:51`) while `(tx0 >> k) != (tx1 >> k) or (ty0 >> k) != (ty1 >> k)` (`visioncore/grid.py:50`).
- Computes the aligned block origin Morton code `m0 = layout.morton((tx0 >> k) << k, (ty0 >> k) << k)` (`visioncore/grid.py:52`) and returns `(k, m0, m0 + 4 ** k - 1)` (`visioncore/grid.py:53`).
- Verified cases in `test_aligned_block` (`tests/test_grid.py:69`):
  - `(0, 0, 0, 0) -> (0, 0, 0)` (`tests/test_grid.py:70`);
  - `(2, 2, 3, 3) -> (1, layout.morton(2, 2), layout.morton(2, 2) + 3)` (`tests/test_grid.py:71`);
  - `(1, 0, 2, 0) -> (2, 0, 15)` (`tests/test_grid.py:72`, `tests/test_grid.py:73`);
  - `(3, 3, 4, 4) -> (3, 0, 63)` (`tests/test_grid.py:74`, `tests/test_grid.py:75`).
- Validation that `tx0 <= tx1`, `ty0 <= ty1`, or that `(tx1, ty1)` is within `0..AXIS_MAX` inside `aligned_block` (`visioncore/grid.py:46`) is `ABSENT`; only the aligned origin `((tx0 >> k) << k, (ty0 >> k) << k)` is range-checked by `layout.morton` (`visioncore/layout.py:51`).

## 7. Class, Dirty, and Auxiliary Enums (`visioncore/layout.py:63`)

### 7.1 Tile Class and Dirty Enums (`CLS`, `CLS_ID`, `CLS_DIRTY_MIN`, `cls_name`, `cls_id`, `is_dirty`)

```python
CLS = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")
CLS_ID = {n: i for i, n in enumerate(CLS)}
CLS_DIRTY_MIN = 2
```

```python
def cls_name(v):
    if not isinstance(v, int) or not 0 <= v < len(CLS):
        raise VqaError("E_SCHEMA", {"cls": v})
    return CLS[v]

def cls_id(name):
    if name not in CLS_ID:
        raise VqaError("E_SCHEMA", {"cls_name": name})
    return CLS_ID[name]
```

```python
def is_dirty(cls):
    cls_name(cls)
    return cls >= CLS_DIRTY_MIN
```

| `cls` Integer (`uint8`) | Class Name (`CLS[v]`) | `is_dirty(cls)` (`cls >= CLS_DIRTY_MIN`) | Citations |
|---:|---|---|---|
| `0` | `"UNCHANGED"` | `False` | `visioncore/layout.py:63`, `visioncore/layout.py:65`, `visioncore/layout.py:88` |
| `1` | `"JITTER"` | `False` | `visioncore/layout.py:63`, `visioncore/layout.py:65`, `visioncore/layout.py:88` |
| `2` | `"CHANGED"` | `True` (`CLS_DIRTY_MIN = 2`) | `visioncore/layout.py:63`, `visioncore/layout.py:65`, `visioncore/layout.py:88` |
| `3` | `"MOVED"` | `True` | `visioncore/layout.py:63`, `visioncore/layout.py:88` |
| `4` | `"FLICKER"` | `True` | `visioncore/layout.py:63`, `visioncore/layout.py:88` |
| `5` | `"FROZEN"` | `True` | `visioncore/layout.py:63`, `visioncore/layout.py:88` |
| `6` | `"DRIFT"` | `True` | `visioncore/layout.py:63`, `visioncore/layout.py:88` |

Validation rules:
- `cls_name(v)` (`visioncore/layout.py:74`): Checks `if not isinstance(v, int) or not 0 <= v < len(CLS):` (`visioncore/layout.py:75`), raising `VqaError("E_SCHEMA", {"cls": v})` (`visioncore/layout.py:76`), and returns `CLS[v]` (`visioncore/layout.py:77`). Explicit `isinstance(v, bool)` rejection in `cls_name` (`visioncore/layout.py:75`) is `ABSENT`.
- `cls_id(name)` (`visioncore/layout.py:80`): Checks `if name not in CLS_ID:` (`visioncore/layout.py:81`) against `CLS_ID` (`visioncore/layout.py:64`), raising `VqaError("E_SCHEMA", {"cls_name": name})` (`visioncore/layout.py:82`), and returns `CLS_ID[name]` (`visioncore/layout.py:83`).
- `is_dirty(cls)` (`visioncore/layout.py:86`): Validates `cls` via `cls_name(cls)` (`visioncore/layout.py:87`) before returning `cls >= CLS_DIRTY_MIN` (`visioncore/layout.py:88`).

### 7.2 Other Enum Tuples (`visioncore/layout.py:66`)

| Symbol | Tuple Value | Citations |
|---|---|---|
| `TILE_FLAGS` | `("EDGE", "TEXTURE", "SHADE_ONLY", "SAT", "UI_OCC", "ACTOR_OCC", "GLOBAL_MV", "PAD")` | `visioncore/layout.py:66` |
| `TRACK_FLAGS` | `("FROZEN", "DRIFT", "CYCLE", "APPEAR", "VANISH", "CUT")` | `visioncore/layout.py:67` |
| `VERDICTS` | `("PASS", "FAIL", "CANNOT_SEE")` | `visioncore/layout.py:68` |
| `UNIT_LAYERS` | `("FG", "BG")` | `visioncore/layout.py:69` |
| `UNIT_PINS` | `("FAIL", "CATALOGUED", "GOLDEN", None)` | `visioncore/layout.py:70` |
| `JUDGE_MODELS` | `("gemini-3.8-flash", "gemini-4-argon")` | `visioncore/layout.py:71` |

## 8. Binary Layouts and Functions (`visioncore/layout.py:94`)

All binary layouts in `visioncore/layout.py` use little-endian (`<`) `struct` format strings with standard widths and zero alignment padding (`visioncore/layout.py:94`, `visioncore/layout.py:104`, `visioncore/layout.py:108`).

### 8.1 64-Byte `tiles.bin` Header (`TILE_HDR_FMT`, `pack_tile_header`, `parse_tile_header`)

Header constants:
- `TILE_MAGIC = b"VQATILE1"` (`visioncore/layout.py:101`)
- `TILE_HDR_SIZE = 64` (`visioncore/layout.py:102`)
- `TILE_HDR_FMT = "<8sHHHHHHHHII16s8s8s"` (`visioncore/layout.py:104`), verified by `assert struct.calcsize(TILE_HDR_FMT) == TILE_HDR_SIZE` (`visioncore/layout.py:105`).

Exact 64-byte little-endian (`<`) layout:

| `struct` Slot | Field / Dict Key | Byte Offset | Width (B) | `struct` Token | Endianness & Native Type | Packing and Parsing Semantics | Citations |
|---:|---|---:|---:|---|---|---|---|
| `f[0]` | `magic` | `0` | `8` | `8s` | Raw bytes (`char[8]`) | Packed as `TILE_MAGIC` (`b"VQATILE1"`); parsed header requires `f[0] == TILE_MAGIC` | `visioncore/layout.py:101`, `visioncore/layout.py:114`, `visioncore/layout.py:123` |
| `f[1]` | `ver` | `8` | `2` | `H` | Little-endian `uint16_t` | Packed as `1`; parsed header requires `f[1] == 1` | `visioncore/layout.py:114`, `visioncore/layout.py:123` |
| `f[2]` | `"s"` | `10` | `2` | `H` | Little-endian `uint16_t` | Tile side `s` in pixels | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[3]` | `"W"` | `12` | `2` | `H` | Little-endian `uint16_t` | Frame width `W` in pixels | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[4]` | `"H"` | `14` | `2` | `H` | Little-endian `uint16_t` | Frame height `H` in pixels | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[5]` | `"gw"` | `16` | `2` | `H` | Little-endian `uint16_t` | Grid width `gw` in tiles | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[6]` | `"gh"` | `18` | `2` | `H` | Little-endian `uint16_t` | Grid height `gh` in tiles | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[7]` | `"N"` | `20` | `2` | `H` | Little-endian `uint16_t` | Tile count per frame `N` | `visioncore/layout.py:114`, `visioncore/layout.py:125` |
| `f[8]` | `rec` | `22` | `2` | `H` | Little-endian `uint16_t` | Packed as `TILE_SIZE` (`32`); parsed header requires `f[8] == TILE_SIZE` | `visioncore/layout.py:98`, `visioncore/layout.py:114`, `visioncore/layout.py:123` |
| `f[9]` | `"n_frames"` | `24` | `4` | `I` | Little-endian `uint32_t` | Number of frames | `visioncore/layout.py:114`, `visioncore/layout.py:126` |
| `f[10]` | `"f0"` | `28` | `4` | `I` | Little-endian `uint32_t` | Starting frame index | `visioncore/layout.py:114`, `visioncore/layout.py:126` |
| `f[11]` | `"params_sha256_16"` | `32` | `16` | `16s` | Raw bytes (`uint8_t[16]`) | Packed as `bytes.fromhex(params_sha256_hex)[:16]`; parsed as `f[11].hex()` | `visioncore/layout.py:115`, `visioncore/layout.py:126` |
| `f[12]` | `"api_version"` | `48` | `8` | `8s` | Raw ASCII bytes (`char[8]`) | Packed as `api_version.encode("ascii")[:8].ljust(8, b"\\0")`; parsed as `f[12].rstrip(b"\\0").decode("ascii")` | `visioncore/layout.py:115`, `visioncore/layout.py:127` |
| `f[13]` | `zero8` | `56` | `8` | `8s` | Raw bytes (`uint8_t[8]`) | Packed as `b"\\0" * 8` | `visioncore/layout.py:103`, `visioncore/layout.py:116` |

Header functions (`visioncore/layout.py:113` and `visioncore/layout.py:119`):

```python
def pack_tile_header(s, W, H, gw, gh, N, n_frames, f0, params_sha256_hex, api_version):
    return struct.pack(TILE_HDR_FMT, TILE_MAGIC, 1, s, W, H, gw, gh, N, TILE_SIZE, n_frames, f0,
                       bytes.fromhex(params_sha256_hex)[:16], api_version.encode("ascii")[:8].ljust(8, b"\0"),
                       b"\0" * 8)
```

```python
def parse_tile_header(b):
    if len(b) < TILE_HDR_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "short header"})
    f = struct.unpack(TILE_HDR_FMT, b[:TILE_HDR_SIZE])
    if f[0] != TILE_MAGIC or f[1] != 1 or f[8] != TILE_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "bad magic/ver/rec"})
    return {"s": f[2], "W": f[3], "H": f[4], "gw": f[5], "gh": f[6], "N": f[7],
            "n_frames": f[9], "f0": f[10], "params_sha256_16": f[11].hex(),
            "api_version": f[12].rstrip(b"\0").decode("ascii")}
```

- If `len(b) < 64` (`TILE_HDR_SIZE`), `parse_tile_header` raises `VqaError("E_VC_FORMAT", {"reason": "short header"})` (`visioncore/layout.py:120`, `visioncore/layout.py:121`).
- Unpacks `f = struct.unpack(TILE_HDR_FMT, b[:TILE_HDR_SIZE])` (`visioncore/layout.py:122`). If `f[0] != TILE_MAGIC or f[1] != 1 or f[8] != TILE_SIZE`, `parse_tile_header` raises `VqaError("E_VC_FORMAT", {"reason": "bad magic/ver/rec"})` (`visioncore/layout.py:123`, `visioncore/layout.py:124`).
- Validation of `f[13] == b"\\0" * 8` or consistency between `(s, W, H, gw, gh, N)` and `grid_params(W, H)` inside `parse_tile_header` (`visioncore/layout.py:119`) is `ABSENT`.

### 8.2 32-Byte Packed Tile Record (`TILE_FMT`, `unpack_tile`, `check_tile_records`)

Tile record constants (`# @anchor:vc.layout.tile_dtype` at `visioncore/layout.py:92`):
- `TILE_FMT = "<HBBHbbHHBBbbQQ"` (`visioncore/layout.py:94`)
- `TILE_FIELDS = ("morton", "cls", "flags", "y_q4", "co_h", "cg_h", "sig_q4", "grad_q6", "edge", "resid", "mv_dx", "mv_dy", "sig64", "xh64")` (`visioncore/layout.py:95`, `visioncore/layout.py:96`)
- `TILE_OFFSETS = (0, 2, 3, 4, 6, 7, 8, 10, 12, 13, 14, 15, 16, 24)` (`visioncore/layout.py:97`)
- `TILE_SIZE = 32` (`visioncore/layout.py:98`), verified by `assert struct.calcsize(TILE_FMT) == TILE_SIZE` (`visioncore/layout.py:99`).

Exact 32-byte little-endian (`<`) field layout:

| Index | Field Name (`TILE_FIELDS`) | Byte Offset (`TILE_OFFSETS`) | Width (B) | `struct` Token | Endianness & Native Type | Signedness and Domain | Citations |
|---:|---|---:|---:|---|---|---|---|
| `0` | `morton` | `0` | `2` | `H` | Little-endian `uint16_t` | Unsigned `0..65535` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `1` | `cls` | `2` | `1` | `B` | Little-endian `uint8_t` | Unsigned `0..255` (valid `CLS` index `0..6`) | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97`, `visioncore/layout.py:139` |
| `2` | `flags` | `3` | `1` | `B` | Little-endian `uint8_t` | Unsigned `0..255` (`TILE_FLAGS` bits `0..7`) | `visioncore/layout.py:66`, `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `3` | `y_q4` | `4` | `2` | `H` | Little-endian `uint16_t` | Unsigned `0..65535` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `4` | `co_h` | `6` | `1` | `b` | Little-endian `int8_t` | Signed `-128..127` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `5` | `cg_h` | `7` | `1` | `b` | Little-endian `int8_t` | Signed `-128..127` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `6` | `sig_q4` | `8` | `2` | `H` | Little-endian `uint16_t` | Unsigned `0..65535` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `7` | `grad_q6` | `10` | `2` | `H` | Little-endian `uint16_t` | Unsigned `0..65535` | `visioncore/layout.py:94`, `visioncore/layout.py:95`, `visioncore/layout.py:97` |
| `8` | `edge` | `12` | `1` | `B` | Little-endian `uint8_t` | Unsigned `0..255` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |
| `9` | `resid` | `13` | `1` | `B` | Little-endian `uint8_t` | Unsigned `0..255` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |
| `10` | `mv_dx` | `14` | `1` | `b` | Little-endian `int8_t` | Signed `-128..127` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |
| `11` | `mv_dy` | `15` | `1` | `b` | Little-endian `int8_t` | Signed `-128..127` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |
| `12` | `sig64` | `16` | `8` | `Q` | Little-endian `uint64_t` | Unsigned `0..2^64-1` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |
| `13` | `xh64` | `24` | `8` | `Q` | Little-endian `uint64_t` | Unsigned `0..2^64-1` | `visioncore/layout.py:94`, `visioncore/layout.py:96`, `visioncore/layout.py:97` |

Record functions (`visioncore/layout.py:130` and `visioncore/layout.py:134`):

```python
def unpack_tile(b, off=0):
    return dict(zip(TILE_FIELDS, struct.unpack_from(TILE_FMT, b, off)))

def check_tile_records(b):
    """Validate cls of every 32 B record in b; cls > 6 -> E_SCHEMA (T-R2)."""
    if len(b) % TILE_SIZE:
        raise VqaError("E_VC_FORMAT", {"reason": "length not multiple of 32"})
    for off in range(0, len(b), TILE_SIZE):
        cls_name(b[off + 2])
```

- `unpack_tile(b, off=0)` (`visioncore/layout.py:130`) calls `struct.unpack_from(TILE_FMT, b, off)` and zips the 14 unpacked integers with `TILE_FIELDS` (`visioncore/layout.py:131`). Catching `struct.error` or validating `cls` inside `unpack_tile` is `ABSENT`.
- `check_tile_records(b)` (`visioncore/layout.py:134`) checks `if len(b) % TILE_SIZE:` (`visioncore/layout.py:136`), raising `VqaError("E_VC_FORMAT", {"reason": "length not multiple of 32"})` (`visioncore/layout.py:137`), then loops `for off in range(0, len(b), TILE_SIZE):` (`visioncore/layout.py:138`) and validates `cls_name(b[off + 2])` (`visioncore/layout.py:139`), which raises `VqaError("E_SCHEMA", {"cls": b[off + 2]})` (`visioncore/layout.py:76`) whenever byte `off + 2` is `> 6`. Validation of fields other than `cls` in `check_tile_records` is `ABSENT`.

### 8.3 8-Byte Global Motion Layout (`GLOBAL_MOTION_FMT`)

`visioncore/layout.py:108` defines `GLOBAL_MOTION_FMT = "<hhI"` with `GLOBAL_MOTION_SIZE = 8` (`visioncore/layout.py:109`), verified by `assert struct.calcsize(GLOBAL_MOTION_FMT) == GLOBAL_MOTION_SIZE` (`visioncore/layout.py:110`): two little-endian signed 16-bit integers (`h`, 2 B each at offsets `0` and `2`) followed by one little-endian unsigned 32-bit integer (`I`, 4 B at offset `4`). Field names and pack/unpack helpers for `GLOBAL_MOTION_FMT` in `visioncore/layout.py` are `ABSENT`.

## 9. Complete Error Code Summary (`visioncore/layout.py`, `visioncore/grid.py`, `visioncore/params.py`, `tests/test_grid.py`)

| Error Code | Exit `rc` | Raising Function(s) | Trigger Condition | `detail` Payload | Citations |
|---|---:|---|---|---|---|
| `E_GRID` | `2` | `grid_params`, `grid_ident`, `Grid.__init__` | `not (isinstance(W, int) and isinstance(H, int))` or `not (1 <= W <= 8192 and 1 <= H <= 8192)` | `{"W": W, "H": H, "reason": "dimension outside 1..8192"}` | `visioncore/layout.py:17`, `visioncore/layout.py:18`, `tests/test_grid.py:27` |
| `E_GRID` | `2` | `grid_params`, `grid_ident`, `Grid.__init__` | Search loop `s <= 1 << 16` finishes without satisfying `gw * gh <= 4096 and max(gw, gh) <= 255` | `{"W": W, "H": H}` | `visioncore/layout.py:20`, `visioncore/layout.py:22`, `visioncore/layout.py:25` |
| `E_VC_ID_RANGE` | `3` | `morton`, `aligned_block` | `not (0 <= tx <= 255 and 0 <= ty <= 255)` | `{"tx": tx, "ty": ty}` | `visioncore/layout.py:51`, `visioncore/layout.py:52`, `visioncore/grid.py:52` |
| `E_VC_ID_RANGE` | `3` | `unmorton` | `not 0 <= m <= 0xFFFF` | `{"m": m}` | `visioncore/layout.py:57`, `visioncore/layout.py:58` |
| `E_VC_ID_RANGE` | `3` | `field_ids_of_rect` | Positive-area `rect_px` disjoint from frame bounds (or invalid `frame` / `morton` in delegated `ids.field_ids_of_rect`) | `{"rect": list(rect_px)}` (delegated to `ids.field_ids_of_rect`) | `visioncore/grid.py:42`, `visioncore/grid.py:43`, `tests/test_grid.py:67` |
| `E_SCHEMA` | `2` | `cls_name`, `is_dirty`, `check_tile_records` | `not isinstance(v, int) or not 0 <= v < 7` | `{"cls": v}` | `visioncore/layout.py:75`, `visioncore/layout.py:76`, `visioncore/layout.py:87`, `visioncore/layout.py:139` |
| `E_SCHEMA` | `2` | `cls_id` | `name not in CLS_ID` | `{"cls_name": name}` | `visioncore/layout.py:81`, `visioncore/layout.py:82` |
| `E_SCHEMA` | `2` | `effective` | Duplicate key after `.upper()` folding | `{"param": key, "reason": "duplicate key after case folding"}` | `visioncore/params.py:72`, `visioncore/params.py:73` |
| `E_SCHEMA` | `2` | `effective` | Key in `PARAMS` but `not in PROFILE_KEYS` | `{"param": key, "reason": "not profile-overridable"}` | `visioncore/params.py:77`, `visioncore/params.py:78` |
| `E_VC_FORMAT` | `2` | `parse_tile_header` | `len(b) < 64` (`TILE_HDR_SIZE`) | `{"reason": "short header"}` | `visioncore/layout.py:120`, `visioncore/layout.py:121` |
| `E_VC_FORMAT` | `2` | `parse_tile_header` | `f[0] != b"VQATILE1" or f[1] != 1 or f[8] != 32` | `{"reason": "bad magic/ver/rec"}` | `visioncore/layout.py:123`, `visioncore/layout.py:124` |
| `E_VC_FORMAT` | `2` | `check_tile_records` | `len(b) % 32 != 0` (`TILE_SIZE`) | `{"reason": "length not multiple of 32"}` | `visioncore/layout.py:136`, `visioncore/layout.py:137` |
| `E_VC_PARAM_UNKNOWN` | `2` | `effective` | Upper-cased key `k not in PARAMS` | `{"param": key}` | `visioncore/params.py:75`, `visioncore/params.py:76` |
| `E_VC_PARAM_RANGE` | `2` | `_check`, `effective` | `not isinstance(v, int) or isinstance(v, bool)` | `{"param": k, "reason": "not int"}` | `visioncore/params.py:56`, `visioncore/params.py:58`, `visioncore/params.py:59` |
| `E_VC_PARAM_RANGE` | `2` | `_check`, `effective` | `not lo <= v <= hi` where `lo, hi = BOUNDS[k]` | `{"param": k, "value": v, "bounds": [lo, hi]}` | `visioncore/params.py:56`, `visioncore/params.py:60`, `visioncore/params.py:61`, `visioncore/params.py:62` |

## 10. Undefined Behaviours (`ABSENT`) and VM Verification

### 10.1 Behaviours Undefined by the Bound Code (`ABSENT`)

- **Explicit `bool` rejection in `visioncore/layout.py`**: While `_check` (`visioncore/params.py:58`) rejects `isinstance(v, bool)`, explicit `bool` rejection in `grid_params` (`visioncore/layout.py:17`), `morton` (`visioncore/layout.py:51`), `unmorton` (`visioncore/layout.py:57`), and `cls_name` (`visioncore/layout.py:75`) is `ABSENT`.
- **Non-integer type guards in `morton` and `unmorton`**: Explicit `isinstance(..., int)` checks before `0 <= tx <= AXIS_MAX` (`visioncore/layout.py:51`) and `0 <= m <= 0xFFFF` (`visioncore/layout.py:57`) are `ABSENT`.
- **Array shape and dtype validation in `Grid.pad`**: Validation that `rgb` has shape `(self.H, self.W, 3)` and `dtype == np.uint8` inside `Grid.pad` (`visioncore/grid.py:30`) is `ABSENT`.
- **Bounding-box ordering and grid-cap validation in `aligned_block`**: Validation that `tx0 <= tx1`, `ty0 <= ty1`, `tx1 < gw`, or `ty1 < gh` inside `aligned_block` (`visioncore/grid.py:46`) is `ABSENT`.
- **Internal implementation of `ids.field_ids_of_rect` and `VqaError` in bound files**: `field_ids_of_rect` (`visioncore/grid.py:42`) delegates to `ids.field_ids_of_rect` (`visioncore/grid.py:43`), and `VqaError` is imported at `visioncore/layout.py:5` and `visioncore/params.py:6`; their definitions lie outside the four bound files (`ABSENT` from the bound file set).
- **Header cross-field consistency in `parse_tile_header`**: Validation that `(s, gw, gh) == grid_params(W, H)`, `N == gw * gh`, or `zero8 == b"\\0" * 8` in `parse_tile_header` (`visioncore/layout.py:119`) is `ABSENT`.
- **Single-record packer and `unpack_tile` error wrapping**: A `pack_tile` helper in `visioncore/layout.py` and wrapping `struct.error` into `VqaError` in `unpack_tile` (`visioncore/layout.py:130`) are `ABSENT`.
- **Non-`cls` field validation in `check_tile_records`**: Validation of `morton` or any field other than `cls` (`b[off + 2]`, `visioncore/layout.py:139`) in `check_tile_records` (`visioncore/layout.py:134`) is `ABSENT`.
- **Helpers for `GLOBAL_MOTION_FMT`**: Field names and pack/unpack functions for `GLOBAL_MOTION_FMT` (`visioncore/layout.py:108`) in `visioncore/layout.py` are `ABSENT`.

### 10.2 VM Test Verification

Running `python3 -m unittest tests.test_grid tests.test_e1_synth_grid` in the VM passes all 14 tests (`7` in `tests/test_grid.py:15` and `7` in `tests.test_e1_synth_grid`) in `0.804s` (`VM`).
