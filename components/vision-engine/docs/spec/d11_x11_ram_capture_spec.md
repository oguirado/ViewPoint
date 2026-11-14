# D11 X11 RAM capture API and control protocol

## 1. Scope and authority

This document specifies the Python implementation in `vadapt/x11_ramcap.py` and
the bounded reader additions in `vadapt/xfb.py`. The capture service composes the
existing XWD decoder, canonical RGB24 conversion, RAM workspace and RGB24 ring.
It does not implement a second image decoder or modify the ring wire format.

The source is an operator-configured Xvfb framebuffer produced with
`Xvfb -fbdir <verified-RAM-directory> -nocursor`. The service does not launch an X
server, attach to an arbitrary display, ingest an encoded video stream, or infer
that a source excludes the cursor. Source provenance and cursor exclusion are
trusted configuration supplied by the caller or operator, not claims accepted
from a JSON control request.

The previous `vadapt.xfb` read command and the canonical vectors in
`docs/spec/d11_xfb_vectors.json` remain compatible. The API below is introduced by
this implementation; the bound workspace/ring specification did not previously
define `RamCapture.Start`, `Stop`, `SizeBytes`, `Reap`, or this control protocol.

External OpenSuperficies Go integration: **AUSENTE**. This specification describes
the Python reference and its protocol; it does not claim that a Go client has
been implemented or tested.

## 2. Trusted source configuration

```python
SourceConfig(
    root,
    path,
    width,
    height,
    nocursor_evidence,
    kind="xvfb-xwd",
)
```

`SourceConfig` is immutable. A `RamCapture` receives a mapping from configured
source aliases to these records.

| Field | Contract |
| --- | --- |
| `root` | Absolute trusted directory containing the raw framebuffer; text/path-like input is normalized to text. `.`/`..` aliases, NUL and symlink traversal are rejected. The opened filesystem must be verified as RAM storage. |
| `path` | Relative framebuffer path contained within `root`; absolute paths, empty/`.`/`..` components, NUL, symlink traversal and escaped paths are rejected. |
| `width`, `height` | Expected positive exact-integer pixel dimensions, within the XWD decoder's supported geometry. |
| `nocursor_evidence` | Nonempty trusted operator evidence that the producer was configured with `-nocursor`. The decoder cannot establish this from pixels. |
| `kind` | Exactly `"xvfb-xwd"`. Unknown kinds, lossy streams and alternative image formats are rejected. |

An alias identifies an existing trusted record; it is not a filesystem path.
Source aliases and session identifiers are text matching
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (one through 128 ASCII characters).
There is no request operation to change source roots, attest cursor exclusion,
change an expected dimension, or register an untrusted source.

The service verifies source containment and actual RAM storage, opens the source,
and binds its device/inode identity and size. It accepts only the strict raw XWD
format supported by `xfb`. The first 100 bytes, containing all 25 fixed XWD
header fields, are pinned after the first valid sample. Every subsequent frame
must retain those header bytes, source identity and size. Replacement of the
file, geometry or layout changes, malformed data and
unstable reads fail before ring publication. A changed source requires an
explicitly configured new lifecycle; it is not silently rebound mid-session.

Source verification is a fail-closed filesystem and configuration boundary. It
does not prove that a privileged process, kernel, or trusted producer is benign.
Two identical samples provide the existing XWD consistency rule; they are not a
transactional snapshot supplied by Xvfb.

## 3. Integer domains and budgets

An exact integer means `type(value) is int`. Booleans, floats and numeric strings
do not satisfy Python API or JSON integer domains. Required sizes, limits,
counts, cadence intervals and durations are positive. Source dimensions also
obey `xfb.MAX_SIDE`.

The constructor requires explicit limits:

```python
RamCapture(
    sources,
    *,
    max_frame_bytes,
    max_session_bytes,
    max_total_bytes,
    max_sessions,
    ring_slots,
    interval_ns,
    ttl_ns,
    tries=8,
    workspace_factory=None,
    clock=time.monotonic_ns,
    wait=None,
    cancel_factory=threading.Event,
)
```

