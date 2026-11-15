# WA S4 LEARN contracts

Source snapshot: `example-owner/wender-agent`, explicit ref `bridge/integration`, observed commit `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9`. All path:line citations below refer to that snapshot. **AUSENTE** marks an unspecified implementation contract. The section headed **PROPOSAL** is a design review; its algorithms, fields, policies, and interfaces are not implemented by the current stubs.

## Contracts

### Implemented scope and public symbols

The module declares a TARGET pipeline: vision-engine segmentation from screen/panel changes, cursor motion and keyframes; intent labeling from a few frames; mapping to structured actions; headless replay and verification against the recorded result; then skill admission. It explicitly says that only the honest metric is real in v0 and that frames never touch disk (`wender/learn/demonstrations.py:4-8`).

The following table lists every public function defined by the module. Anchor locations refer to actual marker lines, as returned by the scanner; missing markers are AUSENTE, not inferred from function names (`tools/anchors.py:13`, `tools/anchors.py:33-36`).

| Public symbol | Definition | Existing anchor ID and location | Current body |
|---|---|---|---|
| `segment(video)` | `wender/learn/demonstrations.py:12` | AUSENTE | Raises `NotImplementedError("v0 stub: vision-engine segmentation")` at `wender/learn/demonstrations.py:13` |
| `label_intent(segment_frames)` | `wender/learn/demonstrations.py:16` | AUSENTE | Raises `NotImplementedError("v0 stub: multimodal intent labelling")` at `wender/learn/demonstrations.py:17` |
| `map_to_actions(intent)` | `wender/learn/demonstrations.py:20` | AUSENTE | Raises `NotImplementedError("v0 stub: reasoner -> structured actions")` at `wender/learn/demonstrations.py:21` |
| `replay_and_verify(actions, expected)` | `wender/learn/demonstrations.py:24` | AUSENTE | Raises `NotImplementedError("v0 stub: headless replay + verification")` at `wender/learn/demonstrations.py:25` |
| `reproduced_share_ppm(segments)` | `wender/learn/demonstrations.py:29` | `wa.learn.demonstrations.reproduced_share_ppm` at `wender/learn/demonstrations.py:28` | Counts strict boolean outcomes and returns integer ppm at `wender/learn/demonstrations.py:34-38` |

For calls satisfying their signatures, the four stub bodies inspect no arguments, return no successful result, and invoke no pipeline stage. Input/output schemas, segmentation boundaries, decoding, model selection, action planning, replay, expected-state capture, checkpoint semantics, and verification receipts are AUSENTE. Although the header declares a dependency on `wa.remember.skills`, the current module does not import or call admission (`wender/learn/demonstrations.py:1`, `wender/learn/demonstrations.py:12-25`).

### Exact reproduced-share arithmetic

For an ordinary stable finite sequence of mapping-like records, the implemented computation is:

1. Read `total = len(segments)` before iteration (`wender/learn/demonstrations.py:34`).
2. If `total == 0`, return integer `0` immediately (`wender/learn/demonstrations.py:35-36`).
3. Count one for each record where `s.get("reproduced") is True and s.get("verified") is True` (`wender/learn/demonstrations.py:37`).
4. Return `ok * 1000000 // total`: multiply the count first, then use integer floor division. There is no floating-point intermediate, rounding-to-nearest step, or percentage conversion (`wender/learn/demonstrations.py:38`).

Both flags must be the literal boolean `True`. Numeric `1`, strings such as `"true"`, and other truthy objects do not qualify. With ordinary dictionaries, a missing key supplies `None` through `.get` and does not qualify. The second lookup is short-circuited unless `reproduced is True` (`wender/learn/demonstrations.py:37`).

**Reproduced but unverified segments never count in the numerator.** They remain part of the supplied collection's length. Verification without reproduction also contributes zero. Every supplied element counts toward the denominator; there is no filtering, identity deduplication, retry grouping, source-coverage validation, or examination of actual evidence (`wender/learn/demonstrations.py:34-38`).

| Ordinary stable input | Integer result | Basis |
|---|---:|---|
| Empty sequence | 0 | Early return at `wender/learn/demonstrations.py:35-36` |
| One qualifying record among three | 333333 | Arithmetic consequence of `wender/learn/demonstrations.py:37-38` |
| Two qualifying records among three | 666666 | Arithmetic consequence of `wender/learn/demonstrations.py:37-38` |
| Every record qualifies in a nonempty sequence | 1000000 | Arithmetic consequence of `wender/learn/demonstrations.py:34-38` |
| Two qualifying records among the bound four-record fixture | 500000 | Assertion at `tests/test_layers.py:88-91` |

