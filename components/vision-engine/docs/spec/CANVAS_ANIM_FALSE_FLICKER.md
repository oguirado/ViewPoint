# Canvas animation falsely classified as FLICKER

## 1. Finding and evidence status

**The code admits a perceptual return without an RGB return.** Rule 3 compares the current tile with the tile two frames earlier through a residual that omits the RGB hash; a sufficiently small lag-two residual and a sufficiently large lag-one residual create a FLICKER candidate (`visioncore/change.py:54-61,325-345`). The canvas generator changes its seed every frame while retaining coordinate bands and substantial glyph structure (`eval/gui_synth/raster.py:19-23,33-54,129-131`). This is a plausible mechanism for the reported error, with the probability and attribution limits below.

**CITED — operator-transcribed campaign observation:** the task card reports **1855** DEV `canvas_anim` tile-frames predicted as FLICKER while truth is CHANGED, attributing that number to the campaign false FLICKER diagnosis (internal task card (not published)). The original diagnosis was **AUSENTE in the inspected material**. Its denominator, campaign baseline commit, effective profile and frame/tile breakdown were not established. This investigation does not claim to reproduce the count or prove that every reported tile followed the same path.

### Source binding

All implementation citations below refer to files read through the GitHub connector with explicit ref `bridge/integration` in `oguirado/vision-engine`. That ref was resolved to commit `05f98d8426c99470ee7af941ae3020c0f0d74d95` before source reads; the task files and all six bound sources were subsequently fetched with that explicit commit and checked for identical contents and blob identifiers.

The task card records an older source commit, `2a07acb0fbf4940f784037853cb9b696e1b4bdad` (internal task card (not published); internal task card (not published)). The current user instruction to resolve and use `bridge/integration` governs this work; the older identifier is not presented as the observed source.

The S1 specification corroborates the descriptor formulas and hash modes (`docs/spec/S1_DESCRIPTOR_SPEC.md:193-203,301-319,325-346`). The S2 specification describes an older implementation snapshot (`docs/spec/S2_CLASSIFY_SPEC.md:7-12`), and its rule-3 discussion cites old implementation lines (`docs/spec/S2_CLASSIFY_SPEC.md:385-388`). The current rule is at `visioncore/change.py:343-345`. Consequently, the source lines freshly read for this task take precedence over inherited line numbers. Parameter defaults below are explicitly **CITED from the specification**, not asserted to be the campaign's effective parameters.

Evidence labels used here:

- **CITED:** retrieved code, a retrieved specification, or an explicitly identified task-card statement.
- **DERIVED:** algebra or a conditional probability model developed from those formulas; assumptions are stated.
- **PROPOSED / HOST CHECK:** an unimplemented change or a falsifiable experiment.
- **AUSENTE:** an unestablished value, behavior or dependency. No empirical probability, performance result or acceptance is certified.

## 2. What the renderer changes, and what truth means

### 2.1 The random-looking field retains structure

The frame starts from a copied background or supplied base (`eval/gui_synth/raster.py:103`). For `canvas_anim`, it selects the first box and replaces its pixels with:

```python
# eval/gui_synth/raster.py:129-131
    elif kind=='canvas_anim':
        x,y,bw,bh=scene['boxes'][0]
        a[y:y+bh,x:x+bw]=texture(bw,bh,scene['seed']^(i*7919))
```

The multiplier **7919** is CITED from that line. The function's default offsets are zero (`eval/gui_synth/raster.py:33`); therefore the replacement keeps its local coordinate system from frame to frame. Its coordinate/seed mixer is reduced to its low five bits, while a separate coordinate-only band pattern is added (`eval/gui_synth/raster.py:36-47`). Text is then overlaid, including a seed-dependent number and a fixed second line (`eval/gui_synth/raster.py:19-23,51-54`).

**DERIVED from those operations:** write an unmasked pixel's low-bit value as $U$, with $U\in\{0,\ldots,31\}$, and its band index as $B\in\{0,1,2\}$. Then

$
g=45+23B+U,\qquad (R,G,B_{\rm rgb})=(g,g+12,g+24).
$

