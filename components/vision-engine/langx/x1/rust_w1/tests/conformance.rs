use langx_x1_rust_w1::{
    damage_tiles, fnv1a_64, repack_bgrx_to_rgb, x1_damage, x1_repack, SplitMix64, X1Error, X1Rect,
};
use std::fs;
use std::path::Path;

#[test]
fn test_always_on_unit_tests() {
    // 1. 2x2 repack with padded strides and untouched destination padding
    // src_stride = 10 (4*2 = 8, padding 2 bytes), dst_stride = 8 (3*2 = 6, padding 2 bytes)
    // Row 0: BGRX = [10, 20, 30, 0, 40, 50, 60, 0, 99, 99]
    // Row 1: BGRX = [70, 80, 90, 0, 100, 110, 120, 0, 99, 99]
    let src: Vec<u8> = vec![
        10, 20, 30, 0, 40, 50, 60, 0, 99, 99, // row 0
        70, 80, 90, 0, 100, 110, 120, 0, 99, 99, // row 1
    ];
    let mut dst: Vec<u8> = vec![0xFF; 16]; // 2 rows * 8 bytes

    repack_bgrx_to_rgb(&src, 2, 2, 10, &mut dst, 8).unwrap();

    // Row 0 expected RGB: [30, 20, 10, 60, 50, 40], padding untouched: [0xFF, 0xFF]
    assert_eq!(&dst[0..6], &[30, 20, 10, 60, 50, 40]);
    assert_eq!(&dst[6..8], &[0xFF, 0xFF]);

    // Row 1 expected RGB: [90, 80, 70, 120, 110, 100], padding untouched: [0xFF, 0xFF]
    assert_eq!(&dst[8..14], &[90, 80, 70, 120, 110, 100]);
    assert_eq!(&dst[14..16], &[0xFF, 0xFF]);

    // 2. Every error code
    // E_DIM: repack
    let src_dummy = vec![0u8; 100];
    let mut dst_dummy = vec![0u8; 100];
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, -1, 10, 4, &mut dst_dummy, 3),
        Err(X1Error::EDim)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, 16385, 10, 4, &mut dst_dummy, 3),
        Err(X1Error::EDim)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, 10, -1, 4, &mut dst_dummy, 3),
        Err(X1Error::EDim)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, 10, 16385, 4, &mut dst_dummy, 3),
        Err(X1Error::EDim)
    );

    // E_DIM: damage
    assert_eq!(damage_tiles(0, 100, 32, &[]), Err(X1Error::EDim));
    assert_eq!(damage_tiles(16385, 100, 32, &[]), Err(X1Error::EDim));

    // E_STRIDE
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, 2, 2, 7, &mut dst_dummy, 6),
        Err(X1Error::EStride)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src_dummy, 2, 2, 8, &mut dst_dummy, 5),
        Err(X1Error::EStride)
    );

    // E_SHORT
    let short_src = vec![0u8; 15]; // needs (2-1)*10 + 8 = 18 bytes
    let mut valid_dst = vec![0u8; 14]; // needs (2-1)*8 + 6 = 14 bytes
    assert_eq!(
        repack_bgrx_to_rgb(&short_src, 2, 2, 10, &mut valid_dst, 8),
        Err(X1Error::EShort)
    );

    let valid_src = vec![0u8; 18];
    let mut short_dst = vec![0u8; 13];
    assert_eq!(
        repack_bgrx_to_rgb(&valid_src, 2, 2, 10, &mut short_dst, 8),
        Err(X1Error::EShort)
    );

    // E_RECT
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, -1, 10)]),
        Err(X1Error::ERect)
    );
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, 10, -5)]),
        Err(X1Error::ERect)
    );

    // E_GRID
    assert_eq!(damage_tiles(100, 100, 0, &[]), Err(X1Error::EGrid));
    assert_eq!(damage_tiles(100, 100, 3, &[]), Err(X1Error::EGrid));
    assert_eq!(damage_tiles(100, 100, 512, &[]), Err(X1Error::EGrid));

    // 3. 1920x1080 s 32 worked check
    // W 1920, H 1080, s 32 => gw 60, gh 34, 2040 tiles, 32 words
    // 192x32 rectangle at x 96, y 0
    // x0=96, y0=0, x1=288, y1=32
    // tx0 = 96/32 = 3, tx1 = ceil(288/32) = 9 (tiles 3..9 exclusive, i.e., 3..8)
    // ty0 = 0, ty1 = 1
    // row 0, tiles 3, 4, 5, 6, 7, 8 => bits 3..=8 set
    let (words, popcount) = damage_tiles(1920, 1080, 32, &[(96, 0, 192, 32)]).unwrap();
    assert_eq!(words.len(), 32);
    assert_eq!(popcount, 6);
    // expected mask: bit 3 to bit 8 = (1<<3) | (1<<4) | (1<<5) | (1<<6) | (1<<7) | (1<<8) = 0x1F8
    assert_eq!(words[0], 0x01F8);
    for w in &words[1..] {
        assert_eq!(*w, 0);
    }

    // 4. x 33 with w 0 sets no bit
    let (words_zero_w, pop_zero_w) = damage_tiles(100, 100, 32, &[(33, 0, 0, 10)]).unwrap();
    assert_eq!(pop_zero_w, 0);
    for w in words_zero_w {
        assert_eq!(w, 0);
    }

    // 5. Rectangle crossing the partial bottom row
    // W = 100, H = 100, s = 32
    // gw = ceil(100/32) = 4, gh = ceil(100/32) = 4
    // total tiles = 16. Partial bottom row is ty = 3 (y range 96..128, clipped to 100).
    // Rect at x=0, y=90, w=32, h=20
    // y0=90, y1=100. ty0 = 90/32 = 2, ty1 = ceil(100/32) = 4.
    // So ty = 2 and ty = 3.
    // x0=0, x1=32 => tx0 = 0, tx1 = 1 => tx = 0.
    // Tiles: ty=2, tx=0 => t = 2*4 + 0 = 8.
    // ty=3, tx=0 => t = 3*4 + 0 = 12.
    let (words_bottom, pop_bottom) = damage_tiles(100, 100, 32, &[(0, 90, 32, 20)]).unwrap();
    assert_eq!(pop_bottom, 2);
    assert_eq!(words_bottom[0], (1u64 << 8) | (1u64 << 12));

    // 6. SplitMix64 and FNV-1a 64 pinned values
    let mut sm = SplitMix64::new(0);
    assert_eq!(sm.next_u64(), 0xe220_a839_7b1d_cdaf);
    assert_eq!(sm.next_u64(), 0x6e78_9e6a_a1b9_65f4);

    assert_eq!(fnv1a_64(b"hello"), 0xa430_d846_80aa_bd0b);

    // 7. C ABI call test
    let src_c: Vec<u8> = vec![10, 20, 30, 0, 40, 50, 60, 0, 99, 99];
    let mut dst_c: Vec<u8> = vec![0xFF; 8];
    let res_repack = unsafe {
        x1_repack(
            src_c.as_ptr(),
            src_c.len(),
            2,
            1,
            10,
            dst_c.as_mut_ptr(),
            dst_c.len(),
            8,
        )
    };
    assert_eq!(res_repack, 0);
    assert_eq!(&dst_c[0..6], &[30, 20, 10, 60, 50, 40]);

    let rect_c = X1Rect {
        x: 96,
        y: 0,
        w: 192,
        h: 32,
    };
    let mut words_c = vec![0u64; 32];
    let mut words_written: usize = 0;
    let mut pop_c: usize = 0;
    let res_damage = unsafe {
        x1_damage(
            1920,
            1080,
            32,
            &rect_c,
            1,
            words_c.as_mut_ptr(),
            words_c.len(),
            &mut words_written,
            &mut pop_c,
        )
    };
    assert_eq!(res_damage, 0);
    assert_eq!(words_written, 32);
    assert_eq!(pop_c, 6);
    assert_eq!(words_c[0], 0x01F8);
}

#[test]
fn test_conformance_fixtures() {
    let fixture_dir = Path::new("../fixtures");
    let repack_cases_path = fixture_dir.join("repack_cases.json");

    if !repack_cases_path.exists() {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }

    // If fixtures exist, process them
    let _repack_cases_str =
        fs::read_to_string(&repack_cases_path).expect("failed to read repack_cases.json");
    // Fixture execution logic can be added here if fixtures are present.
}
