# P1 Area C — WindowStreamBank (RAM-only stream bank)

## Source and public contract

Task `p1-vc-stream-bank/a1`; source read from `oguirado/vision-engine`,
`bridge/integration` at observed commit
`f998fb90cec9b51b2d83991f1843e7d8773607eb`. The task metadata
also records an earlier source pin
`9d078e3818823e411c96cbd9aacf1bed3f76927c`; that is **not** the
integration HEAD observed by this worker. Bound files:
`vadapt/window_stream_bank.py` (source blob
`2e7f7f06264c57480605041c93c9a562fefd2734`),
`vadapt/rtring.py` (source blob
`1ab4c6f158bf5a216bfe25705ada75bf2eea1550`).

Preserved ordered frozen dataclasses:

- `Handle(kind, canonical_id, generation)`; kinds: `cdp_tab`, `x11_window`,
  `xvfb_screen`, `capsule`, `godot_window`.
- `StreamSpec(width, height, slots, quota_bytes)`.
- `StreamToken(handle, epoch)`.
- `FrameRef(handle, epoch, frame_seq, width, height, rgb, digest)`.
- `Drop(handle, epoch, frame_seq, reason)`.

`WindowStreamBank(*, quota_bytes, ring_factory=None, step_hook=None)` exposes
`open(handle, spec) -> StreamToken`,
`open_many(((handle, spec), ...)) -> tuple[StreamToken]`,
`resize(token, spec) -> StreamToken`,
`resize_many(((token, spec), ...)) -> tuple[StreamToken]`,
`publish(token, rgb, *, frame_seq) -> None`,
`read(token, *, step_hook=None) -> FrameRef | Drop`,
`close(token) -> None`, and `retry_cleanup() -> int` (number of
successfully released entries). `reserved_bytes` reports currently charged
RAM storage, including retired mappings awaiting release.

Runtime-admitted handles require an **exact integer** `generation >= 1`;
the low-level `Handle` constructor still permits legacy generation zero for
wire compatibility. `epoch` and `frame_seq` at runtime require exact
nonnegative integers (not booleans); publication index also fits the
`rtring` 64-bit head convention. Geometry changes require a `resize` and
the resulting new epoch token. Sequence monotonicity persists across resize.
Closed incarnations and older retired generations of a logical
`(kind, canonical_id)` lane cannot reenter the registry. At most one
generation of that lane is active or in admission.

## RAM accounting, integrity and refusal

The charge of each ring is **`rtring.ring_bytes(slots, width, height)`**,
including a 64-byte global header, 64 bytes per slot header, 4096-byte
alignment and `slots * width * height * 3` RGB payload bytes. Per-stream
`spec.quota_bytes` and the shared bank quota are checked before any factory
callback. `open_many` and `resize_many` stage all charges transactionally;
resize requires capacity for both old and replacement buffers, until
release is acknowledged. Failed admission releases only new proven-clean
allocations. A failed close/unmap or a borrowed view retains the specific
mapping's charge; `retry_cleanup` decrements it only after an acknowledged
release. Direct bytearrays are cleared on release and cannot be cleared
while borrowed views exist. Memoryview and NumPy exporters are unwrapped
for conservative alias rejection; one lane never refunds another's charge.

Metadata operations follow `(kind, canonical_id, generation)` canonical
order. Ring-factory callbacks, owner close callbacks and read step hooks
execute outside the registry mutex; raw storage is cached so the user's
owner property is not revisited within a locked publish/read. A pending
resize rejects old-token publish/close without touching pixels or quota.

Existing exception classes remain `StreamError(ValueError)`,
`QuotaError(StreamError)` and `StaleStreamError(StreamError)`:
`StreamError` rejects malformed dimensions/identities/sources,
nonmonotonic sequences, duplicate/order-invalid batches, live alias storage
and slot-inflight/pending operations; `QuotaError` rejects per-stream or
shared quota insufficiency; `StaleStreamError` rejects closed or resized
tokens and duplicate/retired generations. No new wire error codes are
introduced. `Drop` reasons include `absent`, `torn`, `lapped`, and
`stale_epoch`; a status-only result contains no RGB and cannot mint a
`FrameRef`. An accepted frame has **owned, immutable** RGB24 and
`sha256(rgb.tobytes(order='C')).hexdigest()` digest, independent of the
ring slot's CRC32. Seqlock parity, before/after sequence equality,
render index, CRC and current epoch are checked before returning it.

Wire format remains in `vadapt/rtring.py`:
`MAGIC=b'VQARING1'`, `VERSION=2`, `HDR=64`, `SLOT_HDR=64`,
`ALIGN=4096`, `CRC_OFF=16`, `HEAD_OFF=40`. The bank does not
alter the low-level on-wire representation.

## Source coordinates and evidence

Source baseline: `vadapt/window_stream_bank.py:19` kinds, `:24` errors,
`:44-90` structures, `:180-210` admission and quota,
`:211-332` require/reserve/open/resize, `:335-365` publish,
`:366-439` read and hooks, `:440-491` cleanup.
`vadapt/rtring.py:36-59` holds the independent layout primitives
and `:200-260` holds the low-level reader.
The amended file shifts those line numbers; this list identifies
the original source coordinates used to design the repair.

VM: `python3 -m unittest tests.test_p1_vc_stream_bank_unit`:
20 tests, OK, 0 errors, 0 failures, on a local mirror of the actual
branch's stream-bank source with the corresponding `rtring` wire layout
constants. Positive controls precede two installed bad-method mutations:
**killed 2/2**, surviving 0, equivalent 0, invalid 0.
Distinguishing assertions were owned `FrameRef` versus canned `Drop`,
and an unmap acknowledgement versus actual reclaimed bytearray.
Further named refusals and actual two-thread seqlock interleaving use
unmodified production logic. No expected failures, skips or placeholders.

TARGET: four 640×360 windows, no more than four perception groups,
negotiated one or two retained slots per window. In this wire layout,
one slot is **695,296 bytes** per window and two slots **1,386,496 bytes**;
four two-slot windows charge **5,545,984 bytes**, excluding other process
memory. These values are allocation arithmetic, **not** host measurements.

AUSENTE: real X/Godot/capsule capture, host end-to-end timing,
hardware acquire/release memory-order guarantees, multi-process
integration and orchestration acceptance. Do not infer host latency
from operation budgets or VM counters. The existing legacy fixture
`tests/test_jq2a_m4_c1_stream_bank_implementation.py` constructs
`Handle(..., generation=0)` in its `setUp`, which conflicts with this
task's new runtime-registration floor. It was not changed because it is
outside the task's exclusive write paths; an integration owner must
decide whether to migrate that fixture.

No visioncore, other producers, capture devices, sockets, Docker,
network transport or disk frame storage are part of this change.
