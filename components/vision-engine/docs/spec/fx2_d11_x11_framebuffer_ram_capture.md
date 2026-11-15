# FX2 F8: D11 X11 framebuffer RAM capture design note

## Purpose

The existing `vadapt.xfb` decoder already defines strict XWD validation and
canonical RGB24 hashes. What was missing was an explicitly trusted source
boundary and a bounded capture lifecycle around the existing RAM workspace and
frame ring. This change adds that lifecycle in `vadapt.x11_ramcap` and hardens
the existing reader. It preserves the canonical vector file and legacy
`python3 -m vadapt.xfb read <path>` invocation.

The full Python API, JSON request schemas, line framing, CLI options and
lifecycle semantics are in `d11_x11_ram_capture_spec.md`. This note explains the
design decisions and the limits of the evidence.

## Source trust and decoder reuse

XWD records pixels and layout, not the producer's `-nocursor` invocation. A
trusted source record therefore carries explicit cursor-exclusion evidence,
expected geometry, a permitted source kind, and a trusted RAM root. The JSON
service accepts a source alias and cannot invent or replace that trust record.
Only the raw Xvfb XWD source is supported. Encoded/lossy streams and unknown
kinds fail closed.

Source handling binds a file identity and layout rather than repeatedly opening
whatever appears under a filename. Containment, RAM storage, identity, size and
layout checks surround sampling. The first 100 header bytes are pinned; they
contain the full fixed XWD header, including geometry, stride and byte order.
A source replacement or geometry change fails
before publication. The initial valid sample establishes a session only after
the source and allocation checks pass.

`xfb` remains the sole XWD parser and canonical RGB24 converter. Byte order must
be one of the two valid XWD values. Retry counts must be exact integers of at
least two. A raw-frame size cap is checked with `fstat` before a full snapshot is
allocated. Consistency remains the existing two-consecutive-identical-samples
rule, with bounded retries and a typed torn-sample failure.

This rule can reject a continuously changing source even when each individual
read happens to be valid. It also cannot prove transactional consistency of a
writer that pauses after a partial update. These are limits of the observed
producer protocol, not reasons to accept unequal samples or add a second
decoder.

## Allocation and exact accounting

Admission uses `rtring.ring_bytes`, including the aligned metadata region. A
payload-only budget would miss at least one metadata/alignment allocation and
would make exact boundary checks misleading.

`RamCapture` keeps logical ring charges separately from workspace physical
accounting. `RamWorkspace.release` uses `st_blocks`, while reservation starts
from a byte request. Physical pages and logical layout bytes serve different
purposes. Session and aggregate limits use the logical layout, and workspace
quotas/free-space checks remain additional storage guards.

An exact budget boundary is accepted. Stopped, expired, and cleanup-pending
sessions retain their ring charges. Borrowed views do not create fictitious
free capacity, and repeated Start cannot allocate a second ring for the same
retained identifier. Failed initialization rolls back resources or retains a
retryable cleanup owner if rollback cannot complete.

Transient raw snapshots and RGB conversion stay in RAM and are bounded by the
configured frame cap. They are not charged as retained ring bytes, and the
logical ring ledger is not process RSS or total host RAM consumption. Workspace
quota and free-space guards continue to apply. No persistent raw frame, PNG, or
XWD fixture is needed.

Injected workspace factories are trusted ownership seams. They must return
independent, newly owned roots, never shared or nested session workspaces. RAM
and root checks run before ownership transfer. A root overlapping a retained
session, containing a configured source, or failing RAM/path verification is
refused without closing that invalid factory result; recursive cleanup of such
a root could destroy someone else's storage. The factory caller remains
responsible for the rejected object.

## Lifecycle and borrowed views

Start publishes the first validated frame as index zero. Background capture and
manual `capture_once` use the same sampling and publication path. Duplicate
Start is idempotent only for the same effective configuration; it does not
refresh TTL or restart a stopped session.

Stop cancels and joins capture and closes the source while retaining the ring.
Reap handles stopped, expired or failed sessions, and forced Reap first stops
active sessions. This separation lets consumers finish with a retained ring
after capture ends.

