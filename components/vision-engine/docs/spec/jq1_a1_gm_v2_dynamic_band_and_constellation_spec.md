# Router C1: dynamic band v2.1 and a test-local constellation voter

## 1. Scope, provenance, and evidence vocabulary

This is a proposed integer-only global-motion contract, a source audit, and the
specification for the runnable references in
`tests/test_jq1_a1_gm_v2_dynamic_band_and_constellation_spec.py`. It changes no
production estimator, Rust implementation, harness, or fixture generator.
The references use only the Python standard library and NumPy, operate on RAM
arrays, and require no GPU, network, display, or saved frame files.

The requested source ref is `bridge/integration` in `oguirado/vision-engine`.
The GitHub connector resolved that ref in this session to
`9ad5c5181fdd1d013eac069631d5db0e1557aedc`; each bound source was then read
with the ref explicitly supplied. The dispatch metadata's source commit,
`cd3c326ae9e8fc571f535fe36bfe5b591709215d`, is a different dispatch
identifier. It is not substituted for the commit actually observed, and this
document makes no claim that the two trees are identical. All existing
`path:line` coordinates below refer to the five bound source snapshots read at
the observed ref. The task nonce is `46f0209bdd8bf3ae`.

The evidence labels have these meanings:

- **CURRENT**: directly established from a bound source or an explicitly
  reported VM functional run of that source snapshot.
- **PROPOSED**: an implementation or policy choice for the test-local v2.1
  references. A passing reference test does not make this production behavior.
- **AUSENTE**: unavailable or not established from the inspected evidence. It
  does not mean that a named file cannot exist elsewhere in the repository.

The production API is `(dx, dy, score)` or `None`, with a nonnegative Python
integer score and an integer displacement. Positive displacements move right
and down. The inputs are read-only uint8 luma arrays. These are **CURRENT**
contracts in `eval/v2x/README.md:5-11`. The claim-only harness promotes consensus
tiles to MOVED only for a nonzero vector, preserves every unclaimed field, and
skips spatial APIs on identical frames (`eval/v2x/README.md:13`).

## 2. What the current estimator actually does

| Phase | CURRENT implementation and consequence | Bound coordinates |
|---|---|---|
| Initial refusal | Both spatially flat frames return `None`, even if their constant lumas differ. | `eval/v2x/gm_bands_hv.py:60-72` |
| Static band detection | Removes consecutive byte-identical outer rows and columns. The condition is exact equality, not a semantic chrome classifier. A changing clock interrupts the leading-row scan. | `eval/v2x/gm_bands_hv.py:74-115` |
| Identical textured pair | Returns `(0, 0, 0)` directly; the harness separately skips identical spatial pairs. | `eval/v2x/gm_bands_hv.py:106-110`; `eval/v2x/README.md:13` |
| Projection candidates | Uses int64 row sums, valid eight-row box smoothing, a step-eight coarse grid, eight smoothed winners plus one raw coarse winner, and refinement within seven rows. It keeps three refined vertical candidates. | `eval/v2x/gm_bands_hv.py:123-190` |
| Vertical overlap | Requires `max(16, band_H // 4)`, without the proposed upper cap of 64. | `eval/v2x/gm_bands_hv.py:132-145` |
| Horizontal candidates | Builds compensated column sums for the retained vertical candidates, performs the analogous smoothed coarse/refined search, and keeps up to three horizontal choices per vertical choice. | `eval/v2x/gm_bands_hv.py:192-257` |
| Joint score | Uses luma SAD over the candidate's overlap and normalizes it to the complete cropped band area. `_joint_sad` can stop after crossing a safe raw cutoff. | `eval/v2x/gm_bands_hv.py:38-48,259-286` |
| Alias refusal | A non-adjacent candidate is at Chebyshev displacement distance at least four. An exact score tie or a gap below full `band_total_area` rejects the winner. | `eval/v2x/gm_bands_hv.py:291-306` |
| Changed support | Counts overlap-local tiles only if they changed in place. Requires at least 600 ppt of their area to match the translated previous frame exactly. | `eval/v2x/gm_bands_hv.py:308-328` |
| Consensus output | A destination tile must differ in place, have an in-bounds translated source for every real pixel, and match it exactly. Padding outside the real frame is ignored; out-of-overlap real pixels fail. | `eval/v2x/gm_bands_hv.py:331-387` |
| Diagnostic accounting | Operation count is reset per decorated call and isolated by a `ContextVar`; it is not a shared accumulating counter. | `eval/v2x/gm_bands_hv.py:12-35`; `tests/test_v2x_gm_bands_hv_adversarial.py:95-101` |

**Analysis.** Whole-band normalization lets blank pixels alter the alias margin
without supplying texture correspondence. Removing every unchanged boundary can
also hide stationary textured counterevidence. Conversely, counting every
pixel equally lets different-sized independent movers produce an apparently
dominant displacement. The existing 600 ppt winning-support gate alone does
not explicitly measure a second explanatory motion.

The proposed overlap cap is especially relevant to the requested 1080-row
page jump: displacement `dy=-960` leaves 120 observable rows, while the
current full-frame requirement is 270 rows. The arithmetic follows directly
from `eval/v2x/gm_bands_hv.py:134-146`; it is not evidence that all large-scroll
fixtures fail.

