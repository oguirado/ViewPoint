# JQ1 A9: Offline site-replica blueprint

## 1. Status, evidence boundary and purpose

This is an architectural specification and an offline reference model. The
companion `tests/test_jq1_a9_offline_replicas_blueprint.py` defines five synthetic
replicas, their raw Accessibility trees, their border geometry, their permitted
transitions and their proposed site-profile rules. Its core production
integration boundary is `ax.prune`, border-box conversion, and
`schema.build_state` / `schema.validate_state`. One narrow adapter also probes
the existing executor's Potencia pointer fallback. A passing reference-model
test is not acceptance of a live website or implementation of an absent
production API.

The requested repository source is `example-owner/wender-agent`, ref
`bridge/integration`. The source ref observed in this session resolved to
`f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0`. The task metadata records the earlier
commit `100a0e8be3de4c2b7a6fc33901c3534b685015da`; this document describes the files
read at the explicitly requested ref, not an assumed default branch or an
unverified checkout of that earlier metadata commit.

Architectural evidence is limited to these six supplied files. Every source
citation below uses the line numbers of that observed snapshot:

- `wender/perceive/ax.py`
- `wender/perceive/browser.py`
- `wender/act/cdp_executor.py`
- `wender/learn/chrome_driver.py`
- `wender/learn/replay.py`
- `docs/spec/KNOWN_ISSUES_P1_WEB.md`

The supplied `tests/test_p1_demo_chrome.py` and
`tests/test_known_issues_p1_web.py` were read to avoid repeating their generic
checks. They are not additional evidence of a website's current behavior.
Existing P1 evidence reports Google AI mode and ChatGPT only. Perplexity and
Gemini below are **target replicas, not empirically observed acceptance runs**
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:1-7`). Their labels, geometry and event routes
are deliberately specified synthetic inputs.

All references to an HTML file under `tests/fixtures/sites/` describe the
**intended consumer layout**. No such fixture file is required or created by the
companion test. No browser, desktop, GPU, network request, screenshot pipeline,
or replay/admission run is required. Numerical coordinates and event schedules
are fixture constants, not observations or timing measurements.

## 2. Implemented contracts and their limits

| Surface | Implemented contract in the bound sources | Consequence for the blueprint |
|---|---|---|
| AX acquisition | `raw_tree(cdp, session_id)` enables Accessibility, requests `Accessibility.getFullAXTree`, returns `reply.get("nodes") or []`, retries at most three times and raises after exhaustion (`wender/perceive/ax.py:54-55`, `wender/perceive/ax.py:75-91`). | The fake exposes the same `send` entry point. Successful reads are pure observations. Scripted progress is explicit and never triggered by an AX read or a sleep. |
| AX release | `release(cdp, session_id)` sends `Accessibility.disable`, returning a boolean and swallowing transport exceptions (`wender/perceive/ax.py:94-100`). | Enable and disable acknowledgements do not advance replica state. |
| Raw scalar interpretation | `_value` reads a dictionary's `value`, normalizes booleans to the strings `true`/`false`, and stringifies other non-null values; `_props` maps named properties through it (`wender/perceive/ax.py:103-115`). | Typed CDP-style wrappers are the reference fixture schema. Their `type` labels are fixture validation requirements, not a claim that the pruner validates the complete CDP protocol. |
| Pruning | `prune(raw_nodes)` follows `childIds` in document order with a visited set. It emits only non-ignored chassis roles, counts emitted ancestors, and stops beneath an emitted `article` (`wender/perceive/ax.py:35-52`, `wender/perceive/ax.py:119-166`). | Validate raw graph structure before pruning. A valid single-root fixture has deterministic output even if its flat node array is reordered. Multiple-root order is not promised to be invariant under arbitrary input reordering. |
| Raw text boundary | `StaticText`, `InlineTextBox`, `paragraph`, `listitem`, `row` and `code` are not chassis roles; traversing a non-emitted parent does not make that parent survive (`wender/perceive/ax.py:35-44`, `wender/perceive/ax.py:127-154`). | Proposed extraction consumes the transient raw tree. STATE cannot recover paragraph structure or full answer text that pruning discarded. |
| Normalized names | Names undergo NFC normalization, whitespace collapse, stripping, a 64-byte UTF-8 cut at a code-point boundary, then escaping of backslash and vertical bar (`wender/perceive/ax.py:58-68`). | Keep complete source text in raw AX for extraction. Assertions about pruned names must use the production normalizer. |
| State attributes | Only `busy`, `checked`, `current`, `disabled`, `expanded`, `hidden`, `pressed` and `selected` are retained (`wender/perceive/ax.py:45-47`, `wender/perceive/ax.py:137-147`). | Slider range/value fields remain raw fixture data. An emitted status/name or retained attribute must make selection observable in the pruned state. Focus and numeric slider changes alone do not prove a changed chassis. |
| Canonical identity | Canonical lines use emitted depth, role, normalized name and optionally state attributes; nonempty snapshots end in a newline. `canon([])` is the empty string. Node fingerprints use ancestor roles, role and name; stable IDs additionally use the role/name ordinal (`wender/perceive/ax.py:158-203`). | Compare both exact and stable production hashes. Raw CDP IDs, backend IDs, border boxes and non-emitted text are not hashed. A raw streaming update can legitimately leave the pruned hash unchanged. |
| Context | Context includes normalized URL, title hash, actionable count, role set, stable chassis hash and prune version, but context matching does not vote on the chassis hash (`wender/perceive/ax.py:206-227`). | Do not infer answer completion, text equality or a safe action from a matching chassis hash alone. |
| Border boxes | `boxes(cdp, session_id, nodes)` primes `DOM.getDocument`, calls `DOM.getBoxModel` for each emitted node's `backendNodeId`, and uses an eight-value `model.border`; missing IDs, send failures and absent/falsy/wrong-length borders are skipped. `_quad_box` rounds extremal coordinates into integer CSS pixels (`wender/perceive/browser.py:19-41`). | Reference quads are integer rectangles. The fixture uses `model.border`, not the executor-only `box` or `content` alternatives. An eight-value nonnumeric quad or a non-dictionary reply/model can still raise outside the send exception handler; this is not a general malformed-CDP validator. |
| STATE handoff | `page_state(cdp, session_id, with_boxes=True)` obtains raw AX, prunes it, reads URL/title/viewport, resolves boxes, releases AX and passes the nodes, hashes, prune metadata, raw count, boxes, viewport and actionable roles into `schema.build_state` (`wender/perceive/browser.py:44-58`). The module's boundary keeps raw AX/HTML local (`wender/perceive/browser.py:1-5`). | The test reproduces this handoff directly in RAM with fixture URL/title/viewport. It does not depend on `viewport.js`, page evaluation, or a real browser. Raw text and iframe evidence stay inside the local proposed adapter. |
| Executor operations | `OPS` is exactly `click`, `focus`, `type`, `select`. The fixed JS bodies use `this.click()`, `this.focus()`, value assignment and input/change events (`wender/act/cdp_executor.py:14-29`). | `press`, `insert_text`, hover-path and slider-keyboard operations are not implemented executor operations in this corpus. Their fake transitions are proposals, not additions to `OPS`. |
| Executor invocation | `run_ax_action(cdp, session_id, nodes, action, read_nodes, retries=3, wait_ms=250, sleep=time.sleep, allow_coordinates=False)` validates an AX target and action. `expect` accepts only `target_states` and `target_name` (`wender/act/cdp_executor.py:61-108`, `wender/act/cdp_executor.py:217-224`). | Production probes use a current pruned target ID, scripted observations and injected no-op sleep. They do not pass proposed profile rules as executor expectations. |
| JS action transport | The executor scrolls the backend node, resolves a remote object and calls a fixed function with value arguments. JS exceptions become errors (`wender/act/cdp_executor.py:175-214`). | A fake can recognize these specific function bodies and backend IDs. It must reject unknown methods or functions rather than silently acknowledging them as successful actions. |
| Optional pointer fallback | For a reversible click only, after ordinary attempts fail verification, an enabled coordinate fallback resolves a box and emits moved/pressed/released events at its integer center (`wender/act/cdp_executor.py:150-171`, `wender/act/cdp_executor.py:314-364`). | An explicit `expanded=true` expectation can exercise the implemented fallback. There is no production multi-segment hover path in this function. |
| WP1-03 remaining defect | Without an expectation, `_verify_expect` treats a surviving target as success; the ordinary action loop then returns success even when its before/after hashes are identical (`wender/act/cdp_executor.py:111-141`, `wender/act/cdp_executor.py:283-305`). | The desired automatic fallback after a no-op JS click is still a gap. Keep the desired-behavior assertion separate and narrowly marked `unittest.expectedFailure`; do not call the current receipt evidence that a pointer menu opened. |
| Irreversible gate | Irreversible actions require an unambiguous L0/L1 match; coordinate fallback is disallowed (`wender/perceive/ax.py:298-300`, `wender/act/cdp_executor.py:226-258`, `wender/act/cdp_executor.py:314-315`). | Fixture menu interactions are reversible. A STOP wall never grants permission to dispatch another action. |
| Driver | `ChromeDriver(cdp, session_id, start_url, vision=None)` returns `(state, screen_fp)` from `observe`. Candidates are actionable links/buttons only; `act` reacquires raw/pruned nodes and calls the executor with `retries=2`, without enabling coordinate fallback (`wender/learn/chrome_driver.py:88-140`). | A declared menuitem, slider, hover or keyboard route is not proof that `ChromeDriver` can discover or execute it. This task does not extend the driver. |
| RAM screenshot boundary | The driver only captures/decodes a screenshot when given a `VisionEngineAdapter`; the bytes and frame are local values (`wender/learn/chrome_driver.py:98-106`). | The blueprint suite supplies no vision adapter and captures no screenshots; its unsupported-method negative check rejects a fake screenshot request. If a later consumer generates screenshot bytes, they must remain in RAM. Screenshot codec tests belong elsewhere. |
| Replay boundary | `replay_and_admit(draft, driver, verified_by, runs=2)` resets/observes/acts, evaluates post-STATE checkpoints, compares run diff summaries, attaches evidence and calls admission (`wender/learn/replay.py:71-142`). Its current predicates are role/name presence, absence and text containment over STATE names (`wender/learn/replay.py:23-68`). | A deterministic fixture transition is not a replay, a skill admission, or an acceptance run. This task neither duplicates that pipeline nor invents evidence from it. |

### 2.1 Production signatures that remain AUSENTE

**AUSENTE** means no implementation or callable signature is supplied by this
corpus. A requested shape in a known-issues document is not an implemented API.

| Requested production surface | Production call signature | Evidence and local treatment |
|---|---|---|
| `wender.perceive.wait.wait_done` / `wait_done` | **AUSENTE** | WP1-02 requests a generic AX waiter and data-driven rules. The notation `wait_done(rules)` in the wanted column is an intention, not a definition (`docs/spec/KNOWN_ISSUES_P1_WEB.md:12`). The reference adapter below is local test code only. |
| `wender.perceive.text.extract` / `text.extract` | **AUSENTE** | WP1-04 requests raw text grouping and a profile trailer cut. The notation `extract(region_start, region_end)` is an intention, not a signature definition (`docs/spec/KNOWN_ISSUES_P1_WEB.md:14`). |
| `wender.perceive.stopstate.detect` / `stopstate.detect` | **AUSENTE** | WP1-05 requests STOP detection and a human handoff; it provides no function signature (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`). |
| Executor `press`, `insert_text` | **AUSENTE** as implemented executor operations | WP1-03 requests them; the implemented `OPS` tuple excludes them (`docs/spec/KNOWN_ISSUES_P1_WEB.md:13`, `wender/act/cdp_executor.py:14-29`). `type` is value replacement with JS events, not a synonym for incremental keyboard insertion. |
| Hover-path / submenu corridor / slider keyboard dispatcher | **AUSENTE** as production call signatures | The executor has four fixed operations and one center-click fallback, not an arbitrary pointer path or key-event operation (`wender/act/cdp_executor.py:14-29`, `wender/act/cdp_executor.py:314-350`). The proposed fixture input vocabulary must stay visibly local. |

