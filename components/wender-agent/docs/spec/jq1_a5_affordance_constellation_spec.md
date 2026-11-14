# JQ1 A5: pixel affordance constellation reference contract

## 1. Status and source boundary

This is **proposed reference v1**, implemented only in `tests/test_jq1_a5_affordance_constellation_spec.py`. It specifies deterministic CPU candidate geometry, descriptors, constellation matching, abstaining decisions, and integer evaluation. Its algorithms, encodings, quantizers, and thresholds are test-local choices. Passing this harness does not install a production detector, validate a deployment, or authorize actions.

The bound repository is `example-owner/wender-agent`, read through the GitHub connector at the explicit ref `bridge/integration`, resolved in this session to `f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0`. Current-behavior citations below refer to that snapshot. Task metadata mentions the older commit `3d9a4da823936d96588cf15a7e94bf9250fc2ed5`; the source actually retrieved at the requested ref takes precedence. No default-branch content or second checkout supplies implementation claims.

The earlier `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:5` binds itself to `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9`. Its statement that executor enforcement is absent because its actor was a stub (`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:103`, `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:337`) must not be carried forward as the current executor contract. The current `cdp_executor` checks initial and fresh match levels and ties (`wender/act/cdp_executor.py:229–250`). The older document's missing-fingerprint finding remains relevant (`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:320`); its narrow statement about the unchanged `vision_bridge` stub also remains true (`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:157`).

### Implemented baseline

| Surface | Behavior established by bound code |
|---|---|
| RAM frame | `Frame` is a frozen RGB24 byte buffer, with positive integer dimensions, booleans excluded, and exact `width * height * 3` byte length (`wender/perceive/vision_adapter.py:20–37`). |
| Adapter interface | The abstract interface supplies only `screen_identity` and `changed_regions` (`wender/perceive/vision_adapter.py:40–52`). |
| Fake identity and differences | The fake identity hashes dimensions and integer mean RGB values per tile; differences use integer mean absolute RGB-channel differences and four-connected changed-tile regions. Neither produces widget semantics (`wender/perceive/vision_adapter.py:67–157`). |
| Fusion | `fuse_regions` independently floors device x/y/width/height to CSS coordinates, intersects existing STATE node boxes, copies actionability, and sorts hits. It has no IoU assignment or affordance calibration (`wender/perceive/vision_adapter.py:161–219`). |
| Legacy bridge | Both entry points raise `NotImplementedError("vision-engine bridge is a v0 stub")`; its RAM integration docstring is explicitly TARGET (`wender/perceive/vision_bridge.py:4–18`). |
| Separate real engine adapter | `RealVisionEngine` requires an external engine checkout, transfers frames through RAM pipes, and delegates fingerprints/comparison or changed regions to a child interpreter. The child rejects filesystem mutation. This adapter exists independently of the legacy stub; it supplies no affordance detector (`wender/perceive/vision_engine_real.py:34–79`, `wender/perceive/vision_engine_real.py:84–146`). |
| STATE and serialization | Source kinds are `cdp`, `atspi`, and `godot`; node fields are fixed. `schema.dumps` rejects nested floats and returns sorted compact JSON text, without calling `validate_state` (`wender/state/schema.py:11–17`, `wender/state/schema.py:24–39`, `wender/state/schema.py:102–116`). |
| Disabled structured controls | STATE actionability requires an allowed role and `disabled != "true"`; this is a structured attribute, not a pixel inference (`wender/state/schema.py:58–68`). |
| Existing tests | The bound sibling test constructs RGB frames in RAM and checks fake identity, changed regions, four-connectivity, edge tiles, and fusion at DPR 2.0. It does not define this proposed descriptor or detector (`tests/test_vision_adapter.py:15–24`, `tests/test_vision_adapter.py:67–206`). |

### Explicit absence register

These absences are scoped to the eight bound source files, rather than an unsupported claim about every repository file.

| Requested production capability | Status |
|---|---|
| production detector | **AUSENTE**: the bound vision APIs supply identity/differences or raise; no pixel affordance proposal API is implemented. |
| d48 layout | **AUSENTE**: the 48-byte layout in section 5 is a new test-local proposal. |
| hash encoding | **AUSENTE**: the constellation and pair domains, fields, endianness, and truncation in sections 6–7 are new proposals. Existing unrelated hashes do not define them. |
| calibration API | **AUSENTE**: count pooling, lookup, and selective policy below are test-local. |
| vision state kind | **AUSENTE**: `SOURCE_KINDS` and `validate_state` reject `source.kind="vision"` (`wender/state/schema.py:12`, `wender/state/schema.py:103–104`). |

## 2. Input, output, and numeric contract

The detector input is a nonempty NumPy `uint8[H,W,3]` RGB array and a strictly positive Python integer `dpr_ppm`. `PPM=1000000`; the test DPIs are `1000000`, `1250000`, and `2000000`. Pixel proposals receive no accessibility tree, hidden enabled-state label, browser handle, or oracle role. Templates and fitted calibration tables are explicit inputs to later reference stages.

