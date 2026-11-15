# VQA frame manifest and tile descriptor — draft v1 wire specification

## Status and source boundary

This is a descriptive draft of the behavior implemented by the four bound files below. It does not certify acceptance, introduce new validation rules, or fill gaps from other repository files.

- Repository: `oguirado/vision-engine`.
- Requested source ref: `bridge/integration`.
- Observed source commit: `c7039925c8aa4007605758078105359d71e910fa`.
- All four files were retrieved through the GitHub connector with the ref explicitly supplied. Their exact bytes were also retrieved at the observed commit and matched the ref reads.

| Bound source | Lines in this snapshot | Git blob SHA returned by the connector |
|---|---:|---|
| `visioncore/frame.py` | 113 | `4447aefe4245c03a6c94062395fe346ca96b3d0b` |
| `visioncore/layout.py` | 139 | `170fdcd9294f5fb106e64f6c8b09eec3b8aa8eaf` |
| `visioncore/grid.py` | 53 | `e754ed3462ec980e41c419db0e8e06f576ba6cdd` |
| `visioncore/schemas/vqa.core-frame.v1.schema.json` | 1 | `9524d2cc7bb39740363a682389be5318184d3865` |

Every source citation below refers to this snapshot. **ABSENT** means that the bounded evidence does not establish the stated detail. A field name, an imported value, or a reference to another specification does not supply that missing definition.

A **storage range** is the representable range of a binary field, derived from the source's `struct` format. A **validated range** is a condition actually checked by the bound code. These are distinguished throughout. Offsets are in bytes, relative to the start of the record identified by the table.

Three identifiers describe separate objects:

| Identifier | Established object | Source |
|---|---|---|
| `vqa.frame/v1` | Frame manifest dictionary checked by `validate_manifest` | `visioncore/frame.py:10`, `visioncore/frame.py:61-99` |
| `vqa.tile/v1` | Packed 32-byte tile descriptor | `visioncore/layout.py:91-99` |
| `vqa.core-frame/v1` | Object described as a `frames.jsonl` row by the bound JSON Schema | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |

The core-frame schema is not a schema for the frame manifest. Its fields are documented separately in section 6.

## 1. Frame manifest: `vqa.frame/v1`

### 1.1 Document model and serialization boundary

`validate_manifest(m)` requires a Python dictionary, checks the conditions below, and returns the same dictionary. It does not normalize it or reject unspecified additional properties at the manifest, frame-entry, or scope level. Source: `visioncore/frame.py:61-99`.

The reader opens text with `encoding="utf-8"` and passes it through `canon.loads` before validation. The writer validates first, then delegates to `canon.dumps` and `canon.write_atomic`. Exact serialized key ordering, separators, escaping, canonicalization, output encoding, and final-newline policy are **ABSENT** because the implementations of those imported functions are outside the bound sources. Source: `visioncore/frame.py:102-113`.

No manifest property has a fixed binary offset or byte size defined here. In particular, a numeric bound of `2^64-1` does not establish an eight-byte manifest encoding. The field tables describe the validated object model.

### 1.2 Root fields

All nine root properties below are required by the checks. Each missing property fails through the relevant `get` or validation condition.

