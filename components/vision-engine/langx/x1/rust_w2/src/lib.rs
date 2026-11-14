//! LANG-X Experiment X1 Rust Seed (langx_x1_rust_w2)

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum X1Error {
    EDim = -1,
    EStride = -2,
    EShort = -3,
    EGrid = -4,
    ERect = -5,
}

impl std::fmt::Display for X1Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            X1Error::EDim => write!(f, "E_DIM: dimensions out of range 0..=16384 (or 1..=16384 for damage)"),
            X1Error::EStride => write!(f, "E_STRIDE: src_stride < 4W or dst_stride < 3W"),
            X1Error::EShort => write!(f, "E_SHORT: buffer too short"),
            X1Error::EGrid => write!(f, "E_GRID: tile size not a power of 2 in range 1..=256"),
            X1Error::ERect => write!(f, "E_RECT: rectangle width or height negative"),
        }
    }
}

impl std::error::Error for X1Error {}

/// Kernel K1: BGRX32 to RGB24 repack with stride.
///
/// Converts BGRX pixels (4 bytes/pixel) to RGB (3 bytes/pixel).
/// Destination padding bytes between 3W and dst_stride are left untouched.
pub fn repack_bgrx_to_rgb(
    src: &[u8],
    w: usize,
    h: usize,
    src_stride: usize,
    dst: &mut [u8],
    dst_stride: usize,
) -> Result<(), X1Error> {
    if w > 16384 || h > 16384 {
        return Err(X1Error::EDim);
    }
    if src_stride < 4 * w || dst_stride < 3 * w {
        return Err(X1Error::EStride);
    }
    if w == 0 || h == 0 {
        return Ok(());
    }

    let min_src_len = (h - 1) * src_stride + 4 * w;
    let min_dst_len = (h - 1) * dst_stride + 3 * w;

    if src.len() < min_src_len || dst.len() < min_dst_len {
        return Err(X1Error::EShort);
    }

    for y in 0..h {
        let src_row = &src[y * src_stride..y * src_stride + 4 * w];
        let dst_row = &mut dst[y * dst_stride..y * dst_stride + 3 * w];

        for (src_chunk, dst_chunk) in src_row.chunks_exact(4).zip(dst_row.chunks_exact_mut(3)) {
            dst_chunk[0] = src_chunk[2]; // R
            dst_chunk[1] = src_chunk[1]; // G
            dst_chunk[2] = src_chunk[0]; // B
        }
    }

    Ok(())
}

/// Kernel K2: Damage rectangles to tile bitset for grid s.
pub fn damage_tiles(
    w: usize,
    h: usize,
    s: usize,
    rects: &[(i32, i32, i32, i32)],
) -> Result<(Vec<u64>, usize), X1Error> {
    if w < 1 || w > 16384 || h < 1 || h > 16384 {
        return Err(X1Error::EDim);
    }
    if s < 1 || s > 256 || !s.is_power_of_two() {
        return Err(X1Error::EGrid);
    }
    for &(_, _, rw, rh) in rects {
        if rw < 0 || rh < 0 {
            return Err(X1Error::ERect);
        }
    }

    let gw = (w + s - 1) / s;
    let gh = (h + s - 1) / s;
    let num_tiles = gw * gh;
    let num_words = (num_tiles + 63) / 64;

    let mut words = vec![0u64; num_words];

    let w_i64 = w as i64;
    let h_i64 = h as i64;
    let s_i64 = s as i64;

    for &(rx, ry, rw, rh) in rects {
        let x0 = (rx as i64).max(0);
        let y0 = (ry as i64).max(0);
        let x1 = ((rx as i64) + (rw as i64)).min(w_i64);
        let y1 = ((ry as i64) + (rh as i64)).min(h_i64);

        if x1 <= x0 || y1 <= y0 {
            continue;
        }

        let tx_start = (x0 / s_i64) as usize;
        let tx_end = ((x1 + s_i64 - 1) / s_i64) as usize;
        let ty_start = (y0 / s_i64) as usize;
        let ty_end = ((y1 + s_i64 - 1) / s_i64) as usize;

        for ty in ty_start..ty_end {
            for tx in tx_start..tx_end {
                let t = ty * gw + tx;
                let word_idx = t / 64;
                let bit_idx = t % 64;
                words[word_idx] |= 1u64 << bit_idx;
            }
        }
    }

    let popcount = words.iter().map(|w| w.count_ones() as usize).sum();

    Ok((words, popcount))
}

/// SplitMix64 PRNG implementation.
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

    pub fn fill_bytes(&mut self, buf: &mut [u8]) {
        let mut offset = 0;
        while offset < buf.len() {
            let val = self.next_u64();
            let bytes = val.to_le_bytes();
            let to_copy = (buf.len() - offset).min(8);
            buf[offset..offset + to_copy].copy_from_slice(&bytes[..to_copy]);
            offset += to_copy;
        }
    }
}

/// FNV-1a 64 bit hash implementation over bytes.
pub fn fnv1a_64(data: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x100000001b3;
    for &byte in data {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

// --- C ABI exports ---

/// # Safety
/// Caller must ensure `src` (if `src_len` > 0) and `dst` (if `dst_len` > 0) point to valid memory.
#[no_mangle]
pub unsafe extern "C" fn x1_repack(
    src: *const u8,
    src_len: usize,
    w: usize,
    h: usize,
    src_stride: usize,
    dst: *mut u8,
    dst_len: usize,
    dst_stride: usize,
) -> i32 {
    let src_slice = if src.is_null() || src_len == 0 {
        &[]
    } else {
        // SAFETY: guaranteed by caller that src points to src_len readable bytes
        unsafe { std::slice::from_raw_parts(src, src_len) }
    };

    let dst_slice = if dst.is_null() || dst_len == 0 {
        &mut []
    } else {
        // SAFETY: guaranteed by caller that dst points to dst_len writable bytes
        unsafe { std::slice::from_raw_parts_mut(dst, dst_len) }
    };

    match repack_bgrx_to_rgb(src_slice, w, h, src_stride, dst_slice, dst_stride) {
        Ok(()) => 0,
        Err(err) => err as i32,
    }
}

/// # Safety
/// Caller must ensure pointers are valid for their corresponding lengths and counts.
#[no_mangle]
pub unsafe extern "C" fn x1_damage(
    w: usize,
    h: usize,
    s: usize,
    rects: *const i32,
    num_rects: usize,
    out_words: *mut u64,
    out_words_cap: usize,
    out_words_len: *mut usize,
    out_popcount: *mut u64,
) -> i32 {
    let rects_slice: &[(i32, i32, i32, i32)] = if rects.is_null() || num_rects == 0 {
        &[]
    } else {
        // SAFETY: rects points to num_rects * 4 i32 values
        unsafe { std::slice::from_raw_parts(rects as *const (i32, i32, i32, i32), num_rects) }
    };

    match damage_tiles(w, h, s, rects_slice) {
        Ok((words, popcount)) => {
            if !out_words_len.is_null() {
                unsafe { *out_words_len = words.len() };
            }
            if !out_popcount.is_null() {
                unsafe { *out_popcount = popcount as u64 };
            }
            if !out_words.is_null() {
                if out_words_cap < words.len() {
                    return X1Error::EShort as i32;
                }
                unsafe {
                    std::ptr::copy_nonoverlapping(words.as_ptr(), out_words, words.len());
                }
            }
            0
        }
        Err(err) => err as i32,
    }
}
