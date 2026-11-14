# FLICKER versus large-displacement motion

## 1. Decision, scope, and evidence status

**Proposal:** let independently validated translation compete with provisional A-B-A before final FLICKER assignment, and test an extended, representable search range. Evaluate a strict neighbourhood check and a two-frame consistency check as alternative safeguards for the newly admitted motion evidence. Reordering alone cannot recover a displacement that was never searched: the current eligibility mask excludes provisional FLICKER, while both global and island searches inherit the same bounded projection search (`visioncore/change.py:179-187`, `visioncore/change.py:294-300`, `visioncore/change.py:343-345`, `visioncore/change.py:378-411`).

This is an engine-side analysis and fix proposal, not an implementation or an acceptance certificate. Repository: `oguirado/vision-engine`. All source citations below were read through the GitHub connector with explicit ref `bridge/integration`; the resolved commit was `05f98d8426c99470ee7af941ae3020c0f0d74d95`. Each of the six bound source blobs was also read at that explicit commit and matched the branch-read blob. Line numbers are one-based in those actual file contents, not copied from the older S2 specification.

Evidence labels:

- **CITED**: a source statement or number, with its path and line. Campaign counts are supplied evidence, not recomputed results.
- **DERIVED / COMPUTED**: mathematics, operation bounds, or byte checks performed from the cited source; these are not latency measurements.
- **PROPOSED**: a prospective predicate or host experiment, not existing behavior or a measured outcome. Numeric operands in predicates come from cited code or explicitly derived arithmetic.
- **AUSENTE**: behavior, configuration, measurement, or evidence that these reads do not establish. No engine timing was collected. Any future timing in a virtual machine must be labelled **VM**, never **MEASURED**.

### Campaign motivation and source discrepancies

**CITED campaign diagnosis:** V2EVAL2 quick-48 DEV had 13,376 false FLICKER tile-frames; 98.55 percent were attributed to baseline; `scroll_h` accounted for 11,327, including 8,095 with truth MOVED reported removed by a claim-only GM pass; `canvas_anim` accounted for 1,855. Source: internal task card (not published). The raw run, denominator definitions, per-frame outputs, and the exact scope of “all removed” beyond the stated MOVED group are **AUSENTE**. These numbers do not establish the mechanism of every individual false positive.

The task card's recorded source commit is `2be17e36f526005c4b08b08c3ee1b222c2d33805`, also present in task metadata; this differs from the branch resolved during this session (internal task card (not published), internal task card (not published)). The user's explicit instruction to resolve and read `bridge/integration` governs this analysis.

The S2 specification's source table cites an older `change.py` blob and 455 lines, and its classification section uses the older line offsets (`docs/spec/S2_CLASSIFY_SPEC.md:7-12`, `docs/spec/S2_CLASSIFY_SPEC.md:385-429`). **COMPUTED current source:** 23,772 bytes, 502 lines, Git blob `3f3572d91f06b59ab5744a47c37f3c0acd62c4e0`, SHA-256 `2659416b081a9a8014898b7aa554c8d37a2cc8ae513ac34c73e29710a146fdec`. The historic table is contextual evidence; its quoted offsets and parameter defaults are not assumed current.

## 2. What rule 3 actually observes

### Residuals, confidence masks, and hysteresis

For effective parameters `p`, let `T0 = p["T0_Q4"]`, `B = p["HYST_BAND_PERMILLE"]`. The exact thresholds are `t_enter = T0*(1000+B)//1000` and `t_exit = T0*(1000-B)//1000`, with floor division; `classify_tiles` selects the effective table and calls this helper (`visioncore/change.py:42-51`, `visioncore/change.py:313-315`). The actual campaign parameter table and overrides are **AUSENTE**; the historical defaults listed in `docs/spec/S2_CLASSIFY_SPEC.md:113-124` are **CITED historical values**, not proof of runtime settings.

