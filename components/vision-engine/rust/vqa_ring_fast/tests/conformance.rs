use std::sync::atomic::{AtomicU8, Ordering};

use vqa_ring_fast::{copy_atomic_bytes, crc32};

const FRAME_1080P_RGB24: usize = 1_920 * 1_080 * 3;
const BEFORE_GUARD: u8 = 0xa5;
const AFTER_GUARD: u8 = 0x5a;

// Alignment is part of the fixture, rather than an assumption about stack or
// allocator placement. All fixture bytes are generated in RAM; no fixture files
// or other pixel-buffer output are used by this suite.
#[repr(C, align(8))]
struct AlignedBytes<const N: usize>([u8; N]);

#[repr(C, align(8))]
struct AlignedAtomicBytes<const N: usize>([AtomicU8; N]);

// An array of these words has no inter-word padding and provides genuinely
// eight-byte-aligned atomic-capable storage for large copies.
#[repr(C, align(8))]
struct AtomicWord([AtomicU8; 8]);

// Fixed-seed xorshift64*: this is solely a reproducible in-memory fixture
// generator. Filling by little-endian words makes its output platform-stable.
struct FixtureRng(u64);

impl FixtureRng {
    fn new(seed: u64) -> Self {
        assert_ne!(seed, 0);
        Self(seed)
    }

