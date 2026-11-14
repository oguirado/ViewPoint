# MemoryWeb AI authoring guide

<a id="authoring-contract"></a>
## Authoring contract

Normative: [procedure](MEMORYWEB_PROTOCOL.md#end-to-end-procedure).
The AI analyses evidence and authors a site document draft and a declarative
workflow draft. Engines validate, execute and verify under current authorized
scope. Draft output is not a reproduction receipt. A schema pass, fixture pass
or AI assertion cannot admit, shadow or promote a workflow. `operator_demo`
names an evidence basis, not lifecycle admission. Unknown statistics stay null.
Read the narrowest relevant protocol anchor; do not load the entire corpus.

<a id="explanation-and-evidence"></a>
## Explanation and evidence

Normative: [explanation](MEMORYWEB_PROTOCOL.md#operator-explanation-template).
Ask for scoped, nonempty operator evidence under these exact headings, once in
this order. This fenced template contains guidance, never operator testimony:

```markdown
## Intent
State the authorized outcome.
## Starting state
Describe the starting surface and conditions.
## Variable slots
Describe private runtime values without including them.
## Ambiguous controls
Explain how intended controls differ from similar controls.
## Expected result
Describe an independently observable result.
## Exceptions
Describe safe stops and unsupported branches.
```

Map intent to observed controls and outcomes; preserve uncertainty. The site
document draft should contain scoped starting conditions, semantic controls,
state signals, exceptions, evidence provenance and unresolved questions. Keep
it derived and reviewed for its destination; no raw OCR dumps or private URLs.
The historical Stitch explanation artifact is AUSENTE — no inventar.

<a id="choose-representation"></a>
## Choose representation

Normative: [durability](MEMORYWEB_PROTOCOL.md#durability-verification-and-ram-only-storage).

| Rung | Evidence contract | Limit |
|---|---|---|
| T0 | RAM template | Visual evidence remains RAM-only. |
| T1 | Verified anchor and offset | Check frame consistency and tolerance. |
| T2 | Semantic mark kind, OCR label, locale and relative search region | Require unique fresh matching evidence. |
| T3 | Typed state or side effect | T3 alone cannot ground a click. |

Recorded points are observation hints, never layout-independent targets.
Reacquire after prompt growth, zoom, canvas rearrangement or tool changes.
Do not infer semantic labels or crop hashes from coordinates.

<a id="author-workflow"></a>
## Author workflow

Normative: [closed schema](MEMORYWEB_PROTOCOL.md#closed-json-schema-contract).
Use the installed closed schema: all required fields, no extra fields, integer
literals, canonical sorted compact JSON plus LF, contiguous sequences and
declared slots. Specify targets, preconditions, captures, verifiers, final
oracle and a bounded fallback. Explanation belongs here, outside closed JSON.
Paste uses a declared runtime slot, private transport and established focus.
Declare delivery budget, `empty_len_allowed` and `post_type_len_allowed`:
`exact_n` checks the inserted length; `initial_plus_n` includes initial text.
Chip cleanup is conditional, grounded and verified; order right-to-left, then
vertical position and identity for ties. Never embed slot values in argv or
traces. A drag declares either a grounded destination or signed displacement,
never both. This export-tail example contains no paste or drag.

Before dispatch, guidance re-evaluation can skip a step only through `when` or `already_satisfied`. Give non-idempotent steps an observable semantic `already_satisfied` predicate so that work the operator already completed is not acted on twice, such as clicking Export again. `when` permits an optional condition skip only when proven false; `already_satisfied` permits a satisfied skip only when proven true and admitted by the trusted satisfaction owner. UNKNOWN stops. Mandatory preconditions still fail closed, and protected required steps cannot be condition-skipped. Guidance or visual resemblance alone does not prove completion. After input has been sent, guidance re-evaluation re-verifies that input; it does not itself authorize a resend.

<a id="author-reply"></a>
## Author reply

Normative: [semantic gates](MEMORYWEB_PROTOCOL.md#semantic-gates-beyond-json-schema).
The author reply envelope contains exactly `{workflow, site_markdown, step_evidence}`.
Every association contains exactly `{seq, demo_seq, frame_sha256, mark_id, missing_reason}`.
These association fields belong outside the workflow schema.

| Field | Contract |
|---|---|
| `workflow` | Closed-schema workflow object retaining draft status and frozen constraints. |
| `site_markdown` | Site document draft as Markdown text. |
| `step_evidence` | Ordered associations, exactly one per workflow step [CITED, integrated candidate validator]. |
| `seq` | Corresponding workflow step sequence. |
| `demo_seq` | Selected evidence-pack step sequence, starting at one [CITED, integrated candidate validator]. |
| `frame_sha256` | Selected evidence step's before-frame digest, copied exactly: lowercase hexadecimal of length sixty-four [CITED, author response schema]. |
| `mark_id` | Primary target's OCR mark identifier converted to a string, from the selected frame. |
| `missing_reason` | Explicit missingness for T3-only steps; use `mark_id: null` instead of an invented mark. |

For a primary T2 target, `mark_id` must equal the selected mark's stringified
identifier and `missing_reason` must be null. T3-only authoring should supply
a meaningful missing reason. Enforcement of a closed vocabulary or nonempty
reason is AUSENTE — no inventar: the current T3 branch skips mark validation.

`rel_box_bps` is `[left, top, right, bottom]`, with integer basis-point
coordinates bounded by zero and ten thousand [CITED, integrated grounding validator].
Convert each pixel edge using integer floor division after multiplying by ten
thousand [CITED, integrated grounding validator]: horizontal edges by frame
width, vertical edges by frame height. The resulting mark box must be
**CONTAINED** in the region, including its boundary. Require exactly one
same-kind, exact-OCR-label mark fully contained in that region [CITED, integrated grounding validator].
Refuse absent, ambiguous, outside-region or missing-evidence grounding.
Ground each T2 fallback and destination independently by its own kind, label
and region. The association's shared `mark_id` belongs to the primary target.

<a id="verify-state"></a>
## Verify state

Normative: [snapshots and verifiers](MEMORYWEB_PROTOCOL.md#action-snapshot-and-verifier-semantics).
Distinguish a typed Boolean predicate from a baseline capture descriptor.
Use `capture_before` to capture history/download state before the action whose
effect is proved; compare with `delta_gt` or `set_grew` using the same kind,
capability and bindings. Existing downloads and old checkmarks cannot prove
new success. Input acceptance, submission, generation completion and export
completion require separate predicates. Submission uses history-entry growth;
completion uses `busy_predicate` observed before stable done observations.
Missing, stale, foreign or ill-typed observations remain UNKNOWN.
`fallback_verifiers` have at most one fallback level [TARGET], preserve the
postcondition, busy predicate, baseline and original deadline, and never send
input. Read retries consume the original budget, separately from action
retries. Uncertain input requires reconciliation before another input; never
resend to resolve uncertainty. Restart must reconcile journal and site state
without automatic replay or redispatch.

<a id="stitch-export-example"></a>
## Stitch export example

Normative: [semantic gates](MEMORYWEB_PROTOCOL.md#semantic-gates-beyond-json-schema).
Observation source: symbolic Stitch `demo2_steps.json` export, SHA-256
`4de6b0812f388b66380a574bf66ff2526d18da918b0aff02d541336ad2a33774`
[MEASURED, supplied export]. It contains 80 steps [MEASURED, supplied export];
marked controls report `OCR_UNAVAILABLE`. Their labels cannot be recovered.

| Lessons from CU_WEB_ERRORS.md | Authoring consequence |
|---|---|
| R1–R8 [CITED] | Separate delivery and verification; distinguish already satisfied, gestures, content dependence and waits. |
| R9–R15 [CITED] | Transport-aware paste budgets; exclude variable content; semantic modal/newest-screen targets; anchor translucent menus. |
| R16–R19 [CITED] | History completion, downloads growth, input effect and self-contained prompts. |
| R20–R22 [CITED] | Negative-test state checks, validate layout invariants and reconcile submission before another send. |

`examples/stitch_export_routine.workflow.json` proposes a focused semantic tail
of two steps [TARGET], not a reconstruction of historical intermediate inputs:
wait for the current admitted generation to finish, then click Download and
verify new download growth. Starting conditions require the current generation
still busy. The Download wording comes from the historical lesson. Button kind,
English locale, whole-viewport search region and every `mwl3-example-*`
capability are proposed local fixture requirements, not recovered OCR,
measured geometry or registered production capabilities. Live use requires
fresh unique grounding and trusted owners. The final oracle repeats exactly
the download `set_grew` postcondition with its pre-action baseline.

The proposed offline cohort definition is the canonical JSON below, without
the fence or trailing LF. `cohort_sha256` hashes its UTF-8 bytes:

```json
{"fixture_ids":["mwl3-example-completion","mwl3-example-download","mwl3-example-missing-target"],"task_ids":["mwl3-example-export-tail"]}
```

Oracle planned mutants are `stale-completion`, `pre-existing-download` and
`missing-target`. The killed count is zero [TARGET], and receipt references
are empty until real evidence exists. All statistics remain null. Synthetic
gate results are not live reproduction or executed runtime mutant receipts.

Provenance is `import`, not `human`: pin the exact completed guide bytes and
this extracted anchor span; `derived_from_sha256` identifies the export. The
recorded date is the guide authoring date, not the demo date. The provenance
owner attests imported bytes and evidence basis, never operator review.

The distribution basename intentionally fails the frozen filename identity
rule. Stage its unchanged bytes as `stitch-export-routine.json`, and stage
this guide beside it as `AUTHORING_GUIDE.md`. The public gate requires explicit
finite synthetic capability and provenance/ordering owners; missing owners
refuse. Tests perform this staging and validate through `validate_file` with
`ValidationContext`. CLI validation of the distribution basename must refuse;
do not weaken upstream identity rules.

<a id="request-focused-demo"></a>
## Request a focused demo

Normative: [decision policy](MEMORYWEB_PROTOCOL.md#reason-table-and-decision-policy).
Reuse eligible verified knowledge, generate a draft when evidence is sufficient,
request focused evidence when a contract is unresolved, or hand off at a human
boundary. A lexical miss alone does not prove absent knowledge. Preserve the
actual router reason, including null when lookup never occurred.
Use the closed `mw.demo_request.v1` contract: site, task, trigger_reason,
router_reason, decision_row `ask_for_demo`, missing_control_or_signal,
failed_step_seq, current_safe_state, inspected_node_ids, recording_scope and
requested_explanation_topics. Request only unresolved intent/control mapping,
unavailable semantic labels (`unlabeled_vision_control`), unsupported branches,
missing state signals or exhausted bounded repair. Explain the narrow safe
recording scope and required explanation topics. Login, CAPTCHA and
authorization boundaries require human handoff. Never request passwords,
tokens, payment details or private messages in a recording.

<a id="privacy-and-release"></a>
## Privacy and release

Normative: [publication](MEMORYWEB_PROTOCOL.md#local-and-public-folders-publication-and-script-execution).
Visuals, frames, crops and templates remain RAM-only. Keep private slots local;
do not ship credentials, prompts, raw OCR, private URLs, session IDs, absolute
host/RAM paths, pixel payloads or embeddings. Review derived prose and labels
for the destination too. Require exact-byte operator review before release;
schema validation cannot grant authority. Drafts remain drafts until the
lifecycle evidence is independently satisfied. Both script fields stay null:
the execution ruling is declarative-only; there is no contributed-script
backend here. A script reference cannot grant execution rights.

<a id="budgets-and-checks"></a>
## Budgets and checks

Normative: [limits](MEMORYWEB_PROTOCOL.md#artifact-limits-tags-and-script-capability-grammar).
Reject over-cap raw UTF-8 bytes before decoding/parsing; never clip.

| Item | Proposed constraint |
|---|---|
| Guide | 24,576 bytes [TARGET] |
| Workflow | 65,536 bytes [TARGET] |
| Explanation | 16,384 bytes [TARGET] |
| Locate wait / polling | 10,000 / 1,000 ms [TARGET]; wait ceiling 300,000 ms [TARGET] |
| Delivery | 1,000 ms [TARGET] per example action |
| Verification | 30,000 ms [TARGET], including delayed downloads |
| Observation polling | 1,000 ms [TARGET] minimum between polls |
| Stable completion | 2 consecutive polls [TARGET], after busy |
| Segmentation | hi=4, lo=1, quiet=3, min_len=2 [TARGET] |
| Gap / multiplier | 20,000 ms and 3/2 [TARGET], proposed metadata, not recorded timing |
| Runtime model calls in this item | None [TARGET] |

Locate, delivery and verification have separate finite limits. Missing runtime
owner/configuration blocks execution. Bound reads to the narrowest anchor and
retain the original deadline on retries. No live runs are performed here.
From the repository root, run the affected tests under the mandated resource
unit; the build invocation selects CU modules and `tests/scripts/` explicitly:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_authoring_guide
PYTHONDONTWRITEBYTECODE=1 python3 tools/reseal.py --check
```

Production capability registration, fresh Stitch OCR/geometry, reviewed
operator explanation and live reproduction receipts are AUSENTE — no inventar.
Protocol gaps remain explicit: BM25F calibration, complete capability/parameter
registry, script-enforcement backend, token accounting, audio handling,
atomic-switch implementation evidence, template-on-disk policy and calibrated
router anchor-choice accuracy are AUSENTE — no inventar. This guide is scoped
to the supplied protocol, architecture, Stitch symbolic export and lessons.
