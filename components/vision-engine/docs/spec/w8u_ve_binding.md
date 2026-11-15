# W8U / Vision Engine — authenticated HELLO binding admission

**Owner:** w8u-ve-binding (attempt a1). **Release status:** HOLD, not production-qualified or accepted. The module and offline tests prepare an immutable metadata-admission owner; independent accepted OS contract and integrated resident-lane evidence remain prerequisites.

## Scope and structural dependency boundary

The new stdlib-only vadapt/w8u_binding.py exposes admit_hello. It owns HELLO **binding metadata admission only**, not network/frame transport, live RAM allocation, X11 grabs, initial/cumulative ACKs, credit, input actions, receipts, worker-ui promotion or process launch. It imports no unmerged W8U sibling and does not change existing vadapt/ramws.py or vadapt/window_stream_bank.py.

The application must inject five independently trusted structural authority adapters:

1. contract_verifier(canonical_json, binding_sha256) returns an accepted revision-bound ContractProof with its literal binding schema identifier and evidence SHA-256. There is **no local production schema ID default**.
2. grant_verifier(canonical_json, binding_sha256) returns a GrantProof whose authenticated, measured, immutable claims come from the real grant, not the caller's HELLO or a matching digest. The grant proof independently measures grant/observation/ingest roots and private display.
3. channel_verifier(canonical_json, binding_sha256) returns an authenticated measured FrameChannelProof with an exact binding SHA, target, epoch, private display/XID, image/reader/allowlist pins, geometry, record cap and reserved RAM slot/byte capacity.
4. target_snapshot() returns a measured CurrentTarget from the current target registry, including handle generation, epoch, private display/XID and geometry.
5. For committed tick mode only, commit_verifier(fields, grant, channel, current) returns an independent authenticated CommitCoordinationProof binding app render and capture revisions. Grab equality is insufficient.

The proof dataclasses are immutable Python carrier types, not a substitute for cryptographic authentication. Production wiring MUST ensure only trusted adapters can construct authenticated/accepted proofs from independent authority; constructing these objects directly from HELLO is forbidden. Synthetic test authorities exercise invariants but **cannot qualify worker_ui**. Every successful AdmittedBinding still exposes runtime_state=HOLD and worker_ui=False.

## Exact closed descriptor and digest

The binding object contains exactly: schema, run_id, lease_id, seat_grant_sha256, container_id, target_handle, epoch, display, xid, width, height, frames_root, frame_run_root, image_sha256, reader_sha256, exec_allowlist_sha256, max_record_bytes, max_inflight_frames, tick_mode.

Nested target_handle contains exactly kind, canonical_id, generation. Kind is one of cdp_tab, x11_window, xvfb_screen, capsule, godot_window. HELLO JSON is sorted, compact, UTF-8, 16 KiB maximum, with no LF, whitespace padding, duplicate keys, missing keys or extra keys. Integer values require exact int (not bool/float); invalid constants, unknown tick modes, noncanonical spellings and malformed hashes refuse. Accepted binding SHA-256 is calculated on the exact canonical JSON bytes, independently of the separate grant/channel authentication. This digest does not itself authenticate a seat grant.

The input bytearray/memoryview is copied before validation, and a caller-provided plain mapping undergoes a deep JSON snapshot before verifiers. BindingFields and TargetIdentity are frozen; later caller mutation, including nested handle identity, cannot alter an admitted binding. Verifier-provided identity/numeric facts undergo separate exact-type validation to forbid mutable nested identity or float/bool coercion.

Valid tick modes are committed and unavailable; no local mode default is permitted. Committed zero is valid if authenticated app render/capture coordination independently supports it. A captured monotonic timestamp is never a simulation revision. Unavailable projection of a wire-zero tick is null in downstream owners, never a false committed zero.

## Roots, channel capability and refusal predicates

The single accepted frames_root is /dev/shm/vqa-ramws. The exact frame_run_root is /dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>, with run_id constrained to a safe RAM-workspace path component. Grant root, observation root, ingest root and channel root, as well as their run-root counterparts, must agree exactly. An old /dev/shm/w8/frames root, aliases, path traversal, symlink components, alternate run path, missing measurement or cross-root mismatch must never pass. Root path comparisons in this module are metadata checks, not filesystem capability grants: production file openers must independently verify tmpfs, safe fd-relative access and lifetime against TOCTOU; this module never opens or writes a frame.