The companion keeps missing-module imports inside individual
`unittest.expectedFailure` test methods with comments naming WP1-02, WP1-04 or
WP1-05. It does not make the whole module an expected failure, interpret a failed
import as a passing implementation, or invent a production call signature. Any
known executor/driver defect is likewise represented by an assertion of the
wanted behavior, separate from passing fixture and production-integration tests.

WP1-01 applies to all local proposed profiles: decisions use AX roles, names,
document order, containment, states, and explicit STOP evidence. They do not use
`data-message-author-role`, CSS selectors, XPath or HTML scraping. The historical
fragile selector is mentioned here solely to identify the forbidden dependency
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:11`).

## 3. Reference-model boundaries

There are three deliberately different representations:

1. **Fixture declaration:** immutable in-memory data describing a finite set of
   states, raw AX nodes, border quads, URL/title/viewport data, and allowed event
   transitions. IDs and coordinates are synthetic and deterministic.
2. **Production projection:** `ax.prune(raw)` and the STATE built from the
   resulting nodes and production hashes. Only this layer is asserted to use
   existing Wender implementations.
3. **ProposedProfileAdapter:** a local, data-driven evaluator of completion,
   answer extraction and STOP rules over transient raw snapshots. Its schema and
   invocation contracts are proposed test contracts; none of its methods is an
   implementation of an absent `wender.perceive.*` module.

Observations are idempotent. `Accessibility.getFullAXTree`, box requests and
profile evaluation cannot advance a response, finish a stream, close a menu,
or clear a wall. Only a declared event advances state. Repeating a snapshot read
must return equivalent data, preferably a deep copy so callers cannot corrupt
the reference declaration.

Each permitted `(state, event)` pair has exactly one successor. No duplicate
pair, unknown source or target state, dangling AX child, duplicate raw node ID,
invalid backend ID, malformed typed property, or malformed border quad belongs
to a conforming declaration. An undeclared transition raises before changing
the current state or observation data. It is not an implicit no-op, a success
receipt, or an opportunity to invent a transition.

Use explicit scripted ticks for streaming and research progress, and explicit
pointer/key/text events for interaction. A test may inject `sleep=lambda _: None`
into production executor probes; that callback is neither a clock nor a state
transition. The suite never polls a wall clock or uses `time.sleep` to advance
fixture behavior.

The terminal Google wall is absorbing for action purposes: it has no outgoing
interactive transitions. AX/geometry reads remain available so the caller can
describe the STOP and hand control to a human. No fixture transition solves,
evades, retries through, or automatically dismisses a CAPTCHA. This follows the
requested WP1-05 behavior (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`).

