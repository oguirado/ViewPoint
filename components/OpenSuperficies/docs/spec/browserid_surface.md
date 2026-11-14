# Identity browser surface CLI

Round-two implementation: host Docker launch, internal network/proxy lifecycle,
atomic address allocation, exact RAM capture, verified input and a direct unix-socket operator view.
Verification is offline; live deployment is AUSENTE — no inventar.

## Runtime boundary

Operator ruling 15 supersedes the old W8 seat restriction for identity capsules.
The production CLI uses `DockerLauncher` through `Runner`; only the command
adapter imports `os/exec`. `UnavailableLauncher` remains an explicit refusing
adapter for callers that need it. Runtime errors are metadata only: runner
stderr, typed text, profile paths, cookies and credentials are never returned.
`Launch.Argv` is private control data. No raw CDP or remote-debugging endpoint.

Exit codes [TARGET]:

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Invalid command, flags, identity or input |
| 3 | Missing configuration, stopped proxy or unsafe network |
| 4 | Unavailable identity, address pool, state, listener or readiness |
| 5 | Capture framing, pixel-pair failure or malformed AX reply |
| 6 | Indeterminate lifecycle, focus mismatch, viewer, readonly restore or AX refusal |
| 7 | Reserved for an authority adapter |

## Commands and configuration

```sh
srf-identity-surface net up --proxy-bin /ABS/srf-egress-proxy --policy /ABS/policy.json --registry /ABS/registry/registry.json --audit-dir /ABS/audit
srf-identity-surface net down
srf-identity-surface start --identity ID [--windows N] [--capsule srf-id-NAME]
srf-identity-surface frame --container C --window w1
srf-identity-surface ax --container C --window w1 [--text-container HEX_ID]
srf-identity-surface input --container C --window w1 --key Tab
srf-identity-surface input --container C --window w1 --type TEXT
srf-identity-surface input --container C --window w1 --click X,Y [--button B]
srf-identity-surface stop --container C
srf-identity-surface view --container C [--port P] [--login]
srf-identity-surface login --container C [--port P]
```

`/ABS/` denotes an operator-supplied absolute path [TARGET], not a discovered
host path. Shared options are `--store-root`, `--registry`, `--proxy`,
`--state-dir`, `--timeout`; their existing environment equivalents remain
`SRF_IDENTITY_STORE_ROOT`, `SRF_PROXY_REGISTRY`, `SRF_EGRESS_PROXY`.
State defaults to the fixed RAM root's `identity-state` directory. All commands
must share the same state root and registry. The proxy defaults to
`http://172.30.80.2:3128` [TARGET]; other proxy addresses are refused.

Ordinary deadlines default to thirty seconds and cap at three minutes [TARGET].
View runs in the foreground until SIGINT/SIGTERM, with a maximum session
lifetime of twenty-four hours [TARGET]; explicit `--timeout` can shorten it.
View prints one JSON line containing `url`, `native` and `socket`. `login` is
an alias for `view --login`; ordinary `view` keeps xpra readonly.

## Network and launch

`net up` creates `srf-idnet` with `--internal --subnet 172.30.80.0/24` [TARGET].
Existing networks must match the name, internal bit and subnet. It creates the
proxy on `bridge`, attaches `srf-idnet` at `172.30.80.2` [TARGET], then starts it.
It verifies exactly these attachments, no published ports, the running state
and `EGRESS_PROXY_READY`. Runtime IP inspection follows start because Docker
assigns live endpoint addresses at that point. A configuration label allows
identical `net up` calls to reuse the container; a mismatch refuses replacement.
Partial failures require `net down` before retrying. `net down` removes and
verifies both resources; already-absent resources succeed. Attached identity
capsules prevent network removal and produce an error; stop them first.

The proxy uses `opensuperficies-desktop-arm64:1.1.0`, uid/gid `10001:10001`,
`--cap-drop ALL`, `--read-only`, `--security-opt no-new-privileges`, memory/swap
`256m`, pids `256`, and a bounded RAM `/tmp` [TARGET]. Its host executable is
mounted read-only as `/srf-egress-proxy` and selected with `--entrypoint`.
Registry and policy containing directories are mounted read-only, so atomic
replacement stays visible; the audit directory is writable. Flags are
`--listen`, `--registry`, `--policy`, `--audit` from `browserid_proxy.md`.
That older document's seat-only deployment text is superseded by ruling 15.

