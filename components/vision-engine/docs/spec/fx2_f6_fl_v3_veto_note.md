# F6 eval implementation and deferred core proposals

## 1. Scope and source binding

This patch implements the owned eval-layer F6 changes: production `eval.v2x.fl_v3`, explicit baseline-FLICKER veto composition, and exclusion of true-positive FLICKER from false-positive probe aggregates. The native patches in this note are proposals only. **DEF-A2-02, DEF-A2-03, and DEF-A2-04 remain unrepaired by this pull request.**

The GitHub connector read the sources at explicit ref `bridge/integration`, resolved in this session to `f4cf3965493a052afbb7b704f94e531c98b84d30`. Exact bytes were base64-decoded and checked against returned Git blob SHAs. The task card's older dispatch source is `38672a73f5d2821a3972605b776a45398d61fe5f`; it is not substituted for the observed ref. Historic source bindings and measurements inside earlier analyses do not measure this patch.

The bound contracts are `docs/spec/jq1_a2_s2_flicker_veto.md`, the earlier motion/canvas analyses, the native implementations, and the task's explicit tests. The read-only `visioncore/pipeline.py` and dependency `visioncore/parallel.py` establish the real ordering: lazy S1, S2 history capture, then output-copy hash filling. Both proposed core paths remain byte-identical to the fetched source.

## 2. Production FL v3 and composition

`eval.v2x.fl_v3.veto_flicker` has the documented reference signature and explicit evidence envelope. The production module imports production helpers, never a test module. It returns fresh `VetoDecision(mask, reasons)` arrays with Boolean/uint8 dtypes in the original Morton order and stable reason values 0 through 18.

Only original baseline `cls == C_FLICKER` entries with `words_eq == False` are candidates. P1 preserves trusted exact current/lag-two hash returns. Other negative decisions require trusted owned history, three distinct hashes, no padding, and the baseline descriptor-return inequalities. P2 adds all three strict integer pixel-SAD inequalities; F6 adds canonical GM agreement, at least three exact full informative supporters meeting the configured share, at least half a clipped candidate window, zero translated SAD, strict normalized improvement, and an inward full supporter. Missing evidence abstains. Numeric zero is a legitimate digest, not a validity flag.

Inputs remain immutable. The helper never invents MOVED vectors, mutates ClassState, consults scene names/truth, or interprets emitted `resid` as Q4. Its evidence envelope is separate from the production parameter table and must not be passed to `pipeline.analyze`.

The module reexports the established `fl_blinkprior.FlickerTracker` and its live operation accounting. Minimum half-run remains seven when `max_half >= 7`, otherwise one; the third valid toggle can declare; adjacent half-runs may differ by two; half-period is `(r0+r1+1)//2`; held frames remain unclaimed; overlong runs and a third hash state retain their established resets. A genuine held-period tracker declaration is not tested for literal lag-two equality.

The explicit negative channel acts on a baseline copy before positive claims. It must not be reconstructed by treating every changed class as a positive FLICKER claim. Legacy positive-only callers stay supported. A later genuine tracker declaration may reassert FLICKER after baseline demotion, subject to the selected GM/FL chain precedence. Missing negative evidence means abstention.

The concrete opt-in surface is `apply_claims(..., flicker_veto=mask)`, `PostPass(..., baseline_flicker_veto=True)`, or `run_chain(..., baseline_flicker_veto=True)`. Selecting `fl=eval.v2x.fl_v3` in the chain enables its evidence adapter by default; explicit `False` keeps the positive-only mode. The copying overlay validates an actual Boolean mask of baseline shape and demotes only original class-4 entries to CHANGED. It does not change unrelated vectors or classes.

`BaselineFlickerVeto` owns fresh eager RGB descriptors, copied padded RGB, and luma snapshots rather than trusting S2's emitted hash labels. It retains two prior snapshots, freezes owned arrays, and resets on explicit nonconsecutive frame IDs. A new stream requires `reset()` or a new instance. `PostPass` also resets its tracker and spatial cache on such a gap. A chain creates fresh adapters for every scene. Current hash refresh changes the evidence provided to the tracker without changing the tracker's timing algorithm.