These ranges do not overflow the uint8 additions. The band index depends on location, not on the frame seed. The low-bit field is deterministic, shares one seed across the tile, and the successive seeds are coupled by the frame-index expression. **Independent pixels, independent frames and uniformly distributed low bits are modeling assumptions, not claims proved by the source.** Treating the complete generated images as independent, identically distributed RGB noise would also discard the bands and glyphs.

### 2.2 CHANGED truth and FLICKER are different questions

The label generator skips exact unchanged tiles, initializes differing tiles to CHANGED, and then tests jitter, exact translated equality, and a caret-specific return case (`eval/gui_synth/labels.py:54-65`). Its FLICKER exception is restricted to `caret_blink` after a full period and requires exact equality with the stored raster state (`eval/gui_synth/labels.py:62-65`). The independent pixel checker likewise uses exact pixels, translation matching, and a caret-specific period (`eval/gui_synth/labels.py:152-153,174-191`).

Thus, for the reported canvas tiles whose pixels differ and whose truth remains CHANGED, statistical similarity of texture summaries is not a truth-level blink. This does not establish that every conceivable canvas tile is CHANGED: unchanged pixels and exact translated matches have their own preceding checks (`eval/gui_synth/labels.py:54-65`).

## 3. Which descriptor quantities lose the distinction

Let $n=s^2$ be the padded tile's pixel count, as in `visioncore/desc.py:150-151`.

| Quantity | CITED computation | DERIVED consequence for the same generator |
|---|---|---|
| `y_q4` | $\lfloor16\sum Y/n\rfloor$ (`visioncore/desc.py:159,193`) | Stores a tile mean with a $1/16$-grey bin width. Independent noise means can be close even when individual pixels differ. |
| `co_h`, `cg_h` | Floors of chroma sums divided by $2n$, then clipping (`visioncore/desc.py:157-163,194-195`) | Stores chroma means in two-unit bins. For fixed glyph coverage these values are exactly seed-invariant, as derived below. |
| `sig_q4` | $\lfloor16\,\operatorname{isqrt}(n\sum Y^2-(\sum Y)^2)/n\rfloor$ (`visioncore/desc.py:159-161,196-197`) | Stores the population-standard-deviation statistic, with denominator $n$, at $1/16$-grey resolution for the divisible tile sizes considered here. It is not a pixelwise difference or an unbiased variance estimator. |
| `sig64` | One strict comparison of each subblock mean with the tile mean; an $8\times8$ subblock layout (`visioncore/desc.py:175-185,200`) | Keeps the sign of spatial mean differences, not their amplitudes or the individual noise samples. Strong persistent bands/glyphs can lock those signs across seeds. |
| `grad_q6`, `edge`, flags | Gradient, edge and flag computations (`visioncore/desc.py:165-173,187-199`) | These need not be equal: they are absent from the rule-3 residual's field list (`visioncore/change.py:54-61`). |
| `xh64` | BLAKE2b with an eight-byte digest, personalization `vqa-tile`, over padded RGB bytes (`visioncore/desc.py:54-57,134-145`) | A freshly computed hash distinguishes content changes with the usual finite-hash collision limitation, but the residual never consults it (`visioncore/change.py:54-61`). |

The integer square-root routine uses a floating-point seed followed by integer corrections (`visioncore/desc.py:23-29`). That implementation detail does not make the proposed classifier gates below floating-point gates.

### 3.1 Exact chroma cancellation

**DERIVED:** applying the YCoCg lifting operations in `visioncore/desc.py:34-40` to an unmasked generator pixel gives

$
Co=-24,\quad t=g+12,\quad Cg=0,\quad Y=g+12=57+23B+U.
$

Therefore a tile consisting entirely of those pixels has `co_h = -12` and `cg_h = 0` for every seed, regardless of how its noise is correlated. Its two chroma residual terms are exactly zero (`visioncore/desc.py:194-195`; `visioncore/change.py:58-61`).

For a glyph pixel, the renderer's RGB value $(225,230,238)$ (`eval/gui_synth/raster.py:53-54`) transforms to $(Y,Co,Cg)=(230,-13,-1)$. For a fully covered texture tile with a fixed glyph count $g_c$,