| Name | Type implemented by the validator | Byte offset / size | Units or meaning established | Allowed value or range | Exact defining source |
|---|---|---|---|---|---|
| `schema` | String value | ABSENT / ABSENT | Schema identifier | Exactly `"vqa.frame/v1"` | `visioncore/frame.py:10`, `visioncore/frame.py:63-64` |
| `width` | Integer, excluding Boolean | ABSENT / ABSENT | Frame width in pixels; corresponds to `W` | Inclusive `1..8192`, and `grid_params(width,height)` must succeed | `visioncore/frame.py:54-58`, `visioncore/frame.py:65-69`; `visioncore/layout.py:12`, `visioncore/layout.py:15-25`; pixel geometry: `visioncore/grid.py:30-35` |
| `height` | Integer, excluding Boolean | ABSENT / ABSENT | Frame height in pixels; corresponds to `H` | Inclusive `1..8192`, and the same grid check | `visioncore/frame.py:54-58`, `visioncore/frame.py:66-69`; `visioncore/layout.py:12`, `visioncore/layout.py:15-25`; pixel geometry: `visioncore/grid.py:30-35` |
| `channels` | Value comparing equal to integer `3`; independent integer type check ABSENT | ABSENT / ABSENT | Number of channels in the decoded `(H,W,3)` representation | `value == 3`; a JSON numeric value `3.0` passes this equality | `visioncore/frame.py:18`, `visioncore/frame.py:23`, `visioncore/frame.py:70-71` |
| `pixel_format` | String value | ABSENT / ABSENT | Decoded pixel-format identifier | Exactly `"rgb24"` | `visioncore/frame.py:18`, `visioncore/frame.py:70-71` |
| `count` | Integer, excluding Boolean | ABSENT / ABSENT | Number of entries in `frames` | Inclusive `1..4294967295`; must equal the list length | `visioncore/frame.py:14`, `visioncore/frame.py:54-58`, `visioncore/frame.py:72`, `visioncore/frame.py:75-77` |
| `storage` | String enum value | ABSENT / ABSENT | Storage selector; storage layout ABSENT | `"png_dir"` or `"rgb24_raw"` | `visioncore/frame.py:13`, `visioncore/frame.py:73-74` |
| `frames` | List of dictionaries | ABSENT / ABSENT | Ordered frame entries | Exactly `count` entries; each entry obeys section 1.3 | `visioncore/frame.py:75-95` |
| `scope` | Dictionary | ABSENT / ABSENT | Container for `app` and `scene` identifiers; namespace semantics ABSENT | Both fields in section 1.4 are required; additional properties are not rejected | `visioncore/frame.py:96-99` |

The dimension validation occurs in layers: `_int` first checks `1..2^30`, the manifest validator then checks `layout.DIM_MAX`, whose bound definition is `8192`, and finally calls `layout.grid_params`. The draft's `1..8192` range is the combined dimension bound; grid admissibility remains an additional condition. Source: `visioncore/frame.py:65-69`; `visioncore/layout.py:12-25`.

### 1.3 Per-frame fields: `frames[k]`

Here `k` is the zero-based position produced by `enumerate(fr)`.

| Name | Type implemented by the validator | Byte offset / size | Units or meaning established | Allowed value or range | Exact defining source |
|---|---|---|---|---|---|
| `i` | Value comparing equal to `k`; independent integer type check ABSENT | ABSENT / ABSENT | Zero-based position in `frames` | Must equal `k`, with `0 <= k < count` | `visioncore/frame.py:82-86` |
| `t_us` | Integer, excluding Boolean | ABSENT / ABSENT | Explicit time unit, epoch, and origin ABSENT; the source only names it `t_us` | Inclusive `0..18446744073709551615`; nondecreasing across entries; equal adjacent values are allowed | `visioncore/frame.py:54-58`, `visioncore/frame.py:78`, `visioncore/frame.py:87-90` |
| `render_index` | Optional integer, excluding Boolean; null also permitted | ABSENT / ABSENT | Index correspondence and origin ABSENT | If non-null, inclusive `0..4294967295`; non-null on every frame or on no frame | `visioncore/frame.py:14`, `visioncore/frame.py:54-58`, `visioncore/frame.py:79-81`, `visioncore/frame.py:91-92` |
| `sha256` | String | ABSENT / ABSENT | Digest text; decoded-frame hash helper is described in section 1.6 | `_HEX64.match(value)` for `^[0-9a-f]{64}$`; terminal-LF behavior in section 1.5 applies | `visioncore/frame.py:11`, `visioncore/frame.py:93-95`; decoded digest: `visioncore/frame.py:18-27` |
| `file_sha256` | String | ABSENT / ABSENT | Hash input bytes, file selection, and verification rule ABSENT | Same `_HEX64.match` check as `sha256` | `visioncore/frame.py:11`, `visioncore/frame.py:93-95` |

`i`, `t_us`, `sha256`, and `file_sha256` are required. Missing and null `render_index` values are equivalent for these checks. If all entries have a missing or null value, any mixture of those two forms is permitted. No fallback index is inserted. The code does not check uniqueness or ordering of non-null render indices. Source: `visioncore/frame.py:79-95`.

