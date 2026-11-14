# HB-WA-a0-arch - Wender architecture: PERCEIVE, ACT, REMEMBER, LEARN and the STATE contract

Reduced companion book (`hb.companion-format/v1`, see `hyperbooks/COMPANION_FORMAT.json`). It lives
beside the code. Every number is tagged `[MEASURED, <evidence>]`, `[CITED, <source>]` or `[TARGET]`;
unknowns are marked AUSENTE and are not invented. Ruling source: operator, campaign visual-qa-0.

## <a id="hb-s0"></a>0. Reading map

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","code_anchors":["wa.state.schema.StateError","wa.state.schema.canonical_actuation_resource"],"kind":"reading","n":0,"schema":"hb.section.v2","title":"Reading map"}
```
<!-- HB:END -->
Implementer: 2, 3, 6. Reviewer: 1, 5, 7. Planner: 7 (open questions).

## <a id="hb-s1"></a>1. What Wender is and is not

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","code_anchors":[],"kind":"contracts","n":1,"schema":"hb.section.v2","title":"Identity"}
```
<!-- HB:END -->
Wender is the hard layer of Computer Use for AI agents. It lets an agent operate any web page or
desktop program, known or unknown, without recording it first. It is not a browser: it drives
existing programs (Chromium via CDP, desktop apps via AT-SPI, apps with an API via that API) and
pairs with vision-engine for pixels. Name: a nod to Bender; brand assets are pending
(`docs/brand/README.md`).

## <a id="hb-s2"></a>2. PERCEIVE - the read ladder

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s2","code_anchors":["wa.perceive.ladder.choose_rung","wa.perceive.cdp_pipe.CdpPipe","wa.perceive.ax.prune","wa.perceive.browser.page_state","wa.perceive.atspi.window_summary"],"kind":"subsystems","n":2,"schema":"hb.section.v2","title":"PERCEIVE"}
```
<!-- HB:END -->
| Rung | Source | Served in Wender by |
|---|---|---|
| 1 | structured state / JSON-L / app API | app adapters (AUSENTE in v0) |
| 2 | DOM/CDP or AT-SPI accessibility tree | `perceive.cdp_pipe` + `perceive.ax` (web), `perceive.atspi` (desktop) |
| 3 | OCR / pixel diff | vision-engine: screen fingerprint + what changed per tile |
| 4 | vision model | multimodal model on vision-engine-selected crops only |
| 5 | blind coordinates | last resort |

Why vision is rung 4: ~1 token per 1024 px, a 4K screen ~8100 tokens [CITED, ML-a0-1]. Why the
STATE and never raw AX/HTML: raw axTree/HTML dumps of ~50000 tokens per turn destroy prefix
caching [CITED, ML-a0-2]. Browser transport: `--remote-debugging-pipe` (NUL-framed JSON on fd 3/4,
no TCP port). AX: `Accessibility.getFullAXTree`, pruned by `prune_ax_v1` (ported from the operator's
OpenFresnel `companions/browser/ax.py`), giving a reload-stable sha. Reload stability of the
fixture page sha and node ids [MEASURED, tests/test_integration_chrome.py, Chrome 154 headless].

## <a id="hb-s3"></a>3. The STATE contract (`wender.state/v1`)

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s3","code_anchors":["wa.state.schema.build_state","wa.state.schema.validate_state","wa.state.schema.dumps"],"kind":"contracts","n":3,"schema":"hb.section.v2","title":"STATE"}
```
<!-- HB:END -->
Deterministic canonical JSON: nodes (id, role, name, depth, parent, ordinal, states, actionable,
box), the actionable id list, `ax_sha256` (with state attrs) and `ax_stable_sha256` (without),
viewport integers and counts. Ids are sha256 of stable fingerprint + ordinal, hex[16]. Boxes are
integer CSS px and are never hashed. Full field list: `docs/STATE.md`; machine schema:
`wender/state/wender.state.v1.schema.json`. Size ceiling per STATE: AUSENTE (budget is TARGET,
to be set from measured pages).

