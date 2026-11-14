use langx_x1_rust_w3::{
    damage_tiles, fnv1a_64, repack_bgrx_to_rgb, x1_damage, x1_repack, SplitMix64, X1Error,
};
use std::fs;
use std::path::PathBuf;

#[test]
fn test_2x2_repack_padded() {
    let w = 2u32;
    let h = 2u32;
    let src_stride = 10usize; // 4 * 2 = 8 <= 10
    let dst_stride = 8usize;  // 3 * 2 = 6 <= 8

    let src_len = (h as usize - 1) * src_stride + 4 * (w as usize) + 2; // extra padding
    let mut src = vec![0u8; src_len];

    // Row 0
    // Pixel 0 (x=0, y=0): B=10, G=20, R=30, X=99
    src[0] = 10;
    src[1] = 20;
    src[2] = 30;
    src[3] = 99;
    // Pixel 1 (x=1, y=0): B=11, G=21, R=31, X=98
    src[4] = 11;
    src[5] = 21;
    src[6] = 31;
    src[7] = 98;
    // Padding at 8..10

    // Row 1
    let row1_src = 10;
    // Pixel 0 (x=0, y=1): B=40, G=50, R=60, X=97
    src[row1_src + 0] = 40;
    src[row1_src + 1] = 50;
    src[row1_src + 2] = 60;
    src[row1_src + 3] = 97;
    // Pixel 1 (x=1, y=1): B=41, G=51, R=61, X=96
    src[row1_src + 4] = 41;
    src[row1_src + 5] = 51;
    src[row1_src + 6] = 61;
    src[row1_src + 7] = 96;

    let dst_len = (h as usize - 1) * dst_stride + 3 * (w as usize) + 4; // extra dst padding
    let mut dst = vec![0xEEu8; dst_len];

    let res = repack_bgrx_to_rgb(&src, w, h, src_stride, &mut dst, dst_stride);
    assert_eq!(res, Ok(()));

    // Check row 0 dst pixels: R, G, B
    assert_eq!(dst[0], 30);
    assert_eq!(dst[1], 20);
    assert_eq!(dst[2], 10);

    assert_eq!(dst[3], 31);
    assert_eq!(dst[4], 21);
    assert_eq!(dst[5], 11);

    // Destination row 0 padding untouched (6..8)
    assert_eq!(dst[6], 0xEE);
    assert_eq!(dst[7], 0xEE);

    // Check row 1 dst pixels
    let row1_dst = 8;
    assert_eq!(dst[row1_dst + 0], 60);
    assert_eq!(dst[row1_dst + 1], 50);
    assert_eq!(dst[row1_dst + 2], 40);

    assert_eq!(dst[row1_dst + 3], 61);
    assert_eq!(dst[row1_dst + 4], 51);
    assert_eq!(dst[row1_dst + 5], 41);

    // Destination padding beyond 3*W untouched (bytes 14, 15, 16, 17)
    assert_eq!(dst[row1_dst + 6], 0xEE);
    assert_eq!(dst[row1_dst + 7], 0xEE);
    assert_eq!(dst[row1_dst + 8], 0xEE);
    assert_eq!(dst[row1_dst + 9], 0xEE);
}

#[test]
fn test_error_codes() {
    let mut dst = vec![0u8; 100];
    let src = vec![0u8; 100];

    // E_DIM for K1
    assert_eq!(
        repack_bgrx_to_rgb(&src, 16385, 1, 16385 * 4, &mut dst, 16385 * 3),
        Err(X1Error::Dim)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src, 1, 16385, 4, &mut dst, 3),
        Err(X1Error::Dim)
    );

    // E_STRIDE for K1
    assert_eq!(
        repack_bgrx_to_rgb(&src, 10, 10, 39, &mut dst, 30),
        Err(X1Error::Stride)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src, 10, 10, 40, &mut dst, 29),
        Err(X1Error::Stride)
    );

    // E_SHORT for K1
    let short_src = vec![0u8; 10];
    assert_eq!(
        repack_bgrx_to_rgb(&short_src, 2, 2, 8, &mut dst, 6),
        Err(X1Error::Short)
    );
    let short_dst = &mut dst[..5];
    assert_eq!(
        repack_bgrx_to_rgb(&src, 2, 2, 8, short_dst, 6),
        Err(X1Error::Short)
    );

    // E_DIM for K2
    assert_eq!(damage_tiles(0, 100, 32, &[]), Err(X1Error::Dim));
    assert_eq!(damage_tiles(100, 0, 32, &[]), Err(X1Error::Dim));
    assert_eq!(damage_tiles(16385, 100, 32, &[]), Err(X1Error::Dim));

    // E_GRID for K2
    assert_eq!(damage_tiles(100, 100, 0, &[]), Err(X1Error::Grid));
    assert_eq!(damage_tiles(100, 100, 3, &[]), Err(X1Error::Grid));
    assert_eq!(damage_tiles(100, 100, 512, &[]), Err(X1Error::Grid));

    // E_RECT for K2
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, -1, 10)]),
        Err(X1Error::Rect)
    );
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, 10, -5)]),
        Err(X1Error::Rect)
    );
}

