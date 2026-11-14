# JQ1 A6: multiwindow harvest proposal (C9 / WP1-06)

## Status and source boundary

**Design for Wave Mind review before any production build.** The companion
`tests/test_jq1_a6_multiwindow_harvest.py` contains an executable, test-local
reference model and fake-CDP pins. It is not a production broker, a browser
integration, a live harvest, or a MemoryWeb conformance result. Statements marked
as proposed below describe candidate policy; they do not describe existing APIs.

Task: `jq1-a6-multiwindow-harvest`, attempt `a1`, role `builder`, thread
`f-jq1-a6-multiwindow-harvest`. The requested source is
`example-owner/wender-agent@bridge/integration`, resolved through this chat's GitHub
connector to `f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0`. The eight bound files were
read with explicit refs and their blobs checked against that resolved commit.
Source citations below refer to those bound bytes; no default-branch source is
used. The task metadata names older commit
`6981b293a00a5d8f7afa2a841c79dd738b585426`; the user instruction to resolve and read
the actual requested ref controls this work. That drift is recorded, not hidden.

The supplied S1 audit records its own older snapshot at
`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:5`. In particular, its discussion of an
action-executor stub at `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:103` is historical:
the currently bound `cdp_executor.run_ax_action` is implemented at
`wender/act/cdp_executor.py:218`. This proposal uses the current implementation
when describing ACT and retains the audit as evidence of identified S1 risks.

## What the supplied code establishes

| Bound source and exact anchor | Existing behavior and implication |
|---|---|
| `wender/perceive/cdp_port.py:24`, `wender/perceive/cdp_port.py:40`, `wender/perceive/cdp_port.py:44` | Construction rejects non-loopback hosts; start discovers a browser WebSocket through the local DevTools endpoint and checks the returned hostname. Construction alone starts no browser. |
| `wender/perceive/cdp_port.py:98`, `wender/perceive/cdp_port.py:100`, `wender/perceive/cdp_port.py:101`, `wender/perceive/cdp_port.py:104` | `attach_url(url_prefix)` filters existing `page` targets by string prefix and attaches to the first returned match. No match raises `CdpError`; it does not create a target or navigate to a requested route. Multiple matches have no ambiguity rejection. |
| `wender/perceive/cdp_port.py:104`, `wender/perceive/cdp_port.py:105`, `wender/perceive/cdp_port.py:106` | Attach uses `flatten=True`, records the returned session in `_attached`, and enables Page on that session. The code does not record whether it owns a tab, a site lease, or a question. |
| `wender/perceive/cdp_port.py:114`, `wender/perceive/cdp_port.py:117`, `wender/perceive/cdp_port.py:123` | Port close detaches its recorded sessions and closes its socket. It never sends `Browser.close`. This remains a required invariant. |
| `wender/perceive/cdp_pipe.py:173`, `wender/perceive/cdp_pipe.py:184`, `wender/perceive/cdp_pipe.py:193` | Sends serialize under a lock and replies correlate by connection-wide command id. The response deadline starts after the write; it is not a whole-job deadline or cancellation mechanism. |
| `wender/perceive/cdp_pipe.py:90`, `wender/perceive/cdp_pipe.py:199`, `wender/perceive/cdp_pipe.py:203` | Events share one bounded deque. A session-specific `wait_event` leaves nonmatching events in that deque, but omitted session means any session. Queue overflow silently evicts oldest events. |
| `wender/perceive/cdp_pipe.py:216`, `wender/perceive/cdp_pipe.py:223` | `attach_page` can create `about:blank` if there is no page at all; it has no per-site open-or-reuse policy. That helper does not supply WP1-06. |
| `wender/perceive/cdp_pipe.py:229`, `wender/perceive/cdp_pipe.py:231`, `wender/perceive/cdp_pipe.py:236` | `navigate(A, url)` removes **all** buffered load events, including B's, before navigating A and waiting for A's load. The resulting cross-session loss is S1-F4, also recorded at `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:317`. |
| `wender/perceive/browser.py:45`, `wender/perceive/browser.py:46`, `wender/perceive/browser.py:48`, `wender/perceive/browser.py:53` | `page_state` reads AX, metadata, viewport, and optional boxes sequentially, then releases AX and builds STATE. It has no coherent generation check and release is not in `finally`; intermediate errors can skip release. See S1-F5 at `docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:318`. |
| `wender/perceive/browser.py:26`, `wender/perceive/browser.py:28`, `wender/perceive/browser.py:35` | Box reads use the explicit session; individual box failures are ignored while the initial `DOM.getDocument` failure propagates. Box reads do not establish answer identity or completion. |
| `wender/perceive/ax.py:54`, `wender/perceive/ax.py:76`, `wender/perceive/ax.py:83`, `wender/perceive/ax.py:90`, `wender/perceive/ax.py:94` | Raw AX acquisition retries the enable/read pair up to three times, sleeping between caught exceptions; disable is a separate helper. These loops have no broker STOP/cancellation callback. |
| `wender/perceive/ax.py:35`, `wender/perceive/ax.py:45`, `wender/perceive/ax.py:137`, `wender/perceive/ax.py:148` | Chassis pruning excludes `StaticText` and skips descendants of emitted articles. Pruned STATE cannot be assumed to contain a complete answer body. |
| `wender/perceive/ax.py:59`, `wender/perceive/ax.py:67`, `wender/perceive/ax.py:200`, `wender/perceive/ax.py:218` | Names are shortened before escaping; stable ids use fingerprint and ordinal. The context matcher compares coarse fields, not a question, target, session, or navigation generation. These ids do not replace harvest provenance. |
| `wender/act/cdp_executor.py:15`, `wender/act/cdp_executor.py:175`, `wender/act/cdp_executor.py:218` | The implemented operations are `click`, `focus`, `type`, and `select`; dispatch uses explicit-session DOM resolution and fixed JavaScript. `press` and `insert_text` remain outside this tuple. |
| `wender/act/cdp_executor.py:237`, `wender/act/cdp_executor.py:246`, `wender/act/cdp_executor.py:283`, `wender/act/cdp_executor.py:314` | The executor checks match level and ties for irreversible actions, then has retries and optional reversible-click coordinate fallback. Those controls provide neither a question-submission idempotency contract nor a STOP fence between all sends. |
| `docs/spec/KNOWN_ISSUES_P1_WEB.md:11`, `docs/spec/KNOWN_ISSUES_P1_WEB.md:12`, `docs/spec/KNOWN_ISSUES_P1_WEB.md:14`, `docs/spec/KNOWN_ISSUES_P1_WEB.md:15`, `docs/spec/KNOWN_ISSUES_P1_WEB.md:16` | WP1-01/02/04/05/06 request AX-based rules, assistant-specific completion, answer-region extraction, STOP handling, and a per-site tab manager with a lease. These rows state wanted behavior; they do not establish implemented services. |
| `tests/test_known_issues_p1_web.py:16`, `tests/test_known_issues_p1_web.py:25`, `tests/test_known_issues_p1_web.py:29`, `tests/test_known_issues_p1_web.py:48` | Existing pins explicitly mark waiter/extractor/STOP gaps as expected failures and test the port-close invariant. A direct new pin for absent `ensure_tab` belongs in an individually decorated WP1-06 test. |