| Parameter | Meaning |
| --- | --- |
| `max_frame_bytes` | Maximum raw XWD source size, checked through `fstat` before allocating a full snapshot. Includes header, color entries, row padding and pixels. |
| `max_session_bytes` | Maximum logical size of one RGB24 ring, including its metadata and alignment. |
| `max_total_bytes` | Maximum sum of retained ring charges across sessions. |
| `max_sessions` | Maximum simultaneously retained sessions, including stopped and cleanup-pending sessions. |
| `ring_slots` | Default and maximum allowed ring slot count, at most `2**32 - 1` to fit the existing ring header. |
| `interval_ns` | Default and minimum allowed capture interval in integer nanoseconds. |
| `ttl_ns` | Default and maximum allowed session lifetime in integer nanoseconds. |
| `tries` | Maximum number of source snapshots examined for consistency; exact integer at least two. |

Ring accounting uses the existing layout exactly:

```text
data_off = ((64 + 64 * slots + 4095) // 4096) * 4096
ring_bytes = data_off + slots * width * height * 3
```

The logical admission rule is `requested <= limit`, so an exact boundary is
allowed and one byte over is rejected. Aggregate admission includes every ring
whose resources remain retained. A stopped or expired session does not create
free budget merely because it no longer publishes frames.

`SizeBytes` reports this logical ring accounting. It is not process RSS, physical
tmpfs page use, the source framebuffer size, or the workspace's `st_blocks`
accounting. Full source snapshots and canonical conversion are transient RAM
allocations bounded by `max_frame_bytes`; they do not become persistent frame
files. The raw XWD contains at least four pixel bytes for each accepted RGB24
pixel, so the raw-frame bound also bounds canonical pixel conversion.

The workspace's own run quota, host quota and free-space policy remain additional
guards. An otherwise admissible logical request may therefore fail with a
workspace quota error.

## 4. Injection seams and ownership

The default storage path uses `vadapt.ramws`; returned rings use
`vadapt.rtring.RingWriter` and `RingReader`.

`workspace_factory(session_id, ring_bytes)` must return a newly owned, exclusive
workspace with an independent root. It must not return a shared workspace,
another session's root, an ancestor/descendant of another session's root, or a
directory whose unrelated contents belong to someone else. It must be compatible with
the ring's `root`, `path`, `reserve`, `release`, `account`, and `close` operations.
An injected workspace does not waive source or destination RAM verification or
path containment. Test workspaces may reserve tiny verified tmpfs mappings
without changing the production free-space policy.

The default factory creates a `ramws` run under tool `x11_ramcap`, campaign
`sessions`, with an independent generated run identifier and the requested ring
size as its quota. Factory ownership is transferred to the capture session.
The factory contract requires an exclusive newly created workspace. Invalid
roots, overlapping roots and roots containing a configured source are refused
before ownership transfer. Such an invalid factory result is not closed by the
service, because recursive cleanup could delete unrelated storage; repairing
or cleaning that rejected result remains the trusted factory caller's duty.

`clock()` returns monotonic nonnegative exact-integer nanoseconds. A backwards
clock, a negative value, or a non-integer raises `E_CLOCK`.
`wait(cancel_event, timeout_ns)`
waits interruptibly for at most the requested nonnegative interval;
`cancel_factory()` creates each session's cancellation event. These seams allow
offline tests to advance time, wake waits and request cancellation without an X
server or real-time sleeps. Cancellation events must provide callable `set`,
`is_set` and `wait` methods. The default wait converts only its final timeout to
seconds for `threading.Event.wait`; deadline/budget arithmetic remains integer.
They are trusted in-process dependencies, not fields
accepted over the control protocol.

## 5. Python lifecycle API

### 5.1 `Start`