### 2.1 VM functional observations for CURRENT behavior

The unchanged bound estimator was run through
`adv_gm.score(gm_bands_hv, seed=0x5EED, include_1080p=True)` in the VM with
Python 3.12.14 and NumPy 2.3.5. Its status was `OK`.

| Generator kind | Pairs | Exact | Silent | Wrong vectors | Static relabels |
|---|---:|---:|---:|---:|---:|
| `flat_window_drag` | 4 | 4 | 0 | 0 | 0 |
| `two_motions` | 3 | 3 | 0 | 0 | 0 |
| `chrome_clock` | 3 | 0 | 3 | 0 | 0 |
| `periodic_text` | 4 | 4 | 0 | 0 | 0 |
| `bare_caret` | 3 | 0 | 3 | 0 | 0 |
| `big_scroll_1080p` | 4 | 3 | 1 | 0 | 0 |
| **Total** | **21** | **14** | **7** | **0** | **0** |

These are functional outputs from the VM, not a host performance benchmark or
live-recovery evidence. No timing is presented as MEASURED.

The exact additional fixtures requested for this task were also evaluated
against the unchanged estimator, with radius 100 for the small fixtures and
1100 for the page jump:

| Fixture | CURRENT output observed | Required reference result |
|---|---|---|
| RNG 912, unequal competing blocks `(100,150)` and `(100,80)` | `(12, 0, 783227)` | `None` |
| RNG 101, sparse 96-by-128 texture, up 24 | `(0, -24, 0)` | `(0, -24, 0)` |
| RNG 101, same texture, up 4 | `(0, -4, 0)` | `(0, -4, 0)` |
| RNG 960, independent 1080-by-1920 frames sharing only 120 translated rows | `None` | `(0, -960, 0)` |
| RNG 444, translated 64-by-80 block over an otherwise stationary random 240-by-320 texture | `(12, 0, 0)` | `None` |

The unequal-block result is a concrete wrong global choice under the proposed
multi-motion contract. The two sparse-texture regressions already pass this
snapshot. Retaining them guards behavior; this document does not describe
them as newly discovered failures.

The RNG-444 observation uses `default_rng(444)` to generate the background,
copies it into both frames, then draws the block from the same generator.
The previous block is at `[80:144,100:180]`, the current block at
`[80:144,112:192]`, and `max_d=100`. The unchanged surrounding texture is
global counterevidence that the current boundary crop removes. This is a
concrete fixture for the proposed requirement to preserve stationary texture.

## 3. Counts and truth must come from the bound sources

`adv_gm.cases(seed=0x5EED, include_1080p=True)` has **21 cases and 21 frame
pairs**, not nineteen test methods. Each case has two frames and one truth
entry. The test derives the count from the generated list and the truth-vector
lists, and checks the kind distribution rather than trusting prose.

| Kind | Count | Definition and truth |
|---|---:|---|
| Flat window drag | 4 | `(4,4)`, `(8,8)`, `(20,0)`, `(0,-12)`; `eval/v2x/adv_gm.py:44-71` |
| Two motions | 3 | `None`; `eval/v2x/adv_gm.py:73-108` |
| Chrome clock | 3 | `(0,-24)`; `eval/v2x/adv_gm.py:110-151` |
| Periodic text | 4 | `None`; `eval/v2x/adv_gm.py:153-179` |
| Bare caret | 3 | `(8,0)`; `eval/v2x/adv_gm.py:181-206` |
| Large scroll | 4 | Vertical shifts `-600`, `-830`, `600`, `816` on 1088-by-1920 frames; `eval/v2x/adv_gm.py:208-232` |

Disabling the optional large frames gives **17** cases. The label `1080p` in
the generator does not change its actual height of **1088**. The separately
requested RNG-960 fixture really has height **1080**.

There are **seven** `test_` methods in the existing adversarial test class:
static-tile safety, competing motion refusal, large-scroll vector safety,
joint-verification bound, valid smoothing, integer-only source, and isolated
OPS (`tests/test_v2x_gm_bands_hv_adversarial.py:24-101`). The large-scroll method
contains six `subTest` displacements (`:45-56`); those are not six additional
test methods. The proposed module's own method count is obtained from unittest
discovery; it need not equal either fixture count.

The oracle is a third denominator: **71 conformance cases**, comprising seven
named cases plus 64 SplitMix64 cases (`eval/v2x/gm_v2_oracle.py:38-44,47-196`).
It asks a selected implementation for expected vectors, scores, and masks
(`:199-229`) and compares a subprocess response bit for bit (`:304-379`). This
establishes implementation conformance, not an independent annotation of
correct global motion. No Rust conformance run is claimed here.

### 3.1 The bare-caret card/scorer discrepancy

Three different facts must remain separate:

1. **CURRENT scorer truth.** The adversarial generator places a two-column
   caret on spatially flat luma 200, moves it right by eight, and explicitly
   supplies `(8,0)` as truth (`eval/v2x/adv_gm.py:181-206`). When truth is a
   vector, `None` is counted as **silent**, not exact (`:314-320`).
2. **CURRENT oracle card-case fixture.** The fixture called `bare_caret` moves
   a two-column caret right by two over stationary random luma in `[200,240)`
   (`eval/v2x/gm_v2_oracle.py:131-146`). It has no independent `truth_vectors`
   field. `expected()` calls the supplied variant (`:199-229`). The same case
   name does not make these pixels or semantics equivalent to the adversarial
   fixture.