`C9` is the task's campaign label for this design; an independent C9 production
contract is **AUSENTE** from the supplied source corpus. WP1-06 is directly
documented in the issue table cited above.

## MemoryWeb boundary: AUSENTE

MemoryWeb tool definitions, tool-call schemas, permission semantics, safety
contracts, STOP scope, and resumability rules are **AUSENTE** from the eight
bound files. The source provides local transport/perception/action mechanisms
and issue descriptions; none establishes authority to send questions through
MemoryWeb or receive its answers. No MemoryWeb method name, parameter object,
authentication behavior, or approval grant is inferred here.

Adoption is contingent on Wave Mind reviewing the actual MemoryWeb tools and
safety contracts, then reconciling them with the proposed leases, STOP scope,
provenance, timeout, cleanup, and answer-retention rules below. An incompatible
contract blocks integration until explicitly resolved. Local mocked callbacks
stand in for send and extraction solely to test ordering and state transitions.
They are not proposed MemoryWeb APIs or invented provider-specific CDP commands.

## Proposed identities, ownership, and receipts

Every job is identified by `(site, target_id, session_id, question_id,
generation)`. The receipt's `site` is its configured label; the lease key is the
normalized `(https, lowercase hostname, effective port)` origin, which remains
the same when the route or display label changes. Target and session may be absent in a
failure receipt if setup failed before they were returned; never fill them from
another job. `question_id` is assigned by the caller before fan-out, and the same
question can therefore have separate site receipts. `generation` is a local
monotonic per-site lease counter; `navigation` is a separate per-job route epoch.
Together they distinguish later work and navigation in a reused tab.

| Record | Proposed contents and obligation |
|---|---|
| Site configuration | Site key, URL prefix, exact requested `open_url`, and reviewed rule/profile identity. A test route is fixture data, not a claim about a provider's current routes. |
| Lease token | Site origin, question, generation, and owning broker/job. The model releases only the identical token object held by the lease table; an old job cannot release a newer lease. |
| Tab handle | Site, target, session, tab ownership, and session ownership. A caller-owned tab can have a new broker-owned attachment. These are different ownership facts. |
| Event envelope | Original session and event payload plus trusted local question/generation/navigation correlation. Native CDP does not supply a question id. |
| Terminal receipt | Identity, `requested_route`, `status`, `error`, `completion_status`, `extraction_status`, optional answer, attempt counts, ownership, and cleanup disposition. Adding the reviewed profile identity to a production receipt remains proposed. |
| Cleanup obligation | Acknowledged resources belonging to this job, the allowed cleanup action, and whether it remains deferred. Unknown ids stay unknown. |