```python
capture.Start(
    session_id,
    source,
    *,
    slots=None,
    interval_ns=None,
    ttl_ns=None,
    background=True,
)
```

`source` is a configured alias. Optional values use the constructor defaults.
Overrides must satisfy `0 < slots <= ring_slots`,
`interval_ns >= configured_interval_ns`, and
`0 < ttl_ns <= configured_ttl_ns`; all are exact integers. `background` is a
boolean Python scheduling option, not a JSON request field.

Start validates admission, obtains a bounded stable initial sample, checks the
expected source identity and layout, allocates a verified RAM workspace and
ring, and publishes index `0`. It returns session metadata. No session is
successfully started without a valid initial frame.

The metadata has exactly these keys:

| Key | Value |
| --- | --- |
| `session_id` | The session identifier. |
| `source` | Configured source alias. |
| `state` | Session state, normally `running` after initial Start. |
| `ring_path` | Path to the retained RGB24 ring. |
| `ring_bytes` | Full logical ring charge. |
| `frames` | Number of committed frames; initially 1, with newest index `frames - 1`. |
| `expires_ns` | Absolute monotonic expiry boundary. |
| `next_due_ns` | Earliest monotonic time for the next capture attempt. |
| `last_error` | Most recent typed failure code, or `null`. |

A repeated Start for the same retained identifier and the same effective
configuration returns that session's metadata without allocating another ring,
refreshing its lifetime, resetting its render index, or restarting it. Reuse with
different parameters is `E_SESSION_CONFLICT`; the comparison includes the
Python `background` setting. A cleanup-pending session always conflicts, even
when other parameters match. A session identifier may be reused after the
previous session has actually been reaped. Start after `close()` raises
`E_CLOSED`.

Initialization failure rolls back acquired resources. If cleanup cannot yet
complete, ownership and budget remain tracked for later reaping; a failed Start
must not orphan a ring or make its retained bytes disappear from accounting.

### 5.2 `capture_once`

```python
frame = capture.capture_once(session_id, wait=False)
```

The method produces at most one new frame. When called before the next capture
deadline with `wait=False`, it returns `None` without moving that deadline. With
`wait=True`, it waits through
the injected cancellation-aware wait seam. A successful call returns a borrowed
`rtring.Frame` carrying a read-only `uint8[height, width, 3]` RGB24 view and its
render index.

Source reads must be byte-identical on two consecutive snapshots within the
configured retry count. The existing `xfb.validate_frame` and `xfb.rgb24` own
validation and canonical conversion. Identity/layout checks and cancellation
checks precede publication. Invalid, replaced, resized or unstable sources do
not publish a new ring head or consume a render index.

Each publication is serialized for its session. The next capture opportunity
is scheduled relative to completion/current time rather than repeatedly adding
an interval to an old overdue deadline. A delayed capture never triggers a
burst of historical catch-up frames.

The background worker retries `E_TORN` at the next cadence opportunity; a failed
attempt publishes nothing. Fatal source/storage failures mark the session
`failed` and stop its worker while retaining its ring for Reap. An expired
session is marked `expired`; a normally stopped worker is `stopped`. These states
are visible in metadata returned by an idempotent Start. Initialization and
cleanup also use the internal states `starting` and `cleanup_pending`.

The background worker uses the same capture path. Callers should set
`background=False` for deterministic manual capture. The API does not promise a
specific frame count over an elapsed wall-clock interval.

An unknown identifier raises `E_SESSION_UNKNOWN`; an existing non-running
session raises `E_SESSION_STOPPED`. Expiry or cancellation during the operation
raises `E_EXPIRED` or `E_CANCELLED`. `wait` must be an exact boolean.

### 5.3 `Stop`

```python
existed = capture.Stop(session_id)
```

Stop signals cancellation, waits for the session's worker to stop, and closes
its source handle. The published ring, reader and full logical charge remain
retained. Stop is idempotent: it reports whether the session exists and does not
restart capture or discard already published frames.
The result is `False` for an unknown well-formed identifier and `True` for an
existing session, including one that was already stopped.

