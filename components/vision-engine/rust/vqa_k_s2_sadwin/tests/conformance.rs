use vqa_k_s2_sadwin::{tile_sad_windows, tile_sad_windows_scalar, SadWinError};

// SplitMix64 pseudo-random number generator for deterministic tests
struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }

    fn next_i16(&mut self) -> i16 {
        self.next_u64() as i16
    }

    fn range_i64(&mut self, min: i64, max: i64) -> i64 {
        let span = (max - min + 1) as u64;
        let val = self.next_u64() % span;
        min + val as i64
    }
}

#[test]
fn test_small_hand_checked_cases() {
    let h = 4;
    let w = 4;
    let s = 2;
    // 2x2 grid of 2x2 tiles
    let y_cur = vec![
        10, 20, 30, 40,
        50, 60, 70, 80,
        15, 25, 35, 45,
        55, 65, 75, 85,
    ];
    let y_prev = vec![
        10, 20, 30, 40,
        50, 60, 70, 80,
        15, 25, 35, 45,
        55, 65, 75, 85,
    ];

    let tx = vec![0, 1, 0, 1];
    let ty = vec![0, 0, 1, 1];

    let (sad_opt, count_opt) =
        tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();
    let (sad_sca, count_sca) =
        tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();

    assert_eq!(sad_opt, sad_sca);
    assert_eq!(count_opt, count_sca);
    assert_eq!(sad_opt, vec![0, 0, 0, 0]);
    assert_eq!(count_opt, vec![4, 4, 4, 4]);

    // Test shift (dx=1, dy=0)
    let (sad_opt2, count_opt2) =
        tile_sad_windows(&y_cur, &y_prev, h, w, 1, 0, s, &tx, &ty).unwrap();
    let (sad_sca2, count_sca2) =
        tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 1, 0, s, &tx, &ty).unwrap();

    assert_eq!(sad_opt2, sad_sca2);
    assert_eq!(count_opt2, count_sca2);
}

#[test]
fn test_empty_requests() {
    let h = 16;
    let w = 16;
    let s = 16;
    let y_cur = vec![0i16; h * w];
    let y_prev = vec![0i16; h * w];
    let tx = vec![];
    let ty = vec![];

    let (sad_opt, count_opt) =
        tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();
    let (sad_sca, count_sca) =
        tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();

    assert_eq!(sad_opt, sad_sca);
    assert_eq!(count_opt, count_sca);
    assert!(sad_opt.is_empty());
    assert!(count_opt.is_empty());
}

#[test]
fn test_int16_extremes() {
    let h = 2;
    let w = 2;
    let s = 2;
    let y_cur = vec![i16::MAX, i16::MIN, i16::MAX, i16::MIN];
    let y_prev = vec![i16::MIN, i16::MAX, i16::MIN, i16::MAX];

    let tx = vec![0];
    let ty = vec![0];

    let (sad_opt, count_opt) =
        tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();
    let (sad_sca, count_sca) =
        tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 0, 0, s, &tx, &ty).unwrap();

    assert_eq!(sad_opt, sad_sca);
    assert_eq!(count_opt, count_sca);

    // |32767 - (-32768)| = 65535 per pixel, 4 pixels = 262140
    assert_eq!(sad_opt, vec![262140]);
    assert_eq!(count_opt, vec![4]);
}

#[test]
fn test_out_of_bounds_and_negative_tile_indices() {
    let h = 8;
    let w = 8;
    let s = 4;
    let y_cur = vec![0i16; h * w];
    let y_prev = vec![0i16; h * w];

    // max_tx = 2, max_ty = 2
    // Valid: 0, 1, -1 (=1), -2 (=0)
    // Invalid: 2, -3, 3, -4

    let res_valid = tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &[0, -1], [0, -2].as_ref());
    assert!(res_valid.is_ok());

    let res_invalid = tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &[2], &[0]);
    assert_eq!(res_invalid, Err(SadWinError::IndexOutOfBounds));

    let res_invalid2 = tile_sad_windows(&y_cur, &y_prev, h, w, 0, 0, s, &[-3], &[0]);
    assert_eq!(res_invalid2, Err(SadWinError::IndexOutOfBounds));
}

#[test]
fn test_vectors_larger_than_tile_size() {
    let h = 16;
    let w = 16;
    let s = 4;
    let y_cur = vec![10i16; h * w];
    let y_prev = vec![10i16; h * w];

    let tx = vec![0, 1];
    let ty = vec![0, 1];

    // Vector larger than tile size s=4, e.g. dx=10, dy=-10
    let (sad_opt, count_opt) =
        tile_sad_windows(&y_cur, &y_prev, h, w, 10, -10, s, &tx, &ty).unwrap();
    let (sad_sca, count_sca) =
        tile_sad_windows_scalar(&y_cur, &y_prev, h, w, 10, -10, s, &tx, &ty).unwrap();

    assert_eq!(sad_opt, sad_sca);
    assert_eq!(count_opt, count_sca);
}

#[test]
fn test_seeded_random_conformance() {
    let mut rng = SplitMix64::new(0x5EED);

    for s in [16, 32] {
        let gh = 4;
        let gw = 4;
        let h = gh * s;
        let w = gw * s;

        let mut y_cur = vec![0i16; h * w];
        let mut y_prev = vec![0i16; h * w];

        for i in 0..y_cur.len() {
            y_cur[i] = rng.next_i16();
            y_prev[i] = rng.next_i16();
        }

        let max_tx = gw as i64;
        let max_ty = gh as i64;

        for _ in 0..50 {
            let dx = rng.range_i64(-(s as i64) - 5, (s as i64) + 5);
            let dy = rng.range_i64(-(s as i64) - 5, (s as i64) + 5);

            let n_req = rng.range_i64(0, 20) as usize;
            let mut tx = Vec::with_capacity(n_req);
            let mut ty = Vec::with_capacity(n_req);

            for _ in 0..n_req {
                tx.push(rng.range_i64(-max_tx, max_tx - 1));
                ty.push(rng.range_i64(-max_ty, max_ty - 1));
            }

            let (sad_opt, count_opt) =
                tile_sad_windows(&y_cur, &y_prev, h, w, dx, dy, s, &tx, &ty).unwrap();
            let (sad_sca, count_sca) =
                tile_sad_windows_scalar(&y_cur, &y_prev, h, w, dx, dy, s, &tx, &ty).unwrap();

            assert_eq!(sad_opt, sad_sca);
            assert_eq!(count_opt, count_sca);
        }
    }
}