Provision dedicated metadata directories: never put profiles, private session
leases or unrelated files in the mounted registry/policy directories. Private
state/registry require mode `0700` directories and `0600` files [TARGET], owned
by the CLI principal. The proxy's mapped uid must be able to read the registry,
policy and executable and write the audit directory. For a rootful daemon,
running this CLI as the provisioned uid `10001` principal satisfies the shared
private-file ownership contract [TARGET]. Other uid mappings require explicit
operator provisioning; the CLI neither chowns files nor relaxes permissions.
The live daemon's uid/access setup is AUSENTE — no inventar in this offline run.

Start refuses before lease acquisition unless the proxy is running and the
identity network is internal. It stats only opaque profile directory metadata:
mode `0700`, uid `10001` [TARGET]; it never opens profile payloads. The identity
store grants the exclusive lease. A registry flock reserves the first free IP
in `172.30.80.10` through `172.30.80.250` [TARGET], using atomic replacement.
Malformed/duplicate/out-of-pool registry entries fail closed. No available IP
releases the acquired lease without creating a private session.

Launch uses `--network srf-idnet --ip ADDRESS` and the fixed proxy environment,
image `opensuperficies-identity-browser-arm64:0.1.0`, the private profile mount,
a read-only root, all capabilities dropped, CPUs `10-14`, memory/swap `4g`,
pids `256`, and RAM tmpfs mounts [TARGET]. Chromium keeps its sandbox.
Start creates `<state>/view/<capsule>/` owned by the CLI principal with mode
`0700`, and a fresh inner `s/` with mode `0777` [TARGET]. The inner directory is
bind-mounted at `/srfview`; xpra binds `/srfview/xpra.sock`. The private parent
limits host access to the operator. Stop removes the view directory only after
container removal is verified. There is no published Docker port or external
capsule network attachment.
Ambiguous launch retains its lease, address and state for verified `stop`.
Stop verifies absence before deregistering and releasing the identity; the
Docker adapter also handles a launch that created no container.

`tiling.Plan` computes window rectangles within the fixed `1280×900` display,
using an explicit `320×225` minimum and up to sixteen windows [TARGET].
`SRF_TILES` is `[{id,x,y,w,h}]`. Readiness requires both `xpra layer up` and
`IDENTITY_READY`; structured tiles and XIDs must match live `wmctrl -lG`.

## Operator view

`view --container C [--port P] [--login]` binds only host `127.0.0.1:P`;
omitted port selects a free port. The JSON announcement contains:

```json
{"url":"http://127.0.0.1:P/","native":"xpra attach socket://<state>/view/C/s/xpra.sock","socket":"<state>/view/C/s/xpra.sock"}
```

These are templates; the command returns actual paths and the allocated port.
The HTML5 viewer is preferred. The AI uses `frame`, `ax` and `input` instead.
GET/HEAD `/js/lib/jquery.js` serves the host file
`/usr/share/javascript/jquery/jquery.js`, replacing the image's missing target.
If that file is absent, view fails before enabling input with rc `4` and
`AUSENTE` [TARGET]. Every other HTTP request is reverse-proxied in Go to the
unix socket. Websocket upgrades use a fresh unix connection and bidirectional
`io.Copy`, preserving buffered bytes on both handshake boundaries. There is no
per-connection Docker/Python relay. Bytes never enter CLI JSON or logs.

Requests must have the announced loopback Host, a loopback peer and, if
present, exactly the matching HTTP Origin. Foreign origins, ports and Hosts
are refused. Read-header deadlines and transport connection limits bound HTTP
resources; upgraded connections are limited to thirty minutes [TARGET].
SIGINT/SIGTERM closes HTTP and upgraded connections; the foreground session
has a maximum lifetime of twenty-four hours [TARGET]. An explicit `--timeout`
can shorten it. No capsule TCP port or debugging endpoint is added.