$
co_h=-12+\left\lfloor\frac{11g_c}{2n}\right\rfloor,\qquad
cg_h=\begin{cases}0&g_c=0\\-1&g_c>0.\end{cases}
$

These equalities require fixed glyph count, not identical noise. Changed numeric glyphs can change the count, the spatial layout, the luma mean and contrast; the seed-dependent text is explicitly present in `eval/gui_synth/raster.py:19-23`. Partial canvas coverage and mixed background/glyph pixels require their actual sums.

### 3.2 Signature sign, confidence band and Hamming dead zone

The S1 signature has **no confidence dead zone in its sign test**: its bit is set exactly when the subblock mean exceeds the tile mean (`visioncore/desc.py:184-185`). S2 computes a separate confidence bit when the absolute subblock deviation exceeds $D=\text{SIG_DEAD_Q4}/16$ grey levels; equality is inside the band (`visioncore/change.py:67-92`).

For a pair of frames, a flipped signature bit counts if it is confident in **either** frame. The masks are ORed, not ANDed (`visioncore/change.py:325-326,344`). Only flips inside the band in both frames are suppressed. If no luma plane is supplied, confidence defaults to all bits, so that suppression does not occur (`visioncore/change.py:95-103`).

The residual then applies a second, distinct allowance to the counted Hamming distance (`visioncore/change.py:29-30,54-61`):

$
C=|\Delta y_{q4}|+8(|\Delta co_h|+|\Delta cg_h|)
  +|\Delta sig_{q4}|,
\qquad
H=\operatorname{popcount}((sig64_a\oplus sig64_b)\mathbin{\&}(cf_a\mathbin{|}cf_b)),
$
$
R=C+8\max(0,H-h),\qquad h=\text{H_DEAD}.
$

Consequently $R=0$ does not require raw `sig64` equality: equal scalar words and at most $h$ counted flips suffice. Conversely, $R\le t_{\rm exit}$ can hold with nonzero scalar differences and several counted flips. Neither condition requires equality of every descriptor field, much less RGB equality. `words_eq` specifically means exact padded RGB equality to the immediately preceding frame (`visioncore/desc.py:81-85`); it must not be renamed “descriptor equality.”

## 4. How a small lag-two residual becomes false FLICKER

The current implementation executes (`visioncore/change.py:325-345`):

```python
r1 = residual(t, state.prev1, p["H_DEAD"], cf | state.cf1)
r1[words_eq] = 0
active = np.where(hot, r1 > t_exit, r1 > t_enter) & ~words_eq
r2 = residual(t, state.prev2, p["H_DEAD"], cf | state.cf2)
flicker = active & (r2 <= t_exit) & (r1 > t_enter)
```

**DERIVED mechanism:** noise aggregates can put $i$ and $i-2$ inside the return tolerance, while a different noise realization or changed glyph at $i-1$ pushes $r_1$ above entry. This looks like A-B-A only to the residual. A low $r_2$ by itself is insufficient: the lag-one difference must still enter, exact previous-frame equality must be false, and the FLICKER candidate must survive motion processing.

CITED defaults in `docs/spec/S2_CLASSIFY_SPEC.md:115-118` are $T_0=32$, hysteresis band $100$ permille, $h=2$, and `SIG_DEAD_Q4` $=24$. **DERIVED using the actual threshold formulas** (`visioncore/change.py:49-51`): $t_{\rm enter}=35$, $t_{\rm exit}=28$, $D=3/2$. Under those illustrative inputs a return requires

$
C\le28,\qquad H\le2+\left\lfloor\frac{28-C}{8}\right\rfloor.
$

Even at $C=0$, six counted flips fail the return test, while five can pass. Small changes across the scalar or Hamming boundaries can therefore produce occasional candidates. These defaults are not a reconstruction of the campaign profile.

The final label can differ from the initial candidate: published global motion can clear flicker (`visioncore/change.py:378-390`); active tiles are assigned CHANGED, then MOVED, then surviving FLICKER (`visioncore/change.py:416-418`). History rotates once per frame (`visioncore/change.py:478-480`). The campaign total must be reconciled against final labels, not just the initial Boolean candidate.