## <a id="hb-s4"></a>4. ACT - most structured path first

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s4","code_anchors":["wa.act.actor.choose_path","wa.act.actor.ActorLock"],"kind":"subsystems","n":4,"schema":"hb.section.v2","title":"ACT"}
```
<!-- HB:END -->
Order: app API/MCP (e.g. Blender bpy) -> AX/DOM action -> coordinates. One actor per surface at a
time (a second actor is refused, not queued); readers run in parallel. Only AX match levels L0/L1
may authorise an irreversible action (`wa.perceive.ax.level_allows_irreversible`). Executors: AUSENTE.

## <a id="hb-s5"></a>5. REMEMBER - program map and skills

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s5","code_anchors":["wa.remember.program_map.ProgramMap","wa.remember.skills.admit"],"kind":"subsystems","n":5,"schema":"hb.section.v2","title":"REMEMBER"}
```
<!-- HB:END -->
(a) Program map = graph. Nodes are screens/states keyed by (vision-engine fingerprint, pruned AX
stable sha); edges are actions with pre/post and what changed per the engine. Built while
exploring. (b) Skills are parameterised intent-level procedures with verification checkpoints,
stored ONLY after reproduction is verified, as units with provenance and evidence.

## <a id="hb-s6"></a>6. LEARN - from demonstrations

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s6","code_anchors":["wa.learn.demonstrations.reproduced_share_ppm"],"kind":"subsystems","n":6,"schema":"hb.section.v2","title":"LEARN"}
```
<!-- HB:END -->
Video (e.g. expert Blender sessions) -> vision-engine segments (screen changes, panel changes,
cursor motion, keyframes) -> multimodal labels the intent per segment from few frames -> reasoner
maps to structured actions (bpy/MCP or AX) -> headless replay verified against the recorded result
-> stored skill. Honest metric: share of segments reproduced with a verified result (ppm).
Target value: AUSENTE (no baseline yet).

## <a id="hb-s7"></a>7. Decisions and open questions

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s7","code_anchors":[],"kind":"decisions","n":7,"schema":"hb.section.v2","title":"Decisions and open questions"}
```
<!-- HB:END -->
| Id | Decision / question | Status |
|---|---|---|
| D1 | Stack: Rust low level, JS in-page, Python agent layer | ruled |
| D2 | CDP over pipe, never a TCP debugging port | ruled, implemented |
| D3 | STATE only into model context | ruled, implemented |
| D4 | No frames on disk (STORAGE LAW) | ruled |
| Q1 | vision-engine entry point and frame source for the bridge | AUSENTE |
| Q2 | AT-SPI tree walk into STATE (desktop rung 2) | AUSENTE |
| Q3 | Per-STATE token budget | TARGET, value AUSENTE |
| Q4 | Program map persistence and merge policy | AUSENTE |
| Q5 | What goes into rust/ first (frame capture? shm transport?) | AUSENTE |
| Q6 | Cross-origin iframes and closed shadow roots in the AX prune | AUSENTE |

