# FX2-F5: Rust GM v2.1 conformance port

## Prerequisite and scope

This port starts from bridge/integration at
32993e59e3a0ac21afe3319715bd26ecf4669ab3, which contains
the GM v2.1 fix (PR 228, head 99c53d61).
The behavioral authority is eval/v2x/gm_bands_hv.py, not the older
rust/vqa_k_gm_proto estimator. The Python v2 oracle remains unchanged:
its fixture generator, reference module, operation encoding, and canonical
digest are not rewritten to force an apparent native match.

The rust/vqa_k_gm_v2 crate has no third-party dependencies. Its library
implements integer-only dynamic-band projections, compact chrome eligibility,
recomputed aligned four-neighbor informative residuals, uncropped stationary
counterevidence, whole-grid capped changed support, exact ratio sorting and
alias refusal, second-mode runner veto, sparse exact boundary certification,
fixed-chrome exact-viewport score compatibility, and changed-only full real
tile consensus. k_top is a wire compatibility field: it does not alter the
v2.1 candidate retention policy.

The output tuple is (dx: i32, dy: i32, score: i64), or absence. Positive
scores are ceiling(raw informative SAD / informative pixel count).
Ratios are sorted with an integer Euclidean comparator without floating point.
An identical non-flat pair is the special motion tuple (0, 0, 0);
two spatially flat frames abstain.

## Exact wire contract

All words are explicitly encoded little-endian; the byte streams have no
GM2/GM2R magic, no packet lengths, and no synchronization recovery.

| Operation | Request following i32 opcode | Response |
|---|---|---|
| 0 | Nothing | None; terminate normally |
| 1 | h:u32, w:u32, max_d:i32, k_top:u32, s:u32, prev:u8[h*w], cur:u8[h*w] | status:i32=0, has_gm:u8, dx:i32, dy:i32, score:i64, gh:u32, gw:u32, mask:u8[gh*gw] |
| 2 | h:u32, w:u32, s:u32, dx:i32, dy:i32, prev:u8[h*w], cur:u8[h*w] | status:i32=0, gh:u32, gw:u32, mask:u8[gh*gw] |

The mask is row-major with one byte per full REAL destination tile
(0/1). Every real pixel of a claimed tile must have an in-bounds translated
source with equal luma, and at least one destination pixel must have changed
in-place. Padding pixels are ignored; neither the last row nor last column
is discarded for nonmultiple-of-s dimensions. Grid dimensions are
ceil(h/s) and ceil(w/s), not the adaptive prototype grid.

Every response is flushed before accepting the next request. Reading is
repeated until the complete opcode/header/frames arrive; EOF only between
complete requests is clean. A short opcode, partial word, truncated payload,
zero dimensions, zero tile side, negative radius, unsupported operation,
checked length overflow, or failed buffer reservation exits nonzero without
an invented success packet. No arbitrary numeric dimension ceilings are
introduced. Displacements are signed and their subtraction is evaluated
in wider integers before indexing; allocation sizes use checked products.
Luma, tile weights, projections, and residuals use unsigned wide arithmetic.
Exact nonnegative fraction ranking uses Euclidean division to avoid
cross-product overflow.

## Offline build and parity gate

Requested build:

~~~sh
cargo build --release --offline --manifest-path rust/vqa_k_gm_v2/Cargo.toml
~~~

Requested parity and regression run:

~~~sh
python3 -m unittest \
  tests.test_jq1_a4_rust_gm_v2_conformance_gate \
  tests.test_jq1_a4_oracle_check_binary_mismatch_arms \
  tests.test_jq1_a4_oracle_digest_and_card_vectors \
  tests.test_jq1_a4_oracle_op2_and_error_arms \
  tests.test_native_parity_v2k \
  tests.test_v2x_gm_bands_hv \
  tests.test_v2x_gm_bands_hv_adversarial
~~~

**Observed in this ChatGPT VM:** cargo: command not found (exit 127).
The Python command was attempted, but the repository checkout is absent
from the VM: all seven module imports failed with
ModuleNotFoundError: No module named 'tests', exit 1. Consequently neither
Rust compilation nor native parity, including the 71 oracle cases and
interleaved pipe sequence, is established by this VM. The two native tests
are unpinned so that a host lacking a real passing binary fails visibly.
No synthetic native substitute or faked timing was used.

## Safety and follow-up

The native implementation is published for host compilation and collection,
not certified as accepted. The host/orchestrator must execute the offline
build and exact gate and diagnose any difference rather than weakening
assertions or silently marking unexpected success. Future related consumers
fx2-f5-j4-gm-v2-oracle-protocol-and-framing and
fx2-f5-j5-gm-v2-differential-fuzz retain their separate ownership.
No frame bytes were written to disk as test fixtures in this attempt.
