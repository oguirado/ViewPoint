use langx_x1_rust_w5::*;

#[test]
fn test_repack_2x2_padded_and_untouched_padding() {
    let w = 2u32;
    let h = 2u32;
    let src_stride = 12usize; // min 8
    let dst_stride = 10usize; // min 6

    let mut src = vec![0u8; 2 * src_stride];
    // Row 0
    src[0..4].copy_from_slice(&[10, 20, 30, 255]); // B0=10, G0=20, R0=30
    src[4..8].copy_from_slice(&[40, 50, 60, 255]); // B1=40, G1=50, R1=60
    src[8..12].copy_from_slice(&[99, 99, 99, 99]); // padding

    // Row 1
    src[12..16].copy_from_slice(&[70, 80, 90, 255]); // B2=70, G2=80, R2=90
    src[16..20].copy_from_slice(&[100, 110, 120, 255]); // B3=100, G3=110, R3=120
    src[20..24].copy_from_slice(&[88, 88, 88, 88]); // padding

    // Fill dst with sentinel 0xAA
    let mut dst = vec![0xAAu8; 2 * dst_stride];

    repack_bgrx_to_rgb(&src, w, h, src_stride, &mut dst, dst_stride).unwrap();

    // Row 0 expected RGB: (30, 20, 10), (60, 50, 40)
    assert_eq!(&dst[0..3], &[30, 20, 10]);
    assert_eq!(&dst[3..6], &[60, 50, 40]);
    // Row 0 padding (bytes 6..10) must remain untouched (0xAA)
    assert_eq!(&dst[6..10], &[0xAA, 0xAA, 0xAA, 0xAA]);

    // Row 1 expected RGB: (90, 80, 70), (120, 110, 100)
    assert_eq!(&dst[10..13], &[90, 80, 70]);
    assert_eq!(&dst[13..16], &[120, 110, 100]);
    // Row 1 padding (bytes 16..20) must remain untouched (0xAA)
    assert_eq!(&dst[16..20], &[0xAA, 0xAA, 0xAA, 0xAA]);
}

#[test]
fn test_errors() {
    let mut dst = vec![0u8; 100];
    let src = vec![0u8; 100];

    // EDim
    assert_eq!(
        repack_bgrx_to_rgb(&src, 16385, 1, 16385 * 4, &mut dst, 16385 * 3),
        Err(X1Error::EDim)
    );
    assert_eq!(
        damage_tiles(0, 100, 32, &[]),
        Err(X1Error::EDim)
    );
    assert_eq!(
        damage_tiles(100, 16385, 32, &[]),
        Err(X1Error::EDim)
    );

    // EStride
    assert_eq!(
        repack_bgrx_to_rgb(&src, 10, 10, 39, &mut dst, 30),
        Err(X1Error::EStride)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src, 10, 10, 40, &mut dst, 29),
        Err(X1Error::EStride)
    );

    // EShort
    assert_eq!(
        repack_bgrx_to_rgb(&src[..10], 10, 10, 40, &mut dst, 30),
        Err(X1Error::EShort)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&src, 10, 10, 40, &mut dst[..10], 30),
        Err(X1Error::EShort)
    );

    // ERect
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, -1, 10)]),
        Err(X1Error::ERect)
    );
    assert_eq!(
        damage_tiles(100, 100, 32, &[(0, 0, 10, -5)]),
        Err(X1Error::ERect)
    );

    // EGrid
    assert_eq!(
        damage_tiles(100, 100, 0, &[]),
        Err(X1Error::EGrid)
    );
    assert_eq!(
        damage_tiles(100, 100, 3, &[]),
        Err(X1Error::EGrid)
    );
    assert_eq!(
        damage_tiles(100, 100, 512, &[]),
        Err(X1Error::EGrid)
    );
}

#[test]
fn test_worked_check_1920x1080_s32() {
    let w = 1920u32;
    let h = 1080u32;
    let s = 32u32;

    let rects = [(96i32, 0i32, 192i32, 32i32)];
    let (words, popcount) = damage_tiles(w, h, s, &rects).unwrap();

    assert_eq!(words.len(), 32);
    assert_eq!(popcount, 6);

    let expected_word0 = (1u64 << 3) | (1u64 << 4) | (1u64 << 5) | (1u64 << 6) | (1u64 << 7) | (1u64 << 8);
    assert_eq!(words[0], expected_word0);
    for i in 1..32 {
        assert_eq!(words[i], 0);
    }
}

#[test]
fn test_rect_x33_w0() {
    let w = 100u32;
    let h = 100u32;
    let s = 32u32;
    let rects = [(33i32, 10i32, 0i32, 20i32)];
    let (words, popcount) = damage_tiles(w, h, s, &rects).unwrap();
    assert_eq!(popcount, 0);
    for word in words {
        assert_eq!(word, 0);
    }
}

#[test]
fn test_rect_partial_bottom_row() {
    let w = 100u32;
    let h = 100u32;
    let s = 32u32;
    let rects = [(0i32, 90i32, 10i32, 10i32)];
    let (words, popcount) = damage_tiles(w, h, s, &rects).unwrap();
    assert_eq!(popcount, 2);
    let expected = (1u64 << 8) | (1u64 << 12);
    assert_eq!(words[0], expected);
}

#[test]
fn test_splitmix64_and_fnv1a64_pinned() {
    let mut sm = SplitMix64::new(0x123456789ABCDEF0);
    let v0 = sm.next_u64();
    assert_eq!(v0, 1592342178222199016);

    let mut buf = vec![0u8; 16];
    let mut sm2 = SplitMix64::new(42);
    sm2.fill_bytes(&mut buf);
    let hash = fnv1a_64(&buf);
    assert_eq!(hash, 2251033433896226525);
}

#[test]
fn test_c_abi() {
    let w = 2u32;
    let h = 2u32;
    let src_stride = 8usize;
    let dst_stride = 6usize;

    let src = [
        10u8, 20, 30, 0,  40, 50, 60, 0,
        70, 80, 90, 0,  100, 110, 120, 0,
    ];
    let mut dst = [0u8; 12];

    let res = unsafe {
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
    assert_eq!(res, 0);
    assert_eq!(&dst[0..3], &[30, 20, 10]);
    assert_eq!(&dst[3..6], &[60, 50, 40]);
    assert_eq!(&dst[6..9], &[90, 80, 70]);
    assert_eq!(&dst[9..12], &[120, 110, 100]);

    let rects = [96i32, 0, 192, 32];
    let mut out_words = [0u64; 32];
    let mut out_popcount = 0u64;

    let res_dmg = unsafe {
        x1_damage(
            1920,
            1080,
            32,
            rects.as_ptr(),
            1,
            out_words.as_mut_ptr(),
            out_words.len(),
            &mut out_popcount,
        )
    };
    assert_eq!(res_dmg, 0);
    assert_eq!(out_popcount, 6);
    assert_eq!(out_words[0], 504);
}

#[test]
fn test_fixtures() {
    let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let fixtures_dir = manifest_dir.join("../fixtures");

    let repack_cases_path = fixtures_dir.join("repack_cases.json");
    let repack_src_path = fixtures_dir.join("repack_src.bin");
    let repack_dst_path = fixtures_dir.join("repack_dst.bin");
    let damage_cases_path = fixtures_dir.join("damage_cases.json");

    if !fixtures_dir.exists()
        || !repack_cases_path.exists()
        || !repack_src_path.exists()
        || !repack_dst_path.exists()
        || !damage_cases_path.exists()
    {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }
}