### 5.4 `SizeBytes`

```python
total = capture.SizeBytes()
one_session = capture.SizeBytes(session_id)
```

Without an identifier, returns the sum of logical retained ring bytes. With an
identifier, returns that session's logical retained ring charge. Stopped,
expired, failed-cleanup and borrowed-view-retained resources remain charged
until cleanup actually succeeds. No file scan or `st_blocks` approximation
replaces this ledger.
An unknown well-formed identifier returns zero.

### 5.5 `Reap` and `close`

```python
result = capture.Reap(session_id=None, force=False)
result = capture.close()
```

Reap considers one identifier or all sessions. Normally it collects sessions
that are stopped, expired, or failed. `force=True` first stops otherwise active
sessions. It returns lists of identifiers under `reaped` and `retained` so a
caller can distinguish completed cleanup from resources that still need a
retry. Both lists are sorted. An active session not selected for forced or
expired cleanup appears in `retained`. An unknown identifier returns two empty
lists. A second successful reap is harmless.

Cleanup closes the session reader before releasing the writer. If an exported
view keeps a mapping alive, the ring and its complete charge remain retained.
The service does not unlink the workspace underneath live views. Release
failures also remain retryable; a close method returning without an exception
is not alone proof that resources disappeared.

`close()` permanently prevents new Starts and requests forced reaping of all
sessions. It can leave borrowed-view
resources retained for a subsequent retry. Callers must release their views and
retry cleanup. A workspace must not be silently discarded to conceal that
pending ownership.

The class supports a Python context manager: `__enter__` returns the capture
object and `__exit__` calls `close()`. Exiting the context does not override the
borrowed-view retention rule. Forced reaping does not depend on a valid clock,
so an invalid injected clock cannot prevent EOF/close from cancelling resources.

### 5.6 Expiry and borrowed views

TTL is measured using the injected monotonic clock from session creation; it is
not refreshed by a duplicate Start or by capture activity. A session at its
expiry boundary must not publish another frame. Expiry stops further work;
physical cleanup follows the same borrowed-view and retry rules as explicit
reaping.
An idempotent Start observed at or beyond the expiry boundary reports `expired`
and cancels that session instead of restarting or extending it.

Borrowed arrays are not immutable snapshots: a ring slot is overwritten when
the producer wraps. Check `frame.valid()` after consuming the view, or use the
ring reader's post-use validity pattern. Copy into caller-owned RAM when a
stable independent snapshot is required. Hold neither a `Frame.view` nor any
derived NumPy slice across cleanup unless retaining the ring is intended.

Borrowed-view deferral covers views returned by `capture_once` through the
session's managed reader, including derived slices. An independent `RingReader`
mapping in the same process, or a mapping in another process, has a separate
owner. Those consumers must close their mappings before Reap; the Python manager
cannot observe or account for their exported buffers. External consumer lifetime
coordination, including Go integration, is **AUSENTE**.

## 6. Bounded one-frame read

```python
raw_xwd = fbread(source_config, max_bytes=..., tries=8, cancelled=None)
```

`fbread` applies trusted-source, RAM, containment, identity, geometry and stable
sampling checks and returns validated raw XWD bytes. `max_bytes` is mandatory
and positive. `tries` is an exact integer at least two. It uses the existing
`xfb` validation/conversion contract; no alternate decoder is introduced.
When provided, `cancelled` must be a zero-argument callable. A truthy result
raises `E_CANCELLED`, including before source access and between samples.

The source's size is checked through `fstat` before full snapshot allocation.
Malformed byte order, invalid retry domains, files larger than the cap, short
reads and unstable samples are typed failures. No incomplete or unvalidated raw
bytes are returned as a successful result.

### Existing decoder API and bounded-reader additions

