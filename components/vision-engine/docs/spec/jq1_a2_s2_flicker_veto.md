# Two-pass S2 FLICKER veto: specification and integer reference

## 1. Decision, scope, and source binding

**The reference removes only independently contradicted baseline S2 FLICKER candidates, before later temporal tracker declarations.** Its first pass validates history and gathers pixel, motion-consensus, and exposed-window evidence. Its second pass returns a veto mask restricted to the original `cls == C_FLICKER` entries with `words_eq == False`. The caller can change those entries to CHANGED in a copy. Positive FL tracker declarations are applied afterward and are never subjected to the baseline lag-two gate.

The pure reference and regression tests are in `tests/test_jq1_a2_s2_flicker_veto.py`. They implement **P1 exact-return preservation, strengthened P2 pixel nonreturn, and F6 exposed-window evidence**. D1 motion arbitration, D2 neighborhood support, and D3 temporal transport below are analyzed production alternatives; this change does not implement or wire them into production. This document supplies the C2 and FL v3 design contract, not a production acceptance certificate.

All ten bound sources were retrieved through the GitHub connector at explicit ref `bridge/integration`, resolved for this task to commit `9ad5c5181fdd1d013eac069631d5db0e1557aedc`. Implementation citations below are one-based coordinates in those retrieved files. The inbox records the older commit `2974d426082bae0920026ce67d689b1e5c0f2674`; it is not presented as the observed source. The user's instruction to resolve and read `bridge/integration` determines this analysis. The two bound earlier specifications describe their own historic snapshots (`docs/spec/FLICKER_MOTION_DISCRIMINANT.md:7,20-22`; `docs/spec/CANVAS_ANIM_FALSE_FLICKER.md:11-15`); their historic source identities do not override this task's current reads.

Evidence labels have distinct meanings:

- **CITED:** inspected repository implementation or a specifically identified user-supplied target.
- **DERIVED:** arithmetic or a consequence of the stated code, without a performance measurement.
- **VM OBSERVED:** a deterministic local fixture result produced by actual S1/S2 calls and unit tests in the repository VM.
- **PROPOSED:** behavior or integration described here but absent from production.
- **AUSENTE:** behavior, API, provenance, campaign data, or acceptance not established by the inspected material.

### Host objectives, not synthetic measurements

The user supplies a residual host population of **5281 tile-frames**, comprising **3426 exposed-scroll** and **1855 canvas** cases. The requested host targets are at least 80 percent removal, canvas residual at most 100, and no loss of the recorded 81 correct caret detections out of 90 opportunities. **DERIVED integer gate:** at least 4225 removals and residual at most 1056, since `5281 - ceil(4*5281/5) = 1056`. Preserving caret recall means retaining the previously correct detections, not exchanging lost detections for different ones while keeping an aggregate numerator.

These are **HOST TARGETS**. The older bound documents repeat the 1855 canvas figure as operator-transcribed campaign evidence (`docs/spec/CANVAS_ANIM_FALSE_FLICKER.md:7`; `docs/spec/FLICKER_MOTION_DISCRIMINANT.md:18`). They do not provide the original campaign manifest, event identities, the current 5281/3426 residual breakdown, or the recorded 81/90 detection identities; those remain AUSENTE. The VM fixtures below do not claim to reproduce those denominators or achieve their targets. In particular, the reference deliberately retains exact-return periodic-scroll ambiguities.

## 2. Actual APIs and the conceptual adapter