## <a id="s10-ownership"></a>10. Serialized script ownership and acceptance

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-ownership","code_anchors":[],"kind":"decisions","n":10,"schema":"hb.section.v2","title":"Serialized script ownership and acceptance"}
```
<!-- HB:END -->
S9 hands ROUTE to S10 after code integration. Book/index edits and ROUTE assertions serialize; runtime implementations and owner suites remain frozen; the Conductor ruling permits dependency-header corrections only. Exact source first-line metadata is authoritative. Logical generated artifacts use artifact and step identities; disk file coordinates apply only to repository code.

| Module | Book | Owner | Layer |
|---|---|---|---|
| `wa.perceive.script_observation` | `HB-WA-s1-perceive` | PERCEIVE | 2 [CITED: `wender/perceive/script_observation.py:1`] |
| `wa.perceive.step_binding` | `HB-WA-s1-perceive` | PERCEIVE | 2 [CITED: `wender/perceive/step_binding.py:1`] |
| `wa.perceive.redact` | `HB-WA-s1-perceive` | PERCEIVE | 2 [CITED: `wender/perceive/redact.py:1`] |
| `wa.perceive.element_table` | `HB-WA-s1-perceive` | PERCEIVE | 2 [CITED: `wender/perceive/element_table.py:1`] |
| `wa.perceive.disambig` | `HB-WA-s1-perceive` | PERCEIVE | 1 [CITED: `wender/perceive/disambig.py:1`] |
| `wa.scripts.act_api` | `HB-WA-s2-act` | ACT | 3 [CITED: `wender/scripts/act_api.py:1`] |
| `wa.scripts.frame_wait` | `HB-WA-s2-act` | ACT | 3 [CITED: `wender/scripts/frame_wait.py:1`] |
| `wa.act.proto1_action_policy` | `HB-WA-s2-act` | ACT | 3 [CITED: `wender/act/proto1_action_policy.py:1`] |
| `wa.learn.script_ledger` | `HB-WA-s3-remember` | LEARN | 2 [CITED: `wender/learn/script_ledger.py:1`] |
| `wa.learn.script_integration` | `HB-WA-s3-remember` | LEARN | 3 [CITED: `wender/learn/script_integration.py:1`] |
| `wa.learn.dream_journal` | `HB-WA-s3-remember` | LEARN | 2 [CITED: `wender/learn/dream_journal.py:1`] |
| `wa.learn.cu_memory` | `HB-WA-s3-remember` | LEARN | 2 [CITED: `wender/learn/cu_memory.py:1`] |
| `wa.learn.cu_loop` | `HB-WA-s3-remember` | LEARN | 2 [CITED: `wender/learn/cu_loop.py:1`] |
| `wa.scripts.types` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/scripts/types.py:1`] |
| `wa.scripts.header` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/scripts/header.py:1`] |
| `wa.scripts.ast_gate` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/scripts/ast_gate.py:1`] |
| `wa.scripts.compiler` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/scripts/compiler.py:1`] |
| `wa.scripts.predicates` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/scripts/predicates.py:1`] |
| `wa.scripts.runner` | `HB-WA-s4-learn` | LEARN | 3 [CITED: `wender/scripts/runner.py:1`] |
| `wa.learn.script_store` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/script_store.py:1`] |
| `wa.learn.catalogue` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/catalogue.py:1`] |
| `wa.learn.resolver` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/resolver.py:1`] |
| `wa.learn.dream_replay` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/dream_replay.py:1`] |
| `wa.learn.script_promotion` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/script_promotion.py:1`] |
| `wa.learn.context_router` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/context_router.py:1`] |
| `wa.learn.script_generator` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/script_generator.py:1`] |
| `wa.learn.script_policy` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/script_policy.py:1`] |
| `wa.learn.script_metrics` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/script_metrics.py:1`] |
| `wa.learn.cascade` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/cascade.py:1`] |
| `wa.learn.calibrate` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/calibrate.py:1`] |
| `wa.learn.memory` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/memory.py:1`] |
| `wa.learn.model_client` | `HB-WA-s4-learn` | LEARN | 2 [CITED: `wender/learn/model_client.py:1`] |

Generated write inventory: each book manifest, plus `out/code_index/anchors.jsonl`, `hb.json`, `cols.json`, `post.json`, `manifest.json`, and `ID`. Reseal before rebuilding. Existing mathlinks and companion configuration remain sealed. The builder preserves integer serialization and source/output hashes. Its per-file ceiling remains 1572864 bytes [CITED: `tools/build_code_index.py:21`].