```python
xfb.consistent_read(read_fn, tries=8, *, max_bytes=256 * 1024 * 1024)
xfb.read_screen_fd(
    fd, *, max_bytes, tries=8, width=None, height=None, before_read=None
)
xfb.read_screen(
    path, tries=8, *, max_bytes=256 * 1024 * 1024, width=None, height=None
)
```

`consistent_read` makes no hidden reads beyond `tries`. It accepts byte buffers
from its callback, checks their byte length, and freezes mutable buffers before
comparison. Invalid retry/limit domains call no callback. The callback remains
responsible for bounding its own allocation before returning; the descriptor
reader supplies the stronger pre-allocation `fstat` guarantee.

`read_screen_fd` leaves the caller-owned descriptor open and uses bounded
`pread`, not source `mmap`. This avoids a SIGBUS path if the producer truncates
the source while it is read. The descriptor must refer to a regular file whose
device, inode and size remain fixed during the read. Size checks occur before
each snapshot allocation and after each read. `before_read()` runs before each
snapshot and before successful return; raising from it provides the capture
layer's cancellation, expiry and source revalidation seam.

`read_screen` retains the legacy positional path and retry arguments, closes its
own descriptor, rejects symlink leaves where `O_NOFOLLOW` is supported, and opens
nonblocking so a FIFO fails the regular-file check instead of waiting for a
writer. This low-level function does not establish RAM storage or cursor trust;
those checks belong to `x11_ramcap`.

The fixed 256 MiB compatibility default bounds the legacy CLI. Trusted capture
calls always pass their configured cap explicitly. `rgb24(buf, hd=None)`
validates the complete frame before allocating output; a supplied cached header
must match the parsed frame. `frame_sha256` continues to hash canonical RGB24
pixels. Both XWD `byte_order` and `bitmap_bit_order` must be 0 or 1.

| `XwdError.code` | Meaning |
| --- | --- |
| `E_XWD_SHORT` | Insufficient header/source bytes. |
| `E_XWD_VERSION` | Unsupported XWD file version. |
| `E_XWD_FORMAT` | Unsupported pixmap format, depth or bits per pixel. |
| `E_XWD_BYTE_ORDER` | Byte order or bitmap bit order outside 0/1. |
| `E_XWD_VISUAL` | Unsupported visual class or color masks. |
| `E_XWD_GEOMETRY` | Invalid or unexpected geometry/stride, or a mismatched cached header. |
| `E_XWD_SIZE` | Raw size does not exactly match the parsed layout, or a snapshot is short. |
| `E_XWD_TRIES` | Retry count is not an exact integer of at least two. |
| `E_XWD_LIMIT` | Invalid size cap or source/snapshot larger than that cap. |
| `E_XWD_SOURCE` | Invalid source type, descriptor/path I/O failure, or source identity/size change. |
| `E_TORN` | No two consecutive identical snapshots within the retry count. |

## 7. JSON-line control service

```python
serve(capture, input_binary, output_text, max_control_line_bytes=...)
```

`max_control_line_bytes` is a required positive exact integer no larger than
`sys.maxsize - 1`, so the bounded `cap + 1` read remains representable. The limit includes
the final LF when present. Input is read through a bounded binary-line operation
before an entire arbitrary-size line can be allocated. An oversized line is
drained in bounded pieces, rejected once with `E_CONTROL_LINE`, and the following
request remains readable. A final line without LF is still a request.

Each accepted request is one UTF-8 JSON object. Invalid UTF-8, malformed JSON,
duplicate object keys, nonfinite numeric constants, a non-object top level,
unknown operations, missing/extra fields, wrong field types, or invalid integer
domains produce `E_CONTROL_SCHEMA`. Malformed requests do not mutate sessions.

All request field sets are exact:

| Operation | Required fields and domains |
| --- | --- |
| `Start` | `op="Start"`, positive exact-integer `request_id`, nonnull `session_id` identifier, configured `source` alias, positive exact-integer `slots`, `interval_ns`, `ttl_ns` |
| `Stop` | `op="Stop"`, positive exact-integer `request_id`, nonnull `session_id` identifier |
| `SizeBytes` | `op="SizeBytes"`, positive exact-integer `request_id`, `session_id` string or `null` for all |
| `Reap` | `op="Reap"`, positive exact-integer `request_id`, `session_id` string or `null` for all, exact boolean `force` |

The service invokes background capture for Start. There is no per-request path,
source kind, cursor assertion, workspace, frame cap, line cap, decoder selection,
or arbitrary Python method invocation.

Every response has exactly these fields:

```json
{"code":"OK","ok":true,"op":"SizeBytes","request_id":1,"result":{"bytes":0}}
```

On success, `ok` is true and `code` is `"OK"`. On error, `ok` is false, `code` is
the typed error code, and `result` is null.
Identity fields are returned where they can be established from the request;
invalid or unparseable identity fields are null. A recognized operation name is
echoed even when another field is invalid; an unknown operation is null. Responses
use sorted JSON keys and compact separators, followed by one LF, and are flushed
for the caller. No source
pixels or raw frames appear on the control stream.

Success result shapes are exact:

| Operation | `result` |
| --- | --- |
| `Start` | The nine-key metadata object specified in section 5.1. |
| `Stop` | `{"stopped": true}` or `{"stopped": false}`. |
| `SizeBytes` | `{"bytes": <nonnegative integer>}`. |
| `Reap` | `{"reaped": [<identifier>, ...], "retained": [<identifier>, ...]}`. |

Examples:

```json
{"op":"Start","request_id":1,"session_id":"s1","source":"screen0","slots":2,"interval_ns":100000000,"ttl_ns":5000000000}
{"op":"SizeBytes","request_id":2,"session_id":"s1"}
{"op":"Stop","request_id":3,"session_id":"s1"}
{"op":"Reap","request_id":4,"session_id":"s1","force":false}
```

EOF cancels active capture, stops workers and attempts forced reaping of all
sessions. Normal EOF does not print an unsolicited extra protocol response.
`serve` returns the final `close()` result to its Python caller. Stream failures
also trigger `close()` in a `finally` block.
The Python borrowed-view retention rule still applies when a caller holds
service-owned views in the same process.

## 8. Command-line interface

```text
python3 -m vadapt.x11_ramcap fbread <source-options>
python3 -m vadapt.x11_ramcap serve <source-options> <service-options>
```

Source options:

| Option | Requirement |
| --- | --- |
| `--source` | Required configured source alias. |
| `--source-root` | Required trusted source directory. |
| `--source-path` | Required contained relative framebuffer path. |
| `--width`, `--height` | Required positive integer dimensions. |
| `--nocursor-evidence` | Required trusted evidence string. |
| `--max-frame-bytes` | Required positive integer raw XWD cap. |
| `--tries` | Optional retry count, default 8, at least 2. |
| `--source-kind` | Optional kind, default `xvfb-xwd`; no other kind is accepted. |

Serve additionally requires `--max-session-bytes`, `--max-total-bytes`,
`--max-sessions`, `--ring-slots`, `--interval-ns`, `--ttl-ns`, and
`--max-control-line-bytes`, all positive integers.

Repeated CLI options are rejected, including repetitions expressed with
`--option=value`. Parsing and duplicate detection complete before source access.
Usage errors return status 2 and typed `E_USAGE` output on stderr. Source,
storage, consistency and other operational failures return status 5. Successful
execution returns 0. A `serve` request error is a JSON reply and need not terminate
the entire service.

Invalid constructor configuration (`E_CONFIG`) also returns status 2. CLI numeric
values use positive decimal syntax `[1-9][0-9]*`; no signs, leading zeroes,
fractional forms or exponents are accepted. `main(argv=None, *, stdin=None,
stdout=None, stderr=None)` exposes streams for offline tests. `stdin` is binary
for serve, serve stdout/stderr are text, and fbread stdout is binary (or exposes
a binary `.buffer`). CLI failure stderr is one JSON object containing `code` and
`detail`. If EOF cleanup retains a session, the CLI reports `E_CLEANUP` and status
5 instead of claiming a clean shutdown.

