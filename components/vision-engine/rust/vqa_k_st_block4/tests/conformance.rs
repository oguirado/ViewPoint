use vqa_k_st_block4::{subtile_motion_opt, subtile_motion_scalar, grid_params};

#[test]
fn test_caret_moving_right() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = vec![bg; (w * h) as usize];
    let mut f2 = vec![bg; (w * h) as usize];

    // Caret 2x16 at x=40, y=40 in tile (1, 1)
    for y in 40..56 {
        for x in 40..42 {
            f1[y * w + x] = fg;
        }
    }
    // Moves 12 px right -> x=52, y=40
    for y in 40..56 {
        for x in 52..54 {
            f2[y * w + x] = fg;
        }
    }

    let res_scalar = subtile_motion_scalar(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);
    let res_opt = subtile_motion_opt(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);

    assert_eq!(res_scalar, res_opt);
    assert!(res_opt.is_some());
    let r = res_opt.unwrap();
    assert_eq!((r.dx, r.dy), (12, 0));
}

#[test]
fn test_caret_crossing_tile_border() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = vec![bg; (w * h) as usize];
    let mut f2 = vec![bg; (w * h) as usize];

    for y in 40..56 {
        for x in 58..60 {
            f1[y * w + x] = fg;
        }
    }
    for y in 40..56 {
        for x in 70..72 {
            f2[y * w + x] = fg;
        }
    }

    let res_scalar = subtile_motion_scalar(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);
    let res_opt = subtile_motion_opt(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);

    assert_eq!(res_scalar, res_opt);
    assert!(res_opt.is_some());
    let r = res_opt.unwrap();
    assert_eq!((r.dx, r.dy), (12, 0));
}

#[test]
fn test_window_drag() {
    let w = 128;
    let h = 128;
    let s = 32;
    let bg = 200u8;
    let fg = 50u8;

    let mut f1 = vec![bg; (w * h) as usize];
    let mut f2 = vec![bg; (w * h) as usize];

    for y in 40..70 {
        for x in 40..70 {
            f1[y * w + x] = fg;
        }
    }
    for y in 42..72 {
        for x in 44..74 {
            f2[y * w + x] = fg;
        }
    }

    let res_scalar = subtile_motion_scalar(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);
    let res_opt = subtile_motion_opt(&f1, &f2, w as i32, h as i32, Some(s), (1, 1), 16);

    assert_eq!(res_scalar, res_opt);
    assert!(res_opt.is_some());
    let r = res_opt.unwrap();
    assert_eq!((r.dx, r.dy), (4, 2));
}

#[test]
fn test_seeded_random_conformance() {
    // SplitMix64 generator
    let mut state = 0x5EEDu64;
    let mut next_u64 = || {
        state = state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    };

    let cases = vec![
        (128, 128, 32, 16),
        (128, 122, 32, 16),
        (64, 64, 16, 8),
        (192, 108, 16, 12),
    ];

    for (w, h, s, max_d) in cases {
        let mut f1 = vec![0u8; (w * h) as usize];
        let mut f2 = vec![0u8; (w * h) as usize];

        for i in 0..(w * h) as usize {
            f1[i] = (next_u64() & 0xFF) as u8;
            f2[i] = f1[i]; // start identical
        }

        // Apply random modifications to f2
        let n_mods = (next_u64() % 10) + 1;
        for _ in 0..n_mods {
            let rx = (next_u64() % (w as u64)) as usize;
            let ry = (next_u64() % (h as u64)) as usize;
            f2[ry * (w as usize) + rx] = (next_u64() & 0xFF) as u8;
        }

        let (_, gw, gh) = grid_params(w, h).unwrap();
        for ty in 0..gh {
            for tx in 0..gw {
                let res_scalar = subtile_motion_scalar(&f1, &f2, w, h, Some(s), (tx, ty), max_d);
                let res_opt = subtile_motion_opt(&f1, &f2, w, h, Some(s), (tx, ty), max_d);
                assert_eq!(
                    res_scalar, res_opt,
                    "Mismatch at tx={}, ty={}, w={}, h={}",
                    tx, ty, w, h
                );
            }
        }
    }
}
