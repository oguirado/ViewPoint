use vqa_k_gm_proto::{
    consensus_tiles_fast, consensus_tiles_scalar, estimate_global_motion_fast,
    estimate_global_motion_scalar,
};

struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    fn fill_bytes(&mut self, buf: &mut [u8]) {
        let mut i = 0;
        while i < buf.len() {
            let val = self.next_u64();
            let bytes = val.to_le_bytes();
            let take = (buf.len() - i).min(8);
            buf[i..i + take].copy_from_slice(&bytes[..take]);
            i += take;
        }
    }
}

#[test]
fn test_hand_checked_small_cases() {
    // 1. Flat frame
    let h = 32;
    let w = 32;
    let prev = vec![100u8; h * w];
    let cur = vec![100u8; h * w];
    assert_eq!(
        estimate_global_motion_fast(&prev, &cur, h, w, 8, 16),
        None
    );
    assert_eq!(
        estimate_global_motion_scalar(&prev, &cur, h, w, 8, 16),
        None
    );

    // 2. Simple shift
    let mut prev = vec![50u8; h * w];
    let mut cur = vec![50u8; h * w];

    // Create a distinctive block in prev at (y: 10..18, x: 10..18)
    for y in 10..18 {
        for x in 10..18 {
            prev[y * w + x] = 220;
        }
    }
    // Shifted by dx = 3, dy = 2 -> cur block at (y: 12..20, x: 13..21)
    // cur[y, x] = prev[y - 2, x - 3]
    for y in 12..20 {
        for x in 13..21 {
            cur[y * w + x] = 220;
        }
    }

    let gm_scalar = estimate_global_motion_scalar(&prev, &cur, h, w, 8, 16);
    let gm_fast = estimate_global_motion_fast(&prev, &cur, h, w, 8, 16);

    assert_eq!(gm_scalar, gm_fast);
    if let Some((dx, dy, score)) = gm_fast {
        assert_eq!((dx, dy), (3, 2));
        assert_eq!(score, 0);
    } else {
        panic!("Expected global motion result for hand-checked shift case");
    }

    // Check consensus tiles
    let mask_scalar = consensus_tiles_scalar(&prev, &cur, h, w, 16, 3, 2);
    let mask_fast = consensus_tiles_fast(&prev, &cur, h, w, 16, 3, 2);
    assert_eq!(mask_scalar, mask_fast);
}

#[test]
fn test_seeded_random_cases_scalar_equals_fast() {
    let mut rng = SplitMix64::new(0x123456789ABCDEF0);

    let test_dims = [(32, 32), (64, 48), (100, 80), (128, 128)];

    for (w, h) in test_dims {
        for trial in 0..5 {
            let mut prev = vec![0u8; w * h];
            let mut cur = vec![0u8; w * h];

            rng.fill_bytes(&mut prev);
            rng.fill_bytes(&mut cur);

            let max_d = (trial % 4 + 1) * 8;
            let k_top = 16;
            let s = 16;

            let gm_scalar = estimate_global_motion_scalar(&prev, &cur, h, w, max_d, k_top);
            let gm_fast = estimate_global_motion_fast(&prev, &cur, h, w, max_d, k_top);

            assert_eq!(
                gm_scalar, gm_fast,
                "Mismatch in estimate_global_motion for {}x{} trial {}",
                w, h, trial
            );

            let (dx, dy) = match gm_fast {
                Some((dx, dy, _)) => (dx, dy),
                None => (0, 0),
            };

            let mask_scalar = consensus_tiles_scalar(&prev, &cur, h, w, s, dx, dy);
            let mask_fast = consensus_tiles_fast(&prev, &cur, h, w, s, dx, dy);

            assert_eq!(
                mask_scalar, mask_fast,
                "Mismatch in consensus_tiles for {}x{} trial {}",
                w, h, trial
            );
        }
    }
}
