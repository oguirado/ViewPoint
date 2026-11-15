# HB-VN-s0s1 (HB-VNS0S1-001) - RT2 native S0+S1a kernel bit contract (pad, luma, words_eq, compute_tiles, isqrt)

Book-first hyperbook (hb-hybrid/v1) of the slot kernels that RT2 must replace byte for byte. `KERNELS_REAL=False` today: `vnative/backend.py` carries Python identity shims, `dual_verify` refuses with `E_VN_NATIVE_MISSING`, `rust/visioncore_native/src/s0.rs`, `s1.rs` and `isqrt.rs` are one-line stubs, and the crate is still a stub build: `rust/visioncore_native/Cargo.lock` is present (commit eac6d05, pyo3 0.22, rayon 1.8) and `tools/build_native.py` builds `--offline --locked` reproducibly (.so sha 5d60d879), but it exports no S0/S1 kernel. Nothing here claims a native result. Numbers are tagged `[MEASURED, <receipt>]`, `[CITED, <excerpt id>]` or `[TARGET]`. Ownership: this book owns the bit contract of each output byte; crate, FFI, build and parallelism belong to `HB-VN-n1-native` (cross-reference only through `pm.edges.v2`).

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"reading","n":0,"purpose":"Task to section table, personas and negative exclusions for the native S0+S1a bit contract.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Reading map"}
```
<!-- HB:END -->

<!-- PM:BEGIN pm.node.v2 -->
```json
{"applicability":"constructive","corpus":"vision-engine rt","generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.node.v2","title":"RT2 native S0+S1a kernel bit contract"}
```
<!-- PM:END pm.node.v2 -->

<!-- PM:BEGIN pm.reading.v2 -->
```json
{"entry_rule":"If you write a kernel read 1, 2, 4. If you verify it read 4, 5, 6. If you plan the fused path read 3, 6.","full_body_anchor":"pm-s7","generation_status":"COMPLETE","personas":{"kernel_writer":["pm-s1","pm-s2","pm-s4"],"planner":["pm-s3","pm-s6"],"verifier":["pm-s4","pm-s5","pm-s6"]},"pm_id":"HB-VNS0S1-001","schema":"pm.reading.v2"}
```
<!-- PM:END pm.reading.v2 -->

| question | go to |
|---|---|
| what bytes must a slot kernel produce? | hb-s1, hb-s2 |
| in what order do slots replace Python? | hb-s3 |
| how is a divergence found? | hb-s4 (Panel 3) |
| which rule must I not break? | hb-s5 |
| what is not done yet? | hb-s6 |

Negative exclusions: crate layout, FFI guard, `par.rs` and Rayon belong to `HB-VN-n1-native`; S2 and later stages are not covered; xh64 hashing is not a native slot here.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contract of the toolchain

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","claims":[],"code_anchors":["vnative/backend.py::KERNELS","vnative/backend.py::check_available"],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"contracts","n":1,"purpose":"Slot signatures, refusal rule, error vocabulary and determinism of the native S0+S1a kernels.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Contract of the toolchain"}
```
<!-- HB:END -->

Slots (names proposed, bit semantics fixed by `visioncore/desc.py` and `visioncore/grid.py`; the engine contract wins over any design table):

| slot | Python reference | native name (TARGET) |
|---|---|---|
| pad | `Grid.pad` | `s0::pad_into` |
| luma | `desc.luma_of_padded` | `s0::luma_of_padded` |
| words_eq | per-tile byte equality of padded RGB (`desc.describe_padded`) | `s0::words_eq` |
| tile fields | `desc._compute` | `s1::compute_tiles` |
| isqrt | `desc.isqrt` | `isqrt::isqrt_u64` |

- Backend modes are `python`, `native`, `dual_verify` (`VISIONCORE_BACKEND`, read only in `vnative/backend.py`). With `KERNELS_REAL=False`, `check_available('dual_verify')` raises `E_VN_NATIVE_MISSING` with `kernels_real` false even when a library file exists [MEASURED, R-vn-s0s1-unittest].
- `E_VN_*` codes are `VNativeError` codes from `vnative/`; they are not registered in `visioncore/errors.py` `CODES` (no rc class assigned there, so the caller maps them). Engine exit codes come from `RC_CLASSES` (0, 2, 3, 4, 5, 6), rc 1 is banned; shape errors are `E_VC_FRAME_SHAPE` and `E_GRID`, both rc class 2.
- Divergence is reported as `E_VN_DIVERGE {name, first_index, field}` by `vnative/dual.py`; a missing `Cargo.lock` makes `tools/build_native.py` refuse (rc 3), never a silent fallback to Python.
- Deterministic: integer-only arithmetic, no clock, no float reduction, no RNG; `TAU_EDGE` is an input parameter (range 1..255) and is never a literal inside a kernel.

