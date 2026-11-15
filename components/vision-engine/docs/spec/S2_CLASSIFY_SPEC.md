# Stage S2 Change Classification Specification (`vc.s2`)

This document specifies Stage S2 tile change classification as implemented in `visioncore/change.py` at ref `bridge/integration`, with supporting parameter and record layout definitions from the bound source files `visioncore/params.py`, `visioncore/layout.py`, and `AGENTS.md`. Everything in this specification is derived exclusively from those four bound files; any behaviour, symbol, or definition not present in the bound files is marked `ABSENT`.

## 1. Bound Source Files, Module Identity, and Invariants

| Bound Path | Lines | Bytes | Git Blob SHA-1 | SHA-256 | Role in S2 |
|---|---:|---:|---|---|---|
| `visioncore/change.py` | 455 | 21,442 | `5434abe9d29c50b0602e789383c9dc5c900b8333` | `a0c4c4c1874ba152f7c68b54130f6bd31bc102e90b5264ce4fcebca17a16b251` | Stage S2 implementation (`vc.s2`, Layer 2) (`visioncore/change.py:1-455`) |
| `visioncore/params.py` | 86 | 5,042 | `377487af75d2b29be65b28b4b7417628590adcc8` | `4c9956f74e8be6634ca15dc2d87f9f9bc7677fb24722854dc4b78c7305689378` | Integer parameter registry (`vc.p`, Layer 0) (`visioncore/params.py:1-86`) |
| `visioncore/layout.py` | 139 | 4,714 | `170fdcd9294f5fb106e64f6c8b09eec3b8aa8eaf` | `0750a6dc4eb84b4be018bd41e3302ab244df3ec2bb4ab5b6eb3c49729db0de02` | Grid rule, Morton codec, class/flag enums, 32-byte tile layout (`vc.layout`, Layer 0) (`visioncore/layout.py:1-139`) |
| `AGENTS.md` | 57 | 4,121 | `203953c5cbcbe9c0e7f3da800f4aa0ccd93ac825` | `c8bc2632bb713339d108c1125df359e0d3a51171f13f1c715aa4ed6a68da6202` | Layering, integer-only rules, ownership, exit codes (`AGENTS.md:1-57`) |

### 1.1 Module Header and Architectural Constraints
- **Module declaration**: `# @module {"id":"vc.s2","layer":2,"stage":"S2","owner":"CORE","deps":["vc.layout","vc.p","vc.errors"],"inputs":"tile records t, ClassState, words_eq[N]","outputs":"tile records with cls/resid/mv, ClassState, summary","invariants":["integer-only","deterministic","pure-step"],"anti_uses":["float thresholds","per-engine branches"],"oracle":"tests/test_change.py","book":"HB-VC-s2-change"}` (`visioncore/change.py:1`).
- **Layer rule**: `change` resides at Layer 2 (`AGENTS.md:20`) and may import only Layer `<= 2` modules (`AGENTS.md:15-23`). In code, `visioncore/change.py` imports `numpy as np` (`visioncore/change.py:7`), `layout` and `params as P` (`visioncore/change.py:9`), and `VqaError` from `.errors` (`visioncore/change.py:10`).
- **Integer-only & determinism invariants**:
  - Layers L0–L3 use integers only; no float literals, `float(`, or true division `/` (`AGENTS.md:32`, `visioncore/change.py:5`).
  - Floor division (`//`) and arithmetic right shift (`>>`) are used for every division/shift (`visioncore/change.py:5`).
  - No clock and no RNG are used (`visioncore/change.py:5`).

## 2. The Seven Change Classes, IDs, and Tile Flags

### 2.1 Change Class Enumeration and IDs
The seven tile change classes and their integer IDs (`0..6`) are defined in `visioncore/change.py:39` and `visioncore/layout.py:63-65,74-88`:

| ID (`uint8`) | Symbolic Constant (`visioncore/change.py:39`) | Canonical Name (`visioncore/layout.py:63`) | Dirty (`cls >= CLS_DIRTY_MIN`, `visioncore/layout.py:65,86-88`) | Summary Count Slot (`visioncore/change.py:285,437-439`) |
|---:|---|---|---|---|
| `0` | `C_UNCHANGED` | `"UNCHANGED"` | `False` | `summary["n"][0]` |
| `1` | `C_JITTER` | `"JITTER"` | `False` | `summary["n"][1]` |
| `2` | `C_CHANGED` | `"CHANGED"` | `True` | `summary["n"][2]` (counted in `cov_ppm`) |
| `3` | `C_MOVED` | `"MOVED"` | `True` | `summary["n"][3]` (counted in `cov_ppm`) |
| `4` | `C_FLICKER` | `"FLICKER"` | `True` | `summary["n"][4]` (counted in `cov_ppm`) |
| `5` | `C_FROZEN` | `"FROZEN"` | `True` | `summary["n"][5]` (counted in `cov_ppm`) |
| `6` | `C_DRIFT` | `"DRIFT"` | `True` | `summary["n"][6]` (counted in `cov_ppm`) |

- **Enum tuple and lookup table**:
  - `C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)` (`visioncore/change.py:39`).
  - `CLS = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")` (`visioncore/layout.py:63`).
  - `CLS_ID = {n: i for i, n in enumerate(CLS)}` (`visioncore/layout.py:64`).
  - `CLS_DIRTY_MIN = 2` (`visioncore/layout.py:65`).
- **Validation helpers in `visioncore/layout.py`**:
  - `cls_name(v)` (`visioncore/layout.py:74-77`): if `not isinstance(v, int) or not 0 <= v < len(CLS)`, raises `VqaError("E_SCHEMA", {"cls": v})`; otherwise returns `CLS[v]`.
  - `cls_id(name)` (`visioncore/layout.py:80-83`): if `name not in CLS_ID`, raises `VqaError("E_SCHEMA", {"cls_name": name})`; otherwise returns `CLS_ID[name]`.
  - `is_dirty(cls)` (`visioncore/layout.py:86-88`): calls `cls_name(cls)` to validate `cls`, then returns `cls >= CLS_DIRTY_MIN` (`cls >= 2`).

### 2.2 Tile Flags (`flags` Byte)
`TILE_FLAGS = ("EDGE", "TEXTURE", "SHADE_ONLY", "SAT", "UI_OCC", "ACTOR_OCC", "GLOBAL_MV", "PAD")` (`visioncore/layout.py:66`).