The q4 residual is the absolute luma-mean difference, plus eight times the two absolute half-chroma differences, plus the absolute sigma difference, plus eight times the nonnegative excess masked Hamming distance over `H_DEAD`. The weights and integer widening are explicit (`visioncore/change.py:28-30`, `visioncore/change.py:54-61`). The Hamming input is `(a.sig64 ^ b.sig64) & mask`; the mask is the union of the current and relevant prior signature-confidence masks, not an exact-pixel test (`visioncore/change.py:59-61`, `visioncore/change.py:67-92`, `visioncore/change.py:325-326`, `visioncore/change.py:344`).

| Quantity | Exact present behavior | Current source |
|---|---|---|
| `r1` | Residual against `state.prev1` with `cf \| state.cf1`; force it to zero for `words_eq` tiles. | `visioncore/change.py:325-327` |
| `active` | A previously hot tile needs `r1 > t_exit`; a cold tile needs `r1 > t_enter`; both require `~words_eq`. | `visioncore/change.py:328-329` |
| `r2` | Residual against `state.prev2` with `cf \| state.cf2`. | `visioncore/change.py:344` |
| Provisional `flicker` | `active & (r2 <= t_exit) & (r1 > t_enter)`. The one-step test is strict even for a hot tile; the two-step test is inclusive. | `visioncore/change.py:343-345` |
| History | `hot` records activity; descriptor and confidence histories advance by one frame at the end. | `visioncore/change.py:469-480` |

Exact equality with the preceding padded RGB tile determines `words_eq`; perceptual descriptor fields are computed separately. The descriptor includes tile averages, sigma, gradients, edge density, and a signature of sub-block means relative to the tile mean (`visioncore/desc.py:81-98`, `visioncore/desc.py:148-200`). Rule 3's residual uses only the fields in its formula; neither exact hash nor gradient/edge fields enter that formula (`visioncore/change.py:54-61`). Thus A-B-A means **descriptor return at a fixed destination tile**, not “the same object blinked.”

### How horizontal translation produces apparent A-B-A

**DERIVED counterexample:** let a translated texture have descriptor phase period `P` along x and a per-frame displacement `d` such that `2*d` returns the descriptor phase but `d` does not. At a fixed destination window the descriptors can be A, B, A. If `R(A,B) > t_enter`, `R(A,A) <= t_exit`, and the adjacent frames are not byte-equal, the tile satisfies rule 3. This follows directly from its residual and mask tests; it does not require flickering pixels or a hash collision (`visioncore/change.py:54-61`, `visioncore/change.py:325-345`). Approximate descriptor repetition also suffices because the return test permits residual up to `t_exit`.

One-frame repetition alone is not this failure: if the destination bytes repeat every frame, `words_eq` makes `active` false. Nor does any arbitrary displacement necessarily alias; the texture, descriptor aggregation, and thresholds must meet the stated inequalities (`visioncore/desc.py:84-85`, `visioncore/change.py:327-329`, `visioncore/change.py:345`).

The scene graph assigns `scroll_h` a negative horizontal velocity with magnitude `max(1, catalogue_value*s//16)`; `motion()` returns the scene's scroll vector. The actual DEV velocity catalogue is imported from another file, so its values are **AUSENTE** in the bound sources (`eval/gui_synth/scenes.py:13-14`, `eval/gui_synth/scenes.py:24-30`, `eval/gui_synth/scenes.py:40`, `eval/gui_synth/scenes.py:94-104`). The specific texture period and per-tile `r1/r2` distributions of the cited run are also **AUSENTE**. For `canvas_anim`, the graph names the kind but its motion helper falls through to zero; that does not prove the renderer is stationary or that every canvas false positive has the scroll mechanism (`eval/gui_synth/scenes.py:10-11`, `eval/gui_synth/scenes.py:61-65`, `eval/gui_synth/scenes.py:94-104`).

## 3. Why ordinary motion offers and later clearing miss it

### Offer eligibility and publication

