# X3 JavaScript seed: js_s2

Task: `os-f-x3-ab1-js-s2`, attempt `a1`. This is an independent fluency seed for
read-only AX/snapshot queries and click planning. It produces plain action data;
actuation belongs to XTEST outside this code.

## Run

Requirements: Node 18 or later, Python 3, and a POSIX environment with child file
descriptors and signals. There are no third-party dependencies or network calls.
Run from the repository root:

```sh
node --test bench/oracle/x3/js_s2/client.test.mjs
```

The test file imports only `node:test`, `node:assert`, `node:child_process`, and
the local client. Its fixture reads use asynchronous `execFile` with Python's
standard library. An additional diagnostic command is:

```sh
node bench/oracle/x3/js_s2/client.test.mjs
```

In the builder VM, the prescribed command returned rc 0 with a file-level
summary, while the diagnostic command listed all 22 cases passing. Validation
used Node v24.19.0 and Python 3.12.14. A Node 18 executable was unavailable;
compatibility with Node 18 is an implementation target, not a separately
observed runtime result. All test durations are VM timings, not performance
benchmarks. The task report retains the initial synchronous fixture-read
failure and the subsequent outputs.

## Public API and action format

`parseAXTree(result)` and `parseSnapshot(result)` decode the two CDP result
shapes without I/O. `parseTree({ax, snapshot})` joins their nodes, and also accepts
a bare DOMSnapshot or AX result. `findNode(tree, query)` takes exactly one of:

- `{id: 'box'}`: exact attribute-id equality.
- `{role: 'textbox'}`: exact AX-role equality.
- `{focused: true}`: the first AX node whose focused property is boolean true.

AX `nodeId` is preserved, including the fixture's string `"7"`. It is never
used as a substitute for `backendDOMNodeId`. AX and DOM rows join only through
AX `backendDOMNodeId` and snapshot `nodes.backendNodeId`. The focus fixture has
no backend identifiers, so its focused textbox remains queryable with unknown
geometry. Such a node cannot produce a click.

`plan_click(query, snapshotProvider, retries = 3, {timeoutMs = 10000} = {})`
awaits a fresh provider result on each attempt. The provider can be synchronous
or asynchronous, receives the one-based attempt number, and returns either a
`{ax, snapshot}` bundle or a bare DOMSnapshot result. `retries` is the **total
attempt budget**, including the first snapshot, and must be positive.

For the unmodified snapshot fixture, `{id: 'box'}` produces:

```json
{"kind":"click","xy":[420,440],"viewport_xy":[420,376],"attempts":1}
```

Failure after the budget produces, for example:

```json
{"kind":"abort","reason":"not_visible","attempts":3}
```

`not_found` means the final snapshot lacks a matching node. `not_visible` means
the final snapshot contains it without usable visible geometry. `refused`
returns immediately, before planning any click. Aborts have no coordinates.
Provider errors propagate; a provider deadline raises `ClientError` code 7.
Plans contain no timestamps, random values, process identifiers, or session IDs.

Example transport ownership:

```js
import { FakeCDPClient, plan_click } from './client.mjs';

const client = new FakeCDPClient();
try {
  const plan = await plan_click({ id: 'box' }, () => client.snapshot());
  console.log(JSON.stringify(plan));
} catch (error) {
  process.exitCode = [2, 5, 7].includes(error.code) ? error.code : 5;
} finally {
  await client.close();
}
```

The bound echo fake returns empty results for both snapshot methods. Therefore
this example aborts with `not_found` after three attempts. Nonempty geometry is
exercised by the supplied JSON fixtures in the pure-function tests.

## Coordinates, visibility, and selection

The computed-style order is exactly `visibility`, `opacity`, `display`.
String values and attribute pairs are decoded through the snapshot `strings`
table. Layout rows map to node rows through `layout.nodeIndex`; bounds and
styles must not be indexed directly by DOM node position.

A usable node has visibility `visible`, a finite nonzero numeric opacity,
display other than `none`, and positive rectangle width and height. Missing
styles, malformed geometry, and AX-only geometry fail closed. Offscreen
placement and occlusion are not part of this definition.