3. **PROPOSED router policy.** A lone caret has insufficient distributed global
   support and is refused. This is intentional abstention, and its cost remains
   visible as three silent pairs in the unchanged adversarial scorer. It must
   not be relabeled as three exact successes to improve a total.

The estimator refers readers to `CARD_GM_V2.md`
(`eval/v2x/gm_bands_hv.py:3`), but that card's text is **AUSENTE from the five
bound sources**. Its literal acceptance wording cannot be quoted or inferred
from an oracle fixture name. Any intended card-level refusal of a bare caret
therefore differs from the explicit adversarial vector truth; the host must
resolve that policy mismatch before treating a recovery denominator as a
semantic accuracy claim. Neither source file is edited by this proposal.

The scorer has a separate explicit exception for the two byte-identical
periodic cases: `None` or `(0,0)` is exact for `periodic_text_1` and `_3`
(`eval/v2x/adv_gm.py:321-331`). Refusing those cases does not incur a silent
count.

### 3.2 Independent physical-edge safety is required

The canonical static-mask helper pads the previous frame with zero and the
current frame with one before testing equality
(`eval/v2x/adv_gm.py:18-25`). Therefore a partial tile at the physical frame
edge is nonstatic in that helper even if all of its real pixels are unchanged.
The canonical `static_relabels` count remains useful and is preserved, but it
cannot alone establish partial-edge safety. The references additionally compare
real in-frame pixels in place and reject every claim whose translated source
would leave the real frame. This matters for generated dimensions such as
300-by-300, which are not multiples of 16.

## 4. Proposed integer evidence contract

Every rule in sections 4 through 8 is **PROPOSED** and is scoped to the new test
module. Existing production behavior remains as described in section 2.

### 4.1 Coordinates, overlap, and arithmetic

Let `P` be the previous luma array and `C` the current one. Coordinates are
`(y,x)`, vectors are `(dx,dy)`, and a translated correspondence means
`C[y,x] == P[y-dy,x-dx]`. The observable destination rectangle for a frame of
shape `(H,W)` is the half-open rectangle

```text
y in [max(0,dy), min(H,H+dy))
x in [max(0,dx), min(W,W+dx))
```

Intersect that rectangle with the chosen common evidence band and its
translated source bounds before counting any evidence. Newly exposed real
pixels have no source; they receive no score, support, or correspondence vote.
This is a geometry condition, not a special luma value for a synthetic border.
No wraparound, mirror padding, or invented source pixels are permitted.

The reference output is `None` or a triple of Python integers with nonnegative
score and a nonzero vector inside the supplied componentwise radius. A zero
vector is a competing stationary explanation during verification, but never a
global-motion emission from these references. Identical and flat pairs abstain.
This conservative reference contract is narrower than the current production
function's direct identical-texture return and is compatible with the
claim-only harness's treatment of identical frames.

Use signed integer luma differences, int64 reductions, and Python integers for
cross-products and histogram counters. Never subtract uint8 arrays and then
interpret wrapped differences as luma error. There is no true division, float
literal, FFT, floating normalization, subpixel interpolation, or random choice
in either estimator. Generator RNGs are fixture construction only.

### 4.2 Informative support and stationary counterevidence

A luma pixel is informative if at least one permitted, in-bounds four-neighbor
has a different luma. The four neighbors are left, right, above, and below;
diagonal neighbors are not used. Masked pixels cannot create information in a
neighbor through a gradient across the mask boundary. The spatial gradient
predicate has no floating threshold: unequal luma means positive information.

For candidate `v`, let `A_v` contain observable, unmasked destination pixels
whose current pixel **or translated previous pixel** is informative. Define

```text
N_dyn(v) = number of pixels in A_v
S(v) = sum(abs(int(C[y,x]) - int(P[y-dy,x-dx]))) for (y,x) in A_v
```

`N_dyn` names the active comparison support; it is not a synonym for the number
of temporally changed pixels. In particular, stationary textured regions are
active counterevidence against a nonzero vector. The common band must retain
their coordinates even when their pixels are unchanged in place. It cannot be
the bounding box of temporal changes alone.

If both aligned neighborhoods are spatially flat, their comparison has zero
weight, including when their constant lumas differ. If one side is textured and
the other flat, the OR predicate retains that comparison and its full absolute
luma disagreement. Testing only `informative_current AND informative_previous`
would wrongly delete text-flat mismatches. `N_dyn == 0` always means abstention;
a flat coincidence is not an exact-match vote.

### 4.3 Compare aliases on informative support

Candidate quality is the rational residual `S/N_dyn`, compared without division:
candidate `a` is strictly better than `b` exactly when
`S(a)*N_dyn(b) < S(b)*N_dyn(a)`. Equal rational means are exact ties even if
their integer display scores were to differ under another normalization.
The returned score is the ceiling integer mean:

```text
score(v) = (S(v) + N_dyn(v) - 1) // N_dyn(v)
```

Thus score zero means exact zero residual, and every positive residual gets a
positive display score. Coordinate order makes search and reporting
deterministic; it does not make a tied match unique.

