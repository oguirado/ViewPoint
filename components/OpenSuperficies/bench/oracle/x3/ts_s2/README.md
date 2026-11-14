# X3 TypeScript seed 2 — query and click planning

Task: `os-f-x3-ab1-ts-s2`, attempt `a1`. This seed is fluency evidence only;
collection of the pull request is not acceptance.

## Run

From the repository root:

```sh
node --test bench/oracle/x3/ts_s2/client.test.mjs
```

The runtime uses Node and Python standard libraries only. The intended minimum is
Node 18 on a POSIX platform with `python3` on `PATH`; no installation or network
access is needed. This VM provided Node `v24.19.0` and Python `3.12.14`. Node 18
execution and a strict TypeScript compiler check were not available here.

`client.ts` uses explicit, erasable TypeScript types, with `unknown` at JSON
boundaries. No `tsc` executable or TypeScript compiler package was found in the
available runtime. `client.mjs` was produced with hand-authored type erasures;
there are no runtime rewrites. Its first line contains the SHA-256 of the exact
UTF-8 bytes of `client.ts`. The tests recompute that hash using `node:crypto`.
Node's built-in type erasure was also used to verify the emission, not to generate
the committed JavaScript. When a TypeScript toolchain and Node type declarations
are available, a separate semantic check is:

```sh
tsc --strict --target ES2022 --module ES2022 --moduleResolution node --types node --noEmit bench/oracle/x3/ts_s2/client.ts
```

That command is documentation for a future check; it was not run in this VM.

## API and behavior

```js
import { FakeCdpClient, plan_click } from './client.mjs';

const client = await FakeCdpClient.open();
try {
  const plan = await plan_click({ by: 'id', value: 'box' }, () => client.snapshot());
  console.log(JSON.stringify(plan));
} finally {
  await client.close();
}
```

The actual fake returns empty results, so this example returns an abort with
`reason: "not_found"` and `attempts: 3`. It never sends input or performs a click.
Actuation belongs to the external XTEST layer.

The pure functions `parse_ax`, `focused_node`, `parse_snapshot`, `find_node`, and
`refused` do no I/O and do not modify their arguments. Queries use
`{ by: "id", value: "box" }`, `{ by: "role", value: "textbox" }`, or
`{ by: "focused" }`. Matches follow fixture document/node order, with unjoined AX
nodes appended in AX order. AX `backendDOMNodeId` joins snapshot
`nodes.backendNodeId`; an AX `nodeId` is never treated as a backend ID.

`plan_click(query, provider, retries = 3)` takes a provider returning
`{ ax, snapshot }` or a promise of that pair. The retry parameter is the total
number of permitted snapshot attempts, including the first. Each attempt parses
fresh data. A missing node retries with `not_found`; an existing invisible node
retries with `not_visible`. The last failed snapshot determines the exhaustion
reason. A sensitive target or focused node aborts immediately with `refused`,
including when the queried target is missing. An unresolved provider promise
times out after 10 seconds with `ClientError.code === 5`.

Sensitive checks cover `type=password`, `type=file`, and case-insensitive
substring matches in attribute `id`, attribute `name`, and AX name:
`password`, `passwd`, `secret`, `token`, `cc-`, `card.?num`, `cvv`, and `cvc`.
The check includes unjoined focused AX nodes with sensitive names.

Visibility requires `visibility === "visible"`, finite numeric opacity other
than zero, a nonempty display other than `none`, and positive integer width and
height. Missing/invalid style or rectangle evidence is invisible. Attribute and
style values are decoded through the snapshot strings table. Layout rows are
matched through `layout.nodeIndex`, independently of node-array order.

## Coordinate and scroll convention

`layout.bounds` is interpreted as `[x, y, width, height]` in that document's
coordinates. Finite coordinates, dimensions, and scroll offsets are truncated
toward zero before arithmetic. The centre is
`[x + floor(width / 2), y + floor(height / 2)]`.

The returned `xy` stays in document coordinates. The returned `viewport_xy`
subtracts that document's `scrollOffsetX` and `scrollOffsetY` exactly once. An
absent scroll offset defaults to zero. Safe-integer overflow refuses with code 5;
the planner never returns fractional or nonfinite coordinates.

