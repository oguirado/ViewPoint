# W8U vision-engine paired-frame wire reader — U3 seam

**Status: implemented bounded W8UF subset; integration acceptance still BLOCKED on revision-bound `w8u-os-contract`.** This document does not authorize any missing protocol constant, acceptance decision, or production producer/consumer integration. The independently injected *test-only* W8UH/END kinds and error codes are not normative.

## Source coordinates and scope

Read with the GitHub connector at **`oguirado/vision-engine` `bridge/integration`**, observed commit `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b` (inbox task names an older commit `143a14de13ed523121c74553beb4fbd94fa79e4e`). The bound files are:

- `vadapt/xfb.py` lines **46–68**: `parse_header(buf)` validates XWD format, byte-order fields, visuals, masks and lower stride but has no W8U metadata caps.
- `vadapt/xfb.py` lines **70–77**: `validate_frame(buf, width=None, height=None)` checks exact frame size and declared geometry.
- `vadapt/xfb.py` lines **80–100**: `rgb24(buf, hd=None)` returns canonical owned RGB24 row-major bytes for BGRX or XRGB input.
- `vadapt/xfb.py` lines **102–104**: `frame_sha256(...)` returns **hex text**, not the 32-byte wire digest.
- `tests/test_xfb.py`: synthetic XWD generator and existing same-canonical-hash, size/format/geometry, order, consistency and read-limit regressions.

The prerequisite `oguirado/OpenSuperficies` at `bridge/integration`, observed commit `f4d479e789f16b643201ad839654ce916bd0450e`, has a pending `w8u-os-contract` task but does **not** contain `contracts/w8ui/w8u.ui.v1.schema.json`, `tools/test_w8u_contract.py` or its acceptance outbox. Consequently literal binding/action-result schema IDs, exact W8UH confirmation semantics, END kind/error numbers, accepted cross-language vectors and native error translation are **AUSENTE**, not locally defaulted.

## Public-path API and trust boundaries

`validate_binding(binding, limits) -> BindingInfo` accepts a closed structural dictionary with the 19 requested fields and closed `target_handle` (kind/canonical_id/generation), exact signed JSON integer checks (never bool/float), digest hex fields, target geometry, strict RAM root, a confined `opensuperficies/os-cu-0/{run_id}` child, positive negotiated caps and `tick_mode` `committed|unavailable`. `WireLimits` explicitly requires a trusted `binding_schema`, a limit for frames and controls, reserved capacity, **and an externally provided mapping of native `xfb.XwdError` names to closed W8U errors**. No schema or native mapping defaults exist. `BindingInfo.digest` is SHA-256 of sorted compact UTF-8 canonical JSON (without LF); a matching digest is *not* independent grant/capability authentication.

`WireSession.for_binding(binding, limits)` owns `next_seq`, pending overread bytes, state flags and a separate `CreditWindow`. One persistent session must accompany every record. `parse_control(reader, *, binding, limits, session, verify_hello, verify_confirmation, contract, verify_end_exit, verify_ack_release)` reads one control. It checks known `W8UC` HELLO (kind 1, bounded canonical JSON), ACK (kind 2, 16 bytes) and STOP (kind 3, empty). A caller-supplied `verify_hello(binding, digest)` must return **exactly True** after authenticating the grant/capability. This marks HELLO received but **not confirmed** and awards zero credit. `W8UH` requires a contract-injected `confirmation_kind` and `verify_confirmation(body, binding, digest)` returning exactly True. No confirmation or END can succeed with missing contract fields. `W8UE` requires contract-injected `end_kind`, full `end_error_codes` domain, 20-byte body, clean EOF and `verify_end_exit(last_seq, sent, error_code)` returning exactly True after joined-exit verification. Unknown numeric END codes refuse. No magic scanning or resynchronisation occurs.

`decode_record(reader, *, binding, limits, session) -> PairRecord | None` requires confirmed HELLO, not-stopped/not-ended state and matching session binding. Clean EOF at a frame boundary returns `None`; a partial header or body raises `WireError('E_TORN')`. Short reads are retried, and overreturned coalesced bytes are preserved in the bounded session buffer for the next call. The reader is injected (`read(n)` or `callable(n)`), no network/process/disk calls are made. The `PairRecord` holds only RAM XWD bytes plus a **closed** metadata projection: seq, tick, tick_valid, capture_mono_ns, width, height, stride, pixel_format, sha256 and binding_sha256. Unknown tick projects `None`, not zero; committed tick 0 is valid.