Reference boxes use **half-open endpoints** `[x0,y0,x1,y1]`, unlike the production fusion API's `[x,y,w,h]`. `box4` requires four Python integers in signed 32-bit range and non-reversed endpoints; zero-area boxes are allowed for geometry evaluation. Descriptor extraction separately requires a nonempty in-frame box. The integer validator rejects booleans and floats. NumPy work uses integer arrays; extracted scalar components and serialized counts are converted to Python integers. Geometry areas, rational comparisons, and squared distances use Python integer arithmetic, preserving values larger than signed 64-bit products where necessary.

`reference_detect` returns a separate envelope with schema `jq1-a5-affordance-reference/v1`, `dpr_ppm`, and ordered `proposals`. Each proposal has a deterministic `p0000`-style id, CSS endpoint box, role hint, probe label, `fp_struct`, match hypotheses, and selective-decision evidence. This envelope is not `wender.state/v1`. Canonical serialization with `schema.dumps` proves absence of output floats; it does not confer STATE compatibility.

All frames, crops, edge masks, fixtures, and fake action calls remain in RAM. The implementation uses stdlib and NumPy, with no GPU, browser service, network, external engine invocation, or image-file output. Reading this document and its sibling Python source as UTF-8 is permitted; the documentation test creates no artifacts.

## 3. Candidate proposals from original pixels

### 3.1 Luma and signed Sobel

Convert each RGB pixel with the exact integer formula:

```text
Y = (77*R + 150*G + 29*B + 128) >> 8
```

Promote channels to signed `int32` before multiplication. The literal black, white, red, green, and blue results are respectively `0,255,77,149,29`. This is the reference formula, not a claim about the fake adapter's different RGB mean operation.

Replicate the outer pixel border before applying Sobel. `Gx` uses kernel rows `[-1,0,1]`, `[-2,0,2]`, `[-1,0,1]`; `Gy` uses `[-1,-2,-1]`, `[0,0,0]`, `[1,2,1]`. Positive x points right and positive y points down. Magnitude is `abs(Gx)+abs(Gy)`, without a square root. The binary original edge mask is `magnitude >= 128`. A black-to-white vertical step produces `Gx=1020`; reversing it gives `-1020`, with zero `Gy` in the fixture.

### 3.2 Components and contour evidence

Find eight-connected components of the original binary edge mask. Scan seeds row-major; traverse the eight neighboring offsets deterministically; return half-open bounds sorted by `(y0,x0,y1,x1)`. Undo the one-device-pixel Sobel halo by moving each component boundary inward one pixel. Retain only components with pre-shrink width/height at least five and boundaries strictly inside the image. Components touching the image edge are conservatively omitted because their contours cannot be established completely.

For each resulting ink box, evaluate its four sides independently in **top, right, bottom, left** order. A horizontal sample is supported when any original edge exists within one device pixel vertically at that x. A vertical sample uses the corresponding horizontal neighborhood. Side support is `floor(255 * supported_positions / side_length)`. A corner may contribute to both incident sides.

Only the original Sobel edge mask is evidence. No dilation, morphological closing, filled rectangle, or hypothesized missing side may provide contour support. Rectangle candidates require CSS width and height at least six and all four support values at least `200`. Sort candidates by descending device area, then y, x, and the full endpoint tuple. Keep a candidate only when it does not have IoU at least one-half with an already-kept rectangle. This favors outer contours and suppresses duplicate inner borders. It can also suppress a heavily overlapping real control; the reference does not claim perfect nested-control recall.

The U-shaped and missing-upper-side fixtures establish a meaningful rejection: an imagined closed mask can attain full support while the actual original mask still fails. Success on these fixtures is not a general theorem about all stylized borders or anti-aliasing.

### 3.3 Contextual hints

A plain closed rectangle yields `INTERACTIVE_UNKNOWN` as a **role hint**. It does not yet establish interactivity. The checkbox hint requires a normalized box of width and height `8..24` CSS pixels, with width/height difference at most two, plus three separate glyph-like components to its right. Each context component must lie vertically within the candidate, have width at most 16, begin at least three CSS pixels after the right edge, and end no more than 80 CSS pixels beyond it. The chosen run has nonoverlapping x intervals separated by at least two pixels and bottom endpoints within a two-pixel baseline band. This is geometric label context, without OCR or a claim about checked/enabled state. An isolated square gets no checkbox hint.

The close-icon probe tests a glyph against a kept enclosing rectangle. Its normalized dimensions must be `5..20` in both axes and differ by at most two. It must be inset by at least two pixels at left, right, and top; its bottom must be within `min(24,parent_height//2)` of the parent's top, and its right edge within `min(24,parent_width//2)` of the parent's right edge.

Within that glyph's original luma crop, require contrast `max-min >= 64`; dark pixels are `Y <= (min+max)//2`. For each row, compute two expected diagonal x positions using signed nearest rounding. Use a device tolerance of `max(1,ceil(dpr_ppm/PPM))`. Each diagonal must have dark support in at least three-quarters of rows, and dark pixels outside both diagonal bands must be at most one-eighth of all dark pixels. A passing contextual X supplies `kind=K_CLOSE`, role hint `button`, and probe `close_context`. An isolated X and a central multiplication-like X do not satisfy that context.

