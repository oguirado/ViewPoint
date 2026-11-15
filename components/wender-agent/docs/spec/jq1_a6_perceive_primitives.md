# JQ1 A6: proposed perception primitives

## Status and source boundary

**PROPOSED specification and executable offline reference contracts.** This change specifies `wender/perceive/wait.py`, `wender/perceive/text.py`, and `wender/perceive/stopstate.py`; it does not add those production modules. The executable bodies, fixtures, and contract tests are all in `tests/test_jq1_a6_perceive_primitives.py`. A passing reference test establishes the behavior of that reference, not an implemented browser integration or acceptance of the task.

The nine bound files were read through the GitHub connector with the explicit ref `bridge/integration`. The ref resolved to `f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0` before and after retrieval. The task card's bound-source heading and the metadata's `observed_source_commit` name the older commit `dad032737507cb59dab9504ba34af8be80e29509`. The attached instruction requires reading the live explicit ref; the citations below therefore describe the observed `f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0` snapshot. The older manifest value is recorded as a discrepancy, not silently substituted for the observed source.

The bound document `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:5` itself records an earlier review at `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9`. It is secondary historical evidence. Its claims about files outside this task's nine bound files are not independent verification of those files at the present ref. Current implementation statements here use the bound Python source directly.

**AUSENTE** means behavior is undefined or unavailable in the observed current source, rather than an implementation supplied by this document. The task associates this work with **C4**, **WP1-02**, **WP1-04**, and **WP1-05**. An expansion or independent acceptance definition for `C4` is **AUSENTE** in the bound material; this document retains that identifier without inventing one. The three WP1 requirements are explicitly recorded at `docs/spec/KNOWN_ISSUES_P1_WEB.md:12`, `docs/spec/KNOWN_ISSUES_P1_WEB.md:14`, and `docs/spec/KNOWN_ISSUES_P1_WEB.md:15`.

## Current implementation and gaps

