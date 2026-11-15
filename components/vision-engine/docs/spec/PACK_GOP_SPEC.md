# PACK and GOP Output Specification

## Source snapshot and reading conventions

This specification describes the executable producers in `oguirado/vision-engine` at commit `2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5`, resolved from `bridge/integration`. It uses only the following four bound files. References are original, one-based source lines; a range is inclusive. Statements labeled **ABSENT** identify a missing definition or guarantee in this evidence set, rather than an inferred implementation elsewhere.

| Bound source | Source snapshot | Role in this specification |
|---|---|---|
| `visioncore/gop.py` | [gop.py at the observed commit](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/gop.py) | GOP planning, binary codec, tracks/cycles/events, and output records. (`visioncore/gop.py:117-141`, `visioncore/gop.py:231-280`, `visioncore/gop.py:503-521`.) |
| `visioncore/pack.py` | [pack.py at the observed commit](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/pack.py) | Pack files, V3 fitting, image generation, selection manifests, requests/cache, and token accounting. (`visioncore/pack.py:556-673`, `visioncore/pack.py:688-780`.) |
| `visioncore/canon.py` | [canon.py at the observed commit](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/canon.py) | JSON serialization, parsing, hashes, and staged file replacement. (`visioncore/canon.py:13-81`.) |
| `AGENTS.md` | [AGENTS.md at the observed commit](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/AGENTS.md) | Repository claims about integer arithmetic, identifiers, schemas, and output preservation; these claims do not substitute for producer validation. (`AGENTS.md:31-38`.) |

An output field described as copied is not thereby schema-validated. An integer conversion, explicit guard, binary representable range, and upstream convention are different constraints and are identified separately below. The canonical serializer rejects floats and non-string dictionary keys, but does not supply the missing application schemas. (`visioncore/canon.py:13-29`; copying examples: `visioncore/gop.py:503-508`, `visioncore/pack.py:660-672`.)

**`pack_tokens`: ABSENT.** No definition, import, or call with that name occurs in the four bound files. The token behavior actually present is `tok_image`, `est_tokens`, the `pack` media ladder, `judge_batch` request accounting, and `RunTokenBudget`; those implementations are specified below. (`visioncore/pack.py:444-455`, `visioncore/pack.py:619-636`, `visioncore/pack.py:730-780`.)

### Contents

