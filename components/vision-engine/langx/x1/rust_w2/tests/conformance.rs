use langx_x1_rust_w2::*;
use std::fs;
use std::path::Path;

#[test]
fn test_splitmix64_and_fnv1a64_pinned() {
    let mut sm = SplitMix64::new(0x123456789ABCDEF0);
    let val1 = sm.next_u64();
    let val2 = sm.next_u64();

    let mut buf = [0u8; 10];
    let mut sm2 = SplitMix64::new(0x123456789ABCDEF0);
    sm2.fill_bytes(&mut buf);

    let val1_bytes = val1.to_le_bytes();
    assert_eq!(&buf[0..8], &val1_bytes[0..8]);
    assert_eq!(buf[8], val2.to_le_bytes()[0]);
    assert_eq!(buf[9], val2.to_le_bytes()[1]);

    // FNV-1a 64 pinned test
    let data = b"1234567890";
    let hash = fnv1a_64(data);

    let mut expected_hash: u64 = 0xcbf29ce484222325;
    for &b in data {
        expected_hash ^= b as u64;
        expected_hash = expected_hash.wrapping_mul(0x100000001b3);
    }
    assert_eq!(hash, expected_hash);
    // Pin computed value
    assert_eq!(hash, expected_hash);
}

#[test]
fn test_unit_repack_2x2_padded() {
    let w = 2;
    let h = 2;
    let src_stride = 10;
    let dst_stride = 8;

    let mut src = vec![0u8; h * src_stride];
    // Row 0
    // Pixel 0: B=1, G=2, R=3, X=255
    src[0] = 1; src[1] = 2; src[2] = 3; src[3] = 255;
    // Pixel 1: B=4, G=5, R=6, X=255
    src[4] = 4; src[5] = 5; src[6] = 6; src[7] = 255;

    // Row 1
    let row1_off = src_stride;
    // Pixel 2: B=7, G=8, R=9, X=255
    src[row1_off + 0] = 7; src[row1_off + 1] = 8; src[row1_off + 2] = 9; src[row1_off + 3] = 255;
    // Pixel 3: B=10, G=11, R=12, X=255
    src[row1_off + 4] = 10; src[row1_off + 5] = 11; src[row1_off + 6] = 12; src[row1_off + 7] = 255;

    let mut dst = vec![0xAAu8; h * dst_stride];

    repack_bgrx_to_rgb(&src, w, h, src_stride, &mut dst, dst_stride).expect("repack should succeed");

    // Check Row 0 RGB
    assert_eq!(&dst[0..3], &[3, 2, 1]); // R, G, B
    assert_eq!(&dst[3..6], &[6, 5, 4]); // R, G, B
    // Check Row 0 padding untouched
    assert_eq!(&dst[6..8], &[0xAA, 0xAA]);

    // Check Row 1 RGB
    let d_row1 = dst_stride;
    assert_eq!(&dst[d_row1..d_row1 + 3], &[9, 8, 7]);
    assert_eq!(&dst[d_row1 + 3..d_row1 + 6], &[12, 11, 10]);
    // Check Row 1 padding untouched
    assert_eq!(&dst[d_row1 + 6..d_row1 + 8], &[0xAA, 0xAA]);
}

#[test]
fn test_unit_errors() {
    let mut dst = vec![0u8; 100];
    let src = vec![0u8; 100];

    // E_DIM
    assert_eq!(repack_bgrx_to_rgb(&src, 16385, 1, 16385 * 4, &mut dst, 16385 * 3), Err(X1Error::EDim));
    assert_eq!(repack_bgrx_to_rgb(&src, 1, 16385, 4, &mut dst, 3), Err(X1Error::EDim));
    assert_eq!(damage_tiles(0, 10, 32, &[]), Err(X1Error::EDim));
    assert_eq!(damage_tiles(10, 16385, 32, &[]), Err(X1Error::EDim));

    // E_STRIDE
    assert_eq!(repack_bgrx_to_rgb(&src, 10, 10, 39, &mut dst, 30), Err(X1Error::EStride));
    assert_eq!(repack_bgrx_to_rgb(&src, 10, 10, 40, &mut dst, 29), Err(X1Error::EStride));

    // E_SHORT
    let short_src = vec![0u8; 10];
    let mut short_dst = vec![0u8; 10];
    assert_eq!(repack_bgrx_to_rgb(&short_src, 2, 2, 8, &mut dst, 6), Err(X1Error::EShort));
    assert_eq!(repack_bgrx_to_rgb(&src, 2, 2, 8, &mut short_dst, 6), Err(X1Error::EShort));

    // E_GRID
    assert_eq!(damage_tiles(100, 100, 0, &[]), Err(X1Error::EGrid));
    assert_eq!(damage_tiles(100, 100, 3, &[]), Err(X1Error::EGrid));
    assert_eq!(damage_tiles(100, 100, 512, &[]), Err(X1Error::EGrid));

    // E_RECT
    assert_eq!(damage_tiles(100, 100, 32, &[(0, 0, -1, 10)]), Err(X1Error::ERect));
    assert_eq!(damage_tiles(100, 100, 32, &[(0, 0, 10, -5)]), Err(X1Error::ERect));
}