### 1.4 Scope fields

| Name | Type | Byte offset / size | Units or meaning established | Allowed value or range | Exact defining source |
|---|---|---|---|---|---|
| `scope.app` | Required string | ABSENT / ABSENT | Identifier; namespace, interpretation, and units ABSENT | `_SCOPE.match(value)` using `^[a-z0-9_]{1,48}$`; terminal-LF behavior below applies | `visioncore/frame.py:12`, `visioncore/frame.py:96-98` |
| `scope.scene` | Required string | ABSENT / ABSENT | Identifier; namespace, interpretation, and units ABSENT | Same check as `scope.app` | `visioncore/frame.py:12`, `visioncore/frame.py:96-98` |

### 1.5 Exact acceptance details

These are descriptions of the implementation, not recommendations for stricter producers.

- `_int` rejects Booleans and values that are not Python integers. `channels` and `i` use equality instead. Thus `3.0 == 3` passes for `channels`; `0.0` and `false` can match frame index zero, and `true` can match frame index one. Sources: `visioncore/frame.py:54-58`, `visioncore/frame.py:70`, `visioncore/frame.py:85`.
- Both regexes use Python `$` with `match`. That anchor may match immediately before one terminal LF. Consequently, 64 lowercase hex characters followed by one LF also pass the digest check; a valid 1-to-48-character scope identifier followed by one LF also passes. The validator does not strip that LF. Sources: `visioncore/frame.py:11-12`, `visioncore/frame.py:94`, `visioncore/frame.py:97-99`.
- The validator checks digest syntax, but does not open storage, recompute a frame or file digest, or associate an entry with a filename. Source: `visioncore/frame.py:61-99`.
- The bound files do not establish storage paths, PNG encoding settings, raw-file framing, a frame rate, or a required relationship between timestamps and render indices. These details are **ABSENT**.

### 1.6 Decoded frame and aggregate hash helpers

The in-memory `Frame` object has slots `i`, `t_us`, `render_index`, `rgb`, and `sha256`. These slots are not a separately defined wire record and have **ABSENT** byte offsets and fixed sizes. `rgb` must expose a three-dimensional shape whose third dimension is 3, dtype spelling `uint8`, and C-contiguity. This is a decoded `(H,W,3)` RGB buffer with byte-valued components, representable as `0..255`. Source: `visioncore/frame.py:18-24`.

The constructor computes `hashlib.sha256(rgb.tobytes()).hexdigest()`. A supplied, non-null `sha256` must equal that computed digest or it raises `E_VC_FRAME_SHA`. It attempts to make the buffer non-writeable, but ignores `ValueError` from that attempt. It assigns `i`, `t_us`, and `render_index` directly, without the manifest's numeric checks. Source: `visioncore/frame.py:21-32`.

`frames_sha256(frame_shas)` calls `canon.sha256_obj(list(frame_shas))`; its docstring describes a canonical list of per-frame hashes. The exact canonical bytes are **ABSENT**. This helper does not establish or require a manifest property named `frames_sha256`. Source: `visioncore/frame.py:49-51`, `visioncore/frame.py:61-99`.

## 2. Grid rule derived from frame size

### 2.1 Executable selection rule

`grid_params(W,H)` returns `(s,gw,gh)`, where `s` is tile side length in pixels, `gw` is the number of tile columns, and `gh` is the number of tile rows. `Grid` sets `N = gw * gh`. The coordinate and padding operations establish this geometry. Sources: `visioncore/layout.py:15-25`; `visioncore/grid.py:9-19`, `visioncore/grid.py:30-39`.

The direct grid entry point checks that `W` and `H` satisfy `isinstance(value,int)` and inclusive `1..8192`. Unlike the manifest's `_int` check, it does not explicitly exclude Boolean values; Python `True` satisfies this dimension check as 1. Invalid dimensions raise `E_GRID`. Source: `visioncore/layout.py:12`, `visioncore/layout.py:17-18`.

The effective parameter values are imported:

| Symbol | Bound definition | Effective numeric value |
|---|---|---|
| `TILE_MIN_PX` | `PARAMS["TILE_MIN_PX"]` at `visioncore/layout.py:9` | ABSENT |
| `CELL_MAX` | `PARAMS["CELL_MAX"]` at `visioncore/layout.py:10` | ABSENT |
| `AXIS_MAX` | `PARAMS["AXIS_MAX"]` at `visioncore/layout.py:11` | ABSENT |

`PARAMS` comes from the unbound `.params` import at `visioncore/layout.py:6`. No value from that file is assumed.

The executable rule is reproduced below, with the source exception payloads omitted:

```python
s = TILE_MIN_PX
while s <= 1 << 16:
    gw, gh = -(-W // s), -(-H // s)
    if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
        return s, gw, gh
    s <<= 1
raise E_GRID
```

Source: `visioncore/layout.py:19-25`. For positive `s`, the two quotient expressions compute ceiling division. The function tries the initial parameter value, then repeatedly doubles it, returning the first candidate satisfying both cell-count and axis-count conditions. Its loop bound is inclusive `65536`. The function does not independently validate that the imported initial value is positive or a power of two.

The docstring states the intended rule as the smallest power of two at least 16, with at most 4096 cells and at most 255 cells on either axis. Those numbers are **documented assertions** at `visioncore/layout.py:16`. The effective imported values needed to verify that runtime behavior are **ABSENT** from the bound source set. In particular, this draft does not substitute those docstring numbers for the parameter lookups.

### 2.2 Grid identity and coordinate domain

The generated grid identifier is exactly:

```text
grid:<W>x<H>:s<s>
```

The actual formatting expression is `"grid:%dx%d:s%d" % ((W, H, grid_params(W,H)[0]))`. Source: `visioncore/layout.py:28-29`.

Existing cells have `tx` drawn from `range(gw)` and `ty` from `range(gh)`. Thus the enumerated tile coordinates are zero-based column and row positions. The source stores `W,H,s,gw,gh` and the derived `N` as in-memory attributes; no independent wire offsets for these attributes are defined. Their separate header representations are documented in section 5. Source: `visioncore/grid.py:11-19`.

## 3. Morton codes, table order, and padding

### 3.1 Coordinate encoding

`morton(tx,ty)` checks `0 <= tx <= AXIS_MAX` and `0 <= ty <= AXIS_MAX`, raising `E_VC_ID_RANGE` on a range failure. Its accepted upper bound is the imported symbol, not a hardcoded 255. It returns `_spread(tx) | (_spread(ty) << 1)`. Source: `visioncore/layout.py:50-53`.

`_spread` first retains only the low eight input bits using `v &= 0xFF`. It spreads them with masks `0x0F0F`, `0x3333`, and `0x5555`. Therefore, as a direct derivation of the bit operations, x bits occupy positions `0,2,...,14` and y bits occupy `1,3,...,15` in the resulting 16-bit code. High input bits are discarded regardless of the effective imported axis cap. Source: `visioncore/layout.py:32-38`, `visioncore/layout.py:53`.

`unmorton(m)` checks inclusive `0..65535` and returns `(_compact(m), _compact(m >> 1))`. The compact operation recovers the alternating bit positions into eight-bit coordinate values. The range failure is `E_VC_ID_RANGE`. Source: `visioncore/layout.py:41-46`, `visioncore/layout.py:56-59`.

### 3.2 Order over existing cells

The implementation constructs a `uint16` array of shape `(gh,gw)` containing the Morton code of every existing cell. It flattens that array in row-major order and computes `np.argsort(..., kind="stable")`. This gives ascending code order over the existing rectangular grid; any equal-code ties retain their row-major order. It does not insert cells for missing Morton-code values. Source: `visioncore/grid.py:9`, `visioncore/grid.py:16-23`.

The resulting mappings are:

| In-memory attribute | Meaning and type established | Source |
|---|---|---|
| `flat` | Sorted permutation; Morton rank to row-major cell index | `visioncore/grid.py:21-22` |
| `mortons` | `uint16` Morton codes selected in that order | `visioncore/grid.py:16`, `visioncore/grid.py:23` |
| `tx` | `(perm % gw).astype(np.int32)` | `visioncore/grid.py:24` |
| `ty` | `(perm // gw).astype(np.int32)` | `visioncore/grid.py:25` |
| `order` | `order[perm] = np.arange(N, dtype=np.int16)`, reshaped to `(gh,gw)`; intended cell-to-rank mapping, with no explicit rank-capacity check | `visioncore/grid.py:26-28` |