    fn next_u64(&mut self) -> u64 {
        let mut state = self.0;
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        self.0 = state;
        state.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    fn fill(&mut self, bytes: &mut [u8]) {
        for chunk in bytes.chunks_mut(8) {
            let word = self.next_u64().to_le_bytes();
            chunk.copy_from_slice(&word[..chunk.len()]);
        }
    }
}

#[test]
fn crc_empty_and_standard_check_vector() {
    assert_eq!(crc32(&[]), 0);
    assert_eq!(crc32(&[]), vqa_ring::layout::crc32(&[]));

    let bytes = b"123456789";
    assert_eq!(crc32(bytes), 0xcbf4_3926);
    assert_eq!(crc32(bytes), vqa_ring::layout::crc32(bytes));
}

#[test]
fn crc_every_length_through_70_at_four_start_alignments() {
    let mut fixture = AlignedBytes([0u8; 80]);
    FixtureRng::new(0x81ba_d2f0_95e7_4c63).fill(&mut fixture.0);
    assert_eq!(fixture.0.as_ptr() as usize % 8, 0);

    // Actual starting-address residues 0, 1, 2, and 3 modulo eight, covering
    // all four byte alignments modulo four as well.
    for alignment in 0..4 {
        for length in 0..=70 {
            let input = &fixture.0[alignment..alignment + length];
            assert_eq!(input.as_ptr() as usize % 8, alignment);
            assert_eq!(
                crc32(input),
                vqa_ring::layout::crc32(input),
                "CRC mismatch: length={length}, alignment={alignment}"
            );
        }
    }
}

#[test]
fn crc_seeded_random_buffers_through_one_1080p_rgb24_frame() {
    // Each of these seeds generates its own full-size buffer. Boundary lengths
    // exercise both complete eight-byte groups and remainders; all three seeds
    // include exactly 6_220_800 bytes, the required 1080p RGB24 frame size.
    let cases = [
        (0x0d83_9b12_6c5a_47efu64, 0usize),
        (0x72ea_5fc0_9613_b84du64, 1usize),
        (0xc416_07ad_5e29_f3b8u64, 7usize),
    ];
    let lengths = [
        71,
        72,
        79,
        80,
        127,
        128,
        129,
        255,
        256,
        257,
        1_023,
        1_024,
        4_095,
        4_096,
        65_535,
        65_536,
        1_048_576,
        FRAME_1080P_RGB24 - 1,
        FRAME_1080P_RGB24,
    ];

    assert_eq!(FRAME_1080P_RGB24, 6_220_800);
    for (seed, offset) in cases {
        let mut fixture = vec![0u8; FRAME_1080P_RGB24 + 7];
        FixtureRng::new(seed).fill(&mut fixture);
        for length in lengths {
            let input = &fixture[offset..offset + length];
            assert_eq!(
                crc32(input),
                vqa_ring::layout::crc32(input),
                "CRC mismatch: seed={seed:#018x}, length={length}, offset={offset}"
            );
        }
    }
}

#[test]
fn copy_every_length_through_70_at_all_source_and_destination_alignments() {
    let mut expected = [0u8; 88];
    FixtureRng::new(0x37d4_06ca_f819_a25b).fill(&mut expected);
    let source = AlignedAtomicBytes(std::array::from_fn::<_, 88, _>(|index| {
        AtomicU8::new(expected[index])
    }));
    assert_eq!(source.0.as_ptr() as usize % 8, 0);

    // This is 71 lengths x 8 source residues x 8 destination residues: 4_544
    // copies, including empty copies and every head/body/tail transition.
    for length in 0..=70 {
        for source_alignment in 0..8 {
            for destination_alignment in 0..8 {
                let mut destination = AlignedBytes([BEFORE_GUARD; 96]);
                let start = 8 + destination_alignment;
                let end = start + length;
                let expected_payload = &expected[source_alignment..source_alignment + length];
                for (byte, expected_byte) in destination.0[start..end]
                    .iter_mut()
                    .zip(expected_payload)
                {
                    // A byte left unwritten must differ from its expected value.
                    *byte = !expected_byte;
                }
                destination.0[end..].fill(AFTER_GUARD);

                let source_pointer = source.0.as_ptr().wrapping_add(source_alignment);
                assert_eq!(source_pointer as usize % 8, source_alignment);
                assert_eq!(
                    destination.0[start..end].as_ptr() as usize % 8,
                    destination_alignment
                );

                // SAFETY: the source contains live, initialized AtomicU8 values
                // covering the full range and is suitably aligned for any
                // eight-byte body. There are no concurrent source writes and
                // the separate destination allocation cannot overlap it.
                unsafe {
                    copy_atomic_bytes(source_pointer, &mut destination.0[start..end]);
                }

                assert_eq!(
                    &destination.0[start..end],
                    expected_payload,
                    "copy mismatch: length={length}, source={source_alignment}, destination={destination_alignment}"
                );
                assert!(
                    destination.0[..start].iter().all(|&byte| byte == BEFORE_GUARD),
                    "prefix guard changed: length={length}, source={source_alignment}, destination={destination_alignment}"
                );
                assert!(
                    destination.0[end..].iter().all(|&byte| byte == AFTER_GUARD),
                    "suffix guard changed: length={length}, source={source_alignment}, destination={destination_alignment}"
                );
                for (index, byte) in source.0.iter().enumerate() {
                    assert_eq!(
                        byte.load(Ordering::Relaxed),
                        expected[index],
                        "source changed: index={index}, length={length}, source={source_alignment}, destination={destination_alignment}"
                    );
                }
            }
        }
    }
}

#[test]
fn copy_empty_destination_accepts_a_null_source() {
    let mut destination = [BEFORE_GUARD, AFTER_GUARD];
    // SAFETY: the destination is empty, so the API must not access source.
    unsafe {
        copy_atomic_bytes(std::ptr::null(), &mut destination[1..1]);
    }
    assert_eq!(destination, [BEFORE_GUARD, AFTER_GUARD]);
}

#[test]
fn copy_one_1080p_rgb24_frame_with_unaligned_source_and_guards() {
    const SOURCE_OFFSET: usize = 5;
    const DESTINATION_PREFIX: usize = 11;
    const DESTINATION_SUFFIX: usize = 13;
    let mut expected = vec![0u8; FRAME_1080P_RGB24 + 8];
    FixtureRng::new(0xa809_2db6_f15c_734e).fill(&mut expected);
    assert_eq!(std::mem::size_of::<AtomicWord>(), 8);
    let source: Vec<AtomicWord> = expected
        .chunks_exact(8)
        .map(|word| AtomicWord(std::array::from_fn(|index| AtomicU8::new(word[index]))))
        .collect();
    let source_pointer = source
        .as_ptr()
        .cast::<AtomicU8>()
        .wrapping_add(SOURCE_OFFSET);
    assert_eq!(source_pointer as usize % 8, SOURCE_OFFSET);

    let start = DESTINATION_PREFIX;
    let end = start + FRAME_1080P_RGB24;
    let mut destination = vec![BEFORE_GUARD; end + DESTINATION_SUFFIX];
    let expected_payload = &expected[SOURCE_OFFSET..SOURCE_OFFSET + FRAME_1080P_RGB24];
    for (byte, expected_byte) in destination[start..end].iter_mut().zip(expected_payload) {
        *byte = !expected_byte;
    }
    destination[end..].fill(AFTER_GUARD);

    // SAFETY: contiguous, initialized eight-byte AtomicWord objects cover the
    // whole source range; their representation has no inter-word padding. No
    // writer runs during the copy, and destination is a separate allocation.
    unsafe {
        copy_atomic_bytes(source_pointer, &mut destination[start..end]);
    }

    assert_eq!(&destination[start..end], expected_payload);
    assert!(destination[..start].iter().all(|&byte| byte == BEFORE_GUARD));
    assert!(destination[end..].iter().all(|&byte| byte == AFTER_GUARD));
    for (word_index, word) in source.iter().enumerate() {
        for (byte_index, byte) in word.0.iter().enumerate() {
            assert_eq!(
                byte.load(Ordering::Relaxed),
                expected[word_index * 8 + byte_index],
                "large source changed: word={word_index}, byte={byte_index}"
            );
        }
    }
}
