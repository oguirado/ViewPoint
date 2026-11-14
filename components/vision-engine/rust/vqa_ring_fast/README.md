# vqa_ring_fast

An isolated SR2 candidate for the SR1-05 hot paths in `vqa_ring`:
`Mapping::load_into` currently loads each payload byte through `AtomicU8`, and
`layout::crc32` computes each CRC bit individually. This crate supplies the two
replacement primitives for conformance and VM timing. It does not change
`vqa_ring` or claim that host performance budgets have been met.

## API and implementation

| Function | Behavior |
| --- | --- |
| `unsafe copy_atomic_bytes(src: *const AtomicU8, dst: &mut [u8])` | Copies exactly `dst.len()` bytes. Loads the unaligned head and short tail through `AtomicU8` with `Relaxed` ordering; loads the body through aligned `AtomicU64` with `Relaxed` ordering. |
| `crc32(bytes: &[u8]) -> u32` | Slicing-by-8 CRC32 with reflected polynomial `0xedb88320`, initial state `0xffffffff`, and final xor `0xffffffff`, matching `vqa_ring::layout::crc32`. Empty input returns zero. |

Both functions allocate nothing. Copy alignment is computed from the actual
source address using `align_of::<AtomicU64>()`; destination alignment is
unrestricted. No source load extends outside the requested range. An empty
destination is a no-op, including when the source pointer is null. Converting
each loaded word with `to_ne_bytes()` preserves memory byte order. CRC chunks
are decoded explicitly as little-endian, so the algorithm is independent of
native endianness and input alignment.

The CRC's eight 256-entry tables occupy 8 KiB and are generated at compile time.
Runtime body iterations combine eight table lookups; the remaining zero to seven
bytes use the base table. There is no runtime bit loop or lazy initialization.

The crate has no production dependencies. Its only direct dependency is
`vqa_ring = { path = "../vqa_ring" }` under `[dev-dependencies]`, used solely as
the CRC oracle in `tests/conformance.rs`. The lockfile also includes `libc`,
which is an existing transitive dependency of that oracle; no new external
crate is introduced. The benchmark uses only this crate and the Rust standard
library. Targets must provide eight-bit and 64-bit atomics. The oracle's tests
require little-endian Linux because that is `vqa_ring`'s platform contract.

## Atomic access contract

The raw-pointer copy is deliberately `unsafe`. For a nonempty destination, the
caller must provide a live, initialized, atomic-capable source region covering
the entire requested range within one allocation or mapping. It must remain
valid throughout the call and be disjoint from the destination. The destination
is exclusively owned through its mutable slice.