| Bit Index | Bitmask Constant | Value (`uint8`) | Name (`visioncore/layout.py:66`) | Stage S2 Handling |
|---:|---|---:|---|---|
| 0 | `ABSENT` in bound files | `1` (`1 << 0`) | `"EDGE"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |
| 1 | `ABSENT` in bound files | `2` (`1 << 1`) | `"TEXTURE"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |
| 2 | `FLAG_SHADE_ONLY` (`visioncore/change.py:37`) | `4` (`1 << 2`) | `"SHADE_ONLY"` | Cleared at `visioncore/change.py:284`; set on qualifying `C_CHANGED` tiles at `visioncore/change.py:412-413` |
| 3 | `ABSENT` in bound files | `8` (`1 << 3`) | `"SAT"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |
| 4 | `ABSENT` in bound files | `16` (`1 << 4`) | `"UI_OCC"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |
| 5 | `ABSENT` in bound files | `32` (`1 << 5`) | `"ACTOR_OCC"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |
| 6 | `FLAG_GLOBAL_MV` (`visioncore/change.py:38`) | `64` (`1 << 6`) | `"GLOBAL_MV"` | Cleared at `visioncore/change.py:284`; set on `moved & is_glob` tiles at `visioncore/change.py:368` |
| 7 | `ABSENT` in bound files | `128` (`1 << 7`) | `"PAD"` | Preserved from input `tiles["flags"]` (`visioncore/change.py:284`) |

Note: In `classify_first(state, tiles, y_cur=None)`, `out["flags"]` is copied directly from `tiles["flags"]` via `_as_tiles(tiles)` without clearing `FLAG_GLOBAL_MV` or `FLAG_SHADE_ONLY` (`visioncore/change.py:106-110,445-449`).

## 3. Tile Record Layout (`TILE_DT` and `vqa.tile/v1`)

Stage S2 operates on 1-D NumPy structured arrays of length `N` with dtype `TILE_DT` (`visioncore/change.py:23-26`), matching the 32-byte little-endian binary record defined in `visioncore/layout.py:94-99`:

- `TILE_FMT = "<HBBHbbHHBBbbQQ"` (`visioncore/layout.py:94`)
- `TILE_FIELDS = ("morton", "cls", "flags", "y_q4", "co_h", "cg_h", "sig_q4", "grad_q6", "edge", "resid", "mv_dx", "mv_dy", "sig64", "xh64")` (`visioncore/layout.py:95-96`)
- `TILE_OFFSETS = (0, 2, 3, 4, 6, 7, 8, 10, 12, 13, 14, 15, 16, 24)` (`visioncore/layout.py:97`)
- `TILE_SIZE = 32` (`visioncore/layout.py:98-99`, `visioncore/change.py:26`)
- `_as_tiles(tiles)` (`visioncore/change.py:106-110`): allocates `out = np.empty(len(tiles), dtype=TILE_DT)` and copies each field `for n in layout.TILE_FIELDS: out[n] = tiles[n]`.

| Field | Byte Offset (`visioncore/layout.py:97`) | `struct` Format (`visioncore/layout.py:94`) | NumPy Dtype (`visioncore/change.py:25`) | S2 Read / Write Semantics |
|---|---:|---|---|---|
| `morton` | 0 | `<H` (`uint16`) | `<u2` | **Read** by `_morton_xy` (`visioncore/change.py:74,100,288,314,385,407,452`); copied to `out["morton"]` (`visioncore/change.py:109,279`) |
| `cls` | 2 | `B` (`uint8`) | `u1` | Input ignored (`visioncore/change.py:267`); **written** with class ID `0..6` (`visioncore/change.py:280,401,446`) |
| `flags` | 3 | `B` (`uint8`) | `u1` | **Read & written**: `classify_tiles` sets `out["flags"] = t["flags"] & ~np.uint8(FLAG_GLOBAL_MV \| FLAG_SHADE_ONLY)` (`visioncore/change.py:284`), then ORs `FLAG_GLOBAL_MV` (`visioncore/change.py:368`) and `FLAG_SHADE_ONLY` (`visioncore/change.py:413`) |
| `y_q4` | 4 | `<H` (`uint16`) | `<u2` | **Read** in `residual` (`visioncore/change.py:58,61`); copied unchanged to `out` |
| `co_h` | 6 | `b` (`int8`) | `i1` | **Read** in `residual` (`visioncore/change.py:58,61`); copied unchanged to `out` |
| `cg_h` | 7 | `b` (`int8`) | `i1` | **Read** in `residual` (`visioncore/change.py:58,61`); copied unchanged to `out` |
| `sig_q4` | 8 | `<H` (`uint16`) | `<u2` | **Read** in `residual` (`visioncore/change.py:58,61`); copied unchanged to `out` |
| `grad_q6` | 10 | `<H` (`uint16`) | `<u2` | Copied to `out` (`visioncore/change.py:109,279`); not read by any S2 computation |
| `edge` | 12 | `B` (`uint8`) | `u1` | **Read** in `SHADE_ONLY` check (`visioncore/change.py:411`); copied unchanged to `out` |
| `resid` | 13 | `B` (`uint8`) | `u1` | Input ignored; **written** with `min(255, r >> 2)` (`uint8`) or `0` for `C_UNCHANGED` (`visioncore/change.py:281,417-420,447`) |
| `mv_dx` | 14 | `b` (`int8`) | `i1` | Input ignored; **written** with `0` or signed horizontal displacement `mvx` (`visioncore/change.py:282,366,448`) |
| `mv_dy` | 15 | `b` (`int8`) | `i1` | Input ignored; **written** with `0` or signed vertical displacement `mvy` (`visioncore/change.py:283,367,449`) |
| `sig64` | 16 | `<Q` (`uint64`) | `<u8` | **Read** in `residual` (`visioncore/change.py:59`); copied unchanged to `out` |
| `xh64` | 24 | `<Q` (`uint64`) | `<u8` | Copied to `out` (`visioncore/change.py:109,279`); not read by any S2 computation |

## 4. Constants and Parameter Table

### 4.1 Module-Level Constants in `visioncore/change.py`

| Constant | Value / Type | Location | Usage in `visioncore/change.py` |
|---|---|---|---|
| `_POP8` | `np.array([bin(i).count("1") for i in range(256)], np.uint8)` | `visioncore/change.py:12` | Fallback byte-level popcount lookup table when `np.bitwise_count` is absent (`visioncore/change.py:17-20`) |
| `W_CHROMA` | `8` (`int`) | `visioncore/change.py:29` | Weight multiplying `d("co_h") + d("cg_h")` in `residual` (`visioncore/change.py:61`) |
| `W_HAM` | `8` (`int`) | `visioncore/change.py:30` | Weight multiplying `np.maximum(0, ham - hd)` in `residual` (`visioncore/change.py:61`) |
| `FROZEN_PRE_ACT` | `8` (`int`) | `visioncore/change.py:31` | Minimum prior activity count in 16-frame history for `C_FROZEN` (`visioncore/change.py:377`) and non-motion activity threshold in aperture override (`visioncore/change.py:390`) |
| `DRIFT_STALL` | `P.PARAMS["DRIFT_STALL"]` (`8`) | `visioncore/change.py:32` | Default alias; `classify_tiles` reads `p["DRIFT_STALL"]` (`visioncore/change.py:32,398-399`) |
| `MOTION_D_MAX` | `64` (`int`) | `visioncore/change.py:33` | Upper bound on 1-D projection search range `dmax = min(s, MOTION_D_MAX)` (`visioncore/change.py:182`) |
| `SHADE_DEN` | `64` (`int`) | `visioncore/change.py:34` | Denominator in `(s * s) // SHADE_DEN` minority-sign pixel count check for `FLAG_SHADE_ONLY` (`visioncore/change.py:412`) |
| `SHADE_EDGE_MAX` | `8` (`int`) | `visioncore/change.py:35` | Maximum `\|edge(t) - edge(prev1)\|` for `FLAG_SHADE_ONLY` (`visioncore/change.py:412`) |
| `ISLAND_MAX` | `16` (`int`) | `visioncore/change.py:36` | Maximum number of 8-connected active islands evaluated per frame (`visioncore/change.py:354`) |
| `FLAG_SHADE_ONLY` | `1 << 2` (`4`) | `visioncore/change.py:37` | Bit 2 of `flags` (`visioncore/change.py:284,413`) |
| `FLAG_GLOBAL_MV` | `1 << 6` (`64`) | `visioncore/change.py:38` | Bit 6 of `flags` (`visioncore/change.py:284,368`) |
| `_ALL64` | `np.uint64(0xFFFFFFFFFFFFFFFF)` | `visioncore/change.py:64` | All-64-bits-set confidence mask (`visioncore/change.py:76,98,133`) |

### 4.2 Registered Parameters in `visioncore/params.py` Relevant to `CORE` / Stage S2

`visioncore/params.py:11-54` defines `_ROWS`, mapping each parameter ID to `(default, unit, owner, profile_overridable, (lo, hi))`:

| Parameter Key | Default | Unit | Owner | Profile-Overridable (`PROFILE_KEYS`, `visioncore/params.py:53`) | Bounds `(lo, hi)` | Definition (`visioncore/params.py`) | Read Location(s) in `visioncore/change.py` |
|---|---:|---|---|---|---|---|---|
| `T0_Q4` | `32` | `"q4"` | `"CORE"` | `True` | `(1, 4080)` | `visioncore/params.py:15` | `visioncore/change.py:49` (`thresholds`) |
| `HYST_BAND_PERMILLE` | `100` | `"permille"` | `"CORE"` | `False` | `(0, 500)` | `visioncore/params.py:16` | `visioncore/change.py:49` (`thresholds`) |
| `H_DEAD` | `2` | `"bits"` | `"CORE"` | `False` | `(0, 64)` | `visioncore/params.py:17` | `visioncore/change.py:57,289,307,394` (`residual`, `classify_tiles`) |
| `SIG_DEAD_Q4` | `24` | `"q4"` | `"CORE"` | `False` | `(0, 4080)` | `visioncore/params.py:18` | `visioncore/change.py:288,452` (`classify_tiles`, `classify_first`) |
| `DRIFT_U_Q4` | `35` | `"q4"` | `"CORE"` | `True` | `(1, 4080)` | `visioncore/params.py:22` | `visioncore/change.py:51` (`thresholds`) |
| `FROZEN_M` | `8` | `"frames"` | `"CORE"` | `False` | `(1, 255)` | `visioncore/params.py:24` | `visioncore/change.py:377` (`classify_tiles`) |
| `FROZEN_COV_PPM` | `10000` | `"ppm"` | `"CORE"` | `False` | `(0, 1000000)` | `visioncore/params.py:25` | `visioncore/change.py:375` (`classify_tiles`) |
| `DRIFT_STALL` | `8` | `"frames"` | `"CORE"` | `False` | `(1, 255)` | `visioncore/params.py:26` | `visioncore/change.py:32,398,399` (`DRIFT_STALL`, `classify_tiles`) |
| `MV_SAD_RATIO_DEN` | `3` | `"ratio"` | `"CORE"` | `False` | `(2, 16)` | `visioncore/params.py:27` | `visioncore/change.py:316` (`classify_tiles`) |
| `GLOBAL_SHARE_PPM` | `500000` | `"ppm"` | `"CORE"` | `False` | `(0, 1000000)` | `visioncore/params.py:28` | `visioncore/change.py:336` (`classify_tiles`) |
| `TILE_MIN_PX` | `16` | `"px"` | `"CORE"` | `False` | `(16, 16)` | `visioncore/params.py:12` | Not read in `change.py`; read in `visioncore/layout.py:9,19` |
| `CELL_MAX` | `4096` | `"cells"` | `"CORE"` | `False` | `(4096, 4096)` | `visioncore/params.py:13` | Not read in `change.py`; read in `visioncore/layout.py:10,22` |
| `AXIS_MAX` | `255` | `"cells"` | `"CORE"` | `False` | `(255, 255)` | `visioncore/params.py:14` | Not read in `change.py`; read in `visioncore/layout.py:11,22,51` |
| `EMA_F_Q16` | `61440` | `"q16"` | `"CORE"` | `False` | `(1, 65535)` | `visioncore/params.py:19` | `ABSENT` in `visioncore/change.py` |
| `EPS_Q4` | `9` | `"q4"` | `"CORE"` | `False` | `(1, 255)` | `visioncore/params.py:20` | `ABSENT` in `visioncore/change.py` |
| `DRIFT_C_Q4` | `8` | `"q4"` | `"CORE"` | `True` | `(1, 4080)` | `visioncore/params.py:21` | `ABSENT` in `visioncore/change.py` |
| `TAU_EDGE` | `48` | `"grey"` | `"CORE"` | `False` | `(1, 255)` | `visioncore/params.py:23` | `ABSENT` in `visioncore/change.py` |