These values describe code arithmetic, not performance observations. For a stable finite collection, the qualifying count lies between zero and its length, so the result lies inclusively between zero and one million. The code provides no baseline, target success rate, statistical confidence interval, or interpretation that an empty run succeeded; those are AUSENTE (`wender/learn/demonstrations.py:34-38`).

### Verification flags and skill admission are distinct

The metric consumes top-level `reproduced` and `verified` flags. Skill admission instead checks a dictionary at `skill["evidence"]`, literal `True` at `evidence.reproduced`, and a truthy `evidence.verified_by`; it does not test a `verified` field. A verifier name or other truthy value is not an implemented execution of that verifier (`wender/learn/demonstrations.py:37`, `wender/remember/skills.py:26-28`).

Admission also requires the seven named record fields, nonempty steps/checkpoints, truthy `provenance.source`, and step paths in `("api", "ax", "coordinates")`, then returns canonical JSON. It does not provide the missing replay implementation or skill store. An adapter connecting actual verification to both field conventions is AUSENTE in S4 (`wender/remember/skills.py:9`, `wender/remember/skills.py:19-34`, `wender/remember/skills.py:5`, `wender/learn/demonstrations.py:24-25`).

The relevant existing admission anchor is `wa.remember.skills.admit` at `wender/remember/skills.py:16`, with definition at `wender/remember/skills.py:17`. It is a proposed future handoff boundary, not a function currently called by the metric (`wender/learn/demonstrations.py:34-38`).

## Failure modes

The four exact `NotImplementedError` messages appear in the public-symbol table. Those unconditional exceptions are current behavior, not a recoverable no-result status or an implemented pipeline fallback (`wender/learn/demonstrations.py:13`, `wender/learn/demonstrations.py:17`, `wender/learn/demonstrations.py:21`, `wender/learn/demonstrations.py:25`).

The metric performs no schema validation or exception wrapping. An ordinary generator fails at `len`; a nonempty list containing `None` fails when its element's `.get` is invoked. Errors from custom length, iteration, or lookup implementations propagate. A custom or concurrently modified collection can have a reported length inconsistent with iteration; the function neither snapshots the input nor verifies that consistency (`wender/learn/demonstrations.py:34-38`).

A record with false or missing ordinary dictionary flags is valid for the arithmetic and contributes zero to the numerator. By contrast, a malformed record is not automatically converted to a failed segment. Domain-specific validation errors, partial-replay handling, retries, timeouts, and recovery rules are AUSENTE (`wender/learn/demonstrations.py:34-38`, `wender/learn/demonstrations.py:12-25`).

## Determinism

The real metric is deterministic for the same stable input length and strict boolean outcomes. Reordering the same ordinary record collection does not change the count or denominator; duplicate records count separately. The function contains no clock, random source, model invocation, or replay operation. It cannot establish determinism of the unimplemented upstream stages (`wender/learn/demonstrations.py:12-25`, `wender/learn/demonstrations.py:34-38`).

The shared serialization primitive available elsewhere recursively rejects float values in dictionary values and list/tuple elements, then calls `json.dumps` with sorted keys, compact separators, and `ensure_ascii=False`. It returns text, preserves array order, and does not normalize arbitrary input schemas or inspect strings for encoded pixels. S4 currently does not call it; its use for future metadata is a proposal (`wender/state/schema.py:25-39`, `wender/learn/demonstrations.py:12-38`).

The bound LEARN test checks the mixed verified/unverified fixture and empty input. It does not test segmentation, labeling, mapping, replay, model repeatability, malformed metric inputs, or frame storage behavior (`tests/test_layers.py:87-92`). Deterministic replay guarantees and performance budgets are AUSENTE.

## STORAGE LAW

The module's declared invariant is that frames never touch disk. The current stubs and arithmetic have no frame I/O, but that absence does not implement a guard for future decoders, adapters, models, or executors (`wender/learn/demonstrations.py:7`, `wender/learn/demonstrations.py:12-38`).

The bound convention test searches below the repository root for `.png`, `.jpg`, `.jpeg`, `.webp`, and `.npy`, ignoring paths containing `.git`. It does not inspect other encodings, text containing pixels, runtime caches outside that root, or external component behavior (`tests/test_repo_conventions.py:38-41`).

No implemented frame buffer, lifetime manager, memory limit, spill prevention, or allowlist for durable metadata exists in S4. These enforcement mechanisms are AUSENTE at the segmentation and replay stubs. The proposal below therefore keeps decoded frames, selected crops, visual expectations, and visual verification results in RAM and treats memory exhaustion as an explicit failure, never a reason to write pixels (`wender/learn/demonstrations.py:12-25`).

