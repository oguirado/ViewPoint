# P1 Area C — trusted Xvfb RAM capture contract

## Source boundary and public API

Producer pin: `oguirado/vision-engine` at live `bridge/integration`
commit `f998fb90cec9b51b2d83991f1843e7d8773607eb` (read through the
GitHub connector on 2026-10-21). The task card's older pin
`f8d1d682719e32911f0b47c089a4838877624846` differs; this work is
based on the live ref as requested. Input blob pins:
`vadapt/multi_x11_ramcap.py` `ab59f0a455a939328df5c771f5a398939d089e5c`,
`vadapt/window_stream_bank.py` `2e7f7f06264c57480605041c93c9a562fefd2734`,
`vadapt/rtring.py` `1ab4c6f158bf5a216bfe25705ada75bf2eea1550`,
`vadapt/xfb.py` `b8a92fc11ab27489bcd51447dc96a33c791613c6`.

Production entry: `vadapt/multi_x11_ramcap.py`. The public frozen records
have these **ordered fields**:

- `CropSpec(handle, epoch, x, y, width, height, frame_seq)`
- `TrustedScreen(handle, epoch, width, height, ring)`
- `FrameRef(handle, epoch, frame_seq, width, height, rgb, digest)`
- `Drop(handle, epoch, frame_seq, reason)`

`capture_many(specs, *, source, bank=None)` returns exactly one `FrameRef`
or `Drop` per input, preserving input order. `source.resolve(handle)` is a
trusted injected registration lookup; it must yield a `TrustedScreen` whose
`handle`, `epoch` and full-screen `width/height` are checked before any
pixel copy or bank publication. Source resolutions are cached per handle,
including unavailable lookups; stable source frames are cached per
handle/epoch/sequence within one call. Only `xvfb_screen` is capturable,
not arbitrary desktop or window handles.

Runtime handle identity comprises `kind, canonical_id, generation`:
kind and canonical ID are nonempty exact strings; generation is an exact
integer **at least one**. This does **not** change the low-level wire
`vadapt.window_stream_bank.Handle` constructor's legacy allowance of
generation zero. A request's epoch and `frame_seq` must be exact
nonnegative integers (never bool). `frame_seq < 2**64 - 1`; the maximum
unsigned value is reserved. Crop `x/y` are exact nonnegative integers,
`width/height` exact positive integers, and
`x+width <= trusted_width`, `y+height <= trusted_height`. A new
trusted geometry for an existing handle requires a **different epoch**.

## RAM wire and evidence

The input buffer uses `vadapt.rtring` v2: 64-byte global header,
64-byte headers per slot, RGB24 rows, and 4096-byte-aligned pixel start
(`data_off(slots)`). Magic is `VQARING1`, version `2`, little-endian
fields; head offset `40`, per-slot CRC offset `16`. Geometry, slot
count, stride, aligned offset and total allocation size must match
the trusted source. For requested index `r`, the reader requires a
retained head and slot index `r`, an even unchanged seqlock and CRC32
of an independently copied immutable payload. It bounds retries and
returns `torn` for inconsistent repeated reads; this Python seqlock is
**not** a native host acquire/release memory fence.

Each crop is independently sliced into contiguous RGB24 **bytes-backed**
NumPy uint8 memory, shape `(height, width, 3)`. No NumPy view of the
source ring or mutable owned array leaves the module. Its read-only
attribute cannot be reversed with `setflags(write=True)`.
`FrameRef.digest` is the SHA-256 lowercase hex of exactly those RGB bytes.
A bank receipt is checked for identity, geometry, sequence, byte equality,
immutable storage backing and digest; a `Drop` or mere status result cannot
mint a frame.

The optional bank must expose `admit(requests)` and
`publish(spec, image)`, with admitted triples `(spec, trusted_width,
trusted_height)`. A default `MemoryStreamBank` maintains independent
in-memory `rtring` output buffers. Its `admit` is transactional for
all new rings under its byte quota, including headers and alignment:
on `quota` failure, no new reservations or publications occur, and
existing streams remain charged/unchanged. `close()` forgets owned
rings and resets accounting, while returned immutable frame bytes
remain valid. `WindowStreamBank` is not interchangeable with this
capture-bank interface; its publish signature differs. Neither
`PoolSnapshot` nor `open_stream` is part of this API.

## Fail-closed observations

Malformed request/interface arguments yield the documented `TypeError`
messages or a `Drop`, not a blank `FrameRef`. Stable reason literals
include `invalid_handle`, `unsupported_mode`, `source_identity`,
`stale_epoch`, `source_geometry`, `invalid_sequence`,
`invalid_crop`, `source_unavailable`, `invalid_source_ring`,
`absent`, `lapped`, `torn`, `quota` and
`bank_invalid_receipt`; direct `MemoryStreamBank` may additionally
raise `geometry_epoch`, `not_admitted`, `stale_sequence`,
`invalid_rgb`, or `busy_writer`. An invalid request or source is
never included in bank admission, and a failed transactional admission
is published nowhere. A bank whose own `publish` implementation returns
an invalid receipt is untrusted: its receipt becomes
`bank_invalid_receipt`; there is no general rollback method on
an arbitrary injected bank.

## Verification and limits

Focused gate: `python3 -m unittest tests.test_p1_vc_capture_contract_unit`.
The tests create genuine ring-format bytes using independent
`struct.pack_into` + `zlib.crc32`, derive expected crop bytes through
literal row arithmetic and digests through `hashlib.sha256`, and exercise
valid siblings, identity, epoch, overflow, odd/torn rings, lapped/absent
indices, invalid receipts, transactional quota refusal and `close`
release. Injected source and bad-bank doubles represent only the
external lookup/publication boundary; no X server, sockets, network,
Docker, disk frames or native host timing enter the test.

**VM:** unittest output is reported in the associated outbox report after
execution; a green PR is collection evidence, not campaign acceptance.
**TARGET:** four 640×360 windows, at most four perception groups, and
one or two negotiated retained frames. **AUSENTE:** host timing,
acquire/release fencing on real shared host mappings, RAM residency
instrumentation, hardware throughput, and multi-process/Xvfb proof.
Operation counts and byte budgets do not prove runtime latency.