- **Parameter table resolution (`params.effective`, `visioncore/params.py:56-81`)**:
  - `effective(profile=None)` copies `PARAMS` (`visioncore/params.py:68`) and overlays `profile` keys case-insensitively (`k = key.upper() if isinstance(key, str) else key`, `visioncore/params.py:71`).
  - Duplicate keys after case folding raise `VqaError("E_SCHEMA", {"param": key, "reason": "duplicate key after case folding"})` (`visioncore/params.py:72-73`).
  - Unknown keys raise `VqaError("E_VC_PARAM_UNKNOWN", {"param": key})` (`visioncore/params.py:75-76`).
  - Keys not in `PROFILE_KEYS` (`{"T0_Q4", "DRIFT_C_Q4", "DRIFT_U_Q4"}`) raise `VqaError("E_SCHEMA", {"param": key, "reason": "not profile-overridable"})` (`visioncore/params.py:77-78`).
  - `_check(table)` (`visioncore/params.py:56-62`) verifies for every `(k, v)` that `isinstance(v, int) and not isinstance(v, bool)` and `BOUNDS[k][0] <= v <= BOUNDS[k][1]`, raising `VqaError("E_VC_PARAM_RANGE", ...)` otherwise.
  - Note: `thresholds(params=None)` (`visioncore/change.py:48`) and `classify_tiles(..., params=None)` (`visioncore/change.py:277`) do not call `P.effective()`; they bind `p = P.PARAMS if params is None else params` directly.

## 5. Thresholds Function and Hysteresis Band

### 5.1 `thresholds(params=None)` (`visioncore/change.py:42-51`)
- **Anchor**: `# @anchor:vc.s2.drift` (`visioncore/change.py:42`).
- **Execution**:
  1. `p = P.PARAMS if params is None else params` (`visioncore/change.py:48`)
  2. `t0, band = p["T0_Q4"], p["HYST_BAND_PERMILLE"]` (`visioncore/change.py:49`)
  3. `t_enter = t0 * (1000 + band) // 1000` (`visioncore/change.py:50`)
  4. `t_exit = t0 * (1000 - band) // 1000` (`visioncore/change.py:51`)
  5. `drift_u = max(t_enter, p["DRIFT_U_Q4"])` (`visioncore/change.py:51`)
  6. Returns tuple `(t_enter, t_exit, drift_u)` in `q4` units (`visioncore/change.py:51`).
- **Default values** (`T0_Q4 = 32`, `HYST_BAND_PERMILLE = 100`, `DRIFT_U_Q4 = 35`):
  - `t_enter = 32 * 1100 // 1000 = 35`
  - `t_exit = 32 * 900 // 1000 = 28`
  - `drift_u = max(35, 35) = 35`

### 5.2 Hysteresis Band Evaluation (`visioncore/change.py:290-292,422`)
In `classify_tiles`:
1. `r1[words_eq] = 0` (`visioncore/change.py:290`).
2. `hot = state.hot.astype(bool)` (`visioncore/change.py:291`), where `state.hot` (`uint8[N]`) equals `active.astype(np.uint8)` from frame `t - 1` (`0` on frame 1 after `ClassState.init`, `visioncore/change.py:126,422`).
3. `active = np.where(hot, r1 > t_exit, r1 > t_enter) & ~words_eq` (`visioncore/change.py:292`):
   - If `words_eq[i]` is `True`: `active[i]` is `False` (regardless of `hot[i]`, `t_enter`, or `t_exit`).
   - If `words_eq[i]` is `False` and `hot[i]` is `False` (cold tile): `active[i]` is `True` iff `r1[i] > t_enter` (strict inequality `>`).
   - If `words_eq[i]` is `False` and `hot[i]` is `True` (hot tile): `active[i]` is `True` iff `r1[i] > t_exit` (strict inequality `>`).
   - **Hysteresis band `(t_exit, t_enter]`**: For `t_exit < r1[i] <= t_enter` with `words_eq[i] == False`, a hot tile remains `active = True` while a cold tile remains `active = False`.
4. At the end of `classify_tiles`, `state.hot = active.astype(np.uint8)` (`visioncore/change.py:422`).

## 6. Residual and Noise-Robust Signature Confidence (`sig_conf`)

### 6.1 Population Count `_popcount(a)` (`visioncore/change.py:12-20`)
- If `hasattr(np, "bitwise_count")`, returns `np.bitwise_count(a)` (`visioncore/change.py:17-18`).
- Otherwise converts `a = np.ascontiguousarray(a)` and returns `_POP8[a.view(np.uint8)].reshape(a.shape + (a.dtype.itemsize,)).sum(axis=-1, dtype=np.uint8)` (`visioncore/change.py:19-20`), where `_POP8` is the 256-entry `uint8` popcount table (`visioncore/change.py:12`).

### 6.2 `sig_conf(y_plane, s, gw, gh, mortons, sig_dead_q4)` (`visioncore/change.py:67-92`)
- **Anchor**: `# @anchor:vc.s2.sig_conf` (`visioncore/change.py:67`).
- **Inputs**:
  - `y_plane`: 2-D padded luma plane of shape `(gh * s, gw * s)` (`visioncore/change.py:68`).
  - `s, gw, gh`: tile size `s` (px) and grid dimensions `gw, gh` (tiles).
  - `mortons`: 1-D array of `N` Morton codes (`visioncore/change.py:69,74`).
  - `sig_dead_q4`: integer dead-band threshold in `q4` grey units (`visioncore/change.py:67,73`).
- **Exact Integer Algorithm**:
  1. `tx, ty = _morton_xy(np.asarray(mortons))` (`visioncore/change.py:74`).
  2. If `sig_dead_q4 <= 0`: returns `np.full(len(tx), _ALL64, np.uint64)` (`0xFFFFFFFFFFFFFFFF` for every tile, `visioncore/change.py:75-76`).
  3. `b = s // 8` and `n = s * s` (`visioncore/change.py:77-78`).
  4. `y = np.asarray(y_plane)` (`visioncore/change.py:79`).
  5. Horizontal `b`-pixel strip sums `c` (`np.int32`, shape `(gh * s, gw * 8)`):
     `c = y[:, 0::b].astype(np.int32)`; for `k` in `range(1, b)`: `c += y[:, k::b]` (`visioncore/change.py:80-82`).
  6. Sub-block sums `sub` (`np.int32`, shape `(gh * 8, gw * 8)`):
     `sub = c[0::b]`; for `k` in `range(1, b)`: `sub = sub + c[k::b]` (`visioncore/change.py:83-85`).
  7. Reshape and sum over the `8 x 8` sub-blocks per tile:
     `sub = sub.reshape(gh, 8, gw, 8)` (`visioncore/change.py:86`)
     `tot = sub.sum(axis=(1, 3), dtype=np.int32)` (shape `(gh, gw)`, `visioncore/change.py:87`).
  8. Precision widening for `s > 64`:
     `if s > 64: sub, tot = sub.astype(np.int64), tot.astype(np.int64)` (`visioncore/change.py:88-89`). (For `s <= 64`, `sub` and `tot` remain `np.int32`.)
  9. Scaled sub-block deviation from tile mean:
     `dev = sub * n - tot[:, None, :, None] * (b * b)` (`visioncore/change.py:90`).
  10. Threshold comparison and Morton indexing:
      `bits = (16 * np.abs(dev) > sig_dead_q4 * n * b * b).transpose(0, 2, 1, 3)[ty, tx].reshape(len(tx), 64)` (`visioncore/change.py:91`).
      Here `.transpose(0, 2, 1, 3)` permutes axes `(gh, 8_y, gw, 8_x)` to `(gh, gw, 8_y, 8_x)`, `[ty, tx]` selects tile `i` at `(ty[i], tx[i])`, and `.reshape(len(tx), 64)` flattens the `8 x 8` sub-block grid in row-major order `k = 8 * iy + ix` (`0 <= iy < 8`, `0 <= ix < 8`).
  11. Little-endian 64-bit packing:
      `return np.packbits(bits, axis=1, bitorder="little").view("<u8")[:, 0].astype(np.uint64)` (`visioncore/change.py:92`), so sub-block `k` corresponds to bit `(1 << k)` of the returned `uint64`.