An alias is non-adjacent when the Chebyshev distance between its displacement
and the best displacement is at least four. First reject exact non-adjacent
rational ties. Then accept a unique zero-residual match if the remaining
support and multi-motion gates pass, even when the next-best mean is less than
one luma unit worse. This avoids refusing a genuine exact match just because a
large flat background diluted the former full-band gap.

For a positive-residual best candidate, the proposed alias improvement margin
is one luma unit on informative support. With runner `r` and best `b`, require

```text
S(r)*N_dyn(b) - S(b)*N_dyn(r) >= N_dyn(b)*N_dyn(r)
```

Use the rational values before score rounding. Never compare this gap to the
full frame or band area. Search-budget truncation is not proof that an omitted
alias does not exist; the bounded proposal and ambiguity-retention policy must
be reported separately.

### 4.4 Geometry must also bound the information predicate

The verifier recomputes the four-neighbor predicate inside each candidate's
eligible overlap crops, using the intersection of source and destination
allowed masks for both. It does not merely slice a full-frame gradient map.
Consequently, an exposed neighbor outside the overlap cannot make a boundary
pixel informative or change its weight. Preparation may inspect the full frame
to propose a band or candidate; only observable pixels can confirm a candidate.

## 5. Proposed compact chrome mask and evidence band

`compact_chrome_mask(P,C)` is a displacement-independent proposal. Its scope is
deliberately narrower than arbitrary small changed regions:

1. Compute temporal change `D = (P != C)`. On each changed row, the candidate
   span begins at its first changed column and ends one past its last changed
   column. Width is that entire span, including gaps. Do not split a wide span
   into several narrow masks to satisfy the cap.
2. Set edge depth to `min(64,H//4)`. A broad interior row has span wider than
   `W//8` and lies outside both edge-depth regions. If there is no broad
   interior changed row, return an empty mask.
3. An eligible top-edge row satisfies `y < edge_depth` and
   `y + 16 < first_broad_interior_row`. An eligible bottom-edge row satisfies
   `y >= H-edge_depth` and `y - 16 > last_broad_interior_row`. These are the
   exact strict comparisons; the separation choice is part of the proposal.
4. The row's complete changed span must be no wider than `W//8`. The remainder
   of that row must be nonempty, byte-identical between frames, and spatially
   flat (`min == max`). Stationary textured remainders make the row ineligible.
5. Mask only changed pixels inside a qualifying span. Keep unchanged pixels,
   including stationary textured islands and their unchanged informative
   adjacencies. The width test still applies to the complete span, so keeping
   an unchanged island cannot make a wider span qualify.

The common evidence band is the smallest rectangle containing every unmasked
temporal change and every unmasked informative pixel in either input.
Stationary texture therefore remains inside the verifier's band. The mask
excludes both current destination and translated previous source endpoints;
neither endpoint may provide a score or vote through an outlier hole.

The clock block in the bound generator occupies columns `[10,50)`, width 40
(`eval/v2x/adv_gm.py:128-130`). At frame widths 256 and 300, the cap is 32 and
37 respectively, so the clock is **not masked**. Its residual remains in the
verification evidence. At width 320, the cap is 40 and the separate scrolling
interior satisfies the geometric conditions. The reference test checks the
mask behavior separately from the requirement to recover all three clocks.
Recovery on the first two clocks must come from dominant distributed motion
support despite the retained clock disagreement.

This mask is a proposed geometric heuristic, not a learned or authoritative UI
role detector. Small moving elements near an edge can resemble changing
chrome. Its usefulness and recall cost require the host replay.

## 6. Proposed changed-support, competing-mode, and consensus gates

### 6.1 Capped support on a fixed global grid

Use a global 16-by-16 destination grid anchored at `(0,0)`. For a candidate `v`,
intersect each tile with the candidate's observable band overlap and allowed
source/destination mask. This is its eligible scoring fragment. Let `c_t(v)` be
the number of pixels in that fragment that are both informative under `A_v`
and changed **in place**, `C[y,x] != P[y,x]`.

```text
w_t(v) = min(128, c_t(v))
N_changed(v) = sum(w_t(v))
```

An unchanged tile has zero weight. A flat-flat fragment has zero weight. A large
number of feature correspondences or changed pixels inside a single tile can
never make it worth more than 128 support units.

A tile supports the candidate only if its weight is nonzero and every eligible
real pixel in its scoring fragment equals the translated previous pixel. Let
`M_t(v)` be that exact-fragment predicate and define

```text
P(v) = sum(w_t(v) for tiles with M_t(v))
```

The SAD and support weights ignore flat-flat comparisons; the exact-fragment
eligibility check is a separate, stronger correspondence condition. Flat
pixels cannot supply positive weight, and an all-flat match cannot pass the
nonzero-support requirement. The final public consensus rule below is stronger
again: it verifies every real pixel of a whole destination tile, not just the
eligible scoring fragment.

After rational residual ranking and alias checks, require all of the following:

- the best vector is nonzero;
- `N_changed(best) > 0`;
- `P(best) >= 512`;
- at least four exact supporting tiles;
- support in at least two distinct tile rows and two distinct tile columns;
- `P(best)*1000 >= 600*N_changed(best)`.

This prevents a lone caret, a single tiny patch, or many repeated features in
one local tile from posing as distributed global evidence. The spatial
requirements intentionally have a possible recall cost for narrow moving
regions; they are proposed choices, not live-calibrated thresholds.