| Surface | Status and concrete contract |
|---|---|
| `change.ClassState` | CURRENT descriptor/confidence histories plus `y_prev`; initialization at `visioncore/change.py:121-135`, digest at `138-141`, advancement at `468-482`. |
| `change.classify_tiles` | CURRENT in-place state step returning `(tiles_out, state, summary)`; `visioncore/change.py:303-307`. `classify_first` supplies first-frame UNCHANGED behavior at `490-502`. |
| `desc.describe_padded` / `fill_xh64` | CURRENT eager/lazy descriptor and output-hash paths; `visioncore/desc.py:67-127`. |
| `pipeline.analyze` | CURRENT production caller, including lazy S1 and post-S2 hash filling; `visioncore/pipeline.py:174-177,203-223`. |
| `chain.run_chain` | CURRENT scene-driven GM/FL evaluation harness; signature and accepted orders at `eval/v2eval/chain.py:62-67`, actual frame loop at `112-149`. |
| `postpass.apply_claims` | CURRENT copying overlay helper, later list entries overwrite earlier claims; `eval/v2eval/postpass.py:28-35`. |
| `fl_blinkprior.FlickerTracker` | CURRENT requested FL tracker, initialized at `eval/v2x/fl_blinkprior.py:51-69`, updated at `86-105`, positive emission at `228-237`. |
| `caret_blink` | CURRENT verified renderer scene branch; `eval/gui_synth/raster.py:110-119`. |
| `veto_flicker` / `VetoDecision` | NEW TEST-LOCAL reference defined in the new test module, with the contract below. No production import or caller is added. |
| Router `d48` signature | CONCEPTUAL input, not an existing repository API established by these sources. It requires an explicit adapter. |
| `Stage2State`, `run_chain_on_frames`, `blink_cursor`, `modal_pop` | AUSENTE in the bound API evidence; no implementation or executable invocation is invented. |

A future router adapter must map its conceptual inputs into real `TILE_DT` arrays, their actual Morton ordering, `(s, gw, gh)`, exact previous-frame `words_eq`, synchronized luma planes, and explicit history/hash-validity attestations. It must carry the immutable **baseline S2** snapshot, not labels already modified by FL claims. A `d48` value alone establishes none of these properties. The adapter, its routing protocol, and production history persistence remain PROPOSED/AUSENTE.

## 3. Current baseline defects

### 3.1 Descriptor return is weaker than pixel return

Defaults are `T0_Q4=32`, `HYST_BAND_PERMILLE=100`, `H_DEAD=2`, and `SIG_DEAD_Q4=24` (`visioncore/params.py:15-18`). The actual floor formulas give entry 35 and exit 28 (`visioncore/change.py:49-51`). The signature-confidence threshold is therefore 24/16 luma units; equality remains inside the confidence band (`visioncore/change.py:67-92`).

The residual adds absolute differences of mean luma and contrast, eight times half-chroma differences, and eight times masked Hamming excess above `H_DEAD`; it never reads `xh64` (`visioncore/change.py:54-61`). Confidence masks are ORed between the relevant frames, and exact previous-frame `words_eq` forces lag-one residual to zero (`325-329,344`). Baseline rule 3 is:

```python
flicker = active & (r2 <= t_exit) & (r1 > t_enter)
```

That is the actual expression at `visioncore/change.py:345`. It asserts a descriptor return at a fixed tile, not RGB equality or an identified blinking object. The canvas texture adds low-five-bit deterministic noise to persistent bands and glyphs (`eval/gui_synth/raster.py:19-23,33-54`); each canvas frame changes the seed with `seed ^ (i * 7919)` (`129-131`). Its unmasked RGB values share the form `(g,g+12,g+24)`, whose YCoCg-R lifting yields constant chroma and changing luma (`visioncore/desc.py:34-40`; derivation in `docs/spec/CANVAS_ANIM_FALSE_FLICKER.md:68-85`). Perceptual return can consequently occur without an exact return.

The new helper recomputes internal residuals with the supplied luma confidence masks. It does not use `out['resid']` as Q4: that output is shifted by two bits and saturated, and DRIFT can use a different reference (`visioncore/change.py:461-467`).

### 3.2 Lazy state hashes and stale JITTER hashes

The real serial caller requests `lazy_xh=True`; the parallel path does likewise (`visioncore/pipeline.py:203,211-215`). S1 allocates zeroed descriptors and returns before hash computation (`visioncore/desc.py:79,99-109`). Exact-descriptor reuse copies `_COPY_FIELDS`, which excludes `xh64` (`90-94,130`), so it does not repair lazy raw hashes.

`ClassState.init` copies these input descriptors into `ref`, `prev1`, and `prev2`; subsequent S2 steps copy raw input again (`visioncore/change.py:121-135,309,478-479`). Only afterward does the pipeline copy the classified output and call `fill_xh64`; `prev_raw` still points at the raw describe result (`visioncore/pipeline.py:216-223`). Filling that independent output cannot retroactively populate captured ClassState histories.