Motion evaluation requires current and preceding luma planes, a grid, and some active tile. SAD values/counts are cached per vector for the current frame, with the cache helper requesting all tile windows (`visioncore/change.py:347-364`). A full-tile offer requires membership, all `s*s` source pixels in bounds, `den*sad_c < sad_0`, `16*sad_c <= s*s*t_exit`, and `sad_c < best`. It then mutates the winner arrays; the returned mask means “new winner accepted in this call,” not all tiles that the vector can explain (`visioncore/change.py:369-376`).

The base membership is `active & ~flicker`. The initial global offer sees that membership, and publication requires its explained count times 1,000,000 to reach `GLOBAL_SHARE_PPM * active_count` (`visioncore/change.py:378-385`). Consequently, provisional FLICKER contributes to the denominator but cannot contribute to the initial numerator. **DERIVED necessary condition:** if `A` is the active count, `F` the provisional FLICKER count, and `q` the share threshold, publication is impossible whenever `(A-F)*1000000 < q*A`, even if every eligible tile matches. This follows from `expl_g_count <= A-F`; it does not establish that the condition held in the cited run.

Island masks are also built from this original `cand_base`, not a recomputed mask after clearing. The largest islands are tried first up to the cited `ISLAND_MAX = 16`; vectors are offered first to their own eligible islands and then pooled over all eligible tiles, still excluding provisional FLICKER (`visioncore/change.py:36`, `visioncore/change.py:396-411`). Recovered global FLICKER tiles are therefore not newly enrolled in these island memberships.

### What gets cleared, and when

Only after publication does the code assign `summary["global_mv"]` and `published`, then offer the global vector to FLICKER candidates. Accepted full windows are removed from `flicker` and retain the motion winner. Partial windows use a separate `mexp` predicate requiring at least half the source pixels, strict normalized SAD improvement, and the residual ceiling (`visioncore/change.py:383-390`). A failed publication withdraws the global winners and flags (`visioncore/change.py:391-395`).

Final ordering assigns CHANGED to active tiles, then MOVED to winners, then FLICKER to the remaining flicker mask. Full-window clearing can therefore yield MOVED; `mexp` alone never creates a winner, so a cleared active exposed-edge tile remains CHANGED. The motion-history bit does include `mexp & active` (`visioncore/change.py:412-418`, `visioncore/change.py:471`). This distinction agrees with the scene graph's declared whole-destination-tile rule for MOVED versus exposed-edge CHANGED (`eval/gui_synth/scenes.py:15`).

### Independent range and representation barriers

The global search computes integer row/column projections and searches each axis only through `[-min(s,64), +min(s,64)]`. Each axis uses overlap-normalized SAD, deterministic `(cost, abs(d), d)` ordering, and a strict factor-three improvement against zero shift. Islands call the same bounded global function on a cropped bounding box (`visioncore/change.py:33`, `visioncore/change.py:157-187`, `visioncore/change.py:294-300`).

**DERIVED:** a true displacement beyond that range cannot be returned as itself. However, the comment at `visioncore/change.py:181` overstates the result by suggesting all such motion returns zero: the actual loop may select a smaller nonzero alias. A candidate still has to survive the pixel SAD and publication checks before any FLICKER clearing happens (`visioncore/change.py:161-176`, `visioncore/change.py:369-390`). Thus missing or incorrect candidates, exclusion from the publication numerator, and insufficient valid overlap are distinct reasons to probe; no one reason is established for every `scroll_h` failure.

The claim-only comparison is structurally broader: its estimator receives `max(grid.W, grid.H)-1`, and its consensus mask is not restricted by baseline class. It stores vectors in widened arrays and applies a GM claim directly to the selected baseline tiles (`eval/v2eval/postpass.py:22-35`, `eval/v2eval/postpass.py:85-92`, `eval/v2eval/postpass.py:134-138`). The estimator and consensus implementations themselves are **AUSENTE** from the bound files; this call site does not certify their accuracy. Native motion fields are documented as int8, while post-pass arrays are int64 (`eval/v2eval/postpass.py:22-25`). Large-vector wire support beyond signed-byte range is **AUSENTE**, and widening a detector must not silently narrow its outputs.

## 4. Three integer-only discriminants to evaluate