The proposal list is sorted by CSS top, CSS left, kind, then CSS endpoint tuple. Contextual hints remain hypotheses: decoration can reproduce them, and pixel-identical enabled and disabled controls necessarily produce identical detector outputs. Missing calibration therefore leaves these proposals at `ABSTAIN` with `enabled=null`.

## 4. Cross-DPI geometry

Normalize **endpoints**, not independently scaled width and height. Device minima use floor and maxima use ceiling:

```text
css_min = device_min * 1000000 // dpr_ppm
css_max = -(-device_max * 1000000 // dpr_ppm)
```

The fixture rasterizer applies the corresponding outward rounding from CSS to device endpoints. Both conversions preserve an empty axis: when its original minimum equals its maximum, set the converted maximum to the converted minimum instead of rounding outward into a nonempty span. Python floor division defines the behavior for negative coordinates. For example, device `(1,3,3,8)` at `1250000` becomes CSS `(0,2,3,7)`. A positive device box `(1,1,2,2)` becomes `(0,0,2,2)`; production fusion independently floors width one to zero at this DPR and produces no hit in the counterexample (`wender/perceive/vision_adapter.py:184–189`). The proposed contract does not silently change that production function.

The cross-DPI test paints each source rectangle afresh at each of the three DPRs, uses a device border thickness of `max(1,ceil(2*dpr_ppm/PPM))`, reruns pixel proposals, and requires exactly three boxes, with exactly one match per expected box. Each normalized endpoint may differ from its intended CSS endpoint by **at most one CSS pixel**. This tolerance concerns independently rerasterized geometry, not transformed detector outputs. It does not require byte-identical descriptors or fingerprint equality across DPI, and does not establish invariance for arbitrary antialiased glyphs, rotations, or other DPRs.

An explicit counterexample makes the limitation stronger than a disclaimer: the rectangle `(7,9,46,32)` passes endpoint agreement at all three DPRs, and its DPR-1 constellation matches its own template with five votes. Its DPR-1.25 and DPR-2 constellations have different `fp_struct` values and no matching pair hypothesis against that DPR-1 template. CSS normalization defines common geometry/offset units; the proposed raster descriptor and exact pair matcher do **not** yet provide general cross-DPI template reuse.

Anchor points and pair offsets use a different operation: nearest integer with half ties away from zero. For numerator `n` and positive denominator `d`, take `(2*abs(n)+d)//(2*d)` and restore n's sign. Consequently division by two maps `-3,-2,-1,0,1,2,3` to `-2,-1,-1,0,1,1,2`. This distinction between outward boxes and nearest anchor points is intentional.

## 5. Proposed explicit d48 layout

`d48` is an immutable tuple of **48 byte-valued Python integers**, with nonuniform ranges. It is not 48 floating-point features, 48 bits, or an existing vision-engine descriptor. Extract luma from the original nonempty crop, then sample a `16x16` grid at cell centers. For sample index `j=0..15`, use `x0 + ((2*j+1)*width)//32`, and the analogous y expression. Small crops can repeat pixels. Sampling is integer nearest selection, without interpolated values.

Compute Sobel and thresholded edges on that sampled patch, with the same replicate-border convention. Partition it into sixteen `4x4` cells in row-major order. The complete byte layout is:

| Byte indices | Count | Range | Exact definition |
|---|---:|---:|---|
| `0..15` | 16 | `0..15` | For each cell, `(sum(cell_luma)//16) >> 4`. |
| `16..31` | 16 | `0..15` | Corresponding cell edge density, `15*edge_count//16`. |
| `32..39` | 8 | `0..255` | Direction histogram, `255*bin_count//max(1,total_edges)`, using the directional rules below. |
| `40..43` | 4 | `0..255` | Top/right/bottom/left contour support of the original image edge mask around the original crop box, as in section 3.2. These are not supports of a repaired or resampled mask. |
| `44` | 1 | `0..255` | Sampled-patch mean, `sum(patch)//256`. |
| `45` | 1 | `0..255` | Sampled-patch contrast, `max(patch)-min(patch)`. |
| `46` | 1 | `0..255` | Dark occupancy, `255*count(patch<=127)//256`. |
| `47` | 1 | `0..255` | Saturated crop aspect ratio, `min(255,16*original_width//original_height)`. |

For every sampled-patch edge, first test `2*abs(Gy) <= abs(Gx)`: assign bin 0 for nonnegative Gx or 4 otherwise. Else test `2*abs(Gx) <= abs(Gy)`: assign bin 2 for nonnegative Gy or 6 otherwise. Remaining diagonals use bins 1 for `Gx>=0,Gy>=0`, 7 for `Gx>=0,Gy<0`, 3 for `Gx<0,Gy>=0`, and 5 otherwise. Horizontal precedence resolves boundaries deterministically. Histogram floor rounding means its sum need not equal 255; a no-edge patch has eight zero bins.

The matching quantizer `quantize48` checks exact length and range. It keeps bytes `0..31` unchanged and divides every byte `32..47` by 16 with floor, yielding **48 values in `0..15`**. Each quantized value still occupies one byte in the pair encoding; nibbles are not packed. Values outside their allowed original ranges, booleans, and floats are rejected.