### 6.2 Explicit second-mode veto

Every distinct nonzero candidate is a possible competing mode, including a
vector one, two, or three pixels away from the winner. The alias distance of
four does **not** exempt nearby independent motions from this veto. Zero is
already present as a stationary competitor in the rational residual ranking;
if it wins, abstain before emitting anything.

For each runner `r`, recount its explanatory support on the primary candidate's
changed informative pixels within the intersection of both candidates' valid
overlaps. Reapply the per-tile cap of 128 on this common support. A runner tile
contributes only if the corresponding eligible common fragment matches the
runner's translation exactly. Call the resulting sum `R(r|best)`. Pixels
outside either valid overlap are unavailable; they are never manufactured as
positive votes or mismatches.

Veto the primary when **either** exact integer inequality holds:

```text
R(r|best)*1000 >= 250*N_changed(best)
P(best) < 3*R(r|best)
```

Use multiplication before comparison; do not round a ppt percentage first.
Exactly 250 ppt triggers refusal. A primary exactly three times the runner
passes the ratio condition, but can still fail the 250-ppt condition. The test
checks these boundaries and an actual pair of independently moving blocks
with horizontal displacements 12 and 13.

The requested unequal RNG-912 blocks make this distinction practical: the
larger mover must not become a global vector merely because it clears a
winning-support fraction. An independently supported runner remains a reason
to abstain.

### 6.3 Whole-real-tile public consensus

`consensus_tiles(P,C,s,dx,dy)` returns a boolean mask of shape
`((H+s-1)//s, (W+s-1)//s)`. A tile is true exactly when:

1. at least one of its real destination pixels changed in place;
2. every real destination pixel has an in-bounds translated source; and
3. every real destination pixel equals that source pixel byte for byte.

The last partial tile row or column includes only real frame pixels. Padding
used to implement reductions is zero/false and supplies no evidence. Real
exposed pixels are initialized as mismatches, so a partly exposed tile cannot
be promoted merely because its in-overlap fragment matched. Calling consensus
with a zero vector cannot claim a changed tile, and a fully out-of-bounds
vector yields no claims.

The mask does not apply the chrome outlier suppression or the informative
weighting to weaken this final equality requirement. A masked clock, a static
tile, and a partly exposed tile cannot acquire MOVED solely because the vector
was accepted elsewhere.

## 7. Runnable candidate generators and every proposed parameter

The test module exports these callable references:

| Callable | Function |
|---|---|
| `estimate_dynamic_band_v21(P,C,max_d)` | Integer projection proposals, local changed-row proposals, then the shared evidence and refusal rules. |
| `estimate_constellation(P,C,max_d)` | Deterministic 16-square exact-luma feature correspondences and an integer offset histogram, then the same verifier. |
| `consensus_tiles(P,C,s,dx,dy)` | Exact changed-only whole-real-tile claims for either reference. |

The references do not call the production `estimate_global_motion`, depend on
an unavailable `gm_constellation`, import a Rust kernel, or use an external
descriptor package. Sharing the verifier makes differences in candidate
coverage inspectable; it does not make the two outputs independent statistical
evidence of live accuracy.

### 7.1 Dynamic-band candidate retention

For a projection of length `L`, enumerate every integer shift in
`[-min(max_d,L-1), min(max_d,L-1)]` with sufficient overlap. Rank by the tuple
`(raw_projection_SAD*L//overlap_length, abs(shift), shift)`. This is candidate
ranking only; the final pixel score uses `N_dyn` and rational comparisons.

Always retain the best eight raw vertical offsets for the main band, together
with zero. Independently apply valid eight-element box smoothing, retain the
best eight coarse candidates whose offsets are multiples of eight, and add
the best raw candidate in each neighborhood of offsets differing by less than
eight. Smoothed proposals add candidates; they cannot replace the retained raw
winners. No projection padding, FFT, or rounding to a tile displacement is
used. For the smoothed projection of length `L-7`, the minimum overlap is
`max(1, minimum_raw_overlap-7)`. Exactly one raw minimum is added per retained
coarse basin (`PROJECTION_BASIN_KEEP=1`).

For each retained `dy`, form column projections over its compensated row
overlap. Apply the same integer retention with at most four raw horizontal
winners, plus smoothed-neighborhood winners and zero, and take the resulting
`(dx,dy)` pairs. Both axes use the bounded minimum-overlap rule; in particular:

```text
min_ov_y = max(16, min(64, band_H // 4))
```

The horizontal application is the same proposed formula with band width.
A 1080-row band requires 64 rows, so 120 rows are sufficient geometric
overlap for `-960`. A `-4` candidate is assessed at its actual integer offset
even though it is not a coarse multiple of eight. Dedicated tests check both
the retained projection candidates and the final exact output tuples.

To expose secondary movers, also form contiguous runs of rows with unmasked
temporal change. Give each run the smallest column interval containing its
changes. Rank runs by descending changed-pixel count, then deterministic
coordinates, and propose from at most eight regions. Each region keeps three
raw offsets per axis, supplemented by the same coarse basins. Regions smaller
than 16 on either axis produce no projection pair. Skip an identical copy of
the main band and deduplicate displacement pairs.