| Judge fix | Implemented source | Gate mapping | Unproven portion |
|---|---|---|---|
| J-01 | `wender/scripts/types.py`; `wender/perceive/script_observation.py`; `wender/scripts/act_api.py` | G-PRINCIPAL, G-SCOPE, G-FENCE | Cross-repository/live environment proof: AUSENTE |
| J-02 | `wender/perceive/redact.py`; `wender/learn/context_router.py`; `wender/learn/script_ledger.py` | G-DECLASSIFY, G-META-POISON, G-PRIVACY | Cross-repository/live environment proof: AUSENTE |
| J-03 | `wender/scripts/ast_gate.py`; `wender/scripts/compiler.py` | G-AST, G-PARSER-CAPS, G-RESOURCE | Cross-repository/live environment proof: AUSENTE |
| J-04 | `wender/learn/script_store.py`; `wender/learn/script_policy.py` | G-AGGREGATE, G-ADMISSION-FLOOD | Cross-repository/live environment proof: AUSENTE |
| J-05 | `wender/scripts/frame_wait.py`; `wender/learn/script_store.py`; `wender/learn/script_ledger.py` | G-HOLDER-HANG, G-COMPACT, G-DRAIN | Cross-repository/live environment proof: AUSENTE |
| J-06 | `wender/learn/script_metrics.py`; `wender/scripts/frame_wait.py` | G-E2E-LATENCY, G-WAIT-PACING | Cross-repository/live environment proof: AUSENTE |
| J-07 | `wender/learn/script_integration.py`; `wender/learn/script_ledger.py` | G-RESTART-UNCERTAIN | Cross-repository/live environment proof: AUSENTE |
| J-08 | `wender/learn/context_router.py`; `wender/scripts/act_api.py` | G-INVALIDATE | Cross-repository/live environment proof: AUSENTE |
| J-09 | `wender/learn/catalogue.py`; `wender/learn/resolver.py`; `wender/learn/context_router.py`; `wender/learn/dream_replay.py` | G-DETERMINISM | Cross-repository/live environment proof: AUSENTE |
| J-10 | `wender/learn/script_promotion.py`; `wender/learn/script_integration.py` | G-CLASSIFY, G-SUSPEND | Cross-repository/live environment proof: AUSENTE |

Acceptance requires complete executed positive/negative gate controls with intended reasons, owner inventory, inputs and provenance. Discovered names alone are not evidence. G-SPRT remains conditional on explicit statistical policy; enabled policy evidence is AUSENTE. External VE execution and live-backend improvement evidence are AUSENTE. Historical decisions and open questions above remain in force unless independently resolved by source. The Conductor ruling makes gate inventory static: immutable contracts identify owner suites, exact loadable unittest methods, and rejecting fixture expressions in each method source. Inventory never executes owner suites or collects runtime evidence. Missing tests, absent fixture labels, an empty mapping, and fabricated zero-input completion records reject. Owner-suite execution is verified separately; static mappings cannot certify runtime behavior or input counts. External G-FRAME and G-RELEASE evidence remains AUSENTE and blocks cross-repository acceptance.

Dependency headers declare module-level internal imports, including imports nested in module-level try/if/with or class blocks. Function-local deferred imports obey layer direction but do not create static dependency edges. The deferred catalogue-to-resolver import therefore needs no header edge; the resolver-to-catalogue declaration remains acyclic. No undeclared-dependency exemption list is permitted. Unique IDs, resolved edges and graph acyclicity remain enforced by the ROUTE controls.

This mandate is limited to the supplied design, fix log and local integration corpus.