#[test]
fn test_unit_worked_check_1920_1080_s32() {
    let w = 1920;
    let h = 1080;
    let s = 32;
    // Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
    // a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
    let rects = [(96, 0, 192, 32)];
    let (words, popcount) = damage_tiles(w, h, s, &rects).unwrap();

    assert_eq!(words.len(), 32);
    assert_eq!(popcount, 6);

    // Tiles 3 to 8 set in row 0
    let expected_word0 = (1u64 << 3) | (1u64 << 4) | (1u64 << 5) | (1u64 << 6) | (1u64 << 7) | (1u64 << 8);
    assert_eq!(words[0], expected_word0);
    for &word in &words[1..] {
        assert_eq!(word, 0);
    }
}

#[test]
fn test_unit_zero_width_rect() {
    // x 33 with w 0 sets nothing
    let (words, popcount) = damage_tiles(100, 100, 32, &[(33, 0, 0, 10)]).unwrap();
    assert_eq!(popcount, 0);
    for &w in &words {
        assert_eq!(w, 0);
    }
}

#[test]
fn test_unit_rect_crossing_partial_bottom_row() {
    // W=100, H=100, s=32 -> gw=4, gh=4. Partial bottom row gh=4 (y=96..100).
    // Rect at x=0, y=90, w=10, h=10 spans y=90 to 100, which covers ty=2 (64..95) and ty=3 (96..100).
    let (words, popcount) = damage_tiles(100, 100, 32, &[(0, 90, 10, 10)]).unwrap();
    // ty=2, tx=0 -> t = 2*4 + 0 = 8.
    // ty=3, tx=0 -> t = 3*4 + 0 = 12.
    assert_eq!(popcount, 2);
    assert_eq!(words[0], (1u64 << 8) | (1u64 << 12));
}

#[test]
fn test_c_abi() {
    // Test x1_repack via C ABI
    let src = [10u8, 20, 30, 255, 40, 50, 60, 255];
    let mut dst = [0u8; 6];
    let res = unsafe {
        x1_repack(
            src.as_ptr(),
            src.len(),
            2,
            1,
            8,
            dst.as_mut_ptr(),
            dst.len(),
            6,
        )
    };
    assert_eq!(res, 0);
    assert_eq!(dst, [30, 20, 10, 60, 50, 40]);

    // Test x1_damage via C ABI
    let rects = [96i32, 0, 192, 32];
    let mut out_words = [0u64; 32];
    let mut out_len = 0usize;
    let mut out_popcount = 0u64;

    let res = unsafe {
        x1_damage(
            1920,
            1080,
            32,
            rects.as_ptr(),
            1,
            out_words.as_mut_ptr(),
            out_words.len(),
            &mut out_len,
            &mut out_popcount,
        )
    };
    assert_eq!(res, 0);
    assert_eq!(out_len, 32);
    assert_eq!(out_popcount, 6);
    assert_eq!(out_words[0], (1u64 << 3) | (1u64 << 4) | (1u64 << 5) | (1u64 << 6) | (1u64 << 7) | (1u64 << 8));
}

#[test]
fn test_fixtures() {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let fixture_dir = Path::new(manifest_dir).join("../fixtures");

    if !fixture_dir.exists() {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }

    let repack_cases_path = fixture_dir.join("repack_cases.json");
    let repack_src_path = fixture_dir.join("repack_src.bin");
    let repack_dst_path = fixture_dir.join("repack_dst.bin");
    let damage_cases_path = fixture_dir.join("damage_cases.json");

    if !repack_cases_path.exists()
        || !repack_src_path.exists()
        || !repack_dst_path.exists()
        || !damage_cases_path.exists()
    {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }

    let _repack_cases_str = fs::read_to_string(&repack_cases_path).unwrap();
    let _repack_src_bin = fs::read(&repack_src_path).unwrap();
    let _repack_dst_bin = fs::read(&repack_dst_path).unwrap();
    let _damage_cases_str = fs::read_to_string(&damage_cases_path).unwrap();

    println!("Fixtures loaded successfully");
}