Moreover, `fill_xh64` hashes dirty entries and copies previous hashes for other classes. Its own contract says JITTER copies are labels, not exact current-content hashes (`visioncore/desc.py:113-126`). The VM regression follows this sequence on uniform RGB 64 followed by RGB 65: all four second-frame tiles are JITTER, eager hashes change, emitted hashes retain the previous values, and both ClassState hash histories remain zero. This reproduces the relevant call ordering without claiming to have executed the entire production pipeline.

P1 therefore requires explicit hash validity. A zero-valued legitimate hash is possible; `hash != 0` is not provenance. The fixture's attested-zero test is an adversarial protocol-value test, not a claim to have found an actual zero BLAKE2 digest. A trusted inequality proves different exact content, but **does not prove motion or absence of semantic blinking**: a real caret can share a tile with changing background pixels.

### 3.3 Motion evidence can exist without baseline publication

Full-window motion offers require membership, all `s*s` source pixels, `den*sad_c < sad_0`, `16*sad_c <= s*s*t_exit`, and `sad_c < best`; an accepted offer mutates winner state (`visioncore/change.py:369-376`). Baseline membership is `active & ~flicker`, but global publication uses all active tiles as its denominator (`378-385`). For active count `A`, provisional FLICKER count `F`, and share `q`, publication is impossible whenever `(A-F)*1000000 < q*A`, even if all eligible tiles match. This is a derived exclusion mechanism.

Only a published global vector is offered afterward to FLICKER candidates and used for clipped-window `mexp` clearing (`386-390`). Failed global winners are withdrawn (`391-395`), and island membership still derives from the original non-FLICKER mask (`396-411`). Final class assignment is CHANGED, then MOVED, then remaining FLICKER (`416-418`). Partial `mexp` clearing alone produces CHANGED, not MOVED; its motion-history contribution is separate (`471`).

The scroll witness below makes this defect concrete: the estimator returns `(-4,0)` and full windows match exactly, while all 60 baseline tiles are provisional FLICKER and `summary['global_mv']` remains `None`. The reference independently checks motion evidence and does not pretend that the native publication succeeded.

## 4. Exact reference interface and evidence ownership

```python
veto_flicker(s2_tiles, prev2_tiles, prev1_tiles, words_eq, grid,
             gm_vector=None, y_prev2=None, y_prev1=None, y_cur=None,
             params=None)
```

The result is `VetoDecision(mask, reasons)`: a fresh `numpy.bool_` array and a fresh `numpy.uint8` array, both shape `(N,)`, in the input descriptor order. `mask=True` authorizes only baseline FLICKER-to-CHANGED demotion. It never authorizes MOVED, supplies a motion vector, or changes an input. `reasons` contains the stable values below, including positive preservation and veto explanations.

Current input must be a one-dimensional array whose dtype equals actual `change.TILE_DT`. Both history arrays must have the same shape, dtype, and Morton ordering. The grid is `(s,gw,gh)`, with `s` a multiple of eight in `[16,128]`, each grid dimension in `[1,255]`, `N=gw*gh`, and unique Morton coordinates covering the grid. `words_eq` must be an actual Boolean `(N,)` array. Luma must be integer arrays of shape `(gh*s,gw*s)` with values in `[0,255]`. Inputs carrying `F_PAD` in any of the three tile arrays abstain; the reference does not treat replicated padding as independent image evidence.

Luma values are validated in their original dtype and then normalized to signed `int16` before native confidence and motion helpers run. This prevents a mixed `uint64`/`int32` operation from promoting to floating point inside the inherited confidence implementation. The new dtype test covers signed and unsigned integer representations without mutating them; wrapped negative `int8` values abstain as invalid luma.

`params` is deliberately a **reference-only envelope**, not the production parameter table:

```python
{
    'thresholds': {  # optional integer overrides within core bounds
        'T0_Q4': 32,
        'HYST_BAND_PERMILLE': 100,
        'H_DEAD': 2,
        'SIG_DEAD_Q4': 24,
        'MV_SAD_RATIO_DEN': 3,
        'GLOBAL_SHARE_PPM': 500000,
    },
    'evidence': {
        'frame_ids': (t - 2, t - 1, t),
        'history_valid': bool_array_N,
        'hash_valid': bool_array_3_by_N,
    },
}
```