`view --login` (also spelled `login`) invokes the IMAGE-installed
`/usr/local/bin/identity-entrypoint.sh xpra-start` with `SRF_LOGIN=1` once before
announcing. On exit it restores `SRF_LOGIN=0` with a fresh bounded context,
including after an ambiguous failed restart. Restoration retries once; if both
attempts fail, it invokes `xpra-stop` and returns rc `6` [TARGET]. A failed stop
is reported separately. The private control lock remains held during
interactive login. Ordinary readonly view releases it before serving, allowing
concurrent AI frame/AX/input operations. The IMAGE wrapper owns the socket
bind and readonly policy.

## Frame and input contracts

`frame` runs `srf-fbread pair :100 0` in the capsule, verifies both XWD legs with
`ramcap.PairLegs`, then crops canonical RGB24. It verifies live geometry before
and after capture. JSON keys are exactly `path`, `seq`, `sha256_rgb24`, `width`,
`height`, `capture_mono_ns`, `generation`, `window`. The window object is
`{id,x,y,w,h,title,url}`; URL is best-effort from a valid HTTP(S) token in the
window title and may be empty. URL query, fragment and userinfo are removed;
HTTP(S) URL tokens in the returned title are filtered identically. Title is bounded to 4096 bytes [TARGET]. The
monotonic capture stamp is taken immediately before invoking the capture runner.
The hash covers cropped RGB pixels, independently of PNG encoding.

The production sink has no configurable disk destination. It verifies tmpfs,
rejects symlink ancestors, creates private directories and publishes mode 0600
PNG files without overwriting existing files [TARGET]. Paths are:

```text
/dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>/frames/<window>/fNNNNNN.png
```

Frame sequence allocation is persisted before publication; failures may leave
sequence gaps. Frame buffers and test images remain in memory. `Root` configures
private control state only; it cannot redirect production PNGs to disk.

Input goes through `browserid/inject.Submit`. A shell-free executor runs
`xdotool` inside the capsule. It resolves and rechecks actual window geometry,
validates local bounds, activates the selected XID and verifies focus before
injecting. Cross-process flock extends the injector's serialization to separate
CLI invocations. Focus mismatch refuses with code 6 [TARGET], returns
`focus_verified:false`, and sends no input. Receipts contain no key/text payload.

## Verification and remaining integration

Offline fake-Runner and local fake unix-server tests cover mount lifecycle,
HTTP forwarding, the jquery override, Host/Origin refusal, interleaved binary
websocket bytes, buffered upgrade bytes, cancellation and readonly restoration.
No Docker, X server, live browser or cockpit app is opened. Tests and gate
receipts are in `build/room_VIEW/`. The preexisting `login_test.go` asserts the
removed Docker relay and is outside the VIEW write surface; see REPORT_VIEW.md
for its gate impact. Live deployment and perceived speed measurements are
AUSENTE — no inventar.

This mandate and its claims are scoped to the supplied corpus and the CLI
lane's exclusive surface.

## Read-only accessibility (`ax`)

```
srf-identity-surface ax --container srf-id-… --window w1 [--text-container <hex-id>]
```

The verb accepts the shared state/store/registry/proxy/timeout flags. Capture a
`frame` first: AX reads that window's persisted `seq` without advancing it. It
uses the same private session lock and checks native window geometry before and
after the request. Missing readiness, frame sequence or shim returns rc 4.

The client executes `docker exec -i -u 10001:10001 <container> python3 -I -c
<fixed-client>` and connects only to `/run/newton-tenant/ax.sock`. One newline
terminated JSON request is followed by write EOF:
`{op:"ax",window_title_or_index:<exact native title>,frame_seq:<current seq>,text_container?:<id>}`.
The shim resolves titles uniquely; ambiguity is refused. Client-supplied CDP
methods are never accepted. The browser pipe belongs exclusively to the shim.

Success prints one JSON line with exactly `window`, `frame_seq`, `scroll`,
`elements`, `truncated`, `skipped_frames`, and optional requested `text`. `window` has the same
`{id,x,y,w,h,title,url}` shape as frame metadata. `scroll` contains numeric `x,y`.
Each element contains `id,role,name,box,region,actionable,focused,states,container,
value_len`. Element IDs are SHA-derived lowercase hexadecimal, stable within the
frame sequence. Names are capped at 120 Unicode characters [TARGET]; elements
at 300 [TARGET]. Boxes are integer `[x,y,w,h]` in window-local pixels, with the
shim's measured browser chrome offset included, and must fit the selected
window. Region is a nearest landmark or null. States are
`{enabled,focused,expanded,checked,selected}`, each boolean or null. Container is
`{id,ordinal}` or null. `value_len` is a nonnegative integer only for textbox or
combobox roles, otherwise null. Length counts Unicode code points, not bytes
or grapheme clusters. Sensitive fields and descendants are omitted, including
`type=password`, autocomplete `current-password`, `new-password`,
`one-time-code` and any `cc-` token. No `value` key is
permitted at any depth. Ordering and overflow prioritization are produced by
the shim (document order, actionable first then area on truncation).

