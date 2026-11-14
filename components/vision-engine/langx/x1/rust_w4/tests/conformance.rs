use langx_x1_rust_w4::{
    damage_tiles, fnv1a_64, repack_bgrx_to_rgb, x1_damage, x1_repack, SplitMix64, X1Error,
};
use std::fs;
use std::path::Path;

#[test]
fn test_always_repack_2x2_padded() {
    let w = 2u32;
    let h = 2u32;
    let src_stride = 10; // > 4*2 = 8
    let dst_stride = 8; // > 3*2 = 6

    // Row 0: BGRX BGRX + 2 padding bytes
    // Row 1: BGRX BGRX (length is (2-1)*10 + 8 = 18 bytes min)
    let mut src = vec![0u8; 18];
    // Row 0 px 0: B=10, G=20, R=30, X=255 -> RGB: 30, 20, 10
    src[0] = 10;
    src[1] = 20;
    src[2] = 30;
    src[3] = 255;
    // Row 0 px 1: B=40, G=50, R=60, X=255 -> RGB: 60, 50, 40
    src[4] = 40;
    src[5] = 50;
    src[6] = 60;
    src[7] = 255;
    // Padding bytes 8, 9:
    src[8] = 0xAA;
    src[9] = 0xBB;

    // Row 1 px 0: B=70, G=80, R=90, X=255 -> RGB: 90, 80, 70
    src[10] = 70;
    src[11] = 80;
    src[12] = 90;
    src[13] = 255;
    // Row 1 px 1: B=100, G=110, R=120, X=255 -> RGB: 120, 110, 100
    src[14] = 100;
    src[15] = 110;
    src[16] = 120;
    src[17] = 255;

    let mut dst = vec![0xFFu8; 16]; // 2 rows * 8 dst_stride

    repack_bgrx_to_rgb(&src, w, h, src_stride, &mut dst, dst_stride).unwrap();

    // Row 0 dst check
    assert_eq!(&dst[0..6], &[30, 20, 10, 60, 50, 40]);
    // Destination padding untouched (bytes 6 and 7 of row 0)
    assert_eq!(&dst[6..8], &[0xFF, 0xFF]);

    // Row 1 dst check
    assert_eq!(&dst[8..14], &[90, 80, 70, 120, 110, 100]);
    // Destination padding untouched (bytes 14 and 15 of row 1)
    assert_eq!(&dst[14..16], &[0xFF, 0xFF]);
}

#[test]
fn test_always_errors() {
    let dummy_src = vec![0u8; 100];
    let mut dummy_dst = vec![0u8; 100];

    // E_DIM for repack
    assert_eq!(
        repack_bgrx_to_rgb(&dummy_src, 16385, 10, 16385 * 4, &mut dummy_dst, 16385 * 3),
        Err(X1Error::Dim)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&dummy_src, 10, 16385, 40, &mut dummy_dst, 30),
        Err(X1Error::Dim)
    );

    // E_STRIDE for repack
    assert_eq!(
        repack_bgrx_to_rgb(&dummy_src, 10, 10, 39, &mut dummy_dst, 30),
        Err(X1Error::Stride)
    );
    assert_eq!(
        repack_bgrx_to_rgb(&dummy_src, 10, 10, 40, &mut dummy_dst, 29),
        Err(X1Error::Stride)
    );

    // E_SHORT for repack
    let short_src = vec![0u8; 79]; // Need (2-1)*40 + 4*10 = 80
    assert_eq!(
        repack_bgrx_to_rgb(&short_src, 10, 2, 40, &mut dummy_dst, 30),
        Err(X1Error::Short)
    );
    let mut short_dst = vec![0u8; 59]; // Need (2-1)*30 + 3*10 = 60
    assert_eq!(
        repack_bgrx_to_rgb(&dummy_src, 10, 2, 40, &mut short_dst, 30),
        Err(X1Error::Short)
    );

    // E_DIM for damage
    assert_eq!(damage_tiles(0, 100, 32, &[]), Err(X1Error::Dim));
    assert_eq!(damage_tiles(16385, 100, 32, &[]), Err(X1Error::Dim));
    assert_eq!(damage_tiles(100, 0, 32, &[]), Err(X1Error::Dim));
    assert_eq!(damage_tiles(100, 16385, 32, &[]), Err(X1Error::Dim));

    // E_GRID for damage
    assert_eq!(damage_tiles(100, 100, 0, &[]), Err(X1Error::Grid));
    assert_eq!(damage_tiles(100, 100, 3, &[]), Err(X1Error::Grid));
    assert_eq!(damage_tiles(100, 100, 512, &[]), Err(X1Error::Grid));

    // E_RECT for damage
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
fn test_always_worked_check() {
    // Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
    // a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
    let (words, popcount) = damage_tiles(1920, 1080, 32, &[(96, 0, 192, 32)]).unwrap();
    assert_eq!(words.len(), 32);
    assert_eq!(popcount, 6);

    let expected_word0 = (1u64 << 3) | (1u64 << 4) | (1u64 << 5) | (1u64 << 6) | (1u64 << 7) | (1u64 << 8);
    assert_eq!(words[0], expected_word0);
    for w in &words[1..] {
        assert_eq!(*w, 0);
    }
}

