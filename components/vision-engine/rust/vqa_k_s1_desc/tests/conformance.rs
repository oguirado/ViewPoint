use vqa_k_s1_desc::{
    compute_tile_optimized, compute_tile_scalar, isqrt, luma_of_padded, pad_frame, ycocg, Grid,
    TAU_EDGE,
};

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

    fn fill_bytes(&mut self, dest: &mut [u8]) {
        for chunk in dest.chunks_mut(8) {
            let val = self.next_u64().to_le_bytes();
            let len = chunk.len().min(8);
            chunk[..len].copy_from_slice(&val[..len]);
        }
    }
}

#[test]
fn test_isqrt_hand_checked() {
    assert_eq!(isqrt(0), 0);
    assert_eq!(isqrt(1), 1);
    assert_eq!(isqrt(2), 1);
    assert_eq!(isqrt(3), 1);
    assert_eq!(isqrt(4), 2);
    assert_eq!(isqrt(8), 2);
    assert_eq!(isqrt(9), 3);
    assert_eq!(isqrt(15), 3);
    assert_eq!(isqrt(16), 4);
    assert_eq!(isqrt(100), 10);
    assert_eq!(isqrt(101), 10);
    assert_eq!(isqrt((1i64 << 52) - 1), 67108863);
}

#[test]
fn test_ycocg_hand_checked() {
    let (y, co, cg) = ycocg(100, 150, 200);
    assert_eq!(co, -100);
    assert_eq!(cg, 0);
    assert_eq!(y, 150);

    let (y, co, cg) = ycocg(255, 0, 0);
    assert_eq!(co, 255);
    assert_eq!(cg, -127);
    assert_eq!(y, 63);
}

#[test]
fn test_uniform_tile_scalar_vs_optimized() {
    let w = 100u32;
    let h = 70u32;
    let grid = Grid::new(w, h).unwrap();
    assert_eq!(grid.s, 16);
    assert_eq!(grid.gw, 7);
    assert_eq!(grid.gh, 5);

    let frame = vec![128u8; (w * h * 3) as usize];
    let padded = pad_frame(&frame, w, h, &grid);
    let padded_w = (grid.gw * grid.s) as usize;
    let padded_h = (grid.gh * grid.s) as usize;
    let luma = luma_of_padded(&padded, padded_w, padded_h);

    for rank in 0..grid.n {
        let tx = grid.tx[rank] as usize;
        let ty = grid.ty[rank] as usize;
        let m = grid.mortons[rank];
        let is_pad = grid.is_pad_tile(rank);

        let scalar = compute_tile_scalar(
            &padded, padded_w, padded_h, &luma, 16, tx, ty, m, is_pad, TAU_EDGE,
        );
        let opt = compute_tile_optimized(
            &padded, padded_w, padded_h, &luma, 16, tx, ty, m, is_pad, TAU_EDGE,
        );

        assert_eq!(scalar, opt);
        assert_eq!(scalar.y_q4, 2048); // 128 * 16
        assert_eq!(scalar.co_h, 0);
        assert_eq!(scalar.cg_h, 0);
        assert_eq!(scalar.sig_q4, 0);
        assert_eq!(scalar.grad_q6, 0);
        assert_eq!(scalar.edge, 0);
        assert_eq!(scalar.sig64, 0);
    }
}

#[test]
fn test_seeded_random_cases() {
    let mut rng = SplitMix64::new(0x5EED);

    let test_dims = [(100, 70), (320, 240), (1280, 720), (1920, 1080)];

    for &(w, h) in &test_dims {
        let grid = Grid::new(w, h).unwrap();
        let mut frame = vec![0u8; (w * h * 3) as usize];
        rng.fill_bytes(&mut frame);

        let padded = pad_frame(&frame, w, h, &grid);
        let padded_w = (grid.gw * grid.s) as usize;
        let padded_h = (grid.gh * grid.s) as usize;
        let luma = luma_of_padded(&padded, padded_w, padded_h);
        let s = grid.s as usize;

        for rank in 0..grid.n {
            let tx = grid.tx[rank] as usize;
            let ty = grid.ty[rank] as usize;
            let m = grid.mortons[rank];
            let is_pad = grid.is_pad_tile(rank);

            let scalar = compute_tile_scalar(
                &padded, padded_w, padded_h, &luma, s, tx, ty, m, is_pad, TAU_EDGE,
            );
            let opt = compute_tile_optimized(
                &padded, padded_w, padded_h, &luma, s, tx, ty, m, is_pad, TAU_EDGE,
            );

            assert_eq!(scalar, opt, "Mismatch at dim {}x{}, rank {}", w, h, rank);
        }
    }
}