Receipts use explicit status values instead of treating an empty string or a
falsy payload as success. A successful healthy answer requires matching site,
target, session, question, and generation, established completion, and successful
extraction. Completion evidence and extracted text remain distinguishable. A
failure receipt is an output in its own right; one bad site never supplies
another site's text. Fields and spelling in this table are a proposed design,
not a production schema registration. The model's `_Job` stores the handle and
owns the receipt; its `ensure_tab(job, ...)` populates that record, while the
proposed public `ensure_tab(url_prefix, open_url)` API remains absent.

## Proposed `ensure_tab`: open or reuse, then direct navigation

WP1-06 proposes `ensure_tab(url_prefix, open_url)`. Its minimum candidate contract
is a returned tab handle, not an unqualified session string. The surrounding
job supplies its lease and, optionally, an explicit selected target. Proposed
selection and error policy deliberately differs from current first-match attach.

1. Validate configured site and route before issuing CDP. Derive one normalized
   origin lease key; routes on that origin cannot acquire independent leases.
   Match only `type == 'page'` targets within the configured site/prefix. A worker,
   iframe, browser target, unrelated host, or deceptive origin is not a match.
2. Acquire the site's lease before discovery, attachment, creation, or navigation.
   A contending question fails immediately with its own busy receipt and no CDP
   or provider callback. It cannot release or detach the incumbent's resources.
3. Send `Target.getTargets`. With one matching page, reuse its target. With
   several matches, reject ambiguity without attachment or creation unless the
   caller explicitly selected one of those matching targets. Missing or invalid
   explicit selection fails; do not silently replace it with a newly created tab.
4. Only when there are no matches and no invalid explicit selection, send exactly
   one `Target.createTarget` with `{'url': open_url}`. Record the returned target
   immediately as broker-owned. A missing/invalid id or exception is a creation
   failure; do not attach to a different arbitrary page or retry creation blindly.
5. Ordinarily send `Target.attachToTarget` with the selected `targetId` and `flatten=True`.
   Record an acknowledged session as broker-owned even when its tab is caller-owned.
   Reject returned sessions already in the job or trusted caller-session registry
   before claiming ownership or permitting detach.
   The explicit test-only `_CallerTab(target_id, session_id)` variant reuses an
   identified caller-owned session without attaching; validate its matching target
   and reject an active/fenced session collision. Never adopt a sibling's attachment
   as an untracked convenience. The model verifies the session/target pair against
   a trusted caller-supplied registry before CDP; this is not a CDP introspection
   API. Real caller-handle authenticity and registry authority are review gates.
6. Send `Page.enable` with that exact returned `session_id`. Then send
   `Page.navigate` for the exact requested route with the same session. Even a
   creation path has an explicit direct-route navigation after Page is enabled;
   it does not click through a homepage or menu. Never use a sessionless navigate.
7. Observe that session's correlated navigation result/load evidence. Preserve
   all other session queues. A load event alone does not imply the answer is done.
   Gate question send on known STOP evidence. Validating the complete observed
   final route against a reviewed route policy is an additional production gate;
   the reference tests validate requested navigation and the known STOP fixture.
8. On every exit, either transfer the held lease to the active job or release
   that exact token. Setup failure must produce its own receipt and account for
   acknowledged resources. It must not detach another session or retain a lease.

All steps are fenced by control state before dispatch and after each result.
Discovery, creation, attachment, enabling, and navigating are single attempts in
the reference scenario. A creation exception can mean an unacknowledged target
was created remotely. Record uncertain ownership and require reconciliation;
there is no id with which to authorize a guessed detach/close. An attachment
exception similarly does not authorize guessing the session id.

### Ownership and cleanup policy

Reusing a tab grants this job no right to close that tab. The broker tracks its
attachment separately; a local failure can detach only an acknowledged session
it attached. Closing any caller-owned page or issuing `Browser.close` is
prohibited in this proposal. `CdpPort.close` remains as implemented at
`wender/perceive/cdp_port.py:114`; do not call connection-wide close from one
site's worker, since that would detach its siblings too.

