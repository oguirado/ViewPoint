#![deny(unsafe_op_in_unsafe_fn)]
//! Allocation-free candidates for the SR1-05 payload-copy and CRC hot paths.
//!
//! These primitives do not implement publication, slot ownership, or retries.
//! In particular, widening loads requires the access-width contract documented
//! on [`copy_atomic_bytes`]; it cannot be substituted into a racing byte writer.

#[cfg(not(all(target_has_atomic = "8", target_has_atomic = "64")))]
compile_error!("vqa_ring_fast requires 8-bit and 64-bit atomic loads");

use std::mem::{align_of, size_of};
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};

/// Copy `dst.len()` bytes from an atomic-capable shared region into owned bytes.
///
/// The unaligned source head and the short tail use `AtomicU8` relaxed loads.
/// The body uses aligned `AtomicU64` relaxed loads. Destination alignment is
/// unrestricted, and no load extends outside the requested source range.
/// Native-endian conversion preserves the source byte order on either endian.
/// The function allocates nothing. An empty destination does not access `src`.
///
/// Relaxed loads do not publish data or guarantee a coherent frame snapshot;
/// the caller supplies the necessary synchronization and generation checks.
///
/// # Safety
///
/// For a nonempty destination, the caller must guarantee all of the following:
///
/// - `src` spans at least `dst.len()` initialized bytes in one live allocation
///   or mapping, remains valid throughout the call, and does not overlap `dst`.
/// - The source storage permits atomic loads of the widths used here. For
///   read-only mappings, the target must support relaxed eight-byte loads on
///   read-only memory as specified by Rust's atomic module documentation.
/// - All conflicting source accesses obey Rust's atomic memory model. In
///   particular, no unsynchronized byte store may overlap a word load. Concurrent
///   atomic writes must access exactly the same bytes with the same width as
///   each overlapping load; concurrent non-atomic writes are forbidden.
///   Synchronizing all source writes before/after the copy is sufficient.
///
/// A seqlock retry or a CRC check cannot make conflicting mixed-width accesses
/// valid. The existing byte-wise `vqa_ring::Mapping::store` must not race this
/// function. For an empty destination, even a null `src` is accepted.
pub unsafe fn copy_atomic_bytes(src: *const AtomicU8, dst: &mut [u8]) {
    let len = dst.len();
    if len == 0 {
        return;
    }

    const WORD_BYTES: usize = size_of::<AtomicU64>();
    const WORD_ALIGN: usize = align_of::<AtomicU64>();
    // Only inspect the address; all pointers used for access retain provenance.
    let head = ((src as usize).wrapping_neg() & (WORD_ALIGN - 1)).min(len);
    for (offset, byte) in dst[..head].iter_mut().enumerate() {
        // SAFETY: offset is inside the caller-provided initialized region;
        // AtomicU8 needs no additional alignment, and access rules are promised.
        *byte = unsafe { (&*src.add(offset)).load(Ordering::Relaxed) };
    }

    let mut offset = head;
    while len - offset >= WORD_BYTES {
        // SAFETY: the byte head aligned this address for AtomicU64. A whole word
        // remains inside the source region. The caller guarantees compatible
        // concurrent access widths and validity for atomic loads on this target.
        let word = unsafe { (&*src.add(offset).cast::<AtomicU64>()).load(Ordering::Relaxed) };
        dst[offset..offset + WORD_BYTES].copy_from_slice(&word.to_ne_bytes());
        offset += WORD_BYTES;
    }

    for (relative, byte) in dst[offset..].iter_mut().enumerate() {
        // SAFETY: every remaining offset is in bounds; the tail uses byte loads.
        *byte = unsafe { (&*src.add(offset + relative)).load(Ordering::Relaxed) };
    }
}

const POLYNOMIAL: u32 = 0xedb8_8320;

// Compile-time generation: the runtime CRC path never computes bits individually
// and has no lazy initialization, allocation, or synchronization cost.
const fn make_tables() -> [[u32; 256]; 8] {
    let mut tables = [[0u32; 256]; 8];
    let mut byte = 0;
    while byte < 256 {
        let mut crc = byte as u32;
        let mut bit = 0;
        while bit < 8 {
            crc = (crc >> 1) ^ (POLYNOMIAL & 0u32.wrapping_sub(crc & 1));
            bit += 1;
        }
        tables[0][byte] = crc;
        byte += 1;
    }
    let mut slice = 1;
    while slice < 8 {
        byte = 0;
        while byte < 256 {
            let previous = tables[slice - 1][byte];
            tables[slice][byte] = (previous >> 8) ^ tables[0][(previous & 0xff) as usize];
            byte += 1;
        }
        slice += 1;
    }
    tables
}

static TABLES: [[u32; 256]; 8] = make_tables();

/// CRC32 compatible with `vqa_ring::layout::crc32` for any byte slice.
///
/// Uses the reflected polynomial `0xedb88320`, initial state `0xffffffff`, and
/// final xor `0xffffffff`. Eight bytes are folded through eight lookup tables
/// per body iteration; a single table handles the remaining zero to seven bytes.
/// Input alignment and native byte order do not affect the result. Empty input
/// returns zero. The tables occupy 8 KiB and are generated at compile time.
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = !0u32;
    let mut chunks = bytes.chunks_exact(8);
    for chunk in &mut chunks {
        let low = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]) ^ crc;
        let high = u32::from_le_bytes([chunk[4], chunk[5], chunk[6], chunk[7]]);
        crc = TABLES[7][(low & 0xff) as usize]
            ^ TABLES[6][((low >> 8) & 0xff) as usize]
            ^ TABLES[5][((low >> 16) & 0xff) as usize]
            ^ TABLES[4][(low >> 24) as usize]
            ^ TABLES[3][(high & 0xff) as usize]
            ^ TABLES[2][((high >> 8) & 0xff) as usize]
            ^ TABLES[1][((high >> 16) & 0xff) as usize]
            ^ TABLES[0][(high >> 24) as usize];
    }
    for &byte in chunks.remainder() {
        crc = (crc >> 8) ^ TABLES[0][((crc ^ u32::from(byte)) & 0xff) as usize];
    }
    !crc
}