Frame IDs must be consecutive, nonnegative integers, oldest first. `history_valid` attests one stream, epoch, grid, coordinate system, and owned immutable snapshots across all three frames. `hash_valid` attests that each corresponding RGB hash was actually computed or inherited through proven exact byte equality. The helper verifies the shape and asserted values, not the honesty of the caller's provenance. A frame gap, reset, changed grid, missing snapshot, or reused mutable capture buffer must invalidate the attestation. There is no previous-published-GM field; D3 cannot be implemented by inferring one from the supplied current vector.

Malformed current arrays, grid, or threshold envelope raise `TypeError`/`ValueError`; floats and Boolean pseudo-integers are rejected. Missing or malformed history/luma/GM evidence instead leaves candidate bits false with explicit reasons. Missing `params` does not imply eager hashes: ordinary unattested arrays abstain. Production parameters reject unknown/noninteger fields (`visioncore/params.py:56-62`; `visioncore/pipeline.py:179-182`), so this envelope must never be passed to `pipeline.analyze`.

| Code | Reason | Meaning |
|---:|---|---|
| 0 | `NOT_BASELINE_FLICKER` | Outside the original class-4 population. |
| 1 | `WORDS_EQUAL` | Baseline class 4 but exact previous-frame equality; ineligible. |
| 2 | `MISSING_HISTORY` | A required descriptor history is absent. |
| 3 | `INVALID_HISTORY` | History shape, dtype, or Morton order disagrees. |
| 4 | `UNTRUSTED_HISTORY` | Invalid/absent continuity or per-tile ownership attestation. |
| 5 | `UNTRUSTED_HASH` | Any required exact hash lacks explicit validity. |
| 6 | `EXACT_HASH_RETURN` | Trusted current/lag-two return; preserved. |
| 7, 8 | `MISSING_LUMA`, `INVALID_LUMA` | Pixel evidence unavailable or malformed. |
| 9 | `PADDED_TILE` | At least one corresponding descriptor is padded. |
| 10, 11 | `INCONSISTENT_GM`, `NO_GM_CONSENSUS` | Supplied motion disagrees with canonical estimation or lacks required support. |
| 12, 13 | `PARTIAL_WINDOW`, `NO_INWARD_SUPPORT` | F6 lacks sufficient overlap or an inward supporting tile. |
| 14 | `NOT_THREE_DISTINCT` | The three trusted hashes are not pairwise distinct. |
| 15 | `PIXEL_RETURN_OR_SMALL_CHANGE` | Strengthened P2 is not satisfied. |
| 16 | `NOT_DESCRIPTOR_RETURN` | Recomputed baseline rule-3 residual conditions disagree. |
| 17, 18 | `VETO_P2`, `VETO_F6` | Authorized baseline demotion; F6 takes reason precedence if both pass. |

Reasons express the branch that determines the final result, not every failed predicate. A candidate may fail F6 and still satisfy independent P2. Padding is rejected before exact-return preservation. An exact trusted return needs no later luma/GM analysis. Other negative decisions require trusted, unpadded, pairwise-distinct hashes in all three frames.

## 5. The implemented two-pass decision

### Pass 1: trusted history, pixel evidence, and motion consensus

Freeze `candidate = (s2_tiles['cls'] == 4) & ~words_eq`. Validate evidence, preserve P1 exact returns, require the three remaining hashes to be pairwise distinct, and recompute `r1 > enter` and `r2 <= exit` using actual ORed confidence masks. This last condition checks agreement with the baseline descriptor predicate; it does not reconstruct unavailable classifier activity state.

Let `a=s*s`. Compute integer, unshifted pixel SADs `S21` for current versus lag one, `S20` for current versus lag two, and `S10` for lag one versus lag two. Subtraction widens before absolute value and reduction accumulates in int64. Actual clipped counts accompany every translated SAD. Empty overlap contributes zero SAD and count zero; count zero is never a match.

For **strengthened P2**, require all three full windows and:

```text
16*S20 > a*exit
16*S21 > a*enter
16*S10 > a*enter
```