The adapter first evaluates P2 without asserting a GM vector. It separately proposes canonical GM for F6 and adds only independently validated `VETO_F6` bits. A failed internal GM proposal therefore does not cancel an independent P2 decision; the pure helper's conservative contract for explicitly supplied inconsistent GM remains unchanged. These adapters use the default core thresholds; the pure helper separately supports its documented override envelope.

The chain captures the worker's actual positive claim mask rather than reconstructing it from class differences. That preserves genuine tracker declarations on tiles already labeled FLICKER in the baseline. `compose_chain` retains the named first-wins precedence: GM wins overlap in `gm_then_fl`, FL wins overlap in `fl_then_gm`. The opt-in `PostPass(..., baseline_flicker_veto=True)` uses GM, then subtile, then FL precedence. Default PostPass retains its existing later-claim-wins overlay order (global, subtile, flicker), including when a caller supplies only an explicit `flicker_veto` mask to `update`. Both modes apply negative evidence before positive claims. Veto-enabled chain output explicitly reports the evidence adapter's cost outside worker opcode/deadline accounting and leaves affected opcode totals null; it does not present partial accounting as complete cost.

The false-FLICKER probe filters truth-class-4 observations before creating any record. The exclusion therefore applies to `records`, per-kind and summary `counts_by_truth_class`, `counts_by_cause_bucket`, and `total_false_flicker_tile_frames`. The FLICKER truth-class count remains zero in these false-positive populations, while the existing cause definitions and record fields remain intact.

## 3. Deferred pins and host integration

The following decorators remain unchanged in this eval-only patch:

| Defect | Deferred expected-failure test |
|---|---|
| DEF-A2-03, canvas descriptor return | `tests.test_jq1_a2_canvas_chroma_cancellation_flicker.TestCanvasChromaCancellationFlicker.test_def_a2_03_canvas_flicker_regression` |
| DEF-A2-04, true content history | `tests.test_jq1_a2_lazy_xh64_history_provenance.TestLazyXh64HistoryProvenance.test_def_a2_04_lazy_xh64_history_provenance_contract` |
| DEF-A2-02, publication/repeated offer | `tests.test_jq1_a2_s2_publication_starvation.TestS2PublicationStarvation.test_desired_publication_and_flicker_resolution` |

Only the owned `TestJQ1A2ChainClaimOnlyVetoGap.test_def_a2_01_desired_veto_contract` and `TestFalseFlickerProbeBuckets.test_true_positive_flicker_overcount_defect` pins were promoted after their desired assertions passed. Existing native characterization fixtures continue describing unchanged core behavior. They are not rewritten to imply that a documented patch has run.

The later Jules publication and provenance contracts remain **pending host integration**. Their exact task identifiers are not supplied by the bound sources and are not invented here. Their owners must activate them only after the host applies and verifies the core changes.

## 4. Proposed native changes and their limits

### DEF-A2-02: independent publication evidence

The proposed `explains` function is pure: it checks full-window validity, strict SAD-ratio improvement, and the residual ceiling without touching winner state. Publication counts that evidence across all active tiles, including provisional FLICKER, using the existing active denominator. A global candidate is offered once, after publication; an unpublished candidate never enters winner state or sets global flags.

Island discovery and deterministic offers use all active tiles. Final FLICKER clearing uses surviving full-window winners and the existing published-vector clipped `mexp`. This avoids re-offering an identical vector and mistaking failure to beat an already equal best SAD for absence of motion evidence. Strict `< best` tie order, full-window MOVED eligibility, clipped CHANGED handling, and the original estimator range remain intact.

This proposal does not expand displacement range, implement D2's nine-window guard, or add D3's previous-publication history. Changes to FROZEN, DRIFT, motion history, and semantic-caret recall require host verification.

### DEF-A2-03/04: eager hashes before history capture

The proposed S1 contract computes every tile's current padded-RGB hash before returning descriptors, including `lazy_xh=True` calls. Compatibility arguments remain accepted, but previous labels and nonzero-value heuristics never establish provenance. `ClassState.init`, `prev1`, and `prev2` already own descriptor copies, so their existing rotation now captures true hashes before S2 needs them.

The legacy `fill_xh64` refresh also computes every current hash, including JITTER, and remains valid for direct callers. Filling an independent output copy no longer has to repair history retroactively. The parallel path calls the same S1 function, and its whole-frame reuse follows exact RGB-byte equality. That reuse therefore inherits true content hashes.