All overlapping accesses must satisfy the
[Rust atomic memory model](https://doc.rust-lang.org/std/sync/atomic/index.html#memory-model-for-atomic-accesses).
Unsynchronized conflicting atomic operations may not partially overlap: a
concurrent byte store cannot overlap an eight-byte load. Concurrent writes are
allowed only when they use exactly the same access boundaries and widths as
the overlapping loads. Concurrent non-atomic writes are forbidden. Synchronizing
all source writes before and after the copy is a sufficient alternative.

`Relaxed` loads supply neither publication synchronization nor a coherent frame
snapshot. A seqlock retry, CRC check, or later validation cannot repair an
invalid mixed-width access. **The current byte-wise `Mapping::store` must not
race the wide copy.** Tests and the benchmark have no concurrent source writers.

Read-only mappings require a target-specific check of Rust's
[documented relaxed-load guarantees](https://doc.rust-lang.org/std/sync/atomic/index.html#atomic-accesses-to-read-only-memory).
The documented eight-byte limit includes x86_64 and aarch64; the availability
of `AtomicU64` alone does not establish that every target supports its loads
from read-only mappings. The caller is responsible for valid atomic storage
and underlying page permissions on the actual target.

## Conformance

From the repository root:

```sh
cargo test --release --manifest-path rust/vqa_ring_fast/Cargo.toml
cargo test --release --offline --manifest-path rust/vqa_ring_fast/Cargo.toml
```

The offline command requires `vqa_ring`'s pinned `libc` package already in the
Cargo cache. The committed lockfile records the exact dependency graph.

`tests/conformance.rs` checks:

- Empty input and the standard `123456789` CRC32 check value.
- CRC equality with `vqa_ring::layout::crc32` for every length `0..=70` at four
  verified start-address alignments: 284 comparisons.
- Three fixed xorshift64* seeds across 19 larger lengths, including exactly
  6,220,800 bytes, one 1920 x 1080 RGB24 frame, and adjacent body/tail boundaries.
- Copy equality for every length `0..=70` at all eight source alignments crossed
  with all eight destination alignments: 4,544 cases. Full prefix/suffix guards
  must remain intact, and source bytes must remain unchanged.
- A null-source empty copy and a guarded 1080p copy with an unaligned source.

Explicit aligned storage establishes the tested address residues. The fixed
seeds are recorded in the source, and generated byte streams are deterministic.

## VM benchmark

```sh
cargo run --release --offline --manifest-path rust/vqa_ring_fast/Cargo.toml --bin bench
```

The binary generates two RGB24 buffers in RAM: 1920 x 1080 (6,220,800 bytes) and
3840 x 2160 UHD 4K (24,883,200 bytes). Fixed seeds are `0x10805eed5a17ba11` and
`0x21605eed5a17ba11`, respectively. Atomic copy sources use aligned
`Vec<AtomicU64>` storage and remain unchanged during timing.

Allocation and fixture generation occur before timing. Each operation receives
three warm-up calls and 31 timed samples. `std::hint::black_box` protects inputs
and outputs, including the entire copy destination. Copy equality is asserted
outside the timed interval. Every timing line starts with `VM` and includes the
operation, dimensions, byte count, sample count, and integer `median_ns`.

These are warmed VM microbenchmarks. They do not time the ring protocol, validate
concurrent overwrites, represent cold-cache behavior, or certify a speedup over
the existing implementation. The host decides performance and design budgets.

## SR2 adoption path

1. Collect host timings for both old and candidate paths on the intended hardware
   and workload. Evaluate the copy and CRC budgets separately, then evaluate the
   complete reader/writer path. Keep VM results identified as VM evidence.
2. Adopt the CRC implementation and its tables into `vqa_ring::layout` under the
   SR2 change scope, retaining the old bit-wise algorithm as a test oracle and
   preserving polynomial, initialization, final xor, and wire CRC values. Moving
   the implementation or extracting a shared core avoids a dependency cycle.
3. Establish the copy's access contract for every producer and consumer before
   replacing the current byte loop. Either enforce synchronized slot ownership
   that excludes overlapping writes for the whole copy, or make all concurrent
   payload operations use the same disjoint word/head/tail partition. Arbitrary
   partial copies can change that partition, so they require their own audit.
   Changing only the reader while retaining racing byte stores is invalid.
4. Keep the pointer adaptation inside `vqa_ring`'s existing `map.rs` unsafe
   boundary. Validate the complete source range with overflow-safe arithmetic
   before copying; apply the optimization only to compatible payload storage.
   Preserve the separate metadata access widths, publication ordering, fences,
   bounded retries, and generation checks. Verify read-only mapping support on
   each target used for consumers.
5. Run the existing ring conformance, ordering, and stress checks after the SR2
   integration, alongside these CRC/copy tests and host performance evaluation.
   The standalone tests here establish byte results under the documented
   contract; they do not establish a concurrent ring integration's correctness.

No integration or modification of `rust/vqa_ring` is part of this crate.

## Storage law

Frames, luma planes, and pixel buffers live only in RAM. Fixtures are generated
from fixed seeds in memory; there are no binary fixtures and no image, frame,
or payload files. Neither tests nor the benchmark write pixel buffers to disk.
The benchmark prints only scalar metadata, CRCs, and VM timing results. Cargo
build products and textual reports contain no serialized fixture payloads.