Current registry target handle kind/canonical_id/generation/epoch, geometry, private display and XID must all match the bound grant and authenticated channel. Image, reader and exec-allowlist SHA-256 identities must exactly match. Bounds are positive width/height u32, positive XID u32, positive exact max_inflight_frames and max_record_bytes at least 144. Geometry upper cap is 2*(4*width*height + 1 MiB) + 16 + 128. max_record_bytes must not exceed this cap and must fit the wire u64 domain; the independent measured channel must reserve at least max_inflight_frames slots and max_record_bytes * max_inflight_frames RAM bytes. One-frame capacity is valid. HELLO admission never issues credit.

The module uses closed symbolic refusals: E_BINDING for malformed/authentication/identity failures; E_ROOT for old/mismatched/aliased or symlink paths; E_TICK for bad modality and missing/invalid committed coordination; E_GEOMETRY for invalid dimensions/changed display/geometry or overflow; E_LENGTH for bad HELLO length and record cap; E_CREDIT for missing slot capacity; E_RAM_QUOTA for unreserved bytes. Other wire-transport refusals E_TORN, E_WIRE, E_HASH, E_SEQ, E_BACKPRESSURE and E_TIMEOUT belong to other owners. Numeric END assignments and native-to-W8U error translation remain AUSENTE absent the accepted contract, and this module does not invent them.

Every refusal must occur without grants of credit, publication of frames, action authority, worker-ui promotion, live capture, file writes or process execution. The admitted descriptor does none of these actions on success, either.

## Out of scope and required gate evidence

The following are **not** implemented by metadata admission and MUST NOT be inferred from it: W8UF FRAME pair parsing/canonical RGB24 hashing, XWD byte-order/subset validation, W8UC HELLO/ACK/STOP and W8UH confirmation semantics, W8UE END numeric error domain, cumulative credit/backpressure, receipt projection w8u.receipt/v1, input owner actions and U5 receipt evidence. These require the accepted authoritative w8u-os-contract, literal schema IDs and independently authored cross-language accepted/rejected vectors; omitted facts are AUSENTE. Do not infer values from placeholders or pending PREPARED contracts. Hash-only retention is metadata_only and cannot claim replay after purge.

Runtime must remain HOLD until **merged revision-bound** w8u-lane-ui-image, w8u-lane-ui-schema, w8u-lane-ui-seat and w8u-lane-ui-stream demonstrate one installed resident sender, authenticated channel capability and matching migrated grant/observation/ingest/receipt roots. No old-root aliases, symlinks, dual writing or relabelled historical receipts.

Gate names: U3 authenticated HELLO binding, U5 Receipt evidence, Root agreement. Execute the checked MANDATORY_CASES gate at the accepted merged owner revision. The local suite is only a 54-case synthetic public-path offline admission gate: no X, Xvfb, Docker, network, live capsule or frames stored on disk, including temp directories. It positively authenticates a complete immutable HELLO *only through injected test doubles*, and rejects bad schema, missing auth or measurement, mutable identities, old root, disagreement, stale/foreign target, false committed tick, wrong pins, geometry and capacity. No test skip/xfail/XPASS is accepted. Tests use a deliberately artificial test-only.binding.schema/v1 ID, **not** the real protocol schema.

## Source coordinates checked on bridge/integration

Observed head: 5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b.

- vadapt/ramws.py lines 19, 27, 60–62: authoritative RAM workspace root and safe tool/campaign/run path-fragment discipline.
- vadapt/ramws.py lines 32–37: RamError native code structure.
- vadapt/ramws.py lines 449–467: open_run signature and tmpfs workspace creation.
- vadapt/window_stream_bank.py lines 19, 43–52: supported Handle kinds and generation validation.
- vadapt/window_stream_bank.py lines 56–65: StreamSpec admits slots=1 and requires positive geometry and quota.
- vadapt/window_stream_bank.py lines 68–71: Handle+epoch StreamToken.
- vadapt/window_stream_bank.py lines 180–194: WindowStreamBank quota/factory seam.
- vadapt/window_stream_bank.py lines 211–216: stale generation/epoch refusal.

**Acceptance statement:** Code and tests are collection-ready for independent review, not proof of accepted OS contract or qualified live worker UI. External production authority adapters and release integration remain mandatory.