These are prospective engine changes. This document contains no code patch. Retain current behavior when required luma/history is unavailable; never interpret absence of translation evidence as proof of physical blinking. No variant may consult scene names, truth labels, or campaign counts at classification time.

Cost notation is **DERIVED**, not timed: `N` tiles, side `s`, `A=N*s*s=H*W` padded pixels, `F` provisional FLICKER tiles, `L` islands, `I=min(L,ISLAND_MAX)`, and `K <= I+2` distinct cached vectors including zero and global. The latter bound follows from the single global candidate and at most one vector per selected island (`visioncore/change.py:349-354`, `visioncore/change.py:379`, `visioncore/change.py:401-411`). A new full-grid cached SAD is `O(A)` work and `O(N)` retained sums/counts, not `O(F)`; the helper batches pixel windows, and membership is applied only after it runs (`visioncore/change.py:205-259`, `visioncore/change.py:351-372`). Exact wall time and allocation peaks are **AUSENTE**.

### D1. Arbitrate validated translations before final FLICKER

**PROPOSED predicate and flow.** Preserve line 345's raw A-B-A mask as `F_raw`. Compute a pure translation-explanation mask separately from the mutating `offer` winner mask. Evaluate the global candidate over all active tiles for the publication share, retain the same integer validity/ratio/ceiling gates, and include active A-B-A tiles in island discovery and offers. After publication/withdrawal and ordered global/island winner selection, remove from `F_raw` only surviving validated motion winners and the existing published-vector `mexp` cases. Keep final class assignment after this arbitration.

**Range arm of this proposal.** Compare unchanged range against a symmetric representable range `D=min(2**7-1, max(H,W)-1)`, derived from signed int8's positive bound and the image extent. This removes tile size as the search ceiling while admitting only vectors that fit the native output. Both full-frame and island projections must receive the intended bound; cropping must retain sufficient overlap for the candidate. This covers only displacements up to that bound. A displacement outside representable range remains unsupported: preservation of the original classification is the proposed conservative fallback, and complete engine support is **AUSENTE**, not supplied by a cast. The relevant current constraints are the tile-bound search, cropped island call, and int8 motion interface (`visioncore/change.py:179-187`, `visioncore/change.py:294-300`, `visioncore/change.py:413-415`, `eval/v2eval/postpass.py:22-25`).

**Exact edit sites:** `visioncore/change.py:343-345` for provisional naming; `visioncore/change.py:369-386` for pure explanation, membership, publication, and saved masks; `visioncore/change.py:391-411` for withdrawal and active-island eligibility; `visioncore/change.py:412-418` for final arbitration. The range arm touches `visioncore/change.py:179-187` and `visioncore/change.py:294-300`, with the constants at `visioncore/change.py:33` reviewed for the new bound. Preserve the deterministic vector order and strict winning-cost tie behavior at `visioncore/change.py:347-348`, `visioncore/change.py:372`, `visioncore/change.py:401-411`.

**Critical trap:** changing only `cand_base` to `active` is insufficient. The first offer would already set `best` for raw FLICKER; re-offering the same vector at line 386 then fails `sad_c < best` on an equal cost, returning false, and line 418 restores FLICKER. Use saved pure explanation/surviving winner masks, not the return value of a repeated offer (`visioncore/change.py:372-386`, `visioncore/change.py:416-418`).

**Cost per frame, DERIVED.** For unchanged vectors, expanded membership and saved masks add `O(K*N)` boolean work and `O(N)` masks; cached SAD pixels are reused. Changed island membership can change both bounding boxes and candidates. Including range expansion, the conservative total is `O(A + D*(W+H) + N + L*log(L) + I*N + sum_i[A_i + D*(W_i+H_i)] + K*A + K*N)`, where island crop sizes are `A_i=W_i*H_i`. This follows from projection loops, component labeling/ranking, full-grid membership scans, and cached SADs (`visioncore/change.py:157-187`, `visioncore/change.py:273-300`, `visioncore/change.py:349-411`). Peak retained SAD arrays are `O(K*N)`; larger candidate sets may increase real work despite the same asymptotic bound. Host latency and the practical accepted bound are **AUSENTE**.