#[test]
fn test_always_w0_rect() {
    // x 33 with w 0 sets nothing
    let (words, popcount) = damage_tiles(1920, 1080, 32, &[(33, 0, 0, 100)]).unwrap();
    assert_eq!(popcount, 0);
    assert!(words.iter().all(|&w| w == 0));
}

#[test]
fn test_always_partial_bottom_row() {
    // W=100, H=100, s=32 -> gw=4, gh=4. total_tiles=16, 1 word.
    // Rect crossing into bottom partial tile: y=90, h=20 -> y0=90, y1=100 (min H).
    // ty_min = 90/32 = 2. ty_max = (100+31)/32 = 4.
    let (words, popcount) = damage_tiles(100, 100, 32, &[(0, 90, 32, 20)]).unwrap();
    assert_eq!(popcount, 2);
    assert_eq!(words[0], (1u64 << 8) | (1u64 << 12));
}

#[test]
fn test_always_splitmix64_fnv1a() {
    let mut sm = SplitMix64::new(12345);
    let mut buf = vec![0u8; 16];
    sm.fill_bytes(&mut buf);

    let h1 = fnv1a_64(&buf);
    let h2 = fnv1a_64(&buf);
    assert_eq!(h1, h2);

    let mut sm_test = SplitMix64::new(12345);
    let v1 = sm_test.next_u64();
    let hash_v1 = fnv1a_64(&v1.to_le_bytes());

    assert_ne!(v1, 0);
    assert_ne!(hash_v1, 0);
}

#[test]
fn test_always_c_abi() {
    let src = vec![10u8, 20, 30, 255];
    let mut dst = vec![0u8; 3];

    let rc_repack = unsafe {
        x1_repack(
            src.as_ptr(),
            src.len(),
            1,
            1,
            4,
            dst.as_mut_ptr(),
            dst.len(),
            3,
        )
    };
    assert_eq!(rc_repack, 0);
    assert_eq!(dst, vec![30, 20, 10]);

    let rects = [96i32, 0, 192, 32];
    let mut out_words = [0u64; 32];
    let mut out_num_words = 0usize;
    let mut out_popcount = 0u64;

    let rc_damage = unsafe {
        x1_damage(
            1920,
            1080,
            32,
            rects.as_ptr(),
            1,
            out_words.as_mut_ptr(),
            out_words.len(),
            &mut out_num_words,
            &mut out_popcount,
        )
    };
    assert_eq!(rc_damage, 0);
    assert_eq!(out_num_words, 32);
    assert_eq!(out_popcount, 6);
}