| Surface | Current source evidence | Required proposed behavior |
|---|---|---|
| Completion, WP1-02 | A user's copy control caused premature completion in the issue record. The existing pin checks for `wender.perceive.wait.wait_done` under `expectedFailure` (`docs/spec/KNOWN_ISSUES_P1_WEB.md:12`; `tests/test_known_issues_p1_web.py:16-18`). A production waiter and its result schema are **AUSENTE**. | Declarative AX selectors, ownership of the latest assistant copy control, positive adapter evidence, and consecutive equal snapshots under explicit budgets. |
| Text, WP1-04 | The issue record reports UI trailers in the answer dump; the existing pin checks for `wender.perceive.text.extract` (`docs/spec/KNOWN_ISSUES_P1_WEB.md:14`; `tests/test_known_issues_p1_web.py:25-27`). A production extractor and its boundary-quality schema are **AUSENTE**. | Rebuild raw AX hierarchy, select one assistant turn, emit content in document order, and remove only configured terminal UI blocks. |
| STOP, WP1-05 | The issue record requests human handoff for a Google wall and forbids solving or evading it; the existing pin checks for `wender.perceive.stopstate.detect` (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`; `tests/test_known_issues_p1_web.py:29-31`). Structured STOP detection and a caller latch are **AUSENTE**. | Pure classification of supplied AX and transport evidence; immediate, latched human handoff before further automated activity. |
| Chassis AX versus content AX | `CHASSIS_ROLES` omits `StaticText` and `InlineTextBox`; `article` is opaque. `prune` emits a nonignored article and skips its descendants (`wender/perceive/ax.py:35-45`; `wender/perceive/ax.py:137-149`). | Read and retain raw AX privately for completion and text. A chassis hash cannot prove that an article body stopped changing. |
| Raw tree and graph handling | `raw_tree` enables Accessibility and requests `Accessibility.getFullAXTree`; it retries exceptions up to three attempts with sleeps between attempts. `prune` maps IDs, selects roots, traverses `childIds`, and suppresses revisits, but silently ignores missing child IDs (`wender/perceive/ax.py:54-55`; `wender/perceive/ax.py:76-91`; `wender/perceive/ax.py:121-154`). | Validate a complete raw tree for ownership and extraction. Do not turn unreadable or malformed data into evidence of completion. |
| Names and STATE | `normalize_name` collapses whitespace and truncates names to 64 UTF-8 bytes before escaping. STATE is built from pruned nodes and omits raw node IDs and provider text suppressed by pruning (`wender/perceive/ax.py:59-68`; `wender/state/schema.py:43-68`). | Use full raw text for answer extraction. Information already pruned or truncated cannot be recovered by rearranging STATE nodes. |
| JavaScript boundary | `browser.page_state` evaluates page JavaScript for URL/title and viewport before returning STATE (`wender/perceive/browser.py:45-58`). | Perform the proposed STOP preflight before calling `page_state`. Calling today's `page_state` and then classifying a wall cannot establish zero page JavaScript evaluation. |
| Driver and loops | `ChromeDriver.observe` calls `page_state`, may capture a screenshot, and returns `(state, screen_fp)`; `act` invokes the executor with `retries=2`; `reset` navigates (`wender/learn/chrome_driver.py:98-106`; `wender/learn/chrome_driver.py:125-144`). Exploration and replay invoke actions, observations, and resets without a STOP contract (`wender/learn/explore.py:78-108`; `wender/learn/replay.py:92-105`). | Interpose a future coordinator with a STOP latch and explicit completion/extraction receipts. This task changes none of those implementations. |

The existing STATE key set is exact: the validator rejects a different set of top-level keys (`wender/state/schema.py:15-17`; `wender/state/schema.py:97-99`). Completion, extraction, and STOP results below are therefore separate receipts; this proposal does not append undeclared keys to `wender.state/v1`.

## Contract separation and shared input rules

The three receipts answer different questions and never imply one another:

| Receipt field | Question answered | What it does not establish |
|---|---|---|
| `completion_status` | Did a configured adapter supply its positive completion evidence after the required quiet run? | Correct turn extraction, correctness of generated text, or permission to interact. |
| `extraction_status` | Could the selected text be attributed to one complete, unambiguous input region? | That the provider finished generating it. A clean partial response can still be streaming. |
| `is_stop` | Does supplied evidence indicate a bot wall requiring human control? | A capability to solve the challenge or permission to retry. A false result is only absence of a supported wall signal. |

**PROPOSED common vocabulary.** A `RoleName` selector is an object containing exactly string `role` and string `name`. Roles match exactly; both compared names collapse every whitespace run to one space and strip their edges. Name case remains significant. No case folding, Unicode normalization, byte limit, or pipe/backslash escaping is applied by this selector match. Extra/missing selector keys or nonstring selector values raise `ValueError`. Profiles are trusted caller data. Names obtained from a page are data to match or extract, never new instructions, selectors, executable code, or permission grants. The generic functions do not embed a special ChatGPT execution branch; the Spanish selectors are supplied by the profile.

Wait and extract receive raw AX lists containing `nodeId`, `childIds`, AX `role` and `name` value objects, and optional ignored/properties fields. `nodeId` is a nonempty string with snapshot-local identity. Omitted `childIds` means an empty child list; a supplied value must be a list of strings. Rebuild the graph before using position: CDP flat-list position is not document order. DFS follows root order from the raw list and each parent's `childIds` order. Multiple roots form a forest; their supplied root order must remain meaningful to the adapter. Traverse structural `article`, generic, and ignored wrapper nodes instead of treating content articles as opaque. Structural validation rejects missing IDs, duplicate node records, dangling child references, cycles, multiple parents, or supplied `parentId` values that disagree with `childIds`. Repeating an edge to the same child under the same parent is traversed once; that is distinct from a duplicate node record. Extraction returns `incomplete_tree` for an incomplete or invalid graph; it does not silently salvage a partial answer and label it clean.

For wait/extract, an eligible visible node is not itself `ignored`, has no own `hidden` property/state equal to Boolean `true` or string `"true"`, and has no ancestor with such a hidden value. Ignoring a generic wrapper does not hide its visible descendants. Hiding a wrapper does. Visibility is a statement about supplied AX evidence, not a pixel-visibility or occlusion measurement. Raw AX value objects are read through their `value` field; role/name helpers otherwise coerce the supplied value to a string. No general CDP schema validation beyond the graph contract is claimed.

The current `prune` contract provides a useful traversal precedent but not the complete-tree validation needed here (`wender/perceive/ax.py:119-154`). Its stable hash deliberately excludes state attributes when `with_state=False` (`wender/perceive/ax.py:174-187`), while its raw content has already lost article interiors. Neither that hash nor repeated chassis STATE alone is the proposed completion comparison.

## PROPOSED `wait.py`

### Interface and exact result

Signature: `wait_done(snapshot_reader, rules, *, clock, sleep, max_polls, poll_interval_ms=100, timeout_ms=None)`. Executable reference: `tests/test_jq1_a6_perceive_primitives.py:126`; declarative ChatGPT profile: `tests/test_jq1_a6_perceive_primitives.py:480`.

`snapshot_reader()` returns one raw AX list from the caller's observation source. It performs no implicit action. `clock()` returns monotonic integer milliseconds; `sleep(ms)` advances or waits for an integer millisecond duration. Both are mandatory injected callables in the executable reference. `max_polls` is a nonnegative integer and provides a hard count bound even if the supplied clock does not advance. `poll_interval_ms` is a nonnegative integer; zero permits immediate successive reads while the count bound remains active. `timeout_ms` is either `None` or a positive integer. Booleans are not integer budgets. Invalid budgets, unknown rule keys, nonsequence role/name condition lists, and malformed selectors raise `ValueError`. The reader, clock, and sleep callables are invoked as supplied; their exceptions propagate without retry or a synthetic result.

The result has exactly these three keys:

| Key | Type and invariant |
|---|---|
| `done` | Boolean. True only for `confirmed_by_adapter`. |
| `polls` | Nonnegative integer incremented immediately before each snapshot-reader invocation. On every returned receipt it counts acquired snapshots, including malformed snapshots and reads that returned too late. It never exceeds `max_polls`. A reader exception propagates, so that failing call has no result receipt. |
| `completion_status` | Exactly `confirmed_by_adapter`, `stable_unconfirmed`, or `timeout`. |

`confirmed_by_adapter` means all configured absence, anchor, and presence conditions are satisfied together in an adequately quiet observation, with at least one positive `present_roles_names` selector. `stable_unconfirmed` means the quiet threshold and all supplied gates are satisfied but the profile supplies no positive presence evidence; it returns `done=false`. It is useful as a conservative observation result, not a generated-answer success. `timeout` returns `done=false` on poll or elapsed-time exhaustion. A missing required copy control or an active forbidden stop control cannot be converted to `stable_unconfirmed` just because the page remains unchanged.

### Declarative rule schema

`rules` is a dictionary permitting only the following four fields. Each field is optional, with the defaults shown; unknown keys are rejected. The ChatGPT profile explicitly supplies all four.

| Field | Value and default | Predicate |
|---|---|---|
| `absent_roles_names` | List or tuple of `RoleName` objects; default `[]` | Every selector is absent from eligible visible nodes in the current tree. This is a global veto. |
| `after_last_anchor` | One `RoleName` object or `null`; default `null` | If configured, locate its last match in reconstructed document order and scope positive evidence to its turn. A missing anchor prevents success. |
| `present_roles_names` | List or tuple of `RoleName` objects; default `[]` | Every selector has a qualifying match in the selected region, strictly after the selected anchor when one is configured. Without an anchor the positive conditions inspect all eligible visible nodes. |
| `quiet_snapshots` | Integer at least 2; default `2` | Required count of consecutive equal, valid raw snapshots. The first snapshot of a run counts as one. |

The ChatGPT profile is data:

```json
{
  "absent_roles_names": [{"role": "button", "name": "Detener"}],
  "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
  "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
  "quiet_snapshots": 2
}
```

The last assistant anchor is selected by reconstructed document order, not flat-list index or the first matching heading. Positive controls must belong to that anchor's own nearest article scope and occur after its entire descendant subtree. A copy nested inside the speaker heading cannot qualify. A copy control from the user article before the anchor cannot qualify; a copy control in a later user or unrelated sibling article cannot qualify either. The scope constraint is structural, independent of the profile's literal language. With no owning article, the reference fails the anchor gate; it does not infer a turn boundary from an entire page root. The anchor heading itself is not positive completion evidence. The full absence predicate still inspects the page so an active `button "Detener"` vetoes completion even when the assistant copy control is present.

### Equality, polling, and termination

The reference reconstructs the raw tree and canonicalizes the full raw node payload in its document order for equality. It does not compare only a node count, answer length, chassis stable hash, or rendered strings. Article-body text changes and changed controls therefore reset the equal-snapshot run. Keeping IDs/properties in the compared payload is conservative: identity or incidental-property churn may postpone success, but cannot make a changing supplied payload appear equal. Reordering the flat raw list without changing its rebuilt document tree must not change equality.

At a new valid snapshot, an equal previous value increments the quiet run; a different value starts a run of one. A graph rejected with `IncompleteTree` consumes that poll, clears the previous signature and quiet run, and continues only within the remaining budgets. It cannot bridge two valid but nonconsecutive observations into a quiet run. Thus `good, malformed, good, good` with threshold two completes at poll four; an always malformed stream times out. There is no hidden extra warmup observation. The reader is invoked immediately when count and time budgets permit. Sleep occurs between observations or for the final remaining deadline interval, never after success or after count exhaustion. The poll budget is independent of the time budget and remains decisive with a frozen fake clock. A transport/reader exception propagates; the reference does not retry, label the failure as a wall, or count an unread snapshot as a quiet observation. Non-graph payload errors such as an unserializable value also propagate.

The initial clock value establishes the deadline. Subsequent clock values must be integers and must not decrease; a noninteger value or backward movement raises `ValueError`. `max_polls=0` returns before consulting the clock. No new read begins at or beyond its deadline; the clock is checked after a read as well, and a read returning exactly at or beyond the deadline yields `timeout` even if its content would otherwise qualify. Sleeps are capped by remaining elapsed budget. This is a cooperative deadline: a synchronous snapshot reader must itself have bounded transport calls. The waiter cannot preempt a reader that blocks forever. Current `ax.raw_tree` has its own retries (`wender/perceive/ax.py:83-91`); an integrating adapter must explicitly account for that inner transport behavior instead of claiming `max_polls` bounds each CDP call.

### Deterministic fixture recipe

Use an in-memory tree with a user article and its copy button before the assistant article's `heading "ChatGPT dijo:"`. Add a streaming `button "Detener"`, then change the assistant answer text, remove the stop control, and repeat the final complete tree. All role/name and boundary gates are independently testable. `A` and `B` below are different valid snapshots; unless a row says otherwise, both have the required assistant copy and no forbidden stop control.

| Fixture | Quiet threshold and budget | Exact proposed result |
|---|---|---|
| User copy only; no assistant copy | Threshold 2; `max_polls=4` | `done=false`, `polls=4`, `completion_status="timeout"`. |
| Active stop control, even with assistant copy | Threshold 2; `max_polls=3` | `done=false`, `polls=3`, `completion_status="timeout"`. |
| Latest assistant anchor absent | Threshold 2; bounded repetitions | Timeout at the configured count; user or prior-turn copy cannot finish the call. |
| `A, B, B` | Threshold 2; sufficient budget | `done=true`, `polls=3`, `completion_status="confirmed_by_adapter"`. |
| `A, B, B, B` | Threshold 3; sufficient budget | `done=true`, `polls=4`, `completion_status="confirmed_by_adapter"`. |
| Every supplied snapshot changes | `max_polls=5` | `done=false`, `polls=5`, `completion_status="timeout"`. No sixth reader call. |
| Equal snapshots and a profile with no positive selectors | Threshold 2 | `done=false`, `polls=2`, `completion_status="stable_unconfirmed"`. |
| No permitted observations | `max_polls=0` | `done=false`, `polls=0`, `completion_status="timeout"`. No read or sleep. |

The contract fixtures use a fake clock starting at 0 and a 10 ms interval: three permitted immediate-return reads occur at 0, 10, and 20 ms. Threshold two on `A, B, B` completes after sleeps `[10, 10]`. Threshold three on `A, B, B, B` completes at 30 ms after sleeps `[10, 10, 10]`. A never-completing profile with a 25 ms elapsed budget and a larger poll budget reads at `[0, 10, 20]`, sleeps `[10, 10, 5]`, and returns `timeout` with `polls=3`; it does not read at 25 ms. A reader that consumes 10 ms per call, with zero inter-poll delay and a 20 ms budget, returns `timeout` with `polls=2` when its second call returns exactly at the deadline. These are fixture clock values, not browser latency observations. Any VM test-run duration belongs in the report as **VM**, never **MEASURED**.

## PROPOSED `text.py`

Signature: `extract(raw_nodes, region_start, region_end, excluded_roles_names=(), trailers=())`. Executable reference: `tests/test_jq1_a6_perceive_primitives.py:226`.

This is a pure operation on one in-memory snapshot. `region_start` and `region_end` are exact `RoleName` selectors. `excluded_roles_names` is a reiterable sequence of selectors whose matching subtrees are UI chrome. `trailers` is a reiterable sequence of literal terminal block strings supplied by the site profile. The reference validates every selector and rejects a nonstring or whitespace-only trailer with `ValueError`; it does not broadly catch unsupported argument-container types. Inputs are not mutated. The result has exactly `text` (a string) and `extraction_status` (one of `clean`, `ambiguous_boundary`, `incomplete_tree`).

| `extraction_status` | Meaning | `text` policy |
|---|---|---|
| `clean` | Input hierarchy is complete and the selected start/end determine one owned answer interval. | Render retained content, which may legitimately be the empty string. |
| `ambiguous_boundary` | The graph is structurally readable, but the latest start is missing, no own end is found, more than one own end qualifies, or turn ownership is ambiguous. | Empty string; do not return guessed content. |
| `incomplete_tree` | IDs or edges do not form the complete, traversable hierarchy required by the reference, including dangling children, duplicates, cycles, or multiple parents. | Empty string; no partially salvaged answer is called clean. |

These values are deliberately distinct from the waiter's `completion_status`. A caller may retain a clean extraction as a partial observation, but must not claim adapter-confirmed completion unless the waiter returned `confirmed_by_adapter` for the corresponding generation.

### Latest-turn boundary

Rebuild the full `nodeId`/`childIds` hierarchy first, including ignored and article wrappers. Select the last matching `region_start` in DFS document order, identify its nearest owning article, and choose the unique matching `region_end` strictly after it within that same ownership region. The extracted interval excludes the start heading and its entire descendant subtree, and ends before the assistant's own copy control and its descendants; neither delimiter contributes answer text. In particular, a child `StaticText "ChatGPT dijo:"` under the anchor does not leak into the answer. A preceding user's copy is outside the interval. A later copy belonging to a different article is also outside it. A missing or multiply matching delimiter causes `ambiguous_boundary`, rather than a scan through all subsequent page text.

For ChatGPT the start selector is `heading / ChatGPT dijo:` and the end selector is `button / Copiar mensaje`. These delimiters are supplied profile data. The inner answer heading `Results` is retained; it is not confused with the configured assistant-turn anchor. Previous assistant turns and all user text remain outside the latest assistant interval. A start without an owning article returns `ambiguous_boundary`; a future adapter for a nonarticle turn layout requires an explicit new boundary contract rather than an automatic whole-page fallback.

### Text emission and formatting

Walk retained children in `childIds` order. Emit each retained `StaticText` node once by its `nodeId`, then suppress its `InlineTextBox` descendants. An InlineTextBox under any StaticText remains suppressed even if the ancestor's text is whitespace-only. An orphan InlineTextBox without a StaticText ancestor may emit. A `StaticText` node named `Hello world` with two child runs `Hello` and `world` therefore yields `Hello world` once. Distinct nodes named `Yes` remain distinct; a set of string values is never a text-deduplication mechanism. The block roles are exactly `heading`, `paragraph`, `listitem`, `row`, and `code`. A named block node emits its own name only when it has no eligible StaticText or InlineTextBox descendant in the selected interval. Once a node emits, its descendants do not emit its aggregate content again.

Ignored and generic wrappers do not supply text by themselves, but their content is traversed. Article interiors are likewise traversed, unlike current `prune` (`wender/perceive/ax.py:137-149`). Configured exclusion selectors prune their matching subtrees. Text is taken from full raw values, without `normalize_name`'s 64-byte cut or pipe/backslash escaping (`wender/perceive/ax.py:59-68`). Each text node belongs to its nearest block ancestor, or to itself when there is no block. Consecutive noncode texts in one block join with one space; code runs in one block concatenate with no injected separator. Distinct blocks join with one newline. Prose whitespace collapses; code preserves spaces and newlines after CRLF/CR conversion to LF. Empty/whitespace-only runs do not emit. The final assembled string does not apply a global strip, preserving a code-only answer's leading indentation and trailing newline. This preserves heading, paragraph, repeated list-item, row, and code order without inventing Markdown bullets, HTML, or table delimiters.

Trailer stripping is terminal and literal. Normalize whitespace for matching configured terminal block strings, remove only a matching final block, and repeat until the final block is not a configured trailer. Do not remove an interior sentence, replace a substring everywhere, or cut on the first occurrence of a vague phrase. A trailer phrase quoted or discussed in an interior answer block must survive. The configured fixture trailers are `ChatGPT puede cometer errores`, `Última respuesta`, and `Respuesta completada`; a variant with extra text must be separately configured if it is to be stripped. Whitespace-only retained content returns `text=""` with `extraction_status="clean"` once boundaries are valid.

### Multi-turn fixture obligations

The reference fixture includes an earlier assistant turn, a user turn and user copy, then the latest assistant article and its own copy. Inside the latest article place `Results`, `Hello world` with two `InlineTextBox` runs, two distinct list-item `Yes` nodes, a row containing `A` and `B`, a code block, and the three configured terminal trailers. An ignored generic wrapper contains the body, and a configured `button / Share` exclusion has a StaticText child. Reverse the flat raw-node list while keeping the root/`childIds` structure unchanged. The exact clean answer is:

```text
Results
Hello world
Yes
Yes
A B
if ready:
    emit()
```

It retains each required content node once, both `Yes` items, and code indentation inside the code block. It contains no user, prior-turn, speaker-anchor, copy-label, Share, post-copy, or stripped terminal-chrome text. The reference tests also cover broken links, duplicate IDs, cycles, multiple parents, inconsistent parentId, ambiguous or foreign copy boundaries, interior trailer phrases, hidden ancestry, full names beyond 64 bytes, no input mutation, and an empty answer.

## PROPOSED `stopstate.py`

Signature: `detect(state_or_raw, *, url='', frame_urls=(), headers=None)`. Executable reference: `tests/test_jq1_a6_perceive_primitives.py:333`.

The input is either a raw AX list or a supplied STATE object with nodes. `url` is the top-level document URL string. `frame_urls` is a sequence of actual frame-document URL strings observed by the transport, not script resources or a list scraped from page text. It carries no synthetic visibility flag. Visibility and interactivity must be corroborated by visible AX evidence in `state_or_raw`. `headers` is a mapping of the top-level document response's header names to values, or `None`; headers from a subresource cannot be relabeled as main-document challenge evidence. The detector does not discover frames, fetch resources, inspect a live page, evaluate JavaScript, click, solve a challenge, navigate, sleep, or retry. The reference uses only stdlib parsing and in-memory values.

For a STATE object, an empty `url` argument falls back to `state_or_raw.source.url`; a nonempty explicit URL takes precedence. Missing STATE `nodes` defaults to an empty list. Unsupported top-level input and STATE nodes that are not a list of objects raise `TypeError`. A malformed raw list is passed through `_tree`; its `IncompleteTree` error suppresses AX-based cues but does not suppress independently supplied Google URL or Cloudflare HTML-header evidence. Without such evidence, the detector returns its no-wall receipt; this is not proof of complete observation. The guard reference separately validates the raw graph before permitting downstream observation. Valid STATE input is an adapter obligation: this detector does not invoke the complete STATE validator, and its parent traversal only filters hidden or cyclic ancestry. Other unsupported value/container types may propagate their ordinary Python errors; no broad exception handler converts them into successful observation.

The exact result has four keys:

| Key | Type and invariant |
|---|---|
| `is_stop` | Boolean. True means end automated progress and return the structured receipt to the human coordinator. |
| `reason` | Exactly `google_sorry`, `recaptcha_v2`, `turnstile_challenge`, `cloudflare_challenge`, `unusual_traffic`, or `null`. |
| `confidence` | Integer from 0 to 100. Deterministic strength assigned by the rule, not an empirically calibrated probability. No wall is 0. |
| `evidence` | A list of objects each containing exactly `kind` and `value`, both strings. `kind` is one of `url`, `frame_url`, `ax_role_name`, `header`, or `text`. An empty list accompanies no wall. |

The fixed reason-to-confidence mapping is `google_sorry: 100`, `recaptcha_v2: 95`, `turnstile_challenge: 95`, `cloudflare_challenge: 100`, and `unusual_traffic: 90`. When no supported wall matches, the result is exactly `{"is_stop": false, "reason": null, "confidence": 0, "evidence": []}`. These values order explicit rule strength; no statistical calibration or measured false-positive rate is claimed. Evaluation stops at the first matching reason in this fixed precedence: Google sorry URL, interactive reCAPTCHA v2, visible Turnstile, Cloudflare main-document HTML challenge, then unusual-traffic wall text. It does not select by maximum confidence or combine all signals. Within a frame rule it uses the first qualifying supplied frame and first qualifying visible AX cue.

URL evidence retains only scheme, hostname, optional port, and path. It drops userinfo, query, and fragment. AX evidence identifies the matched role/name; header evidence identifies only the two required header signals; wall-text evidence contains only the matched configured phrase. Evidence is structured observation data, not executable remediation instructions.

### Wall signals and adversarial controls

URL decisions parse scheme, hostname, and path. Only `http` and `https` URLs with a parseable hostname and port qualify. Scheme/hostname casing is normalized, and trailing dots are removed from the hostname. A string containing `google.com`, `/sorry/`, or `recaptcha` is not itself proof of a recognized provider endpoint. Hostnames use the exact domain-boundary rules below; userinfo, query contents, and fragments do not establish ownership. Paths are case sensitive and are not percent-decoded into a match. Query parameters may qualify an actual frame endpoint, but are not blindly echoed as evidence. Nonstring or unparsable URLs are ignored as URL signals. Malformed or deceptive URLs do not acquire a recognized host through substring matching.

| Supplied evidence | Proposed classification | Required discrimination |
|---|---|---|
| Hostname exactly `google.com` or ending in `.google.com`; path after removing trailing slashes exactly `/sorry` or `/sorry/index`; arbitrary query/fragment | `google_sorry`, confidence 100, with sanitized URL evidence. | `https://google.com.evil.invalid/sorry/index`, `notgoogle.com`, a `google.com` userinfo followed by a foreign host, `/sorry/indexed`, and a non-Google host whose path/query contains sorry text are URL-only negatives. |
| Interactive reCAPTCHA v2 frame on `www.google.com`, `recaptcha.google.com`, or `www.recaptcha.net`; path `/recaptcha/(api2\|enterprise)/(anchor\|bframe)`; visible corroborating AX challenge | `recaptcha_v2`, confidence 95, with recognized-frame and visible-control evidence. The AX corroboration is a checkbox named `I'm not a robot` or `No soy un robot`, or a visible reCAPTCHA dialog. | A `size=invisible` frame is excluded. Background v3, an invisible endpoint, or a supported frame URL without a visible challenge does not stop the run. |
| Frame at `challenges.cloudflare.com` under `/cdn-cgi/challenge-platform/`, plus visible AX `iframe` titled `Cloudflare security challenge` or `Widget containing a Cloudflare security challenge`, or visible checkbox `Verify you are human` | `turnstile_challenge`, confidence 95, with provider-frame and visible-challenge evidence. | A script URL, provider-name text, generic Verify control, or passive frame by itself is insufficient. |
| Top-level response headers `cf-mitigated: challenge` and content type `text/html`, including a charset suffix | `cloudflare_challenge`, confidence 100, with header evidence values exactly `cf-mitigated: challenge` and `content-type: text/html`. | Header names are lowercased and values stripped/lowercased; the content type is the media type before its first semicolon. `cf-mitigated` alone, non-HTML responses, and unrelated Cloudflare metadata are insufficient. |
| Visible configured English or Spanish unusual-traffic wall sentence | `unusual_traffic`, confidence 90, with the recognized wall sentence as evidence. | A vague mention of unusual traffic, a generic `Verify` button, cookie consent, login control, or HTTP 429 alone is not classified as a bot wall. |

For reCAPTCHA and Turnstile, the displayed cue names above compare after whitespace normalization and case folding. Checkbox/dialog role names remain exact; the iframe role spelling is case-insensitive to accept `Iframe`. reCAPTCHA frame paths permit one trailing slash. A `size` query value equal to `invisible` under case folding excludes that endpoint. Turnstile excludes frame paths ending in `.js` or `.css` under case folding. These rules require co-occurring frame and AX evidence; the string-only `frame_urls` API does not establish that a particular AX node belongs to a particular frame. A future adapter must retain common page/session provenance and must not mix observations from unrelated tabs or generations.

The text-only wall rule joins visible `StaticText`, `heading`, `alert`, and `paragraph` names in order, normalizes whitespace, case-folds, and checks containment of either complete literal sentence: `Our systems have detected unusual traffic from your computer network.` or `Nuestros sistemas han detectado tráfico inusual procedente de tu red de ordenadores.` Evidence uses the matched configured sentence. An arbitrary mention of the two words `unusual traffic` does not qualify. These literal rules are proposed fixture contracts; site-wide or all-language detection coverage is **AUSENTE**.

Positive fixtures supply meaningful evidence, and assert its presence and reason instead of testing only `is_stop`. Negative fixtures are ordinary passing reference tests, including all requested endpoints, a deceptive non-Google sorry URL, background v3, script-only URLs, HTTP 429 alone, consent, login, and generic verify controls. False means no supported wall predicate matched the supplied observations; unavailable frame/header data remains an observation limitation rather than permission to interact.

### STOP ownership and safety

The caller treats a true receipt as a latch for the current automated attempt. Once latched, there are zero additional polling reads for completion, zero page JavaScript evaluations, zero action/executor calls, zero retries, zero resets/reloads, and zero challenge evasion attempts. Return the reason and minimal matched evidence to the human. Resumption requires a new, explicitly authorized attempt after the human handles the condition; a timeout or an exception does not clear the latch.

Detecting a STOP state is not an instruction from the page. Profile settings and automation policy are trusted inputs; AX names, headers, and URLs are untrusted observations. Never execute or obey text embedded in those observations. Evidence contains only the signal needed to explain the decision; avoid serializing full answer trees, credentials, response bodies, cookies, or URL secrets. The offline reference fixtures contain no real credentials; the URL-sanitization fixture uses synthetic userinfo strings. The fixtures never create or write frames.

## Proposed integration points, with current files unchanged

### ChromeDriver observation and action boundaries

The future coordinator must collect top-level/frame/response metadata from transport observations and a raw AX snapshot without page evaluation, run `detect`, and return immediately on STOP. A URL/header-only wall can be identified before asking for AX at all. A visible-control predicate may require one raw AX acquisition, still without page JavaScript. This preflight cannot be implemented by first calling today's `ChromeDriver.observe`: that calls `browser.page_state`, which evaluates JavaScript at `wender/perceive/browser.py:48` and `wender/perceive/browser.py:50`; with vision enabled it may also capture a screenshot at `wender/learn/chrome_driver.py:101-105`.

For a non-STOP observation, the coordinator may supply the same raw tree to the waiter and extractor under an attempt/generation identity, and separately build the ordinary chassis STATE. Raw provider content stays inside perception and is released after deriving the bounded answer receipt; it is not attached to STATE nodes or written as a frame. The present browser module explicitly exposes only STATE (`wender/perceive/browser.py:4`), and the present schema has no raw AX field (`wender/state/schema.py:15-17`). A production generation/coherence receipt and a metadata collector with a no-JavaScript guarantee are **AUSENTE** in these bound files.

Check the latch before proposing candidates, before every action, before reset, and before retry entry. `ChromeDriver.candidate_actions` currently offers actionable links/buttons (`wender/learn/chrome_driver.py:108-123`); the proposed coordinator must not offer challenge controls once STOP is known. `ChromeDriver.act` currently rereads AX and passes `retries=2` to the executor (`wender/learn/chrome_driver.py:125-138`); placing the STOP check only after that call is too late. `ChromeDriver.reset` currently navigates immediately (`wender/learn/chrome_driver.py:142-144`), so it also needs an outer latch gate.

### Exploration

The driver interface returns `(state, screen_fp)` and separately exposes `candidate_actions` and `act` (`wender/learn/explore.py:14-26`). `explore` observes initially, takes candidate actions, executes `driver.act`, and observes afterward (`wender/learn/explore.py:78-98`). It can call a supplied reset and resume observation (`wender/learn/explore.py:104-109`). Its current stopped values are `budget`, `exhausted`, and `error` (`wender/learn/explore.py:34-38`; `wender/learn/explore.py:119-131`); a typed STOP result is **AUSENTE**.

A future wrapper should intercept before entering `explore` and at each driver boundary, latch structured STOP evidence, and terminate the attempt. A wall must not become another clickable program-map state or trigger its fallback reset. Merely returning an empty candidate list is insufficient because the current loop may call `reset` when no route remains. Raising a generic exception also loses the STOP receipt and is not a complete proposed integration: the wrapper must preserve and return the typed evidence separately, while preventing all subsequent act/reset calls. These are integration requirements, not changes made by this task.

### Replay and admission

`replay_and_admit` resets each run when the driver has a reset method, observes, executes every step, observes again, evaluates checkpoints, compares run summaries, writes evidence, and invokes admission (`wender/learn/replay.py:92-105`; `wender/learn/replay.py:111-142`). Current checkpoints can test node presence, absence, and text containment; those checks do not establish a latest assistant boundary or a sustained quiet period (`wender/learn/replay.py:35-68`).

The future replay coordinator must gate initial reset, each action, every observation, and subsequent runs on its STOP latch. A wall aborts replay without admitting the draft, annotating it as successfully reproduced, retrying another run, or issuing a reset to work around it. Completion evidence and extraction status must be checked before an answer-dependent checkpoint is accepted. The existing `reproduced` evidence and summary comparison are not substitutes for that receipt. Typed STOP handling, completed-turn checkpoints, and clean-answer admission criteria are **AUSENTE** in current replay.

## Executable reference inventory and validation boundary

The allowed test module is the single executable reference implementation source. This document carries the behavioral specification without duplicating executable function bodies.

| Executable reference symbol or contract group | Exact location in this change | Purpose |
|---|---|---|
| `_tree`, `IncompleteTree` | `tests/test_jq1_a6_perceive_primitives.py:59`; `tests/test_jq1_a6_perceive_primitives.py:55` | Validate/reconstruct node identity, ancestry, and document order. |
| `wait_done`, `CHATGPT_RULES` | `tests/test_jq1_a6_perceive_primitives.py:126`; `tests/test_jq1_a6_perceive_primitives.py:480` | Executable waiter and declarative profile. |
| `extract`, `answer_tree` | `tests/test_jq1_a6_perceive_primitives.py:226`; `tests/test_jq1_a6_perceive_primitives.py:515` | Executable extractor and complete multi-turn fixture. |
| `detect` | `tests/test_jq1_a6_perceive_primitives.py:333` | Pure structured wall classifier. |
| `StopRequested`, `GuardedDriverReference` | `tests/test_jq1_a6_perceive_primitives.py:422`; `tests/test_jq1_a6_perceive_primitives.py:430` | Separate STOP receipt and a mocked pre-observation latch. |
| `WaitReferenceContracts` | `tests/test_jq1_a6_perceive_primitives.py:574` | Exact completion statuses, ownership, full-payload changes, budgets, clock/error paths. |
| `TextReferenceContracts` | `tests/test_jq1_a6_perceive_primitives.py:754` | Exact answer, identity-based deduplication, structural/boundary abstention, trailers and code whitespace. |
| `StopReferenceContracts` | `tests/test_jq1_a6_perceive_primitives.py:864` | All wall families, required negative controls, evidence and malformed-AX handling. |
| `StopIntegrationReferenceContracts` | `tests/test_jq1_a6_perceive_primitives.py:981` | No page evaluation/action/reset/retry on STOP and no downstream observation from incomplete AX. |
| `MissingProductionPrimitiveContracts` | `tests/test_jq1_a6_perceive_primitives.py:1072` | Three direct production expected failures plus a normally running transitive-import guard. |

`GuardedDriverReference.observe` performs one direct `Accessibility.enable`, `Accessibility.getFullAXTree`, and `Accessibility.disable` sequence against a mock, then calls `detect`. It deliberately avoids the current retrying `ax.raw_tree` helper. True STOP is latched and raised as `StopRequested` before the wrapped driver's observation. A no-wall result still requires a complete raw graph before delegation. Once latched, the wrapper blocks `observe`, `candidate_actions`, `act`, and `reset` with the same structured receipt. The fixture starts with this pre-observation check; it is not a production wiring of ChromeDriver, exploration, or replay, and it does not establish automatic preflight before a caller's very first reset. The future coordinator must do that before entering those loops.

Run the requested gate normally:

```text
python3 -m unittest tests.test_jq1_a6_perceive_primitives
```

Reference behavior tests are ordinary `unittest.TestCase` methods and must pass normally. Direct production behavior tests import the missing primitive inside the individual test method and apply narrowly scoped `unittest.expectedFailure` to that primitive's behavior test. Their names identify the missing module and method (`tests/test_jq1_a6_perceive_primitives.py:1094`; `tests/test_jq1_a6_perceive_primitives.py:1102`; `tests/test_jq1_a6_perceive_primitives.py:1110`). A separate normal test requires a missing import to name the requested primitive itself, so an unrelated transitive `ModuleNotFoundError` remains a normal failure (`tests/test_jq1_a6_perceive_primitives.py:1079`). Production primitive imports are not made at module-discovery time, no broad import fallback hides unrelated errors, and no test uses a skip to conceal the absence. When the production primitive is later implemented, an unexpected success is a visible prompt to remove the corresponding known-gap decorator and run the contract directly.

All fixtures and transports are offline, in memory, and use `unittest` and `unittest.mock` from the standard library. The STOP harness asserts no `evaluate`, navigation/reset, action, retry, or evasion call occurs after detection; the known URL-wall path reaches STOP before ordinary observation. There is no browser process, live network, display, GPU, external service, screenshot capture, or frame file. Actual observed output and any VM timing belong in the companion task report; this document makes no real-site measurement or acceptance claim.

## Limitations and AUSENTE register

- **AUSENTE, production primitives:** no production `wait_done`, `extract`, or `detect` is supplied by this change. The existing pins document those gaps (`tests/test_known_issues_p1_web.py:16-31`). The executable reference and this specification are intended for a later implementation task.
- **AUSENTE, C4 expansion:** the bound material does not define a separate expansion or acceptance protocol for that parent task identifier.
- **AUSENTE, snapshot coherence:** separate raw AX, URL/header, STATE, and layout reads are not currently bound to one coherent generation (`wender/perceive/browser.py:46-52`; historical review question at `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:333`). A future coordinator must prevent cross-generation completion/text pairings.
- **AUSENTE, production STOP latch and typed loop result:** current ChromeDriver, exploration, and replay lack the proposed preflight/latch/receipt behavior at the integration sites cited above.
- **AUSENTE, complete provider coverage:** locale variants, every Google country domain, every challenge variant, partial/cross-origin AX coverage, virtualized missing turns, and future site UI changes are not established by offline examples. A missing boundary yields abstention; it does not authorize CSS guessing or challenge bypass.
- **AUSENTE, completion correctness guarantee:** a copy control and stable supplied snapshots are adapter evidence, not proof that the answer is correct or that a provider will never emit another token. Stability without positive evidence remains `stable_unconfirmed` with `done=false`.
- **AUSENTE, hard preemption and latency measurements:** count bounds are deterministic; an injected synchronous transport must supply its own call deadline. Fixture clocks and VM unit-test durations are not browser performance measurements.
- **AUSENTE, acceptance:** passing local contracts, creating the collection PR, and emitting the ready signal are collection evidence only. The orchestrator evaluates its own gate.