**Caret risk.** A small blinking feature on repetitive or moving background can still satisfy a luma SAD ceiling after translation; admitting it earlier may lower true FLICKER recall. Strict improvement excludes flat zero-SAD ties, but does not prove semantic motion (`visioncore/change.py:369-372`). Global publication also affects later FROZEN/DRIFT ambiguity handling, and changed labels affect motion history, so inspect those migrations as well (`visioncore/change.py:420-445`, `visioncore/change.py:469-475`). Quantitative losses are **AUSENTE**.

**Falsifiable host arm.** Run D1 with original range and with the representable extended range on the identical quick-48 manifest. The membership-only arm must reduce failures tagged “qualifying candidate excluded / publication starved” when such cases exist; it must not be credited with recovering an unavailable displacement. The range arm must reduce truth-MOVED FLICKER for previously out-of-search-range, now representable velocities, with correct vectors. Reject its benefit claim if false FLICKER merely migrates to wrong MOVED or true caret FLICKER decreases.

### D2. Require independent neighbourhood translation evidence

**PROPOSED safeguard on D1's newly cleared A-B-A tiles.** For the candidate vector, require the centre and its immediate eight neighbours to have full in-bounds source windows and zero translated pixel SAD; require positive zero-shift SAD at the centre and at least one neighbour. The neighbourhood is the same immediate grid stencil used in component connectivity; its nine-window size is **DERIVED** from the three-by-three iteration in `visioncore/change.py:286-287`. Exact-match and strict-improvement tests introduce no empirical threshold. Deduplicate overlapping requested windows. Only suppress FLICKER when this predicate holds; missing border neighbours or insufficient informative support preserve the original FLICKER decision. Keep D1's already-established baseline `mexp` behavior separate.

This directly asks whether the A-B-A tile belongs to a consistently translating neighbourhood. It does not estimate a new vector: it needs the preflight candidates and range treatment in D1. A repeated texture with genuinely indistinguishable translations remains ambiguous; semantic identifiability is **AUSENTE**.

**Exact edit sites:** preserve `visioncore/change.py:343-345` as raw FLICKER; insert the new guard into the pure explanation/arbitration developed at `visioncore/change.py:369-386` and `visioncore/change.py:409-418`. Reuse the coordinate/window-validity machinery at `visioncore/change.py:194-202` and `visioncore/change.py:205-270`. The strict source-window check must use actual pixel counts, not just a nearby tile's descriptor. Existing motion offers use pixel SAD and full-window validity at `visioncore/change.py:370-372`.

**Cost per frame, DERIVED.** With D1's full-grid SAD cache, neighbouring SAD/count lookup and support reduction cost `O(K*F)` and `O(N)` indexing/mask storage, with a fixed nine-window stencil and no new pixel SAD for cached vectors. A candidate-restricted alternative would cost `O(K*U*s*s)` for the deduplicated union of `U <= min(N,9*F)` windows; that optimization is proposed, not current `sad_for` behavior (`visioncore/change.py:205-259`, `visioncore/change.py:351-354`). Vector estimation cost remains D1's. Runtime is **AUSENTE**.

**Caret risk and coverage limit.** A stationary caret toggle generally has no translated exact-match neighbourhood, and a blink over moving content leaves a centre mismatch unless another image region happens to reproduce it. This is a hypothesis to test, not a recall guarantee. Exact equality deliberately forgoes recovery under noise, nonrigid animation, occlusion, and insufficient border context. No label should be changed solely because a neighbour moved.

**Falsifiable host arm.** Compare D1+D2 with D1 on the same tiles and retain the guard's pass/fail reason. Require zero newly lost true caret FLICKER tile-frames relative to baseline, while counting how many D1 truth-MOVED repairs it retains. If it loses a caret example or retains none of the targeted repairs, that tradeoff fails for this campaign. Analyse failures by centre mismatch, neighbour mismatch, no informative neighbour, and invalid overlap; do not merge these into an undifferentiated rejection count.