## PROPOSAL — deterministic segmentation and replay

**Status: PROPOSAL throughout this section.** The only implementation basis is the declared TARGET pipeline and RAM-only law; each concrete algorithm, record field, policy, and component boundary below is proposed. Current implementations remain AUSENTE at the four stub bodies (`wender/learn/demonstrations.py:4-7`, `wender/learn/demonstrations.py:12-25`).

### 1. Bind inputs and deterministic configuration

Require an authorized source handle that yields decoded frames into RAM with integer frame ordinals. Bind the source identity, decoder/engine version, segmentation-rule version, action-plan version, and verifier version into nonpixel metadata. If time is needed, represent it with an explicitly defined integer unit or rational integers; never use floating-point or wall-clock arrival timing as the boundary decision. Actual interfaces, units, thresholds, buffer capacities, and budget values are AUSENTE today (`wender/learn/demonstrations.py:4-7`, `wender/learn/demonstrations.py:12-13`, `wender/state/schema.py:25-39`).

Pin configuration before segmentation. If an input can be reconsumed, require the same identified content and decode configuration on each pass. For nonrewindable input, retain sufficient required frames in RAM or fail explicitly. Do not spool a video, frame stream, or recovery cache to disk; the existing stub offers no such input-lifetime contract (`wender/learn/demonstrations.py:7`, `wender/learn/demonstrations.py:12-13`).

### 2. Segment with a pure ordered rule

Use the TARGET event categories supplied by vision-engine: screen changes, panel changes, cursor motion, and keyframes. Convert them to proposed records indexed by frame ordinal, with integer comparison values and a fixed event-priority order. Apply a versioned, deterministic boundary predicate; sort boundary ordinals, deduplicate equal boundaries, and form nonoverlapping half-open intervals. Define first/last-frame, simultaneous-event, empty-source, and malformed-event handling before implementation. These predicates and interval schemas are AUSENTE in `segment` (`wender/learn/demonstrations.py:4-5`, `wender/learn/demonstrations.py:12-13`).

Select the few frames used for labeling through a fixed policy, such as interval endpoints and the earliest ordinal attaining a selected integer change score. The precise policy, maximum selection count, and required event fields remain explicit design decisions, not existing defaults. Model output must not move the segmentation boundaries after the cohort is fixed (`wender/learn/demonstrations.py:4-5`, `wender/learn/demonstrations.py:16-17`).

### 3. Freeze the denominator before attempting reproduction

Create a complete ordered segment manifest before intent/replay outcomes can affect membership. Give each segment one stable identity and one outcome entry. Unsupported, failed, timed-out, unverifiable, and memory-exhausted segments retain their entries. Retries update the same entry instead of increasing the denominator or deleting unsuccessful attempts. This cohort logic is needed because the implemented metric merely trusts the supplied length and flags (`wender/learn/demonstrations.py:34-38`).

If source or segmentation failure prevents establishing the complete manifest, mark the run incomplete and withhold a complete-run metric receipt. Do not publish the metric of a conveniently truncated successful prefix, and do not turn a failed source read into an apparently successful empty run. For a genuinely complete empty manifest, retain the implemented zero result and report that there were no segments (`wender/learn/demonstrations.py:12-13`, `wender/learn/demonstrations.py:34-38`).

### 4. Make model decisions explicit replay inputs

Supply only the selected RAM frames and appropriate structured metadata to labeling, following the few-frame TARGET. Validate the resulting intent/action representation, canonicalize nonpixel text metadata, and bind the specific accepted decision to its segment before replay. Input/output schemas and actual model/reasoner integrations remain AUSENTE (`wender/learn/demonstrations.py:5-6`, `wender/learn/demonstrations.py:16-21`, `wender/state/schema.py:25-39`).

Pinning a model, prompt, and decoding configuration does not establish deterministic generation. The proposal's reproducibility guarantee is conditional on fixed accepted labels/action plans and fixed segmentation inputs; a fresh generation is a separately identified attempt. The current stubs offer no model-repeatability guarantee (`wender/learn/demonstrations.py:16-21`).

For proposed structured context, preserve the STATE boundary described by the schema module instead of injecting raw AX/HTML. Define size limits and explicit failure behavior before use; neither the labeling stub nor generic serializer supplies such a budget (`wender/state/schema.py:4-6`, `wender/state/schema.py:25-28`, `wender/learn/demonstrations.py:16-17`).