Literal controls prevent helper self-equality from standing in for specification: a black `16x16` square gives 44 zeros followed by `(0,0,255,16)`; a white square gives sixteen 15s, twenty-eight zeros, then `(255,0,0,16)`. Boundaries `0,15,16,31,32,127,128,255` quantize to `0,0,1,1,2,7,8,15` in the last sixteen fields.

## 6. Five-node fp_struct and its bytes

Kinds are fixed as `K_CORNER=1`, `K_CENTER=2`, `K_CHECK=3`, `K_CLOSE=4`, and `K_GLYPH=5`. `Anchor` requires a kind in that range, signed-32-bit CSS coordinates, and a valid immutable d48 tuple. The canonical key is `(y,x,kind,d48)` using the full unquantized tuple as the last ordering field.

For every proposal box, `box_constellation` places four landmarks at its half-open device endpoints `(x0,y0)`, `(x1,y0)`, `(x0,y1)`, `(x1,y1)`, all of kind `K_CORNER`; the fifth is the center `((x0+x1)//2,(y0+y1)//2)` of kind `K_CENTER`. Right and bottom landmarks are geometric endpoints, not the last included pixel centers. This builder uses the same landmark kinds for all proposal role hints. Normalize each point to CSS with nearest signed rounding, and sort canonically. The literal box `(20,20,90,62)` at DPR 1 produces canonical positions `(20,20)`, `(90,20)`, `(55,41)`, `(20,62)`, and `(90,62)`.

Each landmark receives a descriptor from the original image neighborhood with radius `max(1,ceil(4*dpr_ppm/PPM))` device pixels. The half-open patch is `[x-radius,y-radius,x+radius,y+radius]`, clipped to the frame. Its unclipped side is `2*radius`: eight device pixels at DPR 1, ten at DPR 1.25, and sixteen at DPR 2. Descriptor sampling then normalizes that patch to the fixed grid of section 5. The canonical encoder requires exactly five distinct normalized spatial positions; normalization collapse or wrong cardinality raises.

Let origin be `(minimum_node_x,minimum_node_y)`. The exact **291-byte** constellation payload is:

| Field | Encoding |
|---|---|
| Domain/version | Five literal bytes `b"WAF5\x01"`. |
| Cardinality | One byte `0x05`. |
| Five ordered records | Each is 57 bytes: kind as unsigned byte, relative x and y as little-endian signed 32-bit integers (`struct.pack("<Bii",...)`), then the 48 unquantized descriptor bytes. Relative values must be `0..2**31-1`. |

`hash64(payload)` is SHA-256's first eight digest bytes, displayed as sixteen lowercase hexadecimal characters in their original byte order. There is no integer-endianness conversion of the digest. `fp_struct` applies it to the constellation payload. Literal hash controls are `hash64(b"")="e3b0c44298fc1c14"` and `hash64(b"abc")="ba7816bf8f01cfea"`.

Canonicalization is invariant to node-input permutations and to translation of already-extracted anchors with unchanged descriptors. The test checks all 120 five-node permutations, literal node coordinates, payload length and bytes, translation, and invalid cardinalities. These facts do not prove invariance after rerasterizing an arbitrary scene. A 64-bit fingerprint is also not semantic or cryptographic identity proof.

## 7. Pair indexing, offsets, and independent votes

`extract_anchors` is the reference's separate glyph fixture path: one `K_GLYPH` anchor per accepted original-edge ink component, at its nearest-rounded CSS center, with a descriptor from that component's original pixel box. This lets the tests compare a real 8-by-8 pixel glyph grid with five distinct pixel patterns.

Pairs are directed. Deduplicate identical anchors, sort canonically, and select at most **eight partners per anchor** by ascending squared CSS distance, then the partner's canonical key. A five-node constellation can have only four partners per anchor and twenty directed pairs in total. General fixtures may have more anchors, while retaining this degree bound.

The exact **111-byte** pair payload is the five-byte domain `b"WAP2\x01"`, then `kind_i` and `kind_j` as one byte each, then 48 one-byte quantized descriptor values for i and 48 for j, then signed little-endian 32-bit `dx_q,dy_q`. Compute relative offsets from normalized CSS anchor coordinates and nearest-round each to a **two-CSS-pixel step**. Pair reversal changes ordering and signed offsets. The literal vector from kind 1 at `(4,10)` to kind 2 at `(-2,20)`, with zero descriptors, ends in `fdffffff05000000`, representing `(-3,5)`.

Index each template pair by `hash64(payload)`. At lookup, require byte-for-byte payload equality after the hash hit, so a forced collision cannot create a match. A matching scene anchor supplies translation offsets to its matching template anchor, again rounded into two-pixel cells. Hypotheses are keyed by `(template_id,offset_dx_q,offset_dy_q)`.

The independent evidence group is **`(template_anchor.kind, quantize48(template_anchor.d48))`**, not an anchor index, scene location, match count, or partner identity. At one hypothesis, use a set of these groups and define `votes=min(8,number_of_distinct_groups)`. Identical or quantization-aliased copies share a group even when they occupy different template locations. Repeated scene occurrences cannot manufacture additional independent template support. Distinct templates remain separate hypotheses; their scores are not summed.