### D3. Confirm transport over two consecutive frames

**PROPOSED alternative safeguard on D1's newly cleared A-B-A tiles.** Require equal nonzero published vectors on the current and preceding transitions, a valid current one-step explanation using the original strict SAD improvement, and a full-window two-step match against luma at `t-2` shifted by `2*v`. Use `16*SAD_2v <= s*s*t_exit` for that two-step match; compute `2*v` in widened integers solely as an internal source offset. Its extra factor two is **DERIVED** by composing equal per-frame translations.

Do **not** require two-step SAD to improve strictly over unshifted `t-2`: in the exact A-B-A alias, both may be zero. Nonzero transport must be established by the one-step test and valid vector history; a two-frame return by itself is already the ambiguous signal (`visioncore/change.py:343-345`, `visioncore/change.py:369-386`). Test this guard only as an alternative to D2, not as a claim that a published-vector-only check solves the original missing-vector problem. Current full tiles explained by an already published vector are already cleared at `visioncore/change.py:386`.

**Exact edit sites and proposed state semantics:** initialize a second preceding luma reference and preceding published vector as unavailable beside `visioncore/change.py:132-135`; initialize the first-frame luma at `visioncore/change.py:490-500`; rotate luma and publication history at `visioncore/change.py:478-482`. Missing luma, a gap, changed grid, or a missing/different previous vector invalidates the temporal guard; cold-start A-B-A retains baseline handling. Apply the guard at D1's arbitration sites `visioncore/change.py:383-390` and `visioncore/change.py:412-418`, using counts from `visioncore/change.py:205-270` for the doubled shift. Island-only evidence without the required global history does not pass D3. Review state serialization at `visioncore/change.py:138-141`: current `digest_bytes` includes descriptor/confidence histories but not even `y_prev`; a new persistent temporal-state serialization contract is **AUSENTE** and must be specified before implementation acceptance.

**Cost per frame, DERIVED.** Retain one extra luma plane, `A*itemsize` bytes; on the cited int16 luma path this is `2*A` bytes (`visioncore/change.py:144-154`, `visioncore/desc.py:44-51`). An immutable rotating buffer can avoid a copy; ownership of supplied `y_cur` beyond its assignment is **AUSENTE** (`visioncore/change.py:481-482`). A copy-safe implementation adds `O(A)` copy work. The second-lag SAD is `O(F*s*s)` for requested candidate windows plus `O(F)` masks; using all current tile coordinates instead makes it `O(A)`. Vector-history comparison is constant work. Additional reset/digest costs depend on the unresolved state contract; latency is **AUSENTE**.

**Caret risk and coverage limit.** Requiring two consistent published motions should reject isolated blinking without transport, but repeated content can remain observationally equivalent. First transitions, acceleration, vector sign changes, and oscillatory/nonrigid canvas motion will fail the guard by design. Do not present this as a general canvas-animation solution.

**Falsifiable host arm.** Compare D1+D3 with D1, keeping current and previous vector availability and lag-two valid counts. It should retain constant-translation repairs with equal vectors, including exact lag-two zero-SAD ties, while rejecting unsupported promotions. A constant-motion repair lost solely because `SAD_2v == SAD_0lag2 == 0` falsifies the intended implementation. Report recovery loss at startup and vector changes separately from caret recall.

## 5. Host V2EVAL2 experiment and baseline truth-class probe

### Freeze the experiment before evaluating variants

**PROPOSED host protocol:** use the campaign's identical DEV quick-48 scene manifest, seeds, dimensions, frame count, effective parameters, and engine configuration. Record their identities and the exact host runner command before testing. The V2EVAL2 runner entry point, CLI flags, manifest, scoring implementation, and campaign configuration are **AUSENTE** from the six bound files; no executable command is invented here. The defined comparison interface available in these reads is `PostPass.update(tiles, rgb, frame_key=...)` returning variant arrays and claim masks (`eval/v2eval/postpass.py:56-62`, `eval/v2eval/postpass.py:133-139`).

