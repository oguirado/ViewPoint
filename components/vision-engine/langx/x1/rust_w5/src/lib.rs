//! LANG-X experiment X1 fluency seed rust_w5 implementation.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum X1Error {
    EDim,
    EStride,
    EShort,
    ERect,
    EGrid,
}

impl std::fmt::Display for X1Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            X1Error::EDim => write!(f, "E_DIM: dimensions outside valid range"),
            X1Error::EStride => write!(f, "E_STRIDE: stride below minimum required"),
            X1Error::EShort => write!(f, "E_SHORT: buffer length too short"),
            X1Error::ERect => write!(f, "E_RECT: invalid rectangle with negative dimension"),
            X1Error::EGrid => write!(f, "E_GRID: tile size not a power of two in range"),
        }
    }
}

impl std::error::Error for X1Error {}

/// Kernel K1: Repack BGRX32 to RGB24 with stride.
///
/// Converts a BGRX32 image buffer into an RGB24 image buffer.
///
/// # Arguments
/// * `src` - Shared slice containing source BGRX32 data.
/// * `w` - Image width in pixels (0 to 16384).
/// * `h` - Image height in pixels (0 to 16384).
/// * `src_stride` - Bytes per row in source buffer (must be >= 4 * w).
/// * `dst` - Mutable slice to write destination RGB24 data.
/// * `dst_stride` - Bytes per row in destination buffer (must be >= 3 * w).
pub fn repack_bgrx_to_rgb(
    src: &[u8],
    w: u32,
    h: u32,
    src_stride: usize,
    dst: &mut [u8],
    dst_stride: usize,
) -> Result<(), X1Error> {
    if w > 16384 || h > 16384 {
        return Err(X1Error::EDim);
    }

    if w == 0 || h == 0 {
        return Ok(());
    }

    let w_usize = w as usize;
    let h_usize = h as usize;

    let min_src_stride = w_usize.checked_mul(4).ok_or(X1Error::EStride)?;
    let min_dst_stride = w_usize.checked_mul(3).ok_or(X1Error::EStride)?;

    if src_stride < min_src_stride || dst_stride < min_dst_stride {
        return Err(X1Error::EStride);
    }

    let req_src_len = (h_usize - 1)
        .checked_mul(src_stride)
        .and_then(|v| v.checked_add(min_src_stride))
        .ok_or(X1Error::EShort)?;

    let req_dst_len = (h_usize - 1)
        .checked_mul(dst_stride)
        .and_then(|v| v.checked_add(min_dst_stride))
        .ok_or(X1Error::EShort)?;

    if src.len() < req_src_len || dst.len() < req_dst_len {
        return Err(X1Error::EShort);
    }

    for y in 0..h_usize {
        let src_row_offset = y * src_stride;
        let dst_row_offset = y * dst_stride;

        let src_row = &src[src_row_offset..src_row_offset + min_src_stride];
        let dst_row = &mut dst[dst_row_offset..dst_row_offset + min_dst_stride];

        for (src_pixel, dst_pixel) in src_row.chunks_exact(4).zip(dst_row.chunks_exact_mut(3)) {
            // src: [B, G, R, X]
            // dst: [R, G, B]
            dst_pixel[0] = src_pixel[2]; // R
            dst_pixel[1] = src_pixel[1]; // G
            dst_pixel[2] = src_pixel[0]; // B
        }
    }

    Ok(())
}