These are implementation arrays, not additional declared manifest properties or wire records. Their wire byte offsets and sizes are **ABSENT**.

For illustration, applying the bound bit operations and stable ordering to the existing cells of a hypothetical `gw=3, gh=2` grid gives:

| Morton rank | `(tx,ty)` | Morton code | Row-major cell index |
|---:|---|---:|---:|
| 0 | `(0,0)` | 0 | 0 |
| 1 | `(1,0)` | 1 | 1 |
| 2 | `(0,1)` | 2 | 3 |
| 3 | `(1,1)` | 3 | 4 |
| 4 | `(2,0)` | 4 | 2 |
| 5 | `(2,1)` | 6 | 5 |

This is a derived illustration of `visioncore/layout.py:33-38`, `visioncore/layout.py:53`, and `visioncore/grid.py:16-28`. It does not claim that a particular frame size selects this grid under the unseen parameters. It shows why Morton code and Morton rank are different values.

### 3.3 Edge tiles

`Grid.pad(rgb)` computes `ph = gh*s-H` and `pw = gw*s-W`. If both are zero it returns the original input. Otherwise it applies `np.pad(rgb, ((0,ph),(0,pw),(0,0)), mode="edge")`, extending the bottom and right edges by replication. The docstring describes a `uint8[gh*s,gw*s,3]` result; the method itself does not validate or cast the supplied array's dtype or shape. Source: `visioncore/grid.py:30-35`.

`pad_flags()` returns one Boolean per Morton-ranked tile:

```text
((tx + 1) * s > W) OR ((ty + 1) * s > H)
```

A true value indicates a tile that crosses the right or bottom frame border. The relation between this Boolean array and a numeric bit in the serialized `flags` field is **ABSENT**. Source: `visioncore/grid.py:37-39`; flag names: `visioncore/layout.py:66`.

## 4. Tile descriptor: `vqa.tile/v1`

### 4.1 Record layout

The descriptor is exactly **32 bytes, packed, little-endian**, using:

```text
<HBBHbbHHBBbbQQ
```

The format, ordered field names, explicit offsets, record size, and size assertion are at `visioncore/layout.py:91-99`. The table's type widths, signedness, and storage ranges are derived from that standard `struct` format; its offsets match the literal `TILE_OFFSETS` tuple.

