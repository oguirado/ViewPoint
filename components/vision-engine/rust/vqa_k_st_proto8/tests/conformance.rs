use vqa_k_st_proto8::{subtile_motion, subtile_motion_scalar};

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
}

fn make_frame(w: usize, h: usize, bg: u8) -> Vec<u8> {
    vec![bg; w * h]
}

#[test]
fn test_conformance_caret_moving_right() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = make_frame(w, h, bg);
    let mut f2 = make_frame(w, h, bg);

    // Caret 2x16 at x=8, y=8 in f1
    for y in 8..24 {
        for x in 8..10 {
            f1[y * w + x] = fg;
        }
    }
    // Moves 12 px right -> x=20, y=8 in f2
    for y in 8..24 {
        for x in 20..22 {
            f2[y * w + x] = fg;
        }
    }

    let scalar_res = subtile_motion_scalar(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);
    let opt_res = subtile_motion(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);

    assert_eq!(scalar_res, opt_res);
    assert!(opt_res.is_some());
    let (dx, dy, n_blocks) = opt_res.unwrap();
    assert_eq!((dx, dy), (12, 0));
    assert!(n_blocks > 0);
}

#[test]
fn test_conformance_word_moving_down() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = make_frame(w, h, bg);
    let mut f2 = make_frame(w, h, bg);

    // Word (24x10 block) at x=40, y=40 in tile (1,1)
    for y in 40..50 {
        for x in 40..64 {
            f1[y * w + x] = fg;
        }
    }
    // Moves 5 px down -> x=40, y=45
    for y in 45..55 {
        for x in 40..64 {
            f2[y * w + x] = fg;
        }
    }

    let scalar_res = subtile_motion_scalar(&f1, &f2, w as u32, h as u32, s, 1, 1, 16);
    let opt_res = subtile_motion(&f1, &f2, w as u32, h as u32, s, 1, 1, 16);

    assert_eq!(scalar_res, opt_res);
    assert!(opt_res.is_some());
    let (dx, dy, n_blocks) = opt_res.unwrap();
    assert_eq!((dx, dy), (0, 5));
    assert!(n_blocks > 0);
}

#[test]
fn test_conformance_shape_change() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = make_frame(w, h, bg);
    let mut f2 = make_frame(w, h, bg);

    for y in 8..24 {
        for x in 8..24 {
            f1[y * w + x] = fg;
            f2[y * w + x] = fg;
        }
    }
    for y in 12..20 {
        for x in 12..20 {
            f2[y * w + x] = 100;
        }
    }

    let scalar_res = subtile_motion_scalar(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);
    let opt_res = subtile_motion(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);

    assert_eq!(scalar_res, opt_res);
    assert_eq!(opt_res, None);
}

#[test]
fn test_conformance_move_crossing_tile_boundary() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = make_frame(w, h, bg);
    let mut f2 = make_frame(w, h, bg);

    for y in 8..24 {
        for x in 26..28 {
            f1[y * w + x] = fg;
        }
    }
    for y in 8..24 {
        for x in 38..40 {
            f2[y * w + x] = fg;
        }
    }

    let scalar_res = subtile_motion_scalar(&f1, &f2, w as u32, h as u32, s, 1, 0, 16);
    let opt_res = subtile_motion(&f1, &f2, w as u32, h as u32, s, 1, 0, 16);

    assert_eq!(scalar_res, opt_res);
    assert!(opt_res.is_some());
    let (dx, dy, n_blocks) = opt_res.unwrap();
    assert_eq!((dx, dy), (12, 0));
    assert!(n_blocks > 0);
}

#[test]
fn test_conformance_noise_free_bg() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;

    let f1 = make_frame(w, h, bg);
    let f2 = make_frame(w, h, bg);

    let scalar_res = subtile_motion_scalar(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);
    let opt_res = subtile_motion(&f1, &f2, w as u32, h as u32, s, 0, 0, 16);

    assert_eq!(scalar_res, opt_res);
    assert_eq!(opt_res, None);
}

#[test]
fn test_conformance_seeded_random_twin_verification() {
    let mut rng = SplitMix64::new(0x5EED);

    for idx in 0..20 {
        let w = (32 + (rng.next_u64() % 100)) as u32;
        let h = (32 + (rng.next_u64() % 100)) as u32;
        let s = if idx % 2 == 0 { 16 } else { 32 };
        let max_d = 15;

        let frame_size = (w * h) as usize;
        let mut f1 = vec![200u8; frame_size];
        let mut f2 = vec![200u8; frame_size];

        // Randomly modify background and add moving blocks
        for i in 0..frame_size {
            if rng.next_u64() % 20 == 0 {
                f1[i] = (rng.next_u64() % 256) as u8;
                f2[i] = f1[i];
            }
        }

        let obj_w = (4 + (rng.next_u64() % 16)) as usize;
        let obj_h = (4 + (rng.next_u64() % 16)) as usize;
        let x0 = (rng.next_u64() % (w as u64 - obj_w as u64)) as usize;
        let y0 = (rng.next_u64() % (h as u64 - obj_h as u64)) as usize;
        let dx = (rng.next_u64() % 10) as i32 - 5;
        let dy = (rng.next_u64() % 10) as i32 - 5;

        for r in 0..obj_h {
            for c in 0..obj_w {
                let idx1 = (y0 + r) * w as usize + (x0 + c);
                f1[idx1] = 50;

                let target_x = x0 as i32 + c as i32 + dx;
                let target_y = y0 as i32 + r as i32 + dy;
                if target_x >= 0 && target_x < w as i32 && target_y >= 0 && target_y < h as i32 {
                    let idx2 = target_y as usize * w as usize + target_x as usize;
                    f2[idx2] = 50;
                }
            }
        }

        let gw = (w + s - 1) / s;
        let gh = (h + s - 1) / s;

        for ty in 0..gh as i32 {
            for tx in 0..gw as i32 {
                let scalar_res = subtile_motion_scalar(&f1, &f2, w, h, s, tx, ty, max_d);
                let opt_res = subtile_motion(&f1, &f2, w, h, s, tx, ty, max_d);
                assert_eq!(
                    scalar_res, opt_res,
                    "Mismatch at test case {}, tile ({}, {})",
                    idx, tx, ty
                );
            }
        }
    }
}