## 5. DERIVED per-tile frequency model — no renderer or engine run

### 5.1 Assumptions and domain

This section is an analytic conditional model, not a fitted campaign result. Fix the tile size, its position relative to bands, canvas coverage, padding, and a glyph mask. Let $J$ be the set of noisy pixels, $K=|J|$, and let $a_p$ be each pixel's fixed luma component: $Y_p=a_p+U_p$ for $p\in J$, and $Y_p=a_p$ elsewhere.

Assume the $U_p$ are independent uniform integers on $0,\ldots,31$ and are independently redrawn at the compared frames. This replaces the actual deterministic seed coupling (`eval/gui_synth/raster.py:38-41,131`). Normal approximations additionally require sufficiently many independent contributions. Treat changing glyph masks as separate strata or as an explicitly specified mixture; their actual mixture weights are AUSENTE. Edge replication, if present, repeats samples rather than adding independent ones (`visioncore/change.py:144-154`), so independent-pixel estimates cannot use padded area as new sample count.

**DERIVED by finite sums over the assumed uniform distribution:**

$
E[U]=31/2,\qquad v=\operatorname{Var}(U)=341/4=85.25,
$
$
\mu_4=E[(U-31/2)^4]=13064.5625,\qquad \mu_4-v^2=5797.
$

These are properties of the stated replacement model and the CITED five-bit range, not observations of generated frames.

### 5.2 Quantized scalar collisions

Let $A=\sum_pa_p$ and $S=\sum_{p\in J}U_p$. The **exact model probability** of a mean-luma word $q$, including the actual floor quantizer, is

$
p_y(q)=32^{-K}\!
 \sum_{\substack{u:\ qn\le16(A+u)<(q+1)n}}
 [z^u](1+z+\cdots+z^{31})^K.
$

The coefficient counts the possible noise sums; no scene is rendered to define this expression. Its quantizer comes from `visioncore/desc.py:193`. In particular,

$
P(y_{q4,i}=y_{q4,i-2})=\sum_qp_y(q)^2.
$

For a quicker normal approximation,

$
\operatorname{Var}(\overline Y)=vK/n^2,\qquad
\operatorname{SD}(\overline Y_i-\overline Y_{i-2})=\sqrt{2vK}/n.
$

For any normal-approximated statistic $X$ with center $\mu_X$, standard deviation $\tau_X$, and quantization width $\Delta$,

$
p_X(q)\simeq
 \Phi\!\left(\frac{(q+1)\Delta-\mu_X}{\tau_X}\right)
-\Phi\!\left(\frac{q\Delta-\mu_X}{\tau_X}\right),\qquad
P_{\rm same}\simeq\sum_qp_X(q)^2.
$

This expression retains the bin phase. When bins are small relative to the spread,
$P_{\rm same}\simeq\Delta/(2\sqrt{\pi}\tau_X)$. The approximation must not be extended beyond its small-bin regime; a population mean exactly on a bin edge also needs the full bin masses.

For a **noise-only, fixed-offset tile** with $K=n$, the delta method gives
$\tau_{\overline Y}=\sqrt{341/4}/s$ and
$\tau_{\widehat\sigma}\simeq\sqrt{17}/s$, since
$(\mu_4-v^2)/(4v)=17$. The latter applies to the population-standard-deviation statistic implemented at `visioncore/desc.py:196-197`. For the subdivision's sizes with $n$ divisible by $16$, the nested floor gives exactly $\lfloor16\widehat\sigma\rfloor$: writing $k=n/16$, $\lfloor\lfloor n\widehat\sigma\rfloor/k\rfloor=\lfloor16\widehat\sigma\rfloor$. The approximation is in the distribution/delta method, not a replacement of this quantizer. At the illustrative $s=16$ and $\Delta=1/16$, the small-bin estimates are about **0.0306** for equal mean words and **0.0684** for equal contrast words. Their product is not asserted to be full descriptor equality.