Run baseline, baseline with GM-only claims, D1 with each range arm, D1+D2, and D1+D3 in separate engine states with identical RAM frames. Those are ablations of the three proposals, not additional discriminants. Evaluate each modified engine's raw S2 output before any post-pass as well as the control overlays. Keep frame and image arrays in RAM; persist neither frames nor images. Emit only compact counters and source/configuration identities through the host's authorized reporting route. This documentation task executes none of these engine modifications.

### Observe before mutating state, join truth outside the classifier

An in-memory engine observer should copy scalar/counter evidence around the initial rule-3 mask, publication decision, edge clearing, and final class assignment **before state histories advance** (`visioncore/change.py:325-345`, `visioncore/change.py:378-418`, `visioncore/change.py:468-482`). Capture `r1/r2` directly: `out["resid"]` is shifted/saturated display data and is not the q4 value (`visioncore/change.py:461-465`).

Join these observations to the harness's truth arrays outside `classify_tiles` and outside `PostPass`. The post-pass interface has no truth input, and its claims are computed independently of baseline labels except the explicit subtile eligibility (`eval/v2eval/postpass.py:1-7`, `eval/v2eval/postpass.py:56-65`, `eval/v2eval/postpass.py:85-96`). The actual truth-array layout and indexing adapter are **AUSENTE**. Verify its correspondence to frame/tile/Morton order before counting; post-pass GM masks explicitly use `ravel()[grid.flat]` (`eval/v2eval/postpass.py:89-90`).

The baseline counter is the integer sum of tiles with raw baseline `cls == C_FLICKER`, grouped by truth class. Its false-positive total excludes truth FLICKER; its recall denominator counts all truth FLICKER tile-frames. Classes are the seven enumerated values in `visioncore/change.py:39`. Emit:

| Counter group | Required keys or values |
|---|---|
| Baseline FLICKER by truth | `scene_id, kind, frame, truth_class, baseline_flicker_count`; aggregate across each scene and the exact manifest. |
| FLICKER recall | True positives and false negatives by scene, phase/transition, and supported warm-up interval; use counts and integer ratios, with zero-denominator results AUSENTE. |
| Candidate failures | Missing planes/grid; raw A-B-A; estimated vector; active/eligible/explained counts; publication pass/fail; full/partial/insufficient overlap; ratio/ceiling failure; final winner and whether flicker was cleared. |
| Range attribution | Estimated vector, scene graph/truth displacement where defined, and search bound. Truth is diagnostic data only, never fed back into the engine. |
| Class migration | Each baseline-FLICKER tile's final class, truth class, and predicted/truth vectors. Separate correct MOVED recovery from wrong-vector MOVED and exposed-edge CHANGED. |
| Cost | Candidate/vector counts, requested SAD windows, retained bytes, host elapsed samples and hardware/runtime identity. VM samples must be labelled VM; uncollected values AUSENTE. |

The scene graph supplies stable `scene_id`, `param_bucket`, and motion fields, but graph motion for canvas is not a substitute for per-tile oracle motion (`eval/gui_synth/scenes.py:56-69`, `eval/gui_synth/scenes.py:94-104`).

To attribute **baseline** versus added FLICKER, compare raw baseline arrays with each post-pass's returned arrays and claims. `arrays()` and `apply_claims()` copy arrays; GM can overwrite an existing baseline FLICKER, so these are paired observations rather than independently summed totals (`eval/v2eval/postpass.py:22-35`, `eval/v2eval/postpass.py:134-139`). Combined results require their own count: claims are applied GM, subtile, then flicker, so an overlapping final flicker claim can overwrite GM's MOVED result (`eval/v2eval/postpass.py:28-34`, `eval/v2eval/postpass.py:137-138`). The source docstring's partial-visible-edge GM claim differs from the graph's whole-tile MOVED truth rule; count truth-CHANGED edge regressions explicitly (`eval/v2eval/postpass.py:3-4`, `eval/gui_synth/scenes.py:15`).

### Protect genuine caret FLICKER and avoid a vacuous recall result