### 6.3 `_conf_of(y_cur, grid, mortons, sig_dead_q4)` (`visioncore/change.py:95-103`)
- If `y_cur is None`: returns `np.full(len(mortons), _ALL64, np.uint64)` (`visioncore/change.py:97-98`).
- If `grid is None`: computes `tx, ty = _morton_xy(np.asarray(mortons))`, `gw, gh = int(tx.max()) + 1, int(ty.max()) + 1`, and sets `grid = (np.asarray(y_cur).shape[1] // gw, gw, gh)` (`visioncore/change.py:99-102`).
- Returns `sig_conf(y_cur, grid[0], grid[1], grid[2], mortons, sig_dead_q4)` (`visioncore/change.py:103`).

### 6.4 `residual(a, b, h_dead=None, mask=None)` (`visioncore/change.py:54-61`)
- **Anchor**: `# @anchor:vc.s2.residual` (`visioncore/change.py:54`).
- **Inputs**:
  - `a, b`: structured tile arrays of length `N`.
  - `h_dead`: `hd = P.PARAMS["H_DEAD"] if h_dead is None else h_dead` (`visioncore/change.py:57`).
  - `mask`: optional `uint64[N]` bitmask (`None` means all 64 bits count, `visioncore/change.py:56,60`).
- **Exact Formula (`np.int64[N]` output in `q4` units)**:
  - `d = lambda f: np.abs(a[f].astype(np.int64) - b[f].astype(np.int64))` (`visioncore/change.py:58`)
  - `x = a["sig64"] ^ b["sig64"]` (`visioncore/change.py:59`)
  - `ham = _popcount(x if mask is None else x & mask).astype(np.int64)` (`visioncore/change.py:60`)
  - `r = d("y_q4") + W_CHROMA * (d("co_h") + d("cg_h")) + d("sig_q4") + W_HAM * np.maximum(0, ham - hd)` (`visioncore/change.py:61`)
    with `W_CHROMA = 8` (`visioncore/change.py:29`) and `W_HAM = 8` (`visioncore/change.py:30`).
- **Comparator Masks Used in `classify_tiles`**:
  - `r1` (vs `state.prev1`): `mask = cf | state.cf1`, followed by `r1[words_eq] = 0` (`visioncore/change.py:289-290`).
  - `r2` (vs `state.prev2`): `mask = cf | state.cf2` (not zeroed by `words_eq`, `visioncore/change.py:307`).
  - `rr_all` (vs `state.ref`): `mask = cf | state.cf_ref` (not zeroed by `words_eq`, `visioncore/change.py:394`).

### 6.5 Output `out["resid"]` Encoding (`visioncore/change.py:414-420`)
- `r = np.where(drift, rr_all, r1)` (`visioncore/change.py:417`): selects `rr_all` (residual vs `state.ref`) for tiles classified as `C_DRIFT`, and `r1` (residual vs `state.prev1`) for all other tiles.
- `out["resid"] = np.minimum(255, r >> 2).astype(np.uint8)` (`visioncore/change.py:418`): arithmetic right shift by 2 (`r // 4`, display units, saturating at `255` which corresponds to `1020` in `q4`).
- `unch_like = (cls == C_UNCHANGED)`; `out["resid"][unch_like] = 0` (`visioncore/change.py:419-420`).

## 7. Per-Tile State (`ClassState`) and Frame-0 Initialization (`classify_first`)

### 7.1 `ClassState` Structure (`visioncore/change.py:113-142`)
- `ClassState()` (`visioncore/change.py:116-118`) sets `self.n = 0` and `self.frame = -1` (per-tile arrays are not allocated until `ClassState.init(tiles)`).
- `ClassState.init(tiles)` (`visioncore/change.py:120-136`) converts `t = _as_tiles(tiles)` and initializes all state fields for `N = len(t)` tiles:

| Field | Dtype / Type | Shape | `ClassState.init(tiles)` Value (`visioncore/change.py:124-135`) | Read in `classify_tiles` | Written in `classify_tiles` | Order in `digest_bytes()` (`visioncore/change.py:139-141`) |
|---|---|---|---|---|---|---|
| `n` | `int` | scalar | `len(t)` (`visioncore/change.py:124`) | `visioncore/change.py:273` | Not modified | Excluded |
| `frame` | `int` | scalar | `0` (`visioncore/change.py:125`) | Incremented (`visioncore/change.py:286`) | `state.frame += 1` (`visioncore/change.py:286`) | Excluded |
| `hot` | `np.uint8` | `(N,)` | `np.zeros(st.n, np.uint8)` (`visioncore/change.py:126`) | `visioncore/change.py:291` | `active.astype(np.uint8)` (`visioncore/change.py:422`) | 1 (`1 * N` B) |
| `hist16` | `np.uint16` | `(N,)` | `np.zeros(st.n, np.uint16)` (`visioncore/change.py:127`) | `visioncore/change.py:293` (`hist_before`) | `((hist_before << 1) \| active).astype(np.uint16)` (`visioncore/change.py:302,423`) | 2 (`2 * N` B) |
| `still_run` | `np.uint8` | `(N,)` | `np.zeros(st.n, np.uint8)` (`visioncore/change.py:128`) | `visioncore/change.py:300,377` (updated at line 301 before read at line 377) | `0` if `active`, `1` if `trans`, else `min(still_run + 1, 255)` (`visioncore/change.py:300-301`) | 3 (`1 * N` B) |
| `pre_act` | `np.uint8` | `(N,)` | `np.zeros(st.n, np.uint8)` (`visioncore/change.py:129`) | `visioncore/change.py:298,377,390` (updated at line 298 before reads at lines 377, 390) | `_popcount(hist_before)` on `trans`, else unchanged (`visioncore/change.py:298`) | 4 (`1 * N` B) |
| `ref_max` | `np.uint16` | `(N,)` | `np.zeros(st.n, np.uint16)` (`visioncore/change.py:130`) | `visioncore/change.py:395-396` | `min(rr_all, 65535)` on `grew`; reset to `0` on `upd` (`visioncore/change.py:396,426,429`) | 5 (`2 * N` B) |
| `stall` | `np.uint8` | `(N,)` | `np.zeros(st.n, np.uint8)` (`visioncore/change.py:131`) | `visioncore/change.py:397` | `0` on `grew`, `min(stall + 1, 255)` on `cand & ~grew`; reset to `0` on `upd` (`visioncore/change.py:397,426,430`) | 6 (`1 * N` B) |
| `ref` | `TILE_DT` | `(N,)` | `t.copy()` (`visioncore/change.py:132`) | `visioncore/change.py:394` | `state.ref[upd] = t[upd]` (`visioncore/change.py:427`) | 7 (`32 * N` B) |
| `prev1` | `TILE_DT` | `(N,)` | `t.copy()` (`visioncore/change.py:132`) | `visioncore/change.py:289,411,431` | `t.copy()` (`visioncore/change.py:432`) | 8 (`32 * N` B) |
| `prev2` | `TILE_DT` | `(N,)` | `t.copy()` (`visioncore/change.py:132`) | `visioncore/change.py:307` | `state.prev1` (`visioncore/change.py:431`) | 9 (`32 * N` B) |
| `mvh16` | `np.uint16` | `(N,)` | `np.zeros(st.n, np.uint16)` (`visioncore/change.py:131`) | `visioncore/change.py:299,424` | `((state.mvh16 << 1) \| ((cls == C_MOVED) \| (mexp & active))).astype(np.uint16)` (`visioncore/change.py:424`) | 10 (`2 * N` B) |
| `pre_mov` | `np.uint8` | `(N,)` | `np.zeros(st.n, np.uint8)` (`visioncore/change.py:131`) | `visioncore/change.py:299,390,398` (updated at line 299 before reads at lines 390, 398) | `_popcount(state.mvh16)` on `trans`, else unchanged (`visioncore/change.py:299`) | 11 (`1 * N` B) |
| `cf_ref` | `np.uint64` | `(N,)` | `np.full(st.n, _ALL64, np.uint64)` (`visioncore/change.py:133`) | `visioncore/change.py:394` | `state.cf_ref[upd] = cf[upd]` (`visioncore/change.py:428`) | 12 (`8 * N` B) |
| `cf1` | `np.uint64` | `(N,)` | `st.cf_ref.copy()` (`visioncore/change.py:134`) | `visioncore/change.py:289,433` | `cf` (`visioncore/change.py:433`) | 13 (`8 * N` B) |
| `cf2` | `np.uint64` | `(N,)` | `st.cf_ref.copy()` (`visioncore/change.py:134`) | `visioncore/change.py:307` | `state.cf1` (`visioncore/change.py:433`) | 14 (`8 * N` B) |
| `y_prev` | `ndarray` or `None` | `(gh*s, gw*s)` or `None` | `None` (`visioncore/change.py:135`) | `visioncore/change.py:312,317,324,332,340,341,357,383,386,387,405,408` | `y_cur` (written only when `y_cur is not None`, `visioncore/change.py:434-435`) | Excluded |

- **`digest_bytes(self)` (`visioncore/change.py:138-141`)**: returns `b"".join(np.ascontiguousarray(a).tobytes() for a in parts)` over the 14 arrays in the table order above (`131 * N` bytes total).

