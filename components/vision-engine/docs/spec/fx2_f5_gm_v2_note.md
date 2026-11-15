# GM v2.1 implementation and collection note

## Status and scope

This change implements `eval/v2x/gm_bands_hv.py` and adds
`eval/v2x/gm_constellation.py` from the integer contract in
`docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md`, especially
sections 4 through 7, with the two narrowly scoped production compatibility
rules required by the host follow-up below. The complete prescribed
17-module gate is **green: 75 tests passed in VM 66.178 seconds, with zero
failures, errors, skips, expected failures, or unexpected successes**. All
66 original tests pass, and nine additional regression methods protect the
corrections. This is observed VM and collection evidence, not host acceptance.

The follow-up started from the published task-branch head
`c9ee25bbb9354f5c0ec954b29b261d0e0cb5263b`. Both reported failures were
reproduced before editing. The original alias assertion and the protected
header/footer assertion are unchanged; the estimator now returns their
required `(0,-24,0)` and `(0,30,0)` results respectively. Delivery continues
on the same branch and existing PR #228, with new per-file commits.

The source was read through the GitHub connector at explicit ref
`bridge/integration`, resolved in this session to
`f4cf3965493a052afbb7b704f94e531c98b84d30`. Original decoded file bytes were
checked against their GitHub blob SHAs before editing. Writes belong to
`bridge/fx2-f5-gm-bands-hv-v21-and-constellation/a1` and the task's exclusive
paths. Neither `gm_v2_oracle.py`, its frame generators, the Rust crate, nor
other consumer test files are changed.

The earlier specification describes its algorithms as proposed test-local
references. The current task explicitly adopts those rules for production.
Likewise, the historical prototype test's module docstring says
`gm_constellation.py` is absent and must not be created; the current task
expressly requires creating it. That historical restriction is not applied,
and the prototype file is unchanged. The task also requires preserving the
production identical-frame special case, although the test-local reference
abstains on identical frames.

## Integer evidence and public behavior

Both modules retain `estimate_global_motion(prev_luma, cur_luma, max_d)` and
`consensus_tiles(prev_luma, cur_luma, s, dx, dy)`. Inputs must be equal-shaped,
nonempty, two-dimensional uint8 arrays. Radii and displacements are integers;
Boolean radii, floats, and negative radii are rejected. Consensus requires a
positive integer tile side. Inputs are not modified. Both spatially flat
frames return `None`, even for different constants; identical textured frames
return `(0, 0, 0)`.

A correspondence is `C[y,x] == P[y-dy,x-dx]`. Both endpoints must be inside
the common band and their allowed masks. Exposed pixels are unavailable, with
no wraparound or invented correspondence. The common band encloses every
unmasked temporal change and every unmasked informative pixel in either
frame, retaining stationary textured counterevidence.

Information means inequality with an allowed left, right, upper, or lower
neighbor. The predicate is recomputed inside each candidate's eligible overlap
for both aligned frames. A gradient across a mask or overlap boundary cannot
add evidence. The active set uses current **OR** translated-previous
information. Flat-flat comparisons have zero weight; text-flat mismatches
remain active.

For active pixels, `S` sums absolute signed luma differences and `N_dyn`
counts pixels. Missing information (`N_dyn == 0`) refuses the candidate.
Candidates are compared by exact cross-products of `S/N_dyn`, using Python
integers; reductions use signed integer arrays and int64 sums. The decision
score is `(S + N_dyn - 1) // N_dyn`, so positive decision residual has a
positive ceiling. This is also the public score except for the independently
byte-exact fixed-chrome viewport rule below. That rule retains the full-band
decision residual in diagnostics and reports the exact viewport's zero score.
There is no floating arithmetic, FFT, subpixel interpolation, or estimation RNG.

Non-adjacent aliases have Chebyshev displacement distance at least four.
An exact rational tie refuses the best candidate. For a positive-residual
best `b`, each such runner `r` must satisfy
`S(r)*N_dyn(b) - S(b)*N_dyn(r) >= N_dyn(b)*N_dyn(r)`.
A unique zero residual bypasses this positive one-luma margin; it still
needs support, including the separately documented sparse exact certificate
when ordinary changed support is insufficient, and competing-motion checks. Coordinate ordering is
deterministic and does not turn an exact alias tie into uniqueness.

## Compact edge chrome