Release the local lease on success, STOP, timeout, cancellation, and exception.
Local release is idempotent and does not require a CDP round trip. Preserve a
cleanup obligation for a known broker-owned target/session if it cannot be
settled safely. Ordinary setup errors may perform narrowly scoped session
cleanup when no control fence has fired; failure of that cleanup cannot suppress
the primary failure or keep the lease held. The reference model deterministically
detaches its acknowledged broker-owned session, then closes only its acknowledged
broker-created target using `Target.closeTarget`, on ordinary success or error.
It never detaches an explicit caller-owned session or closes a caller-owned tab.
Managed retention of broker-created tabs is a possible later policy, not behavior
demonstrated by this model. Unknown create/attach outcomes receive
`cleanup_status=reconcile_unknown_outcome`; cleanup failures are recorded separately.

After STOP, timeout, or cancellation, this job sends **zero further CDP commands**,
including detach, Accessibility disable, and tab-close cleanup. Drop its local
lease immediately and defer remote cleanup; successful sibling sessions remain
usable. This trades prompt remote resource release for a strict no-dispatch
fence. A future authorized reconciliation lifecycle must review deferred resources
before reuse. The model also fences unknown submission and event overflow.
Its tombstones cover the normalized **site**, surviving lease release: new work
for that site returns `REVIEW_REQUIRED` without CDP. No automatic resume exists.
Unknown setup outcomes and failed/deferred cleanup also leave that tombstone.
An already-sent create/attach command can acknowledge a resource after cancellation;
the model refreshes receipt identity locally for deferred reconciliation and sends
no new command. Finalization collects its receipt and releases the lease in
`finally`, including an unexpected exception from the cleanup function itself.

## Proposed leases, queues, and concurrent scheduling

One normalized site owns at most one active question at a time, including setup,
navigation, waiting, extraction, and finalization. Unrelated sites can proceed
independently over the shared connection. The reference model uses deterministic
interleaving: all eligible jobs are admitted, each can send its question, and
their observations then interleave. No thread race, sleeps, elapsed-time assertion,
browser process, or live service is needed to exercise concurrency semantics.

The scheduler serializes state transitions and lease updates; a production
implementation would require an equivalent atomic boundary. Cross-process lease
storage, distributed expiry, crashed-worker recovery, and account-sharing scope
are not implemented here and remain review questions. A timeout does not grant a
second worker permission to replay an ambiguously submitted question.

Route incoming messages once at the connection boundary. Reply ids retain their
transport meaning. Session events go to a distinct bounded deque for that exact
session; there is no implicit "current tab" or catch-all event waiter. Navigation
A can discard obsolete A navigation evidence, but cannot rewrite B's deque.
Preserve B's pending load when A starts, then allow B to consume it normally.

While an attachment reply is pending, the model buffers at most 32 early unknown-
session events. It replays only the acknowledged session's events before Page
enable, so an early `/sorry/` STOP fences enable/navigation; unrelated events are
ignored. Overflow fences the pending job. This is one cooperative pending-attach
slot, not a production concurrent-attachment/event-correlation implementation.

An event envelope belongs to one leased question/generation. Reject stale,
foreign, or uncorrelated answer/completion events; never relabel them as belonging
to the currently active question merely because they arrived later. Terminal
generation events cannot resurrect a job or write a second terminal receipt.
The model validates all three question/generation/navigation tags at ingress and
consumption for load and `fixture.ax` work events. Rebinding clears only that
session's old queue; READY discards the pre-submission AX baseline. Navigation
after question submission is rejected. These rules prevent already-buffered old
evidence from becoming the next question's answer.
Overflow must be observable and fail the affected job closed; silently dropping
evidence cannot establish completion. Bounds are configured test/model limits,
not throughput measurements or proof that hostile transport input is fully bounded.
The model's `_Queues` permits 32 queued events per registered session by default;
the overflow pin uses a limit of two and rejects the third event without losing B's.

Control observations have priority over queued work. Dispatch of a STOP event
latches the fence immediately, before another pending action, retry, completion
transition, or extraction callback. If completion and STOP are observed together,
STOP wins. A later healthy observation cannot silently clear that latch.

`Page.loadEventFired` has session identity but no native question/generation
identity in these helpers; source support for loader/frame freshness is
**AUSENTE** (`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:50`). The test model receives
trusted correlation tags from its deterministic fixture. Real mapping from
frames/loaders and observed document changes to those tags must be reviewed and
implemented separately; assigning the newest local generation on arrival is not
a sufficient substitute. The passing model does not fix S1-F4 in production.

## Proposed question, completion, and extraction lifecycle

Each site follows: acquire; ensure/attach; navigate; observe route and safety;
send the question once; wait for correlated completion; extract the answer;
finalize a receipt and release. Healthy jobs can occupy different stages at the
same time. A failure affects only the identified job unless control scope is
explicitly global.

Question send and extraction are symbolic injected local callbacks in the
reference model. The send callback's invocation is tracked with the full job
identity. Its result does not prove that a real service accepted a request.
If submission raises after an uncertain effect, classify it as unknown/failed
submission and stop that job; do not repeat the question. A broker adapter must
later prove its own send and postcondition semantics under reviewed contracts.