<!-- PM:BEGIN pm.claims.v2 -->
```json
{"claims":[{"etag":"MEASURED","id":"C-VNS0S1-REFUSE","receipt":"R-vn-s0s1-unittest","source":"[MEASURED, R-vn-s0s1-unittest]","statement":"dual_verify refuses with E_VN_NATIVE_MISSING while KERNELS_REAL is False."},{"etag":"MEASURED","id":"C-VNS0S1-REFORIG","receipt":"R-vn-s0s1-unittest","source":"[MEASURED, R-vn-s0s1-unittest]","statement":"backend.reference() yields the original desc kernels captured at import."}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.claims.v2"}
```
<!-- PM:END pm.claims.v2 -->

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s2","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"types_budgets","n":2,"purpose":"Output byte formats of the tile record fields the S1a slot writes, constants and TARGET budgets.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Types, weights and budgets"}
```
<!-- HB:END -->

Tile record: 32 B [MEASURED, R-vn-s0s1-unittest] with 14 fields in this order: `morton, cls, flags, y_q4, co_h, cg_h, sig_q4, grad_q6, edge, resid, mv_dx, mv_dy, sig64, xh64` [MEASURED, R-vn-s0s1-unittest]. The slot `compute_tiles` writes exactly `y_q4` (u16), `co_h` (i8), `cg_h` (i8), `sig_q4` (u16), `grad_q6` (u16), `edge` (u8), `sig64` (u64) and `flags` (u8, F_EDGE, F_TEXTURE, F_SAT, F_PAD); `morton` is set by the caller; `xh64` is filled lazily in Python.

Formulas (n = s*s, b = s/8, floor division everywhere; y, co, cg are the YCoCg-R planes):

- `y_q4 = 16*Σy // n`; `co_h = clip(Σco // (2n), -128, 127)`; `cg_h` likewise; `sig_q4 = 16*isqrt(n*Σy² - (Σy)²) // n`.
- `grad_q6 = 64*Σgs // n` with `gs = |dx| + |dy|` inside the tile only (last column and row contribute 0); `edge = 255*#{gs > TAU_EDGE} // n`.
- `sig64` bit k = sub-block k (row-major 8x8 blocks) strictly above the mean: `sub*n > Σy*b*b`, little-endian bit order.
- Accumulators: i32 while s <= 128, widened to i64 before any product; a subtraction of unsigned projections is widened to i64 first.

<!-- PM:BEGIN pm.objects.v2 -->
```json
{"definitions":[{"definition":"32 B packed little-endian record, 14 fields, Morton rank order [MEASURED, R-vn-s0s1-unittest]","id":"OBJ-TILE","name":"tile record"},{"definition":"A function the backend may swap: luma_of_padded, _compute (live) and grid.pad, words_eq (RT2 slots, None today) [MEASURED, R-vn-s0s1-unittest]","id":"OBJ-SLOT","name":"kernel slot"}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.objects.v2"}
```
<!-- PM:END pm.objects.v2 -->

<!-- PM:BEGIN pm.quantities.v2 -->
```json
{"entries":[{"id":"Q-TILE-SIZE","name":"TILE_SIZE","source":"[MEASURED, R-vn-s0s1-unittest]","value":"32 B"},{"id":"Q-FIELDS","name":"TILE_FIELDS count","source":"[MEASURED, R-vn-s0s1-unittest]","value":"14"},{"id":"Q-CELL-MAX","name":"CELL_MAX","source":"[MEASURED, R-vn-s0s1-unittest]","value":"4096"},{"id":"Q-ISQRT-DOMAIN","name":"isqrt domain","source":"[MEASURED, R-vn-s0s1-unittest]","value":"[0, 2^52)"},{"id":"Q-BUDGET-720","name":"S0+S1a 720p budget","source":"[TARGET, CARDS_RT.md:L112-117]","value":"2.0 ms"},{"id":"Q-BUDGET-4K","name":"S0+S1a 4K s=64 budget","source":"[TARGET, CARDS_RT.md:L112-117]","value":"16 ms"}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.quantities.v2"}
```
<!-- PM:END pm.quantities.v2 -->