The existing ring provides read-only borrowed NumPy views. A slot may later be
overwritten, so `Frame.valid()` is a post-use check, not a promise that bytes
never change. A retained slice can keep an mmap alive even after the enclosing
Frame object has been discarded.

Cleanup therefore closes the reader before the writer, verifies that unmapping
actually completed, and retains the ring and its full charge while exported
buffers remain. It does not delete the workspace underneath live views. A later
Reap retries cleanup after views are dropped. Release failures must likewise
remain retryable: a lower-level close call returning, or an internal released
flag being set, is not sufficient evidence that the file and reservation are
gone.

Cleanup deferral tracks the managed reader used by `capture_once`, including its
derived slices. Independent `RingReader` mappings and readers in other processes
have separate owners and must close before Reap. Their lifetime cannot be
observed or accounted by this Python manager. External Go integration and
cross-process consumer lifetime coordination: **AUSENTE**.

## Cadence and shutdown

Capture uses integer nanoseconds from an injectable monotonic clock, along with
injectable waiting and cancellation. Cancellation is checked before sampling,
during retries, and before publishing. No rejected or cancelled sample consumes
a new published ring index.

Deadlines advance relative to completed/current time. An old overdue deadline
does not generate a sequence of immediate catch-up captures. TTL limits the
session from its initial start; repeated requests do not renew it implicitly.

The JSON loop remains available while background capture runs. EOF signals all
workers, waits for them to stop and attempts reaping. Borrowed views can still
cause a documented retained result for the Python caller, rather than being
silently invalidated by deleting their backing files.
Forced Reap/close does not require a valid clock, so an injected clock failure
does not block EOF cancellation and cleanup. Cancellation waits do not hold the
session lock; duplicate Start and Stop can proceed while a worker is waiting.

## Protocol bounds and deterministic failures

The control line cap is mandatory, lies in `1..sys.maxsize-1`, and counts LF when
present. Bounded binary
reads enforce that cap before allocating an arbitrary entire line. Oversized
lines are drained in bounded pieces and produce one rejection; the next request
is not discarded. JSON decoding happens only after this bound has been checked.

The service rejects duplicate object keys, unknown or extra fields, missing
fields, malformed UTF-8/JSON, nonfinite numbers, and non-exact integer domains.
Each operation has a fixed field set. No request can weaken trusted source
configuration or global limits. Responses have one stable envelope and one JSON
line per request; no pixel bytes are mixed into the control channel.

CLI duplicate options, including `--name=value`, are rejected before any source
access. `fbread` keeps stdout exclusively for successfully validated raw XWD
bytes and reports typed errors on stderr.

## Offline verification and remaining checks

Run only the replacement gate for this task:

```sh
python3 -m unittest tests.test_fx2_f8_ramcap tests.test_xfb
```

The test suite exercises the new lifecycle and protocol with RAM-generated
frames and tiny verified tmpfs mappings. The canonical decoder vectors remain
unchanged. The live Xvfb test requires explicit opt-in, so the offline gate does
not acquire an accidental display dependency.

Do not run `tests.test_e16_raw_capture` for this task: its fixtures deliberately
write persistent raw/PNG files. Do not run the large `tests.test_rt1_ring`
benchmark. Those files were read as source evidence only.

The VM has a verified tmpfs source/destination surface. Its available RAM space
can be below the production workspace's 8 GiB free-space floor; injected test
workspaces allow tiny mappings without changing that production policy. A
production factory still correctly rejects a reservation that violates its
configured workspace guards.

Observed test outcomes and any intermediate failures belong in the accompanying
task report. Timings, when reported, are labeled **VM**. No host performance
claim follows from an offline VM gate.

Remaining evidence is explicitly limited:

- Live Xvfb/GetImage parity for this change: **AUSENTE** unless the opt-in probe
  is separately run on a suitable host.
- External OpenSuperficies Go client/session integration: **AUSENTE**.
- Native acquire/release fence guarantees beyond the existing Python ring
  implementation: **AUSENTE**.
- Cross-process borrowed-view lifetime coordination: **AUSENTE**.
- Host throughput/latency measurements: **AUSENTE**.

The pull request is a collection artifact. Passing the local replacement gate
does not certify orchestrator acceptance.