The mask is displacement-independent. A changed row's span is its complete
first-to-last changed-column interval, including gaps. It is never split to
fit the width cap. Edge depth is `min(64, H//4)`. There must be an interior
changed row outside both edge regions whose span is wider than `W//8`.

An eligible top row requires `y < edge_depth` and
`y + 16 < first_broad_interior_row`; a bottom row requires
`y >= H-edge_depth` and `y - 16 > last_broad_interior_row`. Its full span must
be no wider than `W//8`. The nonempty remainder must be byte-identical between
frames and flat. Only changed pixels are masked; stationary texture and
unchanged islands remain. Both correspondence endpoints are excluded by the
mask during verification.

Consequently, the adversarial generator's width-40 clock stays unmasked at
frame widths 256 and 300, where caps are 32 and 37. Its disagreement remains
counterevidence. At width 320 the cap is 40 and qualifying changed clock
pixels can be masked. Recovery of the narrower-frame clocks depends on
distributed scrolling support, not silently dropping their residual.

## Projection proposals and candidate budget

Every legal integer shift is ranked using
`(raw_projection_SAD*L//overlap_length, abs(shift), shift)`. The main band
retains eight raw vertical winners and zero. Eight-element box smoothing uses
only `mode="valid"`; the best eight coarse multiples of eight each add one
raw minimum within distance less than eight. Smoothing adds proposals and
cannot displace raw winners, including `-4`.

Each retained vertical offset receives compensated column projections and
four raw horizontal winners, the same coarse-basin additions, and zero.
Both axes use `max(16, min(64, band_length//4))` as minimum overlap. A
1080-row band therefore permits the 120-row overlap of `dy=-960`. Smoothed
minimum overlap is `max(1, raw_minimum-7)`.

Contiguous runs of changed rows propose additional vectors from their smallest
changed-column intervals. Rank regions by decreasing changed-pixel count,
then coordinates; retain at most eight. Each keeps three raw offsets per
axis plus coarse basins and zero. Regions smaller than 16 on either axis
produce no pair. Local regions do not replace the common verification band.

| Proposal source | Conservative bound before deduplication |
|---|---:|
| Main band: at most 17 vertical and 13 horizontal choices | `17*13 = 221` |
| Eight local regions: at most 12 choices per axis | `8*12*12 = 1152` |
| Explicit stationary competitor | `1` |
| Total upper bound, intentionally counting possible duplicates | `1374` |

Actual sets are deduplicated and checked against `max_d`. Every retained
eligible proposal receives exact verification. The bounded proposal set does
not establish uniqueness over all conceivable image displacements.

## Changed support, competing modes, and consensus

Support uses the original global 16-by-16 grid anchored at `(0,0)`, including
partial edge tiles. A tile's weight is `min(128, c_t)`, where `c_t` counts
eligible pixels that are both informative and changed in place. Let
`N_changed` sum these weights. A tile contributes to primary support `P`
only when its weight is positive and **every eligible real pixel** in its
scoring fragment matches the translated source.

The ordinary acceptance route requires a nonzero best vector and all of: `N_changed > 0`,
`P >= 512`, at least four supporting tiles, at least two supporting tile rows
and columns, and `P*1000 >= 600*N_changed`. Stationary zero is a residual
competitor; if it wins, the estimator abstains. The sparse exact route below
extends only the absolute support qualification and leaves these spatial,
primary-share, and runner tests intact.

Every other distinct nonzero vector is a runner, including offsets only one,
two, or three pixels from the best. Recount runner support `R` on the
primary's changed informative pixels inside both candidates' legal overlaps,
reapplying the global per-tile cap and exact common-fragment match. Refuse if
`R*1000 >= 250*N_changed` **or** `P < 3*R`. Exactly 250 ppt vetoes; exact
three-to-one equality passes only the ratio condition.

Public consensus is stronger than scoring-fragment support. A tile must
change in place, and every real destination pixel must have an in-frame,
exactly equal translated source. Padding beyond the physical frame is
ignored, while exposed real pixels invalidate the tile. Unchanged tiles are
never relabeled, including partial physical-edge tiles. Both plugins share
this implementation.

## Host follow-up: exact sparse translations and fixed-chrome scores

The host required both original assertions to pass after verifying head
`c9ee25bbb935`. The literal reference contract cannot produce both requested
tuples: its absolute changed-support floor refuses the sparse glyphs, and its
full-band residual ceiling is 17 for the textured header/footer scene. These
are explicit production compatibility refinements to that reference, not
claims that the old reference already specified the new outcomes. The source
specification, independent reference implementation, fixtures, and original
assertions remain unchanged.