### Frame validation order

1. Inspect W8UF fixed 128-byte big-endian header: version 1, header 128, flags required pair bit 0 / optional tick bit 1 only, pixel format RGB24=1, positive contiguous seq beginning at 1, exact binding digest, declared width/height/stride, committed/unavailable tick semantics. Reject early without reading body on mismatch.
2. Enforce `record_bytes == 128 + payload_bytes` and cap by the **minimum** of externally supplied `limits.max_record_bytes`, binding `max_record_bytes`, and `128 + 16 + 2*(4*width*height + 1 MiB)` before allocating XWD bodies. Any overflow or underflow refuses `E_LENGTH`.
3. Read u64 length A, then inspect its **100-byte XWD fixed prefix** before allocating the remainder. Apply outer geometry, XWD version 7, ZPixmap 2, depth 24, 32 bpp, both order bits 0/1, visual 4/5, RGB masks, header range 100–65536, color count 0–65536, 4-byte-aligned stride between `4*width` and 65536, and exact `header + 12*colors + height*stride == lenA`. Require A <= `4*width*height + 1 MiB`.
4. Read u64 length B, require `16+lenA+lenB == payload_bytes` (no trailing bytes), repeat all subset checks before its body allocation, and use existing `xfb.validate_frame` and `xfb.rgb24` for final native exactness and canonical RGB24 conversion.
5. Hash each RGB24 image independently as raw 32-byte SHA-256 and compare **both** to advertised `sha256_rgb24`. Only then advance `session.next_seq`. Equal canonical RGB with different XWD headers/pixel byte orders is accepted; raw XWD/BGRX/RGBA hashes are never compared.

### Credit / lifecycle seam

`CreditWindow(capacity)` models strict ACK window rules independently of producer internals. Initial `(ack_seq=0, credit_total=min(2, capacity))` is exact; no HELLO credit. Duplicated identical ACK is idempotent. Later grants must be monotonic, refer to already produced frames and obey `credit_total <= ack_seq + capacity`. `require_credit(next_seq)` is to be called **before** a producer grabs a frame; exhausted credit refuses. The control parser additionally requires injected `verify_ack_release` for every non-initial ACK to attest validated publication and recording-consumer lease release. STOP sets a stop flag independently of credit/full queues. This module does **not** implement actual X11 grab/ungrab, queue scheduling, memory reservation, recording publication, action execution, or process joining: their production owners must provide those capabilities and call at the documented boundary.

### Failures and effects

Wire failures use named `E_TORN`, `E_WIRE`, `E_LENGTH`, `E_HASH`, `E_SEQ`, `E_TICK`, `E_BINDING`, `E_GEOMETRY`, `E_CREDIT` etc. Numeric END mappings remain external. Refusals leave `next_seq` unchanged, never produce a last-good fallback, and do not themselves write frames or data to disk. Missing accepted contract fields fail closed. Production stdout/stderr, artifacts, receipts and logs must not contain pixel bytes. Hash-only retention is metadata-only, not evidence of replayable pixels; UI, simulation, action-log and canonical RGB24 digest domains remain distinct.

## Offline coverage and acceptance gap

`tests/test_w8u_ve_wire_unit.py` constructs independent XWD/outer-frame/control bytes with `struct.pack`, **not** a production encoder. It checks every split point, coalesced successors, clean/partial EOF, no magic resync, overrun/overflow before body reads, invalid sizes/both XWD order fields, forged version/flags/pixel format, sequence gap/duplicate, matching/bad canonical hashes, tick mode, closed binding/root/caps, HELLO independent verification, contract-required confirmation/END, credit=1/exhaustion/duplicates/inflation, STOP, and a nonempty exact `MANDATORY_CASES` manifest. All synthetic pixels exist only as in-memory byte arrays. Tests inject clearly labelled nonproduction W8UH/END values and do not imply their acceptance by OpenSuperficies.

A local VM unit run can exercise a copy of the relevant observed `xfb` seams. **It cannot satisfy the orchestrator's acceptance gate against the actual current `bridge/integration` tree until the contract is accepted, verified at its merged revision, and the code rerun in that tree.** Cross-language accepted/rejected goldens, literal schema IDs, control bodies and full END enumeration are still AUSENTE. Do not mark the PR as an accepted implementation on fixture-only evidence.