Fixed bands/glyphs require a different contrast variance. If $c_p=E[Y_p]$, $\bar c=n^{-1}\sum_pc_p$, and $V_0=n^{-1}\sum_p(c_p-\bar c)^2$, then

$
E[\widehat V]=V_0+vK(n-1)/n^2.
$

A leading-order delta-method approximation, neglecting smaller quadratic-centering terms, is

$
\operatorname{Var}(\widehat\sigma)\simeq
\frac{4v\sum_{p\in J}(c_p-\bar c)^2+K(\mu_4-v^2)}
     {4n^2E[\widehat V]}.
$

Use this only with nonzero contrast and the stated independent symmetric noise. Geometry changes the result. Scalar fields and the signature also share pixels; multiplying their marginal probabilities is an additional approximation.

### 5.3 Counted signature flips depend on tile size and persistent structure

The actual subdivision has $b=s/8$ and $m=b^2=n/64$ for supported divisible tile sizes (`visioncore/desc.py:175-185`). Let $K_k$ be the noisy-pixel count in subblock $k$, and define its margin $V_k=\overline Y_k-\overline Y$. Under the model,

$
\delta_k=E[V_k],\qquad
\nu_k^2=v\left(\frac{K_k}{m^2}+\frac{K}{n^2}
                  -\frac{2K_k}{mn}\right).
$

Persistent bands, glyphs and static background determine $\delta_k$. For all-noise coverage, $\nu_k^2=63v/s^2$. Thus subblock averages narrow as tile width grows, while a fixed structural margin can keep the signature sign stable.

Approximate $V_k$ by $N(\delta_k,\nu_k^2)$, with CDF $F_k$, and assume independent frames. The probability that one signature bit both flips and survives the **OR** confidence mask is

$
q_k=2\left[
 F_k(0)(1-F_k(0))
 -(F_k(D)-F_k(0))(F_k(0)-F_k(-D))
 \right].
$

The second term removes opposite-sign pairs that are inside the confidence band in both frames. For a centered cell $\delta_k=0$, let $u=\Phi(-D/\nu_k)$; then $q_k=2u(1-u)$. For deterministic margins ($\nu_k=0$), use their exact sign/confidence outcome instead of dividing by zero.

For a discrete margin distribution, preserve the code's exact ties instead of the continuous CDF approximation:

$
q_k=2\left[P(V_k>0)P(V_k\le0)
-P(0<V_k\le D)P(-D\le V_k\le0)\right].
$

These probabilities can be evaluated by finite sums of the same uniform-noise sum coefficients used in Section 5.2. At the illustrative $s=16$, a subblock has only four pixels, so the normal tails in the following table are a coarse illustration, particularly near zero and the dead-band endpoints.

With the further approximation of independent subblock flip indicators, $H$ has the Poisson-binomial generating function

$
G(z)=\prod_{k=0}^{63}(1-q_k+q_kz).
$

Actual indicators are not independent: for all-noise coverage the Gaussian margin correlation between different cells is $-1/63$, derived from their shared tile mean. The product is an approximation even under the iid-pixel model.

**DERIVED illustration:** normal centered cells, independent flip indicators, all-noise coverage, and the CITED defaults $D=3/2,h=2,t_{\rm exit}=28$. The table evaluates the binomial CDF $P(H\le5)$. This is a **Hamming-only ceiling on the return probability within that approximation**; scalar costs can lower it. It is neither a fitted canvas rate nor a FLICKER rate.

| Assumed tile width $s$ | Subblock width $b$ | Counted-flip probability $q_k$ | $E[H]$ | Approximate $P(H\le5)$ |
|---:|---:|---:|---:|---:|
| 16 | 2 | 0.467052 | 29.8914 | $1.40\times10^{-11}$ |
| 32 | 4 | 0.381164 | 24.3945 | $3.56\times10^{-8}$ |
| 64 | 8 | 0.172123 | 11.0159 | 0.025997 |
| 128 | 16 | 0.008757 | 0.5604 | 0.999978 |

The very small lower-width values show why “random textures have the same histogram” is insufficient by itself: centered random signature bits can still strongly reject the return. The actual generator's persistent spatial structure must be included.