Text mode returns `text:{container_id,bytes,text}` only for the requested current
snapshot container. UTF-8 text is capped at 64 KiB [TARGET], and `bytes` must
match its encoded length. The shim refuses containers containing password,
credit-card or one-time-code inputs, and unknown snapshot containers (rc 6).

Replies are bounded to 16 MiB [TARGET], reject duplicate/unknown/missing keys,
invalid types, invalid UTF-8, mismatched sequence/window/geometry, oversized
names, lists or text, and trailing JSON (rc 5). A `value` disclosure or a box
outside the window is refused (rc 6). Shim errors are exactly
`{error:<bounded string>,code:4|5|6}`; details are replaced with fixed client
messages so errors never echo page content. No names or text are logged.
The sequence associates metadata with the latest frame allocation; page changes
between frame capture and AX collection are not an atomic browser snapshot.

AX URL/title metadata is independently filtered in the shim and client to
HTTP(S) `scheme://host[:port]/path`; userinfo, query and fragment never reach
those public fields. Non-HTTP(S) or malformed URL fields become empty.
Only main-document nodes/text/geometry are projected; same-origin and
cross-origin frame documents are skipped. `skipped_frames` is a nonnegative
count of extra captured documents and main-document frame owners whose
contents were not captured. Chains exceeding 256 nodes [TARGET] return refusal
6 [CITED: shim]. Per-call deadlines, stale-id draining and lifecycle restart
requirements are specified in `browserid_ax.md`.

## kind program

`start --kind program --program notepad --identity ID [--windows N]
[--capsule srf-pg-NAME]` uses the compiled `programsurface` catalog. Omitting
`--kind` selects the browser contract. Unknown kinds, missing program IDs and
`--program` with browser kind are invalid flags (code 2 [CITED]). Disabled or
unknown catalog entries also return code 2 [CITED]. Program profiles must have
store metadata `browser: "program"`; browser and program starts refuse a profile
of the other kind (code 4 [CITED]). Domain validation remains unchanged.

Successful program start JSON includes `"kind":"program"`. Browser start JSON
and the key, click, text, scroll and drag receipts retain their byte encoding.
Subsequent commands use `--container` and private session state; old state with
no kind loads as browser. Generated program capsule names use `srf-pg-` followed
by twelve lowercase hexadecimal digits [CITED].

Catalog minimum tile dimensions and window limits govern program planning.
Notepad uses `--network none`: start, failed-start handling and stop do not
reserve addresses, inspect Docker IPs, check the proxy/network or update/lock
the registry. Proxy catalog entries retain the identity network flow. The
catalog independently controls singleton cleanup and identity seccomp. Common
launch hardening and RAM-only frame capture remain in effect.

Readiness requires `xpra layer up` and the strict `PROGRAM_READY` window report.
The reported tiles and unique XIDs must match the plan, then each XID's live
geometry is verified. Malformed reports return code 6 [CITED]. Program window
URLs are empty; titles use the existing safe title path. Program AX refuses
before executing a capsule command with code 4 [CITED] and detail
`AUSENTE — program ax arrives in P2`.

Downloads, export and delete select the helper folder from saved kind:
browser `/profile/Downloads`, program the saved launch exchange directory
(`/profile/exchange` for notepad). The directory is an explicit Python helper
argument. Traversal, symlink, regular-file and export-size checks are unchanged.

Program sessions snapshot the launch network decision, exchange directory, seccomp selection,
and singleton policy at Start. Later verbs, including Stop, use that private snapshot without
consulting the current program catalog. Program state without a complete snapshot is refused;
older browser state remains supported. A malformed PROGRAM_READY separator is refused immediately.