Completion is an observation state, not the absence of an exception. Data-driven
AX rules must identify the latest assistant response belonging to the submitted
question, exclude the user turn's copy control and historical responses, and
exclude an active streaming/stop control. WP1-02 describes why the user-turn
copy button is insufficient (`docs/spec/KNOWN_ISSUES_P1_WEB.md:12`). Its Spanish
labels are source evidence of that example, not a universal provider rule.
The model recognizes only its synthetic assistant marker, answer article, later
copy control, and absence of `Stop generating`. It rejects user-only/streaming
fixtures and stale envelopes. Parsing multiple historical assistant turns in a
real page, and proving which text belongs to the current request, remain review-only.

Extraction occurs only after completion has independently been established and
the STOP/deadline fence is checked again. The extractor must return that response
region's content with the same identity, excluding UI controls and trailers.
WP1-04 requests this behavior at `docs/spec/KNOWN_ISSUES_P1_WEB.md:14`. A complete
assistant turn with extraction failure yields `completion_status=complete` and
`extraction_status=error`; a waiting timeout yields `completion_status=timeout`
and `extraction_status=blocked`. A STOP after completion preserves `complete`
while blocking extraction. Before extraction begins normally its status is
`not_attempted`; successful extraction is `ok`. Neither failed case yields a
successful answer. Partial content,
missing regions, and foreign provenance cannot be silently promoted to success.

Use in-memory raw AX fixtures to distinguish answer text from chassis nodes.
Current `ax.prune` intentionally discards StaticText and article interiors;
neither a chassis hash nor `browser.page_state` is a generic answer extractor.
The model's fixture extractor demonstrates isolation and failure handling only.
Provider-specific AX region rules, content formats, answer-size bounds, locale
coverage, and truncation policy require their own reviewed contracts. Do not
invent CSS selectors, write raw trees/frames to disk, or claim those gaps solved.

## Proposed STOP, timeout, retry, and cancellation rules

| Control observation | Proposed scope and immediate result |
|---|---|
| `/sorry/` redirect on a known session, or that session's wall evidence | Local STOP for that target/question. Emit its failure receipt; no further sends, retries, reads, extraction, navigation, or remote cleanup for it. Healthy siblings may finish. |
| User STOP, or a future reviewed MemoryWeb STOP designated global | Fence every current job in the harvest and reject further admission. No sibling continues after the global fence. This MemoryWeb mapping is proposed and contingent on its actual contract review. |
| Ambiguous or missing authenticated control scope (review-only) | Conservatively stop the harvest pending review; do not reinterpret a global stop as a local site failure. |
| Per-job deadline exhaustion | Terminal timeout for that job, retaining its known provenance and distinct completion/extraction states. Other site deadlines remain independent. |
| Local cancellation | Cancel only its identified job/generation, release its lease, and defer remote cleanup. Late completion cannot resurrect it. |
| Shared transport closure (review-only fan-out) | Fail each active job with its own identity and transport reason; no fabricated healthy answers. |

The executable model exercises known-session `/sorry/` or unusual-traffic alert
evidence and explicit local/global `cancel` callbacks. MemoryWeb control delivery,
challenge-frame recognition, ambiguous authenticated control scope, and a shared
transport-outage broadcast are not implemented. Unknown unrelated session events
are ignored; such data is not an authenticated global STOP instruction.

WP1-05 supplies the `/sorry/`, unusual-traffic, and challenge-wall requirement
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`). The deterministic STOP fixture is
`https://www.google.com/sorry/index`; the model does not solve or evade it. Page
text and AX labels are observation data, never instructions that expand authority.

The fence applies before every externally effective callback/CDP send, after each
result, and before consuming a result into a successful receipt. A command already
sent before STOP was observed cannot be recalled; the promise is zero subsequent
dispatch, with pending late replies/events ignored for the terminal generation.
Production must linearize control observation and dispatch to close that race.

The fixture defaults are `waits_left=8`, shared across navigation and completion
waits, and `MAX_READ_ERRORS=3`, counting classified transient read errors across
the job. Missing/incomplete evidence and nonterminal transient errors consume
logical wait steps; the third transient error terminates as `READ_ERROR`.
Success permits one extraction; submission has one attempt. These bounds count
cooperative transitions, not elapsed time or every setup command. The default
`read` Mock consumes in-memory `fixture.ax` queue events; it does not call the
production AX reader. A separate pin demonstrates a guarded compound mock read.
No duration or VM timing is acceptance evidence. A production deadline must cover discovery, waiting
for the dispatch lock, writes, replies, polling, retries, and extraction rather
than repeatedly resetting a full command timeout. The current response-only
deadlines do not establish that property (`wender/perceive/cdp_port.py:83`,
`wender/perceive/cdp_pipe.py:184`, `wender/perceive/cdp_pipe.py:233`).