These local regions only propose vectors. They never crop the final
counterevidence or change its support denominator. Every retained pair,
including zero, is verified against the common evidence band.

### 7.2 Test-local constellation: exact luma signatures

This reference chooses the explicitly permitted **luma-signature** route.
There is no inferred d48 layout, external d48 module, perceptual hash tolerance,
or floating descriptor. A signature is exactly the 256 uint8 bytes of a
16-by-16 patch in row-major order. It is not a hash whose collision could be
mistaken for equality.

For every integer patch origin with a complete unmasked in-frame square,
compute its gradient as the sum of absolute differences over the patch's
internal horizontal and vertical edges: 16 times 15 edges of each orientation.
Use signed int64 differences and integral-image sums. Retain candidates with
gradient at least 256.

Partition possible **origins** into fixed 16-by-16 cells. Keep at most two
origins per cell, in descending gradient order, with smaller row and then
smaller column breaking a gradient tie. Then take the global best 256 by
`(-gradient,y,x)`. All integer origins are considered before this selection;
origins are not restricted to the tile lattice. This is necessary to match
motions such as four, ten, twelve, or 24 pixels without inventing an aligned
16-pixel displacement.

Pairing means a previous-frame patch/current-frame patch correspondence.
Group previous features by exact signature bytes. Iterate current features in
`(y,x)` order and matching previous features in `(y,x)` order. Each matching
pair proposes `(current_x-previous_x, current_y-previous_y)`, if both components
are within `max_d`. A current origin's destination tile may contribute at most
one vote to a given displacement, regardless of duplicate feature records.
The histogram consists only of integer counts.

Rank histogram modes by `(-votes, abs(dx)+abs(dy), dx, dy)`. The nominal budget
is 32 modes, but retain **all** modes whose votes equal or exceed the vote
count at rank `min(32,number_of_modes)`. Thus a coordinate tie-break cannot
silently drop equally supported aliases at the cutoff. If this retained set
has more than 256 modes, propose only zero and abstain; the hard limit is an
explicit saturation refusal, not an arbitrary selection among tied modes.
An empty histogram likewise leaves only zero. Always include zero in final
verification. A feature
vote is a proposal, not a MOVED claim: the candidate still needs informative
residual uniqueness, distributed exact changed support, the second-mode veto,
and the public whole-real-tile consensus. Coordinate order does not resolve
an exact motion-score tie in favor of one vector.

### 7.3 Parameter table

**Every entry is a PROPOSED CHOICE.** Where a numeric value resembles the
current code, its meaning below is still the new reference's definition. No
entry is claimed as a calibrated optimum.

| Code parameter | Proposed value | Exact role |
|---|---:|---|
| `TILE` | 16 | Global support grid and destination vote-cell side. |
| `CHROME_EDGE_CAP` | 64 | Maximum edge depth for compact-row masking. |
| `CHROME_EDGE_DIVISOR` | 4 | Edge depth also bounded by `H//4`. |
| `CHROME_SPAN_DIVISOR` | 8 | Complete changed-span width limited to `W//8`. |
| `CHROME_SEPARATION` | 16 | Strict edge-row/interior-row separation test in section 5. |
| `PROJECTION_SMOOTH` | 8 | Valid smoothing width, coarse-grid spacing, and strict refinement distance. |
| `PROJECTION_COARSE_KEEP` | 8 | Retained coarse smoothed basins per axis. |
| `PROJECTION_RAW_KEEP` | 8 | Main-band raw vertical winners retained independently of smoothing. |
| `PROJECTION_AXIS_KEEP` | 4 | Maximum raw horizontal winners per retained vertical candidate. |
| `PROJECTION_BASIN_KEEP` | 1 | Raw minimum added per retained smoothed coarse basin. |
| `PROJECTION_REGION_KEEP` | 3 | Raw per-axis winners in each local changed-row region. |
| `PROJECTION_REGION_LIMIT` | 8 | Maximum local changed-row regions. |
| `MIN_OVERLAP` | 16 | Lower overlap bound and minimum projection-region side. |
| `MAX_MIN_OVERLAP` | 64 | Cap on the quarter-band overlap requirement. |
| `ALIAS_DISTANCE` | 4 | Chebyshev distance defining non-adjacent aliases. |
| `ALIAS_MIN_LUMA` | 1 | Required positive-residual improvement in rational mean luma units. |
| `PPT` | 1000 | Integer ratio scale, with no rounded intermediate percentages. |
| `PRIMARY_MIN_PPT` | 600 | Minimum primary fraction of capped changed support. |
| `SECONDARY_VETO_PPT` | 250 | Inclusive secondary explanatory fraction triggering refusal. |
| `PRIMARY_RUNNER_RATIO` | 3 | Primary below this multiple of a runner is refused. |
| `TILE_WEIGHT_CAP` | 128 | Maximum changed-information support weight of one global tile. |
| `MIN_SUPPORT_WEIGHT` | 512 | Minimum sum of capped exact supporting weights. |
| `MIN_SUPPORT_TILES` | 4 | Minimum number of distinct exact supporting tiles. |
| `MIN_SUPPORT_ROWS` | 2 | Minimum number of distinct supporting tile rows. |
| `MIN_SUPPORT_COLS` | 2 | Minimum number of distinct supporting tile columns. |
| `FEATURE_SIZE` | 16 | Exact luma patch side. |
| `FEATURE_CELL` | 16 | Origin-cell side for local feature selection. |
| `FEATURES_PER_CELL` | 2 | Maximum selected feature origins per origin cell. |
| `FEATURE_TOP_K` | 256 | Global feature budget per frame. |
| `FEATURE_MIN_GRADIENT` | 256 | Minimum summed internal-edge gradient for a feature. |
| `HISTOGRAM_KEEP` | 32 | Nominal histogram mode budget. |
| `HISTOGRAM_HARD_LIMIT` | 256 | Saturation refusal if retaining every mode tied at the nominal cutoff exceeds this count. |
| `PAIR_PER_DEST_TILE_MODE_CAP` | 1 | One correspondence vote per destination tile per mode. |