## 4. Raw AX and border-quad contract

The reference uses a flat `Accessibility.getFullAXTree` response:

```json
{
  "nodes": [
    {
      "nodeId": "potencia",
      "ignored": false,
      "role": {"type": "role", "value": "menuitem"},
      "name": {"type": "computedString", "value": "Potencia"},
      "backendDOMNodeId": 1004,
      "childIds": [],
      "properties": [
        {"name": "expanded", "value": {"type": "boolean", "value": false}}
      ]
    }
  ]
}
```

This fragment illustrates the proposed node shape; the companion's complete
state declarations provide the root, ancestors, siblings, exact backend IDs and
geometry. It is not a captured Chromium tree.

| Field | Reference shape | Meaning |
|---|---|---|
| `nodeId` | Nonempty string, unique within a raw snapshot | Synthetic raw identity for child edges; not a CSS or DOM locator. |
| `ignored` | Boolean; exactly `false` in all declared snapshots | In production, an ignored node is traversed without being emitted; this does not make an ignored subtree automatically disappear from traversal. |
| `role` | Object with `type: "role"` and string `value` | Exact case-sensitive AX role. `StaticText` and `RootWebArea` retain their AX spelling. |
| `name` | Object with `type: "computedString"` and string `value` | Full accessible text, before production normalization and truncation. |
| `backendDOMNodeId` | Deterministic positive integer | Key for geometry and specific fake CDP action targets. It is not hashed by `ax.prune`. |
| `childIds` | Ordered list of known node ID strings | Sole reference order for the raw depth-first walk. No cycles, duplicate edges or multiple parents in these fixtures. |
| `properties` | List of unique named typed-value objects | Boolean properties use JSON booleans; numeric slider metadata uses integers; scalar strings remain strings. Only production `STATE_ATTRS` survive pruning. |
| `value` | Optional typed-value object: `type: "number"` with an integer, or `type: "string"` with a string | Raw slider/combobox value. This is separate from `properties` and does not become a hashed `STATE_ATTRS` entry. |

The replica includes raw paragraph/list/code text and an opaque article trap to
make the loss boundary observable. A provider article can have raw descendants,
but `ax.prune` does not emit any of those descendants after emitting the article
itself. A text extractor cannot use the absence of those descendants in STATE
as evidence that the raw answer is empty (`wender/perceive/ax.py:137-154`).

For a rectangle `[x, y, w, h]` in integer CSS pixels, the required clockwise
border quad is:

```text
[x, y, x + w, y, x + w, y + h, x, y + h]
```

The FakeCDP reply is exactly the compatible nesting
`{"model": {"border": quad}}`. Coordinates, width and height are integers, not
booleans; widths/heights are positive. Applying the production
`browser._quad_box` produces the original rectangle. A center click uses
`x + w // 2`, `y + h // 2`, matching the implemented fallback
(`wender/perceive/browser.py:19-22`,
`wender/act/cdp_executor.py:317-335`). A hover route instead consists of several
explicit coordinates and belongs to the local proposed transition table.

The production STATE construction is the same handoff used in
`wender/perceive/browser.py:54-58`: the pruned nodes, `kind="cdp"`, fixture URL and
title, both `ax.snapshot_sha` variants, prune name/version, raw count, boxes,
integer viewport and `ax.ACTIONABLE_ROLES`. `schema.validate_state(state)` must
return `[]`. The reference does not extend the STATE schema with raw text,
frame handles, HTML, floating-point coordinates or proposed rule objects.

## 5. Intended self-contained HTML/JS fixtures

### 5.1 Consumer layout and semantic purpose

These paths are recipes for a future consumer, not files that this task creates:

| Replica ID | Intended file | States | Required fixture behavior |
|---|---|---|---|
| `chatgpt_potencia` | `tests/fixtures/sites/chatgpt_potencia.html` | `closed`, `menu`, `slider`, `selected` | A model menu contains the Radix-style `Potencia` item. Its programmatic JS click is a declared no-op; a declared pointer sequence opens the submenu/slider. A selected effort has an emitted observable marker as well as raw slider metadata. |
| `chatgpt_stream` | `tests/fixtures/sites/chatgpt_stream.html` | `idle`, `streaming`, `streaming_more`, `settling`, `done` | A user turn already has `Copiar mensaje`. The assistant turn streams beneath `ChatGPT dijo:` with `Detener` and/or busy evidence until completion. The final assistant copy button is distinct from the user-copy trap. Footer chrome and an opaque article exercise exclusion boundaries. |
| `perplexity_lpath` | `tests/fixtures/sites/perplexity_lpath.html` | `closed`, `menu`, `entered`, `corridor`, `submenu`, `selected` | A target hover menu requires entry into its parent, horizontal movement through a corridor, then vertical movement into a submenu. A shortcut or a missing event is not silently promoted to the valid L-path. |
| `gemini_research` | `tests/fixtures/sites/gemini_research.html` | `closed`, `models`, `levels`, `configured`, `research`, `collapsed`, `done` | A target model menu opens a level submenu, records a level, and opens a Deep Research panel. Panel collapse is a distinct state from answer completion. Research only completes on its declared scripted event and completion evidence. |
| `google_ai_wall` | `tests/fixtures/sites/google_ai_wall.html` | `cookie`, `search`, `ai`, `query`, `wall` | Cookie handling, search, AI-mode selection and text entry precede a synthetic `/sorry/` wall. URL/frame/text indicators yield terminal STOP and a human handoff. There is no wall-solving route. |