### 5. Replay against fixed expectations

Define reset/precondition state, ordered structured actions, required application/environment identifiers, and checkpoint predicates before execution. The TARGET favors structured actions such as bpy/MCP or AX; accepted skill path strings do not prove that any corresponding executor works. Execution mechanisms and replay preconditions remain AUSENTE at `map_to_actions` and `replay_and_verify` (`wender/learn/demonstrations.py:6-7`, `wender/learn/demonstrations.py:20-25`, `wender/remember/skills.py:31-33`).

Derive expected outcomes from the demonstration or separately justified observation while its frames are in RAM, not from the replay's own claim of success. Compare replay results against those fixed expected outcomes using a versioned deterministic predicate over available structured results or RAM visual references. The comparator's schema, tolerance rules, required checkpoints, and definition of reproduction completion are AUSENTE and must be specified before promotion to implemented behavior (`wender/learn/demonstrations.py:6-7`, `wender/learn/demonstrations.py:24-25`).

Treat missing expectations, unavailable checkpoint evidence, unsupported predicates, execution errors, ambiguous observations, or incomplete coverage as unverified. Completing an action sequence alone must not set `verified=True`. Default outcome flags to false until their separate required conditions are met; aggregate using the current strict-boolean arithmetic (`wender/learn/demonstrations.py:24-25`, `wender/learn/demonstrations.py:37-38`).

### 6. Enforce RAM ownership on every path

Retain decoded frames, crops, screenshots, selected label frames, expected visual references, and actual visual checkpoint captures only in bounded RAM buffers. Keep expectations alive until their verification completes, or deterministically rederive them from the same source. Release references on completion, error, cancellation, and exhausted budgets. If required data cannot remain available in RAM, record the segment as unverified and retain its denominator entry (`wender/learn/demonstrations.py:7`, `wender/learn/demonstrations.py:12-25`, `wender/learn/demonstrations.py:34-38`).

Prohibit disk spill in decoders, temporary files, screenshot artifacts, logs, traces, diagnostic bundles, and pixel encodings placed inside JSON. Apply backpressure or terminate the affected operation when RAM limits are reached. Enforcement for opaque adapters, runtime crash behavior, and any automatic disk-backed buffers is AUSENTE in the current code and must be established before claiming compliance for an implementation (`wender/learn/demonstrations.py:7`, `wender/learn/demonstrations.py:12-25`).

Allow only explicitly selected nonpixel metadata to cross any future persistence boundary: segment ordinals, declared identifiers, structured actions, checkpoint outcomes, and provenance/evidence descriptions. The actual store and its policies remain AUSENTE. Generic canonical serialization neither rejects pixel-bearing strings nor establishes permission to persist them (`wender/remember/skills.py:5`, `wender/remember/skills.py:34`, `wender/state/schema.py:25-39`).

### 7. Bind verification, admission, and honest reporting

Set `reproduced=True` only after the separately defined replay-completion condition. Set `verified=True` only after all required checkpoint predicates succeed against the fixed expected result. Reproduced-but-unverified segments must retain a false verification flag and therefore contribute zero to the numerator (`wender/learn/demonstrations.py:30-38`).

For a verified segment, derive skill checkpoints, provenance, `evidence.reproduced`, and the actual verifier description in `evidence.verified_by` from the same verified result. Run the proposed adapter's independent verification checks before calling `admit`. Neither a truthy verifier label nor successful JSON admission alone may manufacture the metric's `verified=True`; the two implemented field conventions are distinct (`wender/remember/skills.py:19-34`, `wender/learn/demonstrations.py:37`).

Aggregate a stable manifest-sized sequence exactly once per report. Include the qualifying count and total alongside integer ppm so a zero result can be interpreted, and disclose incomplete runs without representing them as complete cohort results. Do not remove failed or unverified segments, substitute action-completion rate for verified reproduction, or average independently rounded per-batch scores in place of the single count-based formula. These reporting protections are proposed; the current arithmetic remains `ok * 1000000 // total` (`wender/learn/demonstrations.py:34-38`).

## Findings

These findings distinguish implemented defects or gaps from future integration risks. All tests in this table are **PROPOSAL**; observed VM tests and probes are reported separately in the task report.

