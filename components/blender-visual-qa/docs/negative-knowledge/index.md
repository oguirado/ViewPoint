# Negative Knowledge and Operational Lessons

## Lessons

The development and live operational calibration of `godot-visual-qa` across multiple engine rounds produced 30 foundational operational lessons (L-1 through L-30). These are codified as normative negative rules to prevent regression:

| Lesson ID | Mandatory Mechanism and Operational Rule | Positive Witness | Rejecting Negative Contrast |
|---|---|---|---|
| **L-1** | Reject undersized MP4 clips before dispatch; encode clips $\ge 1.0\text{ s}$ while preserving every captured frame exactly once when lowering playback fps. | Short valid-frame clip meets duration with same frame count | Remux of undersized clip (< 1.0 s) is rejected |
| **L-2** | Render literal UTF-8 media paths in judge prompts without escaping unicode characters. | Non-ASCII directory (e.g. `Música`) appears literally | Escaped unicode path prevents model from accessing evidence |
| **L-3** | Configurable direct bounded worker argv and agy path; transport wrapper lifetime must cover declared deadline. | Environment overrides reach selected transport | Unavailable worker path halts execution immediately |
| **L-4** | Derive validation and check expectation counters dynamically from `checklist.json`. | Adding check updates expectation dynamically | Stale hardcoded check counter is rejected |
| **L-5** | Render attention canaries via `ffmpeg drawtext` using DejaVuSansMono.ttf ($\ge 32\text{ px}$) and restricted alphabet `ACDEFHJKLMNPRTUVWXY3479`. | Alphabet renders distinct nonblank glyphs | Missing font fails closed before dispatch |
| **L-6** | Enforce measured admission limits (480s call, 2400s wall, 300k in/call, 1.5M in/run) and max 3 media items per batch. | Excess media split into bounded batches | Excess resource request rejected before dispatch |
| **L-7** | Video sampling requires explicitly mapped 1 fps slow judge copies and $\ge 2\text{ s}$ temporal canaries. | Selected endpoints and frame mapping retained | Raw fast clip cannot prove all frames observed |
| **L-8** | Frozen-frame detection is deterministic-only (`frame.frozen`) over declared motion windows, excluding PAUSED. | Subtle real motion passes deterministic check | Model vote overriding deterministic freeze is rejected |
| **L-9** | Second adapter (`example_game`) must demonstrate dual channels (deterministic oracles alongside semantic rubrics). | Clean fixture passes truthful fake judge + oracles | Mutant fails named target check on both channels |
| **L-10** | Timestamp `0` on PNG still observations normalizes to `null` with an audit note; non-zero timestamps on PNGs fail validation. | Timestamp 0 normalized to null explicitly | Non-zero timestamp on PNG still is rejected |
| **L-11** | Production CLI path must evaluate and persist deterministic oracle results to `oracles/results.json` before judging. | Black fixture + fake judge PASS produces FAIL | CLI monkeypatching or unit-only oracle is rejected |
| **L-12** | Bounded same-batch transport retry once after 20s backoff with fresh attempt ID; preserve worker diagnostic logs. | Transient worker error retries and succeeds | Repeated failure halts with preserved diagnostic files |
| **L-13** | Calibration aggregation separates clean errors from false positives; preserve null metrics with absence reasons. | Matching-pin passing clean qualifies mutant trial | Coercing unknown metrics to zero is rejected |
| **L-14** | Replay hooks must instantiate the real arena scene and capture real node telemetry; solid-colour scenes are invalid. | Non-uniform arena scene with HUD nodes visible | Uniform solid-colour scene fails qualification |
| **L-15** | Add dedicated deterministic `frame.uniform` oracle to catch non-black single-colour frames. | Multi-colour arena frame passes | Solid green frame fails uniform oracle |
| **L-16** | Headless scratch project asset import is mandatory before capture; preserve engine logs. | Texture-backed scene renders after import | Bypassing import renders untextured fallback |
| **L-17** | Complete captures must supply valid frame evidence for all declared point ranges. | Complete capture yields evaluable PASS or FAIL | Unexplained CANNOT_SEE on complete capture rejected |
| **L-18** | Checks assessing unimplemented game features must be declared advisory (`required: false`) with a revision-scoped reason. | Advisory check reports finding without gating run | Unimplemented feature causing run failure is rejected |
| **L-19** | Forbidden UI overlap pairs must include all interactive and informational leaves (quick-buy bar, HUD labels, controls help). | Corrected layout passes and broken pin fails | Missing overlap pair in checklist is rejected |
| **L-20** | Judge observations are validated by kind: scene observations need no bbox; OCR/text observations need a valid bbox. | Scene observation without bbox stays valid | Rejecting a scene observation for a missing bbox is rejected |
| **L-21** | A single attention-canary misread is retried before the batch is declared ERROR. | One-character canary slip recovers on retry | Immediate batch ERROR on one misread is rejected |
| **L-22** | Godot runs use `--audio-driver Dummy`; the ALSA open error must never reach the strict engine-ERROR gate. | Preflight passes on hosts without audio | Strict gate failing on ALSA noise is rejected |
| **L-23** | Calibration per-run timeout is composed from stage budgets (default 3600 s), never hard-coded short. | Long OZ runs complete | 300 s per-run cap is rejected |
| **L-24** | Adapter judge budgets must cover the measured per-call and per-run token cost. | Judge evaluates every required check | Judge stopping after one call is rejected |
| **L-25** | A kill requires the named target check to FAIL; non-target abstentions in mutant runs are recorded, not invalidating. | Target FAIL + collateral CANNOT_SEE = KILLED | Demanding full coverage in mutant runs is rejected |
| **L-26** | MP4 scene observations without `time_ms` are normalized; only unlocated OCR observations are dropped. | Run continues with a validation note | Run ERROR for a missing time_ms is rejected |
| **L-27** | Checks whose evidence is absent from the pinned capture window are advisory with a revision-scoped reason. | No combat in window → advisory | Gating on absent evidence is rejected |
| **L-28** | A malformed bbox invalidates only its observation; the check stands or becomes CANNOT_SEE. | `[x,y,w,h]` box dropped with a note | Whole-run ERROR for one bad box is rejected |
| **L-29** | Calibration aggregation re-decodes every PNG with a pure-Python decoder and is slow on large captures. | Aggregate in the background | Assuming aggregation is negligible is rejected |
| **L-30** | The mutant witness verifier compares one aggregate before/after receipt to every target (OZ `hud_hidden` INVALID while its target FAILED). | Read INVALID with its target verdict | Reading that INVALID as a detection miss is rejected |

