# vqa_ring

Standalone Linux little-endian Cargo package; production depends only on cached
libc. No workspace registration, Python extension or storage-lease adoption.

```rust
use vqa_ring::{Writer, Geometry, Format, ReadResult};
let geometry = Geometry::new(2, 4, 4, Format::Rgb24)?;
let mut producer = Writer::create(geometry, true)?;
let consumer = producer.reader()?;
producer.publish(0, &[7; 48])?;
if let ReadResult::Frame(frame) = consumer.try_read(0, true) {
    assert_eq!(frame.bytes, vec![7; 48]);
    assert!(frame.valid());
}
# Ok::<(), vqa_ring::RingError>(())
```

The producer owns a writable shared mapping created before final sealing.
Consumers map read-only. Payload copies use AtomicU8 accesses; returned frames
own their copies and retain the mapping for generation-validity checks. Geometry
is rederived with checked arithmetic. Unsafe operations remain in `map.rs`.

`begin(r)`, `write(bytes)`, `commit()` are exclusive and ordered. Unfinished
writes stay odd. `write` caches the CRC; `commit` stores it before the even
sequence, matching Python even during abandoned or overwritten writes. Indices
increase and leave room for `head = r + 1`. A zero sequence is unwritten and
returns Absent independently of verification/CRC flags. Odd/changing sequences
and CRC mismatches return Torn after bounded retries. Frame.crc32 is the stored
CRC when verification is disabled. Reader histories are cumulative.

The normative design is `campaigns/visual-qa-0/v12h/pilot/s3/DESIGN_HB-HV-shmring.md`
(outside this repository). Sections 1 (versioning), 4 (publication and F-02),
and 8 (O1/O2) define the following contracts [CITED]:

| Mode | poll behavior | drop/gap accounting |
|---|---|---|
| v2 header | At most one frame per call; latest uses acquire(newest) | Matches real rtring.py: initial capacity clipping does not add a gap; subsequent clipping records skipped overruns and a gap; failed latest acquire does not advance last |
| v3 header | Batch bounded by slot capacity; latest selects newest | Capacity clipping and latest skips record gaps; each attempted index advances last, including failed reads; Lapped/Torn record their statuses |

The batch interface is the native extension of design section 4; the versioned
v2 compatibility behavior preserves section 8's Python reader oracle. Both modes
reject zero-sequence slots; this safety fix deliberately corrects Python's empty
slot behavior when CRC verification is disabled. v2 supports RGB24/CRC and polling;
v3 additionally supports BGRX32, CRC_VALID, shutdown and notification words.
O2 normalizes a v3 RGB24/CRC image by setting version to v2 and zeroing bytes
48..63 [CITED design section 1]; every other byte must equal Python's image.

`handover::send/recv` require connected filesystem AF_UNIX SOCK_SEQPACKET sockets.
Peers authenticate uid; receive validates pid, optional nonce, inode uid, seals,
geometry, size and header. Received rights immediately become OwnedFd. Callers
own socket ACLs/deadlines and must acquire the separately authorized storage lease
before integration. Local `Writer::reader` is a trusted-owner construction path.

`wait` shares one monotonic absolute deadline across interruptions and fallback.
Teardown sets shutdown, increments notify and wakes both words. Socket death
policy belongs to the caller; timeout alone does not declare producer death.

Validation (from repository root):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 rust/vqa_ring/tools/generate_fixtures.py
PATH=$HOME/.cargo/bin:$PATH taskset -c 2-4 cargo test --offline --locked --manifest-path rust/vqa_ring/Cargo.toml --target-dir build/room_R/target
PYTHONDONTWRITEBYTECODE=1 python3 rust/vqa_ring/tools/run_mutants.py
```

`generate_fixtures.py` drives the real `vadapt/rtring.py` through
`python_reference.py`: RGB24 O1 snapshot digests must match actual Python memory.
Its v3 batch expectations supplement, rather than replace, that reference.
`fixtures/python_v2` contains Python single-delivery histories and phase digests;
`python_v2_receipt.json` pins the Python source and every fixture SHA-256.
`python_v2_byte_identity_and_reader` regenerates reference results live, compares
header, slot headers/padding and payload bytes at every operation, and verifies
persisted digests and histories. Frame files exist only in temporary `/dev/shm`
directories, deleted in finally; transient comparison bytes travel over stdout.
The workspace adapter changes storage allocation only; ring methods are original.
BGRX32 is v3-only and has no Python v2 reference.

The release structural oracle builds the actual library offline, verifies a
reader relocation to the isolated index recheck, and disassembles that boundary.
It requires `dmb ishld` or an acquiring load on aarch64 [CITED design F-02 and
CARD_SR1FX conductor ruling]; unrelated initial Acquire loads cannot satisfy it.
Without objdump/llvm-objdump it inspects compiler-emitted assembly; absent evidence
fails closed. M-RI-FENCE removes this barrier in release libraries too. **Mutation
features are verification-only and must never be enabled in production.** Other
mutation features affect unit-test builds only. No loom dependency is used.

Writer stress isolates memory publication from notification: fixed stack buffers,
atomic copying, and the same begin/write/commit core with notification disabled.
Thread-local allocator/futex counters assert no heap operation or ring futex call
inside either measured loop. A legacy publish/copy negative control proves these
counters detect both. VQA_STRESS_SECONDS controls duration within a bounded range.
Hardware stress is secondary evidence, not an ordering proof. The runner reports
all trials, including surviving M-FENCE trials and secondary M-RI-FENCE stress;
one observed kill qualifies a hardware mutant, never a compilation/harness error.
See NOTICE for pattern provenance.