Only explicitly classified read-only observation failures are candidates for
bounded retries within those shared limits. Retries stop
immediately upon STOP, cancellation, or exhaustion. Creation, attachment,
navigation, question submission, and extraction have no automatic effectful
replay in this model. In particular, an uncertain submission cannot be cured by
blindly running the executor again. Unknown errors terminate with a receipt.

Do not wire the broker directly to unfenced `ax.raw_tree`, `browser.page_state`,
or the whole `run_ax_action` retry loop. They can send more commands internally
and sleep without checking this job's control state. A future implementation
must provide a reviewed guarded transport/cancellation boundary for **every**
internal dispatch, align nested retry budgets, and handle cleanup deferral. This
design changes none of those production functions.

## Failure matrix

| Failure or contested state | Per-job result | Lease/resource treatment | Sibling behavior |
|---|---|---|---|
| Existing site lease | Busy/contended receipt before CDP | No acquired token; never release incumbent | Continue |
| Invalid route/site/target hint | Invalid-input receipt | Release only if acquired; no fallback create | Continue |
| Multiple matching pages without explicit choice | Ambiguous-target receipt | Release; no attach/create/detach | Continue |
| `Target.getTargets` failure | Discovery failure | Release; no guessed handle | Continue if transport usable |
| Create error or malformed result | Creation failure/unknown outcome | Release; retain uncertainty, no second create or guessed cleanup | Continue if transport usable |
| Attach error before session acknowledgement | Attachment failure | Release; no guessed detach; track acknowledged created tab if any | Continue |
| Page enable or navigate error | Setup/navigation failure | Release; detach only its owned session and close only its owned target when unfenced | Continue |
| Navigation A sees interleaved B event | A still waits for A | Keep B's deque and lease untouched | B consumes its event |
| Stale generation or foreign answer identity | Ignore/quarantine evidence; no success | Keep only current owned token while within budget | Unaffected |
| Session queue overflow | Evidence-loss failure | Release; preserve failure reason rather than trusting incomplete history | Other queues continue |
| Read-only observation failure | Bounded retry or read failure | Same lease/budget until terminal; no question replay | Continue |
| Question callback exception | Submission failed/unknown | Release; no resubmission | Continue |
| Local `/sorry/` STOP | STOP receipt, extraction blocked | Release locally; defer all remaining CDP cleanup | Healthy jobs finish |
| Timeout while waiting | Timeout receipt, no extracted answer | Release locally; fence old generation and defer cleanup | Healthy jobs finish |
| Extraction failure after completion | Complete plus extraction-failed receipt | Release; preserve the independently established completion | Continue |
| STOP during/before extraction | STOP receipt; do not accept answer | Release locally; no further callback/CDP | Continue if local |
| Local cancellation | Cancelled receipt | Exact-token release; no late resurrection | Continue |
| Global STOP/cancellation | Receipt for each unfinished job | Release all their tokens; zero further job dispatch | All fenced |
| Transport closure | Separate transport-failed receipts | Local releases; no remote-cleanup assumptions | All affected |
| Cleanup exception | Primary result plus cleanup obligation | Release must still happen; never detach another handle | Continue if usable |

## Offline fixtures and verification map

Use `unittest`, `unittest.mock`, in-memory target lists, synthetic session ids,
and raw AX node dictionaries. Fake CDP logs exact method/parameter/session tuples
and supplies deterministic replies. Model events arrive in a specified order,
with no threads, network, browser launch, display, GPU, sleeps, or timing assertions.
The fixture's answers are synthetic strings; no live account or service is used.

| Fixture | In-memory target URL and role |
|---|---|
| Perplexity | Target `t-perplexity` at `https://www.perplexity.ai/`; requested route `https://www.perplexity.ai/fixture-route?q=offline`. |
| ChatGPT | Target `t-chatgpt` at `https://chatgpt.com/`; requested route `https://chatgpt.com/fixture-route?q=offline`. |
| Gemini | Target `t-gemini` at `https://gemini.google.com/`; requested route `https://gemini.google.com/fixture-route?q=offline`. |
| Independent redirect tab | Target `t-independent` starts at `https://www.google.com/search?fixture=offline` and is observed redirecting to `https://www.google.com/sorry/index`; its local STOP leaves the three healthy jobs active. |

Variants remove one healthy target to exercise create-on-miss, add a second
matching page to exercise ambiguity, and insert nonpage/deceptive targets. The
fake supports command/callback failure injection; committed cases are mapped
below. Raw AX controls
distinguish a user copy button, an assistant response region, streaming status,
answer text, and a trailer. Test-only rule names and synthetic routes carry no
claim about current real-site markup or accepted service endpoints.