For sensitivity, retain the $s=16$ centered-cell flip probability but assume only $M$ uncertain cells and lock all remaining signs. The same binomial calculation gives $P(H\le5)\approx0.8945$ for $M=8$, $0.1616$ for $M=16$, and $0.0002316$ for $M=32$. These are hypothetical geometry cases, not counts of cells in the campaign. Actual $\delta_k$, mask changes, and scalar costs determine which case, if any, resembles a tile.

### 5.4 Combine the terms and distinguish a return from a false blink

Let $P_C(c)$ be the nonnegative integer scalar-cost distribution defined in Section 3.2. A convenient approximate per-tile return rate is

$
P_{\rm return}(T)\simeq
 \sum_{c=0}^{T}P_C(c)
 \sum_{j=0}^{\min(64,\;h+\lfloor(T-c)/8\rfloor)}
 [z^j]G(z).
$

Obtain $P_C$ from the quantized field distributions or their joint distribution. Using a product of marginal scalar distributions, or making $C$ independent of $H$, must be declared as additional approximations. This formula exposes tile size, pixel coverage, quantization, confidence band, Hamming allowance and threshold dependence without claiming universal numeric frequency.

A correlation-preserving definition is possible without those approximations. Let $X$ include a tile's bytes, quantized fields and confidence, and let $\pi_s$ be their joint distribution obtained by applying the cited formulas to the $32^K$ possible modeled noise assignments. Then

$
P_{\rm return}(T)=
 \sum_{x,z}\pi_s(x)\pi_s(z)\,\mathbf1\{R(x,z)\le T\}.
$

That finite expression is exact for the specified replacement model, although it is not evaluated here. It is not a probability law proved for the deterministic generator.

For valid thresholds $t_{\rm enter}\ge t_{\rm exit}$, define

$
a(x)=P_Z(\text{bytes}(x)\ne\text{bytes}(Z),\,R(x,Z)>t_{\rm enter}),
\quad c(x)=P_Z(R(x,Z)\le t_{\rm exit}).
$

Under independent frame draws, the **initial rule-3 candidate probability** is

$
P_{\rm rule3}=E_X[a(X)c(X)].
$

The two residuals share the current tile; therefore substituting
$P(r_1>t_{\rm enter})P(r_2\le t_{\rm exit})$ generally loses dependence. Final FLICKER additionally depends on the motion veto (`visioncore/change.py:386-390,416-418`). Large return probability can coexist with few candidates if lag-one residuals also remain small and fail entry (`visioncore/change.py:329,345`).

Summing the final event probability over eligible tiles and frames would give an expected campaign count without requiring independence between those events. The actual tile mix, seed correlations, effective parameters, motion outcomes and denominator are AUSENTE, so the number **1855** cannot responsibly be derived from these formulas alone.

## 6. Concrete falsifiable checks for the host

All checks below are **PROPOSED, NOT RUN**. Keep frames and images exclusively in memory; emit only text diagnostics or counters.