## <a id="s10-gate-inventory"></a>11. Mandatory gate inventory and unresolved owner evidence

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-gate-inventory","code_anchors":[],"kind":"decisions","n":11,"schema":"hb.section.v2","title":"Mandatory gate inventory and unresolved owner evidence"}
```
<!-- HB:END -->
The following design predicates retain their owner suites. Executable method discovery does not authenticate fixture outcomes. Per-fixture completion, intended rejection, owner inventory and input/provenance evidence remain required; complete cross-owner acceptance is AUSENTE.

| Gate / owner | Predicate | Required rejection fixtures | Positive control |
|---|---|---|---|
| G-SOURCE / LANG | Only bounded supported source/header ABI enters parsing | Binary/code object, invalid UTF-8, NUL, oversized bytes, duplicate/unknown header key or wrong ABI → typed rejection before capability acquisition | Minimal complete wrapper/header admits; parser setup proven healthy. |
| G-AST / LANG | Whole AST obeys exact closed grammar | Import, loop, branch-hidden call, alias, dunder/getattr, f-string, comprehension, bool timeout, duplicate dict key → AST/schema rejection; never execute prefix | Straight-line find/type/wait lowers to exact expected plan; instrumentation raises on any generated-source execution/import/shell path yet valid admission passes. |
| G-PLAN / LANG | Both source and direct-plan ingress enforce pairing/immutability/goal | Unknown opcode, mutable nested node, missing final checkpoint, TRUE-only postcondition or missing terminal goal → reject | Both ingress forms canonicalize the same finite paired semantics. |
| G-PIN / LANG | Full recomputed artifact/ABI/limits bind certificate and registry | Mutate selector/ceiling/slot schema under old pin; short/placeholder pin; forged ValidatedPlan → identity rejection | Independent canonical recomputation and registered validation agree. |
| G-RESOURCE / LANG+FRAME | Admission/parser/literal/wait ceilings bound work and clean every owner | Deep/oversized AST, parser process stall, cancellation with active borrow or absent supervisor cap → bounded rejection/terminate/join; no surviving worker/input | Near-boundary valid source compiles; supervised local parser terminates and releases on injected timeout; frame cleanup remains exact. |
| G-ARGS / LANG | Keys, exact types, bounds and episode/purpose slots proven | Missing/extra key, bool-as-int, colliding normalized name, literal typing payload, foreign slot → reject before send | Valid declared task slot resolves only at trusted act boundary. |
| G-PRED / LANG | Three-valued predicates and independent vetoes preserve missingness | Missing enabled/complete scope presented as false then negated to true; veto hidden by short-circuit → UNKNOWN/refusal | Complete exact scope establishes TRUE; explicit FALSE remains FALSE; negated UNKNOWN remains UNKNOWN. |
| G-FACTS / OBS+ACT | Action readiness derives from the proper independent wire source | Presentation FOCUSED/READ/ACKED word as AX authority; visible-only row; absent generation/layout; contradictory or busy facts → reject | Same display row with genuine current AX witness/action support and explicit nonbusy scope binds. |
| G-SCOPE / OBS | Unique/last/absence/answer ownership use complete relevant inventory | Rival Copy omitted from projection; parent chosen for leaf action; wrong answer/code/user turn; blocker missing → refuse | Complete last-answer-owned leaf/read container binds only within authorized task scope. |
| G-JOIN / OBS | Required semantic/pixel association is certified and unambiguous | Equal competitor/cycle, stale mark, wrong window/transform, absent transform defaulted to identity → reject | Certified well-separated same-publication role-compatible pair retains competitor/margin evidence. |
| G-FENCE / ACT | Final custody gate serializes revocation, STOP, focus, target and budget | Pause after prepare with a deterministic barrier; revoke lease or change focus/target/generation/STOP/deadline before releasing barrier → no new input | Identical barrier without mutation dispatches once through minted admission and real fence fixture. |
| G-RISK / ACT | Host grant/classification dominates source hints | Script calls submit/delete harmless; Enter hides submit; billing/account target outside grant → refuse | Explicitly permitted nondestructive control and closed keys dispatch. |
| G-POST / ACT | Applied effect, fresh causal observation, scope-specific semantic proof and settle all required | Generic animation/digest change; old frame; missing APPLIED; same-length replacement proved only by growth; wrong-turn Copy → block, revoke | Declared exact semantic transition with correct owner and quiet evidence seals once. |
| G-NORETRY / ACT | Consumed action cannot resend after lost/unknown completion | Input callback entered then raises/timeouts; duplicate completion; first postcheck FALSE → second effect and fallback callbacks never called | One APPLIED completion and verified checkpoint advances exactly once. |
| G-END / ACT | Trusted terminal TRUE establishes success | Local checks pass but task goal FALSE/UNKNOWN; failed click relabeled already done → no success seal | Valid full plan reaches goal, or entry already-complete emits honest no-input seal without support credit. |
| G-LEASE / STORE | Exact registered owner retains/releases; active waits keep bytes | Forged/foreign token, duplicate release, unresolved callback, eviction of retained object → no foreign decrement/free | Episode/replay/context owners release in any order; final unpin starts grace. |
| G-IDLE / STORE | No holders/inflight plus complete idle grace at atomic removal | Advance fake clock during held wait; release old object then sweep before grace; acquire-vs-sweep barrier → refuse eviction | After final release and full fake-clock grace, only eligible victim drains all body ownership. |
| G-CAPACITY / STORE | Source/decoded/snapshot/support bytes are charged and bounded | Small canonical record with oversized retained structures; all victims pinned; missing cap; generation pins exhausted → reject load | Eligible eviction makes room; deep retained inventory matches accounting/high water. |
| G-RELOAD / STORE+ACT | Successor affects future selection, not pinned cursor; revoke dominates | Publish successor mid-wait then splice next step; revoke old and try send via stale prefix → refuse | Healthy old exact lease finishes, next episode acquires successor, old bytes later drain. |
| G-RESOLVE / STORE | Exact scope/full coverage/unique current eligibility required | Empty scope, absent goal, tie, keyword spam, overflow, shadow/revoked/nonresident or fake CALIBRATED → `vacio` with reason | Unique promoted resident exact match → `completo`; acquire still rechecks. |
| G-PUBLISH / STORE | Entry/index/certificates/probe results publish atomically | Fault each staging/probe/pointer seam; empty bank; expected abstention resolves → old generation remains | Nonempty positive/negative probe bank passes; old and new readers each see coherent snapshot. |
| G-EVENT / AUDIT | Intent/terminal capacity precedes effect; truthful dedup after effect | Queue full before input; append failure after possible input; changed bytes under same event ID → no send or stop/quarantine respectively | Identical repeated event idempotent; sealed action counts once; missing usage remains null. |
| G-SPOOL / AUDIT | Only finalized bounded owner-validated regular records admitted | Symlink/swap, wrong owner, huge nested JSON, malformed/list ID mixed with valid spool → poison isolated | Valid records continue under batch cap and deterministic dedup. Use temporary local test root, no daemon. |
| G-JOURNAL / AUDIT | Bounded strict chain/head/payload and trusted host seal required | Changed payload, forged rewritten chain/head without seal, append/head crash, absent exit/score, huge valid prefix → no support | Complete bounded explicit sealed episode loads; incomplete tail is classified without becoming success. |
| G-RECOVER / AUDIT | Owner restart cannot resurrect unattested promoted status | Surviving header/spool says promoted but registry/certificate/body missing → unavailable | Independently revalidated supplied artifact and authenticated evidence rebuild coherent state. |
| G-REPLAY / DREAM | Only exact supported revealed transitions through terminal goal | Off-support action, colliding short key with different full action, conflicting sufficient state, future label access, supported prefix only → insufficient/corrupt | Complete synthetic sealed supported plan reproduces semantic digest on repeated runs with model/input/process/network spies untouched. |
| G-UTILITY / DREAM | Explicit paired units/threshold, no per-episode regression and strict gain | Missing cost → zero, tie, mean hides regression, fabricated quality gain or unsupported IPS → reject | Hand-computable rational utility fixture strictly improves declared objective with no regression. |
| G-SUPPORT / DREAM | Independent host-attested support and later shadow cohorts | Same episode replayed/copied, duplicate callback/no-op/prediction agreement counted, cohort overlap → no credit | Distinct registered seals and frozen support then fully evidenced subsequent shadow meet policy. |
| G-PROMOTE / DREAM | All identity/static/mutant/dream/utility/shadow/probe/CAS predicates true | Omit mutant, change suite after certificate, UNKNOWN postcondition, stale predecessor or insufficient cohort → no publication | Complete synthetic certificate chain promotes exact successor; registered mutant demonstrably rejected for intended predicate. |
| G-SUSPEND / DREAM+ACT | Immediate local stop plus classified synchronous denial | Proven artifact defect then dropped broadcast and another session send → exact digest blocked; outage cannot masquerade as defect | Healthy unrelated artifact stays eligible; diagnosed transient outage reinstates only after recovery evidence and reconciliation. |
| G-SHADOW / DREAM+ACT | Draft/shadow have no live capabilities | Present shadow to act bridge or attempt service acquisition inside recorded driver → reject/spies untouched | Passive fully evidenced prediction emits intentions/results without input or inference. |
| G-FRAME / VEFRAME | Publication/source/digest/CRC/postcopy identity owns returned bytes | Odd/torn sequence, wrong CRC, moved publication, forged residency, source mismatch → committed-frame rejection | Coherent synthetic capture matches trusted publication/digests; release callback runs on success. |
| G-RELEASE / VEFRAME+FRAME | Every capture/image/wait failure drains exactly its owned borrows | Inject digest/encode/cancel exception after borrow; retained fake registry detects leak → gate rejects | Faults at each ownership seam release exactly once; unrelated retained image stays valid. |
| G-WAIT / FRAME | Fresh semantic predicate and all applicable settle signals within bounds | Duplicate publication earns quiet credit; busy/missing signal; quiet wrong answer; expired input authority → timeout/UNKNOWN/refusal | Fresh causal frames, correct turn and all required quiet evidence satisfy configured window. |
| G-DEADLINE / FRAME | Stalled acquisition/provider cannot run beyond enforced authority | Nonadvancing source or ignored cancellation; excessive poll count → bounded stop, no later input | Injected clock crosses deadline/cancel; callbacks terminate/join and release. Near-boundary valid observation passes. |
| G-TICKS / FRAME | Half-open cursor emits each due event once | Equality-only scheduler misses skipped event; inclusive ends duplicate event → mismatch detected | Sparse tick jump yields all due events once; zero advance yields none. |
| G-IMAGE / OBS | Reasons and same-frame clean/overlay/crops preserved | Lone relevant unnamed icon classified tags-only; overlay from old publication; mask removes necessary evidence → request images or refuse | Fully grounded named dialog tags-only; unknown canvas requests safe images without granting a script click. |
| G-PRIVACY / OBS+CONTEXT+AUDIT | Public artifacts/prompts/events contain no forbidden content or reusable private hash | Unique canaries in slots/history/answers/nested keys/exception/comment/image pixels; instruction-like labels → unsafe draft/payload rejected, no control change | Safe structural labels survive encoding, private slot reaches actuator only, authorized result reaches task consumer only. |
| G-PREFIX / CONTEXT | Entire reusable request prefix frozen per epoch | Capability/image order/live counter/receipt inserted before boundary; delayed response requests revoked digest → mismatch or current-authority refusal | Distinct suffix observations preserve exact static bytes; policy change opens explicit epoch. |
| G-PAGER / CONTEXT | Mandatory evidence fits; optional paging deterministic | Drop deny/uncertainty/latest postcondition to fit; omit active checkpoint → CONTEXT_INSUFFICIENT/no call | Remove only optional cards/history; exact rendered budget and mandatory inventory pass. |
| G-DELTA / CONTEXT | Exact acknowledged retained base required | Missing base/checkpoint/epoch/generation or omitted removal accepted → full reground required | Matching base plus explicit removals reproduces sufficient current state. |
| G-GENERATE / CONTEXT | Draft provenance contiguous, pinned and privacy approved | Remove retry to join fragments, fabricate historical sidecar, mix image/tag observations, model supplies promoted status → reject provenance/status | Complete synthetic segment and fake Muse response produce inert draft admitted by A/B, no input. |
| G-CALIBRATE / ROUTE | Actual model/config/artifact evidence precedes distribution gates | Self-reported calibrated label, missing held-out records/split, changed tokenizer, empty conformal set → unsupported automatic inference | Pinned valid synthetic calibration artifact/distribution and common target proof authorize eligible option. |
| G-METRICS / ROUTE | Cache/latency/usage reports preserve real denominators and missingness | Byte-identical prefix labeled provider hit, script-only success counted as cached call, missing timestamps/usage treated as zero → metric rejection | Synthetic provider counters and monotonic timestamps yield hand-computed prefix-eligible/all-input fractions and TTFT; absent fields remain null with reasons. |
| G-COST / ROUTE | Comparable explicit units/probability convention; safety precedes rank | Missing cost coerced zero; unconditional reach multiplied twice; cheapest unsafe script selected → reject | Exact rational hand-calculated fixture ranks only eligible routes; null cost remains unknown. |
| G-DRIFT / ROUTE | Required target mutation/UNKNOWN stops before effect | Rename/move target while global signature stays similar; old coordinates reused → suspend/no input | Unchanged exact target with fresh complete binding proceeds; explicit planned transition rebinds only after checkpoint. |
| G-CASCADE / ROUTE | Ordinary no-input miss routes onward; possible-input failure cannot | SCRIPT callback sends then loses completion and TEXT/MUSE repeats → fixture observes forbidden second input | Not-sent lookup miss uses another eligible lane once; post-input uncertainty enters reconciliation only. |
| G-ANCHOR / ROUTE+VEFRAME | Every public definition routes to exact file/symbol/book with valid deps | Missing/duplicate anchor, wrong stage/owner, stale index or unrelated lexical hit → fail | Rebuilt temporary index routes every public addition and modified anchor to exact intended coordinates/stage. |
| G-DRAIN / ROUTE | Repeated run/wait/reload/cancel drains real owned inventory | Leak replay/context holder/callback while counters claim zero; force-evict leased plan → detect mismatch | 100 cycles [TARGET] with failures at ownership seams drain handles/FDs/tmpfs/tasks after grace; retained cache and allocator trend reported separately. |

Additional mandatory revision gates: G-PRINCIPAL, G-DECLASSIFY, G-META-POISON, G-PARSER-CAPS, G-AGGREGATE, G-ADMISSION-FLOOD, G-HOLDER-HANG, G-COMPACT, G-E2E-LATENCY, G-WAIT-PACING, G-RESTART-UNCERTAIN, G-INVALIDATE, G-DETERMINISM and G-CLASSIFY. Judge mappings above identify their implemented owner sources. G-SPRT requires explicit enablement; no enabled statistical policy is established here.

The source import audit requires zero undeclared module-level internal imports in the integrated module set. Dependency-header corrections cover script_observation, step_binding, redact and proto1_action_policy. Catalogue retains its original static dependencies; its deferred resolver import is checked for layer direction without introducing a static cycle. Legacy proto1 public omissions remain unchanged and are explicitly bounded in the ROUTE inventory; newly integrated anchored methods are covered. Historical constant anchor aliases identify their actual following assignments, not invented symbols. External VE frame/release evidence, complete authenticated owner-fixture records and live environment capability proof remain AUSENTE.

The Conductor ruling makes gate inventory static: immutable contracts identify owner suites, exact loadable unittest methods, and rejecting fixture expressions in each method source. Inventory never executes owner suites or collects runtime evidence. Missing tests, absent fixture labels, an empty mapping, and fabricated zero-input completion records reject. Owner-suite execution is verified separately; static mappings cannot certify runtime behavior or input counts. External G-FRAME and G-RELEASE evidence remains AUSENTE and blocks cross-repository acceptance.

Dependency headers declare module-level internal imports, including imports nested in module-level try/if/with or class blocks. Function-local deferred imports obey layer direction but do not create static dependency edges. The deferred catalogue-to-resolver import therefore needs no header edge; the resolver-to-catalogue declaration remains acyclic. No undeclared-dependency exemption list is permitted. Unique IDs, resolved edges and graph acyclicity remain enforced by the ROUTE controls.

This mandate is limited to the supplied design, fix log and local integration corpus.

The scoped test-source fixture proves that both source scanners previously omitted the serialized ROUTE module: exact lookup abstained and book admission rejected its real test anchors. The source inventories in `tools/build_code_index.py` and `tools/anchors.py` now include only `tests/scripts/test_routing_integration.py` in addition to their existing patterns. No general tests-tree scan is enabled. Removing either inclusion is rejected by the public routing/scanner control.