Successful `fbread` writes only the complete validated raw XWD bytes to stdout.
Errors go to stderr; no diagnostic prefix is mixed with binary frame data. The
caller is responsible for consuming that stdout in RAM. No command shown here
redirects frame output to a persistent file.

The legacy entry point remains:

```text
python3 -m vadapt.xfb read <Xvfb_screen0>
```

### Capture and control error codes

`CaptureError` exposes `.code`; its string representation adds optional detail.
Existing `xfb.XwdError` and `ramws.RamError` codes propagate unchanged where
applicable. The control protocol returns the code, not an exception traceback.

| Code | Meaning |
| --- | --- |
| `E_CONFIG` | Invalid API configuration, name, domain, policy override or injected seam. |
| `E_SOURCE_PATH` | Invalid, aliased, escaped or unsafe configured source path. |
| `E_SOURCE_KIND` | Unsupported source kind or a non-regular source object. |
| `E_NOCURSOR` | Missing/blank trusted cursor-exclusion evidence. |
| `E_SOURCE_IO` | Initial source-open failure. |
| `E_SOURCE_UNKNOWN` | Requested source alias is not configured. |
| `E_SOURCE_CHANGED` | Bound source disappeared, was replaced, or changed size/identity. |
| `E_SOURCE_GEOMETRY` | Bound fixed XWD header changed after initial validation. |
| `E_RAM_NOT_TMPFS` | Opened source or destination storage was not verified RAM storage. |
| `E_RAM_ESCAPE` | Unsafe, overlapping, source-containing or escaped workspace path. |
| `E_BUDGET` | Session/aggregate/count admission limit, machine-representable ring size, or render-index limit exceeded. |
| `E_CLOCK` | Invalid or backwards monotonic clock value. |
| `E_CLOSED` | Start attempted after capture.close(). |
| `E_SESSION_CONFLICT` | Incompatible duplicate Start, or a cleanup-pending identifier. |
| `E_SESSION_UNKNOWN` | Manual capture requested an absent identifier. |
| `E_SESSION_STOPPED` | Manual capture requested an existing non-running session. |
| `E_CANCELLED` | Sampling was cancelled before publication. |
| `E_EXPIRED` | Session reached its monotonic expiry boundary. |
| `E_CAPTURE_IO` | Other operational allocation, capture or I/O failure. |
| `E_CLEANUP` | Release/cleanup remains pending or failed. |
| `E_CONTROL_SCHEMA` | Invalid control encoding, JSON, duplicate keys, exact field set, or field domain. |
| `E_CONTROL_LINE` | One control line exceeded its configured byte cap. |
| `E_USAGE` | CLI command, option, duplicate option or value syntax error. |

Workspace quota/lock errors retain their `E_RAM_*` codes. `E_TORN` is a bounded
consistency failure that the background worker may retry at the next cadence;
it does not publish a partial frame.

## 9. Verification scope

The replacement gate is:

```sh
python3 -m unittest tests.test_fx2_f8_ramcap tests.test_xfb
```

Offline fixtures create frames in RAM and use only tiny verified tmpfs mappings
where a real file or ring is required. The live Xvfb test requires
`VADAPT_RUN_LIVE_XVFB=1`, installed `Xvfb` and `xwd`, and the RAM test surface.
The gate does not launch or require an X server, execute the e16 raw-capture
fixtures, or run the large ring benchmark. The canonical vector file is read
and checked, not regenerated or rewritten.

Any runtime measurements made in this environment are labeled **VM**. Host
capture timing, a live Xvfb bit-exact probe, native memory-ordering proof, and
external Go integration are **AUSENTE** unless independently reported with their
own evidence.