| Check | Procedure and cited hook | Prediction or falsifier |
|---|---|---|
| Separate equality notions | For each canvas lag-two pair compare exact padded RGB equality, fresh `xh64_of` equality, equality of all perceptual fields, $R=0$, and $R\le t_{\rm exit}$. Use `visioncore/desc.py:54-57,130,134-145` and the actual residual/masks at `visioncore/change.py:54-61,325,344`. | The hypothesis predicts byte/hash-different pairs that pass the residual return gate. If the reported false cases all have exact repeating pixels, investigate the truth/count attribution instead. |
| Decompose both residuals | Log each scalar cost, raw and masked Hamming counts, confidence populations, `words_eq`, `hot`, thresholds, $r_1,r_2$, initial candidate and final class (`visioncore/change.py:54-61,325-345,416-418`). | Establish which term moves $r_1$ above entry while $r_2$ remains below exit. A small $r_2$ with no qualifying $r_1$ cannot explain a FLICKER label. |
| Stratify geometry | Separate full canvas interiors, changing digits, fixed glyphs, canvas boundaries and padding; record $s,K,K_k,\delta_k$ in memory. Relevant generator and aggregation hooks: `eval/gui_synth/raster.py:19-23,43-54,129-131`; `visioncore/desc.py:134-185`. | Compare conditional frequencies with the formulas. A universal tile-size curve that ignores mask/coverage is not the model presented here. |
| Falsify independence | For fixed-mask interiors, inspect noise-value marginals and spatial/lag correlations against the assumed uniform iid law; inspect mean dispersion as tile width varies (`eval/gui_synth/raster.py:38-41,131`). | Material correlations invalidate the iid frequency estimate even though chroma cancellation remains exact. |
| Isolate both allowances | Recompute pair diagnostics with all confidence bits enabled and separately with $h=0$, recording both residuals (`visioncore/change.py:54-61,75-76,95-103`). These are diagnostic ablations, not additional fixes. | Raising masked Hamming counts should raise pair residuals. Final FLICKER need not change monotonically because the lag-one entry test changes too. |
| Verify hash provenance | Compare eager descriptors, lazy descriptors, post-S2 output hashes, and `state.prev1/prev2` against fresh hashes of retained corresponding RGB tiles (`visioncore/desc.py:79,99-127`; `visioncore/change.py:106-110,309,316,478-480`). | The documented lazy path has zero pre-fill hashes; filling an independent output copy cannot retroactively repair the captured state. Any hash gate using placeholders fails this check. |
| Reconcile the campaign | Obtain the primary diagnosis and count only final predicted FLICKER intersected with truth CHANGED for the same DEV scene/frame/tile keys; record the effective profile and baseline revision (`eval/gui_synth/labels.py:67-73`; `visioncore/change.py:416-418,484-487`). | Recover the cited **1855** only on the matching campaign input. A count mismatch must remain visible; never substitute another benchmark's aggregate. |
| Regress cursor behavior | Compare baseline and each proposal on exact lag-two A-B-A, longer blink periods, small antialias perturbations, blinking over moving/noisy backgrounds, and RGB changes with identical luma. Renderer/label hooks: `eval/gui_synth/raster.py:110-119`; `eval/gui_synth/labels.py:32-33,62-65`. | Report FLICKER recall and CHANGED/MOVED/JITTER outcomes separately. Distinguish label `ideal` from `v1_expected`: caret blink's expected baseline label is CHANGED in `eval/gui_synth/labels.py:32-33`. |

Do not reconstruct Q4 thresholds from the stored display residual: the output field is shifted and saturated, and drift can use a different reference (`visioncore/change.py:461-467`). Instrument or recompute the internal residual with the actual masks.

## 7. At most two integer-only fixes, proposed only

### Proposal 1 — require an actual RGB hash return

**Preferred first falsification target:** retain the existing rule-3 candidate and require matching valid exact-content hashes for the current tile and lag two. Target the predicate at `visioncore/change.py:343-345`.

```python
# PROPOSED predicate; exact_* are new S2-private hash/validity state.
flicker = active & (r2 <= t_exit) & (r1 > t_enter)
flicker &= exact_cur_valid & exact_prev2_valid & (exact_cur == exact_prev2)
```

Compute actual current padded-RGB hashes before this test, using the existing functions at `visioncore/desc.py:54-57,134-145`. Preserve exact hash history and explicit validity alongside the initialization and history rotation at `visioncore/change.py:121-135,478-480`. Reuse an older exact hash only when exact byte equality justifies it (`visioncore/desc.py:84-85`). A legitimate hash can equal zero; do not use nonzero value as the validity test.

**Required integration detail:** appending `t["xh64"] == state.prev2["xh64"]` alone is insufficient. The lazy path returns the zero-initialized hash field before hashing (`visioncore/desc.py:79,99-109`); the post-S2 filler may copy an older label for JITTER (`visioncore/desc.py:113-127`). S2 separately copies the input, output and historical descriptors (`visioncore/change.py:106-110,309,316,478-479`), so filling the returned output afterward does not repair those previous copies. A dedicated exact history, or an explicitly verified eager-before-capture contract, is essential. The campaign caller's actual wiring is AUSENTE from the bound files.