The proposed rule-3 gate then intersects descriptor return with current/lag-two hash equality. **Apply these two diffs together.** A hash-equality check alone on existing lazy zero placeholders is invalid. A zero hash remains legal; a 64-bit collision can conservatively retain a false return. Strict byte identity would require retained RGB verification. Direct callers that manufacture descriptors must supply actual frame-specific hashes; the bare tile ABI cannot authenticate arbitrary externally supplied arrays. Restart histories at migration rather than mixing pre-change placeholders with new descriptors.

This is a conservative eager-before-capture implementation: it hashes every tile before S2 and again in the legacy output refresh. The extra work is explicit and **unmeasured**. Optimized reuse needs proven exact equality and a trusted-provenance contract. Host integration must update obsolete lazy-zero/JITTER-copy characterization assertions and stale lazy-path documentation only after verifying the new behavior. No such core or characterization update is made here.

The native exact-return proposal is deliberately stronger than the conservative eval P1/P2/F6 veto. It can reject a semantic blink sharing a tile with noise, antialias changes, or moving background. Genuine temporal tracker declarations remain a separate later positive channel. Event-level host recall must establish the tradeoff.

## 5. Exact proposed unified diffs — not applied

The following hunks are generated against the fetched core bytes. An independent parser verified clean application of all five `change.py` hunks and three `desc.py` hunks to in-memory copies of those bytes; the resulting Python was syntax-compiled without execution or any core-file write. This verifies source matching and syntax, not behavioral correctness. The host must review and apply both changes, rerun native gates, adjust only justified characterizations, and then consider promotion of the deferred pins.

