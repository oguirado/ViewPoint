# JQ2A M4-C2: multiplexed trusted-Xvfb RAM crops

Task `jq2a-m4-c2-ram-capture` attempt `a1`. This note specifies the
implementation in `vadapt/multi_x11_ramcap.py`, not a native-window backend.

## Source coordinates and prerequisites

Source reads: GitHub `oguirado/vision-engine`, explicitly at
`bridge/integration`, observed head `e7bc08675995c29a68ef8b5e05eb893cf534a057`.
The task card/metadata instead name `8a9a4b6ee77808d45d17f09c1a1ed8108e35048b`;
that was *not* the branch head observed for this attempt.

- `vadapt/xfb.py:46-99` validates raw XWD dimensions/layout and decodes exact
  canonical RGB24; `:122-153` bounds two-consecutive-identical snapshot reads.
- `vadapt/x11_ramcap.py:44-87` defines trusted operator `SourceConfig` (including
  `-nocursor` evidence); `:114-171` pins source identity and XWD layout;
  `:339-390` admits RAM rings using `rtring.ring_bytes`.
- `vadapt/rtring.py:29-43,48-51` defines the ring layout and exact byte charge;
  `:130-161` is the single-writer even-sequence/CRC commit sequence;
  `:241-273` is its source-side stable-index/CRC reader rule.
- `docs/spec/d11_x11_ram_capture_spec.md:1-150` is the trust, geometry, and
  budget boundary. `docs/spec/fx2_f7_rtring_ramws_note.md:62-101`
  documents the repeated `(seq, render_index)` mismatched-index check.
- Bound regression sources include `tests/test_fx2_f8_ramcap.py`,
  `tests/test_adv_rtring_stale_seq.py`, `tests/test_rtring_props.py`, and the
  `fx2_f7`, `fx2_f8`, `jq1_a3` RAM/XWD/quota suites; this task changes none.

Prerequisite order remains **identity and streams → ownership and perception →
readiness → scheduling and Godot → scorecard**. Only the isolated RAM-crop
surface is implemented here. Until the separately owned identity/stream,
readiness and integration gates are actually satisfied, integration dispatch
is blocked; success is not substituted with a no-op. The provider
`vadapt.window_stream_bank` was **AUSENTE** at the observed source ref.

## Public API and the trusted injection seam

```python
CropSpec(handle, epoch, x, y, width, height, frame_seq)  # frozen, exact field order
capture_many(specs, *, source, bank=None) -> list[FrameRef | Drop]

# Trusted, in-process adapter:
source.resolve(handle) -> TrustedScreen(handle, epoch, width, height, ring)

# Optional retained RAM bank; default is ephemeral for this one call:
bank = MemoryStreamBank(quota_bytes=256 * (1 << 20), slots=2)
bank.admit([(spec, source_width, source_height), ...])
bank.publish(spec, owned_crop) -> FrameRef
bank.token_for(spec) -> StreamToken | None
bank.reserved_bytes  # exact sum of live rtring.ring_bytes reservations
```

`ring` is a trusted byte-oriented buffer (e.g. an injected `bytearray` or a
caller-owned ring mapping) in the existing `vadapt.rtring` v2 format. The
source owns the buffer's lifetime for the call and must attest the producing
Xvfb screen, immutable screen dimensions/epoch, and full-frame RGB24 layout.
It must not accept untrusted requests as provenance or turn paths into frame
fixtures. This module neither opens a file nor reads an arbitrary native
X window, launches Xvfb, produces XWD, or validates operator provenance anew.
A production adapter still has to bind it to an independently verified
`x11_ramcap` source. `xfb` remains the sole raw-XWD decoder upstream.

Only `Handle.kind == 'xvfb_screen'` can pass. `x11_window`, `cdp_tab`,
`godot_window`, `capsule`, and unknown kinds return `Drop('unsupported_mode')`;
a capsule and its child screen share actuation identity in the owning registry,
which is not implemented or altered here. All bound crop integers are exact
(no booleans/floats); x/y are nonnegative, width/height positive, and both
far edges must lie within trusted source geometry. A stale epoch or different
source handle/generation is rejected. A missing registered source gives
`Drop('source_unavailable')`.