`max_d` and the public consensus tile side `s` are explicit caller inputs, not
hidden tuning defaults. The tests use the task's prescribed RNG algorithms,
seeds, dimensions, positions, and radii. Array validation rejects floats,
mismatched or empty shapes, Boolean/noninteger radii, and negative radii; it
does not silently quantize malformed inputs.

## 8. Complexity, determinism, and boundaries of the proposal

Both paths perform integer preprocessing over frame pixels. The dynamic path
exhaustively ranks one-dimensional integer offsets, not every two-dimensional
image displacement. Its projection work can grow quadratically with an axis
length, and final verification grows with retained candidates times observable
pixels. The local-region budget bounds how many extra proposal searches run;
it does not establish a host frame-time budget.

The constellation path computes dense 16-square gradients using integral
sums, selects bounded features, and considers at most `256*256` feature pairs
before displacement deduplication. Repeated signatures can increase the
number of offset modes within that bound. Exact pixel verification remains
necessary; a large histogram peak alone is never sufficient evidence.

Ranking retains scalar and tile summaries for candidates. Full per-pixel
primary and runner masks are constructed sequentially during runner checking,
so those frame-sized arrays are not retained for every candidate at once.
This is a test-local resource choice, not a claimed native optimization.

Every ordering is explicit: coordinates resolve feature-gradient ties, raw
projection ties prefer smaller absolute displacement then signed displacement,
histogram ties use vote count then Manhattan magnitude and coordinates, and
rational residual ties have the same deterministic coordinate ordering for
inspection. Safety decisions inspect ties before any non-adjacent winner can
be emitted. There is no dependence on Python set iteration order, RNG state
during estimation, wall time, thread order, or filename/case identity.

The finite feature and proposal budgets mean uniqueness is established over
the proposed hypotheses, not every conceivable displacement. The reference
tests make periodic aliases, adjacent competing modes, compact outliers, sparse
texture, and exposed borders concrete. General recall, scale robustness, and
exhaustive ambiguity detection outside those tests remain unverified.

## 9. Synthetic validation contract

Both independently callable candidate generators feed the same conservative
verifier and consensus rule. Each reference is evaluated on every generated
adversarial case, using `max_d=max(W,H)-1` as the canonical scorer does
(`eval/v2x/adv_gm.py:279-284`). The tests require zero wrong vectors and zero
static relabels; additionally, all three `chrome_clock` cases must emit exactly
`(0,-24)` and all three `two_motions` cases must return `None`. Silent responses
remain explicit in per-kind and overall scorer output.

The required deterministic additions are:

| Test input construction | Required output from each reference |
|---|---|
| `default_rng(912)`; `(480,640)` luma-40 background; independent blocks of shape `(100,150)` at `(64,64)` and `(100,80)` at `(280,380)`; shifts `(12,0)` and `(0,-10)` respectively; `max_d=100` | `None` |
| `default_rng(101)`; `(480,640)` luma-250 background; one `(96,128)` texture at `(180,256)`, moving up 24 rows; `max_d=100` | `(0,-24,0)` |
| The same RNG-101 fixture definition, moving up 4 rows; `max_d=100` | `(0,-4,0)` |
| `default_rng(960)`; independent previous and current `(1080,1920)` frames; overwrite `cur[:120]` with `prev[-120:]`; `max_d=1100` | `(0,-960,0)` |

The unequal-block fixture is deliberately different from the current helper's
two equal `(96,128)` blocks (`tests/test_v2x_gm_bands_hv_adversarial.py:12-21`)
and from its smaller equal-block refusal fixture (`:35-43`). A passing
equal-block test does not establish the unequal-block veto.

Further targeted checks cover exact non-adjacent tie refusal, acceptance of a
unique exact zero, flat support abstention, flat-flat versus text-flat evidence,
stationary texture as counterevidence, nonzero distributed support, vote caps,
overlap border exclusion, input immutability, deterministic integer output,
integer-only source arithmetic, and the existence of this nonempty document.
There are no `expectedFailure` markers in the references. Functional failures
must remain visible; they cannot be converted into skips or nominal passes.

The task's VM command is:

```sh
python3 -m unittest tests.test_jq1_a1_gm_v2_dynamic_band_and_constellation_spec
```

Final observed command output belongs in the accompanying collection report.
The command verifies this bounded synthetic contract. It does not certify
production integration, a native port, live accuracy, host throughput, or the
21-of-24 live target.

## 10. Host replay acceptance protocol: live recovery is UNVERIFIED