The following names are exact methods in `tests/test_jq1_a6_multiwindow_harvest.py`.
`P` means `ProductionPins`; `R` means `ReferenceModelTests`. Related variants in
the same class extend these pins; this table does not assert production adoption.

| Requirement | Exact method(s) | Deterministic assertion |
|---|---|---|
| Current attach | P: `test_attach_url_reuses_first_matching_page_and_enables_its_session`; `test_attach_url_missing_page_raises_without_creating` | First matching page is reused and Page enabled on its returned session; missing page raises, with no create/navigation. |
| WP1-06 production gap | P: `test_wp1_06_production_ensure_tab_open_or_reuse_is_absent` | Only this method's absent-API access is decorated `expectedFailure`; no import-time access. |
| S1-F4 production gap | P: `test_s1_f4_production_navigate_must_preserve_other_session_load` | Separately decorated expected-failure exposes A's global purge of B's buffered load using an immediate fake load. |
| Event preservation | P: `test_wait_event_consumes_only_the_requested_session`; R: `test_interleaved_navigation_preserves_sibling_pending_load_and_purges_own_old_load` | Session filtering preserves B; model navigation also preserves B and rejects delayed old A navigation. |
| Reuse/create/direct route | R: `test_reuse_enables_correct_session_and_navigates_direct_route`; `test_no_match_creates_once_at_open_url_then_attaches_enables_and_routes` | Zero creates on match, exactly one on miss, explicit flattened attach, matching-session enable, and exact direct route. |
| Target and route validation | R: `test_non_page_and_other_origin_targets_are_not_reused`; `test_route_outside_configured_site_rejected_before_any_send` | Reject nonpages/deceptive origins; invalid requested route sends nothing. |
| Ambiguity | R: `test_multiple_matches_reject_without_creating_or_attaching`; `test_explicit_selection_resolves_ambiguity_but_invalid_hint_never_creates` | No arbitrary selection, create, or fallback from invalid hint. |
| Leases | R: `test_contention_is_per_site_origin_and_does_not_release_incumbent`; `test_stale_release_cannot_unlock_a_new_question` | Alias labels share a site lease; contention and stale releases preserve the incumbent token. |
| Create uncertainty | R: `test_creation_exception_releases_lease_without_retry_or_sibling_detach`; `test_malformed_creation_reply_never_guesses_target_or_retries` | No guessed target, repeated create, leaked lease, or sibling cleanup. |
| Attach uncertainty | R: `test_attach_exception_on_reused_tab_never_detaches_sibling`; `test_attach_exception_after_creation_closes_only_acknowledged_owned_target` | Never guess a session; only an acknowledged broker-created target can close. |
| Ordinary setup/cleanup errors | R: `test_enable_exception_cleans_its_attachment_not_sibling_or_caller_target`; `test_navigation_error_cleans_only_acknowledged_owned_resources`; `test_cleanup_error_keeps_primary_failure_and_releases_lease`; `test_unexpected_cleanup_exception_still_collects_primary_receipt` | Owned cleanup is scoped; primary failure, collection, and lease release survive cleanup error. |
| Healthy fan-out plus STOP | R: `test_three_healthy_sites_finish_with_provenance_while_independent_tab_stops` | All three healthy submissions occur before answer draining; shuffled observations produce correct provenance while the fourth tab stays fenced. |
| Timeout | R: `test_timeout_releases_only_its_lease_and_sibling_finishes`; `test_navigation_timeout_never_submits_question` | Independent timeout receipts, no later actions, no premature submission, healthy sibling finishes. |
| Immediate STOP fence | R: `test_stop_observed_during_enable_prevents_navigation`; `test_stop_between_compound_read_commands_fences_next_command`; `test_stop_after_completion_preserves_completion_and_blocks_extraction` | Queued navigation, another internal read, and extraction cannot bypass newly observed STOP. |
| Read retry bound | R: `test_idempotent_read_retries_are_bounded_without_question_resubmission`; `test_transient_read_can_recover_within_budget`; `test_stop_before_scheduled_read_retry_prevents_retry`; `test_stop_during_transient_read_error_prevents_retry` | Three-error limit, successful bounded recovery, one submission, and immediate retry cancellation. |
| Unknown submission | R: `test_ambiguous_submission_is_never_replayed` | One callback attempt, unknown receipt, released lease, no later actions. |
| Completion/extraction | R: `test_user_copy_button_and_generation_stop_button_are_not_completion`; `test_extraction_error_keeps_successful_completion_separate`; `test_empty_extraction_is_failure_not_empty_success` | Synthetic user-only/streaming fixtures cannot finish; extraction failure/empty output keeps completed status without success. |
| Raw AX boundary | P: `test_raw_ax_uses_explicit_session_with_immediate_fixture_success`; `test_pruned_ax_cannot_be_used_as_full_answer_text` | Immediate fake AX success uses explicit session with sleeps prohibited; chassis pruning cannot replace raw answer extraction. |
| Stale provenance | R: `test_unknown_session_and_old_question_envelopes_cannot_supply_answer`; `test_old_generation_on_reused_caller_session_cannot_finish_new_question` | Foreign session, old question, and prior lease generation cannot provide the current answer. |
| Buffered/untagged provenance | R: `test_untagged_completion_cannot_bypass_question_correlation`; `test_already_buffered_old_navigation_answer_is_rejected_on_consumption`; `test_already_buffered_old_question_answer_is_rejected_after_caller_rebind` | Missing tags and stale evidence queued before navigation/rebinding cannot provide an answer. |
| Persistent fence | R: `test_stop_tombstone_survives_lease_release_and_late_events` | Released site remains review-required; late events do not produce an answer or reopen it. |
| Reconciliation fence | R: `test_unknown_creation_outcome_blocks_readmission_until_review`; `test_failed_cleanup_blocks_readmission_without_holding_a_lease` | Uncertain creation or failed cleanup blocks new work without retaining a lease or issuing another command. |
| Early control/late acknowledgement | R: `test_stop_event_before_attach_reply_binding_prevents_enable_and_navigation`; `test_cancellation_during_create_retains_late_acknowledged_resource_identity` | Early STOP is replayed before enable; cancelled job records late ownership locally while sending nothing further. |
| Cancellation | R: `test_local_cancellation_releases_one_lease_and_sibling_finishes`; `test_global_cancellation_fences_every_job_and_future_start` | Correct local/global scope, released leases, no further dispatch or automatic admission. |
| Queue bound | R: `test_session_queue_overflow_is_explicit_and_does_not_drop_sibling_event` | Overflow terminates its job without evicting the sibling event. |
| Ownership | P: `test_close_only_detaches_owned_sessions_never_browser_close`; R: `test_success_releases_and_detaches_only_own_session_on_caller_tab`; `test_broker_owned_tab_is_closed_on_ordinary_success`; `test_caller_owned_session_is_never_detached_or_closed` | Port close never closes browser; model retains caller tabs/sessions and cleans only acknowledged owned resources. |
| Caller binding/attach collision | R: `test_caller_session_target_mismatch_is_rejected_before_any_command`; `test_attach_reply_cannot_claim_or_detach_a_known_caller_session` | Trusted binding mismatch sends nothing; an attachment cannot claim or clean up a known caller-owned session. |