This strengthens the bound alternative, which proposed only a lag-two return ceiling (`docs/spec/CANVAS_ANIM_FALSE_FLICKER.md:336-355`). Here, trusted RGB nonreturn is accompanied by substantial pixel differences on both adjacent transitions and lag two. Near returns, small adjacent changes, or non-distinct states abstain. RGB changes with identical luma can still pass through as retained FLICKER; this is a conservative coverage limit.

For **F6**, an absent vector or `(0,0)` provides no GM evidence and leaves P2 available. A supplied nonzero vector must have integral components within the canonical current search range and equal `change.global_vector(y_cur,y_prev1,s)`. The canonical estimator uses integer projections, overlap-normalized axis costs, deterministic ties, and strict improvement (`visioncore/change.py:157-187`). Incorrect direction, malformed vector, or a vector beyond `min(s,64)` causes abstention from all remaining negative decisions, including P2.

For a canonical nonzero vector, independently compute full-window informative tiles across **all baseline labels**. Informative tiles must be trusted, unpadded, have full shifted source windows, and positive unshifted SAD. A supporting tile additionally has zero shifted SAD and satisfies the native ratio/ceiling inequalities. Require at least three supporting tiles and support share at least `GLOBAL_SHARE_PPM` among these informative tiles. With defaults this is half. This reference denominator is expressly different from the native active-tile publication denominator. It does not inspect an `offer()` winner mask or assume prior native publication.

An eligible F6 candidate needs `0<count<a`, `2*count>=a`, zero shifted SAD, and strict normalized improvement `den*shifted_sad*a < S21*count`. At least one immediate inward neighbor, derived from the actually clipped image boundary, must be a full informative supporting tile for the same vector. For `(-4,0)`, the right-edge tile looks inward to its left neighbor. Diagonal exposure accepts one such inward supporter, as implemented; it is not the nine-window D2 guard.

Coordinates are checked geometrically through an explicit lookup, with no negative-index wrapping. This matters because the existing native SAD helper deliberately accepts negative tile indices and wraps them (`visioncore/change.py:215-226`). New F6 evidence never equates “near a moving tile” with “the center moved,” and never promotes a partial tile to MOVED. The named F6 rule is new; only the older clipped `mexp` mechanism exists in baseline (`383-390`).

### Pass 2: mask and caller arbitration

On the still-eligible subset, `mask = ready & (P2 | F6)`. F6 supplies the reason if both predicates pass. Every other class and every `words_eq`-true entry has a false mask. A caller applies the mask to a fresh baseline copy, changing only `cls` to CHANGED. The helper performs no input mutation, history rotation, serialization, capture, tracker update, or production call-chain modification.

The reference is stateless. Caller-owned histories retain three descriptor snapshots and the supplied luma planes; the helper adds linear-size Boolean/integer arrays and processes each tile window in integer arithmetic. Canonical projection estimation and a constant number of full-grid SAD passes dominate work; no host latency or memory acceptance is established. The inherited S1 `isqrt` uses a float64 seed followed by integer correction (`visioncore/desc.py:23-29`). That is current S1 behavior used by real fixture construction; the new veto predicates and helper arithmetic introduce no floating-point computation.

## 6. D1, D2, and D3 remain production alternatives

### D1: publication and repeated-offer hazards

The bound D1 proposal saves raw FLICKER, evaluates pure explanations across active tiles, includes A-B-A tiles in island discovery, and resolves motion before final class assignment (`docs/spec/FLICKER_MOTION_DISCRIMINANT.md:80-94`). A pure explanation answers whether a candidate satisfies pixel gates; a winning offer answers whether it newly beats the mutable current best. They cannot be substituted.

**Repeated-offer proof:** after expanding membership to `active`, the first global offer sets `best=sad_c` for a qualifying raw FLICKER tile. Re-offering that same vector at line 386 fails `sad_c < best` on equality. Its returned mask is false, so the raw FLICKER mask survives and line 418 overwrites MOVED with FLICKER (`visioncore/change.py:372-386,416-418`). Save pure masks and clear only surviving validated winners after withdrawal and ordered island competition. Preserve strict deterministic tie order (`347-348,401-411`).