Budgets are `[TARGET]` only; no native measurement exists (rt2 bench receipt AUSENTE).

## <a id="hb-s3"></a><a id="pm-s3"></a>3. Build and routing DAG

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s3","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"dag","n":3,"purpose":"Order in which slot kernels replace Python and where the fused describe_frame sits.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Build and routing DAG"}
```
<!-- HB:END -->

1. Slot kernels first, each behind its own `dual_verify` check: `isqrt_u64`, `s0::luma_of_padded`, `s0::pad_into`, `s0::words_eq`, `s1::compute_tiles`.
2. Only after every slot passes the corpus D2 (hb-s4) may `KERNELS_REAL` become True.
3. Fused `describe_frame` (virtual clamp `min(x, W-1)`, `min(y, H-1)`, no padded copy) is a `[TARGET]` reached through `vnative/`, gated by the same `dual_verify` over all outputs.
4. xh64 stays Python (BLAKE2b-64, person `vqa-tile`, `desc.xh64_of`); no blake2 crate is vendored. Build only through `tools/build_native.py --offline --locked`.

<!-- PM:BEGIN pm.roadmap.v2 -->
```json
{"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.roadmap.v2","steps":[{"id":"STEP-1","status":"TARGET","text":"isqrt_u64 on [0, 2^52) with the two correction guards"},{"id":"STEP-2","status":"TARGET","text":"s0 luma, pad, words_eq as slot kernels"},{"id":"STEP-3","status":"TARGET","text":"s1 compute_tiles into a caller-owned buffer"},{"id":"STEP-4","status":"TARGET","text":"KERNELS_REAL True after dual_verify on corpus D2"},{"id":"STEP-5","status":"TARGET","text":"fused describe_frame behind the same dual_verify"}]}
```
<!-- PM:END pm.roadmap.v2 -->

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Engineering triptych

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s4","claims":[],"code_anchors":["vnative/backend.py::reference","vnative/dual.py::assert_same","vnative/run.py::compare_runs","vnative/run.py::run"],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"triptych","n":4,"purpose":"Panel 1 contract, Panel 2 anti-uses, Panel 3 = the dual_verify oracle (P3.v1) with a 7-field header per slot.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Engineering triptych"}
```
<!-- HB:END -->

**Panel 1 (contract).** Every slot kernel is byte-identical to its Python reference over corpus D2; the reference leg is `backend.reference()`.

**Panel 2 (anti-uses).** No truncating division on a signed sum; no `>=` in sig64; no zero padding; no luma-only xh64; no unguarded `#[pyfunction]`; no heap allocation inside the tile loop; no sparse raw-pointer writes (parallel writes only over disjoint tile-record chunks with a dense bitset); no literal 48 for `TAU_EDGE`; no per-frame `Vec` for luma (the kernel writes into a caller `&mut [i16]`).

**Panel 3 (oracle P3.v1).** Reference leg `vnative.backend.reference()`; compare `vnative.run.compare_runs` plus `vnative.dual.assert_same`, reporting `{name, first_index, field}`. Corpus D2: sizes 1x1, 15x15, 17x17, 16x16, 100x40, 4097x1, 1x300 (each s in {16, 32, 64, 128} reached by the grid cases); content random (SplitMix64), constant 0/255, checker 1 px, R-B = -1 tiles. Property legs run on the Python reference: P1 isqrt at k²-1, k², k²+1 for k < 2^22 plus 2^52-1; P2 YCoCg-R factored into (r,b) and (g,t) planes of 2^16 values with all 2^24 triples checked against luma; P3 Morton bijection on 256x256. Mutants run on a scratch copy and are killed only by the named test failing with rc in {1, 5} after a clean control.