The first two recipes are motivated by the reported ChatGPT copy/stream/menu
defects, and the last by the reported Google wall
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:11-15`). The Perplexity L-path and Gemini level /
Deep Research stories are task-requested targets; the supplied source does not
establish their real site's labels, geometry or acceptance behavior.

### 5.2 HTML construction contract

Each intended `.html` is one complete UTF-8 document containing its declaration,
inline script and presentation. It needs no CDN, package install, Radix runtime,
remote font, image, stylesheet, external script, cookie, account, service worker,
storage API or real provider frame. “Radix-style” specifies pointer-sensitive
behavior; it does not claim that the offline file contains Radix itself.

The common renderer constructs elements with `document.createElement` and keeps
references in a `Map` keyed by the synthetic raw node IDs. It never discovers
controls with `querySelector`, `querySelectorAll`, XPath, CSS selectors or site
data attributes. Event listeners are attached to the stored element references
when that state's tree is rendered. These references are fixture internals;
automation decisions still use the AX projection.

Use native elements where their semantics match (`button`, `input`, `p`, `h2`,
`article`, `code`) and explicit ARIA roles where needed (`menu`, `menuitem`,
`group`, `status`, `dialog`, `region`, `listitem`, `row`). Put declared names in
text content and/or `aria-label`; set retained state properties with their ARIA
counterparts. A slider uses `input type="range"` with integer min/max/step/value
and an accessible name. Its raw value alone cannot signal a production pruned
change; update the declared emitted selection marker too
(`wender/perceive/ax.py:45-47`, `wender/perceive/ax.py:137-147`).

Render the root's title as `document.title`; Chromium provides its own actual
`RootWebArea`. The renderer preserves declared parent/child order. Full answer
text becomes text-node content inside the appropriate paragraph, heading,
list-item, row or code container; never truncate it to a pruned accessible name.
Use inline element styles and declared integer rectangles for the pointer
targets. A renderer can position child elements relative to their parent's
declared origin while keeping border widths and box sizing explicit.

Raw node IDs, backend IDs and the exact flat tree here are **FakeCDP fixture
identities**. An actual Chromium renderer can add generic/text wrapper nodes,
assign different backend IDs, or calculate different font metrics. A future
browser consumer must verify and map its captured semantics and geometry; this
blueprint does not claim an unrun HTML document already reproduces Chrome's raw
numeric identifiers or pixels. The current conformance suite requires neither
that browser run nor the intended fixture files.

### 5.3 Event wiring, timers and the stop wall

The inline script owns a current state, the immutable snapshots and the exact
transition rows. Its one reducer checks `(current_state, normalized_event)` in
the table before changing state. It rerenders from the declared successor
snapshot only after a valid transition. An unknown transition throws before any
mutation. No mutation observer, network response, animation timer, randomness,
clock read, wall-clock delay or repeated AX read may advance the reducer.

Ordinary controls normalize a click, key or text event into the corresponding
reference event. The Potencia item intentionally distinguishes a programmatic
`this.click()` from a pointer press/release: the former follows its declared
unchanged-state row and the latter follows the pointer route. Do not accidentally
bind both handlers so a single release advances two states. The Perplexity
renderer tracks the ordered pointer segments and cannot open the submenu by
teleporting directly to its final coordinate. Slider keys and text insertion
are local proposed fixture events, not additional production executor ops.

Expose an explicit local fixture stepping hook for streaming chunks, settling
and research completion. It consumes only events present in the transition
table. It must not masquerade as `wait_done`, a production clock, or a live
provider response. A complete turn takes exactly the declared steps regardless
of CPU speed or how often the observer reads its tree.

The Google fixture changes its **synthetic snapshot URL** to the declared
`/sorry/` value. It must never navigate to Google or request a real CAPTCHA. A
visible CAPTCHA panel may use text and an inert local `iframe` with `srcdoc` and
an accessible title; it must have no remote `src`. Its frame signature is
synthetic evidence in the snapshot. A future real-browser URL-path test would
need an explicitly local route; it cannot treat a `file:` address as if it had
already navigated to an observed Google address.

After STOP, the fixture may display the reason and human-handoff status and
answer read-only observation requests. It must not create a hidden bypass,
automated retry, CAPTCHA submission, fake success click, or outgoing transition
from `wall` (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`).

## 6. Exact synthetic snapshots and transitions

### 6.1 Snapshot metadata and geometry

The executable fixture declarations are `_node`, `_snapshot`, the five state
factories, `NODE_CATALOG`, `BACKEND` and `REPLICAS` in the companion module.
These names identify proposed fixture data, not additional empirical sources.
A snapshot has exactly `url`, `title`, `nodes`, `border_quads`, `viewport`, and
`frames`. Each frame record has string `name` and `url`; the URLs are inert
strings, never fetched. Its viewport is:

```json
{"w":1024,"h":768,"scroll_x":0,"scroll_y":0,"doc_w":1024,"doc_h":2048,"dpr_ppm":1000000}
```

The ordered node catalog below defines every backend ID as
`1000 + NODE_CATALOG.index(nodeId)`. Omitted nodes do not renumber surviving IDs.

```text
root, main, model, menu, potencia, effort, status,
user, user_heading, prompt, prompt_text, user_copy,
assistant, assistant_heading, article, answer_heading,
answer_heading_text, paragraph, answer_text, list, item,
item_text, table, row, row_text, code, code_text,
trailer, trailer_text, assistant_copy, stop, footer,
footer_text, parent_menu, submenu, choice, fast, thinking,
level, levels, high, research_button, panel, collapse,
panel_heading, cookies, reject, ai_mode, query, wall, captcha
```

Every state has `root: RootWebArea -> main: main`. `root` has no border quad;
requesting its box raises the declared no-layout error, which production
`browser.boxes` skips. Every other raw node has a quad, even if it will not survive
pruning. For zero-based index `i` in the state's flat `definitions` list, the
default rectangle is `[24, 32 + 24*i, 360, 20]`. The only overrides are:

| Replica / raw node | `[x,y,w,h]` | Exact `model.border` | Center |
|---|---|---|---|
| `chatgpt_potencia` / `potencia` | `[120,100,160,40]` | `[120,100,280,100,280,140,120,140]` | `(200,120)` |
| `chatgpt_potencia` / `effort` | `[300,100,240,32]` | `[300,100,540,100,540,132,300,132]` | `(420,116)` |
| `perplexity_lpath` / `parent_menu` | `[100,100,160,40]` | `[100,100,260,100,260,140,100,140]` | `(180,120)` |
| `perplexity_lpath` / `submenu` | `[300,180,160,80]` | `[300,180,460,180,460,260,300,260]` | `(380,220)` |
| `perplexity_lpath` / `choice` | `[300,200,160,40]` | `[300,200,460,200,460,240,300,240]` | `(380,220)` |

Typed properties are ordered by property name. A boolean becomes
`{"type":"boolean","value":true/false}`, an integer becomes
`{"type":"integer","value":integer}`, and a string becomes a `token` value.
The optional top-level value uses `number` for an integer and `string` for text.
No box is inserted into a raw AX node. All declaration data is compatible with
the existing integer/string canonical serializer used by the tests.

### 6.2 Transition row schema

Each row has exactly five positions:

```text
(source_state, local_event, raw_AX_nodeId_or_empty, string_value, destination_state)
```

The key is the first four positions. The empty string is an actual value, not a
wildcard. Target IDs here are raw fixture IDs such as `potencia`; they are not
the hashed `id` accepted by the production executor. `move`, `press`,
`insert_text`, `submit`, `tick`, `pointer_click` and `js_click` in these tables
are **local event vocabulary**, not production API signatures.

The following are all 28 rows across five replicas and 27 states. Unlisted
combinations fail with `MissingTransition`. A table is complete for its declared
scenario without accepting every conceivable user action.

### 6.3 ChatGPT Potencia submenu and slider

Initial state: `closed`. No state is declared terminal. `model` is
`button "Modelo"`; the menu and its trigger expose `expanded` state. Opening
the synthetic submenu adds `effort: slider` beside `potencia` under `menu`.
`effort` has `valuemin=1`, `valuemax=4`, and raw numeric value `2` in `slider` or
`3` in `selected`. Its accessible name and the emitted `status` both change from
`Potencia Media` to `Potencia Alta` for selection. This is the observable marker
required because raw slider values alone are not hashed.

| From | Event | Target | Value | To |
|---|---|---|---|---|
| `closed` | `js_click` | `model` | `""` | `menu` |
| `menu` | `js_click` | `potencia` | `""` | `menu` |
| `menu` | `pointer_click` | `potencia` | `""` | `slider` |
| `slider` | `press` | `effort` | `ArrowRight` | `selected` |
| `selected` | `press` | `effort` | `ArrowLeft` | `slider` |
| `menu` | `press` | `model` | `Escape` | `closed` |
| `slider` | `press` | `effort` | `Escape` | `closed` |

The `menu -> menu` row deliberately models the reported `this.click()` failure;
it is distinct from an undeclared event (`docs/spec/KNOWN_ISSUES_P1_WEB.md:13`).
Arrow keys and Escape are proposed fixture events. The production `press`
operation remains AUSENTE.

### 6.4 ChatGPT streaming, the user-copy trap and footer chrome