### 7.2 `classify_first(state, tiles, y_cur=None)` (`visioncore/change.py:443-455`)
- **Anchor**: `# @anchor:vc.s2.classify_first` (`visioncore/change.py:443`).
- **Execution**:
  1. `out = _as_tiles(tiles)` (`visioncore/change.py:445`).
  2. Sets `out["cls"] = C_UNCHANGED` (`0`), `out["resid"] = 0`, `out["mv_dx"] = 0`, `out["mv_dy"] = 0` (`visioncore/change.py:446-449`).
  3. If `y_cur is not None` (`visioncore/change.py:450-453`):
     - `state.y_prev = y_cur` (`visioncore/change.py:451`)
     - `cf = _conf_of(y_cur, None, out["morton"], P.PARAMS["SIG_DEAD_Q4"])` (`visioncore/change.py:452`)
     - `state.cf_ref, state.cf1, state.cf2 = cf, cf.copy(), cf.copy()` (`visioncore/change.py:453`)
  4. `n = state.n` (`visioncore/change.py:454`).
  5. Returns `(out, state, {"first": True, "n": [n, 0, 0, 0, 0, 0, 0], "cov_ppm": 0, "global_mv": None})` (`visioncore/change.py:455`).

## 8. Motion Search Ranges, Projections, Islands, and Shifted-Window Integer Arithmetic

### 8.1 Padded Luma Plane `luma_padded(rgb, s, gw, gh)` (`visioncore/change.py:144-154`)
- Casts RGB channels to `np.int16`: `r = rgb[..., 0].astype(np.int16)`, `g = rgb[..., 1].astype(np.int16)`, `b = rgb[..., 2].astype(np.int16)` (`visioncore/change.py:146-148`).
- Lossless YCoCg-R lifting luma in `int16`:
  - `co = r - b` (`visioncore/change.py:149`)
  - `t = b + (co >> 1)` (`visioncore/change.py:150`)
  - `cg = g - t` (`visioncore/change.py:151`)
  - `y = t + (cg >> 1)` (`visioncore/change.py:152`)
- Pads `y` of shape `(H, W)` to `(gh * s, gw * s)` by edge replication:
  `np.pad(y, ((0, gh * s - H), (0, gw * s - W)), mode="edge")` (`visioncore/change.py:153-154`).

### 8.2 Morton Coordinate Decoding `_morton_xy(m)` (`visioncore/change.py:193-201`)
- Casts `m = m.astype(np.int64)` (`visioncore/change.py:194`).
- Helper `comp(v)` (`visioncore/change.py:196-200`, matching `layout._compact` at `visioncore/layout.py:41-46`):
  - `v = v & 0x5555`
  - `v = (v | (v >> 1)) & 0x3333`
  - `v = (v | (v >> 2)) & 0x0F0F`
  - `return (v | (v >> 4)) & 0x00FF`
- Returns `(tx, ty) = (comp(m), comp(m >> 1))` (`visioncore/change.py:201`).

### 8.3 1-D Projection Shift Search `_axis_shift(cur, prev, dmax)` (`visioncore/change.py:157-176`)
- `n = len(cur)`; `best, cost0 = None, None` (`visioncore/change.py:159-160`).
- Loops `for d in range(-dmax, dmax + 1):` (`visioncore/change.py:161`):
  - If `abs(d) >= n`: `continue` (`visioncore/change.py:162-163`).
  - If `d >= 0`: `sad = int(np.abs(cur[d:] - prev[:n - d]).sum())` (`visioncore/change.py:164-165`).
  - Else (`d < 0`): `sad = int(np.abs(cur[:n + d] - prev[-d:]).sum())` (`visioncore/change.py:166-167`).
  - Overlap-normalized cost: `cost = sad * n // (n - abs(d))` (`visioncore/change.py:168`).
  - If `d == 0`: `cost0 = cost` (`visioncore/change.py:169-170`).
  - Candidate key: `key = (cost, abs(d), d)` (`visioncore/change.py:171`).
  - Update best: `if best is None or key < best: best = key` (`visioncore/change.py:172-173`).
- Winner validation (`visioncore/change.py:174-176`):
  - `if best is None or best[2] == 0 or not 3 * best[0] < cost0: return 0`
  - Otherwise returns `best[2]` (`d`).
  - Note: the multiplier `3` in `3 * best[0] < cost0` is a hard-coded literal `3` (`visioncore/change.py:174`), independent of `p["MV_SAD_RATIO_DEN"]`.

### 8.4 Global and Island Motion Search Ranges (`global_vector`, `_islands`, `island_vector`)
- **`global_vector(y_cur, y_prev, s)` (`# @anchor:vc.s2.motion`, `visioncore/change.py:179-186`)**:
  - Search range per axis: `dmax = min(s, MOTION_D_MAX)` where `MOTION_D_MAX = 64` (`visioncore/change.py:33,182`), so `d` ranges over `[-min(s, 64), +min(s, 64)]` subject to `abs(d) < len(cur)`.
  - Casts `c = y_cur.astype(np.int64)` and `p = y_prev.astype(np.int64)` (`visioncore/change.py:183-184`).
  - Horizontal shift `gdx = _axis_shift(c.sum(axis=0), p.sum(axis=0), dmax)` (column sums of length `Ww`, `visioncore/change.py:185`).
  - Vertical shift `gdy = _axis_shift(c.sum(axis=1), p.sum(axis=1), dmax)` (row sums of length `Hh`, `visioncore/change.py:186`).
  - Returns `(gdx, gdy)`.
- **`_islands(mask)` (`visioncore/change.py:236-254`)**:
  - Labels 8-connected components of boolean `(gh, gw)` array `mask` into `lab = np.zeros((gh, gw), np.int32)` (`visioncore/change.py:238-240`).
  - Scans `for y0, x0 in zip(*np.nonzero(mask)):` (row-major raster order: ascending `y0`, then ascending `x0`, `visioncore/change.py:241`).
  - For each unlabeled seed `(y0, x0)`, increments `n += 1`, sets `lab[y0, x0] = n`, and expands via DFS stack over 8-neighbors `yy in range(max(0, y - 1), min(gh, y + 2))` and `xx in range(max(0, x - 1), min(gw, x + 2))` (`visioncore/change.py:244-253`).
  - Returns `(lab, n)` (`visioncore/change.py:254`).
- **`island_vector(lab_mask, y_cur, y_prev, s)` (`# @anchor:vc.s2.island_vector`, `visioncore/change.py:257-263`)**:
  - Extracts island tile coordinates `ys, xs = np.nonzero(lab_mask)` and plane dimensions `Hh, Ww = y_cur.shape` (`visioncore/change.py:259-260`).
  - Computes pixel bounding box expanded by 1 tile (`s` px) on each side and clipped to `[0, Hh]` and `[0, Ww]` (`visioncore/change.py:261-262`):
    - `r0, r1 = max(0, (int(ys.min()) - 1) * s), min(Hh, (int(ys.max()) + 2) * s)`
    - `c0, c1 = max(0, (int(xs.min()) - 1) * s), min(Ww, (int(xs.max()) + 2) * s)`
  - Returns `global_vector(y_cur[r0:r1, c0:c1], y_prev[r0:r1, c0:c1], s)` (`visioncore/change.py:263`). The search range is `d in [-min(s, 64), +min(s, 64)]` with `abs(d) < (c1 - c0)` horizontally and `abs(d) < (r1 - r0)` vertically.

### 8.5 Per-Tile Shifted-Window SAD, Validity, and Clipping (`_tile_sums`, `_tile_sad`, `_tile_sad_clip`)
- **`_tile_sums(absdiff, s, gh, gw)` (`visioncore/change.py:189-190`)**:
  - Returns `absdiff.reshape(gh, s, gw, s).sum(axis=(1, 3))` of shape `(gh, gw)`.
- **`_tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty)` (`visioncore/change.py:204-219`)**:
  - `dx, dy = c`; `Hh, Ww = y_cur.shape` (`visioncore/change.py:206-207`).
  - Allocates `sh = np.zeros((Hh, Ww), np.int64)` and `ok = np.zeros((Hh, Ww), bool)` (`visioncore/change.py:208-209`).
  - Destination pixel bounds in `y_cur` whose source `(x - dx, y - dy)` lies inside `[0, Ww) x [0, Hh)`:
    - `ys0, ys1 = max(0, dy), min(Hh, Hh + dy)` (`visioncore/change.py:210`)
    - `xs0, xs1 = max(0, dx), min(Ww, Ww + dx)` (`visioncore/change.py:211`)
  - If `ys0 < ys1 and xs0 < xs1` (`visioncore/change.py:212-216`):
    - `cur = y_cur[ys0:ys1, xs0:xs1].astype(np.int64)`
    - `src = y_prev[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx].astype(np.int64)`
    - `sh[ys0:ys1, xs0:xs1] = np.abs(cur - src)`
    - `ok[ys0:ys1, xs0:xs1] = True`
  - `sad = _tile_sums(sh, s, gh, gw)` (`np.int64`, shape `(gh, gw)`, `visioncore/change.py:217`).
  - `valid = _tile_sums(ok.astype(np.int64), s, gh, gw) == s * s` (`bool`, shape `(gh, gw)`, `visioncore/change.py:218`): `True` for tile `(ty, tx)` iff all `s * s` pixels in `[ty*s:(ty+1)*s, tx*s:(tx+1)*s]` are in-bounds after shifting by `(-dy, -dx)`.
  - Returns `sad[ty, tx], valid[ty, tx]` (both 1-D arrays of length `N`, `visioncore/change.py:219`).