The supplied `box` fixture has bounds `[100, 200, 640, 480]`, so its click plan is:

```json
{"kind":"click","xy":[420,440],"viewport_xy":[420,376],"attempts":1}
```

Its vertical scroll offset is 64. The `cv` fixture is invisible and exhausts the
default budget with `{"kind":"abort","reason":"not_visible","attempts":3}`.

## Fake-only transport

The executable and scene URL are fixed: the client starts
`python3 -B docker/cu/tests/fixtures/cdp/fake_chromium.py` with child fd 3 for
request input, fd 4 for response output, `FAKE_MODE=echo` by default, and
`FAKE_URL=file:///opt/cu/scenes/v3/ff_form_01.html`. There is no arbitrary command,
URL, environment, or allow-list path override. Test options select only the bound
fake's modes, a smaller frame limit, byte count, and timeout.

`FAKE_LOG` names a temporary mode-0600 FIFO. The fake writes its received requests
to that file; the client drains it and exposes the accumulated text through
`read_log()`. FIFO data stays in kernel memory and client memory. No frame bytes or
screenshots are persisted to disk. The FIFO and its private temporary directory
are removed on close.

The client loads `docker/cu/oracle/allowlist.json` and checks method, scope, and
parameter rules locally before allocating an ID or writing any bytes.
`Page.navigate` is always refused, including navigation to the scene URL, despite
its entry in the shared allow-list. Input, Runtime, Emulation, and DOM methods
are always refused. Browser methods have no session ID; session methods require
the bound page session. Local refusals have code 5 and do not close an otherwise
healthy transport.

The handshake sends exactly `autoAttach: true`, `waitForDebuggerOnStart: false`,
and `flatten: true` to `Target.setAutoAttach`. It binds the first matching
`Target.attachedToTarget` page event and retains that session. Every other event,
including `Runtime.consoleAPICalled` and `Target.targetInfoChanged`, is ignored.
The existing proxy has broader `about:blank` tracking; this seed follows the
task's narrower attachment rule.

JSON messages end in one NUL byte. The size limit counts payload bytes excluding
that delimiter, including incomplete frames. The default comes from the bound
allow-list; a test override may lower it but cannot raise it. Response IDs are
monotonically increasing safe integers and are matched against pending requests;
unknown IDs are ignored. Framing/protocol errors and transport timeouts fail the
client and kill the child.

Transport waits default to 2 seconds and accept only 1–10,000 ms. The temporary
FIFO helper also has a bounded wait. `close()` is idempotent, rejects pending
requests, sends SIGKILL to the owned child, waits for its close/reap event with a
timeout, and releases pipes and temporary paths. Use `try/finally` as above.
Invalid caller configuration uses error code 2; refusal/transport failures use
code 5. This is a module with no explicit process exit of 1. The standard Node
test runner retains its own conventional failure status.

## Validation observed and open questions

The prescribed test command passed 22 tests in the VM. An earlier emission had
unremoved TypeScript modifiers and failed syntax checking. After correcting it,
the sandboxed test command encountered `spawnSync python3 EPERM`; approved local
subprocess execution then allowed the real fake-pipe tests to run. Full observed
commands, statuses, and failures are in the task's outbox report. All recorded
durations are VM durations, not benchmark measurements.

Open questions for a later integration, beyond this seed:

- Should multiple matches be rejected or explicitly disambiguated? This seed
  selects the first match in stable snapshot order.
- Should frame-local viewport coordinates be translated through containing iframe
  offsets? This seed reports each document's own coordinates and scroll.
- What authoritative rectangle/attribute source should be used for AX nodes
  without `backendDOMNodeId`? The AX fixture's focused node 7 has no join key and
  remains queryable but cannot itself produce a click rectangle.
- Should the collector require a regular `FAKE_LOG` file? This seed uses a FIFO
  to satisfy both the log requirement and the prohibition on persisting frames.
- Node 18 execution and semantic `tsc --strict` verification remain unestablished
  in this VM. No real browser, XTEST, Docker, X11, Xvfb, or network integration was
  exercised or is claimed.