Initial state: `idle`. Its `user: group "Usuario"` contains
`heading "Tú dijiste:"`, paragraph text `Resume esta idea`, and the already
present user `button "Copiar mensaje"`. The assistant container is
`group "Respuesta de ChatGPT"` with `heading "ChatGPT dijo:"`, an opaque
`article "Respuesta"`, and an assistant copy button only in `done`.

| From | Event | Target | Value | To |
|---|---|---|---|---|
| `idle` | `submit` | `""` | `""` | `streaming` |
| `streaming` | `tick` | `""` | `chunk` | `streaming_more` |
| `streaming_more` | `tick` | `""` | `stop_removed` | `settling` |
| `settling` | `tick` | `""` | `assistant_copy` | `done` |

`streaming` and `streaming_more` have `button "Detener"` and assistant
`busy=true`. Their raw answer text differs while their production pruned hashes
are equal, because the body is beneath an opaque article. `settling` has no
stop button and `busy=false`, but still lacks the assistant copy; it must remain
PENDING. `done` adds that final copy button. Two stable observations of `done`
are required by the proposed waiter; reaching the `done` fixture state is not
itself a call to an implemented production completion API.

The raw article contains these blocks in order: `heading`/`StaticText` Resumen;
the answer `paragraph`/`StaticText`; `list`/`listitem`/`StaticText` Primer punto;
`table`/`row`/`StaticText` clave valor; `code`/`StaticText` `print('hola')`; and an
in-region trailer paragraph starting with `ChatGPT puede cometer errores`.
The separate `contentinfo "ChatGPT puede cometer errores"` footer contains
`Última respuesta. Respuesta completada.` It is outside the assistant container.
The complete paragraph is:

```text
Una respuesta determinista conserva todo el texto original, incluidos acentos, listas y código, aunque supere sesenta y cuatro bytes.
```

The expected extracted answer has exactly five blocks (heading, paragraph,
listitem, row, code), retains that complete paragraph, excludes the user prompt,
cuts the in-region trailer, and never enters the outer footer. This models the
reported WP1-02 and WP1-04 failures without claiming a production fix
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:12-14`).

### 6.5 Perplexity L-path target

Initial state: `closed`. `parent_menu` is `menuitem "Modelos"`. The parent entry
sample `(180,120)`, horizontal elbow `(380,120)` and vertical destination
`(380,220)` are all required in that order. The `entered` and `corridor` states
are explicit pointer-history states even though their visible AX can equal the
open menu's AX. The selection is `menuitemradio "Modelo objetivo"`.

| From | Event | Target | Value | To |
|---|---|---|---|---|
| `closed` | `js_click` | `model` | `""` | `menu` |
| `menu` | `move` | `""` | `180,120` | `entered` |
| `entered` | `move` | `""` | `380,120` | `corridor` |
| `corridor` | `move` | `""` | `380,220` | `submenu` |
| `menu` | `move` | `""` | `280,170` | `closed` |
| `entered` | `move` | `""` | `280,170` | `closed` |
| `submenu` | `js_click` | `choice` | `""` | `selected` |

The diagonal trap `(280,170)` closes the menu. Skipping parent entry or skipping
the elbow is an undeclared transition and raises without changing state. In
`selected`, the menu disappears, the button is named `Modelo objetivo`, and the
status is `Modelo seleccionado`. These are target-fixture conventions; they
are not observed live Perplexity behavior.

### 6.6 Gemini level submenu and Deep Research target

Initial state: `closed`. `model` is `button "Gemini"`. Its synthetic model menu
contains `Rápido` (checked), `Razonamiento` (unchecked), and `menuitem "Nivel"`.
Only the declared `Nivel -> Alto` route is actionable in this reference; the
presence of another radio item does not invent a transition for it.

| From | Event | Target | Value | To |
|---|---|---|---|---|
| `closed` | `js_click` | `model` | `""` | `models` |
| `models` | `js_click` | `level` | `""` | `levels` |
| `levels` | `js_click` | `high` | `""` | `configured` |
| `configured` | `js_click` | `research_button` | `""` | `research` |
| `research` | `js_click` | `collapse` | `""` | `collapsed` |
| `collapsed` | `tick` | `""` | `research_complete` | `done` |

Configuration closes the menu and emits `status "Nivel Alto"`. `research`
shows `complementary "Deep Research"`, `heading "Plan de investigación"`,
`button "Ocultar Deep Research"`, busy state and `button "Detener"`.
`collapsed` removes the panel while the status and assistant remain busy and
the stop button remains. Collapsing the panel is PENDING, not completion.
`done` removes stop/busy evidence and adds `button "Copiar respuesta"` inside
`group "Respuesta de Gemini"`, after `heading "Gemini"` and the answer article.
The answer contains heading and paragraph blocks only. The whole route is a
synthetic target; no empirical Gemini acceptance run is asserted.

### 6.7 Google AI mode and terminal CAPTCHA wall

Initial state: `cookie`; the sole terminal state is `wall`. The cookie dialog is
`Antes de continuar`, followed by `button "Rechazar todo"`. The AI-mode control
is `button "Modo IA"`, and the query is `combobox "Pregunta a Google"`.

| From | Event | Target | Value | To |
|---|---|---|---|---|
| `cookie` | `js_click` | `reject` | `""` | `search` |
| `search` | `js_click` | `ai_mode` | `""` | `ai` |
| `ai` | `insert_text` | `query` | `¿Qué es Wender?` | `query` |
| `query` | `press` | `query` | `Enter` | `wall` |

`query` has the exact inserted string as its raw value and the emitted
`status "Consulta preparada"`. `wall` changes the snapshot URL to
`https://www.google.com/sorry/index`, contains
`alert "Our systems have detected unusual traffic"` and raw
`Iframe "reCAPTCHA"`, and supplies the inert frame record
`{"name":"reCAPTCHA","url":"https://www.google.com/recaptcha/api2/anchor"}`.
The raw `Iframe` is not a chassis role; frame evidence is still available to the
local STOP adapter. URL, frame and text each suffice independently. The expected
combined result is:

```json
{"status":"STOP","reasons":["url","frame","text"],"handoff":"human"}
```