| Field | Storage type | Offset | Size | Units or meaning established | Allowed range and validation | Exact defining source |
|---|---|---:|---:|---|---|---|
| `morton` | Unsigned 16-bit LE, `H` | 0 | 2 | Encoded tile-coordinate code; no physical unit | Storage `0..65535`; generation is section 3; record checker does not validate membership in a grid | `visioncore/layout.py:94-95`, `visioncore/layout.py:97-99`; generation: `visioncore/layout.py:50-53` |
| `cls` | Unsigned 8-bit, `B` | 2 | 1 | Classification ID | Storage `0..255`; validated IDs `0..6` | `visioncore/layout.py:94-95`, `visioncore/layout.py:97`; `visioncore/layout.py:63-64`, `visioncore/layout.py:74-77`, `visioncore/layout.py:134-139` |
| `flags` | Unsigned 8-bit, `B` | 3 | 1 | Named flags exist; numeric bit assignments and units ABSENT | Storage `0..255`; semantic range ABSENT | `visioncore/layout.py:66`, `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `y_q4` | Unsigned 16-bit LE, `H` | 4 | 2 | Meaning, unit, quantization, and scale ABSENT | Storage `0..65535`; tighter range ABSENT | `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `co_h` | Signed 8-bit, `b` | 6 | 1 | Meaning, unit, and scale ABSENT | Storage `-128..127`; tighter range ABSENT | `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `cg_h` | Signed 8-bit, `b` | 7 | 1 | Meaning, unit, and scale ABSENT | Storage `-128..127`; tighter range ABSENT | `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `sig_q4` | Unsigned 16-bit LE, `H` | 8 | 2 | Meaning, unit, quantization, and scale ABSENT | Storage `0..65535`; tighter range ABSENT | `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `grad_q6` | Unsigned 16-bit LE, `H` | 10 | 2 | Meaning, unit, quantization, and scale ABSENT | Storage `0..65535`; tighter range ABSENT | `visioncore/layout.py:94-95`, `visioncore/layout.py:97` |
| `edge` | Unsigned 8-bit, `B` | 12 | 1 | Meaning, unit, and scale ABSENT | Storage `0..255`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |
| `resid` | Unsigned 8-bit, `B` | 13 | 1 | Meaning, unit, and scale ABSENT | Storage `0..255`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |
| `mv_dx` | Signed 8-bit, `b` | 14 | 1 | Coordinate convention, motion interpretation, unit, and scale ABSENT | Storage `-128..127`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |
| `mv_dy` | Signed 8-bit, `b` | 15 | 1 | Coordinate convention, motion interpretation, unit, and scale ABSENT | Storage `-128..127`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |
| `sig64` | Unsigned 64-bit LE, `Q` | 16 | 8 | Meaning, algorithm, and units ABSENT | Storage `0..18446744073709551615`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |
| `xh64` | Unsigned 64-bit LE, `Q` | 24 | 8 | Meaning, algorithm, and units ABSENT | Storage `0..18446744073709551615`; tighter range ABSENT | `visioncore/layout.py:94`, `visioncore/layout.py:96-97` |

Suffixes such as `q4` and `q6` do not establish scaling denominators, rounding, saturation, or a measurement algorithm in these files. Those definitions remain **ABSENT**.

### 4.2 Classification and flags

`CLS_ID` assigns IDs by enumerating the following tuple:

| ID | Name | Source |
|---:|---|---|
| 0 | `UNCHANGED` | `visioncore/layout.py:63-64` |
| 1 | `JITTER` | `visioncore/layout.py:63-64` |
| 2 | `CHANGED` | `visioncore/layout.py:63-64` |
| 3 | `MOVED` | `visioncore/layout.py:63-64` |
| 4 | `FLICKER` | `visioncore/layout.py:63-64` |
| 5 | `FROZEN` | `visioncore/layout.py:63-64` |
| 6 | `DRIFT` | `visioncore/layout.py:63-64` |

`is_dirty(cls)` validates the class and returns whether its ID is at least 2. Classification decision rules beyond those labels and that predicate are **ABSENT**. Source: `visioncore/layout.py:65`, `visioncore/layout.py:74-88`.

`TILE_FLAGS` lists `EDGE`, `TEXTURE`, `SHADE_ONLY`, `SAT`, `UI_OCC`, `ACTOR_OCC`, `GLOBAL_MV`, and `PAD` in that tuple order. Numeric values, bit positions, reserved-bit behavior, and valid combinations are **ABSENT**. The tuple alone does not define `1 << index` as the wire encoding. Source: `visioncore/layout.py:66`.

### 4.3 What the record routines check

`unpack_tile(b,off=0)` uses `struct.unpack_from(TILE_FMT,b,off)` and zips the values with `TILE_FIELDS`. It provides no additional semantic validation. Source: `visioncore/layout.py:130-131`.

`check_tile_records(b)` requires the supplied buffer length to be a multiple of 32, then checks the class byte at offset 2 in each record with `cls_name`. A non-multiple raises `E_VC_FORMAT`; an invalid class raises `E_SCHEMA`. The empty buffer passes these checks. Source: `visioncore/layout.py:74-77`, `visioncore/layout.py:134-139`.

These routines do not establish a record count per frame, a frame-to-record mapping, Morton uniqueness, grid membership, ordering validation, flag semantics, or measurement constraints. The grid table order is established in section 3, but these record routines do not verify that an input byte stream follows it.

## 5. Adjacent binary definitions in the bound layout

### 5.1 Tile header

`layout.py` also defines a separate 64-byte packed little-endian header. The field names below follow the header comment; parser key aliases are given where different. Header offsets are **derived from the packed format**, rather than an explicit header-offset tuple.

```text
<8sHHHHHHHHII16s8s8s
```

Sources: `visioncore/layout.py:101-105`. For every row below, `visioncore/layout.py:103-104` supplies the field sequence and storage format; the additional citations give packing or parsing behavior.

| Field / parser key | Type | Offset | Size | Units or meaning established | Allowed range / behavior | Exact defining source |
|---|---|---:|---:|---|---|---|
| `magic` | 8 bytes | 0 | 8 | File marker | Packer writes and parser requires `VQATILE1` | `visioncore/layout.py:101`, `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:123-124` |
| `ver` | Unsigned 16-bit LE | 8 | 2 | Header version | Packer writes and parser requires `1` | `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:123-124` |
| `s` | Unsigned 16-bit LE | 10 | 2 | Same-named grid quantity is tile side in pixels; header consistency is not checked | Storage `0..65535`; additional header validation ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:113-114`, `visioncore/layout.py:125`; geometry: `visioncore/grid.py:12-13`, `visioncore/grid.py:30-35` |
| `W` | Unsigned 16-bit LE | 12 | 2 | Same-named grid width in pixels | Storage `0..65535`; additional header validation ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:113-114`, `visioncore/layout.py:125`; geometry: `visioncore/grid.py:30-35` |
| `H` | Unsigned 16-bit LE | 14 | 2 | Same-named grid height in pixels | Storage `0..65535`; additional header validation ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:113-114`, `visioncore/layout.py:125`; geometry: `visioncore/grid.py:30-35` |
| `gw` | Unsigned 16-bit LE | 16 | 2 | Same-named grid tile-column count | Storage `0..65535`; additional header validation ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:125`; grid: `visioncore/grid.py:12-19` |
| `gh` | Unsigned 16-bit LE | 18 | 2 | Same-named grid tile-row count | Storage `0..65535`; additional header validation ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:125`; grid: `visioncore/grid.py:12-19` |
| `N` | Unsigned 16-bit LE | 20 | 2 | Same-named grid cell count | Storage `0..65535`; header does not check `N == gw*gh` | `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:125`; grid: `visioncore/grid.py:14` |
| `rec` | Unsigned 16-bit LE | 22 | 2 | Tile record size in bytes | Packer writes and parser requires `32` | `visioncore/layout.py:98`, `visioncore/layout.py:103-104`, `visioncore/layout.py:114`, `visioncore/layout.py:123-124` |
| `n_frames` | Unsigned 32-bit LE | 24 | 4 | Explicit count interpretation and association with manifest `count` ABSENT | Storage `0..4294967295`; additional constraint ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:113-114`, `visioncore/layout.py:126` |
| `f0` | Unsigned 32-bit LE | 28 | 4 | Meaning, units, and index origin ABSENT | Storage `0..4294967295`; additional constraint ABSENT | `visioncore/layout.py:103-104`, `visioncore/layout.py:113-114`, `visioncore/layout.py:126` |
| `params` / `params_sha256_16` | 16 bytes; parser returns hex string | 32 | 16 | Prefix of hex-decoded `params_sha256_hex` input; digest provenance ABSENT | Packer takes first 16 decoded bytes; fixed `16s` storage pads shorter data with zero bytes; parser accepts any 16 bytes | `visioncore/layout.py:103-104`, `visioncore/layout.py:115`, `visioncore/layout.py:126` |
| `api` / `api_version` | 8 bytes, ASCII packing/decoding | 48 | 8 | API-version text; vocabulary ABSENT | Packer truncates ASCII encoding to 8 bytes and NUL-pads; parser removes trailing NULs then decodes ASCII | `visioncore/layout.py:103-104`, `visioncore/layout.py:115`, `visioncore/layout.py:127` |
| `zero` | 8 bytes | 56 | 8 | Zero padding emitted by the packer | Packer writes eight zero bytes; parser does not check their values | `visioncore/layout.py:103-104`, `visioncore/layout.py:116`, `visioncore/layout.py:122-127` |

The parser requires at least 64 bytes and unpacks only the first 64. It checks magic, version, and record size. It does not validate grid consistency, positivity, frame counts, a parameters digest, or the final zero bytes. Source: `visioncore/layout.py:119-127`.

The presence of these routines does not establish a complete tile-file stream layout. The location of the first tile descriptor, placement of any motion data, grouping of records by frame, and required relationship to a frame manifest are **ABSENT**.

### 5.2 GlobalMotion format

The bound file defines only `GLOBAL_MOTION_FMT = "<hhI"` and `GLOBAL_MOTION_SIZE = 8`, plus a size assertion. It does not name the three components. Source: `visioncore/layout.py:107-110`.

| Positional component; field name ABSENT | Storage type | Derived offset | Size | Storage range | Units / semantic range | Source |
|---|---|---:|---:|---|---|---|
| First | Signed 16-bit LE | 0 | 2 | `-32768..32767` | ABSENT / ABSENT | `visioncore/layout.py:108-110` |
| Second | Signed 16-bit LE | 2 | 2 | `-32768..32767` | ABSENT / ABSENT | `visioncore/layout.py:108-110` |
| Third | Unsigned 32-bit LE | 4 | 4 | `0..4294967295` | ABSENT / ABSENT | `visioncore/layout.py:108-110` |

A mapping between these components and the core-frame `global_mv` array is **ABSENT**; no names such as displacement, confidence, or flags are assigned by this draft.

## 6. Separate core-frame carrier: `vqa.core-frame/v1`

The bound one-line JSON Schema declares draft 2020-12, an object type, `additionalProperties: true`, and the description `"carrier: frames.jsonl row"`. It requires all eight fields below. Every constraint in this section is at `visioncore/schemas/vqa.core-frame.v1.schema.json:1`.

| Name | Declared type | Byte offset / size | Units or meaning established | Allowed range / additional constraints | Exact defining source |
|---|---|---|---|---|---|
| `schema` | Constant string | ABSENT / ABSENT | Schema identifier | Exactly `"vqa.core-frame/v1"` | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `frame` | Integer | ABSENT / ABSENT | Units and mapping to a manifest index ABSENT | Minimum, maximum, and index origin ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `grid` | String | ABSENT / ABSENT | Enforced relationship to `grid_ident` ABSENT | Pattern, length, and vocabulary ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `n` | Array | ABSENT / ABSENT | Element meanings and units ABSENT | Item types and length ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `cov_ppm` | Integer | ABSENT / ABSENT | Explicit unit definition ABSENT; suffix alone supplies no validation rule | Minimum and maximum ABSENT; `0..1000000` is not specified | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `global_mv` | Array or null | ABSENT / ABSENT | Component meanings and units ABSENT | Array length and item types ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `regions` | Integer | ABSENT / ABSENT | Explicit interpretation and units ABSENT | Minimum and maximum ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |
| `tiles_sha256` | String | ABSENT / ABSENT | Hash input and verification rule ABSENT | Length, hex pattern, and format constraint ABSENT | `visioncore/schemas/vqa.core-frame.v1.schema.json:1` |

Required `global_mv` may be null; required does not mean non-null. No relation between `n` and the classification tuple, or between `tiles_sha256` and a particular tile-file byte sequence, is supplied by this schema.

## 7. Remaining ABSENT definitions

The following omissions limit the completeness of this source-bounded draft:

- A complete manifest serialization/canonicalization rule, fixed manifest field offsets, and a manifest JSON Schema.
- Effective numeric values of the three imported grid parameters; validation of their configuration.
- Explicit timestamp units, epoch, frame-rate relationships, and render-index correspondence.
- Storage naming/layout, PNG or raw framing, and the hash input domain of `file_sha256`.
- Tile measurement definitions, units, quantization, rounding, saturation, and meaningful value ranges beyond storage widths.
- Serialized tile-flag bit assignments, valid flag combinations, and mapping from `pad_flags()`.
- Algorithms or input domains for `sig64` and `xh64`.
- A complete binary stream layout, frame grouping, descriptor count rules, and header/manifest cross-validation.
- GlobalMotion field names, scales, semantic constraints, and its relationship to `global_mv`.
- Additional core-frame property constraints beyond its declared types, required names, schema constant, and allowance for extra properties.

No unbound implementation, referenced specification, default-branch content, or remembered project convention was used to fill these gaps.