The ordinary five-node path has at most **five independent groups and five votes**, and frequently fewer when corner patches alias. Eight is the general matcher cap, not a promised five-node score. The 64 repeated-glyph fixture extracts 64 anchors, gives each eight partners, and still yields only one supporting group per hypothesis. Five distinct glyph anchors translated by `(8,12)` CSS pixels yield five groups, five votes, and offset cell `[4,6]`. Ten synthetic distinct groups demonstrate the general cap of eight.

Sort hypotheses by descending votes, template id lexicographically, offset y, then offset x. Return this first hypothesis as `best`, with `tie=1` whenever the top two vote counts are equal. Deterministic presentation does not resolve evidential ambiguity: tied matches cause abstention. Exact duplicate inputs and input/template permutations do not change results. Pair quantization deliberately aliases some nearby offsets; tests demonstrate such an alias rather than claiming lossless geometry.

## 8. Calibration and selective policy

Actionability calibration and role calibration are separate count tables over integer vote scores `0..8`. An actionable-correct label means the evaluated claim targets an actionable control; role-correct additionally requires the claimed role. These labels are offline oracle evidence, never extra inputs to `propose`. Hidden opposite enabled-state labels for identical pixels penalize actionability calibration: twenty successes and twenty failures give 500 per mille, not permission to invent enabled-state evidence.

For counts TP and FP, the exact smoothed score is:

```text
pm = floor(1000 * (TP + 1) / (TP + FP + 2))
```

All arithmetic is integer. `smoothed(0,0)=500` is a mathematical prior value; an empty calibration bin is **unavailable**, omitted from usable blocks, and cannot authorize selection. Aggregate raw counts for duplicate score bins before fitting.

`pool_calibration` sorts nonempty score bins and merges adjacent blocks whenever the lower-score block has a greater **rational smoothed rate**. Compare `(TP_a+1)*(N_b+2)` and `(TP_b+1)*(N_a+2)`, not rounded per-mille values. Merge raw TP/FP counts, retain inclusive low/high score bounds, and apply the `+1,+2` prior once to the resulting block. Never average already-rounded percentages or add one prior per original bin.

Revisit the left neighbor after a merge and continue checking the right: removing duplicated priors can raise a pooled rate above both original values, causing a new inversion. For `(score,TP,FP)` bins `(1,2,0),(2,1,0)`, pooling yields TP 3, FP 0, and 800. Adding `(3,10,2)` forces a further merge to TP 13, FP 2, and 823. The counterexample `(1,8,0),(2,1,1),(3,0,8)` cascades to TP 9, FP 9, and 500. These are fixed algorithm examples, not empirical performance measurements.

Each pooled block retains a sorted `scores` list of its nonempty observed score bins as well as low/high bounds. `calibration_at` returns a copied block only if the requested score belongs to that explicit list; otherwise it returns `None`. Unobserved and empty score bins remain unavailable even inside a pooled interval. The gap/empty-bin counterexample checks that unobserved score 4 cannot borrow the pooled support of observed scores 3 and 5. No interpolation or extrapolation is supplied. `calibration_examples` admits only records whose split is `calibration`, deduplicates by `(family,unit)`, and rejects contradictory score/correctness labels for a repeated unit. Thus 64 duplicates of one unit still contribute one sample. This helper does not independently prove that the caller assigned honest family/unit identifiers or kept families disjoint across train, calibration, and test; that remains a dataset-construction requirement.

The **proposed policy thresholds** are `MIN_SAMPLES=20`, minimum three independent votes, `ACTION_PM=950`, and `ROLE_PM=900`. The supported role hints are exactly `button`, `checkbox`, and `INTERACTIVE_UNKNOWN`; other supplied hints raise. With a tie, fewer than three votes, missing actionability calibration, fewer than twenty pooled actionability examples, or actionability score below 950, return `ABSTAIN`. If actionability passes but the role hint is unknown, role calibration is missing/sparse, or role score is below 900, return `INTERACTIVE_UNKNOWN`. Otherwise return the role hint. These are starting policy choices, not validated universal operating points; the smoothed estimate is not a confidence bound.

All decisions retain `provenance="vision"`, `level=null`, `corroborated=0`, `enabled=null`, and `irreversible_admitted=0`. `INTERACTIVE_UNKNOWN` means sufficient calibrated actionability evidence with uncertain role; `ABSTAIN` means insufficient evidence for selection. Geometry proposals remain visible for evaluation under either decision.

## 9. Integer matching and honest metrics

For half-open boxes a and b, compute width intersection as `max(0,min(a.x1,b.x1)-max(a.x0,b.x0))`, with the analogous height; their product is `inter`. `union=area(a)+area(b)-inter`. Accept IoU at least one-half exactly when `union>0 and 2*inter>=union`. Touching edges have zero intersection. Coincident zero-area boxes do not pass. The large-coordinate fixture evaluates area `4000000000000000000` exactly.