### Exact sparse outline certificate

At the bound source commit, [spec section 6.1, line 380](https://github.com/oguirado/vision-engine/blob/f4cf3965493a052afbb7b704f94e531c98b84d30/docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md#L380)
requires `P(best) >= 512`; the constant table repeats this floor at
[line 577](https://github.com/oguirado/vision-engine/blob/f4cf3965493a052afbb7b704f94e531c98b84d30/docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md#L577).
D-A1-ALIAS has only three times 128 changed glyph pixels after its four
eligible edge anchors are masked. Its true vector `(0,-24)` has `S=0`,
`N_dyn=1158`, and `P=N_changed=384`, distributed over 16 exact tiles, two
tile rows, and eight tile columns. The tile cap loses no changed support.
An unchanged literal 512 changed-weight floor therefore cannot fix this pin.

The correction keeps `MIN_SUPPORT_WEIGHT=512` and the ordinary route intact.
A candidate below that changed-weight floor must instead meet every one of
these stronger exactness conditions:

1. `S=0` and `P=N_changed>0`: every changed-support tile is explained exactly.
2. Every eligible raw byte in the translated overlap matches, including
   flat-flat comparisons omitted from SAD.
3. No other retained candidate has zero residual, even one, two, or three
   pixels away. The usual non-adjacent rational-alias check also remains.
4. At least 512 capped **unchanged informative boundary correspondences**
   are attached to the changed information, as defined below.
5. The original minimum overlap, four-tile, two-row, two-column, 600-ppt,
   and every-runner veto requirements still pass.

Let `C` be the primary's existing changed informative pixels and `I` its
informative OR predicate, both restricted to the allowed overlap. Form
`B_pixels = I & ~C & adj4(C)`, where `adj4` uses only immediate horizontal
and vertical neighbors, with no wrapping or crossing the overlap boundary.
The predicate `I` excludes invalid endpoints. Because all eligible bytes
were independently verified exact, these are exact correspondences at the
unchanged outlines of the moving glyphs.

Reduce `B_pixels` on the same global 16-by-16 grid, cap each tile at 128,
and sum **only over the already exact tiles with positive changed support**.
Call the resulting separate weight `B`; require `B>=512`. The alias fixture
has `B=768`. Neither flat interiors, distant stationary texture, nor wholly
unchanged tiles can supply this certificate. Each changed pixel can attach
at most four boundary positions before deduplication and caps.

Boundary evidence is never added to `P` or `N_changed`, and never improves
the primary's ratio against a runner. Counting only unchanged boundary
information is deliberately more conservative than counting changed pixels
plus their neighbors: the latter could promote some 22-by-22 dense patches
with `P=484` to 512. Their unchanged immediate outline is too small for this
certificate. Both 16-by-16 and 22-by-22 isolated patches remain refused over
all 16 independently tested grid phases.

The rule uses no fixture name, dimensions, seed, glyph width, or displacement
special case. Reverse and transposed inputs, and different glyph seeds,
origins, and widths, pass. One corrupted glyph byte and an added stationary
32-by-32 textured island both cause abstention. A sparse periodic scene with
exact vectors `(0,-2)` and `(0,1)` also refuses, even though either candidate
alone has enough boundary evidence. Both production plugins use this rule
through their shared verifier.

### Independently exact fixed-chrome viewport score

The bound spec's [section 4.2, line 244](https://github.com/oguirado/vision-engine/blob/f4cf3965493a052afbb7b704f94e531c98b84d30/docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md#L244)
requires stationary texture as counterevidence in the common band. This is
preserved for all candidate ranking, alias checks, support qualification,
and runner vetoes. The original RNG-4000 header/footer fixture consequently
still has decision `S=4891176`, `N_dyn=288000`, and ceiling score 17.

[Section 4.3, line 264](https://github.com/oguirado/vision-engine/blob/f4cf3965493a052afbb7b704f94e531c98b84d30/docs/spec/jq1_a1_gm_v2_dynamic_band_and_constellation_spec.md#L264)
defines `score(v) = (S(v) + N_dyn(v) - 1) // N_dyn(v)`; line 267 gives the
positive-residual/positive-score interpretation. The host-requested legacy
zero is a narrow exception to that **public score domain**, not a claim
that the full-band SAD is zero. It applies only after the nonzero motion has
already passed every decision gate:

1. Motion is purely vertical or purely horizontal.
2. Raw temporal differences independently identify the first and last changed
   rows, or columns for horizontal motion. There must be a nonempty,
   byte-identical outer strip at **both** ends. Preserve the full perpendicular
   image dimension; the winning displacement does not select the bounds.
3. The translated interior viewport meets both existing minimum-overlap floors.
4. **Every raw byte** in that observable translated viewport is equal. This
   comparison uses neither the compact mask nor the informative predicate.

Only this certificate changes the public score to zero. It does not crop
decision evidence, rerank candidates, relax support, change consensus, or
emit a previously refused vector. In the original fixture the fixed strips
are rows `[0,50)` and `[440,480)`. At `dy=30`, viewport destinations
`[80,440)` match sources `[50,410)` over all `360*640=230400` bytes. Returning
`(0,30,0)` therefore preserves the pre-existing exact scrolling-body meaning.

`OPS_DETAIL` retains `decision_raw_sad`, `decision_informative_pixels`, and
`decision_score`; `exact_viewport_score_zero=1` marks the compatibility case.
The original decision score remains 17 in the diagnostic record. The
constellation wrapper exposes these shared values with its existing `bands_`
prefix and restores the caller's bands contexts.

In the tested fixtures, a one-byte corruption inside the observable body
overlap or the changing textured header fails the raw-byte certificate and
keeps score 17. Newly exposed pixels remain unobservable. The width-256/300/320 clock fixtures retain
positive scores 14/11/12 respectively. Signed vertical and horizontal
versions of the exact viewport all return zero. A small local drag over
stationary random texture remains `None` before this scoring helper is reached.

## Constellation proposals

Each feature is the exact 256 row-major uint8 bytes of a 16-by-16 patch,
without perceptual tolerance or hash-collision assumptions. All integer
origins with complete unmasked patches are considered. Signed int64 internal
edge sums measure 240 horizontal plus 240 vertical differences; gradient
must be at least 256.

Fixed 16-by-16 origin cells retain at most two origins, ordered by decreasing
gradient then row and column. The same ordering selects the global best 256
per frame. Previous features are grouped by exact bytes; current and matching
previous features are visited in coordinate order. Each destination tile
casts at most one vote per displacement, within the caller's componentwise
radius.

Modes rank by `(-votes, abs(dx)+abs(dy), dx, dy)`. Keep all modes tied with
or above the nominal rank-32 cutoff. More than 256 retained modes triggers
zero-only saturation refusal; an empty histogram also leaves zero only.
Otherwise add zero to the retained set. At most `256*256` selected feature
pairs can be considered. Votes never bypass the common verifier or public
consensus, and the finite feature budget can cause abstention.

## Honest operation accounting

`OPS` and `OPS_DETAIL` reset per public call and are isolated by `ContextVar`.
The plugin transfers shared work counts while restoring the bands module's
previous counters. `OPS` is deterministic diagnostic work accounting, not
elapsed time or a hardware-instruction count.

`DENSE_SAD_BUDGET=9` bounds calls to the legacy dense `_joint_sad` kernel.
It is used only when all pixels of an eligible rectangle are informative.
Sparse evidence and additional candidates use exact packed one-dimensional
informative-pixel reductions. Their arithmetic is included in `OPS` and
`packed_informative_comparisons`; no candidate is dropped to meet the dense
budget. Other counters expose candidate hypotheses, verifications, dense
calls/pixels, and runner comparisons. A winning candidate is verified again
to construct its runner masks, so verification count can exceed hypotheses.
The follow-up exactness checks also count their full raw comparisons,
informative-neighbor work, adjacency operations, and capped grid reductions.
`sparse_exact_comparisons`, `sparse_exact_boundary_weight`,
`sparse_exact_accepted`, and `viewport_exact_comparisons` expose that work and
its outcome separately from the unchanged primary and runner weights.

The unchanged bounded regression still observes at most nine dense SAD calls
and at most nine frame areas of two-dimensional `np.abs` work. This is **not
a bound on all expanded verifier work**. On its 120-by-160 rolled-frame
fixture, observed diagnostics were 102 hypotheses, nine dense calls over
46,426 pixels, 753,625 packed informative comparisons, 650,437 runner
comparisons, and `OPS=23,976,015`.

`_joint_sad` retains its real strict-cutoff early return. Exact production
rational ranking passes `cutoff=None`; a partial lower bound cannot replace
an exact residual. The normal-versus-forced-full regression therefore proves
decision equality and nonincreasing OPS, without claiming production cutoff
speedup. A separate direct helper check observed cutoff OPS 10,240 versus
full OPS 40,960, with a returned lower bound 1,305,600 versus full SAD
5,222,400.

## Observed VM validation and corrected host failures

All fixtures were CPU-only stdlib/NumPy RAM arrays; no frames were saved.
The full prescribed 17-module unittest command was run unchanged, before and
after implementation and after the host follow-up:

| Run | Tests | Observed VM result |
|---|---:|---|
| Bound-source baseline | 66 | VM 29.220 s; `OK (expected failures=8)` |
| Published head `c9ee25bbb935`, activated pins | 66 | VM 65.705 s; `FAILED (failures=2)` |
| Follow-up reproduction of the two host failures | 2 | VM 0.547 s; `FAILED (failures=2)` |
| Corrected estimators plus first eight new methods | 74 | VM 63.028 s; `OK` |
| Final corrections plus all nine new methods | 75 | VM 66.178 s; `OK` |

The final run has 75 passes, no failures, errors, expected failures, skips,
or unexpected successes. Eight requested methods were activated by removing six decorator
sites: one each in the borderline, alias, micro-scroll, page-jump, and
single-pixel-clock files, plus the generated three-clock site. Their fixture,
assertion, and safety-method ASTs remain identical to the source after
excluding the new methods and the requested decorator removals. All eight
activated methods now pass. The protected header/footer file is byte-identical
to the source. Complete earlier failure output is retained in `report.md`.

Six new alias regression methods exercise signed/horizontal sparse motion,
different glyphs and grid origin, one-byte corruption, stationary texture,
an adjacent exact tie, and localized patches. Three new chrome methods cover
signed/horizontal exact viewports, a body-byte disagreement, and a changing
textured header. No existing assertion was altered or replaced.

All seven oracle card tuples remain valid; two focused card/custom-fixture
methods also passed in VM 3.543 s. No oracle test edit was justified. For
example, its `flat_window_drag` fixture has independent random backgrounds
and only one copied rectangle: independent capped support is 6,144 out of
144,746 at `(16,24)`, below 600 ppt. The independent digest and both mutant
checks remain unchanged and pass in the required run.

Separate production scorer runs were repeated after the corrections, distinct
from test-local reference output, and observed the following for `adv_gm.score(..., seed=0x5EED,
include_1080p=True)`:

| Production plugin | Pairs | Exact | Silent | Wrong vectors | Static relabels |
|---|---:|---:|---:|---:|---:|
| Bands | 21 | 18 | 3 | 0 | 0 |
| Constellation | 21 | 18 | 3 | 0 | 0 |

The three silent cases are bare carets, whose generator still records local
translation truth. They were not relabeled as exact successes. Additional
checks covered reference evidence/runner parity, positive residuals, ragged
masks, integer-only ASTs, feature signatures/gradients, tied-mode saturation,
read-only strided inputs, and threaded OPS restoration. Independent follow-up
checks passed 86 sparse/safety fixture checks plus two sparse adjacent-tie
checks, 22 viewport and raw-byte counterexample checks, and eight threaded
result/OPS/diagnostic comparisons. Shared verification
means the two plugin totals are not independent evidence of live accuracy.

## Remaining host checks

The requested VM gate now has zero failures; host execution and acceptance
remain separate. The two production compatibility rules above must be
considered when comparing final decisions against the unchanged literal
reference. The separate j1/j2/j3/j5 consumer files and their activation
decorators remain their owners' responsibility. Rust conformance
and differential checks also depend on
`fx2-f5-rust-vqa-k-gm-v2-conformance`; no Rust or oracle fixture is changed.

The live 24-episode corpus, its frozen recovery predicate, and evidence of
at least 21 recovered episodes are unavailable here. Host replay must still
establish that target, zero wrong vectors, zero static false emissions, and
physical-edge safety. Native parity, live recall, and host latency/memory
budgets remain unverified. Exact matching and bounded proposals can lose
recall under resampling, antialiasing changes, occlusion, repeated patterns,
or insufficient distributed texture. Synthetic VM results do not resolve
those limits or constitute acceptance.