**DERIVED risk for real cursor blink:** a qualifying lag-two blink whose complete RGB tile repeats preserves this gate. A blink sharing a tile with noise, antialias changes or a moving background can fail it. Hash equality also has a finite collision possibility; if strict byte identity is required, validate matching candidates with retained in-memory RGB bytes. No performance or collision rate is asserted.

This proposal preserves the existing lag-two limitation rather than extending period detection: the renderer can hold each blink state for runs of frames (`eval/gui_synth/raster.py:118-119`), and truth uses a full-period return (`eval/gui_synth/labels.py:62-65`). A rejected candidate becomes eligible for existing motion handling and CHANGED/MOVED assignment (`visioncore/change.py:378,416-418`); this does not repair unrelated nonactive canvas classifications.

### Proposal 2 — require a small pixelwise lag-two luma residual

As an alternative to the exact RGB-hash requirement, confirm each descriptor candidate with

$
16\sum_{p\in{\rm tile}}|Y_i(p)-Y_{i-2}(p)|\le n\,t_{\rm exit}.
$

Apply this additional integer inequality at `visioncore/change.py:345`, using the unshifted tile-SAD helpers at `visioncore/change.py:262-270`. Retain an additional luma history plane: the current state has only `y_prev` (`visioncore/change.py:135`), initializes it in `visioncore/change.py:497-500`, and updates it in `visioncore/change.py:481-482`. Rotate a new lag-two plane before overwriting lag one, define its ownership/copying, and require available valid history. Missing pixels must not silently authorize the candidate. These are proposed state changes; their performance and caller ownership contract are AUSENTE.

**DERIVED rationale under the fixed-mask iid model:**

$
E|U-U'|=\frac1{32^2}\sum_{u,v=0}^{31}|u-v|
       =\frac{341}{32}=10.65625.
$

The illustrative exit threshold gives $28/16=1.75$ grey levels per tile pixel. For a full noisy interior that separates typical independent redraws from an exact luma return. With noisy fraction $\rho=K/n$, the model's tile-average expected difference is instead $\rho\,341/32$; static coverage dilutes this gate. An expectation comparison is not a guaranteed rejection rate.

**DERIVED risk for real cursor blink:** exact repeated luma passes, but enough noise or antialias change can exceed the tolerance. Two grey levels across enough pixels already exceed the illustrative average ceiling. RGB changes preserving luma can pass, so this is weaker than RGB confirmation. It also retains the lag-two period limitation (`visioncore/change.py:343-345`).

Both proposals add integer/hash comparisons, Boolean masks and state operations; neither changes S1 quantization or the general jitter residual. Their empirical effect must be established by the host checks, including any new CHANGED, MOVED or JITTER outcomes.

## 8. AUSENTE and execution record

- **Primary campaign evidence:** original false FLICKER diagnosis, baseline commit/profile, denominator, tile-size/coverage distribution, and event-level evidence linking every cited false label to this mechanism.
- **Generator probability law:** independence and effective sample counts for the deterministic spatial/temporal mixer; mask-mixture probabilities and padding correlations for the campaign.
- **Caller behavior:** whether the campaign supplies `y_cur`, initializes confidence from pixels, requests lazy hashes, mutates luma buffers, or repairs exact historical hashes. The bound functions expose options and copies, not the full call chain (`visioncore/desc.py:67-73,99-127`; `visioncore/change.py:95-103,106-110,478-500`).
- **Undefined extension behavior:** a hash-validity/history contract and lag-two luma ownership for the proposals do not currently exist in the cited state fields (`visioncore/change.py:121-141`). Their missing definitions are AUSENTE, not assumed behavior.
- **Empirical outcomes:** reproduction of **1855**, actual per-tile return rates, cursor-regression results, fix efficacy, timing, memory and acceptance are AUSENTE.

No renderer, engine, scene generation, Monte Carlo simulation or benchmark was executed for this investigation. Only source retrieval, source-byte hashing and arithmetic evaluation of the displayed analytic formulas were performed. No frames or images were written to disk. No timing result is reported; any future virtual-machine timing must be labeled **VM**, never **MEASURED**. Both fixes remain proposals, and the orchestrator performs its own acceptance gate.