/// Kernel K2: Damage rectangles to tile bitset for grid size `s`.
///
/// # Arguments
/// * `w` - Screen width (1 to 16384).
/// * `h` - Screen height (1 to 16384).
/// * `s` - Tile size (power of two from 1 to 256).
/// * `rects` - List of rectangles (x, y, w, h) as signed 32-bit integers.
///
/// Returns a tuple `(Vec<u64>, u64)` containing the words of the bitset and total popcount.
pub fn damage_tiles(
    w: u32,
    h: u32,
    s: u32,
    rects: &[(i32, i32, i32, i32)],
) -> Result<(Vec<u64>, u64), X1Error> {
    if !(1..=16384).contains(&w) || !(1..=16384).contains(&h) {
        return Err(X1Error::EDim);
    }

    if !(1..=256).contains(&s) || !s.is_power_of_two() {
        return Err(X1Error::EGrid);
    }

    for &(_x, _y, rw, rh) in rects {
        if rw < 0 || rh < 0 {
            return Err(X1Error::ERect);
        }
    }

    let w_64 = w as i64;
    let h_64 = h as i64;
    let s_64 = s as i64;

    let gw = (w as usize + s as usize - 1) / (s as usize);
    let gh = (h as usize + s as usize - 1) / (s as usize);
    let total_tiles = gw * gh;
    let num_words = (total_tiles + 63) / 64;

    let mut words = vec![0u64; num_words];

    for &(x, y, rw, rh) in rects {
        let x0 = (x as i64).max(0);
        let y0 = (y as i64).max(0);
        let x1 = ((x as i64) + (rw as i64)).min(w_64);
        let y1 = ((y as i64) + (rh as i64)).min(h_64);

        if x1 <= x0 || y1 <= y0 {
            continue;
        }

        let tx_start = (x0 / s_64) as usize;
        let tx_end = ((x1 + s_64 - 1) / s_64) as usize;

        let ty_start = (y0 / s_64) as usize;
        let ty_end = ((y1 + s_64 - 1) / s_64) as usize;

        for ty in ty_start..ty_end {
            for tx in tx_start..tx_end {
                let tile_idx = ty * gw + tx;
                let word_idx = tile_idx / 64;
                let bit_idx = tile_idx % 64;
                words[word_idx] |= 1u64 << bit_idx;
            }
        }
    }

    let total_popcount: u64 = words.iter().map(|&w| w.count_ones() as u64).sum();

    Ok((words, total_popcount))
}

/// SplitMix64 pseudo-random generator state.
pub struct SplitMix64 {
    pub state: u64,
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
        let mut filled = 0;
        while filled < dest.len() {
            let val = self.next_u64();
            let bytes = val.to_le_bytes();
            let to_copy = (dest.len() - filled).min(8);
            dest[filled..filled + to_copy].copy_from_slice(&bytes[..to_copy]);
            filled += to_copy;
        }
    }
}

/// FNV-1a 64-bit hash over bytes.
pub fn fnv1a_64(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    let prime: u64 = 0x100000001b3;
    for &byte in bytes {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(prime);
    }
    hash
}

// C ABI exports

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
    if src.is_null() || dst.is_null() {
        return -3; // E_SHORT
    }

    // SAFETY: caller guarantees validity of pointers for specified lengths
    let src_slice = std::slice::from_raw_parts(src, src_len);
    let dst_slice = std::slice::from_raw_parts_mut(dst, dst_len);

    match repack_bgrx_to_rgb(src_slice, w, h, src_stride, dst_slice, dst_stride) {
        Ok(()) => 0,
        Err(X1Error::EDim) => -1,
        Err(X1Error::EStride) => -2,
        Err(X1Error::EShort) => -3,
        Err(X1Error::ERect) => -4,
        Err(X1Error::EGrid) => -5,
    }
}

#[no_mangle]
pub unsafe extern "C" fn x1_damage(
    w: u32,
    h: u32,
    s: u32,
    rects: *const i32,
    rect_count: usize,
    out_words: *mut u64,
    out_words_len: usize,
    out_popcount: *mut u64,
) -> i32 {
    if rect_count > 0 && rects.is_null() {
        return -4;
    }

    let rect_slice = if rect_count == 0 {
        &[]
    } else {
        // SAFETY: caller guarantees rects points to rect_count * 4 i32 values
        std::slice::from_raw_parts(rects as *const (i32, i32, i32, i32), rect_count)
    };

    match damage_tiles(w, h, s, rect_slice) {
        Ok((words, popcount)) => {
            if out_words.is_null() || out_words_len < words.len() {
                return -3; // E_SHORT
            }

            // SAFETY: out_words is non-null and valid for words.len()
            let out_slice = std::slice::from_raw_parts_mut(out_words, words.len());
            out_slice.copy_from_slice(&words);

            if !out_popcount.is_null() {
                *out_popcount = popcount;
            }

            0
        }
        Err(X1Error::EDim) => -1,
        Err(X1Error::EStride) => -2,
        Err(X1Error::EShort) => -3,
        Err(X1Error::ERect) => -4,
        Err(X1Error::EGrid) => -5,
    }
}
