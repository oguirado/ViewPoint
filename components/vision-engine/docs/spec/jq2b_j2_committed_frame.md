# J2 committed-frame boundary — G J2 2

## API and source coordinates

The source-neutral **vadapt.committed_frame.bind_committed_frame** takes keyword-only
**binding**, **pixel_decl**, **capture**, **lookup_publication**, and optional
**max_frame_bytes**. It returns a frozen **BoundFrame** containing an immutable
validated pixel declaration, frozen bound metadata and privately owned
**raw_bytes** and **normalized_bytes** in RAM. The injected capture callback
returns a **CaptureObservation** lease: borrowed bytes, complete identity,
even slot sequence, CRC32, independent post-copy revalidation, a release
callback, provenance, coherence witness, and residency witness. No Godot,
X11 or CDP adapter is imported. No frame is saved.

**BoundaryIdentity** binds source kind, handle ID and generation, epoch,
geometry, environment ID, binding ID, independent publication ID, presentation
revision, frame sequence, committed simulation tick, state SHA-256, and
observer reference. A CDP-derived observation additionally requires a trusted
window ID and four-coordinate crop. A render_index is never a simulation
tick. Same-tick distinct commits need an independent publication ID;
otherwise the operation refuses to bind.

**PixelDeclaration** validates all eleven fields: channel_order,
stride_bytes, orientation, alpha, color_space, width, height, dpr_ppm,
zoom_ppm, environment_id and binding_id. Supported layouts are RGB, BGR,
RGBA, BGRA, ARGB and ABGR, top-down or bottom-up, sRGB, with compatible
absent/opaque/straight alpha. Unknown color space, premultiplied alpha and
ambiguous interpretation are errors. The declared stride includes padding.

**pixel_decl_sha256** = SHA256 of the declaration's ASCII canonical JSON
(sorted keys, compact separators). **raw_sha256** = SHA256 of privately
owned raw source bytes including padding. **normalized_sha256** = SHA256
of top-down, tightly packed RGB24 (or RGBA32 for straight alpha). These
digests have intentionally different meanings. Bound metadata declares
schema_version **committed-frame-v1** and carries the exact frame_seq,
committed_tick, state_sha256 and observer_ref from the sealed publication.
Legacy receipts remain unchanged: frame_sha256, dimensions, tick, telemetry.
The richer binding does not change their shape.

Source coordinates read at explicit ref **bridge/integration**:
- vadapt/xfb.py: parse_header, validate_frame, rgb24, consistent_read and
  read_screen_fd — XWD validation, byte normalization, bounded repeated reads.
- vadapt/x11_ramcap.py: SourceConfig, _Source.check, fbread, _capture,
  capture_once — configured capture, nocursor and source/geometry guards.
- vadapt/rtring.py lines 4–20, 136–175, 200–267, 314–324 — render-index
  slots, even sequence, CRC and borrowed Frame.valid/read callbacks.
- vadapt/ramws.py lines 19–25, 44–57, 449–521 — RAM filesystem and
  workspace constraints.
- Existing tests read first: tests/test_xfb.py and
  tests/test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.py. New tests
  exercise the public boundary rather than copying these primitive tests.

## Invariants and independent contrasts

1. **INV-J2-1.** Validate complete C-PIXEL declaration and allocation
   bound before hashing or capture. Contrast healthy padded/flipped BGR
   and explicit BGRA controls with invalid layouts, missing fields and
   declaration/publication mismatches.
2. **INV-J2-2.** Validate the full handle generation, epoch, geometry,
   publication completion, independent publication ID, presentation
   revision and committed identity before capture; compare the copied
   capture identity and independently read publication afterward, then
   recheck once more after digest computation. Contrast an immutable
   committed snapshot with stale epochs, changed generations, changed
   same-tick presentation revision, changed publication ID and later
   frames relabeled as the old commit. CDP provenance cannot be
   substituted by a relabeled Godot tree.
3. **INV-J2-3.** Copy borrowed source bytes into owned immutable bytes;
   validate CRC32 over the copy, even slot sequence and independent
   (sequence, CRC32) witness after copying, after publication lookup and
   immediately before return. Always release the borrowed lease after
   capture, including on errors. Contrast input mutation after success
   with stale/torn/CRC-mutated samples. Neither the source buffer nor
   request mappings are retained by returned metadata.
4. **INV-J2-4.** Require positive coherent-capture and verified-residency
   witnesses and explicit provenance. Unsupported capture, absent
   witness or unobserved residency return typed errors, never default
   zero values. A Chromium screen is not a tab; capturePage alone is
   neither publication coherence nor residency evidence. The offline
   tests include in-memory NumPy, injected brokers, and a fake-CDP
   DOM/AX fixture with trusted window/crop; write spies reject pixel
   persistence APIs without executing a forbidden write.

The errors are stable CommittedFrameError codes (E_PIXEL_DECL,
E_PIXEL_UNSUPPORTED, E_PIXEL_LIMIT, E_PIXEL_BINDING,
E_PUBLICATION_INCOMPLETE, E_PUBLICATION_IDENTITY,
E_PUBLICATION_CHANGED, E_CAPTURE_IDENTITY, E_CAPTURE_TORN,
E_FRAME_DIGEST, E_SOURCE_PROVENANCE, E_UNTRUSTED_CROP,
E_CAPTURE_UNSUPPORTED, E_RESIDENCY_UNOBSERVED, etc.). An
admission response is not an economic acceptance.

## Blocked limits and dependencies

**AUSENTE:** The host-local source
$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md, lines 517/521 (section
10) and 764/776 (section 14), is not present in this ChatGPT VM.
Compliance with those external lines cannot be established from this
session; the design instead follows the attached task and bound files.

**AUSENTE:** A real Electron brokered publisher (H2/H3), trusted OS
residency monitor, native ARM acquire/release fences, a trusted Chromium
window-to-tab association and actual capture coherence/residency proof.
The injected residency_verified flag is a required trusted seam, NOT
a VM measurement of page residency or absence of swap. CRC is no
substitute for native acquire/release fencing. No host capacity, speed
or live adapter support is claimed from fixture-based VM tests.

**AUSENTE:** Revision-bound earlier-wave owner-gate receipts and the
conductor's admission binding. This change is a worker artifact for
collection only: hold downstream dispatch until independently proven.
The orchestrator alone decides G J2 2 acceptance.

T2 dispatch, outcomes, reset and economic authority are outside this
frame-only API. If integrated by other owner modules, external T2 fields
must stay exactly tick, seq, cmd and args with authority in trusted
injected context, and rejection may advance trusted bookkeeping without
simulation mutation. Holdings pause has empty args; new_game needs
country_id, difficulty, opening_mode and seed. No T2, receipt, ring,
X11 or Godot production path is modified here.

Offline gate: python3 -m unittest
tests.test_jq2b_j2_committed_frame_implementation. No live system
or screenshot-save API is required.