```diff
--- a/visioncore/change.py
+++ b/visioncore/change.py
@@ -111,7 +111,11 @@
 
 
 class ClassState:
-    """Per-tile state (SPEC_CORE §3.2). Arrays are length N; None until init."""
+    """Per-tile state (SPEC_CORE §3.2). Arrays are length N; None until init.
+
+    Input xh64 must be the true current padded-RGB digest supplied by S1.
+    Histories own copies; placeholder or emitted-label hashes are not valid inputs.
+    """
 
     def __init__(self):
         self.n = 0
@@ -303,6 +307,7 @@
 def classify_tiles(state, tiles, words_eq, y_cur=None, grid=None, params=None):  # @anchor:vc.s2.classify
     """One step of S2. tiles: descriptors of frame t (cls ignored); words_eq: bool[N] exact pixel equality with t-1.
 
+    xh64: true current padded-RGB digest, computed before S2 even for JITTER.
     y_cur: optional padded luma plane (see luma_padded) enabling MOVED; grid: (s, gw, gh), needed with y_cur.
     Returns (tiles_out, state, summary); `state` is updated in place and returned.
     """
@@ -340,9 +345,11 @@
     cls = np.zeros(n, np.uint8)
     cls[~words_eq & ~active] = C_JITTER
 
-    # FLICKER (rule 3): A-B-A at descriptor level; no transition counter (A->B->B->C is not FLICKER)
+    # Provisional FLICKER: descriptor return plus true RGB-content hash return.
+    # S1 must complete hashes before ClassState captures them; zero is a valid digest.
     r2 = residual(t, state.prev2, p["H_DEAD"], cf | state.cf2)
     flicker = active & (r2 <= t_exit) & (r1 > t_enter)
+    flicker &= t["xh64"] == state.prev2["xh64"]
 
     # Reuse each vector's sums/counts only within this frame. Offers still execute
     # in sealed order: strict < best makes first-match tie breaking observable.
@@ -366,33 +373,32 @@
         mvy = np.zeros(n, np.int64)
         is_glob = np.zeros(n, bool)
 
+        def explains(vec, member):
+            # Pure evidence: publication must not depend on mutable winner state.
+            sad_c, count_c = sad_for(vec)
+            return member & (count_c == nn) & (den * sad_c < sad_0) & (16 * sad_c <= nn * t_exit)
+
         def offer(vec, member, glob):
-            sad_c, count_c = sad_for(vec)
-            ok_c = count_c == nn
-            expl = member & ok_c & (den * sad_c < sad_0) & (16 * sad_c <= nn * t_exit) & (sad_c < best)
+            sad_c, _ = sad_for(vec)
+            expl = explains(vec, member) & (sad_c < best)
             best[expl] = sad_c[expl]
             mvx[expl], mvy[expl] = vec
             is_glob[expl] = glob
             return expl
 
-        cand_base = active & ~flicker
+        cand_base = active  # provisional FLICKER participates in motion evidence
         gv = global_vector(y_cur, state.y_prev, s)
         published = None
         if gv != (0, 0):
-            expl_g = offer(gv, cand_base, True)
+            expl_g = explains(gv, cand_base)
             if int(expl_g.sum()) * 1000000 >= p["GLOBAL_SHARE_PPM"] * int(active.sum()):
                 summary["global_mv"] = [gv[0], gv[1]]
                 published = gv
-                flicker &= ~offer(gv, flicker, True)  # global motion explains an A-B-A tile -> MOVED (DIAG_X4)
+                offer(gv, cand_base, True)  # offer once, only after publication
                 sgc, nvc = sad_for(gv)
                 s0c = sad_0
                 mexp = (nvc < nn) & (2 * nvc >= nn) & (den * sgc * nn < s0c * nvc) & (16 * sgc <= nvc * t_exit)
-                flicker &= ~mexp  # edge tiles only, under offer()'s residual ceiling (ADV 2B); interior blinks stay
-            else:  # not published: global candidate withdrawn
-                best[:] = np.iinfo(np.int64).max
-                is_glob[:] = False
-                mvx[:] = 0
-                mvy[:] = 0
+            # An unpublished global candidate never enters winner state.
         amask = np.zeros((gh, gw), bool)
         amask[ty[cand_base], tx[cand_base]] = True
         lab, nl = _islands(amask)
@@ -413,6 +419,9 @@
         out["mv_dx"][moved] = mvx[moved]
         out["mv_dy"][moved] = mvy[moved]
         out["flags"][moved & is_glob] |= np.uint8(FLAG_GLOBAL_MV)
+    # Resolve only surviving full-window winners or published clipped evidence.
+    # A second identical offer would fail strict < best and is not a veto proof.
+    flicker &= ~(moved | mexp)
     cls[active] = C_CHANGED
     cls[moved] = C_MOVED
     cls[flicker] = C_FLICKER
--- a/visioncore/desc.py
+++ b/visioncore/desc.py
@@ -65,12 +65,12 @@
 
 
 def describe_padded(padded, grid, words_prev=None, prev_xh64=None, p=None, prev_tiles=None, y_plane=None, lazy_xh=False):  # @anchor:vc.s1.describe_padded
-    """padded = grid.pad(frame). words_prev = previous padded frame (None: every tile counts as differing).
-    prev_xh64 = previous tiles['xh64'] (None: hash every tile). prev_tiles = previous describe output: with
-    words_prev, byte-equal tiles copy their descriptor verbatim (exact: equal bytes give equal descriptors) and only
-    the other tiles are computed. y_plane = luma_of_padded(padded) when the caller already has it (else computed once
-    here, only if some tile needs computing). lazy_xh=True (SPEC_INT I2) leaves xh64 = 0: the caller runs
-    fill_xh64 after S2. cls/resid/mv fields are left 0 for S2. Tile-major: each tile is one contiguous block."""
+    """padded = grid.pad(frame); words_prev enables exact previous-frame equality.
+    prev_tiles = previous describe output: equal tiles reuse perceptual fields, and
+    only the other perceptual descriptors are computed. y_plane is optional cached luma.
+    Exact hashes are always computed from current padded RGB before S2 can snapshot history.
+    prev_xh64 and lazy_xh remain accepted for compatibility, but do not authorize deferred
+    or inherited hashes. cls/resid/mv are left zero for S2; tiles are contiguous blocks."""
     p = PARAMS if p is None else p
     s, gw, gh, N = grid.s, grid.gw, grid.gh, grid.N
     n = s * s
@@ -84,8 +84,6 @@
         eq = (padded == words_prev).reshape(gh, s, gw, s * 3).all(axis=(1, 3))
         words_eq = eq.reshape(-1)[grid.flat]
     reuse = prev_tiles is not None and words_prev is not None
-    if prev_xh64 is None and reuse:
-        prev_xh64 = prev_tiles["xh64"]
     idx = np.nonzero(~words_eq)[0] if reuse else np.arange(N)
     if reuse:
         keep = np.nonzero(words_eq)[0]
@@ -96,34 +94,20 @@
         if y_plane is None:
             y_plane = luma_of_padded(padded)
         _compute(out, idx, padded, grid, p, y_plane)
-    if lazy_xh:
-        return out, words_eq
-    xh = np.zeros(N, dtype=np.uint64)
-    if prev_xh64 is not None:
-        xh[words_eq] = np.asarray(prev_xh64, dtype=np.uint64)[words_eq]
-        hidx = np.nonzero(~words_eq)[0]
-    else:
-        hidx = np.arange(N)
-    if len(hidx):
-        xh[hidx] = _hash_tiles(padded, grid, hidx)
-    out["xh64"] = xh
+    # Correctness-first provenance: no placeholder or stale JITTER label may
+    # enter ClassState, including through the parallel lazy_xh caller.
+    out["xh64"] = _hash_tiles(padded, grid, np.arange(N))
     return out, words_eq
 
 
 def fill_xh64(out, padded, grid, prev_xh64=None, hash_all=False):  # @anchor:vc.s1.fill_xh64
-    """Lazy xh64 (SPEC_INT I2), run after S2 on the classified tiles `out` (mutated in place). BLAKE2b-64 only for
-    tiles with cls >= 2 (CLS_DIRTY_MIN) and, when hash_all (keyframe / no previous frame), for every tile; every other
-    tile copies prev_xh64 (exact for UNCHANGED; a label only for JITTER, which TIME's period test must mask)."""
-    cls = out["cls"]
-    if prev_xh64 is None:
-        hash_all = True
-        xh = np.zeros(grid.N, dtype=np.uint64)
-    else:
-        xh = np.array(prev_xh64, dtype=np.uint64)
-    idx = np.arange(grid.N) if hash_all else np.nonzero(cls >= layout.CLS_DIRTY_MIN)[0]
-    if len(idx):
-        xh[idx] = _hash_tiles(padded, grid, idx)
-    out["xh64"] = xh
+    """Refresh exact current RGB hashes on `out` in place, including JITTER.
+
+    Legacy prev_xh64/hash_all arguments remain accepted. Neither previous labels
+    nor numeric zero establish provenance. Full refresh supports direct callers;
+    pipeline output already carries these exact S1 hashes.
+    """
+    out["xh64"] = _hash_tiles(padded, grid, np.arange(grid.N))
     return out
 
 
```