- **`_tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty)` (`visioncore/change.py:222-233`)**:
  - `dx, dy = c`; `Hh, Ww = y_cur.shape` (`visioncore/change.py:224-225`).
  - Allocates `sh = np.zeros((Hh, Ww), np.int64)` and `ok = np.zeros((Hh, Ww), np.int64)` (`visioncore/change.py:226-227`).
  - `ys0, ys1 = max(0, dy), min(Hh, Hh + dy)` and `xs0, xs1 = max(0, dx), min(Ww, Ww + dx)` (`visioncore/change.py:228-229`).
  - If `ys0 < ys1 and xs0 < xs1` (`visioncore/change.py:230-232`):
    - `sh[ys0:ys1, xs0:xs1] = np.abs(y_cur[ys0:ys1, xs0:xs1].astype(np.int64) - y_prev[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx].astype(np.int64))`
    - `ok[ys0:ys1, xs0:xs1] = 1`
  - Returns `_tile_sums(sh, s, gh, gw)[ty, tx], _tile_sums(ok, s, gh, gw)[ty, tx]` (`visioncore/change.py:233`), yielding per-tile clipped SAD `sgc` (`int64[N]`) and in-bounds pixel count `nvc` (`int64[N]`, range `0..s*s`).

## 9. Exact Rule Evaluation Order in `classify_tiles`

`classify_tiles(state, tiles, words_eq, y_cur=None, grid=None, params=None)` (`# @anchor:vc.s2.classify`, `visioncore/change.py:266-440`) executes a single deterministic step for frame `t >= 1` in the following exact sequence:

### Step 1: Input Coercion, Length Check, and Output/Summary Initialization (`visioncore/change.py:272-286`)
1. `t = _as_tiles(tiles)` and `n = state.n` (`visioncore/change.py:272-273`).
2. If `len(t) != n or len(words_eq) != n`: raises `VqaError("E_VC_GRID_MISMATCH", {"state_n": n, "tiles_n": len(t), "words_eq_n": len(words_eq)})` (`visioncore/change.py:274-275`).
3. `words_eq = np.asarray(words_eq, bool)` (`visioncore/change.py:276`).
4. `p = P.PARAMS if params is None else params` and `t_enter, t_exit, drift_u = thresholds(p)` (`visioncore/change.py:277-278`).
5. `out = t.copy()`; `out["cls"] = C_UNCHANGED`; `out["resid"] = 0`; `out["mv_dx"] = 0`; `out["mv_dy"] = 0`; `out["flags"] = t["flags"] & ~np.uint8(FLAG_GLOBAL_MV | FLAG_SHADE_ONLY)` (`visioncore/change.py:279-284`).
6. `summary = {"first": False, "n": [0] * 7, "cov_ppm": 0, "global_mv": None}` (`visioncore/change.py:285`).
7. `state.frame += 1` (`visioncore/change.py:286`).

### Step 2: Residual `r1` vs `prev1` and Hysteresis Activity (`active`) (`visioncore/change.py:288-295`)
1. `cf = _conf_of(y_cur, grid, t["morton"], p["SIG_DEAD_Q4"])` (`visioncore/change.py:288`).
2. `r1 = residual(t, state.prev1, p["H_DEAD"], cf | state.cf1)` (`visioncore/change.py:289`).
3. `r1[words_eq] = 0` (`visioncore/change.py:290`).
4. `hot = state.hot.astype(bool)` (`visioncore/change.py:291`).
5. `active = np.where(hot, r1 > t_exit, r1 > t_enter) & ~words_eq` (`visioncore/change.py:292`).
6. `hist_before = state.hist16` and `prev_active = (hist_before & 1).astype(bool)` (`visioncore/change.py:293-294`).

### Step 3: Stillness Bookkeeping (`pre_act`, `pre_mov`, `still_run`, `hist`) (`visioncore/change.py:296-302`)
1. `trans = ~active & prev_active` (`visioncore/change.py:297`).
2. `state.pre_act = np.where(trans, _popcount(hist_before), state.pre_act).astype(np.uint8)` (`visioncore/change.py:298`).
3. `state.pre_mov = np.where(trans, _popcount(state.mvh16), state.pre_mov).astype(np.uint8)` (`visioncore/change.py:299`).
4. `run = np.where(trans, 1, np.minimum(state.still_run.astype(np.int64) + 1, 255))` (`visioncore/change.py:300`).
5. `state.still_run = np.where(active, 0, run).astype(np.uint8)` (`visioncore/change.py:301`).
6. `hist = ((hist_before << np.uint16(1)) | active.astype(np.uint16)).astype(np.uint16)` (`visioncore/change.py:302`).

### Step 4: Base Classes `UNCHANGED` (`0`) and `JITTER` (`1`) (`visioncore/change.py:303-304`)
1. `cls = np.zeros(n, np.uint8)` — initializes all tiles to `C_UNCHANGED` (`0`) (`visioncore/change.py:303`).
2. `cls[~words_eq & ~active] = C_JITTER` (`1`) (`visioncore/change.py:304`).
   - Tiles with `words_eq == True` have `active == False` and remain `C_UNCHANGED` (`0`) at this stage (they can still be overwritten by `C_FROZEN` at line 391 or `C_DRIFT` at line 400 if those non-active post-rules fire).

### Step 5: Initial `FLICKER` (`4`) A-B-A Detection (`visioncore/change.py:306-308`)
1. `r2 = residual(t, state.prev2, p["H_DEAD"], cf | state.cf2)` (`visioncore/change.py:307`).
2. `flicker = active & (r2 <= t_exit) & (r1 > t_enter)` (`visioncore/change.py:308`).
   - Requires descriptor-level A-B-A return: tile is `active`, its residual `r1` against `prev1` is strictly above `t_enter` (`r1 > t_enter`), and its residual `r2` against `prev2` is at or below `t_exit` (`r2 <= t_exit`).

### Step 6: `MOVED` (`3`) Candidate Evaluation (Global Vector, Flicker Overrides, and Island Vectors) (`visioncore/change.py:310-368`)
1. Initializes `moved, mexp = np.zeros(n, bool), np.zeros(n, bool)` (`visioncore/change.py:311`).
2. Enters motion block iff `y_cur is not None and state.y_prev is not None and grid is not None and active.any()` (`visioncore/change.py:312`):
   - Unpacks `s, gw, gh = grid`, `tx, ty = _morton_xy(t["morton"])`, `nn = s * s`, `den = p["MV_SAD_RATIO_DEN"]` (`visioncore/change.py:313-316`).
   - Computes zero-shift SAD `sad_0, _ = _tile_sad(y_cur, state.y_prev, (0, 0), s, gh, gw, tx, ty)` (`visioncore/change.py:317`).
   - Initializes `best = np.full(n, np.iinfo(np.int64).max, np.int64)`, `mvx = np.zeros(n, np.int64)`, `mvy = np.zeros(n, np.int64)`, `is_glob = np.zeros(n, bool)` (`visioncore/change.py:318-321`).
   - Defines `offer(vec, member, glob)` (`visioncore/change.py:323-329`):
     - `sad_c, ok_c = _tile_sad(y_cur, state.y_prev, vec, s, gh, gw, tx, ty)` (`visioncore/change.py:324`)
     - `expl = member & ok_c & (den * sad_c < sad_0) & (16 * sad_c <= nn * t_exit) & (sad_c < best)` (`visioncore/change.py:325`)
     - Updates `best[expl] = sad_c[expl]`, `mvx[expl], mvy[expl] = vec`, `is_glob[expl] = glob`, and returns `expl` (`visioncore/change.py:326-329`).
   - **Candidate 1 — Global vector `gv`** (`visioncore/change.py:331-348`):
     - `cand_base = active & ~flicker` (`visioncore/change.py:331`).
     - `gv = global_vector(y_cur, state.y_prev, s)` and `published = None` (`visioncore/change.py:332-333`).
     - If `gv != (0, 0)` (`visioncore/change.py:334`):
       - `expl_g = offer(gv, cand_base, True)` (`visioncore/change.py:335`).
       - Publication test: `if int(expl_g.sum()) * 1000000 >= p["GLOBAL_SHARE_PPM"] * int(active.sum()):` (`visioncore/change.py:336`).
       - If published (`visioncore/change.py:337-343`):
         1. `summary["global_mv"] = [gv[0], gv[1]]` and `published = gv` (`visioncore/change.py:337-338`).
         2. Interior A-B-A flicker override to `MOVED`: `flicker &= ~offer(gv, flicker, True)` (`visioncore/change.py:339`).
         3. Edge-tile clipped-window flicker suppression to `CHANGED`:
            - `sgc, nvc = _tile_sad_clip(y_cur, state.y_prev, gv, s, gh, gw, tx, ty)` (`visioncore/change.py:340`)
            - `s0c, _ = _tile_sad_clip(y_cur, state.y_prev, (0, 0), s, gh, gw, tx, ty)` (`visioncore/change.py:341`)
            - `mexp = (nvc < nn) & (2 * nvc >= nn) & (den * sgc * nn < s0c * nvc) & (16 * sgc <= nvc * t_exit)` (`visioncore/change.py:342`)
            - `flicker &= ~mexp` (`visioncore/change.py:343`).
       - Else (not published, `visioncore/change.py:344-348`): withdraws `gv` by resetting `best[:] = np.iinfo(np.int64).max`, `is_glob[:] = False`, `mvx[:] = 0`, `mvy[:] = 0`.
   - **Candidate 2 — Island vectors** (`visioncore/change.py:349-364`):
     - Marks `amask = np.zeros((gh, gw), bool)`; `amask[ty[cand_base], tx[cand_base]] = True` (using `cand_base` from line 331, `visioncore/change.py:349-350`).
     - `lab, nl = _islands(amask)` (`visioncore/change.py:351`).
     - If `nl > 0` (`visioncore/change.py:352`):
       - `sizes = np.bincount(lab.ravel(), minlength=nl + 1)[1:]` (`visioncore/change.py:353`).
       - `order = sorted(range(1, nl + 1), key=lambda k: (-int(sizes[k - 1]), k))[:ISLAND_MAX]` (`visioncore/change.py:354`).
       - For each `k` in `order`: computes `v = island_vector(lab == k, y_cur, state.y_prev, s)`; if `v != (0, 0) and v != published`, appends `k` to `by_vec.setdefault(v, [])` (`visioncore/change.py:355-359`).
       - `tile_lab = lab[ty, tx]` (`visioncore/change.py:360`).
       - Pass 1 (own-island tiles): `for v in sorted(by_vec): offer(v, np.isin(tile_lab, by_vec[v]) & cand_base, False)` (`visioncore/change.py:361-362`).
       - Pass 2 (all `cand_base` tiles): `for v in sorted(by_vec): offer(v, cand_base, False)` (`visioncore/change.py:363-364`).
   - **Finalizing `MOVED` outputs** (`visioncore/change.py:365-368`):
     - `moved = best != np.iinfo(np.int64).max` (`visioncore/change.py:365`)
     - `out["mv_dx"][moved] = mvx[moved]` (`visioncore/change.py:366`)
     - `out["mv_dy"][moved] = mvy[moved]` (`visioncore/change.py:367`)
     - `out["flags"][moved & is_glob] |= np.uint8(FLAG_GLOBAL_MV)` (`visioncore/change.py:368`).