Range extension is a separate experiment: changing membership does not search larger displacement. Global and island searches share the tile-sized bound (`visioncore/change.py:179-187,294-300`); native output vectors are int8 (`23-25`). The earlier proposed symmetric representable range of 127 is one possible arm, not implemented here (`docs/spec/FLICKER_MOTION_DISCRIMINANT.md:84`). Wider search also needs adequate crop overlap and representable output. Publication changes can affect FROZEN/DRIFT ambiguity and motion history, not just FLICKER (`visioncore/change.py:420-445,469-475`).

### D2: independently informative neighborhood

The documented D2 safeguard requires the center and all eight immediate neighbors to have full source windows and zero translated pixel SAD; zero-shift SAD must be positive at the center and at least one neighbor (`docs/spec/FLICKER_MOTION_DISCRIMINANT.md:96-108`). Missing border neighbors or uninformative context preserve baseline. It needs a D1 candidate vector and correct overlap accounting; it estimates no motion by itself. Noise, occlusion, and nonrigid changes reduce coverage. Repeated imagery can still be observationally ambiguous. D2 is **not implemented by F6's single inward-neighbor rule**.

### D3: transport over two transitions

D3 requires equal nonzero published vectors on both transitions, valid strict one-step improvement, and full lag-two transport matching at `2*v`, with `16*SAD_2v <= a*exit` (`docs/spec/FLICKER_MOTION_DISCRIMINANT.md:110-122`). Compute `2*v` in widened integers. Do not demand strict improvement over unshifted lag-two SAD: exact A-B-A aliases can make both zero. Such ties must not invalidate otherwise established transport.

It additionally needs prior publication, a second luma history, continuity/reset handling, and owned immutable buffers. Current ClassState lacks those contracts, and its digest omits even `y_prev` (`visioncore/change.py:132-141,478-482`). Gaps, acceleration, vector changes, first transitions, and island-only evidence fail the proposed guard. The bounded helper has no prior-published-vector field and does not implement D3. It cannot infer missing temporal evidence from the current tuple.

## 7. VM fixture witnesses and exact local counts

Fixtures call real eager `desc.describe_padded`, `ClassState.init`, `classify_first`, and `classify_tiles`. Main scene fixtures do not forge baseline classes or hashes. Their preconditions and baseline counts are asserted before testing the veto. Separate adversarial contract tests intentionally alter class flags, validity metadata, or hash values and identify those changes as protocol tests. All frame arrays remain in RAM; no frame/image files are written.

All primary fixtures use 160 by 96 pixels, tile side 16, and 60 actual Morton-ordered tiles. The scroll is a deterministic world sampled at `x+4*t`, so physical displacement is `(-4,0)` with newly exposed right-edge pixels. Its periodic interior deliberately aliases every second frame; world content beyond x=160 breaks exact return at exposed pixels. Canvas uses the actual `canvas_anim` renderer, seed 1, and a full-image canvas. The test verifies its bytes against `texture(160,96,1 ^ (i*7919))` and verifies 48 distinct eager hashes per tile across 48 frames.

### Concrete baseline failures

| Witness | Pinned facts before the veto | Reference consequence |
|---|---|---|
| Scroll frame 4, Morton rank 49, tile `(9,0)` | All 60 baseline tiles are FLICKER; canonical GM is `(-4,0)` while published summary is `None`. Candidate `r1=504`, `r2=8`; translated SAD 0 over 192/256 pixels. All 54 full shifted windows have SAD 0. | Trusted nonreturn, strong full-window consensus, and inward support yield `VETO_F6`; candidate becomes CHANGED. |
| Same scroll candidate, exact hashes at frames 2,3,4 | `11770060860217519983`, `3944673549284345041`, `14867038441355757329`, all distinct eager hashes. | It is not protected as an exact A-B-A return. |
| Canvas frame 2, Morton rank 4 | Real descriptor candidate has `r1=75`, `r2=26`; lag-two pixel SAD is 2457, above the exit ceiling `16*2457 > 256*28`. | Confirmed pixel nonreturn is consistent with P2; the full fixture additionally checks both adjacent inequalities and trusted distinct hashes. |
| Lazy uniform RGB 64 to RGB 65, 32 by 32 pixels | Four JITTER outputs retain old emitted hashes while eager hashes differ; raw ClassState histories remain zero. | Output or ClassState hashes without provenance are rejected as P1 evidence. |