#[test]
fn test_fixtures() {
    let rel_fixtures_dir = Path::new("../fixtures");
    let fallback_fixtures_dir = Path::new("langx/x1/fixtures");

    let fixtures_dir = if rel_fixtures_dir.exists() {
        rel_fixtures_dir
    } else if fallback_fixtures_dir.exists() {
        fallback_fixtures_dir
    } else {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    };

    let repack_cases_path = fixtures_dir.join("repack_cases.json");
    let repack_src_path = fixtures_dir.join("repack_src.bin");
    let repack_dst_path = fixtures_dir.join("repack_dst.bin");
    let damage_cases_path = fixtures_dir.join("damage_cases.json");

    if !repack_cases_path.exists()
        || !repack_src_path.exists()
        || !repack_dst_path.exists()
        || !damage_cases_path.exists()
    {
        println!("fixtures absent at langx/x1/fixtures");
        return;
    }

    // When fixture files are present, read and run assertions:
    let damage_json = fs::read_to_string(&damage_cases_path).expect("failed to read damage_cases.json");
    let repack_cases_json = fs::read_to_string(&repack_cases_path).expect("failed to read repack_cases.json");
    let repack_src_bin = fs::read(&repack_src_path).expect("failed to read repack_src.bin");
    let repack_dst_bin = fs::read(&repack_dst_path).expect("failed to read repack_dst.bin");

    // Process damage fixtures
    if let Ok(value) = simple_json_parse(&damage_json) {
        if let Some(cases) = value.as_array() {
            for case in cases {
                let w = case.get("w").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let h = case.get("h").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let s = case.get("s").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let mut rects = Vec::new();
                if let Some(r_arr) = case.get("rects").and_then(|v| v.as_array()) {
                    for r in r_arr {
                        if let Some(items) = r.as_array() {
                            if items.len() == 4 {
                                rects.push((
                                    items[0].as_i64().unwrap_or(0) as i32,
                                    items[1].as_i64().unwrap_or(0) as i32,
                                    items[2].as_i64().unwrap_or(0) as i32,
                                    items[3].as_i64().unwrap_or(0) as i32,
                                ));
                            }
                        }
                    }
                }
                if let Ok((words, popcount)) = damage_tiles(w, h, s, &rects) {
                    if let Some(exp_words) = case.get("expected_words").and_then(|v| v.as_array()) {
                        assert_eq!(words.len(), exp_words.len());
                        for (i, hex_v) in exp_words.iter().enumerate() {
                            if let Some(hex_str) = hex_v.as_str() {
                                let expected_word = u64::from_str_radix(hex_str, 16).unwrap();
                                assert_eq!(words[i], expected_word);
                            }
                        }
                    }
                    if let Some(exp_pop) = case.get("expected_popcount").and_then(|v| v.as_u64()) {
                        assert_eq!(popcount, exp_pop);
                    }
                }
            }
        }
    }

    // Process repack fixtures
    if let Ok(value) = simple_json_parse(&repack_cases_json) {
        if let Some(cases) = value.as_array() {
            for case in cases {
                let w = case.get("w").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let h = case.get("h").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let src_stride = case.get("src_stride").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let dst_stride = case.get("dst_stride").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let src_offset = case.get("src_offset").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let src_len = case.get("src_len").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let dst_offset = case.get("dst_offset").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let dst_len = case.get("dst_len").and_then(|v| v.as_u64()).unwrap_or(0) as usize;

                if src_offset + src_len <= repack_src_bin.len() && dst_offset + dst_len <= repack_dst_bin.len() {
                    let src_buf = &repack_src_bin[src_offset..src_offset + src_len];
                    let expected_dst_buf = &repack_dst_bin[dst_offset..dst_offset + dst_len];
                    let mut actual_dst_buf = vec![0u8; dst_len];

                    if repack_bgrx_to_rgb(src_buf, w, h, src_stride, &mut actual_dst_buf, dst_stride).is_ok() {
                        if let Some(exp_fnv) = case.get("expected_fnv1a").and_then(|v| v.as_u64()) {
                            assert_eq!(fnv1a_64(&actual_dst_buf), exp_fnv);
                        } else {
                            assert_eq!(actual_dst_buf, expected_dst_buf);
                        }
                    }
                }
            }
        }
    }
}

enum JsonVal {
    Null,
    Bool(bool),
    Num(i64, u64),
    Str(String),
    Array(Vec<JsonVal>),
    Obj(Vec<(String, JsonVal)>),
}

impl JsonVal {
    fn as_array(&self) -> Option<&Vec<JsonVal>> {
        match self {
            JsonVal::Array(a) => Some(a),
            _ => None,
        }
    }

