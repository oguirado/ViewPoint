# W8U vision-engine resident-pair RGB24 validation

**Status:** production-owner candidate; not cross-language ACCEPT.
**Gate families:** Required vision-engine pair mode; U3 Host API and gates.

## Bound source evidence

Read repository **oguirado/vision-engine**, ref **bridge/integration**, commit **5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b**.

- vadapt/xfb.py lines 46–68: native XWD parser checks format, masks, geometry and both byte-order fields, but has no explicit cap on header_size and ncolors.
- vadapt/xfb.py line 70: validate_frame(buf, width=None, height=None) checks exact native frame length and optional geometry.
- vadapt/xfb.py lines 80–100: rgb24 converts both native byte orders to owned top-down RGB24 bytes.
- vadapt/xfb.py line 102: frame_sha256 hashes canonical RGB24 pixels, not XWD metadata or packed BGRX.
- vadapt/xfb.py lines 114–139: consistent_read admits only consecutive byte-identical reads and raises E_TORN on bounded exhaustion.
- tests/test_xfb.py: bound native tests remain unchanged.

## Boundary and APIs

The source file vadapt/w8u_pair.py does not open X11, spawn a process, write frames, authenticate a binding, or serialize W8U control/FRAME records. It consumes an **injected external** srf-fbread-style PairSample containing framebuffer and GetImage XWD legs, a raw advertised canonical digest, and an opaque sender-side completion receipt. An independently injected trusted callback must verify sender-side grab and checked ungrab; a grabbed flag alone is explicitly rejected.

**select_capture_mode** is closed to resident_pair or unsynchronised_consistent. Unknown values and aliases fail E_WIRE. A rejected pair never auto-downgrades.

**PairBounds** requires four explicit per-session exact integer caps: max_header_bytes, max_colors, max_stride_bytes and max_record_bytes. These are injected structural capabilities, not locally invented W8U canonical protocol limits.

**validate_pair**, with keyword expected_geometry and explicit bounds, checks typed geometry, both byte-buffer sizes, and the smaller of caller record cap and geometry cap, namely 128 + 16 + 2 × (4 × width × height + 1 MiB), prior to owned buffer copies or RGB24 allocation. For each leg it checks XWD metadata caps and exact total size, then invokes existing xfb.validate_frame, xfb.rgb24 and xfb.frame_sha256. Equal canonical RGB24 is admitted despite unequal XWD headers; unequal pixels raise E_TORN and a forged advertised canonical SHA raises E_HASH. Evidence owns RGB24 bytes, width, height, stride and both canonical hash values, never raw XWD bytes.

**capture_resident_pair** requires the injected producer, independent sender completion verifier, and atomic publisher. A VerifiedGrab must attest coverage of both legs, successful ungrab and release no later than the deadline. An advertised SHA is mandatory for publication, and cancellation is checked immediately before publication. Failed validation, release, expiry, or cancellation never calls the publisher and never returns last-good data.

**capture_unsynchronised_consistent** is an explicitly selected legacy mode, not a fallback to a rejected pair. It uses unchanged xfb.consistent_read with a bounded single-leg read cap. Continuously moving valid XWD pixels exhaust bounded retries and raise E_TORN.

## Refusals and missing authority

Tests verify E_TORN for moving or disagreeing legs, E_HASH for forged/missing SHA, E_TIMEOUT for failed ungrab, expired verified grab and prepublication cancellation, E_WIRE for missing/invalid sender verification, E_LENGTH for record/header/color bounds, E_GEOMETRY for wrong dimensions and preserved native E_XWD_BYTE_ORDER for invalid bitmap bit order. These are local exception values; **native-to-W8U numeric error translation has not been frozen**, and tests do not claim cross-language acceptance.

At inspection, oguirado/OpenSuperficies ref bridge/integration was commit **f4d479e789f16b643201ad839654ce916bd0450e**. Its tree included the w8u-os-contract task dispatch but not a merged, accepted revision-bound contract or its independent cross-language vectors. The following remain **AUSENTE**: exact binding/action-result schema IDs, W8UH HELLO confirmation, all W8UE END kind and numeric code values including normal completion and unknown-code refusal, digest byte domains, complete XWD subset numeric caps, native-to-W8U error mapping, and independently authored accepted/rejected cross-language fixtures.

Do not substitute local defaults. Transport framing and credit, initial ACK capacity, STOP/END, storage, lease release, target/grant/root and binding authentication, closed receipt projection, simulation tick and input-action ownership remain external capabilities and cannot be claimed by this pair validator. No capture gives input authority; no stale last-good is accepted, and metadata_only retention cannot claim pixel replay. A conductor must rerun gates at accepted merged owner revisions.

## Offline VM gate

tests/test_w8u_ve_pair_unit.py declares **19 nonempty exact MANDATORY_CASES**, with no skips, expected failures or unexpected successes. An in-memory source redraws one valid pixel on each ungrabbed read; a fake Grab freezes that same source through framebuffer and GetImage readings, and Ungrab resumes redraw. Tests include equal RGB24 despite differing XWD headers, no publishing after forged SHA/mutating grab/failing ungrab/expiry/cancellation, refusal of flag-only verification, metadata limits before canonical allocation, closed mode, and no reuse of last good.

The exact supplied unittest and MANDATORY_CASES gate expression ran in an isolated VM directory with a **stand-in exposing the bound xfb public seams** and passed: **19 tests, OK**. The new module and test file subsequently published on the branch match the VM-tested copies by exact Git blob SHA. This is **VM smoke evidence, not an authoritative full-repository or host run**. The final acceptance gate against a full bridge/integration checkout and the accepted cross-language owner remains unverified.