`one_to_one` requires unique nonempty string ids separately within predictions and truth, validates boxes, and constructs all accepted geometric edges. Sort by descending rational IoU using cross products; break equal-IoU ties lexicographically by `(prediction_id,truth_id)`. Greedily accept an edge only when neither endpoint was already used; sort the returned matches by ids. Roles and actionability never influence assignment.

This is deterministic **greedy assignment**, not maximum-cardinality matching. A documented counterexample returns one greedy match although a different assignment could return two. Stable ids and input permutations cannot change that choice. Duplicated boxes with distinct ids cannot both consume the same truth control; duplicated ids are rejected. When disabled and actionable truths overlap, geometry and ids still determine which is consumed. Therefore metrics depend on this explicit matching policy and on correct oracle boxes.

`evaluate` receives hidden truth only after detection. Let P be all proposals, S proposals whose decision is not `ABSTAIN`, K selected proposals with a known role, and T+ all actionable truth objects. Perform assignment separately for P and S against all truth, including disabled/nonactionable objects. Selected TP counts selected matches to actionable truth regardless of role; all remaining selected objects are FP. Role TP counts selected known-role matches that are both actionable and role-correct; remaining known-role selections are role FP.

| Metric | Numerator / denominator |
|---|---|
| `proposal_recall_pm` | Actionable truths matched by all proposals / count(T+). |
| `class_agnostic_recall_pm` | Selected actionability TP / count(T+), ignoring role agreement. |
| `action_precision_pm` | Selected actionability TP / count(S). |
| `role_conditioned_precision_pm` | Role TP / count(K). Unknown roles do not enter this denominator. |
| `selection_coverage_pm` | count(S) / count(P). |
| `role_coverage_pm` | count(K) / count(S). |
| `selective_risk_pm` | Selected actionability FP / count(S). |

Every available ratio is `floor(1000*numerator/denominator)` and every zero denominator produces `null`, meaning unavailable. No-selected-output precision is not perfect precision. With actionable truth but no selections, selected recall is zero. A selected object with empty truth is an FP with risk 1000, while recall is unavailable. A correct geometry with an incorrect known role can count toward class-agnostic recall while failing role-conditioned precision. Disabled truth can match geometrically but still causes an actionability FP.

Keeping proposal recall separate from selected recall reveals the effect of abstention. The fixture with one correct proposal that abstains has proposal recall 1000 and selected recall zero. Report counts, both coverages, and both recall notions with precision/risk so an aggressive abstainer cannot hide lost coverage. Floors are computed independently; rounded precision plus rounded risk need not sum to 1000.

An eventual held-out evaluation should freeze templates and policy before evaluating families excluded from calibration, retain disabled and decorative controls as negative evidence, and identify repeated controls as dependent units. These dataset and operating-point validations are future work; the current RAM fixtures establish algorithm behavior and counterexamples, not production accuracy across websites, desktops, canvas applications, and games.

## 10. Safety boundary and isolated live gaps

Uncorroborated vision targets must remain below both `L0_EXACT` and `L1_STABLE`. Neither a recognizable glyph, exact pair payload, high vote count, role string, nor a calibration score upgrades evidence into structured identity. The reference `irreversible_allowed` predicate requires trusted provenance in `cdp/atspi/godot`, `corroborated=1`, an L0/L1 level, the corresponding nonempty sixteen-lowercase-hex exact/stable fingerprint, `fresh=1`, `tie=0`, `enabled=1`, and `authorized=1`. Every flag must have exact Python type `int`, so boolean, float, and string substitutes are refused. A vision-origin record is rejected even if it supplies valid-looking L0/L1 fields.

This predicate is an interface boundary for a trusted host adapter. A freely editable JSON provenance field is not authentication; only the trusted structured consumer may construct corroborated records. The reference does not implement that host integration. The ordinary safety test includes a positive structured control and thirteen adversarial records, covering vision origin, missing fingerprints, stale/disabled/uncorroborated evidence, ties, absent authorization, and every selective decision class. It additionally tests invalid flag types. Its acceptance condition is **zero false irreversible admissions in these reference cases**, without claiming zero production risk.

An integrated positive fixture exercises pixels, proposals, descriptors, template matching, and selection together. The labeled-checkbox image supplies a template and fabricated calibration counts of 1000 TP/0 FP at each score from 3 through 8. These counts are test inputs, not observed accuracy. The resulting checkbox suggestion has five votes and no tie. A separately constructed pixel-identical disabled image produces identical output, still with `enabled=null`; every output is refused for irreversible use and remains below L0/L1. This positive suggestion path and the trusted structured admission control show that safety is not obtained by making the entire reference always abstain or always deny.

The live AX implementation has a specific unresolved gap: a missing or null `fp_exact` allows a same-role/name/ordinal candidate to return `L0_EXACT` with `established=True` (`wender/perceive/ax.py:265–275`), and the level helper tests only the L0/L1 string (`wender/perceive/ax.py:298–300`). The current executor checks levels and ties, including fresh nodes, but overlays caller `action.describe` and does not independently reject missing proof, `established`, or vision provenance (`wender/act/cdp_executor.py:229–250`). A valid AX-shaped fixture with a backend id and null fingerprints can therefore reach the fake irreversible dispatch path. A raw vision record missing AX fields could fail earlier in `ax.describe`; that would be a setup error rather than proof of this safety finding.