### Step 7: Active Class Assignment (`CHANGED`, `MOVED`, `FLICKER`) (`visioncore/change.py:369-371`)
1. `cls[active] = C_CHANGED` (`2`) (`visioncore/change.py:369`).
2. `cls[moved] = C_MOVED` (`3`) (`visioncore/change.py:370`).
3. `cls[flicker] = C_FLICKER` (`4`) (`visioncore/change.py:371`).

### Step 8: Non-Active Post-Rule 1 — `FROZEN` (`5`) and Aperture Rule (`visioncore/change.py:373-391`)
1. `n_active = int(active.sum())` (`visioncore/change.py:374`).
2. `alive = summary["global_mv"] is not None or n_active * 1000000 // n >= p["FROZEN_COV_PPM"]` (`visioncore/change.py:375`).
3. If `alive`: `frozen = ~active & (state.still_run >= p["FROZEN_M"]) & (state.pre_act >= FROZEN_PRE_ACT)` (`visioncore/change.py:376-377`); else `frozen = np.zeros(n, bool)` (`visioncore/change.py:378-379`).
4. Aperture ambiguity masks `amb` and `amb_lt` (`visioncore/change.py:382-389`):
   - Initialized `amb = amb_lt = np.zeros(n, bool)` (`visioncore/change.py:382`).
   - If `summary["global_mv"] is not None and y_cur is not None and state.y_prev is not None and grid is not None` (`visioncore/change.py:383`):
     - `s_, gw_, gh_ = grid`; `tx_, ty_ = _morton_xy(t["morton"])` (`visioncore/change.py:384-385`)
     - `s0, _ = _tile_sad(y_cur, state.y_prev, (0, 0), s_, gh_, gw_, tx_, ty_)` (`visioncore/change.py:386`)
     - `sg, nv = _tile_sad_clip(y_cur, state.y_prev, tuple(summary["global_mv"]), s_, gh_, gw_, tx_, ty_)` (`visioncore/change.py:387`)
     - `amb = ~active & (2 * nv >= s_ * s_) & (sg * (s_ * s_) <= s0 * nv)` (`visioncore/change.py:388`)
     - `amb_lt = amb & (sg * (s_ * s_) < s0 * nv)` (`visioncore/change.py:389`).
5. Aperture suppression on `frozen`:
   `frozen &= ~(amb & (state.pre_act.astype(np.int64) - state.pre_mov < FROZEN_PRE_ACT))` (`visioncore/change.py:390`).
6. `cls[frozen] = C_FROZEN` (`5`) (`visioncore/change.py:391`).

### Step 9: Non-Active Post-Rule 2 — `DRIFT` (`6`) Latch (`visioncore/change.py:392-401`)
1. `cand = ~active & ~frozen` (`visioncore/change.py:393`).
2. `rr_all = residual(t, state.ref, p["H_DEAD"], cf | state.cf_ref)` (`visioncore/change.py:394`).
3. `grew = cand & (rr_all > state.ref_max)` (`visioncore/change.py:395`).
4. `ref_max = np.where(grew, np.minimum(rr_all, 65535), state.ref_max).astype(np.uint16)` (`visioncore/change.py:396`).
5. `stall = np.where(grew, 0, np.where(cand, np.minimum(state.stall.astype(np.int64) + 1, 255), state.stall)).astype(np.uint8)` (`visioncore/change.py:397`).
6. `drift = cand & (rr_all > drift_u) & (stall < p["DRIFT_STALL"]) & ~(amb_lt | (amb & (state.pre_mov > 0)))` (`visioncore/change.py:398`).
7. `settle = cand & (stall >= p["DRIFT_STALL"])` (`visioncore/change.py:399`).
8. `cls[drift] = C_DRIFT` (`6`) and `out["cls"] = cls` (`visioncore/change.py:400-401`).

### Step 10: `SHADE_ONLY` Flag (`FLAG_SHADE_ONLY`) (`visioncore/change.py:403-413`)
1. `chg = cls == C_CHANGED` (`visioncore/change.py:404`).
2. If `y_cur is not None and state.y_prev is not None and grid is not None and chg.any()` (`visioncore/change.py:405`):
   - `s, gw, gh = grid`; `tx, ty = _morton_xy(t["morton"])` (`visioncore/change.py:406-407`).
   - `dy_ = y_cur.astype(np.int64) - state.y_prev.astype(np.int64)` (`visioncore/change.py:408`).
   - `n_pos = _tile_sums((dy_ > 0).astype(np.int64), s, gh, gw)[ty, tx]` (`visioncore/change.py:409`).
   - `n_neg = _tile_sums((dy_ < 0).astype(np.int64), s, gh, gw)[ty, tx]` (`visioncore/change.py:410`).
   - `d_edge = np.abs(t["edge"].astype(np.int64) - state.prev1["edge"].astype(np.int64))` (`visioncore/change.py:411`).
   - `shade = chg & (np.minimum(n_pos, n_neg) <= (s * s) // SHADE_DEN) & (d_edge <= SHADE_EDGE_MAX)` (`visioncore/change.py:412`).
   - `out["flags"][shade] |= np.uint8(FLAG_SHADE_ONLY)` (`visioncore/change.py:413`).

### Step 11: Output Residual Field, State Commit, and Summary (`visioncore/change.py:414-440`)
1. `r = np.where(drift, rr_all, r1)` (`visioncore/change.py:417`).
2. `out["resid"] = np.minimum(255, r >> 2).astype(np.uint8)` (`visioncore/change.py:418`).
3. `unch_like = (cls == C_UNCHANGED)`; `out["resid"][unch_like] = 0` (`visioncore/change.py:419-420`).
4. `state.hot = active.astype(np.uint8)` (`visioncore/change.py:422`).
5. `state.hist16 = hist` (`visioncore/change.py:423`).
6. `state.mvh16 = ((state.mvh16 << np.uint16(1)) | ((cls == C_MOVED) | (mexp & active)).astype(np.uint16)).astype(np.uint16)` (`visioncore/change.py:424`).
7. `upd = (cls == C_CHANGED) | (cls == C_MOVED) | (cls == C_FLICKER) | settle` (`visioncore/change.py:425`).
8. `state.ref_max, state.stall = ref_max, stall` (`visioncore/change.py:426`).
9. `state.ref[upd] = t[upd]`; `state.cf_ref[upd] = cf[upd]`; `state.ref_max[upd] = 0`; `state.stall[upd] = 0` (`visioncore/change.py:427-430`).
10. `state.prev2 = state.prev1`; `state.prev1 = t.copy()`; `state.cf2, state.cf1 = state.cf1, cf` (`visioncore/change.py:431-433`).
11. `if y_cur is not None: state.y_prev = y_cur` (`visioncore/change.py:434-435`).
12. `counts = np.bincount(cls, minlength=7)`; `summary["n"] = [int(c) for c in counts[:7]]`; `summary["cov_ppm"] = int(counts[2:7].sum()) * 1000000 // n` (`visioncore/change.py:437-439`).
13. Returns `(out, state, summary)` (`visioncore/change.py:440`).