This seed treats each layout rectangle `[x, y, w, h]` as **document coordinates**.
The centre is `[x + floor(w / 2), y + floor(h / 2)]`. `viewport_xy` subtracts
that document's `scrollOffsetX` and `scrollOffsetY` exactly once. For the box
fixture, the document centre `[420, 440]` minus scroll `[0, 64]` is `[420, 376]`.
Viewport coordinates are not screen coordinates, and the client never scrolls.

Finite fractional bounds and scroll offsets are floored individually before
planning; the resulting coordinates must be safe integers. Missing scroll
offsets default to zero. Duplicate matches use document/node order, with AX
order breaking ties for rows sharing a backend identifier. Unmatched AX rows
follow the DOM-backed rows. No cross-document parent offset is synthesized.

## Refusal and transport boundary

On every attempt the planner checks the target and **every focused AX row**,
including an unmatched focused row with a sensitive AX name. Attribute `type`
equal to `password` or `file` refuses, ignoring case and surrounding whitespace.
The attributes `id`, `name`, and the AX name refuse on a case-insensitive
substring match for `password`, `passwd`, `secret`, `token`, `cc-`, `cvv`, `cvc`,
or `card` followed by zero or one character and `num`. This includes
`card_number`. Sensitivity takes precedence over absence or invisibility.

`FakeCDPClient` can spawn only:

```sh
python3 -B docker/cu/tests/fixtures/cdp/fake_chromium.py
```

Its cwd is the repository root resolved from the module location. The child
reads requests on fd 3 and writes replies on fd 4. Environment values include
`FAKE_MODE=echo`, `FAKE_URL=file:///opt/cu/scenes/v3/ff_form_01.html`, and
`FAKE_LOG` in a fresh private temporary directory. Tests can select only modes
implemented by that same bound fake. No real browser, remote endpoint, Docker,
X11, or Xvfb is involved.

Frames are UTF-8 JSON followed by one NUL. The default payload limit is loaded
from `docker/cu/oracle/allowlist.json`; an optional test limit can only lower it.
Exactly-limit payloads are accepted, and an oversized unterminated payload is
rejected without waiting for its delimiter. Framing and JSON decoding stay in
memory. The fake's explicitly required temporary request audit log is the only
protocol file; no image frames or screenshots are written. `auditLog()` returns
that audit text, retained in memory after close; close removes the temporary
directory.

The handshake sends only `autoAttach: true`, `waitForDebuggerOnStart: false`,
and `flatten: true` to `Target.setAutoAttach`. It binds the first
`Target.attachedToTarget` with type `page` and the exact fake URL. All other
events are ignored, including console events and `Target.targetInfoChanged`.
Later attachments cannot replace the bound session.

Requests receive increasing integer IDs; only a reply for a pending ID can
resolve its promise. Browser and session method names are looked up as own
allowlist keys; parameter shapes are checked against their rules. Session
requests require the bound session. `Page.navigate` is always refused, even
though the upstream allowlist includes it. Input, Runtime, Emulation, and DOM
methods are refused locally with code 5 before id allocation or pipe writes.
The client sends `DOMSnapshot` methods, which are a separate allowlisted domain.

Attachment, response, provider, and close waits have deadlines of at most
10 seconds. Protocol failures and transport deadlines terminate the fake.
Failed startup closes automatically. Callers must use `finally` to close an
otherwise live client; close rejects pending requests, sends SIGKILL, waits for
Node's child-close event (after reaping), and cleans up. It is idempotent. Library
errors use code 2 for invalid options, 5 for policy/protocol/transport failures,
and 7 for deadlines. There is no standalone CLI or explicit exit-1 path.

## Open questions

1. Should the integration contract reject fractional geometry or use a different
   rounding rule? This seed floors bounds and scroll values independently.
2. How should nested-frame document coordinates be translated into the top-level
   viewport and then XTEST screen coordinates? The fixtures establish only a
   single-document scroll convention; no frame ancestry transform is invented.
3. What consistency or recency guarantee should pair AX focus with DOM geometry?
   They are two sequential read-only requests, not an atomic snapshot. A stale
   focused-node association cannot be repaired from the supplied fixtures.
4. Which downstream component owns occlusion checks, viewport clipping, window
   origin, and scale calibration before actuation? This seed returns plans and
   does not establish those conditions.

The collection PR supplies fluency evidence. The orchestrator owns acceptance.