The executable command is:

```sh
python3 -m unittest tests.test_jq1_a6_multiwindow_harvest
```

Only the two narrowly scoped known-production-gap cases may be expected failures;
all reference/model checks run normally. Unexpected success is review evidence
that the relevant production gap/pin needs reassessment, not permission to remove
checks silently. Actual VM test output belongs in the task report, including
failures observed during development. No timings here are labeled MEASURED.

## Wave Mind review criteria and remaining limits

1. Confirm the actual source commit and the metadata/audit drift; review current
   code separately from historical findings. Agree that this change contains only
   the design, its test-local reference, and task deliverables.
2. Review the missing MemoryWeb tool and safety contracts before adoption. Resolve
   submission authority, STOP scope, retry/idempotency semantics, cancellation,
   and retention explicitly. Passing fake tests cannot stand in for that review.
3. Approve normalized site ownership, multiple-match selection, explicit-target
   policy, direct route navigation, and create/attach uncertainty handling.
4. Review strict post-STOP/timeout no-dispatch fencing, including cleanup deferral
   and resource reconciliation. Preserve caller tabs, sibling sessions, and the
   existing prohibition on `Browser.close` for CdpPort.
5. Require a separate production decision for S1-F4 and an actual generation-safe
   routing design. Review loader/frame correlation, navigation races, stale replies,
   queue overflow, dispatch atomicity, cross-process leases, and crash recovery.
6. Review completion and extraction as independent contracts with current-question
   provenance. Supply provider AX fixtures/rules that establish answer regions,
   locale handling, maximum content, and truncation without relying on chassis text.
7. Reconcile bounded nested retry/deadline behavior with AX and ACT, including
   ambiguous sends and cleanup exceptions. Source constants are not guarantees of
   a whole-harvest timeout, prompt cancellation, or safe resubmission.
8. Run and inspect the stated offline gate, its two expected failures, and normally
   passing reference checks. Review the change through its collection PR; collection
   is not acceptance or authorization to implement/wire a production broker.

**AUSENTE:** MemoryWeb tools/safety/permissions; a production `ensure_tab` or harvest
broker in this change; a real provider completion/extraction adapter; production
generation/loader correlation; distributed leases; live-service results; live
harvest correctness; MemoryWeb conformance; performance measurements. The proposed
model demonstrates explicit isolated behavior for its finite in-memory fixtures.