| Finding | Exact implementation point | Proposed test |
|---|---|---|
| The four public stubs lack individual anchors | `wender/learn/demonstrations.py:12`, `wender/learn/demonstrations.py:16`, `wender/learn/demonstrations.py:20`, `wender/learn/demonstrations.py:24`; the sole function marker is at `wender/learn/demonstrations.py:28` | Compare all five public function definitions with actual markers; identify the four current gaps. The present uniqueness/count test does not enforce public-symbol coverage (`tests/test_repo_conventions.py:32-36`). |
| The denominator and flags are trusted, not proven | `wender/learn/demonstrations.py:34`, `wender/learn/demonstrations.py:37-38` | Characterize one success plus two failures as 333333 versus a filtered success-only input as 1000000. For the proposed coordinator, reject cohort filtering and retain failed, unverified, and RAM-exhausted entries. |
| Admission and metric verification fields differ | `wender/remember/skills.py:27` versus `wender/learn/demonstrations.py:37` | Admit an otherwise valid record with truthy `verified_by` and no `verified`; show that this does not establish a qualifying metric record. Test the proposed adapter using real checkpoint success/failure, including reproduced-but-unverified rejection. |
| Strict booleans, floor division, and duplicates have limited existing coverage | `wender/learn/demonstrations.py:37-38`; current fixture at `tests/test_layers.py:88-92` | Use True/False/missing/integer-1/string-true flag cases; assert 333333 and 666666 for thirds; verify duplicate records count separately and proposed retry grouping does not duplicate manifest entries. |
| Malformed or unstable metric input is unguarded | `wender/learn/demonstrations.py:34`, `wender/learn/demonstrations.py:37` | Characterize a generator's `len` failure and a nonmapping element's `.get` failure. Separately specify any future domain validation; do not claim malformed inputs already become zero-contribution records. |
| No executed pipeline or runtime STORAGE LAW enforcement | Unconditional stubs at `wender/learn/demonstrations.py:13`, `wender/learn/demonstrations.py:17`, `wender/learn/demonstrations.py:21`, `wender/learn/demonstrations.py:25`; suffix-only guard at `tests/test_repo_conventions.py:38-41` | Assert every exact current stub message. For a future implementation, inject decoder/replay failures and RAM exhaustion; assert no pixel-write attempts and unchanged cohort membership. |

## Open questions mapped to HB-WA-a0-arch Q1 to Q6

Question names come from the bound architecture table. The S4 mappings identify dependencies of the proposal, without asserting behavior of unbound producers or executors.

| Question | LEARN mapping and unresolved decision |
|---|---|
| **Q1 — vision-engine entry point and frame source** (`hyperbooks/HB-WA-a0-arch/book.md:114`) | Blocks the concrete frame/event producer and RAM-lifetime contract. TARGET event categories exist, but the segmentation interface and implementation are AUSENTE (`wender/learn/demonstrations.py:4-5`, `wender/learn/demonstrations.py:12-13`). |
| **Q2 — AT-SPI tree walk into STATE** (`hyperbooks/HB-WA-a0-arch/book.md:115`) | Affects proposed desktop expected-state capture and checkpoint verification. No desktop capture or verification binding is implemented by the replay stub: AUSENTE (`wender/learn/demonstrations.py:24-25`). |
| **Q3 — per-STATE token budget** (`hyperbooks/HB-WA-a0-arch/book.md:116`) | Requires explicit context/frame-selection budgets and deterministic truncation or failure policies. Budget values and rejection behavior are AUSENTE in labeling and replay (`wender/learn/demonstrations.py:16-17`, `wender/learn/demonstrations.py:24-25`). |
| **Q4 — program-map persistence and merge policy** (`hyperbooks/HB-WA-a0-arch/book.md:117`) | Governs future durable relationships between state identities, skills, manifests, and verification evidence. Map persistence/merge and skill storage remain explicitly AUSENTE; only nonpixel metadata could cross a proposed store boundary (`wender/remember/program_map.py:5-6`, `wender/remember/skills.py:5`, `wender/learn/demonstrations.py:7`). |
| **Q5 — first responsibility in rust/** (`hyperbooks/HB-WA-a0-arch/book.md:118`) | Affects proposed frame capture/transport, RAM ownership, and backpressure. S4 supplies no Rust or shared-memory boundary, lifetime manager, or budget: AUSENTE at the segmentation stub (`wender/learn/demonstrations.py:12-13`). |
| **Q6 — cross-origin iframes and closed shadow roots in AX prune** (`hyperbooks/HB-WA-a0-arch/book.md:119`) | Affects completeness of proposed observed/expected web-state checkpoints. S4 defines no corresponding coverage or verification decision: AUSENTE. The proposal treats insufficient expected/actual coverage as unverified (`wender/learn/demonstrations.py:24-25`, `wender/learn/demonstrations.py:37`). |