## 6. Verification and separate host acceptance

The companion report records the full explicit unittest gate verbatim, its actual output, and remaining expected failures separately. Whole-repository discovery is excluded because unrelated suites write frames. Exercised fixtures are offline stdlib/NumPy arrays in RAM; no frame files or services are required.

The final required gate ran 182 tests in VM 7.992 seconds and exited 1: two errors, one existing skip, and three expected failures, with no assertion failures or unexpected successes. Both errors are in `tests.test_false_flicker_probe` because the connector cannot deliver the 3397684-byte compressed `eval/results/gui_synth/v1_baseline.json.gz` dependency: Contents returns empty base64 and the other available readers reject its non-UTF-8 bytes. The fixture was not fabricated or replaced. The full gate therefore remains blocked and must be rerun with the original dependency. The 14 focused chain/integration tests passed separately in VM 1.611 seconds.

Supplementary direct-production validation bound existing reference/tracker/adversarial test bodies in memory to `fl_v3` exports: 41 tests, VM 0.751 seconds, OK. This checks the production helper rather than merely rerunning a test-local reference. It is separate from the required gate and does not evaluate the proposed core diffs or host acceptance.

The supplied host goals remain unmeasured: at least 4225 removals from the 5281 residual tile-frames (residual at most 1056), canvas residual at most 100, and preservation of the identities of the previously correct 81 caret detections out of 90 opportunities. These are inherited targets from the bound specification, not results of this patch. The original campaign manifest, event identities, host latency, and host memory results are absent from this run.

Host evaluation must retain exact scene/frame/tile identities, newly lost caret detections, class migrations, and unsupported cases in denominators. Distinguish CHANGED demotions from correct MOVED recovery. Exact periodic scroll can remain protected by P1; P2 may reject a blink over a changing background; F6 depends on genuine translation and trusted history. Local fixtures do not establish rates for these risks.

A collection PR does not certify acceptance of the eval code, deferred native proposals, or corpus performance.