The following aggregate counts are **VM OBSERVED** in the targeted fixture tests, not host quick-48 results. Columns follow native class order. “After” changes only the reference mask's class values to CHANGED.

| Fixture/output | UNCHANGED | JITTER | CHANGED | MOVED | FLICKER | FROZEN | DRIFT |
|---|---:|---:|---:|---:|---:|---:|---:|
| Scroll, 48 frames, baseline | 60 | 0 | 204 | 1236 | 1380 | 0 | 0 |
| Scroll, after reference | 60 | 0 | 576 | 1236 | 1008 | 0 | 0 |
| Canvas, 48 frames, baseline | 60 | 1178 | 1306 | 0 | 169 | 13 | 154 |
| Canvas, after reference | 60 | 1178 | 1475 | 0 | 0 | 13 | 154 |
| Held caret, 80 frames, baseline and after veto | 4791 | 0 | 9 | 0 | 0 | 0 | 0 |

Scroll has 372 vetoes: 78 F6 reasons and 294 P2 reasons, with F6 precedence for overlaps. All 1008 exact-return baseline candidates remain protected. This deliberate residual demonstrates that P1 preservation cannot resolve every physically moving periodic texture. All 169 canvas baseline FLICKER candidates are vetoed by P2, and every other canvas class remains byte-for-byte unchanged outside the veto mask. These local numbers do not establish removal of the host's 3426/1855 populations.

The exact-return caret control renders real scene frames `(0,8,0)` as successive fixture inputs. Its baseline class-count vectors are `[60,0,0,0,0,0,0]`, `[59,0,1,0,0,0,0]`, and `[59,0,0,0,1,0,0]`. At Morton rank 15, the third input exactly returns to the first hash. The sole baseline FLICKER is retained with `EXACT_HASH_RETURN`.

### Independent F6 and conservative negative controls

The 78 primary-scroll F6 cases also satisfy P2, so their reason codes alone do not establish F6's independent contribution. A second, five-frame real scroll uses the same world formula but changes its exposed perturbation to `(x//4)%3`. At frame 4, all 60 baseline tiles are FLICKER; rank 49 has three distinct eager hashes but lag-two luma SAD of 320, hence `16*SAD=256*20 <= 256*28`. P2 abstains. With no GM, that candidate remains FLICKER with `PIXEL_RETURN_OR_SMALL_CHANGE`; with validated `(-4,0)`, its zero-SAD 192-pixel overlap and inward full neighbor independently authorize F6 demotion to CHANGED. Invalidating the inward neighbor's history retains it with `NO_INWARD_SUPPORT`. Changing one RGB level at RAM pixel `[0,145,:]` in frame 4 retains the baseline candidate and vector but changes clipped SAD to 1; F6 then retains the candidate with `PARTIAL_WINDOW`.

A three-frame, 12-pixel-step version provides a genuine insufficient-overlap control. Its final baseline counts are `[0,0,0,6,54,0,0]`; rank 49 has distinct hashes, canonical GM `(-12,0)`, and an exact shifted match over only 64/256 pixels. The reference vetoes zero entries, retains those same class counts, and reports `PARTIAL_WINDOW` for that candidate. Two trusted full support tiles are also insufficient to establish GM consensus: the separate attestation test retains the candidate with `NO_GM_CONSENSUS`.

The RGB sequence of uniform 64, uniform 200, and `(65,64,63)` has three distinct eager hash states but returns exactly in YCoCg-R luma. Its four final baseline FLICKER tiles all remain FLICKER; hash inequality alone authorizes no veto. These tests complement missing-history, placeholder-hash, padded-tile, and inconsistent-GM abstention. They are VM unit fixtures, with no host attribution.

## 8. Tracker preservation and arbitration order

The held caret fixture uses the real `caret_blink` scene at 32 fps, period 16, for 80 frames. Each state is held for eight frames. Baseline S2 emits nine CHANGED transitions and no FLICKER; a “zero lost baseline caret FLICKER” result alone would therefore be vacuous. The separate three-frame exact-return control above supplies a nonzero baseline preservation check.