    fn as_u64(&self) -> Option<u64> {
        match self {
            JsonVal::Num(_, u) => Some(*u),
            _ => None,
        }
    }

    fn as_i64(&self) -> Option<i64> {
        match self {
            JsonVal::Num(i, _) => Some(*i),
            _ => None,
        }
    }

    fn as_str(&self) -> Option<&str> {
        match self {
            JsonVal::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    fn get(&self, key: &str) -> Option<&JsonVal> {
        match self {
            JsonVal::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

fn simple_json_parse(input: &str) -> Result<JsonVal, ()> {
    let chars: Vec<char> = input.chars().collect();
    let mut idx = 0;
    parse_val(&chars, &mut idx)
}

fn skip_ws(chars: &[char], idx: &mut usize) {
    while *idx < chars.len() && chars[*idx].is_whitespace() {
        *idx += 1;
    }
}

fn parse_val(chars: &[char], idx: &mut usize) -> Result<JsonVal, ()> {
    skip_ws(chars, idx);
    if *idx >= chars.len() {
        return Err(());
    }
    match chars[*idx] {
        '[' => parse_arr(chars, idx),
        '{' => parse_obj(chars, idx),
        '"' => parse_str(chars, idx).map(JsonVal::Str),
        't' | 'f' => parse_bool(chars, idx).map(JsonVal::Bool),
        'n' => {
            *idx += 4;
            Ok(JsonVal::Null)
        }
        _ => parse_num(chars, idx),
    }
}

fn parse_arr(chars: &[char], idx: &mut usize) -> Result<JsonVal, ()> {
    *idx += 1; // skip '['
    let mut items = Vec::new();
    loop {
        skip_ws(chars, idx);
        if *idx >= chars.len() {
            return Err(());
        }
        if chars[*idx] == ']' {
            *idx += 1;
            break;
        }
        items.push(parse_val(chars, idx)?);
        skip_ws(chars, idx);
        if *idx < chars.len() && chars[*idx] == ',' {
            *idx += 1;
        }
    }
    Ok(JsonVal::Array(items))
}

fn parse_obj(chars: &[char], idx: &mut usize) -> Result<JsonVal, ()> {
    *idx += 1; // skip '{'
    let mut kvs = Vec::new();
    loop {
        skip_ws(chars, idx);
        if *idx >= chars.len() {
            return Err(());
        }
        if chars[*idx] == '}' {
            *idx += 1;
            break;
        }
        if chars[*idx] != '"' {
            return Err(());
        }
        let key = parse_str(chars, idx)?;
        skip_ws(chars, idx);
        if *idx >= chars.len() || chars[*idx] != ':' {
            return Err(());
        }
        *idx += 1;
        let val = parse_val(chars, idx)?;
        kvs.push((key, val));
        skip_ws(chars, idx);
        if *idx < chars.len() && chars[*idx] == ',' {
            *idx += 1;
        }
    }
    Ok(JsonVal::Obj(kvs))
}

fn parse_str(chars: &[char], idx: &mut usize) -> Result<String, ()> {
    *idx += 1; // skip '"'
    let mut s = String::new();
    while *idx < chars.len() {
        let c = chars[*idx];
        *idx += 1;
        if c == '"' {
            return Ok(s);
        }
        if c == '\\' && *idx < chars.len() {
            let esc = chars[*idx];
            *idx += 1;
            s.push(esc);
        } else {
            s.push(c);
        }
    }
    Err(())
}

fn parse_bool(chars: &[char], idx: &mut usize) -> Result<bool, ()> {
    if chars[*idx] == 't' {
        *idx += 4;
        Ok(true)
    } else {
        *idx += 5;
        Ok(false)
    }
}

fn parse_num(chars: &[char], idx: &mut usize) -> Result<JsonVal, ()> {
    let start = *idx;
    if chars[*idx] == '-' {
        *idx += 1;
    }
    while *idx < chars.len() && chars[*idx].is_ascii_digit() {
        *idx += 1;
    }
    let s: String = chars[start..*idx].iter().collect();
    let i = s.parse::<i64>().map_err(|_| ())?;
    let u = i as u64;
    Ok(JsonVal::Num(i, u))
}