Panel 3 mutant table: `m_co_h_trunc_div` killed by `test_d2_negative_co_floor`; `m_sig64_ge` by `test_d2_constant_tiles_sig64_strict`; `m_isqrt_off_by_one` by `test_p1_isqrt_boundaries`; `m_pad_zero` by `test_d2_pad_is_index_clamp`; `m_tau_hardcoded` by `test_d2_tau_is_a_parameter`. Receipt kept outside this file (`build/room_N/mutants_receipt.json`, killed equals total) [MEASURED, R-vn-s0s1-unittest]. Native mutants are `[TARGET]` until a toolchain exists.

```python
# PM-P3-HEADER: s0::pad_into
# INPUTS: contiguous C-order u8 RGB buffer, W, H, tile edge s, grid gw, gh, caller output buffer.
# PRECONDITIONS: s in {16, 32, 64, 128}; shape (H, W, 3) checked with the GIL held.
# OUTPUTS: padded u8 [gh*s, gw*s, 3] equal to Grid.pad (edge replication by index clamp).
# TIME: O(gw*s*gh*s).
# SPACE: caller output buffer only.
# ARITHMETIC: integer index clamp min(x, W-1), min(y, H-1); no zero fill.
# FAILURE: shape mismatch -> E_VC_FRAME_SHAPE class rc 2 via guard; panic -> runtime error.
# PM-P3-HEADER: s0::luma_of_padded
# INPUTS: padded u8 RGB buffer [P, Q, 3] and caller output &mut [i16] of P*Q.
# PRECONDITIONS: contiguous C order; output length P*Q.
# OUTPUTS: i16 plane equal to desc.luma_of_padded, range 0..255.
# TIME: O(P*Q).
# SPACE: no allocation (caller buffer).
# ARITHMETIC: i16 lift t = b + ((r-b) >> 1), y = t + ((g-t) >> 1), arithmetic shift (floor).
# FAILURE: wrong lengths -> guard error mapped to rc 2.
# PM-P3-HEADER: s0::words_eq
# INPUTS: padded current and previous frames, grid, output bool buffer of N.
# PRECONDITIONS: both frames have shape (gh*s, gw*s, 3).
# OUTPUTS: bool[N] in Morton rank order, true iff the 3*s*s padded RGB bytes of the tile are equal.
# TIME: O(gh*s*gw*s).
# SPACE: output buffer only.
# ARITHMETIC: byte equality, no hashing.
# FAILURE: shape mismatch -> guard error mapped to rc 2.
# PM-P3-HEADER: s1::compute_tiles
# INPUTS: output &mut [u8] of N*32, idx &[u32] ascending Morton ranks, padded RGB, y_plane i16, grid, tau_edge.
# PRECONDITIONS: idx < N; y_plane equals luma_of_padded(padded); tau_edge passed in (1..255).
# OUTPUTS: the fields y_q4, co_h, cg_h, sig_q4, grad_q6, edge, sig64, flags of out[idx] at TILE_FIELDS offsets; one record commit per tile.
# TIME: O(len(idx)*s*s).
# SPACE: locals per tile, no heap allocation.
# ARITHMETIC: i32 sums for s <= 128 widened to i64; floor via shift or div_euclid; sig64 strict compare sub*n > sum_y*b*b.
# FAILURE: bad index or length -> guard error mapped to rc 2; panic -> runtime error.
# PM-P3-HEADER: isqrt::isqrt_u64
# INPUTS: v as u64.
# PRECONDITIONS: 0 <= v < 2^52.
# OUTPUTS: r with r*r <= v < (r+1)*(r+1), equal to desc.isqrt.
# TIME: O(1).
# SPACE: O(1).
# ARITHMETIC: f64 seed (correctly rounded sqrt) then r -= r*r > v; r += (r+1)*(r+1) <= v on i64.
# FAILURE: v outside the domain -> out of contract, caller checks.
# PM-P3-HEADER: backend.reference
# INPUTS: none (context manager).
# PRECONDITIONS: desc not patched outside a live installed() scope.
# OUTPUTS: scope in which desc runs the original kernels captured at import.
# TIME: O(number of swapped names).
# SPACE: O(1).
# ARITHMETIC: none.
# FAILURE: E_VN_SCOPE when desc is patched outside installed().
# PM-P3-HEADER: dual.assert_same
# INPUTS: name, two arrays or byte strings.
# PRECONDITIONS: none.
# OUTPUTS: None when dtype, shape and bytes agree.
# TIME: O(bytes).
# SPACE: O(1).
# ARITHMETIC: exact equality, no tolerance.
# FAILURE: E_VN_DIVERGE {name, first_index, field}.
```