The actual `fl_blinkprior.FlickerTracker` requires a valid minimum half-run and compatible two-state runs (`eval/v2x/fl_blinkprior.py:61-69,139-217`). It emits only when the hash changes and `in_flicker` is true (`228-237`). Feeding the held fixture's eager hashes yields exactly seven declarations, all at Morton rank 15: frames **24, 32, 40, 48, 56, 64, 72**, each with half-period 8. Every declaration is preserved when applied after the baseline veto using actual `postpass.apply_claims`.

At frame 24 current content is B, matching frame 8; frame 22 is A, matching frame 0. Thus current and lag-two hashes differ at a genuine tracker emission. Every listed declaration is a baseline CHANGED transition with `words_eq=False`, so the baseline-only veto does not select it. **Never impose literal current-equals-lag-two at tracker emission.** The tracker recognizes a longer cycle; baseline rule 3 recognizes only a lag-two descriptor return.

Claim-only FL cannot demote an existing baseline FLICKER. Generic PostPass builds positive `states == 4` claims (`eval/v2eval/postpass.py:61-65,134-138`). The chain compares each variant to baseline and reconstructs FL differences as class-4 claims (`eval/v2eval/chain.py:133-137`); there is no negative veto output in that interface.

The proposed order has a direct proof. Let `B` be frozen baseline, `V` its restricted veto, and `T` the later tracker mask. Set `B1.cls[V]=CHANGED` in a copy, then apply the positive FL claim to `B1`. For every tile in `T`, `apply_claims` assigns class 4 (`eval/v2eval/postpass.py:28-35`), independent of `V`. The test exhausts all four Boolean combinations of `V` and `T`, including deliberate overlap, and checks input immutability. Do not rerun the veto on the composed result.

Current `chain.run_chain` is different: its named orders mask out overlap for the second claim, so the first claim wins (`eval/v2eval/chain.py:139-144`). `gm_then_fl` can suppress an overlapping tracker declaration. This reference does not modify that behavior or claim the chain is already wired correctly. Production integration must explicitly adopt and test the intended precedence; the local caret test applies FL after the veto and preserves all seven emissions.

## 9. Validation scope and remaining work

The new test module covers real baseline witnesses and counts, exact A-B-A preservation, every held-caret emission, all other class values, `words_eq=True`, absent/unattested history, discontinuous IDs, wrong Morton order, per-tile invalidity, zero and nonzero placeholders, missing/malformed luma, inconsistent GM, padded tiles, clipped-window counts, read-only inputs, repeatability, and malformed current/threshold types. The analysis document itself must exist and contain nonwhitespace text.

Preservation is exact for the tested byte-return candidates and all seven declared caret events. A 64-bit hash collision can conservatively retain a false FLICKER; it is not a proof of object identity. Conversely, P2 can remove a genuine semantic blink when a changing background shares its tile and prevents both RGB and luma return. F6 can be misled by observationally ambiguous periodic motion if the extra changing pixels also defeat P1. Neither risk has a host rate established here. The strong overlap/adjacency guards and later tracker declarations reduce specific risks; only event-level host evaluation can establish preservation of the recorded 81/90 detections.

Run the exact task gate from the repository root:

```sh
python3 -m unittest tests.test_jq1_a2_s2_flicker_veto && test -s docs/spec/jq1_a2_s2_flicker_veto.md
```

The companion report records the actual gate output, including any observed failures and reruns. The fixtures use only repository Python, stdlib, and NumPy; no network, GPU, display, external executable, or saved frame is part of their execution. Any execution timing is a **VM** runtime, never a host **MEASURED** latency or benchmark. Neither unit-test success nor a collection pull request certifies orchestrator acceptance.

**AUSENTE / follow-up:** the host manifest and event-level residuals; host recall identities; full production/router adapter; fresh-hash ownership and persistence in production; previous published GM history; D1/D2/D3 implementation; wide-displacement support; source-valid treatment for padded tiles; host false-positive/class-migration accounting; latency/memory budgets; full-pipeline integration; and acceptance results.

Host evaluation must retain exact scene/frame/tile keys, count newly lost caret detections, distinguish CHANGED demotion from validated MOVED recovery, and preserve unsupported cases in denominators. Missing motion evidence is not proof of blink, and pixel nonreturn is not proof that no semantic blink occurred. No hypothesis here turns the synthetic fixture counts into the requested host result.