## Non-guarantees

The architecture explicitly rejects common false assumptions:
- **Media Attention $\neq$ Semantic Accuracy**: A model correctly transcribing attention canaries proves that media was opened, but does not guarantee accurate visual deduction.
- **Engine Node Visibility $\neq$ Readable Pixels**: A Godot `CanvasItem` having `visible == true` does not guarantee the element is unoccluded, legible, or rendered within the viewport.
- **Video Duration $\neq$ Simulation Speed**: Slowed 1 fps judge copies alter video playback rate; duration in the clip does not equal real-time gameplay duration.
- **Hash Equality Outside Motion Windows $\neq$ Simulation Freeze**: Identical frame hashes during loading, paused states, or menu screens do not indicate an engine hang.
- **Engine Compile or Import Failure $\neq$ Semantic Mutant Kill**: If a mutant fails due to a syntax error or crashed import, the trial is invalid, not a successful mutant kill.

## Status policy

- **Historical Defect is not Current Defect**: A documented historical bug in `LIVE_FINDINGS.md` records an empirical event during calibration; it does not imply current code remains broken.
- **Accepted Fix is not Implementation**: An agreed-upon design proposal does not grant verified status until sealed code, positive witnesses, and rejecting negative fixtures exist.
- **Preservation of Lessons**: When a defect is resolved, the corresponding L-series lesson is never deleted; it remains permanently documented as an invariant regression guard.