<!-- PM:BEGIN pm.triptych.v2 -->
```json
{"generation_status":"COMPLETE","panel1":{"contract":"byte-identical slot kernels over corpus D2"},"panel2":{"anti_uses":["truncating division","ge in sig64","zero padding","literal TAU_EDGE","sparse raw-pointer writes"]},"panel3":{"compare":"vnative.run.compare_runs","oracle":"P3.v1","reference":"vnative.backend.reference()","report":["name","first_index","field"]},"pm_id":"HB-VNS0S1-001","schema":"pm.triptych.v2"}
```
<!-- PM:END pm.triptych.v2 -->

<!-- HB:BEGIN hb.claim.v2 -->
```json
{"anchors":["vnative/backend.py::KERNELS_REAL"],"etag":"MEASURED","id":"C-VNS0S1-KERNELS-REAL","kind":"behaviour","receipts":["R-vn-s0s1-unittest"],"schema":"hb.claim.v2","statement":"KERNELS_REAL is False, so dual_verify refuses with E_VN_NATIVE_MISSING.","value":{"metric_ref":"hyperbooks/HB-VN-s0s1/receipts/R-vn-s0s1-unittest.json:metrics.failures","number":"0","op":"==","unit":"count"}}
```
<!-- HB:END -->

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s5","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"subsystems","n":5,"purpose":"Slot table and reference leg; dual comparator. Invariants and anti-uses with governed symbols.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Subsystems"}
```
<!-- HB:END -->

<!-- PM:BEGIN pm.domains.v2 -->
```json
{"domains":[{"anchor":"hb-s5.backend","id":"DOM-SLOTS","name":"slot table and reference leg"},{"anchor":"hb-s5.dual","id":"DOM-DUAL","name":"dual comparator"}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.domains.v2"}
```
<!-- PM:END pm.domains.v2 -->

### <a id="hb-s5.backend"></a>5.1 Slot table and reference leg

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s5.backend","claims":[],"code_anchors":[],"covers":"vnative/backend.py","diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"subsystems","n":5,"purpose":"Bit contract of the slots registered in vnative/backend.py and the original-kernel reference.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Slot table and reference leg"}
```
<!-- HB:END -->

`KERNELS` is the slot table (`luma_of_padded`, `_compute` live; `grid.pad`, `words_eq` are `None` RT2 slots); `reference()` runs the originals captured at import.

<!-- HB:BEGIN hb.inv.v2 -->
```json
{"au":[{"id":"AU-VNS01-TRUNC","symbols":["vnative/backend.py::KERNELS"],"text":"Using Rust or C `/` on a negative sum (truncates toward zero) where Python used // or >>."},{"id":"AU-VNS01-SILENT-FALLBACK","symbols":["vnative/backend.py::check_available"],"text":"Falling back to Python when the library is missing inside a dual test; the pair would compare Python with itself."},{"id":"AU-VNS01-TAU-LITERAL","symbols":["vnative/backend.py::KERNELS"],"text":"Hard-coding 48 for TAU_EDGE inside a kernel; it is a parameter (m_tau_hardcoded)."}],"inv":[{"id":"INV-VNS01-FLOOR","symbols":["vnative/backend.py::KERNELS"],"text":"Every signed division by 2n is a floor (shift or div_euclid); a tile with sum_co = -1 gives co_h = -1. Truncation toward zero is a defect (m_co_h_trunc_div)."},{"id":"INV-VNS01-SIG64-STRICT","symbols":["vnative/backend.py::KERNELS"],"text":"sig64 bit k is set iff sub*n > sum_y*b*b (strict, no division, LE bit order); a constant tile has sig64 = 0 (m_sig64_ge)."},{"id":"INV-VNS01-ISQRT-DOMAIN","symbols":["vnative/backend.py::KERNELS"],"text":"isqrt is exact on [0, 2^52): r*r <= v < (r+1)*(r+1); checked at k\u00b2-1, k\u00b2, k\u00b2+1 and 2^52-1 (m_isqrt_off_by_one)."},{"id":"INV-VNS01-REFERENCE-ORIG","symbols":["vnative/backend.py::reference"],"text":"backend.reference() yields the _ORIG objects captured at import and never a kernel-table entry; it refuses (E_VN_SCOPE) when desc is patched outside installed()."},{"id":"INV-VNS01-REFUSE","symbols":["vnative/backend.py::check_available"],"text":"No native kernel is selectable until KERNELS_REAL is True and dual_verify passes; dual_verify with KERNELS_REAL False raises E_VN_NATIVE_MISSING."}],"module":"vnative/backend.py"}
```
<!-- HB:END -->

### <a id="hb-s5.dual"></a>5.2 Dual comparator

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s5.dual","claims":[],"code_anchors":[],"covers":"vnative/dual.py","diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"subsystems","n":5,"purpose":"First-divergence reporting of vnative/dual.py and the run-level comparison.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Dual comparator"}
```
<!-- HB:END -->

`assert_same` and `assert_seq_same` compare exactly (eps = 0). `run.compare_runs` checks tiles, regions, links, global motion, frame rows, GOP files and blobs.

<!-- HB:BEGIN hb.inv.v2 -->
```json
{"au":[{"id":"AU-VNS01-BISECT","symbols":["vnative/dual.py::assert_seq_same"],"text":"Bisecting or comparing only the last frame; it hides the first diverging tile."}],"inv":[{"id":"INV-VNS01-FIRST-INDEX","symbols":["vnative/dual.py::assert_same"],"text":"A divergence names the first differing record index and the field; dtype or shape mismatches report first_index -1."},{"id":"INV-VNS01-EXACT","symbols":["vnative/dual.py::assert_same","vnative/dual.py::assert_seq_same"],"text":"Comparison is byte exact over dtype, shape and bytes; no tolerance, no final-state-only check."}],"module":"vnative/dual.py"}
```
<!-- HB:END -->

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search entries

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s6","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"limits_search","n":6,"purpose":"What is not done: stubs, KERNELS_REAL False, Cargo.lock present but kernels stubbed, fused describe_frame as TARGET; blocked uses and search entries.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Limits and search entries"}
```
<!-- HB:END -->

- `KERNELS_REAL=False`; `dual_verify` refuses with `E_VN_NATIVE_MISSING`.
- `rust/visioncore_native/Cargo.lock` is present (commit eac6d05); `tools/build_native.py` builds `--offline --locked` and the build is reproducible, `build/native/visioncore_native.abi3.so` sha 5d60d879 (status OK); the .so carries no S0/S1 kernel (stubs) and `tools/build_native.py` still refuses rather than build unlocked.
- Fused `describe_frame` is a `[TARGET]`; no native number exists. SVE vector width on the host is 4 words [CITED, EX-SW-0089-SIMD]; a call detached from the interpreter lock contributes zero to a pause bound [CITED, EX-SW-0143-GIL].
- Every `unsafe` block needs a `// SAFETY:` comment [CITED, EX-OC-RUST08-UNSAFE]; builds use a lock file [CITED, EX-OC-RUST14-LOCK].

<!-- PM:BEGIN pm.limits.v2 -->
```json
{"entries":[{"blocked_use":"Do not select native or dual_verify; it raises E_VN_NATIVE_MISSING.","id":"LIM-STUBS","text":"s0.rs, s1.rs, isqrt.rs are stubs; KERNELS_REAL False"},{"blocked_use":"Do not build without --offline --locked; a missing lock is AUSENTE rc class 3, never a silent fallback.","id":"LIM-LOCK","text":"Cargo.lock present (eac6d05); offline locked build reproducible, .so sha 5d60d879; builds stay --offline --locked"},{"blocked_use":"Do not quote a fused speedup; no measurement exists.","id":"LIM-FUSED","text":"fused describe_frame is TARGET"},{"blocked_use":"Do not write a native BLAKE2b slot here; no crate is vendored.","id":"LIM-XH64","text":"xh64 stays Python"},{"blocked_use":"Do not hash only the luma plane; xh64 hashes padded RGB bytes.","id":"LIM-LUMA-ONLY","text":"luma-only xh64 is not the contract"}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.limits.v2"}
```
<!-- PM:END pm.limits.v2 -->

<!-- PM:BEGIN pm.search.v1 -->
```json
{"exclusions":["crate layout and FFI guard (HB-VN-n1-native)","S2 and later stages","native BLAKE2b"],"pm_id":"HB-VNS0S1-001","questions_answered":["why does dual_verify refuse","what bytes does compute_tiles write","which isqrt domain is required","how is a native divergence reported","where is the reference leg"],"schema":"pm.search.v1"}
```
<!-- PM:END pm.search.v1 -->

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative: one frame through dual_verify

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s7","claims":[],"code_anchors":[],"diataxis":"reference","hb_id":"HB-VNS0S1-001","kind":"narrative","n":7,"purpose":"One end-to-end trace of a frame through the reference leg, the native leg and the comparison.","route":{"emit":true,"tags":["software/vision-native"]},"schema":"hb.section.v2","title":"Narrative: one frame through dual_verify"}
```
<!-- HB:END -->

`vnative.run.run(src, mode="dual_verify")` calls `backend.resolve`, then `check_available` (today it raises here with `E_VN_NATIVE_MISSING`). When RT2 sets `KERNELS_REAL` True: the frames are listed once, the reference leg runs under `backend.reference()`, the native leg under `backend.installed()`, `compare_runs` scans every artefact and either returns silently or raises `E_VN_DIVERGE {name, first_index, field}`; the Python run is returned. The Panel 3 tests exercise both branches with identity shims and one planted `sig64` divergence.

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated)

<!-- HB:GEN BEGIN hb-s8 -->
### Files

| path | lines | bytes | sha256 |
|---|---|---|---|
| `vnative/__init__.py` | 1 | 113 | `43228c8aeb0bbbd0` |
| `vnative/backend.py` | 105 | 3873 | `db83fd65a2c03196` |
| `vnative/dual.py` | 46 | 1885 | `db4baf2a74fad176` |
| `vnative/run.py` | 63 | 3194 | `ef5a5a41f43776ef` |

### Symbols (tools)

| id | kind | lines | signature | purpose | governs |
|---|---|---|---|---|---|
| `vnative/backend.py::ENV` | constant | 9-9 | `ENV` | - | hb-s5.backend |
| `vnative/backend.py::KERNELS` | constant | 32-32 | `KERNELS` | - | hb-s1, hb-s5.backend, INV-VNS01-FLOOR, INV-VNS01-ISQRT-DOMAIN, INV-VNS01-SIG64-STRICT, AU-VNS01-TAU-LITERAL, AU-VNS01-TRUNC |
| `vnative/backend.py::KERNELS_REAL` | constant | 12-12 | `KERNELS_REAL` | - | hb-s5.backend |
| `vnative/backend.py::MODES` | constant | 10-10 | `MODES` | - | hb-s5.backend |
| `vnative/backend.py::NATIVE_LIB` | constant | 11-11 | `NATIVE_LIB` | - | hb-s5.backend |
| `vnative/backend.py::RT2_SLOTS` | constant | 33-33 | `RT2_SLOTS` | - | hb-s5.backend |
| `vnative/backend.py::VNativeError` | class | 17-20 | `VNativeError(Exception)` | - | hb-s5.backend |
| `vnative/backend.py::VNativeError.__init__` | method | 18-20 | `__init__(self, code, detail=None)` | - | - |
| `vnative/backend.py::_scope` | function | 73-88 | `_scope(table)` | - | - |
| `vnative/backend.py::_shim_compute` | function | 27-28 | `_shim_compute(out, idx, padded, grid, p, y_plane)` | - | - |
| `vnative/backend.py::_shim_luma` | function | 23-24 | `_shim_luma(padded)` | - | - |
| `vnative/backend.py::_swap` | function | 64-70 | `_swap(table)` | - | - |
| `vnative/backend.py::check_available` | function | 54-61 | `check_available(mode, lib=None)` | - | hb-s1, hb-s5.backend, INV-VNS01-REFUSE, AU-VNS01-SILENT-FALLBACK |
| `vnative/backend.py::installed` | function | 91-95 | `installed(kernels=None)` | - | hb-s5.backend |
| `vnative/backend.py::originals` | function | 40-41 | `originals()` | - | hb-s5.backend |
| `vnative/backend.py::reference` | function | 98-105 | `reference()` | - | hb-s4, hb-s5.backend, INV-VNS01-REFERENCE-ORIG |
| `vnative/backend.py::resolve` | function | 44-51 | `resolve(env=None)` | - | hb-s5.backend |
| `vnative/dual.py::_div` | function | 7-8 | `_div(name, idx, field)` | - | - |
| `vnative/dual.py::assert_same` | function | 11-37 | `assert_same(name, a, b)` | - | hb-s4, hb-s5.dual, INV-VNS01-EXACT, INV-VNS01-FIRST-INDEX |
| `vnative/dual.py::assert_seq_same` | function | 40-46 | `assert_seq_same(name, a, b)` | - | hb-s5.dual, INV-VNS01-EXACT, AU-VNS01-BISECT |
| `vnative/run.py::_ListSource` | class | 54-63 | `_ListSource` | - | - |
| `vnative/run.py::_ListSource.__init__` | method | 55-57 | `__init__(self, src, frames)` | - | - |
| `vnative/run.py::_ListSource.__iter__` | method | 62-63 | `__iter__(self)` | - | - |
| `vnative/run.py::_ListSource.__len__` | method | 59-60 | `__len__(self)` | - | - |
| `vnative/run.py::_jb` | function | 7-8 | `_jb(o)` | - | - |
| `vnative/run.py::compare_runs` | function | 11-27 | `compare_runs(a, b)` | - | hb-s4 |
| `vnative/run.py::run` | function | 30-51 | `run(src, params=None, hints=None, out_dir=None, workers=1, cpus=None, mode=None, env=None, lib=None)` | - | hb-s4 |

### Fixture symbols (Godot)

| id | kind | lines |
|---|---|---|
<!-- HB:GEN END hb-s8 -->

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges (generated)

<!-- HB:GEN BEGIN hb-s9 -->
### Imports (module to module)

| module | imports |
|---|---|
| `vnative/__init__.py` | - |
| `vnative/backend.py` | - |
| `vnative/dual.py` | - |
| `vnative/run.py` | - |

### Calls (tool symbols, resolved static edges)

| symbol | calls |
|---|---|
| `vnative/backend.py::_scope` | `VNativeError`, `VNativeError.__init__`, `_swap` |
| `vnative/backend.py::check_available` | `VNativeError`, `VNativeError.__init__` |
| `vnative/backend.py::installed` | `_scope` |
| `vnative/backend.py::reference` | `VNativeError`, `VNativeError.__init__`, `_scope` |
| `vnative/backend.py::resolve` | `VNativeError`, `VNativeError.__init__` |
| `vnative/dual.py::assert_same` | `_div` |
| `vnative/dual.py::assert_seq_same` | `_div` |
| `vnative/run.py::compare_runs` | `_jb` |
| `vnative/run.py::run` | `_ListSource`, `_ListSource.__init__`, `compare_runs` |

### Tests (runtime trace, most specific first)

| symbol | tests |
|---|---|
<!-- HB:GEN END hb-s9 -->

<!-- PM:BEGIN pm.edges.v2 -->
```json
{"edges":[{"from":"HB-VNS0S1-001","kind":"uses","note":"crate, FFI guard, build and parallelism are owned there","to":"HB-VN-n1-native"},{"from":"HB-VNS0S1-001","kind":"verifies","note":"Python reference leg","to":"HB-VC-s1-desc"},{"from":"HB-VNS0S1-001","kind":"verifies","note":"Grid.pad reference","to":"HB-VC-s0-grid"}],"generation_status":"COMPLETE","pm_id":"HB-VNS0S1-001","schema":"pm.edges.v2"}
```
<!-- PM:END pm.edges.v2 -->

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance (generated)

<!-- HB:GEN BEGIN hb-s10 -->
Source files at extraction time (sha256 prefix, bytes):

| path | sha256 | bytes |
|---|---|---|
| `vnative/__init__.py` | `43228c8aeb0bbbd0` | 113 |
| `vnative/backend.py` | `db83fd65a2c03196` | 3873 |
| `vnative/dual.py` | `db4baf2a74fad176` | 1885 |
| `vnative/run.py` | `ef5a5a41f43776ef` | 3194 |

Extractor `hb_extract2.py/2.0` (Python `ast`, rules 1-5 of the call resolver); runtime trace edges: 0; symbols: 31; graph nodes: 37. Tokeniser: router v2 `toks`; sizes are UTF-8 byte counts.
<!-- HB:GEN END hb-s10 -->

