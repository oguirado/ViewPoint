use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum X1Error {
    Dim = -1,
    Stride = -2,
    Short = -3,
    Rect = -4,
    Grid = -5,
}

impl fmt::Display for X1Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            X1Error::Dim => write!(f, "E_DIM"),
            X1Error::Stride => write!(f, "E_STRIDE"),
            X1Error::Short => write!(f, "E_SHORT"),
            X1Error::Rect => write!(f, "E_RECT"),
            X1Error::Grid => write!(f, "E_GRID"),
        }
    }
}

impl std::error::Error for X1Error {}

/// Kernel K1: Repack BGRX32 to RGB24 with stride.
///
/// Converts a BGRX32 image buffer to RGB24 format.
pub fn repack_bgrx_to_rgb(
    src: &[u8],
    w: u32,
    h: u32,
    src_stride: usize,
    dst: &mut [u8],
    dst_stride: usize,
) -> Result<(), X1Error> {
    if w > 16384 || h > 16384 {
        return Err(X1Error::Dim);
    }

    let w_usize = w as usize;
    let h_usize = h as usize;

    let min_src_stride = w_usize.checked_mul(4).ok_or(X1Error::Stride)?;
    let min_dst_stride = w_usize.checked_mul(3).ok_or(X1Error::Stride)?;

    if src_stride < min_src_stride || dst_stride < min_dst_stride {
        return Err(X1Error::Stride);
    }

    if w == 0 || h == 0 {
        return Ok(());
    }

    let required_src = (h_usize - 1)
        .checked_mul(src_stride)
        .and_then(|val| val.checked_add(min_src_stride))
        .ok_or(X1Error::Short)?;

    let required_dst = (h_usize - 1)
        .checked_mul(dst_stride)
        .and_then(|val| val.checked_add(min_dst_stride))
        .ok_or(X1Error::Short)?;

    if src.len() < required_src || dst.len() < required_dst {
        return Err(X1Error::Short);
    }

    for y in 0..h_usize {
        let src_row_start = y * src_stride;
        let dst_row_start = y * dst_stride;

        let src_row = &src[src_row_start..src_row_start + min_src_stride];
        let dst_row = &mut dst[dst_row_start..dst_row_start + min_dst_stride];

        for x in 0..w_usize {
            let src_px = &src_row[x * 4..x * 4 + 4];
            let dst_px = &mut dst_row[x * 3..x * 3 + 3];

            dst_px[0] = src_px[2]; // R
            dst_px[1] = src_px[1]; // G
            dst_px[2] = src_px[0]; // B
        }
    }

    Ok(())
}

/// Kernel K2: Damage rectangles to a tile bitset for grid s.
pub fn damage_tiles(
    w: u32,
    h: u32,
    s: u32,
    rects: &[(i32, i32, i32, i32)],
) -> Result<(Vec<u64>, u64), X1Error> {
    if w < 1 || w > 16384 || h < 1 || h > 16384 {
        return Err(X1Error::Dim);
    }

    if s < 1 || s > 256 || !s.is_power_of_two() {
        return Err(X1Error::Grid);
    }

    for &(_, _, rw, rh) in rects {
        if rw < 0 || rh < 0 {
            return Err(X1Error::Rect);
        }
    }

    let w_64 = w as u64;
    let h_64 = h as u64;
    let s_64 = s as u64;

    let gw = (w_64 + s_64 - 1) / s_64;
    let gh = (h_64 + s_64 - 1) / s_64;

    let total_tiles = gw * gh;
    let num_words = (total_tiles + 63) / 64;

    let mut words = vec![0u64; num_words as usize];

    for &(x, y, rw, rh) in rects {
        let x0 = (x as i64).max(0) as u64;
        let y0 = (y as i64).max(0) as u64;

        let x1 = ((x as i64) + (rw as i64)).min(w as i64);
        let y1 = ((y as i64) + (rh as i64)).min(h as i64);

        if x1 <= (x0 as i64) || y1 <= (y0 as i64) {
            continue;
        }

        let x1 = x1 as u64;
        let y1 = y1 as u64;

        let tx_min = x0 / s_64;
        let tx_max = (x1 + s_64 - 1) / s_64;

        let ty_min = y0 / s_64;
        let ty_max = (y1 + s_64 - 1) / s_64;

        for ty in ty_min..ty_max {
            for tx in tx_min..tx_max {
                let t = ty * gw + tx;
                let word_idx = (t / 64) as usize;
                let bit_idx = (t % 64) as u32;

                words[word_idx] |= 1u64 << bit_idx;
            }
        }
    }

    let popcount: u64 = words.iter().map(|&word| word.count_ones() as u64).sum();

    Ok((words, popcount))
}

/// PRNG SplitMix64 generator for test fixtures/large data generation.
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    pub fn fill_bytes(&mut self, dest: &mut [u8]) {
        let mut i = 0;
        while i < dest.len() {
            let val = self.next_u64();
            let bytes = val.to_le_bytes();
            let take = (dest.len() - i).min(8);
            dest[i..i + take].copy_from_slice(&bytes[..take]);
            i += take;
        }
    }
}

/// FNV-1a 64 hash over bytes.
pub fn fnv1a_64(data: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x100000001b3;

    let mut hash = OFFSET_BASIS;
    for &byte in data {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

// C ABI wrappers

#[no_mangle]
pub unsafe extern "C" fn x1_repack(
    src: *const u8,
    src_len: usize,
    w: u32,
    h: u32,
    src_stride: usize,
    dst: *mut u8,
    dst_len: usize,
    dst_stride: usize,
) -> i32 {
    if (src.is_null() && src_len > 0) || (dst.is_null() && dst_len > 0) {
        return X1Error::Short as i32;
    }

    let src_slice = if src.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(src, src_len)
    };

    let dst_slice = if dst.is_null() {
        &mut []
    } else {
        std::slice::from_raw_parts_mut(dst, dst_len)
    };

    match repack_bgrx_to_rgb(src_slice, w, h, src_stride, dst_slice, dst_stride) {
        Ok(()) => 0,
        Err(e) => e as i32,
    }
}

#[no_mangle]
pub unsafe extern "C" fn x1_damage(
    w: u32,
    h: u32,
    s: u32,
    rects: *const i32,
    num_rects: usize,
    out_words: *mut u64,
    max_words: usize,
    out_num_words: *mut usize,
    out_popcount: *mut u64,
) -> i32 {
    let rects_slice = if num_rects == 0 || rects.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(rects as *const (i32, i32, i32, i32), num_rects)
    };

    match damage_tiles(w, h, s, rects_slice) {
        Ok((words, popcount)) => {
            if !out_num_words.is_null() {
                *out_num_words = words.len();
            }
            if !out_popcount.is_null() {
                *out_popcount = popcount;
            }
            if words.len() > max_words {
                return X1Error::Short as i32;
            }
            if !out_words.is_null() && !words.is_empty() {
                std::ptr::copy_nonoverlapping(words.as_ptr(), out_words, words.len());
            }
            0
        }
        Err(e) => e as i32,
    }
}