The separate tracker observes `xh64`, not rule 3's q4 residual, and `PostPass` constructs it with cited `max_half=64` (`eval/v2eval/postpass.py:44`, `eval/v2eval/postpass.py:63-65`). Its period logic uses compatible half-runs, clears on third states or stalls, and declares after the specified full-period evidence; it can label held phases FLICKER (`eval/v2proto_flicker.py:42-83`, `eval/v2proto_flicker.py:155-203`, `eval/v2proto_flicker.py:228-287`). Long-period tracker recall and baseline A-B-A recall are different measurements.

The graph derives a period from half-period milliseconds and fps; those catalogues are imported, so actual DEV coverage is **AUSENTE** (`eval/gui_synth/scenes.py:13-14`, `eval/gui_synth/scenes.py:43-50`). The meaning of the quick-48 label and its frame horizon are **AUSENTE**; do not interpret it as a 48-frame clip or assume enough frames for two complete tracker periods. Extend the caret control run in RAM to cover warm-up and both subsequent transitions and held phases for every configured half-period. The minimum four-half-run requirement is **CITED** from `eval/v2proto_flicker.py:66-83`, `eval/v2proto_flicker.py:253-285`; required frames for the actual manifest are **AUSENTE** until its values are supplied.

For each variant, report both absolute caret recall and the count of baseline true-positive caret tile-frames newly lost. If baseline has no true positives in a bucket, “no regressions” is insufficient: that bucket's baseline-preservation test is vacuous. Evaluate the tracker control on the same incoming hashes too, and compare both GM-only and combined overlays for each engine variant. Hash provenance matters: lazy S1 leaves hashes zero until a later fill, and clean/jitter tiles may copy prior hashes; whether the host supplies fully refreshed hashes is **AUSENTE** (`visioncore/desc.py:67-73`, `visioncore/desc.py:99-110`, `visioncore/desc.py:113-126`).

### Falsification and reporting contract

The host should reject a claimed fix when targeted truth-MOVED FLICKER does not decrease, recovered MOVED vectors are wrong, exposed-edge CHANGED becomes unsupported MOVED, or true caret recall declines. These are proposed decision criteria, not observed results. Report every remaining `scroll_h` and `canvas_anim` failure by the probe reasons, including unavailable/out-of-range vectors. Do not infer physical blink from an unsuccessful motion search. Report engine runtime failures and unsupported cases without dropping them from denominators.

Record accelerated/reference mode and cold/warm cache treatment; these choices and cache hits are explicit at `eval/v2eval/postpass.py:39-49`, `eval/v2eval/postpass.py:73-86`, and `eval/v2eval/postpass.py:124-128`. Do not sum the spatial stage timers as total wall time: luma conversion is charged to both passes (`eval/v2eval/postpass.py:68-71`, `eval/v2eval/postpass.py:93`, `eval/v2eval/postpass.py:122`).

Absolute numerical improvement, statistical uncertainty, target latency, host speed, and memory budget are **AUSENTE** until the host run. A pull request collecting this document is not acceptance of its proposals.

## 6. AUSENTE and work actually performed

**AUSENTE:** campaign raw outputs and reproducible quick-48 manifest; actual current parameter defaults/overrides and DEV velocity/period catalogue; per-tile alias periods; renderer and truth-adapter implementation; V2EVAL2 runner/CLI and scoring details; GM estimator/consensus implementation; exact host `xh64` provenance; wide-vector transport beyond int8; new temporal-state reset/serialization and caller-buffer ownership contracts; all engine experiment outputs, caret-recall deltas, and latency/memory measurements.

Performed in this builder session: connector reads of the task card, task metadata, and all six bound sources; explicit branch/commit blob comparisons; base64 decoding of `change.py`, verification of the Git blob identity and equality to the branch UTF-8 read, SHA-256 computation, and extraction of the requested exact lines. No frames or images were produced or written, no classifier implementation was changed, and no V2EVAL2 run or engine timing is claimed. The companion report records observed delivery checks; acceptance remains with the orchestrator.