No outbound edge exists from `wall`. A repeated Enter raises and preserves the
snapshot. The model neither solves nor evades the challenge, consistent with
WP1-05 (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`).

## 7. Proposed site-profile rules and invocation contracts

Everything in this section is a **local proposal**. Production signatures for
`wait_done`, `text.extract` and `stopstate.detect` remain **AUSENTE**.

### 7.1 Exact profile schema

`PROFILES[site]` has exactly these five keys:

```json
{
  "schema": "offline-site-profile/v1",
  "site": "chatgpt_stream",
  "wait_done": {
    "anchor": {"role":"heading","name":"ChatGPT dijo:"},
    "container": {"role":"group","name":"Respuesta de ChatGPT"},
    "completion": {"role":"button","name":"Copiar mensaje"},
    "blocking": [{"role":"button","name":"Detener"}],
    "stable_reads": 2,
    "max_reads": 4
  },
  "text.extract": {
    "region_start": {"role":"heading","name":"ChatGPT dijo:"},
    "container": {"role":"group","name":"Respuesta de ChatGPT"},
    "region_end": {"role":"button","name":"Copiar mensaje"},
    "block_roles": ["paragraph","heading","listitem","row","code"],
    "trailer_prefixes": ["ChatGPT puede cometer errores","Última respuesta","Respuesta completada"]
  },
  "stopstate.detect": {
    "url_path_prefixes": ["/sorry/"],
    "frame_name_contains": ["recaptcha","turnstile"],
    "frame_url_contains": ["/recaptcha/","challenges.cloudflare.com"],
    "text_contains": ["unusual traffic"],
    "terminal": true
  }
}
```

All node predicates have exactly string `role` and `name`; matching is exact and
case-sensitive. The schemas admit no selector, XPath, data-attribute or DOM-ID
locator field. `stable_reads` and `max_reads` are integers with
`1 <= stable_reads <= max_reads`. The common STOP rules are copied into every
profile. Site-specific completion/extraction boundaries are:

| Profile | Anchor name (`heading`) | Container name (`group`) | End name (`button`) | Wait/extract applicability |
|---|---|---|---|---|
| `chatgpt_potencia` | — | — | — | Both rule values are `null`; this is a menu route. |
| `chatgpt_stream` | `ChatGPT dijo:` | `Respuesta de ChatGPT` | `Copiar mensaje` | Synthetic assistant turn. |
| `perplexity_lpath` | — | — | — | Both rule values are `null`; this is a menu route. |
| `gemini_research` | `Gemini` | `Respuesta de Gemini` | `Copiar respuesta` | Target synthetic research answer. |
| `google_ai_wall` | `Respuesta de IA` | `Respuesta de Google` | `Copiar respuesta` | Proposed success boundary only; it never appears on this wall-ending route. |

Do not call a local wait/extract method with `null` rules. A Google wall is not
turned into a successful answer by having a proposed success schema.

### 7.2 `ProposedProfileAdapter.wait_done`

Local signature only:

```python
ProposedProfileAdapter.wait_done(snapshots, rules, *, sleep)
```

The caller supplies an iterable of scripted snapshots and an injected no-op
sleep. Consume at most four snapshots using the rule's budget. For each sample:

1. Walk raw AX from `root` in child order, retaining article interiors and
   recording parent edges. Choose the **last** matching assistant anchor.
2. Find that anchor's matching container ancestor. Walk strictly after the
   anchor until the first matching completion button inside that container.
   Leaving the container or exhausting the tree without an end gives an empty
   region. Never borrow the user's copy, an earlier answer's copy, or a later
   turn's copy.
3. Block if any raw node matches a profile `blocking` predicate. In addition,
   this proposed reference has a fixed generic gate: **any raw-node property
   `busy=true` blocks**, even if it lies outside the selected answer container.
4. For a nonempty, unblocked region, serialize the entire raw region with
   `schema.dumps`. This includes raw IDs/properties and full text; it is not a
   pruned hash or an extracted-text-only signature. Count consecutive identical
   signatures. Reset on an empty region, a blocker, or a changed signature.
5. Return `{"status":"DONE","reads":n}` upon two stable samples. Otherwise
   return `{"status":"PENDING","reads":n}` at iterable exhaustion or the
   four-read budget. The injected callback receives `0` between eligible reads;
   it performs no wall-clock wait and advances no state.

The constants two/four are proposed fixture policy, not measured site timings.
The reference verifies both the user-copy trap and a late assistant heading
after an old copy. The transcript `streaming_more, settling, done, done` returns
DONE on read four. Four idle samples followed by two done samples still return
PENDING after four; the budget cannot consume those later successes.

### 7.3 `ProposedProfileAdapter.extract`

Local signature only:

```python
ProposedProfileAdapter.extract(snapshot, rules)
```

Use the same last-anchor, matching-container, in-container-end region. Without
a complete bounded region, return `[]`; partial streaming text is not presented
as a completed extraction. Within it, consider raw `StaticText` nodes only. Stop
before the first text whose value starts with a configured trailer prefix
(case-sensitive). For each other text leaf, find its nearest ancestor whose
role is in `block_roles` and is still inside the bounded region. Skip leaves
with no such ancestor. Consecutive leaves sharing that ancestor join with one
ASCII space; otherwise start another block. Return ordered objects containing
exactly `role` and `text`.

The full value of each text leaf is retained; the reference does not apply
`ax.normalize_name` or the 64-byte cut. The one-space grouping policy is a
specific proposed behavior, not a guarantee of arbitrary rich-text/code
round-tripping. The supplied code fixture has one complete StaticText leaf.
This preserves the requested role grouping while excluding footer chrome
(`docs/spec/KNOWN_ISSUES_P1_WEB.md:14`).

### 7.4 `ProposedProfileAdapter.detect`

Local signature only:

```python
ProposedProfileAdapter.detect(snapshot, rules)
```

Parse the inert URL with `urlsplit` and compare its path to
`url_path_prefixes`; a `/sorry/` string merely in a query parameter does not meet
that rule. Match lowercase frame names/URLs against their configured substrings,
and lowercase raw AX names against `text_contains`. Reason order is fixed:
`url`, then at most one `frame`, then at most one `text`. Any reason returns
STOP with `handoff="human"`; no reason returns
`{"status":"CONTINUE","reasons":[],"handoff":null}`.

The adapter does not itself execute a handoff or mutate the state machine. The
profile's `terminal=true` declares the desired policy, and the Google transition
table enforces it by having no outgoing wall edges. A future coordinator must
evaluate STOP before completion or another action; the companion evaluates
these operations separately and does not claim an implemented combined
production coordinator (`docs/spec/KNOWN_ISSUES_P1_WEB.md:15`).

### 7.5 Hypothetical production mapping, explicitly not implemented

`ProposedProductionAdapter` isolates a proposed invocation mapping for the three
missing-module expected-failure tests:

| Local adapter method | Hypothetical call it would make after import | Production status |
|---|---|---|
| `wait_done(implementation, snapshots, rules)` | `implementation(snapshots=snapshots, rules=rules, sleep=lambda _: None)` | Signature **AUSENTE**; future integration must verify the real signature and replace this mapping. |
| `extract(implementation, snapshot, rules)` | `implementation(snapshot=snapshot, rules=rules)` | Signature **AUSENTE**; the keyword shape is a proposal. |
| `detect(implementation, snapshot, rules)` | `implementation(snapshot=snapshot, rules=rules)` | Signature **AUSENTE**; the keyword shape is a proposal. |

These are not statements that a production callable accepts those keywords.
At the bound source, the missing imports fail before reaching the hypothetical
calls. The desired-behavior assertions concern the user-copy trap, footer-free
answer blocks, and terminal human handoff, rather than mere symbol existence.

## 8. Exact FakeCDP interfaces and production pointer probe

The base `FakeCdp(site, definition=None)` deep-copies its declaration, starts at
`initial`, and rejects duplicate transition keys, undefined source/destination
states and any outgoing transition from a terminal state. `snapshot()` returns
a deep copy. `step(event, target="", value="")` is the local reducer described
above. The optional declaration parameter is only for negative fixture tests.

`send(method, params=None, session_id=None)` requires exactly
`session_id="offline-replica"`. Its complete base reader vocabulary is:

| Method | Exact permitted parameters | Reply / error |
|---|---|---|
| `Accessibility.enable` | omitted or `{}` | `{}`; no state change. |
| `Accessibility.disable` | omitted or `{}` | `{}`; no state change. |
| `Accessibility.getFullAXTree` | omitted or `{}` | `{"nodes": snapshot["nodes"]}` as an isolated copy. |
| `DOM.getDocument` | `{"depth":0}` | `{"root":{"nodeId":1,"backendNodeId":1000,"nodeType":9,"nodeName":"#document","localName":"","nodeValue":""}}`. DOM IDs here are distinct from AX `nodeId` strings. |
| `DOM.getBoxModel` | `{"backendNodeId": integer}` with no other keys | `{"model":{"border":quad}}`; a node without a declared layout box raises. |

Unknown methods, parameters or session IDs raise `UnsupportedCdp`. In
particular, the base fake rejects `DOM.querySelector`, `Runtime.evaluate`,
`Page.captureScreenshot` and `Network.enable`; it does not pretend to implement
keyboard or hover CDP commands. The `calls` list is an in-RAM diagnostic log of
successful requests. Fixture interaction uses `.step`, not invented transport
methods.

`PotenciaExecutorCdp` is a deliberately narrow additional adapter for one
production probe. It starts in the Potencia `menu` state and recognizes only:

- `DOM.scrollIntoViewIfNeeded` and `DOM.resolveNode` for backend `1004`, with
  `DOM.resolveNode` returning `{"object":{"objectId":"offline:potencia"}}`.
- The exact existing `JS_BY_OP["click"]` function on that object, empty arguments,
  and `returnByValue=true`. It executes the declared no-op `js_click` row.
- This one exact `Input.dispatchMouseEvent` sequence, with no skipped/reordered
  member: `mouseMoved` at `(200,120)`; `mousePressed` at the same coordinates with
  `button="left", clickCount=1`; then `mouseReleased` with the same fields.
  Only release completes the declared `pointer_click` transition to `slider`.

The helper resolves the production hashed target ID from fresh `ax.prune`
output and calls the existing executor with `retries=1`, `wait_ms=0`, an injected
no-op sleep and `allow_coordinates=True`. With
`expect={"target_states":{"expanded":"true"}}`, the ordinary no-op fails
verification, the implemented fallback opens the slider and returns a
coordinates receipt. This is the passing integration probe for the mechanism
already present in `wender/act/cdp_executor.py:283-350`.

Without that expectation, the current executor returns an AX success receipt
while remaining in `menu`. The `KnownExecutorGap` setup builds the fixture,
executes that call and validates the state outside the decorated test method.
Only the final assertion that the menu should have opened is marked
`unittest.expectedFailure`, with a WP1-03 comment. Setup/transport failures cannot
be hidden by that decorator. This narrow probe is neither a general input
emulator nor a repair of `ChromeDriver.act`, which still does not enable
coordinate fallback (`wender/learn/chrome_driver.py:125-140`).

## 9. Concrete inline HTML/JS generation recipe

The following optional **consumer recipe** returns a complete HTML string for
any of the five IDs. It embeds the exact companion declaration, so no network
or fixture-file read is needed after generation. It does not write a file and
is not part of the required unittest gate. A later task may place the returned
string at the corresponding intended path in section 5.1; this task does not.

The renderer is a semantic presentation of the declared snapshots, with the
browser-mapping limitations in section 5.2. The precise synthetic observation
is available through the local `offlineReplica.snapshot()` hook. Scripted
`submit` and `tick` events use `offlineReplica.step(...)`; they have no fake
provider timer. DOM listeners hold element references and normalize only the
declared input vocabulary. Unlisted incidental pointer coordinates are ignored
by the presentation layer; supplying an undeclared normalized event to `step`
still throws. There is no reset or wall-bypass hook.

```python
import copy
import json
from tests.test_jq1_a9_offline_replicas_blueprint import REPLICAS


def render_fixture_html(site):
    spec = {"site": site, **copy.deepcopy(REPLICAS[site])}
    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")
    template = r'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Offline replica</title></head>
<body><script>
"use strict";
(() => {
  const spec = __OFFLINE_SPEC_JSON__;
  const key = parts => JSON.stringify(parts);
  const copy = value => JSON.parse(JSON.stringify(value));
  const table = new Map();
  for (const row of spec.transitions) {
    const k = key(row.slice(0, 4));
    if (table.has(k) || !(row[0] in spec.states) || !(row[4] in spec.states)
        || spec.terminal.includes(row[0])) throw new Error("Invalid declaration");
    table.set(k, row[4]);
  }
  let state = spec.initial;
  const refs = new Map();
  const stage = document.createElement("div");
  document.body.style.margin = "0";
  stage.style.position = "relative";
  document.body.append(stage);
  const snapshot = () => copy(spec.states[state]);
  const has = (event, target = "", value = "") =>
    table.has(key([state, event, target, value]));
  function step(event, target = "", value = "") {
    const k = key([state, event, target, value]);
    if (!table.has(k)) throw new Error("MissingTransition: " + k);
    const destination = table.get(k);
    const changed = destination !== state;
    state = destination;
    if (changed) render();
    return snapshot();
  }
  const tags = {main:"main", button:"button", textbox:"input",
    searchbox:"input", combobox:"input", slider:"input", heading:"h2",
    paragraph:"p", article:"article", code:"code", list:"ul", listitem:"li",
    StaticText:"span", Iframe:"iframe"};
  const explicitRoles = new Set(["menu", "menuitem", "menuitemradio", "group",
    "status", "alert", "dialog", "region", "complementary", "contentinfo",
    "table", "row", "combobox", "searchbox"]);
  function render() {
    const focused = [...refs].find(([, el]) => el === document.activeElement);
    const snap = snapshot();
    const raw = new Map(snap.nodes.map(n => [n.nodeId, n]));
    refs.clear();
    stage.replaceChildren();
    document.title = snap.title;
    stage.style.width = snap.viewport.doc_w + "px";
    stage.style.height = snap.viewport.doc_h + "px";
    refs.set("root", stage);
    function build(id, parent, origin) {
      const n = raw.get(id), role = n.role.value;
      const el = document.createElement(tags[role] || "div");
      refs.set(id, el);
      if (explicitRoles.has(role)) el.setAttribute("role", role);
      if (role === "button") el.type = "button";
      if (role === "slider") { el.type = "range"; el.step = "1"; }
      if (["textbox", "searchbox", "combobox"].includes(role)) el.type = "text";
      if (n.name.value && role !== "StaticText") el.setAttribute("aria-label", n.name.value);
      if (role === "Iframe") {
        el.title = n.name.value;
        el.setAttribute("sandbox", "");
        el.srcdoc = "<!doctype html><title>Offline challenge</title>";
      } else if (!n.childIds.length && el.tagName !== "INPUT") {
        el.textContent = n.name.value;
      }
      for (const p of n.properties) {
        el.setAttribute("aria-" + p.name, String(p.value.value));
        if (role === "slider" && p.name === "valuemin") el.min = String(p.value.value);
        if (role === "slider" && p.name === "valuemax") el.max = String(p.value.value);
      }
      if (n.value) el.value = String(n.value.value);
      if (role.startsWith("menuitem")) el.tabIndex = 0;
      const q = snap.border_quads[n.backendDOMNodeId];
      Object.assign(el.style, {position:"absolute", boxSizing:"border-box",
        margin:"0", padding:"0", overflow:"visible",
        left:(q[0] - origin[0]) + "px", top:(q[1] - origin[1]) + "px",
        width:(q[2] - q[0]) + "px", height:(q[5] - q[1]) + "px"});
      parent.append(el);
      for (const child of n.childIds) build(child, el, [q[0], q[1]]);
      el.addEventListener("click", event => {
        event.stopPropagation();
        if (has("js_click", id)) step("js_click", id);
      });
      el.addEventListener("keydown", event => {
        if (has("press", id, event.key)) {
          event.preventDefault(); event.stopPropagation();
          step("press", id, event.key);
        }
      });
      if (spec.site === "google_ai_wall" && id === "query") {
        el.addEventListener("beforeinput", event => {
          event.preventDefault();
          step("insert_text", id, event.data || "");
        });
      }
      if (spec.site === "chatgpt_potencia" && id === "potencia") {
        let pressed = false;
        el.addEventListener("pointerdown", event => { pressed = event.button === 0; });
        el.addEventListener("pointerup", event => {
          if (pressed && event.button === 0 && has("pointer_click", id)) {
            pressed = false;
            step("pointer_click", id);
          }
        });
      }
    }
    for (const child of raw.get("root").childIds) build(child, stage, [0, 0]);
    if (focused && refs.has(focused[0])) refs.get(focused[0]).focus();
  }
  const points = new Set(spec.transitions.filter(r => r[1] === "move").map(r => r[3]));
  document.addEventListener("pointermove", event => {
    if (!["menu", "entered", "corridor"].includes(state)) return;
    const point = Math.round(event.clientX) + "," + Math.round(event.clientY);
    if (points.has(point)) step("move", "", point);
  });
  window.offlineReplica = Object.freeze({snapshot, step, getState:() => state});
  render();
})();
</script></body></html>'''
    return template.replace("__OFFLINE_SPEC_JSON__", payload)
```

For example, the consumer can generate `render_fixture_html("perplexity_lpath")`
in RAM, then drive the declared route through the local stepping hook:

```javascript
offlineReplica.step("js_click", "model");
offlineReplica.step("move", "", "180,120");
offlineReplica.step("move", "", "380,120");
offlineReplica.step("move", "", "380,220");
offlineReplica.step("js_click", "choice");
```

The other four files use the identical generator with their own replica IDs;
all of their raw snapshots and transition rows are embedded in the returned
document. Nothing is fetched from the URL strings in its metadata. `beforeinput`
models the one declared whole-string insertion, not an arbitrary text editor.
Pointer checkpoints are discrete test samples, not a claim of continuous
hit-testing coverage. Browser rendering and captured AX equivalence remain
future consumer work; the code recipe does not turn synthetic data into an
empirically observed acceptance run.

Authoring verification in the VM generated all five complete strings in RAM
and passed each embedded script to `node --check -` through stdin. All five
JavaScript syntax checks exited successfully. The 28 documentation transition
rows were also compared to `REPLICAS` in order and matched exactly. These checks
did not render a DOM, run Chromium, execute a live-site interaction, write an
HTML fixture or establish browser/AX equivalence.

## 10. Verification, Storage Law and completion criteria

### 10.1 Scope of the required module

The companion currently declares 22 unittest methods: 18 ordinary conformance
tests and four explicitly expected failures for known production gaps. The
ordinary tests cover all five replicas, all 27 declared states and all 28
transition rows through data, graph, raw-shape and production-projection checks.
No live-site success is inferred from those counts.

| Test group | Passing conformance assertions / intended gap |
|---|---|
| `BlueprintFixtureConformance` | Five replicas; deterministic transition table; all states reachable; duplicate-row rejection; missing parent/elbow transition rejection without mutation; raw tree connectivity and typed node/value shapes; exact eight-integer border quads; copied observations and unsupported-CDP rejection; Potencia pointer/slider markers; L-path diagonal trap; exact profile schemas and WP1-01 guard; completion budget, user-copy and last-anchor traps; raw article extraction with trailer/footer cut; research collapse pending; independent Google STOP indicators and terminal wall; nonempty reasoning artifact with all replicas, operations and cited source paths; fenced HTML/JS recipe and AX-only checks over executable Python/JavaScript/HTML code blocks. |
| `ProductionAxStateConformance` | Every initial and pre/post-transition tree runs through real `ax.prune`, `browser.boxes`, `schema.build_state` and `schema.validate_state`; reversed single-root flat arrays prune identically; pruning leaves raw input unchanged; repeated STATE serializations match; raw counts and exact/stable hashes match; boxes are integer rectangles; backend IDs and raw answer text do not leak into STATE; opaque streaming text and raw slider-only changes do not change pruned hashes. |
| `ProductionPointerFixture` | Explicit expansion expectation reaches the existing center-pointer fallback, opens the synthetic slider, yields the coordinates receipt and validates the post-STATE with no side-effect attempt. |
| `MissingProductionOperations` | Three expected failures import the absent modules inside the individual test methods and pin desired WP1-02, WP1-04 and WP1-05 behaviors through a clearly hypothetical invocation adapter. They are not passing production implementations. |
| `KnownExecutorGap` | One expected failure only on the final desired behavior: a no-expectation, unchanged JS click should have opened the pointer submenu. Setup and state validation execute outside the decorated method. This pins WP1-03 rather than hiding transport or setup failures. |

### 10.2 Storage Law and deterministic scheduling

The **Storage Law** for the exercised model is zero disk writes: snapshots,
transition tables, raw text, quads, profiles, serialized states and call logs stay
in RAM. The required suite does not capture screenshots or generate frames.
There is therefore no image to persist. A later consumer that does produce
screenshot bytes must keep those bytes in RAM, consistent with the driver's
existing local screenshot/frame flow (`wender/learn/chrome_driver.py:98-106`).

The suite's `_offline_guard` intercepts writable `builtins.open`, `io.open` and
write/create/truncate/append `os.open` flags, common filesystem mutations,
`socket.socket`, `subprocess.Popen` and `time.sleep`. It records and rejects
attempts before performing those operations. Under that guard, the test visits
every declared state and transition, builds/validates production STATE, and
evaluates every applicable local profile operation with injected no-op sleep.
The observed-attempt list must remain empty. Separate negative guard checks
attempt three representative write paths and verify rejection before mutation.

This is a guard around the exercised Python paths, not a claim of a universal
operating-system sandbox. Interpreter import-cache suppression belongs to the
VM runner; use `PYTHONDONTWRITEBYTECODE=1` in the process environment to avoid
incidental bytecode files. The documentation artifact is read-only input to its
coverage assertion. No file under `tests/fixtures/sites/` is opened, required,
generated or used as an implicit test dependency.

The reference uses only declared events and scripted snapshot samples. All
production pointer probes inject no-op sleep with `wait_ms=0`; the local waiter
uses its bounded sample count and `sleep(0)` callback. The guard rejects an
accidental real `time.sleep`. Neither elapsed time nor repeated reads advance a
stream. Any runtime reported by the VM is labelled **VM**, never **MEASURED**
live-site timing.

### 10.3 Required invocation and honest interpretation

From the repository root, with bytecode writing disabled in the environment:

```bash
python3 -m unittest tests.test_jq1_a9_offline_replicas_blueprint
```

Completion requires the documentation to exist and be nonempty; every ordinary
offline state-machine, raw-schema, profile and production-projection assertion
to pass; no unexpected failure or skip; and the four explicitly named expected
failures to remain visible until the corresponding production work exists. The
actual observed output belongs in the task report, including failures from
intermediate runs. A future unexpected success is a request to revisit the gap
pin and real signature, not grounds for silently accepting the old proposal.

The document does not implement `wait_done`, `text.extract`, `stopstate.detect`,
production keyboard/hover dispatch, a driver fix, tab management, screenshots,
replay admission or live acceptance. The three production signatures remain
**AUSENTE**. The PR collects the specification and its reference conformance
suite; the orchestrator independently evaluates its acceptance gate.