There are two explicitly separated live-gap groups:

- **A5-D1:** `TestLiveBridgeGaps.setUp` constructs a valid frame and calls both live bridge functions outside any expected-failure method. It recognizes only the exact documented `NotImplementedError` message; other errors propagate normally. Decorated test bodies merely assert that each operation returned. These two expected failures record missing live integration without excusing import, frame, or setup errors.
- **A5-D2:** `TestLiveSafetyGap.setUp` builds valid pruned AX nodes, null-fingerprint vision description, fake CDP transport, and fresh read callback, then calls the real matcher/executor. It tolerates the executor's deliberate `ActionError` refusal as the future desired outcome. The decorated body checks that `Runtime.callFunctionOn` was not recorded. Thus the expected failure concerns observable fake admission, not malformed setup. The ordinary structured control independently proves the fixture and fake transport work.

No live probe connects to a browser or executes a real user action. Removing a stub or fixing the safety defect should produce an unexpected success until the corresponding test is deliberately converted to an ordinary passing regression. The ordinary proposed-reference tests are not expected failures and must pass on their own. The AX matcher's float diagnostic score (`wender/perceive/ax.py:230–236`) is not copied into reference JSON or treated as calibration.

## 11. Claim, test, and future-consumer map

Every row below names all exact methods in the sibling module's `CLAIM_TESTS` entry. Consumer references describe future integration responsibilities; none asserts that those integrations have been implemented. `affordance_adversarial` is the future evaluation consumer named by this task, not a module imported by this harness.