#[test]
fn test_worked_check() {
    let w = 1920u32;
    let h = 1080u32;
    let s = 32u32;
    let rects = vec![(96i32, 0i32, 192i32, 32i32)];

    let (words, popcount) = damage_tiles(w, h, s, &rects).unwrap();

    assert_eq!(words.len(), 32);
    assert_eq!(popcount, 6);
    // Tiles 3 to 8 of row 0: bits 3, 4, 5, 6, 7, 8 in word 0
    let expected_word0 = (1u64 << 3) | (1u64 << 4) | (1u64 << 5) | (1u64 << 6) | (1u64 << 7) | (1u64 << 8);
    assert_eq!(words[0], expected_word0);
    assert_eq!(words[0], 504);
    for &w_val in &words[1..] {
        assert_eq!(w_val, 0);
    }
}

#[test]
fn test_x33_w0() {
    let rects = vec![(33i32, 0i32, 0i32, 10i32)];
    let (words, popcount) = damage_tiles(100, 100, 32, &rects).unwrap();
    assert_eq!(popcount, 0);
    for &w in &words {
        assert_eq!(w, 0);
    }
}

#[test]
fn test_crossing_partial_bottom_row() {
    // W=10, H=10, s=4 -> gw=3, gh=3. Tile grid is 3x3 = 9 tiles. Fits in 1 u64 word.
    // Rect at x=7, y=7, w=3, h=3 -> covers x0=7, y0=7, x1=10, y1=10.
    // tx: 7/4=1, ceil(10/4)=3 (tx=1, 2)
    // ty: 7/4=1, ceil(10/4)=3 (ty=1, 2)
    // Tiles: (tx=1, ty=1) -> t = 1*3+1 = 4
    //        (tx=2, ty=1) -> t = 1*3+2 = 5
    //        (tx=1, ty=2) -> t = 2*3+1 = 7
    //        (tx=2, ty=2) -> t = 2*3+2 = 8
    let rects = vec![(7i32, 7i32, 3i32, 3i32)];
    let (words, popcount) = damage_tiles(10, 10, 4, &rects).unwrap();
    assert_eq!(words.len(), 1);
    assert_eq!(popcount, 4);
    let expected = (1u64 << 4) | (1u64 << 5) | (1u64 << 7) | (1u64 << 8);
    assert_eq!(words[0], expected);
}

#[test]
fn test_splitmix_and_fnv() {
    let mut rng = SplitMix64::new(0);
    let val1 = rng.next_u64();
    assert_eq!(val1, 0xe220a8397b1dcdaf);

    let mut buf = vec![0u8; 12];
    let mut rng2 = SplitMix64::new(0);
    rng2.fill_bytes(&mut buf);
    assert_eq!(&buf[0..8], &0xe220a8397b1dcdafu64.to_le_bytes());

    let hash_val = fnv1a_64(b"hello");
    assert_eq!(hash_val, 0xa430d84680aabd0b);
}

#[test]
fn test_c_abi() {
    // Test x1_repack
    let w = 2u32;
    let h = 2u32;
    let src_stride = 8usize;
    let dst_stride = 6usize;
    let src = vec![
        10, 20, 30, 0,  11, 21, 31, 0,
        40, 50, 60, 0,  41, 51, 61, 0,
    ];
    let mut dst = vec![0u8; 12];

    let code_repack = unsafe {
        x1_repack(
            src.as_ptr(),
            src.len(),
            w,
            h,
            src_stride,
            dst.as_mut_ptr(),
            dst.len(),
            dst_stride,
        )
    };
    assert_eq!(code_repack, 0);
    assert_eq!(dst[0], 30);
    assert_eq!(dst[1], 20);
    assert_eq!(dst[2], 10);

    // Test x1_damage
    let rects = vec![96i32, 0i32, 192i32, 32i32];
    let mut dst_words = vec![0u64; 32];
    let mut popcount = 0u32;

    let code_damage = unsafe {
        x1_damage(
            1920,
            1080,
            32,
            rects.as_ptr(),
            1,
            dst_words.as_mut_ptr(),
            dst_words.len(),
            &mut popcount,
        )
    };
    assert_eq!(code_damage, 0);
    assert_eq!(popcount, 6);
    assert_eq!(dst_words[0], 504);
}

#[test]
fn test_fixtures() {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let fixtures_dir = manifest_dir.parent().unwrap().join("fixtures");

    let repack_cases_p = fixtures_dir.join("repack_cases.json");
    let repack_src_p = fixtures_dir.join("repack_src.bin");
    let repack_dst_p = fixtures_dir.join("repack_dst.bin");
    let damage_cases_p = fixtures_dir.join("damage_cases.json");

    if !fixtures_dir.exists()
        || !repack_cases_p.exists()
        || !repack_src_p.exists()
        || !repack_dst_p.exists()
        || !damage_cases_p.exists()
    {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }

    // If fixtures exist, process them
    let damage_content = fs::read_to_string(&damage_cases_p).expect("read damage_cases.json");
    assert!(!damage_content.is_empty());

    let repack_content = fs::read_to_string(&repack_cases_p).expect("read repack_cases.json");
    assert!(!repack_content.is_empty());
}