## 10. Tie-Break Rules and Boundary Inequalities

Every comparison and tie-break in `visioncore/change.py` is listed below with its exact operator and tie resolution:

| Mechanism | Expression / Key | Location | Tie-Break / Boundary Behavior |
|---|---|---|---|
| 1-D projection shift selection (`_axis_shift`) | `key = (cost, abs(d), d)` with `if best is None or key < best: best = key` | `visioncore/change.py:171-173` | 1. Lowest `cost = sad * n // (n - abs(d))` wins.<br>2. Equal `cost`: smallest magnitude `abs(d)` wins.<br>3. Equal `cost` and equal `abs(d)` (`-d` vs `+d`): smaller signed `d` (`-d < +d`) wins (negative shift beats positive shift). |
| 1-D projection improvement gate (`_axis_shift`) | `not 3 * best[0] < cost0` | `visioncore/change.py:174-175` | Strict `<`: if `3 * best[0] == cost0` (or `best[2] == 0`), returns `0`. |
| Motion candidate replacement (`offer`) | `sad_c < best` | `visioncore/change.py:325` | Strict `<`: first offered vector achieving the minimum `sad_c` keeps the tile; later vectors with equal `sad_c` do not replace it (`is_glob` and `(mvx, mvy)` stay with the earlier vector). |
| Motion candidate order (`classify_tiles`) | 1. `gv` on `cand_base` (`glob=True`)<br>2. `gv` on `flicker` (`glob=True`, if `gv` published)<br>3. Pass 1: `for v in sorted(by_vec)` on own-island `cand_base` (`glob=False`)<br>4. Pass 2: `for v in sorted(by_vec)` on all `cand_base` (`glob=False`) | `visioncore/change.py:335,339,361-364` | `gv` is offered before island vectors. Distinct island vectors `v != (0, 0)` and `v != published` are offered in ascending lexicographical tuple order `(dx, dy)`, first to tiles in islands that produced `v`, then to all `cand_base` tiles. If an island vector achieves strictly lower `sad_c < best` than `gv`, it overwrites `mvx, mvy` and sets `is_glob = False`. |
| Motion vs zero-shift SAD (`offer`) | `den * sad_c < sad_0` | `visioncore/change.py:325` | Strict `<`: if `den * sad_c == sad_0`, candidate `vec` is rejected. |
| Motion residual ceiling (`offer`) | `16 * sad_c <= nn * t_exit` | `visioncore/change.py:325` | Inclusive `<=`: if `16 * sad_c == nn * t_exit`, candidate `vec` is accepted. |
| Global vector publication (`classify_tiles`) | `int(expl_g.sum()) * 1000000 >= p["GLOBAL_SHARE_PPM"] * int(active.sum())` | `visioncore/change.py:336` | Inclusive `>=`: on exact equality, `gv` is published. |
| Island ranking & cap (`classify_tiles`) | `sorted(range(1, nl + 1), key=lambda k: (-int(sizes[k - 1]), k))[:ISLAND_MAX]` | `visioncore/change.py:241,354` | Largest island tile count `sizes[k - 1]` first; ties broken by smallest island label `k` (earliest first tile in row-major raster scan `(y0, x0)`); capped at `ISLAND_MAX = 16`. |
| Clipped-window flicker suppression (`mexp`) | `(nvc < nn) & (2 * nvc >= nn) & (den * sgc * nn < s0c * nvc) & (16 * sgc <= nvc * t_exit)` | `visioncore/change.py:342` | Requires partial window (`nvc < nn`), at least half valid (`2 * nvc >= nn`, inclusive), strict SAD ratio (`den * sgc * nn < s0c * nvc`), and inclusive residual ceiling (`16 * sgc <= nvc * t_exit`). |
| Hysteresis entry & exit (`classify_tiles`) | `np.where(hot, r1 > t_exit, r1 > t_enter) & ~words_eq` | `visioncore/change.py:292` | Both `r1 > t_exit` and `r1 > t_enter` are strict `>`: on equality (`r1 == t_exit` for hot, `r1 == t_enter` for cold), `active` is `False`. |
| Sub-block confidence (`sig_conf`) | `16 * np.abs(dev) > sig_dead_q4 * n * b * b` | `visioncore/change.py:91` | Strict `>`: on equality, bit `k` is `0` (inside noise dead band). |
| Residual Hamming dead zone (`residual`) | `np.maximum(0, ham - hd)` | `visioncore/change.py:61` | For `ham <= hd`, Hamming term contributes `0`. |
| Initial `FLICKER` A-B-A (`classify_tiles`) | `active & (r2 <= t_exit) & (r1 > t_enter)` | `visioncore/change.py:308` | `r2 <= t_exit` is inclusive `<=`; `r1 > t_enter` is strict `>`. |
| Frame liveness for `FROZEN` (`alive`) | `summary["global_mv"] is not None or n_active * 1000000 // n >= p["FROZEN_COV_PPM"]` | `visioncore/change.py:375` | Uses floor division `// n` then inclusive `>=`. |
| `FROZEN` tile thresholds (`classify_tiles`) | `~active & (state.still_run >= p["FROZEN_M"]) & (state.pre_act >= FROZEN_PRE_ACT)` | `visioncore/change.py:377` | Both `still_run >= FROZEN_M` and `pre_act >= FROZEN_PRE_ACT` are inclusive `>=`. |
| Aperture ambiguity (`amb`, `amb_lt`) | `amb = ~active & (2 * nv >= s_ * s_) & (sg * (s_ * s_) <= s0 * nv)`<br>`amb_lt = amb & (sg * (s_ * s_) < s0 * nv)` | `visioncore/change.py:388-389` | `amb` uses inclusive `<=` (so `sg * s^2 == s0 * nv` is `amb = True`), whereas `amb_lt` requires strict `<`. |
| `FROZEN` vs `DRIFT` precedence (`classify_tiles`) | `cand = ~active & ~frozen` | `visioncore/change.py:391,393,400` | `C_FROZEN` excludes the tile from `cand`, taking strict precedence over `C_DRIFT`. |
| `DRIFT` growth (`grew`), latch (`drift`), and settle (`settle`) | `grew = cand & (rr_all > state.ref_max)`<br>`drift = cand & (rr_all > drift_u) & (stall < p["DRIFT_STALL"]) & ...`<br>`settle = cand & (stall >= p["DRIFT_STALL"])` | `visioncore/change.py:395-399` | `rr_all > state.ref_max` and `rr_all > drift_u` are strict `>`; `stall < p["DRIFT_STALL"]` is strict `<`; `stall >= p["DRIFT_STALL"]` is inclusive `>=`. On `rr_all == state.ref_max`, `grew` is `False` and `stall` increments. |
| `SHADE_ONLY` flag (`classify_tiles`) | `chg & (np.minimum(n_pos, n_neg) <= (s * s) // SHADE_DEN) & (d_edge <= SHADE_EDGE_MAX)` | `visioncore/change.py:412` | Both `<=` comparisons are inclusive; `(s * s) // SHADE_DEN` uses floor division. |

## 11. ABSENT (Not Defined in Bound Source Files)

The following items are referenced or imported by the bound source files (`visioncore/change.py`, `visioncore/params.py`, `visioncore/layout.py`, `AGENTS.md`) but their definitions are `ABSENT` from those bound files:

1. **Stage S1 Descriptor Generation (`visioncore/desc.py`)**: The computation of input tile fields (`y_q4`, `co_h`, `cg_h`, `sig_q4`, `grad_q6`, `edge`, `sig64`, `xh64`), S1-populated `flags` bits (`EDGE`, `TEXTURE`, `SAT`, `UI_OCC`, `ACTOR_OCC`, `PAD` named in `visioncore/layout.py:66`), and `words_eq` (`bool[N]`) is `ABSENT` from the bound source files.
2. **Error Class Implementation (`visioncore/errors.py`)**: `VqaError` is imported from `.errors` (`visioncore/change.py:10`, `visioncore/layout.py:5`, `visioncore/params.py:6`), but `visioncore/errors.py` is `ABSENT` from the bound source files.
3. **Canonical JSON Helper (`visioncore/canon.py`)**: `canon.sha256_obj` is imported and called in `visioncore/params.py:5,86`, but `visioncore/canon.py` is `ABSENT` from the bound source files.
4. **Unused `"CORE"` Registry Parameters (`EMA_F_Q16`, `EPS_Q4`, `DRIFT_C_Q4`, `TAU_EDGE`)**: Registered in `visioncore/params.py:19-21,23` with owner `"CORE"`, but any usage of these four parameters in Stage S2 is `ABSENT` from `visioncore/change.py`.
5. **External Campaign Documents (`SPEC_CORE`, `SPEC_INT`, `SPEC_TIME`, `HB-VC-s2-change`)**: Mentioned in comments and module headers (`visioncore/change.py:1-2,28,34,46,55,68,114,180,258,296,381,392,403`), but the external documents themselves are `ABSENT` from the bound source files.