| Claim | Reference obligation and exact tests | Future consumer responsibilities |
|---|---|---|
| A5-R01 | Integer luma and signed Sobel vectors: `TestPixels.test_luma_literal_vectors_and_sobel_signed_step`. | `vision_adapter`: preserve RGB/luma arithmetic; `vision_bridge`: preserve channel/layout contract; `affordance_adversarial`: retain literal signed-step controls. |
| A5-R02 | Input validation and original-edge contour rejection: `TestPixels.test_input_validation_and_original_contour_counterexamples`. | `vision_adapter`: propose from original masks; `vision_bridge`: reject invalid frames before transport; `affordance_adversarial`: challenge U-shapes and invented contour closure. |
| A5-R03 | Contextual checkbox and close-icon hints: `TestPixels.test_checkbox_requires_pixel_label_context`; `TestPixels.test_close_requires_enclosing_upper_right_context`. | `vision_adapter`: produce conservative contextual hints; `vision_bridge`: carry probe provenance; `affordance_adversarial`: distinguish isolated squares/X marks from contextual fixtures. |
| A5-R04 | Pixel-identical enabled/disabled ambiguity: `TestPixels.test_pixel_identical_enabled_disabled_controls`. | `vision_adapter`: expose unknown enabled state; `vision_bridge`: keep hidden oracle labels out of inference; `affordance_adversarial`: score identical-pixel positive/negative pairs honestly. |
| A5-R05 | Outward endpoints, rerasterized three-DPR tolerance, and explicit descriptor/matching noninvariance: `TestDpiDescriptor.test_endpoint_normalization_and_width_counterexample`; `TestDpiDescriptor.test_rerasterized_boxes_three_dpis_one_css_edge_tolerance`; `TestDpiDescriptor.test_cross_dpi_box_agreement_is_not_hash_or_match_invariance`. | `vision_adapter`: normalize endpoints without promising template reuse; `vision_bridge`: retain explicit dpr_ppm; `affordance_adversarial`: compare each CSS edge within one pixel and retain the cross-DPI hash/pair counterexample. |
| A5-R06 | Explicit d48 ranges, literal layout, and quantizers: `TestDpiDescriptor.test_d48_literal_layout_ranges_and_quantizer_boundaries`. | `vision_adapter`: validate and extract descriptor fields; `vision_bridge`: transport fixed-layout bytes; `affordance_adversarial`: exercise literal patches and quantization boundaries. |
| A5-R07 | Five canonical landmarks and explicit bytes/hash64: `TestDpiDescriptor.test_five_landmarks_from_pixels_and_all_permutations`; `TestHashVoting.test_hash64_and_pair_literal_byte_vectors`. | `vision_adapter`: build canonical fp_struct; `vision_bridge`: preserve domain/version and byte order; `affordance_adversarial`: check all permutations and independent literal vectors. |
| A5-R08 | Directed pair inputs, signed offsets, and collision verification: `TestHashVoting.test_pair_changes_every_input_negative_rounding_and_reversal`; `TestHashVoting.test_hash_collision_is_not_a_payload_match`. | `vision_adapter`: form and verify complete pair payloads; `vision_bridge`: avoid treating hash64 as identity evidence; `affordance_adversarial`: vary every pair input and force collisions. |
| A5-R09 | Distinct-group votes, eight-partner cap, repetition, and ties: `TestHashVoting.test_eight_by_eight_pixel_grid_vs_distinct_anchors`; `TestHashVoting.test_vote_permutations_duplicates_ties_and_eight_cap`. | `vision_adapter`: de-alias template groups and bound partners; `vision_bridge`: carry ambiguity and counts; `affordance_adversarial`: compare 64 repeated glyphs with five distinct anchors and cap counterexamples. |
| A5-R10 | Smoothed count pooling, cascades, independent units, and split checks: `TestCalibrationMetrics.test_smoothed_literal_scores_count_pooling_and_cascade`; `TestCalibrationMetrics.test_calibration_units_and_split_leakage`. | `vision_adapter`: consume frozen pooled tables; `vision_bridge`: preserve integer calibration evidence; `affordance_adversarial`: detect duplicate-unit inflation and smoothing-induced inversions. |
| A5-R11 | Proposed selection/role thresholds and abstention: `TestCalibrationMetrics.test_abstain_unknown_role_policy_boundaries`. | `vision_adapter`: separate ABSTAIN from INTERACTIVE_UNKNOWN; `vision_bridge`: expose both decisions without promotion; `affordance_adversarial`: test ties, sparse bins, low votes, and exact threshold boundaries. |
| A5-R12 | Half-open IoU and deterministic greedy assignment: `TestCalibrationMetrics.test_half_open_iou_exact_boundary_and_large_integers`; `TestCalibrationMetrics.test_one_to_one_permutations_duplicates_and_greedy_counterexample`. | `vision_adapter`: emit valid endpoint geometry; `vision_bridge`: retain stable proposal ids; `affordance_adversarial`: reproduce rational matching, duplicate rejection, and nonmaximum-cardinality counterexample. |
| A5-R13 | Distinct recall/precision denominators and unavailable ratios: `TestCalibrationMetrics.test_recall_precision_unknown_abstain_and_disabled_truth`; `TestCalibrationMetrics.test_zero_denominators_are_unavailable`. | `vision_adapter`: retain abstained proposals for recall; `vision_bridge`: serialize null unavailable metrics; `affordance_adversarial`: report proposal/selected recall, role precision, risk, and coverage with counts. |
| A5-R14 | Zero false irreversible reference admissions with structured and integrated pixel-path positive controls: `TestReferenceSafety.test_zero_false_irreversible_admissions_with_positive_control`. | `vision_adapter`: retain uncorroborated provenance even after five-vote checkbox selection; `vision_bridge`: admit only trusted structured corroboration at action boundary; `affordance_adversarial`: mutate admission prerequisites and compare pixel-identical enabled/disabled selections. |
| A5-R15 | Float-free full outputs, absent vision STATE kind, and valid live fixtures: `TestReferenceSafety.test_serialization_full_outputs_no_floats_and_no_vision_state_kind`; `TestReferenceSafety.test_valid_live_probe_setup_and_structured_control`. | `vision_adapter`: use schema.dumps without claiming STATE compatibility; `vision_bridge`: keep fixture/setup failures visible; `affordance_adversarial`: reject nested floats and validate structured positive controls. |
| A5-D1 | Isolated documented bridge gaps: `TestLiveBridgeGaps.test_a5_d1_live_screen_identity_contract`; `TestLiveBridgeGaps.test_a5_d1_live_changed_regions_contract`. | `vision_adapter`: keep reference behavior independent of live stubs; `vision_bridge`: implement the missing live calls before removing xfails; `affordance_adversarial`: distinguish the documented gap from ordinary setup errors. |
| A5-D2 | Missing-fingerprint live irreversible regression: `TestLiveSafetyGap.test_a5_d2_missing_fingerprint_irreversible_is_refused`. | `vision_adapter`: never fabricate L0/L1 proof; `vision_bridge`: require host corroboration before irreversible handoff; `affordance_adversarial`: retain valid fake dispatch evidence until production refusal is implemented. |
| A5-R16 | UTF-8 sibling-source AST and exact claim mapping: `TestDocumentation.test_claim_mapping_from_utf8_sibling_module`. | `vision_adapter`: maintain traceable reference changes; `vision_bridge`: keep future obligations linked to tests; `affordance_adversarial`: check every test and absence label without generating artifacts. |

## 12. Validation and limits

Run from the repository root:

```sh
python3 -m unittest tests.test_jq1_a5_affordance_constellation_spec
```

The documentation test reads the sibling test module and this file explicitly as UTF-8, parses the Python AST, and requires the set of actual test methods to equal the union of `CLAIM_TESTS`. It then checks one table row per claim, every mapped test name, every future consumer, and the five required AUSENTE labels. This verifies traceability without generating a fixture inventory or writing frames.

The result/report deliverables record observed VM test output; this document does not claim a final run that was not observed. Any elapsed runtime is VM timing, never MEASURED performance. Expected failures identify the two legacy bridge probes and the one live safety regression; they do not certify acceptance, excuse ordinary failures, or claim the real engine has been validated.

This reference intentionally covers a conservative subset of affordance appearance. Borderless links, subtle hover-only states, general text recognition, scrollbars, radio controls, theme diversity, overlapping widgets, arbitrary antialiasing, robust descriptor invariance across DPI, and production calibration data remain outside its established guarantees. Future implementations should retain the counterexamples, freeze any changed encoding version or thresholds, and measure precision, recall, role uncertainty, abstention coverage, and irreversible admission separately before expanding supported appearance classes.