**Live corpus availability: AUSENTE. Live recovery: UNVERIFIED.** The requested
host target is recovery of at least **21 of 24 episodes**, with **zero wrong
vectors** and **zero static false emissions**. The synthetic set has 21 frame
pairs, while the live target has 24 episodes; these denominators are unrelated.
A synthetic total must never be substituted for a live result.

The following is a **PROPOSED** host protocol. Its evidence remains unfilled
until the host actually supplies and replays the corpus.

1. **Freeze the replay manifest before running.** Record the 24 unique episode
   IDs, corpus version/digest, ordered frame-pair IDs, dimensions, luma
   conversion, search radius, tile size, exact integer vector truth or explicit
   abstention truth, and the episode's designated recovery opportunities. Keep
   pixels in RAM throughout the reference run. Record the source commit,
   reference file digest, Python and NumPy versions, and all proposed parameter
   values. Do not select easy episodes after seeing results.
2. **Fix the episode predicate before counting.** The proposed strict predicate
   is that every predeclared recoverable target pair in an episode returns its
   exact nonzero vector and at least one legal changed consensus tile. Every
   other evaluated pair must also obey its vector/refusal truth and the static
   and border safety rules. An episode with no registered nonzero-motion
   opportunity is recorded as refusal coverage and cannot inflate motion
   recovery. If the owner's existing 24-episode contract uses a different
   definition, record that contract and the difference before execution; its
   definition is currently **AUSENTE**.
3. **Replay deterministically in one pass per variant.** Call each reference on
   each recorded pair in order with the same arrays and radius. Use no caches
   keyed to truth, case ID, or corpus position. Inputs must remain unchanged.
   Capture emitted vector, integer score, consensus mask digest/counts,
   abstention reason, candidate support diagnostics, and the expected truth.
4. **Compute the safety counters independently.** A wrong vector is any
   non-`None` returned vector unequal to registered single-motion truth,
   including `(0,0)` on a nonzero-motion target and any returned vector on
   refusal truth. A returned zero vector is additionally a malformed output
   under the proposed references' nonzero-or-`None` contract. Count every such
   occurrence, even in an otherwise recovered episode. For static false
   emissions, independently count both returned vectors on identical frame
   pairs and claims on exact in-place static tiles, reconstructing the tiles
   from real pixels. Require both counters to be zero and report them
   separately. For every claimed tile also check that its complete
   real destination rectangle has a valid translated source and equals it
   exactly. Border errors and unsupported claims are failures regardless of
   the aggregate score.
5. **Report episode and pair denominators together.** Provide the 24-row
   episode table, recovered/not-recovered status, target-pair exact/silent/wrong
   counts, static false emissions, border failures, and rejection reasons. Keep
   a separate row for each reference. Expose all silent opportunities and all
   exceptions; do not suppress partial failure diagnostics. Preserve the
   untouched scorer truth and separately document any approved semantic truth
   revision, particularly for bare carets and multiple motions.
6. **Apply the gate without averaging away failures.** Each variant is eligible
   only if recovery is at least 21 of 24 under the frozen predicate, wrong
   vectors equal zero, static false emissions equal zero, border violations
   equal zero, and no replay exception or malformed output occurred. The host
   records a failing result when any condition fails; it does not select the
   better reference independently for each episode unless such a router was
   specified and frozen before replay.
7. **Keep performance claims separate.** Functional equality and integer
   determinism are replay evidence. Any latency, memory, or throughput target
   needs an explicitly identified host, workload, methodology, and actual
   observations. VM wall time is diagnostic only, as already required by
   `eval/v2x/README.md:22`. No measured host budget or live latency is supplied by
   this worker.

Reference logs may contain scalar diagnostics and hashes, but the runnable
reference tests never write frames to disk. A pull request is a collection
vehicle; only the host/orchestrator can record acceptance.

## 11. Limits and AUSENTE register

- **AUSENTE: live frames, episode truth, and the existing per-episode recovery
  predicate.** The 21/24 target is a task requirement, not an observed result.
- **AUSENTE: literal `CARD_GM_V2.md` policy text in the bound evidence.** This
  document quotes only the estimator's reference to that card and the actual
  generator/scorer semantics.
- **AUSENTE: a bound `gm_constellation` module.** The reference is implemented
  locally in the new test module and never imports such a module. The source
  API table permits independent variant modules but does not establish this
  absent implementation (`eval/v2x/README.md:3-9`).
- **AUSENTE: production or Rust parity for these new rules.** The oracle can
  verify a selected implementation's bit-level contract; no new native kernel
  or changed production module is delivered here.
- **PROPOSED, not calibrated:** the new masks, support thresholds, feature
  budget, pairing policy, and distribution requirements. Their exact integer
  definitions make the experiment reproducible; synthetic success alone does
  not establish optimality or live recall.
- Exact luma matching is deliberately conservative. Resampling, antialiasing
  changes, fades, lossy capture, rotation, scale change, and occlusion can
  reduce support. Whether the host corpus requires tolerance for those changes
  is **AUSENTE**. Adding tolerance would require a separately specified and
  retested evidence contract.
- The reference imposes bounded candidate retention and compact signatures.
  Candidate loss can cause abstention. Exhaustive detection of every possible
  global displacement is not asserted. Safety gates limit emissions, and the
  live recall cost remains unverified.

