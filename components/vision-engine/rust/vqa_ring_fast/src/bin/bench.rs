//! Warmed, in-VM timing only. The host decides performance; there are no gates here.
//!
//! Every frame is generated in RAM from a fixed seed. This binary writes only
//! scalar metadata and timings to stdout, never pixels or binary fixtures.

use std::hint::black_box;
use std::mem::size_of;
use std::sync::atomic::{AtomicU64, AtomicU8};
use std::time::Instant;

use vqa_ring_fast::{copy_atomic_bytes, crc32};

const WARMUP_RUNS: usize = 3;
const SAMPLES: usize = 31;

fn next_random(state: &mut u64) -> u64 {
    // Fixed-seed xorshift64*. No entropy source or file is involved.
    *state ^= *state >> 12;
    *state ^= *state << 25;
    *state ^= *state >> 27;
    (*state).wrapping_mul(0x2545_f491_4f6c_dd1d)
}

fn generate_frame(byte_len: usize, seed: u64) -> (Vec<AtomicU64>, Vec<u8>) {
    assert_eq!(byte_len % size_of::<u64>(), 0);
    assert_ne!(seed, 0);

    let mut state = seed;
    let mut payload = vec![0_u8; byte_len];
    let mut source = Vec::with_capacity(byte_len / size_of::<u64>());
    for chunk in payload.chunks_exact_mut(size_of::<u64>()) {
        // The seeded byte stream is independent of machine endianness. Native
        // conversion makes each atomic word's in-memory bytes match that stream.
        let bytes = next_random(&mut state).to_le_bytes();
        chunk.copy_from_slice(&bytes);
        source.push(AtomicU64::new(u64::from_ne_bytes(bytes)));
    }
    (source, payload)
}

fn median_ns(mut operation: impl FnMut()) -> u128 {
    for _ in 0..WARMUP_RUNS {
        operation();
    }

    // Allocate sample storage before timing. The odd count has one exact middle
    // observation, so the printed median is an integer number of nanoseconds.
    let mut samples = [0_u128; SAMPLES];
    for sample in &mut samples {
        let started = Instant::now();
        operation();
        *sample = started.elapsed().as_nanos();
    }
    samples.sort_unstable();
    samples[SAMPLES / 2]
}

fn benchmark_frame(label: &str, width: usize, height: usize, seed: u64) {
    let byte_len = width * height * 3;

    // All allocation, initialization and fixture generation precede timing.
    // AtomicU64 storage provides the source's actual required word alignment.
    let (source_words, payload) = generate_frame(byte_len, seed);
    let mut destination = vec![0_u8; byte_len];
    let source = source_words.as_ptr().cast::<AtomicU8>();

    let copy_median = median_ns(|| {
        // SAFETY: source_words owns byte_len initialized bytes with AtomicU64
        // alignment and stays live and unchanged throughout the call. After
        // construction, source storage is accessed only through atomic loads.
        // No other thread accesses it, and destination is a disjoint allocation.
        unsafe {
            copy_atomic_bytes(black_box(source), black_box(destination.as_mut_slice()));
        }
        // Escape the complete copied region, not just one byte or its pointer.
        black_box(destination.as_slice());
    });
    assert_eq!(destination, payload, "copy mismatch for {label}");

    let mut checksum = 0_u32;
    let crc_median = median_ns(|| {
        checksum = black_box(crc32(black_box(payload.as_slice())));
    });

    println!(
        "VM frame={label} resolution={width}x{height} format=RGB24 bytes={byte_len} \
         operation=atomic_copy samples={SAMPLES} warmup={WARMUP_RUNS} median_ns={copy_median}"
    );
    println!(
        "VM frame={label} resolution={width}x{height} format=RGB24 bytes={byte_len} \
         operation=crc32 samples={SAMPLES} warmup={WARMUP_RUNS} median_ns={crc_median} \
         crc32=0x{checksum:08x}"
    );
}

fn main() {
    println!(
        "VM benchmark: warmed timings with fixed-seed RAM-only frames; \
         the host decides performance."
    );
    benchmark_frame("1080p", 1920, 1080, 0x1080_5eed_5a17_ba11);
    benchmark_frame("4K-UHD", 3840, 2160, 0x2160_5eed_5a17_ba11);
}
