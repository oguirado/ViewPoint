# E3 capture and XWD reader preservation evidence

## Outcome and source binding

Task `jq3-e-e3-c1-harden-x11-ramcap-electron-wi-890e75`, attempt `a1`.
The GitHub connector resolved `bridge/integration` to
`5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b`. The bound sources were read at
that explicit ref and their complete base64 content was matched to reads at
the resolved commit. Coordinates below refer to that commit.

**Production defect and repair: AUSENTE.** The owned regression passes against
the unchanged capture and reader files. The frame-counter guard and source-open
flags were already correct. There is no failing-before-repair production result;
the catalogue mutant failures establish regression sensitivity, not a historical
defect. Accordingly this change adds tests and evidence without modifying either
production owner or introducing a new production interface.

## Verified contracts and coordinates

| Contract | Source coordinates | Owned verification |
| --- | --- | --- |
| Trusted `SourceConfig`, exact `xvfb-xwd` kind and operator cursor evidence | `vadapt/x11_ramcap.py:44-76` | Real configured records; Chromium BGRA declaration refused. |
| Leaf is read-only, no-follow, nonblocking and close-on-exec; only regular files are admitted | `vadapt/x11_ramcap.py:101-127`; `vadapt/xfb.py:196-213` | Real opening paths with scoped fake descriptors; nested-directory flags and every descriptor close checked. |
| Identity, size and fixed header are rebound only by a new source lifecycle | `vadapt/x11_ramcap.py:130-178` | Real `_Source.read/check` rejects an inode replacement or fixed-header change even with identical RGB. |
| Canonical validation precedes extraction; cached headers cannot override the frame | `vadapt/xfb.py:46-104` | Both byte orders, padded rows, color-table offsets, geometry declarations and forged cached headers. |
| Bounded consistent snapshots and cancellation before return | `vadapt/xfb.py:107-193`; `vadapt/x11_ramcap.py:186-205` | Real reader/fbread paths; exact caps, FIFO/oversize refusal, replaced inode and final cancellation cleanup. |
| Final source/expiry/cancellation guard precedes publication | `vadapt/x11_ramcap.py:447-457` | Actual `RamCapture.Start/capture_once/Stop/Reap`, including the source-check and wait boundaries. |
| Ring head is unsigned 64-bit `render_index + 1` | `vadapt/rtring.py:41-46,148-153`; `vadapt/x11_ramcap.py:451-455` | Last legal index `2**64 - 2` publishes; frame count `2**64 - 1` refuses the next publication with `E_BUDGET`. |

The landed `RamCapture` has no source/reader/writer constructor keywords. The
suite uses its real `workspace_factory`, `clock`, `wait` and `cancel_factory`
seams, with scoped patches of `x11_ramcap._Source` and the landed
`x11_ramcap.rtring.RingWriter/RingReader` references. Fake directory attestation
supports an entirely in-memory workspace; actual containment, quota, lifecycle,
decoding and publication guards still execute. The fake writer deliberately does
not enforce the counter ceiling, so an overflow must be stopped by `RamCapture`.

## E3 invariants and evidence limits

| Invariant | Evidence in this change |
| --- | --- |
| INV-E3-1: declare layout before hashing | Real XWD headers and trusted geometry are validated; a Chromium byte buffer remains non-XWD. Equal flattened RGB under a different geometry cannot satisfy the declared dimensions. |
| INV-E3-2: separate raw and normalized digests | `hashlib.sha256(raw_xwd)` is distinct from `xfb.frame_sha256(raw_xwd)`, whose independently computed oracle hashes row-major RGB. Header/byte-order/stride changes preserve normalized equality while changing raw equality. |
| INV-E3-3: recheck association | Source inode, size, fixed layout, cancellation and render-index association are exercised. Identical pixels do not override a stale source identity or declaration. External application revision/presentation correlation is **AUSENTE**; these source checks do not substitute for that producer contract. |
| INV-E3-4: no persistent pixels | Fixtures, source snapshots and NumPy frames exist only in RAM. No frame, clip, pixel-bearing receipt, X server, network or application binary is used. Mutated code is compiled only in memory. |

The positive control is a declared matching exact RAM pair. Negative controls
keep the same decoded pixels while changing source identity, pinned header or
declared geometry. No receipt/STATE schema, sibling provider, broker, admit,
parity, readiness or correlation module is added or inferred from an analogue.

## Reproduction, mutation and validation

The exact owned gate is:

```sh
python3 -m unittest tests.test_jq3_e_e3_c1_harden_x11_ramcap_electron_wi_890e75
```

Observed on unchanged production source: **13 tests, OK**, no skips or expected
failures; final measured test duration **0.132 seconds VM**, within the
**15-second VM** target. Full output is retained in this task's `report.md`.

- **64 property cases**, `random.Random(0xe3c1890e75)`, with independently generated
  channel-byte/hash oracles and seed/case/geometry diagnostics.
- **32 logical schedules**, `random.Random(0xe3c132)`: eight each of publication
  before Stop, cancellation during source read, cancellation during source check,
  and Stop during cadence wait. There are two workers plus the main thread;
  every barrier, Event wait and join is bounded to two seconds. Schedules record
  and check operation order, frame count, quota retention and cleanup. Cleanup
  releases gates and aborts barriers; leaks stop further schedules and cannot
  fall into an unbounded lifecycle cleanup.
- **M-vadapt-x11-ramcap-1** changes the left `1` in `1 << 64` to `2`: killed by
  semantic `AssertionError`, `exhausted counter published a frame`.
- **M-vadapt-x11-ramcap-2** changes the final subtraction `1` to `2`: killed by
  semantic `AssertionError`, `last legal frame refused`.

The AST harness uniquely anchors `RamCapture._capture` and its complete counter
comparison, runs both the real and an unmutated compiled module as controls,
sets package/module context for imports and dataclasses, and removes temporary
modules in `finally`. Only the dedicated semantic `AssertionError` subtype and
the expected reason count as a kill. Import, compile, runtime, timeout, setup
and cleanup errors fail the gate. No allocation scales with the seeded count.

This is offline VM evidence. Live Xvfb/Electron behavior, actual host filesystem
attestation, external presentation correlation and orchestrator acceptance are
not established. The pull request is ready for collection, not acceptance.