The imported `Handle`, `FrameRef`, `Drop`, `StreamToken` are canonical exports of
`vadapt.window_stream_bank` when that module exists. Only the *absence of that
module* activates identical frozen structural fallback classes with the exact
published field orders. Internal provider import failures propagate. Cross-
repository consumers compare fields rather than class identity.

## Invariants and refusal semantics

**INV-M4-1 (identity and streams).** Each bank reservation is keyed by
`(kind, canonical_id, generation, epoch, x, y, width, height)`. The exposed
`StreamToken` embeds unambiguously length-prefixed source ID plus crop
coordinates and preserves handle generation and geometry epoch. Multiple crops
of one screen or screens of different generations have separate writer rings;
source identity in `FrameRef` remains the original handle. The bank pins full
screen geometry for each `(handle, epoch)` across calls and refuses changes
until a new epoch. Its per-ring `last_seq` rejects stale or repeated publication.

**INV-M4-2 (all-or-nothing accounting).** A single `admit` validates every
requested geometry and deduplicates repeated stream keys. Each new ring is
charged once using `rtring.ring_bytes(slots, crop_width, crop_height)`, including
metadata/alignment. Aggregate quota is checked before any buffer construction;
all new bytearrays are constructed before insertion or ledger debit. A failed
batch never releases existing reservations, erases a previous ring, or credits
a live mapping. Input/source rings are separately owned and not double-charged
by this output bank. `close()` releases this bank's in-RAM buffers only;
returned FrameRefs have their own owned bytes.

**INV-M4-3 (read and delivery).** The reader checks trusted width/height against
ring header, ring length, valid v2 magic/layout, head window, then requests the
exact `render_index == frame_seq` slot. An odd sequence consumes a bounded
retry. A mismatched index is rechecked as the adjacent `(seq, index)` pair
before classification. For a matching index it copies RGB24 into owned
contiguous NumPy RAM, rechecks `(seq, index, crc)`, validates the window/head,
compares CRC32 over the copied array, and checks `(seq, index, crc)` again.
A result never includes a raw borrowed view that could change on wraparound.
After bank publication, the same validation runs on the output ring. Returned
RGB arrays are `uint8`, C-contiguous and marked read-only; digest is lowercase
SHA-256 of canonical crop RGB bytes. `torn`, `lapped`, `absent`, invalid geometry,
and quota all return explicit Drops. CRC32 detects accidental inconsistencies,
**not** collision-free synchronization or a hardware acquire/release fence.

All capture-source callbacks run *before* the bank metadata lock; the bank
performs only in-memory admission/publication under its own lock. It never holds
that lock across transport, waits or executor admission. A single writer per
output stream is serialized by that lock. The default per-call bank does not
persist rings across calls: pass the same bank instance for a retained stream.

## Offline verification and blocked limits

`tests/test_jq2a_m4_c2_ram_capture_implementation.py` exercises the public
module with real v2-format `bytearray` source rings, fake trusted registration,
positive byte-exact controls, odd-sequence/out-of-bounds mutant distinctions,
reused/corrupt/lapped slots, independent crops/screens/generations,
transactional quota exact-boundary/rollback, epoch pinning and forbidden-I/O
fixtures. It does not contact a display, network or Godot or write image files.

**AUSENTE / blocked:** arbitrary native-window capture; strict native zero-copy
and native acquire/release fence proofs; live Xvfb/GetImage parity; owning
`WindowStreamBank` provider and its deployment integration; real actuation
lease, scheduler/focus, readiness and Godot gates; full-host performance claims.
A VM-only isolated test run does not certify repository-wide regressions or
orchestrator acceptance. Integration must be gated on the separate prerequisites.
