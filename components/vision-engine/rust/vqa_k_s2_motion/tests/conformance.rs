// rust/vqa_k_s2_motion/tests/conformance.rs

use vqa_k_s2_motion::*;

struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        SplitMix64 { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    fn next_i16(&mut self) -> i16 {
        (self.next_u64() & 0xFFFF) as i16
    }

    fn next_bool(&mut self) -> bool {
        (self.next_u64() & 1) == 1
    }
}

#[test]
fn test_small_hand_checked_axis_shift() {
    let cur = vec![10, 20, 30, 40, 50];
    let prev = vec![5, 10, 20, 30, 40];
    let res = axis_shift(&cur, &prev, 4);
    let res_s = axis_shift_scalar(&cur, &prev, 4);
    assert_eq!(res, res_s);
}

#[test]
fn test_small_hand_checked_islands() {
    let mask = vec![
        true, false, true, false,
        false, false, true, false,
        true, true, false, false,
        false, false, false, true,
    ];
    let (lab, n) = islands(&mask, 4, 4);
    let (lab_s, n_s) = islands_scalar(&mask, 4, 4);
    assert_eq!(lab, lab_s);
    assert_eq!(n, n_s);
}

#[test]
fn test_seeded_random_conformance() {
    let mut rng = SplitMix64::new(0x5EED);

    for _ in 0..20 {
        let width = 64;
        let height = 48;
        let s = 16;
        let gh = height / s;
        let gw = width / s;

        let mut y_cur = vec![0i16; width * height];
        let mut y_prev = vec![0i16; width * height];
        for i in 0..width * height {
            y_cur[i] = rng.next_i16();
            y_prev[i] = rng.next_i16();
        }

        let mut mask = vec![false; gh * gw];
        for i in 0..gh * gw {
            mask[i] = rng.next_bool();
        }

        let gv = global_vector(&y_cur, &y_prev, width, height, s);
        let gv_s = global_vector_scalar(&y_cur, &y_prev, width, height, s);
        assert_eq!(gv, gv_s);

        let (lab, n) = islands(&mask, gh, gw);
        let (lab_s, n_s) = islands_scalar(&mask, gh, gw);
        assert_eq!(lab, lab_s);
        assert_eq!(n, n_s);

        let iv = island_vector(&mask, &y_cur, &y_prev, width, height, s, gh, gw);
        let iv_s = island_vector_scalar(&mask, &y_cur, &y_prev, width, height, s, gh, gw);
        assert_eq!(iv, iv_s);
    }
}