- [GOP construction and files](#gop-construction-and-files)
- [Pack outputs and public result objects](#pack-outputs-and-public-result-objects)
- [V3 structure, priorities, and byte fitting](#v3-structure-priorities-and-byte-fitting)
- [Frame selection and media fitting](#frame-selection-and-media-fitting)
- [PNG byte format, heatmap rendering, and crops](#png-byte-format-heatmap-rendering-and-crops)
- [Optional coordinate-shift path](#optional-coordinate-shift-path)
- [Judge request, response cache, and run token allowance](#judge-request-response-cache-and-run-token-allowance)
- [File-write behavior and explicit pack errors](#file-write-behavior-and-explicit-pack-errors)
- [Pack-specific ABSENT boundaries](#pack-specific-absent-boundaries)
- [Canonical serialization and the scope of byte stability](#canonical-serialization-and-the-scope-of-byte-stability)
- [Other ABSENT definitions and guarantees](#other-absent-definitions-and-guarantees)

## GOP construction and files

This section describes the values produced by `encode_run`, `gop_record`, and `write_gops`. For direct calls with arbitrary caller-created objects, these functions do not provide an independent schema validator; ranges described as encoder-produced apply to the successful `encode_run` path. A wire range is the range of the specified `struct` field, not an additional application validation. The format strings and size assertions are explicit in the source. (`visioncore/gop.py:19-23`, `visioncore/gop.py:231-280`, `visioncore/gop.py:503-521`.)

Use `T = layout.TILE_SIZE`, `N = grid.N`, `gw = grid.gw`, and `gh = grid.gh` below. The runtime numeric values of `T`, `layout.AXIS_MAX`, `layout.CLS_DIRTY_MIN`, and the implementation of `layout.morton` are **ABSENT** from the bound files: `gop.py` imports them. Although its input documentation and an error string say “32 B,” the executable record-size check, delta lengths, and decoder use `layout.TILE_SIZE`; they do not define that constant. (`visioncore/gop.py:16`, `visioncore/gop.py:101-109`, `visioncore/gop.py:231-245`, `visioncore/gop.py:307-311`, `visioncore/gop.py:338-341`.)

### Run validation, frame order, cuts, and keyframes

`encode_run` first converts every tile table to a contiguous NumPy array. An empty run raises `E_VC_FORMAT` with reason `empty run`. Each table must have shape `(N,)` and dtype item size `T`; otherwise `_rec_bytes` raises `E_VC_FORMAT`. The table is then viewed as an `N × T` array of bytes. The docstring asks for ascending Morton input, but the executable input check does not validate that order, dtype field layout, or agreement between `N` and `gw * gh`. (`visioncore/gop.py:101-105`, `visioncore/gop.py:231-245`.)

Without `frame_nums`, the frame numbers are `0, ..., nf - 1`. Supplied frame numbers are converted with `int`, must number exactly `nf`, and must be strictly increasing; otherwise the code raises `E_VC_FORMAT`. Adjacent source frames need not have adjacent frame numbers. GOP planning uses positions in the captured sequence, whereas binary timestamps and JSON frame fields use those frame numbers. Successful binary construction requires each serialized frame number to fit an unsigned 32-bit field; there is no separate prevalidation or clipping of frame numbers. (`visioncore/gop.py:19-23`, `visioncore/gop.py:160-162`, `visioncore/gop.py:200-208`, `visioncore/gop.py:240-264`.)

For each frame, a tile is dirty when `cls >= layout.CLS_DIRTY_MIN`; `dirty_ppm = n_dirty * 1000000 // N`. With a nonempty valid grid, this yields an integer from `0` to `1000000` inclusive. This calculation also runs for an I-frame; the I-frame is nevertheless stored as the entire table, not as only its dirty records. There is no local check that prevents division by zero when `N == 0`. (`visioncore/gop.py:108-114`, `visioncore/gop.py:243-251`.)

The parameter mapping is `P.PARAMS` unless `params` is supplied. The numeric defaults and validation contract of `GOP_K`, `CUT_PPM_1F`, and `CUT_PPM_2F` are **ABSENT**. For sequence position `t >= 1`, a cut is set exactly when `dirty_ppm[t] >= CUT_PPM_1F`, or when both `dirty_ppm[t] >= CUT_PPM_2F` and the next frame's dirty value is at least `CUT_PPM_2F`. The next value for the last position is the literal `-1`; position zero is initialized to `False` and never changed. No sign/range validation of the cut thresholds occurs here. (`visioncore/gop.py:16`, `visioncore/gop.py:117-125`, `visioncore/gop.py:235`.)

A new GOP starts at position zero, at each cut, or when the number of positions since the last GOP start is at least `GOP_K`. Its end is the next start, exclusive, or the sequence length. A flash is a cut at the GOP start immediately followed by another cut in the captured sequence. Thus sparse frame-number gaps do not themselves split a GOP. Each planned start becomes an I-frame; the remaining positions in that interval become P-frames. Tracks and cycles are constructed separately for each interval, so this encoder does not carry track state across GOP boundaries. (`visioncore/gop.py:128-141`, `visioncore/gop.py:248-279`.)

`plan_gops` returns a list of the following intermediate objects; they are not emitted as a separate file. (`visioncore/gop.py:128-141`.)

| Key | Type and value | Source |
|---|---|---|
| `start` | Integer captured-sequence position, inclusive; for a nonempty run, from `0` through `nf - 1`. | `visioncore/gop.py:132-140` |
| `end` | Integer captured-sequence position, exclusive; greater than `start` and at most `nf`. | `visioncore/gop.py:138-140` |
| `cut` | Boolean `cuts[start]`. | `visioncore/gop.py:140` |
| `flash` | Boolean `cuts[start] and start + 1 < nf and cuts[start + 1]`. | `visioncore/gop.py:140` |

### Output inventory and publication

`gop.py` writes exactly the following two named final files through `write_gops`; it returns the list of serialized JSON lines. The supplied `gops` iteration order is preserved, with no sort inside the writer. `encode_run` itself returns objects without writing a file. (`visioncore/gop.py:247-280`, `visioncore/gop.py:511-521`.)

| File | Exact composition | Source |
|---|---|---|
| `gops.bin` | Each supplied `g.blob` concatenated directly, without separators. Empty input produces empty bytes. | `visioncore/gop.py:514-519` |
| `gops.jsonl` | One `canon.dumps(gop_record(...))` per supplied GOP, concatenated in the same order. Each line ends in LF because `canon.dumps` adds it. Empty input produces empty text. Text is encoded as UTF-8 by `write_atomic`. | `visioncore/gop.py:514-520`; `visioncore/canon.py:27-29`, `visioncore/canon.py:58-66` |

The writer first builds both complete buffers, then invokes `canon.write_atomic` for `gops.bin`, followed by `gops.jsonl`. Each individual write stages to `<filename>.tmp` and uses `os.replace`; there is no transaction across the pair. If the second write fails after the first replacement succeeds, the first file can already have changed. The `write_gops` docstring's claim that *any* failure leaves both targets as before is not implemented as pairwise rollback. The functions do not create the output directory. (`visioncore/gop.py:511-520`, `visioncore/canon.py:58-72`.)

### Binary GOP layout

Each blob is `header | zlib-compressed I-frame | P-frame body`. Its byte length is `48 + izlen + body_len`. The header format is `<4sHHHHHHIHHIIQQ`; the P-frame header format is `<IHHhhIII`. Both explicitly select little-endian standard-size fields, and the source asserts header sizes of `48` and `24` bytes. The offsets below are calculated directly from those formats. (`visioncore/gop.py:19-23`, `visioncore/gop.py:160-162`, `visioncore/gop.py:181-190`, `visioncore/gop.py:264`.)

| Header byte offset | Field | Wire type / width | Value and local constraints | Source |
|---|---|---|---|---|
| `0` | `magic` | `4s`, 4 bytes | Exactly `b"VGOP"`; the parser rejects any other value. | `visioncore/gop.py:19-24`, `visioncore/gop.py:160-162`, `visioncore/gop.py:165-170` |
| `4` | `ver` | Unsigned 16-bit, 2 bytes | Encoder writes `1`; parser requires `1`. | `visioncore/gop.py:19`, `visioncore/gop.py:160-172` |
| `6` | `hdr_len` | Unsigned 16-bit, 2 bytes | Encoder writes `48`; parser requires `48`. | `visioncore/gop.py:19-20`, `visioncore/gop.py:160-172` |
| `8` | `gw` | Unsigned 16-bit, 2 bytes | `grid.gw`; parser requires `1 <= gw <= layout.AXIS_MAX`; numeric upper grid limit is **ABSENT**. | `visioncore/gop.py:19`, `visioncore/gop.py:173-174`, `visioncore/gop.py:243`, `visioncore/gop.py:262-264` |
| `10` | `gh` | Unsigned 16-bit, 2 bytes | `grid.gh`; parser applies the same imported-axis bound. | `visioncore/gop.py:19`, `visioncore/gop.py:173-174`, `visioncore/gop.py:243`, `visioncore/gop.py:262-264` |
| `12` | `tile_px` | Unsigned 16-bit, 2 bytes | `grid.s`; representable values `0..65535`. No further check here. | `visioncore/gop.py:19`, `visioncore/gop.py:165-178`, `visioncore/gop.py:262-264` |
| `14` | `K` | Unsigned 16-bit, 2 bytes | `p["GOP_K"]`; representable values `0..65535`. No local requirement that it be positive. | `visioncore/gop.py:19`, `visioncore/gop.py:135`, `visioncore/gop.py:165-178`, `visioncore/gop.py:262-264` |
| `16` | `f0` | Unsigned 32-bit, 4 bytes | Actual frame number of the I-frame; representable values `0..4294967295`. | `visioncore/gop.py:19`, `visioncore/gop.py:240-242`, `visioncore/gop.py:262-264` |
| `20` | `n_frames` | Unsigned 16-bit, 2 bytes | Number of captured positions in this GOP, `b - a`; successful encoder output is `1..65535`. Parser alone does not enforce a positive count or a relationship with `K`. | `visioncore/gop.py:19`, `visioncore/gop.py:128-141`, `visioncore/gop.py:165-178`, `visioncore/gop.py:262-264` |
| `22` | `flags` | Unsigned 16-bit, 2 bytes | Bitwise combination described below; encoder values and parser-accepted values are within `0..7`. | `visioncore/gop.py:19`, `visioncore/gop.py:25-26`, `visioncore/gop.py:175-176`, `visioncore/gop.py:260-264` |
| `24` | `izlen` | Unsigned 32-bit, 4 bytes | Byte length of compressed I-frame; representable values `0..4294967295`. | `visioncore/gop.py:19`, `visioncore/gop.py:250-264` |
| `28` | `body_len` | Unsigned 32-bit, 4 bytes | Sum of all encoded P-frame byte lengths; representable values `0..4294967295`. | `visioncore/gop.py:19`, `visioncore/gop.py:253-264` |
| `32` | `key_sha8` | Unsigned 64-bit, 8 bytes | First 8 bytes of SHA-256 of raw I-frame bytes, interpreted as little-endian integer; range `0..18446744073709551615`. | `visioncore/gop.py:19`, `visioncore/gop.py:156-162`, `visioncore/gop.py:263-264` |
| `40` | `body_sha8` | Unsigned 64-bit, 8 bytes | First 8 bytes of SHA-256 of the encoded P-frame body, interpreted as little-endian integer; same unsigned 64-bit range. | `visioncore/gop.py:19`, `visioncore/gop.py:156-162`, `visioncore/gop.py:259-264` |

`F_CUT_START = 1` is set for a cut-start GOP; `F_FLASH = 2` is set for a flash-start GOP; `F_RUN_END = 4` is set only on the final GOP returned by this `encode_run` call. `FLAGS_MASK = 7`; the parser rejects all other flag bits as `E_GOP_PAD`. There are no explicit main-header padding fields. (`visioncore/gop.py:19-26`, `visioncore/gop.py:175-176`, `visioncore/gop.py:260-264`.)

The I-frame is `recs[a].tobytes()` compressed with `zlib.compress(raw_i, 6)`. Its uncompressed size on the encoder path is `N * T`. There is no field-by-field recoding, byte-order normalization, or padding removal within the tile records here; the bytes come from the supplied contiguous record array. The complete tile-record schema and its endian/padding rules are **ABSENT**. (`visioncore/gop.py:101-105`, `visioncore/gop.py:244`, `visioncore/gop.py:250-252`.)

The P-frame body concatenates one encoded P-frame for each subsequent captured position, in sequence order. Every P-frame has a 24-byte PHdr, then `8 * n_mb` mask bytes, then `dlen` RLE bytes. A one-frame GOP has an empty P-frame body. (`visioncore/gop.py:200-208`, `visioncore/gop.py:253-259`.)

| PHdr byte offset | Field | Wire type / width | Value and local constraints | Source |
|---|---|---|---|---|
| `0` | `t` | Unsigned 32-bit, 4 bytes | Actual current frame number, `0..4294967295` if packing succeeds. | `visioncore/gop.py:21`, `visioncore/gop.py:200-208`, `visioncore/gop.py:256` |
| `4` | `n_dirty` | Unsigned 16-bit, 2 bytes | Number of true dirty-mask entries; `0..min(N,65535)` if packing succeeds. | `visioncore/gop.py:21`, `visioncore/gop.py:202-207` |
| `6` | First pad | Unsigned 16-bit, 2 bytes | Encoder writes `0`; decoder rejects nonzero. | `visioncore/gop.py:21`, `visioncore/gop.py:207`, `visioncore/gop.py:318-320` |
| `8` | `gdx` | Signed 16-bit, 2 bytes | Motion `dx` converted with `int` and clipped to `-32768..32767`; defaults to zero for falsey motion input. | `visioncore/gop.py:21`, `visioncore/gop.py:207`, `visioncore/gop.py:218-228` |
| `10` | `gdy` | Signed 16-bit, 2 bytes | Motion `dy`, with the same conversion, clipping, and default. | `visioncore/gop.py:21`, `visioncore/gop.py:207`, `visioncore/gop.py:218-228` |
| `12` | `gshare_ppm` | Unsigned 32-bit, 4 bytes | Encoder integer `0..1000000`: among tiles with `cls != 0`, the fraction whose flags include bit `64`, times `1000000`, floored; zero if the denominator is zero. Decoder does not validate the ppm bound. | `visioncore/gop.py:21`, `visioncore/gop.py:222-228`, `visioncore/gop.py:318-320` |
| `16` | `dlen` | Unsigned 32-bit, 4 bytes | Length of the RLE delta in bytes; representable `0..4294967295`. | `visioncore/gop.py:21`, `visioncore/gop.py:203-208` |
| `20` | Second pad | Unsigned 32-bit, 4 bytes | Encoder writes `0`; decoder rejects nonzero. | `visioncore/gop.py:21`, `visioncore/gop.py:207`, `visioncore/gop.py:318-320` |

The numeric fields passed to `struct.pack` are not generally saturated. The explicit saturation is `_clip16` for the two global-motion components. In particular the code does not clip `n_dirty`, dimensions, GOP count, or byte lengths into their wire widths and does not wrap packing failures into `_gerr`. (`visioncore/gop.py:160-162`, `visioncore/gop.py:200-219`, `visioncore/gop.py:262-264`.)

#### Dirty masks, reference updates, and RLE

The grid helper sorts tuples `(layout.morton(tx, ty), tx, ty)` for every grid cell, converts Morton codes to a `uint16` array, and groups successive codes by `morton >> 6`. There is one mask word for each *present* group, not a dense set of all possible group indices. A dirty tile sets bit `morton & 63` of its group's unsigned 64-bit word; words are emitted in the helper's order as explicit little-endian `<u8` values. Coordinates are stored internally as `int32`; the cached Morton/coordinate/start arrays are set read-only and the helper's cache has maximum size `8`. The Morton mapping itself is imported and **ABSENT**. (`visioncore/gop.py:86-98`, `visioncore/gop.py:194-197`.)

For a P-frame, dirty record indexes are selected in ascending array-index order by `np.nonzero(dirty)[0]`. The delta is the bytewise XOR of the current records and the committed reference at those indexes, flattened to bytes and RLE-encoded. Only those reference records are then replaced. Non-dirty input records are not committed, even if their bytes differ from the reference. Thus the decoder reconstructs the committed table, not necessarily every byte in a caller's current input table. (`visioncore/gop.py:200-208`, `visioncore/gop.py:252-256`, `visioncore/gop.py:338-342`.)

An RLE token is `[z: uint8][l: uint8][l literal bytes]`. The encoder consumes the zero run beginning at its current position, capped at `255`; after it, the literal run is at most `255` bytes. It searches for four consecutive zero bytes using a search window ending at `pos + 258` and shortens the literal run to the first such occurrence strictly before its proposed end. It then emits `z`, the literal length, and those bytes, and repeats until the input is consumed. The zero marker is exactly four `0x00` bytes. This is the executable search rule; a long zero run can leave zero bytes in a later literal segment if no four-zero stop exists at that segment's beginning. Empty input encodes to empty bytes. (`visioncore/gop.py:29-30`, `visioncore/gop.py:43-60`.)

`rle_bound(n) = n + 2 + 2 * (n // 255)`. The decoder rejects encoded input longer than that bound with `E_DELTA_BOUND`; rejects an incomplete two-byte token header or literal overrun with `E_DELTA_TOKEN`; and rejects decoded length above or unequal to `n` with `E_DELTA_LEN`. It does not re-encode the stream to test whether it is the encoder's chosen representation, and it does not forbid a zero/zero token. (`visioncore/gop.py:39-40`, `visioncore/gop.py:63-83`.)

#### Size helpers and three hashes

The bound functions implement the following formulas, with `n_mb = len(_cells(gw, gh)[3])`. `max_gop_bytes` computes a value; `encode_run` does not call it as an enforcement check. Numeric defaults for `N`, `K`, and grid limits are **ABSENT**. (`visioncore/gop.py:39-40`, `visioncore/gop.py:145-153`, `visioncore/gop.py:231-280`.)

```text
rle_bound(n) = n + 2 + 2 * (n // 255)
zlib_bound(n) = n + (n >> 12) + (n >> 14) + (n >> 25) + 13
pmax = 24 + 8 * n_mb + rle_bound(T * N)
max_gop_bytes(N, K, gw, gh) = 48 + zlib_bound(T * N) + (K - 1) * pmax
```

| Hash exposed by `gop_record` | Exact preimage | Source |
|---|---|---|
| `key_sha256` | Raw uncompressed I-frame bytes. | `visioncore/gop.py:250`, `visioncore/gop.py:276-277` |
| `gop_blob_sha256` | The complete stored blob: actual header, compressed I-frame, and encoded P-frame body. | `visioncore/gop.py:264-267`, `visioncore/gop.py:278`, `visioncore/gop.py:506` |
| `gop_canon_sha256` | A synthetic header with the same `gw`, `gh`, `tile_px`, `K`, `f0`, `n_frames`, `flags`, `body_len`, and `key_sha8`, but with `izlen = 0` and `body_sha8 = 0`, followed by the uncompressed I-frame and the encoded P-frame body. | `visioncore/gop.py:262-266`, `visioncore/gop.py:278`, `visioncore/gop.py:506` |

All three are SHA-256 `hexdigest()` strings, hence 64 lowercase hexadecimal characters. The canonical GOP hash excludes the compressed I-frame representation but still includes the encoded P-frame body; it is not a hash of JSON and does not hash just decoded frame tables. For identical raw I-frame, header inputs, and P-frame body, changing the zlib representation does not change this canonical hash. Byte identity of `gops.bin` across different zlib implementations/versions is not guaranteed by this code. (`visioncore/gop.py:156-162`, `visioncore/gop.py:250-267`, `visioncore/gop.py:276-278`.)

### `gops.jsonl`: exact record schema

The `gop_record` object has exactly the following keys. A writer-produced offset starts at zero and increases by each preceding `len(g.blob)`. The function itself accepts `offset`, `grid_ident`, and `params_sha256` as arguments and copies them without validating their types or formats. (`visioncore/gop.py:503-520`.)

| Key | Encoder/writer-produced type, range, and meaning | Source |
|---|---|---|
| `schema` | String constant `"vqa.gop/v1"`. | `visioncore/gop.py:504` |
| `g` | Integer GOP index, starting at `0` within this `encode_run` call. | `visioncore/gop.py:248`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `gop` | Same integer value as `g`; an explicit duplicate field. | `visioncore/gop.py:504` |
| `f0` | Integer actual first frame number; unsigned 32-bit on successful binary construction. | `visioncore/gop.py:262-264`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `f1` | Integer actual last captured frame number, at least `f0`; unsigned 32-bit on successful binary construction. It is not computed as `f0 + n_frames - 1`. | `visioncore/gop.py:240-242`, `visioncore/gop.py:254-264`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `frame_ids` | JSON array of the actual increasing integer frame numbers in this GOP, inclusive of both endpoints; length `n_frames`. | `visioncore/gop.py:240-242`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `n_frames` | Integer `b - a`, `1..65535` on the successful encoder path. | `visioncore/gop.py:19`, `visioncore/gop.py:262-264`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `cut` | Boolean cut flag at this GOP start. | `visioncore/gop.py:140`, `visioncore/gop.py:276`, `visioncore/gop.py:504` |
| `offset` | Integer byte offset into `gops.bin`, nonnegative when produced by `write_gops`; no local maximum. | `visioncore/gop.py:505`, `visioncore/gop.py:514-518` |
| `length` | Integer `len(g.blob)`, the complete blob length in bytes. | `visioncore/gop.py:264`, `visioncore/gop.py:505` |
| `key_sha256` | String, 64 lowercase hex characters; raw I-frame SHA-256. | `visioncore/gop.py:277`, `visioncore/gop.py:505` |
| `gop_blob_sha256` | String, 64 lowercase hex characters; full stored blob SHA-256. | `visioncore/gop.py:267-278`, `visioncore/gop.py:506` |
| `gop_canon_sha256` | String, 64 lowercase hex characters; synthetic-header/uncompressed-I/encoded-body SHA-256. | `visioncore/gop.py:265-278`, `visioncore/gop.py:506` |
| `delta_bytes` | Integer byte length of the entire P-frame body, including each PHdr and dirty mask, not only RLE literals; from `0` through `4294967295` when its header field packs successfully. | `visioncore/gop.py:19`, `visioncore/gop.py:259-264`, `visioncore/gop.py:278`, `visioncore/gop.py:506` |
| `grid` | Unchanged `grid_ident` argument. Complete schema/type/range validation is **ABSENT** in this producer. | `visioncore/gop.py:503`, `visioncore/gop.py:506` |
| `params_sha256` | Unchanged argument of that name; the producer does not verify string type, hexadecimal format, length, or correspondence to the parameters used for encoding. | `visioncore/gop.py:503`, `visioncore/gop.py:507` |
| `tracks` | Array of exact track objects specified below. | `visioncore/gop.py:269-278`, `visioncore/gop.py:449-454`, `visioncore/gop.py:507` |
| `cycles` | Array of exact emitted cycle objects specified below; the internal `rank` key is removed before storage in `Gop`. | `visioncore/gop.py:271-279`, `visioncore/gop.py:492-499`, `visioncore/gop.py:507` |
| `global` | Array of four-integer global-motion rows specified below. | `visioncore/gop.py:253-258`, `visioncore/gop.py:279`, `visioncore/gop.py:507` |
| `events` | Array of two-element `[event_name, id]` string arrays specified below. | `visioncore/gop.py:275-290`, `visioncore/gop.py:508` |
| `dirty_ppm` | Array of per-frame integer dirty fractions, in `frame_ids` order and of equal length; each `0..1000000` for positive `N`. | `visioncore/gop.py:108-114`, `visioncore/gop.py:245`, `visioncore/gop.py:268-279`, `visioncore/gop.py:508` |

The host-side `Gop` object is a container created by unrestricted `__dict__.update(kw)`. On this encoder path its exact attributes are `g`, `f0`, `f1`, `frame_ids`, `n_frames`, `cut`, `flash`, `flags`, `blob`, `key_sha256`, `blob_sha256`, `canon_sha256`, `delta_bytes`, `tracks`, `cycles`, `glob`, `events`, and `dirty_ppm`. `blob` is bytes; `flash` is a boolean; `flags` is the integer header bitmask. `gop_record` renames `blob_sha256`, `canon_sha256`, and `glob` to the JSON names above, adds schema/location/caller metadata, and does not emit `blob`, `flash`, or `flags`. In particular a JSON `flash` field is **ABSENT**; a flash is available through the binary flag and the `FLASH` event. (`visioncore/gop.py:211-215`, `visioncore/gop.py:276-279`, `visioncore/gop.py:503-508`.)

### Regions and links: consumed fields, not separate emitted GOP records

`gop.py` does not emit a region table or a full core-link object in its GOP record. It consumes the fields below to build tracks. The upstream region/link JSON schema, additional keys, identifier validation rules, and class-name mapping are **ABSENT** because their producers/parsers are outside the bound files. The following is a consumption contract visible here, not a claim that these keys are the complete upstream schema. (`visioncore/gop.py:16`, `visioncore/gop.py:231-234`, `visioncore/gop.py:348-431`, `visioncore/gop.py:503-508`.)

| Consumed field | Use and locally established representation | Source |
|---|---|---|
| Per-frame region list | `regions[li] or []`, sorted by `reg["id"]`; `regions is None` means no regions. The encoder substitutes a per-frame list of `None` when `regions` is falsey. | `visioncore/gop.py:269-270`, `visioncore/gop.py:372-374` |
| Region `id` | Sort key, link endpoint, and representative ID. Passed to imported `ids.parse_any`; its parsed `frame` and `morton` initialize a track. Full accepted ID grammar is **ABSENT**. | `visioncore/gop.py:373-385`, `visioncore/gop.py:391-400` |
| Region `cls` | Passed to imported `layout.cls_id`; its numeric result selects a bit in `cls_mask`. Full mapping and permitted inputs are **ABSENT**. | `visioncore/gop.py:394` |
| Region `n_tiles` | Converted with `int`; contributes to maximum initialized at zero. Not checked against the actual region's selected tile count. | `visioncore/gop.py:386-387`, `visioncore/gop.py:397` |
| Region `mv` | Optional; a falsey value becomes `(0, 0)`, otherwise converted to a tuple and counted once per region row. Output takes its first two components and converts them with `int`. No local pixel/magnitude bound. | `visioncore/gop.py:395-396`, `visioncore/gop.py:449-452` |
| Region `r_sum` | Optional, default `0`, converted with `int`; ranks representative candidates. No local bound. | `visioncore/gop.py:398-400` |
| Region `spans` | If truthy, iterable of `[lo, hi]` pairs whose entries are parsed by `int(value, 16)`. Each pair selects the inclusive interval in sorted Morton values using left/right `searchsorted`; parts are concatenated. No overlap/reversal validation here. | `visioncore/gop.py:349-357` |
| Region `bbox_t` | Required when `spans` is falsey; unpacked as `[x0,y0,x1,y1]` and selects grid coordinates with inclusive bounds on both axes. No clamping or numeric-range validation here. | `visioncore/gop.py:358-359` |
| Per-frame link object | The local first frame always ignores its link object. Later entries use `links[li] or {}` if a links collection was provided. The encoder slices links into each GOP. | `visioncore/gop.py:269-270`, `visioncore/gop.py:374` |
| Link `links` | Optional list, default `[]`. Each entry contributes only its positions `0` and `1`, interpreted as source and destination region IDs. For duplicate destinations, the last pair wins in `linked_in`; all sources enter `linked_out`. Further positions are ignored here. | `visioncore/gop.py:375-377` |
| Link `split`, `merge` | Optional lists, default `[]`. An entry that is a string supplies that ID; an entry that is a list/tuple supplies only directly contained strings; other entries supply none. Known IDs add `SPLIT`/`MERGE` flags to their tracks. These flags do not create corresponding event rows here. | `visioncore/gop.py:362-365`, `visioncore/gop.py:408-412` |

For each sorted region, the destination-to-source mapping is consulted, then the retained region-to-track map is consulted. A known source continues its mapped track. Otherwise the new track key is `ids.track_id(parsed_frame, parsed_morton)`. The region-to-track mapping persists throughout this GOP; the code performs no geometric rematching and does not constrain a supplied link to the immediately preceding frame beyond the caller's input convention. The first GOP-local region occurrence does not receive `APPEAR`; a later new track does receive it if the `links` argument was supplied, including an empty link object. A previous frame's known region whose ID is absent from `linked_out` causes `VANISH` on its track at the current frame. (`visioncore/gop.py:368-413`.)

### Track objects and ordering

Tracks are sorted by parsing each key as `k[1:].split(":")` and sorting numerically on decimal frame followed by hexadecimal Morton value. This is the local sort operation; exact formatting guarantees of imported `ids.track_id` remain **ABSENT**. (`visioncore/gop.py:384-385`, `visioncore/gop.py:431-436`.)

| Track key | Type, value, and range | Source |
|---|---|---|
| `k` | Track identifier returned by `ids.track_id`; consumed as a string by the sort. Exact formatter implementation is **ABSENT**. | `visioncore/gop.py:384-387`, `visioncore/gop.py:434-436`, `visioncore/gop.py:451` |
| `first` | Parsed `frame` from the region that initialized this track. The code does not compare it with the current captured frame or enforce a local range; parser contract is **ABSENT**. | `visioncore/gop.py:384-386`, `visioncore/gop.py:451` |
| `last` | Actual captured frame number `f` at the latest encountered region on this track. Region-ID frame consistency and `first <= last` are not checked here. | `visioncore/gop.py:372`, `visioncore/gop.py:393`, `visioncore/gop.py:451` |
| `cls_mask` | Integer OR of `1 << layout.cls_id(reg["cls"])`, initialized to `0`; no local upper bound. Full class mapping is **ABSENT**. | `visioncore/gop.py:386`, `visioncore/gop.py:394`, `visioncore/gop.py:451` |
| `mv_mode` | Two-integer array. Most frequently encountered region `mv` tuple; ties choose lexicographically smallest tuple. Defaults to `[0,0]` if no tuple entries exist. No local range/clipping. | `visioncore/gop.py:395-396`, `visioncore/gop.py:449-452` |
| `period` | Integer `0` if no detected cycle rank intersects the track's accumulated tile ranks; otherwise minimum intersecting period, in `2..8` for this encoder. | `visioncore/gop.py:28`, `visioncore/gop.py:439-446`, `visioncore/gop.py:452`, `visioncore/gop.py:475-493` |
| `flags` | String array in the exact order `APPEAR`, `VANISH`, `SPLIT`, `MERGE`, `FROZEN`, `FLICKER`, `DRIFT`, `CYCLE`, `GLOBAL`, containing only members present in the internal set. No duplicates. | `visioncore/gop.py:31`, `visioncore/gop.py:453` |
| `n_tiles_max` | Integer maximum of the encountered `int(n_tiles)` values and initial `0`; nonnegative, with no local upper bound. | `visioncore/gop.py:386-387`, `visioncore/gop.py:397`, `visioncore/gop.py:453` |
| `r_rep` | Representative region ID string on a successfully constructed track. `_track_out` can emit `null` for an internal object with no representative, although every region-created track here gets a candidate. | `visioncore/gop.py:398-400`, `visioncore/gop.py:454` |

Representative candidates are `(int(r_sum), -actual_frame_number, region_id)` and are compared only on the first two tuple components. The representative therefore maximizes `r_sum`, then selects the earliest actual frame. Equal score and frame retain the first encountered candidate; because regions within a frame are sorted by ID, the first such region ID wins under that input order. The code does not compare the ID as a third tie-break component when replacing the representative. (`visioncore/gop.py:373`, `visioncore/gop.py:398-400`.)

For flag calculations, all region-selected ranks for each track/frame are concatenated and deduplicated. The track's `all_ranks` is the unique union across its frames. Each of the following conditions is evaluated per nonempty track/frame rank set and adds to a persistent track flag set; once added, the flag is retained for the GOP. (`visioncore/gop.py:401`, `visioncore/gop.py:414-430`.)

| `Flag` | Exact addition rule | Source |
|---|---|---|
| `APPEAR` | A new track after the GOP's first local frame, when `links is not None`. | `visioncore/gop.py:383-390` |
| `VANISH` | A previous frame region is absent from all source IDs in current link pairs and remains known in `track_of`. | `visioncore/gop.py:402-407` |
| `SPLIT`, `MERGE` | A directly extracted string ID in the corresponding link list is known in `track_of`. | `visioncore/gop.py:362-365`, `visioncore/gop.py:408-412` |
| `FROZEN` | `count(cls == 5) * 1000000 // selected_count >= 500000` in any nonempty track/frame selection. | `visioncore/gop.py:27`, `visioncore/gop.py:420-423` |
| `DRIFT` | At least one selected tile has `cls == 6`. | `visioncore/gop.py:424-425` |
| `FLICKER` | At least one selected tile has `cls == 4`, or a cycle is attached. | `visioncore/gop.py:426-427`, `visioncore/gop.py:443-446` |
| `GLOBAL` | `count((flags & 64) != 0) * 1000000 // selected_count >= 500000`; unlike `_global_triplet`, this denominator does not exclude `cls == 0`. | `visioncore/gop.py:27`, `visioncore/gop.py:420-429` |
| `CYCLE` | At least one detected cycle rank belongs to the track's union of selected ranks; also sets `FLICKER` and the minimum intersecting period. | `visioncore/gop.py:439-446` |

These explicit numeric class tests do not define the complete class enum. Numeric values for other classes and the `layout.cls_id` string conversion are **ABSENT**. (`visioncore/gop.py:16`, `visioncore/gop.py:394`, `visioncore/gop.py:421-429`.)

### Cycles

Cycle detection stacks the `xh64` and `cls` tile fields for the GOP. A hash is marked eligible when `cls >= layout.CLS_DIRTY_MIN`; when `first_hash_exact=True`, all positions of the first row are eligible. `encode_run` supplies that exemption only when the GOP starts at captured position zero, not for every GOP I-frame and not based on its frame number. A tile is active if any row has nonzero class. The size/meaning of `xh64` is not enforced here and its dtype definition is **ABSENT**. (`visioncore/gop.py:271`, `visioncore/gop.py:458-473`.)

Periods are tested in ascending order from `2` through `CYCLE_P_MAX = 8`, stopping once fewer than `2 * period` captured rows exist. At candidate current row `i`, a match requires equal hashes at `i` and `i - period`, unequal hashes at `i` and `i - 1`, and eligible hashes at all three rows. The detector finds contiguous true runs of these comparisons and emits a cycle for each run of length at least `period`. Periods count captured row positions, not numeric frame gaps. (`visioncore/gop.py:28`, `visioncore/gop.py:475-495`.)

The candidate-rank list for a period is calculated before scanning its runs, using the active and not-yet-assigned masks. Emission sets that rank assigned, which prevents it from being selected in later periods. Multiple qualifying disjoint runs for the same rank at the same selected period can still be emitted by the ongoing inner scan; the code does not stop after the first emitted run. Results sort by start-row position, then numeric rank, then period. (`visioncore/gop.py:473-496`.)

| Emitted cycle key | Type, value, and range | Source |
|---|---|---|
| `t` | Field ID returned by `ids.field_id(frames[k], morton_of_rank)` for the detected run's starting captured frame. Complete ID grammar is **ABSENT**. | `visioncore/gop.py:492` |
| `p` | Integer period, `2..8`. | `visioncore/gop.py:28`, `visioncore/gop.py:475`, `visioncore/gop.py:492` |
| `n` | Integer `(run + p) // p`, at least `2`; for this GOP no greater than `n_frames // p`. It is the computed count, not a separate measured duration. | `visioncore/gop.py:465`, `visioncore/gop.py:478-492` |
| `end` | Actual frame number `frames[k1 + p]`, inclusive; sparse numbering is preserved. | `visioncore/gop.py:487-493` |

The direct `find_cycles` return value additionally contains integer `rank`, the tile's array rank. During detection it also contains integer `s = k`, but `s` is removed before `find_cycles` returns. `encode_run` uses `rank` to attach cycles to tracks and then removes it from every cycle before building the emitted `Gop`; neither `rank` nor `s` appears in a normal `gops.jsonl` cycle. (`visioncore/gop.py:271-274`, `visioncore/gop.py:439-446`, `visioncore/gop.py:492-499`.)

### Global-motion rows and event rows

`global` contains `[frame, dx, dy, share_ppm]` integer arrays for P-frames whose clipped `dx` or `dy` is nonzero. `frame` is the actual unsigned-32-bit-serializable frame number; `dx` and `dy` are each `-32768..32767`; `share_ppm` is `0..1000000` as defined for PHdr above. Rows retain ascending captured-frame order. No row is emitted for an I-frame, or for a P-frame with both motion components zero, even if its calculated share is nonzero. The corresponding PHdr still stores the share for each P-frame. (`visioncore/gop.py:200-208`, `visioncore/gop.py:218-228`, `visioncore/gop.py:253-258`, `visioncore/gop.py:279`, `visioncore/gop.py:507`.)

Internally each event is `(actual_frame, rank, name, id)`. `APPEAR` uses the parsed region Morton integer and the track ID. `VANISH` uses `int(previous_region_id.split(":")[1], 16)` and the track ID. A cut or flash appends rank `0`, name `CUT` or `FLASH`, and `ids.global_id(actual_frame)`. All event tuples are sorted lexicographically, then reduced to `[name, id]` arrays. Hence the serialized event does not retain an explicit event-time field or numeric sort rank. Only `APPEAR`, `VANISH`, `CUT`, and `FLASH` are appended by this code; split/merge and other track flags do not themselves create events. Duplicate tuples are not deduplicated. (`visioncore/gop.py:283-290`, `visioncore/gop.py:388-390`, `visioncore/gop.py:402-412`.)

### Decoder and blob splitting: actual validation boundary

`parse_header` returns a dictionary whose keys are exactly `magic`, `ver`, `hdr_len`, `gw`, `gh`, `tile_px`, `K`, `f0`, `n_frames`, `flags`, `izlen`, `body_len`, `key_sha8`, and `body_sha8`, with decoded wire values from the header table. `magic` is Python bytes, so this dictionary is not itself a JSON record. It verifies minimum header length, magic, version/header length, imported grid-axis bounds, and reserved flag bits. It does not verify frame-count positivity, `n_frames <= K`, tile size, payload lengths, or hashes. (`visioncore/gop.py:165-178`.)

`split_blobs` walks from offset zero, parses a header at each offset, computes `length = 48 + izlen + body_len`, and rejects a blob that would extend beyond the supplied bytes. It returns a Python list of `(offset, length)` tuples, despite its docstring using the word “yield”; empty input returns `[]`. It does not decompress, hash-check, or validate P-frame bodies. (`visioncore/gop.py:181-191`.)

`decode_gop` recomputes the grid-cell count from `gw`/`gh`; takes the compressed I-frame slice of `izlen` bytes; treats all remaining bytes as the body; and requires the latter length to equal `body_len`. It decompresses the I-frame, requires uncompressed length `cell_count * T`, and verifies both SHA-256 eight-byte prefixes. The initial decoded row is `(header.f0, raw_I_bytes)`. (`visioncore/gop.py:294-314`.)

For each of `n_frames - 1` P-frames, it verifies a complete PHdr and zero pads, then reads one `<u8` mask word per present Morton group. Bits are visited from least significant set bit upward within each group. A mask bit must correspond to an existing Morton cell, and total selected indexes must equal `n_dirty`. The delta may not overrun the body and must RLE-decode to exactly `T * n_dirty` bytes; those bytes are XORed into the committed reference. The emitted tuple for that row is `(t, ref.tobytes())`. At the end, any unconsumed body bytes cause rejection. Its final return is `(header_dict, frame_tuple_list)`. (`visioncore/gop.py:315-345`.)

The decoder does not recheck timestamp monotonicity, ppm ranges, motion semantics, `K`, or a positive `n_frames`. In particular a zero `n_frames` skips the P-frame loop but still starts with one I-frame tuple; the actual body must then be empty. A truncated mask buffer can fail inside the direct NumPy `frombuffer` call; there is no surrounding conversion of that failure into a custom GOP error. These are visible validation limits, not claims that malformed files satisfy an external schema. (`visioncore/gop.py:165-178`, `visioncore/gop.py:311-345`.)

| Failure checked here | Exception construction / observed code path | Source |
|---|---|---|
| Short/bad main header, wrong/truncated blob length, wrong I-frame size, truncated PHdr, trailing P-body bytes | `E_GOP_MAGIC` through `_gerr`. | `visioncore/gop.py:165-170`, `visioncore/gop.py:187-188`, `visioncore/gop.py:301-308`, `visioncore/gop.py:316-317`, `visioncore/gop.py:343-344` |
| Version or header-size mismatch | `E_GOP_VERSION` through `_gerr`. | `visioncore/gop.py:171-172` |
| Invalid parsed grid dimensions | `E_GOP_GRID` through `_gerr`. | `visioncore/gop.py:173-174` |
| Reserved flag bits or nonzero PHdr pads | `E_GOP_PAD` through `_gerr`. | `visioncore/gop.py:175-176`, `visioncore/gop.py:318-320` |
| Invalid mask Morton code or popcount | `E_GOP_MASK` through `_gerr`. | `visioncore/gop.py:329-335` |
| Encoded RLE exceeds bound | `E_DELTA_BOUND` through `_gerr`. | `visioncore/gop.py:63-66` |
| Incomplete RLE header, literal overrun, or delta body overrun | `E_DELTA_TOKEN` through `_gerr`. | `visioncore/gop.py:69-75`, `visioncore/gop.py:336-338` |
| Wrong decoded RLE length | `E_DELTA_LEN` through `_gerr`. | `visioncore/gop.py:79-82` |
| Zlib exception or mismatched SHA-256 prefix | Direct `VqaError("E_GATE", ..., rc=5)`, detail reason `g_blob_integrity`; a zlib message is truncated to 60 characters. | `visioncore/gop.py:303-310` |

`_gerr` constructs `VqaError(code, detail, rc=2)`. The exception class, any stdout/error JSON serializer, and exit handling are imported and **ABSENT** from the bound implementation; the table specifies exception construction, not an independently established wire error schema. (`visioncore/gop.py:17`, `visioncore/gop.py:34-35`.)

## Pack outputs and public result objects

This section describes executable behavior in visioncore/pack.py. Its public construction entry point is **pack(batch, cap_bytes=None, shuffle_window=None, batch_no=0)**, which returns a **PackResult** holding **pack**, **files**, and **acc**; constructing it does not itself write these files to disk. The return and the three attributes are implemented at `visioncore/pack.py:510-512,556,672-673`. The name **pack_tokens** is **ABSENT**: there is no such definition, call, or import in the four bound files. The actual token functions and dispatch budget are specified below from `visioncore/pack.py:444-455,619-636,730-780`.

### Output file inventory

| File or path | Presence and exact content | Source |
|---|---|---|
| `z1.txt` | Always in a successful pack's file map. The fixed ASCII encoding of the multiline _Z1 literal, including its final LF. Its first line is VISION PACK LEGEND (z1.1). | `visioncore/pack.py:37-69,637` |
| heatmap_g&lt;g&gt;.png | One entry per retained heatmap GOP; name is the literal concatenation "heatmap_g" + str(g["g"]) + ".png". PNG bytes come from heatmap_png. There are at most four selected GOP entries; generation requires a non-None tiles provider. Duplicate names are not rejected and ordinary file-map assignment overwrites the prior value. | `visioncore/pack.py:22,601-604,639-646` |
| crops/&lt;representative-region-id with ":" replaced by "_"&gt;_before.png | Before crop for each retained track crop item, with its original image pixels and original region tile box. The name uses the possibly shifted representative region ID. There are at most six crop items before token fitting; each generates two crop entries. Repeated file names are assigned again without a uniqueness check. | `visioncore/pack.py:23,596-600,647-657` |
| crops/&lt;representative-region-id with ":" replaced by "_"&gt;_after.png | After crop using the same representative region box at the representative region's encoded frame. | `visioncore/pack.py:647-657` |
| `mp4_frames.json` | Always present: canonical UTF-8 JSON, including the canonical terminal LF, for the frame-selection object described below. | `visioncore/pack.py:658-659`; `visioncore/canon.py:27-29` |
| `pack.json` | Always present: canonical UTF-8 JSON, including the canonical terminal LF, for the pack object described below. | `visioncore/pack.py:660-673`; `visioncore/canon.py:27-29` |
| &lt;ResponseCache.root&gt;/&lt;first two hex digits&gt;/&lt;request SHA-256&gt;.resp | Written only by ResponseCache.put, including through judge_batch on a successful cache miss. Raw bytes(data), without a response-format wrapper. This is separate from PackResult.files. | `visioncore/pack.py:704-711,724-727,741-754` |
| Actual MP4, exported original frames, separate V3 JSON, separate accumulator file, request file, or receipt file | **ABSENT** from these writers. The MP4-related artifact is a selection manifest; request and receipt values are returned in memory. | `visioncore/pack.py:637-673,680-697,752-754` |

The primary pack field **sha256** values below are strings produced by canon.sha256_bytes, which returns hashlib.sha256(bytes).hexdigest(); they describe the bytes actually supplied to that function, including canonical JSON's final LF where present. (`visioncore/canon.py:50-55`; `visioncore/pack.py:646,656,662,665`.)

### pack.json object

The table distinguishes enforced conversions from values copied from callers or unbound dependencies. The file is not validated against a schema by pack(). Canonical encoding rejects floats and non-string object keys recursively, but the bound files do not supply a comprehensive pack schema validator. (`visioncore/pack.py:660-673`; `visioncore/canon.py:13-29`.)

| Key | JSON value, presence, type, and constraints | Source |
|---|---|---|
| `schema` | Always the string "vqa.pack/v2". | `visioncore/pack.py:661` |
| `batch` | Always int(batch_no); default batch_no is 0. No minimum or maximum is checked here. | `visioncore/pack.py:556,661` |
| `grid` | Always the value returned by layout.grid_ident(W,H). Its complete object schema, values, and validation are **ABSENT** because layout.py is not bound. | `visioncore/pack.py:8,565-567,661` |
| `params_sha256` | Always batch["params_sha256"], copied without hash-shape or type validation here. Its name alone does not impose a 64-character constraint. | `visioncore/pack.py:661` |
| `z1_version` | Always the string "z1.1". | `visioncore/pack.py:12,662` |
| `z1_sha256` | SHA-256 string of exact z1.txt bytes. | `visioncore/pack.py:662`; `visioncore/canon.py:50-51` |
| `z1_bytes` | Integer len(z1); the ASCII legend's byte length, checked against PARAMS["Z1_MAX_B"] by z1_text. That parameter's actual value is **ABSENT**. | `visioncore/pack.py:64-69,662` |
| `cap_bytes` | The accepted effective cap: explicit cap_bytes or PARAMS["PACK_BATCH_B"]. pack checks isinstance(cap,int) and cap >= 1. Python True passes this initial check as a cap of 1, but even the empty V3 envelope {"g":[]} measures 8 bytes, so normal fitting then rejects that cap; False fails the initial check. A successful pack therefore does not emit the admitted True value. There is no explicit upper-bound check. | `visioncore/pack.py:337-352,367-368,561-563,663`; `visioncore/canon.py:13-29` |
| `gop_cap_bytes` | PARAMS["PACK_GOP_B"], copied. Its actual value and registry constraints are **ABSENT**. | `visioncore/pack.py:590,663` |
| `v3_bytes` | Integer UTF-8 byte length of canonical v3 excluding its single terminal LF. It measures only the nested V3 object, not pack.json or the complete file set. | `visioncore/pack.py:351-352,590-591,663` |
| `v3` | Object specified below, with the sole outer key g. | `visioncore/pack.py:337-348,663` |
| `v1` | Array, possibly empty, of heatmap descriptor objects; at most four selected descriptor entries. | `visioncore/pack.py:22,601-604,638-646,664` |
| `v2` | Array, possibly empty, of crop-pair objects; at most six selected descriptor entries. | `visioncore/pack.py:23,596-600,638,647-657,664` |
| `frame_map` | Array of [enumeration index, selected capture frame] pairs. It equals mp4_frames.json's frame_map. | `visioncore/pack.py:440-441,658,664` |
| `gops` | Array of ids.gop_id(int(g["g"])) for every original sorted GOP, independently of heatmap selection. The formatter's exact accepted ranges are **ABSENT**. | `visioncore/pack.py:564,664` |
| `est_tok` | Current integer-estimator result with accepted inputs, after the media ladder; a successful pack requires it <= _TOK_HARD, which equals _TOK_TARGET. This is an estimate, not observed provider usage. | `visioncore/pack.py:13-21,619-636,665` |
| `mp4_frames_sha256` | SHA-256 string of the exact mp4_frames.json bytes, including its final LF. | `visioncore/pack.py:659,665`; `visioncore/canon.py:27-29,50-51` |
| `placebo` | Optional object with sx and sy, included exactly when shuffle_window is not None. Both are computed integers; under a positive grid, 1 <= sx <= gw-1 and 0 <= sy < gh. | `visioncore/pack.py:578-586,667-668` |
| `crop_frames` | Always a sorted, duplicate-free array of the frame values in every emitted crop pair's before and after descriptors. Empty when no crop pairs are emitted. | `visioncore/pack.py:669` |
| `check_png_max_side` | Optional integer 768, included only when the final check-image sizing step was activated. pack computes revised dimensions and advertises this limit; no check PNG is written or resized by pack. | `visioncore/pack.py:17,609-617,631-632,670-671` |

**v1 element:** {g, file, sha256}. The g value is g["g"] without int conversion; file is the constructed heatmap file-name string; sha256 is the exact PNG's hash. The input normally comes from GOP records, but pack does not independently impose their g type/range. (`visioncore/pack.py:639-646`.)

**v2 element:** {item, region, before, after}. item is the selected track item's ID; region is the track's representative region ID, possibly shifted. Both are copied from the item/track. Each of before and after is an object with precisely the following fields. (`visioncore/pack.py:648-657`.)

| Crop-side key | Value and constraints | Source |
|---|---|---|
| `file` | Constructed crop PNG path string. | `visioncore/pack.py:654-656` |
| `frame` | For after, the frame parsed from r_rep. For before, the largest captured frame strictly below that frame, across the entire batch; if none exists, the same frame as after. Parser validation and allowed frame range are **ABSENT**. | `visioncore/pack.py:241-242,576,649,651-652,656` |
| `box` | The crop function's [x0,y0,x1-x0,y1-y0] original-pixel rectangle before downscaling/trimming. With integer tile coordinates and tile size it is four integers; width and height must be positive to avoid the explicit error. | `visioncore/pack.py:221-237,653,656` |
| `scale` | The final integer box-reduction factor: initially ceil(max(width,height)/256), but reset to 1 if block trimming would leave either side empty. There is no independent maximum. | `visioncore/pack.py:24,228-237,656` |
| `sha256` | SHA-256 string of this crop's exact PNG bytes. | `visioncore/pack.py:656`; `visioncore/canon.py:50-51` |

**mp4_frames.json:** exactly {frames, frame_map, must_keep}. frames is the sorted selected-frame array; frame_map is [[0,frames[0]],[1,frames[1]],...]; must_keep is the count of unique mandatory frames retained before filling with dirty-frame candidates. With ordinary nonnegative slots, selected length is <= slots, and must_keep <= selected length. The pack caller starts slots at 12. No video is encoded here and no FPS metadata key is emitted. (`visioncore/pack.py:25,392-441,575,605,658-659`.)

**PackResult.acc:** a returned Python dictionary, not an emitted JSON file. It starts as a copy of the caller's accumulator, retains pre-existing keys, and sets each current item's ID to 0 if fitted at level 0; otherwise to prior acc.get(id,0) + item weight. This happens before media removal, so later crop/heatmap/frame decisions do not recalculate this dictionary. (`visioncore/pack.py:510-512,569,592-594,623-636,673`.)

### Input normalization and boundary of validation

pack obtains W and H directly, asks layout.grid_params and layout.grid_ident for grid information, sorts batch["gops"] by int(g["g"]), accepts an existing regions dictionary unchanged or maps a regions list to {row["id"]:row}, and shallow-copies the supplied nonempty accumulator. Missing/falsy regions become an empty mapping; duplicate list IDs keep the last assigned row. The numeric conversion used for GOP sorting does not replace each original g field. (`visioncore/pack.py:549-552,564-570`.)

Before fitting, every track in every original GOP must have tr.get("r_rep") present as a key in regions. A missing representative, including missing None unless that key exists, raises E_SCHEMA; this check occurs even if the track would stay at level 2 or its crop would be removed. Other complete input schemas and coercion/validation behavior in ids, layout, cite, PARAMS, and VqaError are **ABSENT** from the bound sources. (`visioncore/pack.py:8-10,571-574`.)

The capture timeline of a GOP is list(g.get("frame_ids", range(int(g["f0"]), int(g["f0"])+int(g["n_frames"])))). Thus an existing frame_ids sequence is copied without per-element conversion; otherwise the fallback is dense. Python evaluates that fallback expression while making the get call, so f0 and n_frames are still accessed when frame_ids is present. The batch capture set is sorted and deduplicated. (`visioncore/pack.py:386-388,397,576`.)

For canonical serialization's supported scalar types, a copied value is not necessarily an integer just because its field name or an upstream comment suggests one; the pack functions only perform the explicit conversions and checks identified here. Floats encountered in serialized objects fail in canon rather than being rounded. (`visioncore/pack.py:660-672`; `visioncore/canon.py:13-29`.)

## V3 structure, priorities, and byte fitting

### V3 objects and tuple layouts

The outer V3 object is **{g: [block,...]}**, with one block per supplied GOP in supplied order. pack passes its sorted (possibly shifted) GOP list. Each block always has **g, C, M, F, Z, D, X** and has **i** only when at least one item is encoded below level 2. It contains no explicit item-level field. The helper's with_items parameter is never read. (`visioncore/pack.py:337-348,564,586,590`.)

| Block key | Value and count rule | Source |
|---|---|---|
| `g` | The GOP's g value, copied without conversion. | `visioncore/pack.py:340-342` |
| `C` | Integer sum over all GOP tracks of (int(tr.get("cls_mask",0)) >> 2) & 1; a missing cls_mask defaults to 0, while an explicit null is not replaced. | `visioncore/pack.py:321-324` |
| `M` | Integer sum of (int(cls_mask default 0) >> 3) & 1. GLOBAL does not remove a MOVED track from this count. | `visioncore/pack.py:323-325` |
| `F` | One per track having class-mask bit 4 or either FLICKER or CYCLE in flags. | `visioncore/pack.py:323,326` |
| `Z` | One per track having class-mask bit 5 or the FROZEN flag. | `visioncore/pack.py:323,327` |
| `D` | One per track having class-mask bit 6 or the DRIFT flag. | `visioncore/pack.py:323,328` |
| `X` | Count of frame-wide candidate items for that GOP, including level-2 items that are not encoded in i. | `visioncore/pack.py:329,340-344` |
| `i` | Optional array of encoded track/frame-wide tuple arrays, in strict priority order; omitted when that array is empty. | `visioncore/pack.py:344-347,360-361` |

C/M/F/Z/D count all source tracks, not just displayed items. Each is between 0 and the number of tracks, and a track can contribute to several counters. X counts all matching frame-wide candidates; IDs are deduplicated during candidate generation for each source GOP, not across duplicate GOP identifiers. Levels change visibility, not these counts. (`visioncore/pack.py:276-293,320-330,337-348,362-382`.)

| Item kind and level | Exact ordered JSON-array positions | Source |
|---|---|---|
| Track, L0 | [k, r_rep, label, list(region["bbox_px"]), int(n_tiles_max), mv, int(period default 0), list(flags default []), quote] | `visioncore/pack.py:309-317` |
| Track, L1 | [k, int(first), int(last), label, mv] | `visioncore/pack.py:309-312` |
| Frame-wide, L0 | [id, label, quote] | `visioncore/pack.py:306-308` |
| Frame-wide, L1 | [id, label] | `visioncore/pack.py:306-308` |
| Either, L2 | No tuple in i. The block and its counters still exist. | `visioncore/pack.py:337-348,362` |

Track k and r_rep are copied IDs. mv is list(tr.get("mv_mode") or [0,0]); pack does not validate its length, bounds, or element types. The bbox is list(reg["bbox_px"]) with no coordinate transformation in ordinary encoding; its length/range are not checked here. n_tiles_max, first, last, and period have int conversions, without additional minimum or maximum checks in _encode. flags is copied as a list in its supplied order. L0 fails with E_SCHEMA if r_rep does not resolve to a region. (`visioncore/pack.py:309-317`.)

**Coordinate semantics conflict:** the emitted Z1 legend advertises L0 bbox as [x,y,w,h], but executable _encode copies bbox_px unchanged, while the optional shift helper treats both bbox arrays as [x0,y0,x1,y1]. Therefore a universal interpretation of emitted V3 bbox_px as width/height, or an implicit conversion to it, is **ABSENT**. Crop-side box is separately and explicitly [x0,y0,width,height]. (`visioncore/pack.py:47,316,470-476,489-491,237`.)

quote is the return value of unbound cite.render_quote, with no type or byte-length check in this module. For frame-wide items, pack parses the ID and provides a host dictionary whose frames key maps the parsed frame to {n: []}, and whose gops value is an empty list. For tracks, the host is {gops:[{tracks:[the track]}], regions:the regions mapping}. Exact quote strings and citation validation are **ABSENT** because cite.py and ids.py are not bound. The legend's examples do not implement quote generation. (`visioncore/pack.py:55-59,296-303,308,317`.)

### Candidate creation and ranking

Each track produces an internal item with keys **id=tr["k"], kind="track", w, lab, first=int(tr["first"]), mm=parsed Morton component of k, tr=original track, g=GOP g**. Each frame-wide item has **id, kind="frameall", w, lab, first=frame, mm=0, g**. These are internal selection dictionaries, not additional emitted file objects. (`visioncore/pack.py:245-247,267-273,292-293`.)

Track labels choose the highest-weight candidate, then lexicographically smallest label on equal weights. Flags are first made a set; unrecognized flags add no candidate. A track with no candidate gets weight 1 and label CHANGED. (`visioncore/pack.py:249-264`.)

| Candidate source | Weight and output label | Source |
|---|---|---|
| FLASH flag; CUT flag | 9, FLASH; 9, CUT | `visioncore/pack.py:32-33,254-256` |
| FLICKER flag; CYCLE flag; FROZEN flag | 8, FLICKER; 8, FLICKER; 8, FROZEN | `visioncore/pack.py:32-33,254-256` |
| APPEAR flag; VANISH flag | 7, APPEAR; 7, VANISH | `visioncore/pack.py:32-33,254-256` |
| DRIFT flag; GLOBAL flag | 6, DRIFT; 2, GLOBAL | `visioncore/pack.py:32-33,254-256` |
| cls_mask bit 2 | 3, CHANGED | `visioncore/pack.py:257-260` |
| cls_mask bit 3 | 4, MOVED, or 2 when GLOBAL is in flags | `visioncore/pack.py:257-260` |
| cls_mask bit 4; bit 5; bit 6 | 8, FLICKER; 8, FROZEN; 6, DRIFT | `visioncore/pack.py:258-260` |

Frame-wide candidates are formed from truthy g.cut at int(g.f0), FLASH events at the frame parsed from ev[1], and global rows whose second or third element is truthy, at int(row[0]). Only these three sources are examined. They use weights CUT=9, FLASH=9, GLOBAL=2. The dictionary is keyed by ids.global_id(frame): a later candidate replaces an existing one only if its weight is strictly higher. Thus a same-frame CUT inserted before FLASH keeps CUT on their equal-weight tie. Final frame-wide discovery order is sorted by the ID-string key, not explicitly numerically by frame. (`visioncore/pack.py:34,276-293`.)

The strict fitting/crop order key is **(-(weight + acc.get(id,0)), first, mm, id)**. Accumulator types and bounds are not validated. The fitted item's level map is keyed by ID, so duplicate IDs share one level entry; no uniqueness check is implemented. Duplicate GOP g values are also not rejected: _v3 associates every matching candidate with each such block, and the promotion cap check examines only the first matching block. This code does not enforce the cap of every duplicate-g block after each promotion. (`visioncore/pack.py:333-344,360-362,374-380,564,595-597`.)

### Greedy fitting and cap accounting

The measured size of any V3 object/block is len(canon.dumps(object).encode("utf-8")) - 1, removing exactly the canonical terminal LF. Each block is measured as its own object; the overall V3 measurement includes the outer g key and surrounding array punctuation. The total pack JSON and media bytes are not compared against these caps. (`visioncore/pack.py:337-352,363-368,663,672`.)

1. Sort items by the strict key and put every distinct item ID at L2. Build each all-L2 block; if its size exceeds gop_cap, raise E_BUDGET with part v3, gop, and cap. Check the all-L2 outer V3 against cap_bytes and raise E_BUDGET if it exceeds that cap. Equality is accepted. (`visioncore/pack.py:360-368`.)
2. Run a promotion pass targeting L1, then a pass targeting L0. Consider an item only if its current level is exactly target+1. Tentatively promote it and rebuild the entire V3. (`visioncore/pack.py:369-375`.)
3. Select the first V3 block whose g equals the item's g (the code uses [matching_blocks][0]). If that block exceeds gop_cap, undo only that promotion and continue to the next item. If its block fits but the batch V3 exceeds cap_bytes, undo that promotion and break the current pass. The later target pass still runs; a level-2 item skipped during the L1 pass cannot jump directly to L0. (`visioncore/pack.py:371-381`.)
4. Return the final V3 plus the level dictionary. Encoded items inside each block inherit global strict-key ordering. (`visioncore/pack.py:360-361,382`.)

## Frame selection and media fitting

### Selection manifest algorithm

select_frames(gops, slots=12) returns ([],[],0) for empty input. Otherwise it forms the sorted unique union of each GOP's capture timeline. For each GOP, it zips that timeline with dirty_ppm (default []), converts each matched dirty value to int, and assigns it by frame; unmatched timeline frames acquire no fill candidate from that GOP and later assignments for the same frame replace earlier ones. (`visioncore/pack.py:25,386-403`.)

Mandatory selection walks the following tiers in order. Each tier is deduplicated and sorted, then contributes only frames in the captured union and not already selected. The priorities determine candidate encounter order; they do not authorize dropping lower-priority mandatory frames when slots is too small. (`visioncore/pack.py:398,404-434`.)

| `Tier` | Candidate frames | Source |
|---|---|---|
| 0 | int(g.f0) for a truthy cut; frame parsed from every FLASH event's ev[1]. | `visioncore/pack.py:404-408` |
| 1 | Frames parsed from APPEAR or VANISH event ev[1]; for a VANISH track, the smallest captured frame greater than int(track.last), if any; for an APPEAR track, int(track.first). | `visioncore/pack.py:409-420` |
| 2 | Frame parsed from each cycles element's t ID. | `visioncore/pack.py:425-426` |
| 3 | int(track.first) for a FROZEN flag. | `visioncore/pack.py:421-422` |
| 4 | int(track.first) for a DRIFT flag. | `visioncore/pack.py:423-424` |
| 5 | int(gops[0].f0), only the first GOP's first frame. | `visioncore/pack.py:427` |

After all tiers, must_n is the number selected. If must_n > slots, the function raises E_BUDGET for mandatory_frames; it does not truncate the mandatory list. Otherwise unused captured frames with recorded dirty values fill remaining capacity in descending dirty value, ascending frame order. The completed selection is sorted ascending, and frame_map is formed by enumeration. No lower bound on the actual returned length is imposed. (`visioncore/pack.py:434-441`.)

Event selection uses the frame encoded in an event's ID, not an independently stored event time. The VANISH-track rule is an additional search for the next captured frame after last, so these are distinct selection sources. Parser grammar/ranges are supplied by ids.py and are **ABSENT** here. (`visioncore/pack.py:241-242,406-420`.)

### Initial media candidates

Crop candidates are every ordered track item whose r_rep is in the regions map and whose region has a truthy bbox_t, independently of its V3 level. No frames provider (batch.get("frames") is None) yields no crop candidates. The first six candidates survive the initial limit. (`visioncore/pack.py:23,595-600`.)

All GOPs are initially eligible for heatmaps when the tiles provider is non-None; otherwise none are. If there are more than four, each GOP's ranking weight is the maximum raw item weight in that GOP, or 0 if it has no item. Select four by descending maximum weight and then g, and re-sort those four by g. Accumulated starvation does not enter this heatmap ranking. (`visioncore/pack.py:22,601-604`.)

### Constants and estimator

| Name | Executable value or dependency | Source |
|---|---|---|
| `_TOK_IMG` | 258 | `visioncore/pack.py:14` |
| `_TOK_IMG_SMALL_PX` | 384 | `visioncore/pack.py:15` |
| `_TOK_TILE_PX` | 768 | `visioncore/pack.py:16` |
| `_CHECK_LADDER_PX` | 768 | `visioncore/pack.py:17` |
| _TOK_TARGET and _TOK_HARD | _TOK_TARGET is PARAMS["PACK_CALL_TOK"] at import time; _TOK_HARD equals that value. Actual numeric cap is **ABSENT**. | `visioncore/pack.py:18,21` |
| `_RETRY_MAX` | PARAMS.get("PACK_RETRY_MAX",2), captured at import time. Only fallback 2 is bound; configured value is **ABSENT**. | `visioncore/pack.py:19-20` |
| MAX_HEATMAPS; MAX_CROP_PAIRS | 4; 6 | `visioncore/pack.py:22-23` |
| `CROP_SIDE` | 256 | `visioncore/pack.py:24` |
| MP4_SLOTS; MP4_SLOTS_MIN | 12; 8 | `visioncore/pack.py:25-26` |

For normal positive integer dimensions, **tok_image(w,h)** is 258 if max(w,h) <= 384; otherwise **258 × ceil(w/768) × ceil(h/768)**, using integer ceiling division. tok_image itself does not validate sign or type. This is local estimator arithmetic; the comments describe the model as assumed, so the bound code does not establish agreement with provider metering. (`visioncore/pack.py:13-16,444-448`.)

**est_tokens(z1_b,v3_b,n_images,mp4_s,text_b=0,image_wh=())** returns **ceil(z1_b/4) + ceil(v3_b/3) + 258×(n_images+mp4_s) + ceil(text_b/4) + sum(tok_image(w,h) for image_wh)**. The function has no input-type/range checks of its own. (`visioncore/pack.py:451-455`.)

Within pack, n_check_png and text_b default to 0 and must satisfy type(value) is int and value >= 0, thereby rejecting booleans. check_png_wh becomes a list of tuple-converted caller entries when that is nonempty; otherwise it is [(W,H)] repeated n_check_png times. A nonempty explicit list need not have length n_check_png. The function does not separately validate those pairs' shape, element type, or sign. (`visioncore/pack.py:606-609`.)

The heatmap's estimated dimensions are **(grid_width×cell_px(grid_width), grid_height×cell_px(grid_width))**, with **cell_px(gw)=max(4,768//gw)**. Its estimator image list is the current check-image dimensions followed by one such pair per retained heatmap. Crops count as exactly two flat-charge images per pair, regardless of their actual encoded dimensions or fallback size. The MP4 estimator term is the number of selected frame-map entries. V3 bytes are fixed by the earlier greedy fit. (`visioncore/pack.py:146-147,591,610-621`.)

### Media reduction ladder

While the estimate exceeds _TOK_TARGET, perform exactly the first available operation below, then recompute the estimate. (`visioncore/pack.py:623-634`.)

1. Remove the last crop item in the ordered crop list. (`visioncore/pack.py:624-625`.)
2. If more than one heatmap remains, remove the last heatmap GOP. Because the selected heatmaps were sorted by g, this is the last g in that list, not a fresh minimum-weight choice. A nonempty heatmap list never falls below one by this ladder. (`visioncore/pack.py:604,626-627`.)
3. If slots > max(8,must_n), decrement slots and rerun select_frames on the original GOPs. The number 8 limits the slots variable; it does not guarantee eight actual frames, since fewer captured/dirty candidates can exist. (`visioncore/pack.py:26,435-441,628-630`.)
4. If check-image reduction has not yet happened and any original check dimension pair has max(w,h)>768, set check_png_max_side to 768. For such a pair use **(max(1,w×768//max(w,h)), max(1,h×768//max(w,h)))** in estimation; already-small pairs are unchanged. (`visioncore/pack.py:17,613-617,631-632`.)
5. Otherwise stop the loop. If the estimate is still above _TOK_HARD, raise E_BUDGET with part tokens, est_tok, and cap. Since hard equals target, there is no separate higher allowed ceiling. (`visioncore/pack.py:18-21,633-636`.)

The ladder does not lower V3 levels, refit V3 to a smaller byte cap, recalculate the accumulator, or generate a reduced check PNG. The V3/accumulator work precedes it; output files are constructed only after the hard-estimate check succeeds. The observable behavior is a returned pack or an exception, not a partially returned PackResult. (`visioncore/pack.py:590-594,623-637,670-673`.)

## PNG byte format, heatmap rendering, and crops

### PNG container

png_encode converts its argument to a contiguous NumPy uint8 array. It requires exactly three dimensions, channel dimension 3, and positive height and width; it does not require the original argument already to have dtype uint8. It prepends one zero byte to each RGB row (filter 0), uses an IHDR containing big-endian width/height and bytes [8,2,0,0,0], compresses the concatenated rows at zlib level 6, and emits precisely signature + IHDR + IDAT + IEND. No text/time/other chunks are emitted. (`visioncore/pack.py:78-88`.)

Each chunk is four-byte big-endian data length, tag bytes, data, then four-byte big-endian **zlib.crc32(tag+data) & 0xffffffff**. The PNG signature bytes are **89 50 4e 47 0d 0a 1a 0a**; IEND has empty data. The bound code fixes these choices but does not pin NumPy/zlib implementations or prove compressed-byte identity across library versions. (`visioncore/pack.py:73-88`.)

has_forbidden_colour is a separate detector returning bool(any(R>=200 and B>=200 and G<=60)); neither png_encode nor pack invokes it. A successful PNG construction is therefore not evidence that this detector ran. (`visioncore/pack.py:78-94,637-673`.)

### Heatmap geometry and selection

heatmap_png obtains s, gw, gh from layout.grid_params(W,H), rejects an empty tile-frame collection, takes its Morton sequence exclusively from the first tile frame, and requires every frame to have that same number of records. It does not compare later Morton columns against the first. Class values are converted to NumPy int64 and the explicit class check rejects max(cls)>6; no explicit lower-bound class check appears. The underlying tile dtype, Morton functions, and grid validation are **ABSENT** from the bound files. (`visioncore/pack.py:151-172,185`.)

The following constants define painted classes. Classes whose lookup weight is zero produce key 0 and do not paint; the explicit dictionary defines positive weights only for IDs 2 through 6. (`visioncore/pack.py:28-31,160-175`.)

| Class ID | Weight | RGB colour | Source |
|---|---|---|---|
| 2 | 3 | (230,25,25) | `visioncore/pack.py:30-31` |
| 3 | 4 | (30,90,230) | `visioncore/pack.py:30-31` |
| 4 | 8 | (250,220,0) | `visioncore/pack.py:30-31` |
| 5 | 8 | (0,200,220) | `visioncore/pack.py:30-31` |
| 6 | 6 | (255,140,0) | `visioncore/pack.py:30-31` |
| Background | — | (32,32,32) | `visioncore/pack.py:28` |
| Macrogrid line | — | (80,80,80) | `visioncore/pack.py:106` |
| Morton label | — | (190,190,190) | `visioncore/pack.py:105` |
| Motion stroke | — | (255,255,255) | `visioncore/pack.py:107` |

For each record, the class key is weight×8+class when weight>0, else 0. A frame replaces the winning class and its motion whenever the new key is >= the previous key and positive. Thus higher class IDs win equal-weight class ties, and the latest supplied frame wins an identical-key motion tie. The active counter increments for cls>=2. (`visioncore/pack.py:173-179`.)

The image has height gh×c, width gw×c, three channels, where c=max(4,768//gw). It starts at background RGB. For each first-frame Morton coordinate (tx,ty) whose key is nonzero, fill its c-by-c cell with, per channel, **BG + (class_colour-BG)×active_count//number_of_frames**. This is integer floor arithmetic; no gamma transformation is implemented. With the accepted ordinary nonnegative classes, active_count is from 0 through number_of_frames. (`visioncore/pack.py:146-147,180-192`.)

Next draw vertical grid lines at columns g×c for g=8,16,...<gw and horizontal lines at rows g×c for g=8,16,...<gh. For a winning class 3 with nonzero motion, set radius=max(1,c//2-1), center=(tx×c+c//2,ty×c+c//2), and each endpoint displacement to clamp(mv_component×c//s,-radius,radius), replacing a zero result with sign(original motion component). _sgn is (v>0)-(v<0). (`visioncore/pack.py:142-143,193-204`.)

The motion stroke's integer stepping is fixed: dx=abs(x1-x0), dy=-abs(y1-y0), sx/sy are +1 when the start coordinate is below the end and -1 otherwise, and err=dx+dy. At each iteration, paint if in bounds; stop at the endpoint; otherwise e2=2×err, advance x with err+=dy when e2>=dy, and advance y with err+=dx when e2<=dx. These two comparisons can both advance on the same iteration. (`visioncore/pack.py:122-139`.)

Finally, visit each first-frame Morton record in sequence and draw one label per unseen macroblock m>>6. Its origin is unmorton((m>>6)<<6), offset by one pixel in x and y; text is "%04x" % ((m>>6)<<6). Four is a minimum formatting width, not an independently enforced maximum. Rendering order is cell fills, grid lines, motion strokes, then labels, so later layers overwrite earlier pixels. (`visioncore/pack.py:184-213`.)

The font has three columns, five rows, four-pixel character advance; each "1" paints at x+character_index×4+column,y+row only if inside the image. Unsupported characters are not given a fallback glyph. The literal glyph bit strings are below, read row-major. (`visioncore/pack.py:98-119`.)

| Glyph | Bits | Glyph | Bits | Source |
|---|---|---|---|---|
| 0 | 111101101101111 | 1 | 010110010010111 | `visioncore/pack.py:99` |
| 2 | 111001111100111 | 3 | 111001111001111 | `visioncore/pack.py:99` |
| 4 | 101101111001001 | 5 | 111100111001111 | `visioncore/pack.py:100` |
| 6 | 111100111101111 | 7 | 111001001010010 | `visioncore/pack.py:100` |
| 8 | 111101111101111 | 9 | 111101111001111 | `visioncore/pack.py:101` |
| `a` | 010101111101101 | b | 110101110101110 | `visioncore/pack.py:101` |
| `c` | 011100100100011 | d | 110101101101110 | `visioncore/pack.py:102` |
| `e` | 111100110100111 | f | 111100110100100 | `visioncore/pack.py:102` |
| : | 000010000010000 | — | — | `visioncore/pack.py:103` |

### Crop pixel arithmetic

crop_png(rgb,bbox_t,s,grow=1) treats bbox_t as inclusive tile endpoints (tx0,ty0,tx1,ty1). It computes **x0=max(0,(tx0-grow)×s)**, **y0=max(0,(ty0-grow)×s)**, **x1=min(W,(tx1+1+grow)×s)**, and **y1=min(H,(ty1+1+grow)×s)**. If x1<=x0 or y1<=y0, it raises E_VC_ID_RANGE with the original bbox_t converted to a list. Type/range validation beyond that condition is absent in this function. (`visioncore/pack.py:218-227`.)

The selected pixel array is converted to uint8. Let side=max(x1-x0,y1-y0) and f=ceil(side/256). If f>1, compute hh=floor(height/f)×f and ww=floor(width/f)×f. If either is zero, reset f to 1 and keep the unreduced array: therefore a narrow crop can retain a side larger than 256. Otherwise truncate the bottom/right remainders, group f-by-f blocks, convert the blocks to uint32, sum with NumPy's default accumulator dtype (no dtype argument is supplied to sum), divide by f² with floor, and convert to uint8. The return is (PNG bytes, [x0,y0,x1-x0,y1-y0], f); the box still describes the original pre-trim rectangle. (`visioncore/pack.py:24,227-237`.)

The before/after crop uses the same original region bbox_t for both image frames. pack's default invocation supplies no explicit grow, so growth is one tile. In placebo mode, it resolves shifted r_rep back to the original region for the actual pixels; the path and descriptor region retain the shifted ID. (`visioncore/pack.py:218,648-656`.)

## Optional coordinate-shift path

The executable trigger is **shuffle_window is not None**, including values that convert to integer zero. pack rejects gw<2, converts the value with int(), and calculates **sx=1+seed%(gw-1)** and **sy=(seed//gw)%gh**. It stores exactly these two values in the optional placebo object. No other mode named Q or separate Q-format output is defined in pack.py; any broader Q meaning is **ABSENT**. (`visioncore/pack.py:578-586,667-668`.)

_shift_tile unpacks a Morton tile coordinate and returns layout.morton((tx+sx)%gw,(ty+sy)%gh). _shift_id parses an ID, preserves its frame, shifts its Morton component, and formats a track ID when the first character is k, otherwise a region ID. Accepted identifier kinds/ranges remain dependent on unbound ids/layout. (`visioncore/pack.py:458-467`.)

_shift_box first converts origin coordinates to int, uses unit u=1 for tile boxes or u=s for pixel boxes, computes **nx=((x0//u+sx)%gw)×u+x0%u** and the analogous ny, and returns **[nx,ny,nx+int(b[2])-x0,ny+int(b[3])-y0]**. It preserves the endpoint differences and within-tile pixel remainders; there is no clipping or splitting of boxes that extend beyond the grid. (`visioncore/pack.py:470-476`.)

For every region, _placebo shallow-copies the row, sets id to the shifted ID, shifts each truthy bbox_px and bbox_t, stores it under the shifted key, and builds a shifted-to-original region-ID back map. For each GOP, it shallow-copies the GOP and every track, always shifts k, and shifts a truthy r_rep; other fields, including event/cycle/global rows and motion components, are left as copied data. This is the precise extent of the transformation, despite the broader wording in its docstring. (`visioncore/pack.py:479-506`.)

Heatmap tiles are separately copied and only their morton column is shifted; class and motion columns are unchanged. V3 and crop-candidate construction use shifted GOPs/regions, while mandatory frame selection uses the original GOPs and crop pixels use the original regions. The accumulator is not remapped before ranking the shifted IDs. (`visioncore/pack.py:543-546,569,575-597,630,639-656`.)

## Judge request, response cache, and run token allowance

### Request bytes and in-memory fields

build_request(pr,static_extra=b"") accepts bytes or bytearray and otherwise raises E_USAGE. Prefix is exact z1.txt bytes + one LF byte + bytes(static_extra). The media list is formed by sorting pr.files items by file name and including every file except z1.txt and pack.json; thus mp4_frames.json is a media entry. Each entry is precisely {file:key, sha256:hash(value)}. (`visioncore/pack.py:688-697`.)

Suffix is canonical UTF-8 JSON for exactly **{pack:pr.pack, media:[descriptor,...]}**, including its final LF. It embeds the in-memory pack object, not raw pr.files["pack.json"] bytes. A caller who modifies those two representations independently is not reconciled here. (`visioncore/pack.py:694-697`; `visioncore/canon.py:27-29`.)

JudgeRequest stores **prefix** and **suffix** bytes, **prefix_sha256** as the exact prefix hash, **body = prefix + b"\n--DYNAMIC--\n" + suffix**, and **request_sha256** as the body hash. It does not write a request file. Prefix stability across batches is conditional on identical z1.txt and identical caller static_extra; arbitrary static_extra can introduce any caller bytes. (`visioncore/pack.py:680-697`.)

The _Z1 literal advertises judge-output fields check_id, status, pointers, reason_class, and digest_quote; it advertises statuses PASS/FAIL/CANNOT_SEE, pointer limits, eight reason-class strings, quote-equality expectations, and examples. These are bytes placed in z1.txt, not a response JSON parser or validator in pack.py. judge_batch accepts any bytes/bytearray response and returns bytes, regardless of whether those bytes are JSON or follow the advertised rubric. (`visioncore/pack.py:37-61,64-69,748-754`.)

| Literal legend field or rule | Advertised contents; not validated by this module | Source |
|---|---|---|
| `check_id` | str in the shown per-check object. | `visioncore/pack.py:53` |
| `status` | The shown string alternatives PASS, FAIL, CANNOT_SEE. | `visioncore/pack.py:53` |
| `pointers` | An array of IDs; accompanying literal text says at most 8 per result and at most 24 characters each. | `visioncore/pack.py:42-44,53` |
| Pointer spellings | f&lt;frame&gt;:t&lt;hhhh&gt;, r&lt;frame&gt;:&lt;hhhh&gt;, f&lt;frame&gt;:all, k&lt;frame&gt;:&lt;hhhh&gt;, g&lt;n&gt;; the legend says decimal frames without leading zeros and exactly four lowercase hex digits. No numeric frame/GOP upper bound is given in the literal. | `visioncore/pack.py:42-45` |
| `reason_class` | str in the shown object. Listed strings: frozen, flicker, teleport, overlap, drift, missing, black, static_content; the literal says required on every FAIL. | `visioncore/pack.py:49-53` |
| `digest_quote` | str in the shown object; accompanying literal says a supplied quote repeats the cited item's quote byte for byte. | `visioncore/pack.py:53-55` |
| FAIL handling | Literal text requires at least one compatible-evidence pointer or CANNOT_SEE and says vision-class checks need compatible host regions independently of reason_class. These are instructions carried as data in z1.txt. | `visioncore/pack.py:52-55` |

### Cache storage and receipt schema

ResponseCache initializes root=str(root) and integer counters hits=misses=0. Cache keys must be strings of exactly 64 characters, each in lowercase 0-9/a-f. The cache path is root/key[:2]/(key+".resp"). get reads the entire file as bytes, increments hits, and returns it; on any caught OSError it increments misses and returns None. It does not verify content against the key. put creates parent directories and delegates raw bytes(data) to canon.write_atomic. (`visioncore/pack.py:704-727`.)

judge_batch always builds the request and checks its per-request estimate before calling cache.get. On a hit, it skips budget validation, charging, provider invocation, and cache writing. On a miss, it requires isinstance(budget,RunTokenBudget), charges first, calls call(req), requires bytes or bytearray, then writes the cache. A failing provider call or invalid response type leaves the preceding charge in place because no rollback is implemented. (`visioncore/pack.py:730-751`.)

The function returns **(bytes(data), receipt)**; receipt is an object with exactly these keys. (`visioncore/pack.py:752-754`.)

| Receipt key | Type and value | Source |
|---|---|---|
| `request_sha256` | SHA-256 string of exact request body bytes. | `visioncore/pack.py:683-684,752` |
| `prefix_sha256` | SHA-256 string of exact prefix bytes. | `visioncore/pack.py:682,752` |
| `prefix_bytes` | Integer len(prefix). | `visioncore/pack.py:752` |
| `request_bytes` | Integer len(body). | `visioncore/pack.py:753` |
| `cache` | String "hit" or "miss". | `visioncore/pack.py:741-744,753` |
| `response_sha256` | SHA-256 string of bytes(data). | `visioncore/pack.py:753`; `visioncore/canon.py:50-51` |

### Request and run token rules

Let extra_b=max(0,len(request.suffix)-pr.pack["v3_bytes"]). Before a cache lookup, judge_batch computes **tokens = pr.pack["est_tok"] + ceil(len(static_extra)/4) + ceil(extra_b/4)** and rejects tokens>_TOK_TARGET with part request_tokens. The suffix includes the entire canonical {pack,media} envelope; this formula is the implemented accounting rule rather than an independently measured tokenization of the request body. (`visioncore/pack.py:694-696,735-740`.)

RunTokenBudget initializes integer **tokens, calls, retries, primary_tokens** to zero. charge(tokens,retry=False) requires type(tokens) is int, tokens>=0, and type(retry) is bool. The counters themselves are public attributes and are not protected against external modification. (`visioncore/pack.py:757-769`.)

For an unmodified budget, the cap for the proposed charge is PARAMS["PACK_RETRY_TOK"] if this charge is marked retry or a previous retry charge has been accepted (even if its later provider call failed); otherwise PARAMS["PACK_RUN_TOK"]. charge rejects before changing counters when any of these conditions holds: tokens exceeds PARAMS["PACK_CALL_TOK"]; cumulative total would exceed the selected cap; a retry is requested when retries>=_RETRY_MAX; or a primary charge would make primary_tokens exceed PARAMS["PACK_RUN_TOK"]. Equality at caps is accepted. The three parameter values and configured retry limit are **ABSENT**; only _RETRY_MAX's fallback 2 is defined locally. (`visioncore/pack.py:18-20,770-774`.)

After success, total tokens increases by the charge. A retry increases retries only; a primary increases calls and primary_tokens. calls therefore counts primary charges, not all dispatch attempts. Once a retry has been charged, subsequent primary charges use the retry total cap as well as the separate primary total cap. Zero-token charges are allowed and still increment the applicable count. There is no automatic retry loop, refund, sleep, or provider invocation inside RunTokenBudget. (`visioncore/pack.py:767-780`.)

## File-write behavior and explicit pack errors

PackResult.write converts out_dir to str, chooses tmp=out_dir+".tmp", and refuses if either path already exists. It iterates the file dictionary, joins tmp with each relative key, creates parent directories, writes each value in binary mode, and calls os.replace(tmp,out_dir). It has no path-sanitization check for arbitrary caller-modified files keys. On an OSError in that try block, it calls recursive _rmtree(tmp), then raises E_IO with the output path and the first 80 characters of the error string; if that cleanup itself fails, there is no inner handler to ensure the E_IO wrapper is reached. Non-OSError exceptions are not caught by this method. (`visioncore/pack.py:514-540`.)

The table lists the module's explicit VqaError sites and their supplied detail dictionaries. It does not claim these dictionaries are themselves a standardized external error JSON object: VqaError's implementation and serialization are **ABSENT**. Ordinary exceptions from unbound dependencies, invalid indexing/conversion, providers, or cleanup are not comprehensively translated. (`visioncore/pack.py:8-10,514-540,748-751`.)

| Code | Trigger and supplied details | Source |
|---|---|---|
| `E_BUDGET` | Z1 exceeds parameter limit: {part:"z1", bytes:len(b), cap:PARAMS["Z1_MAX_B"]}. | `visioncore/pack.py:66-68` |
| `E_USAGE` | PNG array has wrong dimensions/channels or empty side: {reason:"png_encode needs uint8[H,W,3]"}. Conversion to uint8 has already occurred. | `visioncore/pack.py:80-82` |
| `E_USAGE` | No heatmap tile frame: {reason:"heatmap needs >= 1 frame"}. | `visioncore/pack.py:154-156` |
| `E_SCHEMA` | Tile-frame length mismatch: {reason:"tile frame length differs"}. | `visioncore/pack.py:167-169` |
| `E_SCHEMA` | Maximum tile class >6: {cls:int(cls.max())}. | `visioncore/pack.py:170-172` |
| `E_VC_ID_RANGE` | Empty/reversed clamped crop: {bbox_t:list(bbox_t)}. | `visioncore/pack.py:223-226` |
| `E_SCHEMA` | L0 representative absent: {reason:"r_rep not in regions", r_rep:tr["r_rep"]}; pack's earlier check supplies tr.get("r_rep") instead. | `visioncore/pack.py:313-315,571-574` |
| `E_BUDGET` | All-L2 GOP block over cap: {part:"v3", gop:g["g"], cap:gop_cap}. | `visioncore/pack.py:363-366` |
| `E_BUDGET` | All-L2 batch over cap: {part:"v3", reason:"all-L2 exceeds batch cap", cap:cap_bytes}. | `visioncore/pack.py:367-368` |
| `E_BUDGET` | Too many mandatory frames: {part:"mandatory_frames", count:must_n, cap:slots}. | `visioncore/pack.py:434-436` |
| `E_USAGE` | Output directory or staging directory already exists: {reason:"output exists", path:out_dir}. | `visioncore/pack.py:516-519` |
| `E_IO` | Caught pack write OSError after successful cleanup: {path:out_dir, reason:str(error)[:80]}. | `visioncore/pack.py:527-529` |
| `E_USAGE` | Effective cap not an int instance or <1: {cap_bytes:repr(cap)}. | `visioncore/pack.py:561-563` |
| `E_USAGE` | Shift requested with grid width <2: {reason:"placebo needs grid width >= 2"}. | `visioncore/pack.py:580-583` |
| `E_USAGE` | n_check_png/text_b not exact nonnegative integers: {reason:"token inputs must be nonnegative integers"}. | `visioncore/pack.py:606-608` |
| `E_BUDGET` | Exhausted media ladder above hard estimate cap: {part:"tokens", est_tok:est(), cap:_TOK_HARD}. | `visioncore/pack.py:635-636` |
| `E_USAGE` | static_extra neither bytes nor bytearray: {reason:"static_extra must be bytes"}. | `visioncore/pack.py:691-692` |
| `E_USAGE` | Cache key fails the lowercase SHA-shaped string test: {reason:"bad cache key"}. | `visioncore/pack.py:708-710` |
| `E_BUDGET` | Request-envelope estimate exceeds target: {part:"request_tokens", est_tok:tokens, cap:_TOK_TARGET}. | `visioncore/pack.py:737-740` |
| `E_USAGE` | Cache miss with wrong/missing budget: {reason:"judge dispatch requires a shared RunTokenBudget"}. | `visioncore/pack.py:743-746` |
| `E_USAGE` | Provider returned a non-bytes/non-bytearray value: {reason:"call must return bytes"}. | `visioncore/pack.py:748-750` |
| `E_USAGE` | Charge token/retry type or token sign invalid: {reason:"invalid token charge"}. | `visioncore/pack.py:767-769` |
| `E_BUDGET` | Any run-budget limit violated: {part:"run_tokens", tokens:self.tokens, cap:selected_cap}; tokens reports the pre-charge total, not the attempted charge. | `visioncore/pack.py:770-774` |

## Pack-specific ABSENT boundaries

- **pack_tokens:** no such bound symbol. Token specifications above use the implemented helpers, media ladder, request estimate, and run budget. (`visioncore/pack.py:444-455,556-673,730-780`.)
- **Numeric parameter registry:** concrete PACK_BATCH_B, PACK_GOP_B, Z1_MAX_B, PACK_CALL_TOK, PACK_RUN_TOK, PACK_RETRY_TOK, and configured PACK_RETRY_MAX values/ranges are not defined in the bound files; they are imported from params. (`visioncore/pack.py:10,18-20,67-68,561,590,663,770-774`.)
- **Full grid/tile/ID/region schema and validation:** delegated grid calculations, Morton conversion, ID parsing/formatting, source tile dtype, and region production are not bound. The consumed/copied fields and concrete checks are specified above, without inventing a complete schema. (`visioncore/pack.py:8,154,158-179,241-247,313-317,458-467,565-568`.)
- **Exact digest-quote generation, response parsing, verifier behavior, and a substantive Q protocol:** unbound cite is called for quotes; judge_batch returns opaque bytes; only the optional coordinate-shift implementation is defined here. (`visioncore/pack.py:296-303,578-586,748-754`.)
- **Actual MP4 encoding and external check-image resizing:** no such writer/transformer is implemented in these paths. The manifest and optional sizing hint are specified above. (`visioncore/pack.py:613-617,637-673`.)
- **Unconditional cross-platform/cross-version byte identity:** serialization settings, rendering arithmetic, sorting, and compression level are fixed in code, but imported implementation versions and caller-provided bytes/data are not pinned by these files. (`visioncore/pack.py:3-10,73-88,564,694-697`; `visioncore/canon.py:3-5,27-29`.)

## Canonical serialization and the scope of byte stability

This section describes the bound files at `bridge/integration`, observed commit `2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5`. The serialization implementation is the local `canon` wrapper around Python's `json` module. Its explicit choices, callers, and limitations are specified below; a universal claim that all runs or environments produce identical output bytes is **ABSENT**. (`visioncore/canon.py:3-8`, `visioncore/canon.py:13-55`; runtime-dependent producers and callers: `visioncore/pack.py:639-672`, `visioncore/pack.py:688-754`, `visioncore/gop.py:235-279`.)

### Accepted values, validation, and coercion

`dumps(o)` first runs `_check_ints(o)`, then calls `json.dumps` without a custom encoder or a `default` converter. The precheck rejects Python `float` instances, checks that dictionary keys are strings, and recursively visits dictionary values and list/tuple elements. It is not a complete schema validator or a general conversion of objects to JSON-compatible types. (`visioncore/canon.py:13-29`.)

| Python value presented to `dumps` | Serialization / validation behavior | Source |
|---|---|---|
| `None` | Not rejected by the precheck; standard JSON `null`. | `visioncore/canon.py:13-29` |
| `bool` | Not rejected or converted to an integer by the precheck; standard JSON `true` or `false`. | `visioncore/canon.py:13-29` |
| `int` | Not rejected or range-checked by the precheck; passed to the JSON encoder as an integer. An application-level lower/upper bound or fixed integer width is **ABSENT** here. | `visioncore/canon.py:13-29` |
| `str` | Passed to the JSON encoder as a JSON string; also the only accepted dictionary-key type according to `isinstance(k, str)`. | `visioncore/canon.py:16-20`, `visioncore/canon.py:29` |
| `dict` | Every key must be a string; every value is visited recursively. The JSON object is emitted with sorted keys. The function does not coerce numeric or other non-string keys into strings. | `visioncore/canon.py:16-20`, `visioncore/canon.py:29` |
| `list` | Elements are visited recursively and serialized in their existing order as a JSON array. | `visioncore/canon.py:21-23`, `visioncore/canon.py:29` |
| `tuple` | Elements receive the same recursive precheck as a list; the standard JSON encoder represents the tuple as an array. There is no tuple-type marker in the JSON. | `visioncore/canon.py:21-23`, `visioncore/canon.py:29` |
| `float`, including a nested float | Immediately raises `VqaError("E_FLOAT", {"value": repr(o)})` when visited. The check uses `isinstance`, not an exact-type comparison. A float dictionary key instead reaches the non-string-key rejection. | `visioncore/canon.py:13-23` |
| A dictionary key that fails `isinstance(k, str)` | Raises `VqaError("E_SCHEMA", {"key": repr(k)})`; it is neither silently skipped nor converted by this wrapper. | `visioncore/canon.py:16-20` |
| Other values, such as bytes, sets, arrays, or arbitrary application objects | The precheck has no general rejection or conversion branch for these values; it delegates them to the standard encoder. A custom encoding of these types is **ABSENT**, and `dumps` does not catch errors raised by the encoder. | `visioncore/canon.py:13-29` |

These rows describe the wrapper's call into the standard encoder, not a new independently implemented JSON type system. There is no `.tolist()`, scalar normalization, Unicode normalization, rounding, or conversion of a rejected float to an integer in `canon`. Recursion-depth limits, maximum string/array/object sizes, treatment of arbitrary custom subclasses, and a repository-specific error for cyclic structures are **ABSENT** from this implementation. (`visioncore/canon.py:13-29`.)

### Exact JSON text, bytes, and hashes

The complete canonical-text expression is:

```python
json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
```

That expression is the return value of `dumps`: a Python string, with dictionary keys sorted by the standard encoder, no separator spaces after a comma or colon, standard JSON string escaping, and exactly one appended LF character. `ensure_ascii=False` means non-ASCII characters are not universally replaced by ASCII escape sequences; it does not disable JSON escaping. Array order is not sorted by this call. There is no custom comparator, indentation, or Unicode-normalization step. (`visioncore/canon.py:27-29`.)

The writer paths use UTF-8 explicitly. `sha256_obj(o)` hashes `dumps(o).encode("utf-8")`, so its input includes the final LF. `sha256_bytes(b)` hashes the bytes argument directly with `hashlib.sha256(b).hexdigest()`; it does not add a newline or canonicalize JSON. Its returned digest is the SHA-256 hexadecimal digest, i.e. 64 lowercase hexadecimal characters. (`visioncore/canon.py:50-55`.)

`write_atomic` also encodes a string argument with UTF-8 before writing bytes; a bytes argument is not reserialized. No byte-order mark is prepended by these UTF-8 encoding calls, and there is no platform text-mode newline translation in these write paths. (`visioncore/canon.py:58-66`; direct pack bytes: `visioncore/pack.py:521-525`, `visioncore/pack.py:659-672`.)

The size used by the V3 fitter is deliberately different from the size of a complete `dumps` result:

```python
len(canon.dumps(o).encode("utf-8")) - 1
```

Only the appended LF byte is subtracted. This measures encoded bytes rather than Python characters, and it retains every byte inside JSON strings or other syntax. GOP-block caps and the V3 batch cap use this helper. `v3_bytes` is the size of the V3 value under this rule, not the size of the complete `pack.json`, request envelope, or output directory. (`visioncore/pack.py:351-352`, `visioncore/pack.py:363-380`, `visioncore/pack.py:590-591`, `visioncore/pack.py:661-665`.)

### Parsing and canonicality checks

| `Function` | Implemented behavior | Source |
|---|---|---|
| `_no_float(s)` | Raises `VqaError("E_FLOAT", {"value": s})`; the payload preserves the parser-provided token string rather than computing a numeric value. | `visioncore/canon.py:32-33` |
| `loads(text)` | Calls `json.loads(text, parse_float=_no_float, parse_constant=_no_float)`. Floating-point syntax and parser-recognized non-finite constants are routed to the rejecting callback. Other parsing behavior is delegated to the standard JSON parser; no object-pairs hook, duplicate-key check, canonical-whitespace check, or custom integer parser is supplied. | `visioncore/canon.py:36-40` |
| `loads` caught-error branch | A caught `ValueError` is converted to `VqaError("E_SCHEMA", {"reason": str(e)[:120]})`. Other exception families are not caught by this branch. The inheritance and default return-code behavior of imported `VqaError` are **ABSENT** from the bound sources; therefore this section does not independently certify whether a callback-raised `VqaError` is also a `ValueError`. | `visioncore/canon.py:8`, `visioncore/canon.py:36-40` |
| `is_canonical(text)` | Returns the direct comparison `dumps(loads(text)) == text`. A caught `VqaError` returns `False`; there is no catch-all exception handler and no explicit input-type conversion. | `visioncore/canon.py:43-47` |
| `load_schema(schema_id)` | Splits at the first `/`, resolves `<name>.<ver>.schema.json` under `Path(__file__).resolve().parent / "schemas"`, raises `E_SCHEMA` with `{"schema": schema_id}` if the version substring is empty or the resulting path is not a file, then reads UTF-8 and calls `loads`. It loads a JSON value; this function does not validate another object against that value. | `visioncore/canon.py:10`, `visioncore/canon.py:75-81` |

For a supported JSON text value, canonicality therefore depends on matching the re-emitted ordering, escaping, separators, integer representation, and final LF exactly. Merely being parseable JSON is insufficient. A missing final LF, additional insignificant whitespace, or different object ordering is not normalized before this comparison. The actual schema documents and their constraints are **ABSENT** from the allowed evidence; neither output writer invokes `load_schema`. (`visioncore/canon.py:27-47`, `visioncore/canon.py:75-81`; output serialization: `visioncore/pack.py:658-673`, `visioncore/gop.py:511-520`.)

### Serialization at every output surface

| Output surface | Exact serialization or assembly | Source |
|---|---|---|
| `z1.txt` | `_Z1.encode("ascii")`, returned after its byte-cap check; no `canon.dumps` call. The literal itself ends with a newline. | `visioncore/pack.py:37-69`, `visioncore/pack.py:637` |
| `heatmap_g<g>.png` | PNG bytes from `heatmap_png`, ultimately `png_encode`; stored bytes are hashed directly with `canon.sha256_bytes`. No JSON serialization applies to the image. | `visioncore/pack.py:151-213`, `visioncore/pack.py:639-646` |
| `crops/<region-with-colon-replaced-by-underscore>_before.png` and `_after.png` | PNG bytes from `crop_png`, ultimately `png_encode`; each metadata hash covers the returned PNG bytes directly. | `visioncore/pack.py:218-237`, `visioncore/pack.py:647-657` |
| `mp4_frames.json` | `canon.dumps({"frames": frame_sel, "frame_map": frame_map, "must_keep": must_n}).encode("utf-8")`, including the final LF. `mp4_frames_sha256` hashes those complete stored bytes. This is a frame-selection JSON document; no MP4 encoder or `.mp4` file creation occurs in the bound producer. | `visioncore/pack.py:637-672`, especially `visioncore/pack.py:658-665` |
| `pack.json` | `canon.dumps(pk).encode("utf-8")`, including the final LF and all present optional fields. The nested V3 object is part of this document, not separately written by `pack`. | `visioncore/pack.py:660-673` |
| `gops.bin` | Concatenation of each supplied `g.blob` in the writer's input iteration order, starting with `b""`; no delimiter or JSON encoding is inserted between blobs. | `visioncore/gop.py:514-519` |
| `gops.jsonl` | One `canon.dumps(gop_record(...))` string per supplied GOP, joined with `""` and UTF-8 encoded by `write_atomic`. Each record contributes its own final LF; no outer JSON array, comma, or additional blank-line delimiter is added. Offsets accumulate the corresponding blob lengths in exactly the same loop. An empty `gops` iterable produces empty `gops.bin` and empty `gops.jsonl`. | `visioncore/gop.py:503-520`, `visioncore/canon.py:27-29`, `visioncore/canon.py:58-66` |
| `JudgeRequest.prefix` in memory | `pr.files["z1.txt"] + b"\n" + bytes(static_extra)`. `static_extra` must be `bytes` or `bytearray`; it is not parsed, sanitized, or canonicalized. The additional LF remains even if `z1.txt` already ends with LF. | `visioncore/pack.py:688-693` |
| `JudgeRequest.suffix` in memory | `canon.dumps({"pack": pr.pack, "media": media}).encode("utf-8")`. `media` is a filename-sorted list of `{"file": k, "sha256": sha256_bytes(v)}` for every `pr.files` entry except `z1.txt` and `pack.json`; it includes `mp4_frames.json` in a normal `PackResult`. | `visioncore/pack.py:694-697` |
| `JudgeRequest.body` and hashes in memory | `prefix + b"\n--DYNAMIC--\n" + suffix`; `prefix_sha256` covers exactly `prefix`, and `request_sha256` covers exactly this combined body. | `visioncore/pack.py:677-684` |
| Response-cache `<root>/<sha[:2]>/<sha>.resp` | Key must be a string of 64 lowercase hexadecimal characters. `put` converts its data with `bytes(data)` and calls `canon.write_atomic`; it does not append LF or require JSON. `get` reads bytes. `judge_batch` requires the provider return value to be bytes or bytearray before caching it. | `visioncore/pack.py:704-727`, `visioncore/pack.py:748-754` |
| Judge receipt, `PackResult.acc`, and `write_gops` return value | The receipt is an in-memory dictionary returned alongside response bytes; it is not written or canonically serialized here. `PackResult.acc` is retained in memory, and `write_gops` returns the list of already canonical record strings. No additional filename for these values is defined. | `visioncore/pack.py:510-512`, `visioncore/pack.py:592-594`, `visioncore/pack.py:673`, `visioncore/pack.py:752-754`, `visioncore/gop.py:521` |

The request suffix reserializes `pr.pack`; it does **not** embed the saved bytes from `pr.files["pack.json"]`. `PackResult` retains the supplied objects directly, without freezing them or copying them in its constructor. Thus an externally mutated `pr.pack` or `pr.files` can change later request bytes, and correspondence between the saved `pack.json` and a later request is not rechecked here. (`visioncore/pack.py:510-512`, `visioncore/pack.py:688-697`.)

### Order decisions preceding canonical JSON

Sorting dictionary keys alone does not reorder list members or eliminate dependence on caller data. These producer-side ordering rules are visible in the bound files:

| Structure | Ordering rule | Source |
|---|---|---|
| GOPs produced by `encode_run` | Plan order over the supplied frame sequence; supplied frame numbers are converted with `int` and checked for strict increase and one-per-frame correspondence. | `visioncore/gop.py:128-141`, `visioncore/gop.py:240-248`, `visioncore/gop.py:276-280` |
| GOP records and binary blobs written by `write_gops` | The supplied iterable's order; the writer performs no additional GOP sort. | `visioncore/gop.py:514-520` |
| Region traversal in track construction | Regions within a frame are sorted by their `id` string. Link pairs become lookup structures; duplicate destination keys use the ordinary dictionary-comprehension result rather than a uniqueness check in this function. | `visioncore/gop.py:372-377` |
| Track output list | Numeric frame and hexadecimal Morton component extracted from the track ID form the sort key. | `visioncore/gop.py:431-436` |
| Modal motion and flags | Motion candidates sort by descending frequency, then the motion tuple; the first wins. Flags are emitted by the fixed order `APPEAR, VANISH, SPLIT, MERGE, FROZEN, FLICKER, DRIFT, CYCLE, GLOBAL`, not set iteration order. | `visioncore/gop.py:31`, `visioncore/gop.py:449-454` |
| `Events` | Internal event tuples are sorted before each becomes a two-element `[name, id]` row. | `visioncore/gop.py:283-290` |
| `Cycles` | Sort by internal start-position index, rank, and period; then remove the start-position key, and `encode_run` removes rank before publishing cycles in `Gop`. | `visioncore/gop.py:491-499`, `visioncore/gop.py:271-279` |
| GOPs entering `pack` | Sort by `int(g["g"])`. There is no explicit duplicate-GOP-ID rejection in this statement. | `visioncore/pack.py:564` |
| Track label selection | Sort candidates by negative weight and label string after collecting recognized flags/class bits. | `visioncore/pack.py:249-264` |
| Fitted V3 items within GOP blocks | Strict key `(-(weight + accumulator), first_frame, morton, item_id)`; `fit` replaces the input item order with this sorted order before encoding blocks. | `visioncore/pack.py:333-348`, `visioncore/pack.py:360-362` |
| Selected frames and frame map | Mandatory tiers are deduplicated and sorted within each tier; fill candidates sort by decreasing dirty value then frame; the completed selected-frame list is sorted before assigning `[index, frame]` map rows. | `visioncore/pack.py:428-441` |
| Heatmap and crop collections | More than four heatmap candidates are selected by descending per-GOP maximum item weight then GOP value and reordered by GOP value. Crops follow the sorted item-priority order before truncation/removal. | `visioncore/pack.py:595-604`, `visioncore/pack.py:623-657` |
| `crop_frames` and request media | Crop frame numbers are a sorted set; request media rows are sorted by filename through `sorted(pr.files.items())`. | `visioncore/pack.py:669`, `visioncore/pack.py:694-695` |

No rule in `canon` repairs ambiguous or duplicate semantic IDs, validates imported region/link schemas, or makes differently ordered caller arrays equivalent. The producer-specific sort operations above apply where called; they are not a general canonical ordering of arbitrary input lists. (`visioncore/canon.py:13-29`, `visioncore/gop.py:372-377`, `visioncore/pack.py:337-348`, `visioncore/pack.py:549-552`, `visioncore/pack.py:564`.)

### Binary identity and runtime limits

PNG encoding first converts the input to a contiguous `uint8` array, rejects arrays that are not positive-height/positive-width three-channel arrays, and writes a fixed PNG signature followed by `IHDR`, a single `IDAT`, and `IEND`. Every scanline begins with a zero filter byte. The header uses 8-bit samples and color type 2 with zero compression/filter/interlace-method fields; `IDAT` is `zlib.compress(raw.tobytes(), 6)`. Chunk length and CRC are encoded as four-byte big-endian integers, and the CRC is `zlib.crc32(tag + data) & 0xFFFFFFFF`. No text or time chunk is constructed. Cross-version guarantees for the installed NumPy and zlib implementations are **ABSENT**. (`visioncore/pack.py:73-88`.)

GOP storage similarly uses `zlib.compress(raw_i, 6)` for the keyframe, but defines separate identities:

| `Identity` | Exact covered bytes | Source |
|---|---|---|
| `key_sha256` | `raw_i`, the contiguous keyframe table bytes. | `visioncore/gop.py:101-105`, `visioncore/gop.py:250`, `visioncore/gop.py:277` |
| `gop_blob_sha256` | The complete stored blob: real packed header, compressed keyframe, and delta body. | `visioncore/gop.py:263-267`, `visioncore/gop.py:506` |
| `gop_canon_sha256` | A header built with the usual `hdr_args`, compressed-keyframe length set to zero, delta-body length preserved, `key_sha8` preserved, and `body_sha8` set to zero; then uncompressed `raw_i`; then the unchanged delta body. | `visioncore/gop.py:262-266`, `visioncore/gop.py:506` |
| Eight-byte header prefixes | The first eight bytes of a SHA-256 digest interpreted as a little-endian integer. | `visioncore/gop.py:156-162`, `visioncore/gop.py:263-265` |

The canonical GOP identity excludes compressed keyframe bytes and their stored length, so changes only to that compression representation do not alter this identity. The stored blob hash, stored blob length, and later record offsets can still differ. As a result, a matching canonical GOP hash does not establish identical `gops.bin` bytes or identical JSONL records, and it does not cover the track/cycle/event metadata in the JSON record. (`visioncore/gop.py:264-279`, `visioncore/gop.py:503-520`.)

The binary header and P-frame struct formats specify little-endian packing, and dirty-mask words are explicitly converted to `"<u8"`. Conversely, `_rec_bytes` checks table item size and shape and views the existing contiguous record bytes; it does not normalize a tile-record schema or endianness. The imported tile layout definitions and guarantees about caller table representation are **ABSENT** from the bound sources. (`visioncore/gop.py:19-23`, `visioncore/gop.py:101-105`, `visioncore/gop.py:194-208`.)

The static Z1 bytes contain no dynamically inserted run ID or timestamp. These producers do not explicitly read a clock, random source, or environment variable to inject such metadata into their normal serialized outputs. Nevertheless, `static_extra`, batch values, frame/tile callbacks, imported parameters, imported ID/layout/quote helpers, externally supplied global motion and regions/links, and caller mutations remain inputs. There is no sanitization that prevents those inputs from reflecting a timestamp, environment, or changing external data. The absence of a direct clock call therefore establishes only that the shown functions do not themselves add clock values. (`visioncore/pack.py:37-69`, `visioncore/pack.py:3-10`, `visioncore/pack.py:296-317`, `visioncore/pack.py:556-586`, `visioncore/pack.py:639-696`; `visioncore/gop.py:7-17`, `visioncore/gop.py:231-279`.)

### Filesystem writes and failure boundaries

`canon.write_atomic(path, data)` converts the path to `Path`, uses the fixed staging filename `<basename>.tmp` in the same directory, writes bytes there, and calls `os.replace(tmp, path)`. It does not create the parent directory. On a caught `OSError`, it attempts to unlink the temporary file, suppresses an `OSError` from that unlink, and raises `VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})`. Path conversion and string-to-UTF-8 encoding occur before the `try`; non-`OSError` exceptions are not generally caught. (`visioncore/canon.py:58-72`.)

`write_gops` builds the binary concatenation and all JSON strings in memory before writing. It then calls `write_atomic` for `gops.bin` and, separately, for `gops.jsonl`. If replacing the first succeeds but the second write fails, there is no rollback of the first. Thus the docstring's claim that any failure leaves both target files unchanged is broader than the two independent write calls implement. Multi-file transactional atomicity is **ABSENT**. (`visioncore/gop.py:511-521`.)

`PackResult.write(out_dir)` rejects an existing target directory or existing `<out_dir>.tmp`, writes every `self.files` member in binary mode under that staging directory, then calls `os.replace(tmp, out_dir)`. It iterates the mapping without sorting, but does not modify each file's supplied data. On a caught `OSError`, it calls `_rmtree(tmp)` and then raises `E_IO`; cleanup itself contains uncaught filesystem operations, so universal successful cleanup is not established. Existing-file mutation after constructing the `PackResult`, concurrent filesystem changes, durable crash recovery, metadata normalization, file locking, and `fsync` guarantees are **ABSENT** from the implementation. (`visioncore/pack.py:510-540`.)

The response cache adds a separate filesystem dependency to judge dispatch: every `OSError` from its binary read is treated as a cache miss, while any successfully read bytes are treated as a hit. There is no response-schema check or comparison to a previously stored expected response digest. A hit returns existing bytes; a miss calls the external callback and stores its result. Identical request bytes therefore do not by themselves establish identical newly obtained response bytes, and the receipt's `cache` field depends on cache state. (`visioncore/pack.py:708-727`, `visioncore/pack.py:731-754`.)

## Other ABSENT definitions and guarantees

| Subject | What this evidence does and does not establish | Source |
|---|---|---|
| Imported thresholds and layout constants | **ABSENT:** concrete `GOP_K`, `CUT_PPM_1F`, `CUT_PPM_2F`, `layout.TILE_SIZE`, `layout.AXIS_MAX`, and `layout.CLS_DIRTY_MIN` definitions. Formulas and uses are documented above; comments saying 32-byte records do not define the imported runtime value. | `visioncore/gop.py:16`, `visioncore/gop.py:101-125`, `visioncore/gop.py:149-153`, `visioncore/gop.py:173-174`, `visioncore/gop.py:235-245` |
| Complete region/link, ID, grid, and tile schemas | **ABSENT:** upstream producers, additional fields, complete accepted ranges, tile record endianness/padding, and ID validation behavior. The local consumed fields, binary headers, and copied output fields are specified above. | `visioncore/gop.py:16`, `visioncore/gop.py:101-109`, `visioncore/gop.py:348-454`; `visioncore/pack.py:8`, `visioncore/pack.py:241-247`, `visioncore/pack.py:313-317`, `visioncore/pack.py:565-568` |
| External JSON schemas and error envelopes | **ABSENT:** schema-file contents, `VqaError` inheritance/default return codes, and stdout serialization. `load_schema` loads a value, and the producers do not call it to validate their output. Explicit exception constructor arguments are specified above. | `visioncore/canon.py:8-10`, `visioncore/canon.py:75-81`; `visioncore/gop.py:17`, `visioncore/gop.py:34-35`, `visioncore/gop.py:503-521`; `visioncore/pack.py:9`, `visioncore/pack.py:660-673` |
| Universal byte identity and filesystem transactions | **ABSENT:** pinned runtime behavior across Python/NumPy/zlib versions, imported helper determinism, controlled caller/provider data, transactional replacement of both GOP files, durable writes, and concurrency guarantees. The implemented serialization and per-call write boundaries are specified above. | `visioncore/canon.py:3-8`, `visioncore/canon.py:58-72`; `visioncore/gop.py:250-279`, `visioncore/gop.py:511-520`; `visioncore/pack.py:510-540`, `visioncore/pack.py:688-754` |
| Repository-wide conventions and gate results | **ABSENT:** verification of constant registration, global integer-only compliance, catalogue freshness, sealed-tree checks, exit conventions, and repository test/gate outcomes. `AGENTS.md` states these conventions but supplies no observed execution results. Its blanket output-preservation claim is not implemented as pairwise rollback by `write_gops`. | `AGENTS.md:15-25`, `AGENTS.md:31-48`, `AGENTS.md:53-57`; `visioncore/gop.py:511-520` |
